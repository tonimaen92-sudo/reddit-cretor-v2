# UTF-8
"""Anthropic-assisted recovery for Creator phone-workflow failures.

The model never receives Python, a PC shell, arbitrary URLs, or raw secrets.
Ordinary screen controls are frame-bound and destructive/sensitive elements
cannot be tapped directly.  A caller may additionally expose narrowly scoped
high-level operations (for example verified sole-container deletion); local
deterministic code performs and verifies those operations.  Every mutation is
followed by a fresh sanitized observation.  Learned recipes live in
LOCALAPPDATA rather than the OneDrive checkout.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import random
import re
import sqlite3
import threading
import time
import traceback
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from friend_poster.core import diaglog
from friend_poster.core.config import (
    GHOST_BUNDLE,
    LOG_DIR,
    REDDIT_BUNDLE,
    SECRETS_PATH,
    SETTINGS_BUNDLE,
)
from friend_poster.device.ssh_device import PhoneError, is_recoverable_bridge_failure
from friend_poster.device.ui_tree import Node, OperationCancelled, parse_nodes

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"
SCHEMA_VERSION = 3
MAX_API_CALLS_PER_RECOVERY_CHAIN = 1
_MODEL_IMAGE_MAX_SIZE = (750, 1334)
_MODEL_IMAGE_MAX_BYTES = 700_000

_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_IP_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
_DATE_RE = re.compile(
    r"(?<!\d)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})(?!\d)"
)
_CODE_RE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
_UDID_RE = re.compile(r"(?i)\b[0-9a-f]{20,40}\b")
_USERNAME_RE = re.compile(
    r"(?i)(?<![A-Z0-9_])u/[A-Z0-9_-]{3,32}\b"
    r"|(?<![\w@])@[A-Z0-9_][A-Z0-9_.-]{2,31}\b"
    r"|\b(?:username|user[ -]?name|reddit user)\s*(?:[:=]|is)\s*"
    r"@?[A-Z0-9][A-Z0-9._-]{2,63}\b"
)
_TIME_OR_PERCENT_RE = re.compile(r"^(?:\d{1,2}:\d{2}|\d{1,3}%|\d+)$")
_SPACE_RE = re.compile(r"\s+")

_SENSITIVE_HINTS = (
    "password", "passcode", "verification code", "security code", "email",
    "e-mail", "username", "birth", "date of birth", "phone number", "proxy",
    "token", "secret", "api key", "udid", "one-time", "otp",
)
_DESTRUCTIVE_HINTS = (
    "delete", "remove", "erase", "reset", "sign up", "create account",
    "continue with apple", "submit", "confirm purchase", "log out", "logout",
    "deactivate", "stop using", "turn off", "new container", "create container",
)
_KEYBOARD_TYPES = ("keyboard", "key", "securetextfield", "textfield", "textview")
_NON_UI_FAILURE_HINTS = (
    "checkpoint could not be saved", "could not be persisted", "permissionerror",
    "access is denied", "winerror", "ssh", "transport", "connection refused",
    "connection reset", "timed out connecting", "malformed saved", "history is malformed",
    "checkpoint disappeared", "birthday does not match", "saved username differs",
    "already verified deleted", "no validated birthday",
)

_WORKFLOW_TOOL_SCOPES: dict[str, frozenset[str]] = {
    "fill_prepared_field": frozenset(
        {"creation_pipeline", "icloud_unlock", "account_form"}
    ),
    "retry_icloud_unlock": frozenset({"creation_pipeline", "icloud_unlock"}),
    "verified_dismiss_icloud_sheet": frozenset(
        {"creation_pipeline", "icloud_unlock"}
    ),
    # Kept in the generic agent registry for backward-compatible unit-level
    # validation.  The Creator no longer sends deterministic PhoneError paths
    # to AI, so these handlers are not exposed by the production controller.
    "verified_delete_only_container": frozenset(
        {"container_cleanup", "full_reset"}
    ),
    "verified_create_select_container": frozenset(
        {"container_setup", "full_reset"}
    ),
    "verified_rotate_ip": frozenset({"ip_rotation", "full_reset"}),
    "verified_select_existing_container": frozenset({"container_selection"}),
    "discard_restart_current_attempt": frozenset(
        {
            "creation_pipeline", "container_setup", "container_selection",
            "icloud_unlock", "full_reset"
        }
    ),
}


class RecoveryStopped(Exception):
    """The user pressed Stop while recovery was active."""


class RecoveryBudgetExhausted(RuntimeError):
    """No further Anthropic request is allowed for this recovery attempt."""

    def __init__(self, used_calls: int, max_calls: int):
        self.used_calls = int(used_calls)
        self.max_calls = int(max_calls)
        super().__init__(
            "AI recovery API call limit reached before a locally verified "
            f"postcondition ({self.used_calls}/{self.max_calls} requests used "
            "in this attempt)"
        )


class UnknownVisualStateError(PhoneError):
    """A truly unknown screen after every deterministic local handler failed.

    This is intentionally the only PhoneError subtype eligible for paid visual
    recovery.  Deterministic cleanup, container, username, checkpoint and
    transport failures must pause locally instead of being sent to Anthropic.
    """

    ai_recoverable = True


class RecipeDiverged(Exception):
    """A cached action did not produce its previously verified next screen."""


class AnthropicConversationError(RuntimeError):
    """The local Anthropic conversation needs to be rebuilt before retrying."""


class AnthropicResponseError(RuntimeError):
    """Anthropic returned a malformed tool-use response; request a fresh turn."""


@dataclass
class Observation:
    frame_id: str
    fingerprint: str
    semantic_key: str
    screen_class: str
    nodes: list[Node]
    elements: dict[str, Node]
    public_nodes: list[dict[str, Any]]
    png: bytes
    redacted_png: bytes
    redacted_error: str
    # Default preserves compatibility with callers constructing observations
    # directly; live observations always populate the screen-only key.
    screen_semantic_key: str = ""

    def text_payload(self) -> str:
        return json.dumps(
            {
                "frame_id": self.frame_id,
                "screen_fingerprint": self.fingerprint,
                "screen_class": self.screen_class,
                "elements": self.public_nodes,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass
class RecoverySession:
    incident_fingerprint: str
    final_fingerprint: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    expected_fingerprints: list[str] = field(default_factory=list)
    model: str = ""
    source: str = "anthropic"
    directive: str = ""
    incident_semantic_key: str = ""
    final_semantic_key: str = ""


@dataclass
class RecoveryAttemptBudget:
    """Thread-safe paid-request budget shared by every phase of one attempt."""

    max_calls: int = MAX_API_CALLS_PER_RECOVERY_CHAIN
    used_calls: int = 0
    _lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.max_calls = int(self.max_calls)
        self.used_calls = int(self.used_calls)
        if self.max_calls < 1:
            raise ValueError("recovery attempt budget max_calls must be positive")
        if not 0 <= self.used_calls <= self.max_calls:
            raise ValueError(
                "recovery attempt budget used_calls must be between zero and max_calls"
            )

    @property
    def remaining_calls(self) -> int:
        with self._lock:
            return max(0, self.max_calls - self.used_calls)

    @property
    def api_calls(self) -> int:
        """Compatibility/readability alias for the number of reserved requests."""
        with self._lock:
            return self.used_calls

    def reserve(self) -> int:
        """Reserve one real HTTP request or fail before it can be sent."""
        with self._lock:
            if self.used_calls >= self.max_calls:
                raise RecoveryBudgetExhausted(self.used_calls, self.max_calls)
            self.used_calls += 1
            return self.used_calls


@dataclass
class RecoveryChain:
    """In-memory recovery state that survives transport reconnects.

    Full fingerprints deliberately remain recipe-cache keys.  Loop prevention
    uses the semantic screen key instead, so harmless screenshot pixel changes
    cannot reset the same-screen action guard.  The controller owns promotion
    of ``pending_sessions`` after a deterministic postcondition is proven.
    """

    action_pairs: set[tuple[str, str]] = field(default_factory=set)
    history: list[str] = field(default_factory=list)
    pending_sessions: list[RecoverySession] = field(default_factory=list)
    attempt_budget: RecoveryAttemptBudget | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def api_calls(self) -> int:
        """Backward-compatible view of the budget shared by this chain."""
        return self.attempt_budget.api_calls if self.attempt_budget is not None else 0


@dataclass(frozen=True)
class RecoveryDirective:
    """Request that the owning deterministic supervisor perform a transition."""

    name: str
    detail: str = ""


@dataclass(frozen=True)
class RecoveryWorkflowTool:
    """One deterministic operation authorized for a specific recovery scope.

    ``handler`` receives only the validated, non-secret tool arguments and
    must raise ``PhoneError`` (or another exception) unless its own
    postcondition is verified.  Returned text is sanitized before it is sent
    to the model.
    """

    description: str
    handler: Callable[[dict[str, Any]], Any]
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"frame_id": {"type": "string"}},
            "required": ["frame_id"],
            "additionalProperties": False,
        }
    )
    cacheable: bool = True


def load_anthropic_settings() -> tuple[str, str]:
    """Re-read the ignored secrets file on every call; never log the key."""
    data: dict[str, Any] = {}
    try:
        if os.path.isfile(SECRETS_PATH):
            with open(SECRETS_PATH, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data = loaded
    except (OSError, ValueError, TypeError):
        data = {}
    key = str(data.get("anthropic_api_key") or "").strip()
    if not key or "paste_" in key.casefold() or "your-key" in key.casefold():
        key = str(os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    model = str(
        data.get("anthropic_recovery_model")
        or os.environ.get("ANTHROPIC_RECOVERY_MODEL")
        or DEFAULT_MODEL
    ).strip()
    return key, model or DEFAULT_MODEL


def load_anthropic_workspace_id() -> str:
    """Return the optional workspace scope without ever logging credentials."""
    data: dict[str, Any] = {}
    try:
        if os.path.isfile(SECRETS_PATH):
            with open(SECRETS_PATH, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data = loaded
    except (OSError, ValueError, TypeError):
        data = {}
    return str(
        data.get("anthropic_workspace_id")
        or os.environ.get("ANTHROPIC_WORKSPACE_ID")
        or ""
    ).strip()


def _anthropic_error_detail(response: Any) -> str:
    """Extract a bounded, redacted API error without exposing request secrets."""
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("type") or "")
            elif error:
                detail = str(error)
    except Exception:
        detail = ""
    return redact_for_model(detail)[:240]


def _normalize_image_block(block: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded image shape accepted by the Messages API."""
    source = block.get("source")
    if not isinstance(source, dict):
        raise AnthropicConversationError("image content is missing its source object")
    source_type = str(source.get("type") or "").strip()
    media_type = str(source.get("media_type") or "").strip().casefold()
    data = source.get("data")
    if source_type != "base64":
        raise AnthropicConversationError("only base64 recovery images are supported")
    if media_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        raise AnthropicConversationError("recovery image has an unsupported media type")
    if not isinstance(data, str) or not data.strip():
        raise AnthropicConversationError("recovery image has no base64 data")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def _build_tool_result(
    tool_use_id: object,
    content: object,
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    """Build one schema-safe client tool result.

    Anthropic currently requires an ``is_error=true`` result to contain text
    only.  Success results may retain the sanitized observation image.  This
    single builder is also used by the API-boundary repair pass so a future
    call site cannot reintroduce an image-bearing error result.
    """
    result_id = str(tool_use_id or "").strip()
    if not result_id:
        raise AnthropicConversationError("tool_result has an empty tool_use_id")

    if isinstance(content, str):
        raw_blocks: list[object] = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        raw_blocks = list(content)
    elif content is None:
        raw_blocks = []
    else:
        raw_blocks = [{"type": "text", "text": str(content)}]

    text_blocks: list[dict[str, Any]] = []
    success_blocks: list[dict[str, Any]] = []
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            if is_error:
                text = str(raw).strip()
                if text:
                    text_blocks.append({"type": "text", "text": text})
                continue
            raise AnthropicConversationError("tool_result content block is not an object")
        block_type = str(raw.get("type") or "").strip()
        if block_type == "text":
            text = str(raw.get("text") or "").strip()
            if text:
                normalized = {"type": "text", "text": text}
                text_blocks.append(normalized)
                success_blocks.append(normalized)
            continue
        if block_type == "image":
            if not is_error:
                success_blocks.append(_normalize_image_block(raw))
            # Error results deliberately omit images. The unchanged prior
            # observation remains in the conversation and the text payload
            # carries the latest frame id and elements.
            continue
        if is_error:
            # Do not let any future multimodal block poison an error result.
            continue
        raise AnthropicConversationError(
            f"unsupported tool_result content block {block_type!r}"
        )

    if is_error:
        error_text = "\n".join(block["text"] for block in text_blocks).strip()
        if not error_text:
            error_text = "Tool action failed locally. Choose a different safe action."
        return {
            "type": "tool_result",
            "tool_use_id": result_id,
            "is_error": True,
            # A plain string follows Anthropic's documented error example and
            # cannot accidentally carry an image/document block.
            "content": error_text,
        }

    if not success_blocks:
        success_blocks = [
            {"type": "text", "text": "Tool completed with no additional output."}
        ]
    return {
        "type": "tool_result",
        "tool_use_id": result_id,
        "content": success_blocks,
    }


def _validate_assistant_content(content: object) -> list[dict[str, Any]]:
    """Reject malformed provider tool calls before they enter conversation history."""
    if not isinstance(content, list):
        raise AnthropicResponseError("Anthropic response content is not a list")
    validated: list[dict[str, Any]] = []
    tool_ids: set[str] = set()
    for raw in content:
        if not isinstance(raw, dict):
            raise AnthropicResponseError("Anthropic response content block is not an object")
        block = dict(raw)
        block_type = str(block.get("type") or "").strip()
        if not block_type:
            raise AnthropicResponseError("Anthropic response content block has no type")
        if block_type == "tool_use":
            tool_id = str(block.get("id") or "").strip()
            name = str(block.get("name") or "").strip()
            if not tool_id or not name or not isinstance(block.get("input"), dict):
                raise AnthropicResponseError(
                    "Anthropic returned a malformed tool_use id, name, or input"
                )
            if tool_id in tool_ids:
                raise AnthropicResponseError("Anthropic returned a duplicate tool_use id")
            tool_ids.add(tool_id)
        elif block_type == "text" and not isinstance(block.get("text"), str):
            raise AnthropicResponseError("Anthropic returned a malformed text block")
        # Preserve provider-owned thinking and other valid response blocks
        # byte-for-byte at the dict level; only client tool calls are decoded.
        validated.append(block)
    return validated


def _normalize_anthropic_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize and verify a complete Messages API conversation before POST."""
    if not isinstance(messages, list) or not messages:
        raise AnthropicConversationError("Anthropic conversation is empty")

    normalized: list[dict[str, Any]] = []
    pending_tool_ids: list[str] = []
    seen_tool_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            raise AnthropicConversationError("Anthropic message is not an object")
        role = str(message.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            raise AnthropicConversationError(f"unsupported Anthropic message role {role!r}")
        content = message.get("content")

        if role == "assistant":
            if pending_tool_ids:
                raise AnthropicConversationError(
                    "assistant tool_use was not followed by matching tool_result blocks"
                )
            if isinstance(content, str):
                normalized.append({"role": role, "content": content})
                continue
            try:
                blocks = _validate_assistant_content(content)
            except AnthropicResponseError as exc:
                raise AnthropicConversationError(
                    f"stored assistant content is malformed: {exc}"
                ) from exc
            current_ids: list[str] = []
            for block in blocks:
                if block.get("type") != "tool_use":
                    continue
                tool_id = str(block.get("id") or "").strip()
                if tool_id in seen_tool_ids:
                    raise AnthropicConversationError("duplicate tool_use id in conversation")
                seen_tool_ids.add(tool_id)
                current_ids.append(tool_id)
            pending_tool_ids = current_ids
            normalized.append({"role": role, "content": blocks})
            continue

        if isinstance(content, str):
            if pending_tool_ids:
                raise AnthropicConversationError(
                    "pending tool_use requires tool_result blocks before user text"
                )
            normalized.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise AnthropicConversationError("user message content is not text or a list")

        results_by_id: dict[str, dict[str, Any]] = {}
        ordinary_blocks: list[dict[str, Any]] = []
        for raw in content:
            if not isinstance(raw, dict):
                raise AnthropicConversationError("user content block is not an object")
            block_type = str(raw.get("type") or "").strip()
            if block_type == "tool_result":
                raw_error = raw.get("is_error")
                if raw_error is not None and not isinstance(raw_error, bool):
                    raise AnthropicConversationError("tool_result is_error is not boolean")
                result = _build_tool_result(
                    raw.get("tool_use_id"),
                    raw.get("content"),
                    is_error=raw_error is True,
                )
                result_id = result["tool_use_id"]
                if result_id in results_by_id:
                    raise AnthropicConversationError("duplicate tool_result id in user turn")
                results_by_id[result_id] = result
            elif block_type == "text":
                ordinary_blocks.append(
                    {"type": "text", "text": str(raw.get("text") or "")}
                )
            elif block_type == "image":
                ordinary_blocks.append(_normalize_image_block(raw))
            else:
                raise AnthropicConversationError(
                    f"unsupported user content block {block_type!r}"
                )

        if pending_tool_ids:
            if set(results_by_id) != set(pending_tool_ids):
                raise AnthropicConversationError(
                    "tool_result ids do not exactly match the preceding tool_use ids"
                )
            # Anthropic requires every tool_result first in the user turn.
            result_blocks = [results_by_id[tool_id] for tool_id in pending_tool_ids]
            pending_tool_ids = []
        elif results_by_id:
            raise AnthropicConversationError("orphan tool_result without a tool_use")
        else:
            result_blocks = []
        normalized.append(
            {"role": role, "content": [*result_blocks, *ordinary_blocks]}
        )

    if pending_tool_ids:
        raise AnthropicConversationError(
            "conversation ends before every tool_use receives a tool_result"
        )
    return normalized


def _conversation_schema_error(detail: str) -> bool:
    """Return whether an HTTP 400 can be repaired by rebuilding message history."""
    text = str(detail or "").casefold()
    return any(
        marker in text
        for marker in (
            "tool_result",
            "tool result",
            "tool_use",
            "tool use",
            "messages.",
            "content block",
        )
    )


def recovery_configured() -> bool:
    return bool(load_anthropic_settings()[0])


def is_ui_recoverable_error(exc: BaseException) -> bool:
    """Conservative eligibility gate for a live visual/navigation failure."""
    if not isinstance(exc, PhoneError) or isinstance(exc, OperationCancelled):
        return False
    # Paid recovery is opt-in only.  UnknownVisualStateError and narrowly
    # typed workflow failures may set this flag after their complete bounded
    # local handler has failed.  Ordinary PhoneError remains local-only.
    if getattr(exc, "ai_recoverable", None) is not True:
        return False
    if is_recoverable_bridge_failure(exc):
        return False
    text = f"{type(exc).__name__}: {exc}".casefold()
    return not any(hint in text for hint in _NON_UI_FAILURE_HINTS)


def _local_db_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "FriendPoster" / "recovery_recipes.sqlite3"


def _normalize(value: object) -> str:
    text = diaglog.redact(value).casefold()
    text = _USERNAME_RE.sub("<username>", text)
    text = _EMAIL_RE.sub("<email>", text)
    text = _IP_RE.sub("<ip>", text)
    text = _DATE_RE.sub("<date>", text)
    text = _UDID_RE.sub("<udid>", text)
    text = _CODE_RE.sub("<number>", text)
    text = re.sub(r"[^a-z0-9<>]+", " ", text)
    return _SPACE_RE.sub(" ", text).strip()[:240]


def _secret_values() -> list[str]:
    values: set[str] = set()

    def add_strings(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                add_strings(nested)
        elif isinstance(value, list):
            for nested in value:
                add_strings(nested)
        elif isinstance(value, str) and len(value.strip()) >= 4:
            values.add(value.strip())

    try:
        with open(SECRETS_PATH, "r", encoding="utf-8") as handle:
            add_strings(json.load(handle))
    except (OSError, ValueError, TypeError):
        pass

    slot_key_hints = (
        "username", "password", "email", "birth", "verification",
        "credential", "proxy", "public_ip", "api_key", "token", "udid",
    )

    def add_slot_strings(value: Any, *, sensitive: bool = False) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_sensitive = sensitive or any(
                    hint in str(key).casefold() for hint in slot_key_hints
                )
                add_slot_strings(nested, sensitive=key_sensitive)
        elif isinstance(value, list):
            for nested in value:
                add_slot_strings(nested, sensitive=sensitive)
        elif sensitive and isinstance(value, str) and len(value.strip()) >= 3:
            values.add(value.strip())

    try:
        with open(Path(LOG_DIR) / "slots.json", "r", encoding="utf-8") as handle:
            add_slot_strings(json.load(handle))
    except (OSError, ValueError, TypeError):
        pass
    return sorted(values, key=len, reverse=True)


def redact_for_model(value: object, secrets: list[str] | None = None) -> str:
    text = diaglog.redact(value)
    for secret in secrets if secrets is not None else _secret_values():
        # UI frameworks are free to change the casing of labels and entered
        # values.  A saved bare username such as ``SomeUser`` must therefore
        # also redact ``someuser`` even though it has no u/, @, or username=
        # marker for the generic username expression below to recognize.
        if secret:
            text = re.sub(
                re.escape(secret),
                "<redacted>",
                text,
                flags=re.IGNORECASE,
            )
    text = _USERNAME_RE.sub("<redacted-username>", text)
    text = _EMAIL_RE.sub("<redacted-email>", text)
    text = _IP_RE.sub("<redacted-ip>", text)
    text = _DATE_RE.sub("<redacted-date>", text)
    text = _UDID_RE.sub("<redacted-udid>", text)
    text = _CODE_RE.sub("<redacted-number>", text)
    return text


def _node_sensitive(node: Node, label: str) -> bool:
    low = f"{node.ntype} {label}".casefold()
    if any(hint in low for hint in _SENSITIVE_HINTS):
        return True
    if any(kind in str(node.ntype).casefold() for kind in ("securetextfield",)):
        return True
    return bool(
        _EMAIL_RE.search(label)
        or _IP_RE.search(label)
        or _DATE_RE.search(label)
        or _UDID_RE.search(label)
        or _USERNAME_RE.search(label)
        or re.search(r"(?i)\b(?:code|otp)\D{0,8}\d{4,8}\b", label)
    )


def _node_forbidden(node: Node) -> bool:
    low = f"{node.ntype} {node.labels()}".casefold()
    if any(hint in low for hint in _DESTRUCTIVE_HINTS):
        return True
    if any(kind in str(node.ntype).casefold() for kind in _KEYBOARD_TYPES):
        return True
    return _node_sensitive(node, node.labels())


def _public_sensitive_label(node: Node, raw: str) -> str:
    """Expose the control's purpose while withholding its private value.

    Anthropic must be able to understand that an iCloud sign-in sheet and its
    Password/email/code controls are present.  It does not need the stored
    value to choose a prepared-field or deterministic-unlock tool.
    """
    low = f"{node.ntype} {raw}".casefold()
    if "password" in low or "passcode" in low or "securetextfield" in low:
        return "Password field (stored value hidden; use a prepared-value tool)"
    if "verification" in low or "security code" in low or "otp" in low:
        return "Verification-code field (stored value hidden)"
    if "email" in low or "e-mail" in low:
        return "Email field (stored value hidden)"
    if "username" in low or _USERNAME_RE.search(raw):
        return "Username control (stored value hidden)"
    if "birth" in low:
        return "Birth-date control (stored value hidden)"
    return "Sensitive control (value hidden)"


def _redact_image(png: bytes, nodes: list[Node], secrets: list[str]) -> bytes:
    if not png:
        return b""
    from PIL import Image, ImageDraw

    with Image.open(io.BytesIO(png)) as source:
        image = source.convert("RGB")
    sx = image.width / 750.0
    sy = image.height / 1334.0
    draw = ImageDraw.Draw(image)
    for node in nodes:
        raw = node.labels()
        secret_hit = any(secret and secret in raw for secret in secrets)
        if not secret_hit and not _node_sensitive(node, raw):
            continue
        x1 = max(0, int(node.x * sx) - 5)
        y1 = max(0, int(node.y * sy) - 5)
        x2 = min(image.width, int((node.x + node.w) * sx) + 5)
        y2 = min(image.height, int((node.y + node.h) * sy) + 5)
        if x2 > x1 and y2 > y1:
            draw.rectangle((x1, y1, x2, y2), fill=(22, 22, 22))
    if image.width > _MODEL_IMAGE_MAX_SIZE[0] or image.height > _MODEL_IMAGE_MAX_SIZE[1]:
        resampling = getattr(Image, "Resampling", Image)
        image.thumbnail(_MODEL_IMAGE_MAX_SIZE, resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    rendered = out.getvalue()
    if len(rendered) <= _MODEL_IMAGE_MAX_BYTES:
        return rendered
    # Screen captures can be photographic/noisy and several megabytes as
    # lossless RGB PNGs.  A bounded palette preserves navigation details and
    # redaction rectangles while preventing repeated multi-megabyte uploads.
    best = rendered
    for colors in (128, 64, 32):
        out = io.BytesIO()
        image.quantize(colors=colors).save(out, "PNG", optimize=True)
        candidate = out.getvalue()
        if len(candidate) < len(best):
            best = candidate
        if len(candidate) <= _MODEL_IMAGE_MAX_BYTES:
            return candidate
    # A pathological noise-like frame can still defeat palette compression.
    # Reduce dimensions in bounded steps until the transport ceiling is met.
    reduced = image.copy()
    while len(best) > _MODEL_IMAGE_MAX_BYTES and reduced.width > 320:
        reduced.thumbnail(
            (max(320, int(reduced.width * 0.85)), max(568, int(reduced.height * 0.85))),
            resampling.LANCZOS,
        )
        out = io.BytesIO()
        reduced.quantize(colors=32).save(out, "PNG", optimize=True)
        candidate = out.getvalue()
        if len(candidate) < len(best):
            best = candidate
    return best


def _compact_prior_images(messages: list[dict[str, Any]]) -> int:
    """Keep only the newest observation image in an Anthropic conversation."""
    found: list[tuple[list[Any], int]] = []

    def collect(value: Any) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict) and item.get("type") == "image":
                    found.append((value, index))
                elif isinstance(item, (dict, list)):
                    collect(item)
        elif isinstance(value, dict):
            for child in value.values():
                if isinstance(child, (dict, list)):
                    collect(child)

    collect(messages)
    for container, index in found[:-1]:
        container[index] = {
            "type": "text",
            "text": "[Earlier sanitized screenshot omitted; use its recorded frame data.]",
        }
    return max(0, len(found) - 1)


def _perceptual_hash(png: bytes) -> str:
    if not png:
        return "no-image"
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(png)) as source:
        gray = ImageOps.grayscale(source).resize((9, 8))
    flattened = getattr(gray, "get_flattened_data", None)
    pixels = list(flattened() if callable(flattened) else gray.getdata())
    bits = []
    for row in range(8):
        start = row * 9
        bits.extend(pixels[start + col] > pixels[start + col + 1] for col in range(8))
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def _screen_class(labels: list[str]) -> str:
    joined = " ".join(labels).casefold()
    if (
        "container" in joined
        and "default" in joined
        and "new container" in joined
        and "settings" in joined
    ):
        return "crane_container_picker"
    if "containers" in joined and ("edit" in joined or "new" in joined):
        return "crane_container_settings"
    if "ghost" in joined or "change ip" in joined or "country" in joined:
        return "ghost"
    if "customize" in joined or "interests" in joined:
        return "reddit_interests"
    if "reddit" in joined or "home popular watch chat inbox" in joined:
        return "reddit"
    if "settings" in joined or "apple id" in joined or "icloud" in joined:
        return "settings"
    if "swipe up to open" in joined or "no older notifications" in joined:
        return "iphone_home"
    return "unknown"


class RecipeStore:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path is not None else _local_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as conn:
            with conn:
                conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recipes (
                    fingerprint TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    actions_json TEXT NOT NULL,
                    expected_json TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    model TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
                )

    def get(self, fingerprint: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM recipes WHERE fingerprint=? AND schema_version=? "
                "AND status IN ('candidate','promoted')",
                (fingerprint, SCHEMA_VERSION),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            actions = json.loads(result.pop("actions_json"))
            expected = json.loads(result.pop("expected_json"))
            if (
                not isinstance(actions, list)
                or not isinstance(expected, list)
                or not actions
                or len(actions) != len(expected)
                or not all(isinstance(action, dict) for action in actions)
                or not all(isinstance(value, str) and value for value in expected)
            ):
                raise ValueError("cached recipe has an invalid action schema")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            # A corrupt local row must never brick recovery.  Quarantine it so
            # this incident immediately falls through to a fresh Anthropic
            # decision and cannot be replayed again.
            self.quarantine(
                fingerprint,
                f"malformed schema-v{SCHEMA_VERSION} cached recipe: {exc}",
            )
            return None
        result["actions"] = actions
        result["expected"] = expected
        return result

    def record_success(self, session: RecoverySession) -> str:
        now = time.time()
        with self._lock, closing(self._connect()) as conn, conn:
            existing = conn.execute(
                "SELECT success_count,status FROM recipes "
                "WHERE fingerprint=? AND schema_version=?",
                (session.incident_fingerprint, SCHEMA_VERSION),
            ).fetchone()
            if session.source == "cache" and existing is not None:
                successes = int(existing["success_count"]) + 1
            else:
                successes = 1
            status = "promoted" if successes >= 2 else "candidate"
            conn.execute(
                """
                INSERT INTO recipes (
                    fingerprint,schema_version,status,actions_json,expected_json,
                    success_count,failure_count,model,last_error,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    schema_version=excluded.schema_version,
                    status=excluded.status,
                    actions_json=excluded.actions_json,
                    expected_json=excluded.expected_json,
                    success_count=excluded.success_count,
                    failure_count=CASE WHEN excluded.success_count=1 THEN 0 ELSE recipes.failure_count END,
                    model=excluded.model,
                    last_error='',
                    updated_at=excluded.updated_at
                """,
                (
                    session.incident_fingerprint, SCHEMA_VERSION, status,
                    json.dumps(session.actions, separators=(",", ":")),
                    json.dumps(session.expected_fingerprints, separators=(",", ":")),
                    successes, 0, session.model, "", now, now,
                ),
            )
        return status

    def quarantine(self, fingerprint: str, reason: str) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE recipes SET status='quarantined', failure_count=failure_count+1,
                    last_error=?, updated_at=? WHERE fingerprint=?
                """,
                (redact_for_model(reason)[:300], time.time(), fingerprint),
            )


TOOLS: list[dict[str, Any]] = [
    {
        "name": "observe",
        "description": "Capture a fresh sanitized screen and accessibility tree without acting.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "tap_element",
        "description": "Tap one safe element ID from the latest observation.",
        "input_schema": {
            "type": "object",
            "properties": {"frame_id": {"type": "string"}, "element_id": {"type": "string"}},
            "required": ["frame_id", "element_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "long_press_element",
        "description": "Long-press one safe element ID from the latest observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "frame_id": {"type": "string"},
                "element_id": {"type": "string"},
                "hold_ms": {"type": "integer", "minimum": 450, "maximum": 1200},
            },
            "required": ["frame_id", "element_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "long_press_point",
        "description": (
            "Long-press a safe canonical 750x1334 point on the latest frame "
            "when no element is suitable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "frame_id": {"type": "string"},
                "x": {"type": "integer", "minimum": 10, "maximum": 740},
                "y": {"type": "integer", "minimum": 30, "maximum": 1320},
                "hold_ms": {"type": "integer", "minimum": 450, "maximum": 1200},
            },
            "required": ["frame_id", "x", "y"],
            "additionalProperties": False,
        },
    },
    {
        "name": "tap_point",
        "description": "Tap a safe canonical 750x1334 point on the latest frame when no element exists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "frame_id": {"type": "string"}, "x": {"type": "integer", "minimum": 10, "maximum": 740},
                "y": {"type": "integer", "minimum": 30, "maximum": 1320},
            },
            "required": ["frame_id", "x", "y"],
            "additionalProperties": False,
        },
    },
    {
        "name": "swipe",
        "description": "Perform a bounded navigation swipe on the latest frame.",
        "input_schema": {
            "type": "object",
            "properties": {
                "frame_id": {"type": "string"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "distance": {"type": "string", "enum": ["small", "medium", "large"]},
            },
            "required": ["frame_id", "direction", "distance"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait",
        "description": "Wait briefly for an in-progress screen to settle.",
        "input_schema": {
            "type": "object", "properties": {"seconds": {"type": "number", "minimum": 1, "maximum": 15}},
            "required": ["seconds"], "additionalProperties": False,
        },
    },
    {
        "name": "press_home",
        "description": "Press iPhone Home. This does not terminate or delete an app.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "launch_reddit",
        "description": "Launch the Reddit bundle without typing or submitting anything.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "launch_settings",
        "description": "Launch iOS Settings without typing or changing a setting.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "launch_ghost",
        "description": "Launch Ghost without typing or changing network identity.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "resume_pipeline",
        "description": "Ask deterministic code to retry from its durable checkpoint. This does not declare success.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "cache_control": {"type": "ephemeral"},
    },
]

SYSTEM_PROMPT = """You recover an iPhone account-creation workflow from an unexpected visual state.
You may use safe frame-bound navigation plus explicitly supplied high-level workflow tools.
Never type or reveal account data, credentials, codes, proxy data, API keys, or other secrets.
Never directly tap a destructive/sensitive control. Destructive or identity-changing work is
allowed only through an explicitly supplied verified high-level tool; deterministic local code
checks its scope, preconditions, and postcondition. Use only the latest frame_id. Inspect after
every action. Prefer labeled elements over raw coordinates. If an action was rejected or already
failed on the same unchanged screen, choose a different strategy. Follow the sanitized recovery
goal and required result exactly. Merely changing the screen or exposing the requested control is
not success. Call resume_pipeline only after the latest observation satisfies that required
result. A local guard may reject an early resume and explain what remains; continue in the same
conversation when that happens. Deterministic checkpoints, never your claim, decide whether
recovery truly worked."""


class UIRecoveryAgent:
    def __init__(
        self,
        stack: Any,
        phone: Any,
        *,
        stop: Any,
        log: Callable[[str], None],
        status: Callable[[str], None] | None = None,
        store: RecipeStore | None = None,
        api_url: str = ANTHROPIC_MESSAGES_URL,
        post: Callable[..., Any] | None = None,
        context: dict[str, Any] | None = None,
        workflow_scope: str = "creation_pipeline",
        workflow_tools: dict[str, RecoveryWorkflowTool] | None = None,
        resume_guard: Callable[[Observation], tuple[bool, str]] | None = None,
        chain: RecoveryChain | None = None,
        attempt_budget: RecoveryAttemptBudget | None = None,
    ):
        self.stack = stack
        self.phone = phone
        self.stop = stop
        self.log = log
        self.status = status or (lambda _message: None)
        self.store = store or RecipeStore()
        self.api_url = api_url
        self._post = post or requests.post
        self.resume_guard = resume_guard
        self.chain = chain if chain is not None else RecoveryChain()
        chain_budget = self.chain.attempt_budget
        if (
            attempt_budget is not None
            and chain_budget is not None
            and attempt_budget is not chain_budget
        ):
            raise ValueError(
                "recovery chain is already bound to a different attempt budget"
            )
        self.attempt_budget = (
            attempt_budget
            if attempt_budget is not None
            else chain_budget
            if chain_budget is not None
            else RecoveryAttemptBudget()
        )
        self.chain.attempt_budget = self.attempt_budget
        self.workflow_scope = re.sub(
            r"[^a-z0-9_]+", "_", str(workflow_scope).casefold()
        ).strip("_") or "creation_pipeline"
        self.workflow_tools = dict(workflow_tools or {})
        invalid_tools = set(self.workflow_tools).intersection(
            {str(tool["name"]) for tool in TOOLS}
        )
        if invalid_tools:
            raise ValueError(
                "workflow tool names collide with built-in recovery tools: "
                + ", ".join(sorted(invalid_tools))
            )
        for name in self.workflow_tools:
            if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", name):
                raise ValueError(f"invalid recovery workflow tool name {name!r}")
            allowed_scopes = _WORKFLOW_TOOL_SCOPES.get(name)
            if allowed_scopes is None:
                raise ValueError(f"unknown recovery workflow tool {name!r}")
            if self.workflow_scope not in allowed_scopes:
                raise ValueError(
                    f"recovery workflow tool {name!r} is not authorized for "
                    f"scope {self.workflow_scope!r}"
                )
        self.context = {
            str(key)[:40]: redact_for_model(value)[:160]
            for key, value in (context or {}).items()
        }
        self.current: Observation | None = None
        self._action_pairs = self.chain.action_pairs
        self._history = self.chain.history
        self._requested_directive: RecoveryDirective | None = None

    def _tool_definitions(self) -> list[dict[str, Any]]:
        definitions = [dict(tool) for tool in TOOLS]
        for name, spec in sorted(self.workflow_tools.items()):
            definitions.append(
                {
                    "name": name,
                    "description": (
                        f"Authorized only for workflow scope {self.workflow_scope!r}. "
                        f"{spec.description} Local deterministic code verifies success."
                    ),
                    "input_schema": spec.input_schema,
                }
            )
        if definitions:
            definitions[-1] = dict(definitions[-1])
            definitions[-1]["cache_control"] = {"type": "ephemeral"}
        return definitions

    def _check_stop(self) -> None:
        if bool(getattr(self.stop, "is_set", lambda: False)()):
            raise RecoveryStopped("Stopped by user.")

    def _wait(self, seconds: float) -> None:
        self._check_stop()
        waiter = getattr(self.stop, "wait", None)
        if callable(waiter):
            if waiter(max(0.0, float(seconds))):
                raise RecoveryStopped("Stopped by user.")
        else:
            deadline = time.monotonic() + max(0.0, float(seconds))
            while time.monotonic() < deadline:
                self._check_stop()
                time.sleep(min(0.25, deadline - time.monotonic()))
        self._check_stop()

    def observe(self, exc: BaseException) -> Observation:
        self._check_stop()
        blob = self.stack.dump_xml()
        nodes = parse_nodes(blob)
        png = self.stack.screenshot_png() or b""
        secrets = _secret_values()
        redacted_png = _redact_image(png, nodes, secrets)
        public_nodes: list[dict[str, Any]] = []
        elements: dict[str, Node] = {}
        normalized_labels: list[str] = []
        for node in nodes:
            if not node.visible or not node.enabled or not node.on_screen():
                continue
            raw = node.labels().strip()
            if not raw:
                continue
            public = redact_for_model(raw, secrets)
            if _node_sensitive(node, raw):
                public = _public_sensitive_label(node, raw)
            normalized = _normalize(public)
            if normalized and not _TIME_OR_PERCENT_RE.fullmatch(normalized):
                normalized_labels.append(
                    f"{normalized}@{min(9, node.x // 75)},{min(9, node.y // 134)}"
                )
            element_id = f"e{len(elements) + 1:04d}"
            elements[element_id] = node
            public_nodes.append(
                {
                    "id": element_id,
                    "type": str(node.ntype)[:40],
                    "label": public[:160],
                    "bounds": [node.x, node.y, node.w, node.h],
                    "safe_to_tap": not any(
                        _node_forbidden(candidate)
                        and self._meaningfully_overlaps(candidate, node)
                        for candidate in nodes
                    ),
                }
            )
        labels = sorted(set(normalized_labels))[:180]
        screen_class = _screen_class(labels)
        # Loop prevention must describe the screen, not the exception that led
        # us to inspect it.  A reconnect can wrap the same stuck UI in a new
        # error type, message, or traceback; none of those make it safe to tap
        # or long-press the same control again.
        screen_semantic_material = {
            "schema": SCHEMA_VERSION,
            "screen_class": screen_class,
            "workflow_scope": self.workflow_scope,
            "labels": labels,
            # ``self.context`` was bounded and redacted at construction time.
            # It keeps identical-looking controls for different local targets
            # (for example two slot ids) from sharing an action identity.
            "context": self.context,
        }
        screen_semantic_key = hashlib.sha256(
            json.dumps(
                screen_semantic_material,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        semantic_material = {
            **screen_semantic_material,
            "error_type": type(exc).__name__,
            "error": _normalize(exc),
        }
        if exc.__traceback__ is not None:
            last = traceback.extract_tb(exc.__traceback__)[-1]
            semantic_material["callsite"] = (
                f"{Path(last.filename).name}:{last.name}:{last.lineno}"
            )
        semantic_key = hashlib.sha256(
            json.dumps(
                semantic_material, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        material = {
            **semantic_material,
            "image": _perceptual_hash(redacted_png),
        }
        fingerprint = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        frame_id = hashlib.sha256(
            f"{time.monotonic_ns()}:{fingerprint}".encode("ascii")
        ).hexdigest()[:16]
        observation = Observation(
            frame_id=frame_id,
            fingerprint=fingerprint,
            semantic_key=semantic_key,
            screen_semantic_key=screen_semantic_key,
            screen_class=screen_class,
            nodes=nodes,
            elements=elements,
            public_nodes=public_nodes[:160],
            png=png,
            redacted_png=redacted_png,
            redacted_error=redact_for_model(f"{type(exc).__name__}: {exc}", secrets)[:800],
        )
        self.current = observation
        return observation

    @staticmethod
    def _point_intersects(node: Node, x: int, y: int) -> bool:
        return node.x <= x <= node.x + node.w and node.y <= y <= node.y + node.h

    @staticmethod
    def _meaningfully_overlaps(first: Node, second: Node) -> bool:
        """Return whether two accessibility bounds represent one control.

        WDA flattens the XML hierarchy into :class:`Node` objects, so child
        relationships are not retained.  Geometry is the only reliable local
        proof that a destructive/sensitive label belongs to an otherwise
        innocuous actionable parent.  Full containment always qualifies; for
        imperfect accessibility bounds, at least half of the smaller node must
        overlap.  A one-pixel/sibling-edge collision is deliberately ignored.
        """
        left = max(first.x, second.x)
        top = max(first.y, second.y)
        right = min(first.x + first.w, second.x + second.w)
        bottom = min(first.y + first.h, second.y + second.h)
        if right <= left or bottom <= top:
            return False
        intersection = (right - left) * (bottom - top)
        first_area = max(1, first.w) * max(1, first.h)
        second_area = max(1, second.w) * max(1, second.h)
        return intersection * 2 >= min(first_area, second_area)

    def _tap_blockers(
        self,
        observation: Observation,
        x: int,
        y: int,
        semantic_target: Node | None = None,
    ) -> list[Node]:
        return [
            node
            for node in observation.nodes
            if _node_forbidden(node)
            and (
                self._point_intersects(node, x, y)
                or (
                    semantic_target is not None
                    and self._meaningfully_overlaps(node, semantic_target)
                )
            )
        ]

    def _semantic_tap_target(
        self, observation: Observation, x: int, y: int
    ) -> Node | None:
        """Resolve nested accessibility nodes to one stable tap identity.

        iOS commonly nests a small StaticText inside a larger Button.  Both an
        element tap and a point tap at that label must canonicalize to the same
        actionable control, otherwise the model could repeat the physical tap
        by switching tool names.
        """
        candidates = [
            candidate
            for candidate in observation.elements.values()
            if self._point_intersects(candidate, x, y)
        ]
        if not candidates:
            return None

        def rank(candidate: Node) -> tuple[int, int, str, str]:
            kind = str(candidate.ntype).casefold()
            actionable = any(
                marker in kind
                for marker in (
                    "button", "cell", "link", "switch", "tab",
                    "menuitem", "segmentedcontrol",
                )
            )
            return (
                0 if actionable else 1,
                max(1, candidate.w) * max(1, candidate.h),
                _normalize(redact_for_model(candidate.labels())),
                kind,
            )

        return min(candidates, key=rank)

    def _validate_frame(self, frame_id: object) -> Observation:
        if self.current is None or str(frame_id) != self.current.frame_id:
            raise ValueError("stale or unknown frame_id; call observe and use the latest frame")
        return self.current

    def _check_resume_guard(self, observation: Observation) -> tuple[bool, str]:
        """Run the caller's deterministic resume postcondition, if supplied."""
        self._check_stop()
        if self.resume_guard is None:
            return True, ""
        try:
            result = self.resume_guard(observation)
        except (RecoveryStopped, OperationCancelled):
            raise
        except Exception as guard_exc:
            return False, (
                "local resume validation failed: "
                f"{redact_for_model(guard_exc)[:240]}"
            )
        if not isinstance(result, tuple) or len(result) != 2:
            return False, "local resume validator returned an invalid result"
        allowed, reason = result
        if bool(allowed):
            return True, redact_for_model(reason)[:240]
        return False, (
            redact_for_model(reason)[:300]
            or "the deterministic recovery postcondition is not proven"
        )

    def _canonical_action(
        self,
        name: str,
        payload: dict[str, Any],
        current: Observation | None = None,
    ) -> str:
        clean = {key: value for key, value in payload.items() if key != "frame_id"}
        canonical_name = name
        node = None
        if name in {"tap_element", "long_press_element"} and current is not None:
            element_id = str(payload.get("element_id") or "")
            node = current.elements.get(element_id)
            if node is not None:
                node = self._semantic_tap_target(current, *node.center())
        elif name in {"tap_point", "long_press_point"} and current is not None:
            x, y = int(payload.get("x", -1)), int(payload.get("y", -1))
            node = self._semantic_tap_target(current, x, y)
            if node is None:
                # Pixel-level changes are not a new strategy.  Keep free-space
                # point taps in a small coarse cell so Anthropic cannot loop by
                # moving the same tap one pixel at a time.
                clean = {
                    "coarse_point": [
                        min(24, max(0, x // 30)),
                        min(44, max(0, y // 30)),
                    ]
                }
        if name in {"long_press_element", "long_press_point"}:
            # A different press duration is not a different navigation
            # strategy. Keep hold_ms in the executable/recipe payload, but
            # never let it bypass same-screen loop prevention.
            clean.pop("hold_ms", None)
        if node is not None:
            if name in {"tap_element", "tap_point"}:
                canonical_name = "tap_semantic_control"
            elif name in {"long_press_element", "long_press_point"}:
                canonical_name = "long_press_semantic_control"
            clean.pop("element_id", None)
            clean.pop("x", None)
            clean.pop("y", None)
            center_x, center_y = node.center()
            clean.update(
                {
                    "anchor_label": _normalize(
                        redact_for_model(node.labels())
                    ),
                    "coarse_region": [
                        min(9, max(0, center_x // 75)),
                        min(9, max(0, center_y // 134)),
                    ],
                }
            )
        return (
            f"{canonical_name}:"
            f"{json.dumps(clean, sort_keys=True, separators=(',', ':'))}"
        )

    def _execute_action(
        self, name: str, payload: dict[str, Any], exc: BaseException
    ) -> tuple[Observation, dict[str, Any] | None]:
        self._check_stop()
        current = self.current
        if name in {
            "tap_element", "long_press_element", "tap_point", "long_press_point",
            "swipe"
        } or name in self.workflow_tools:
            current = self._validate_frame(payload.get("frame_id"))
        if current is None:
            current = self.observe(exc)
        canonical = self._canonical_action(name, payload, current)
        pair = (current.screen_semantic_key or current.semantic_key, canonical)
        if name != "observe" and pair in self._action_pairs:
            raise ValueError("duplicate action rejected on an unchanged screen")
        if name != "observe":
            self._action_pairs.add(pair)

        recipe_action: dict[str, Any] | None = None
        pending_directive: RecoveryDirective | None = None
        if name == "observe":
            pass
        elif name == "tap_element":
            element_id = str(payload.get("element_id") or "")
            node = current.elements.get(element_id)
            if node is None:
                raise ValueError("unknown element_id on the latest frame")
            x, y = node.center()
            semantic_target = self._semantic_tap_target(current, x, y)
            if _node_forbidden(node) or self._tap_blockers(
                current, x, y, semantic_target
            ):
                raise ValueError("tap rejected: element is sensitive, destructive, or text-entry related")
            self.stack.tap_xy(x, y)
            recipe_action = {
                "tool": "tap_anchor", "label": _normalize(redact_for_model(node.labels())),
                "x": x, "y": y,
            }
        elif name == "long_press_element":
            element_id = str(payload.get("element_id") or "")
            node = current.elements.get(element_id)
            if node is None:
                raise ValueError("unknown element_id on the latest frame")
            x, y = node.center()
            semantic_target = self._semantic_tap_target(current, x, y)
            if _node_forbidden(node) or self._tap_blockers(
                current, x, y, semantic_target
            ):
                raise ValueError(
                    "long press rejected: element is sensitive, destructive, or text-entry related"
                )
            hold_ms = max(450, min(1200, int(payload.get("hold_ms", 650))))
            self.stack.long_tap(x, y, hold_ms=hold_ms)
            recipe_action = {
                "tool": "long_press_anchor",
                "label": _normalize(redact_for_model(node.labels())),
                "x": x,
                "y": y,
                "hold_ms": hold_ms,
            }
        elif name == "long_press_point":
            x, y = int(payload.get("x", -1)), int(payload.get("y", -1))
            if not (10 <= x <= 740 and 30 <= y <= 1320):
                raise ValueError("long-press point outside the safe canonical screen area")
            semantic_target = self._semantic_tap_target(current, x, y)
            blockers = self._tap_blockers(current, x, y, semantic_target)
            if blockers:
                raise ValueError(
                    "long press rejected: point intersects a sensitive, destructive, "
                    "or text-entry control"
                )
            hold_ms = max(450, min(1200, int(payload.get("hold_ms", 650))))
            self.stack.long_tap(x, y, hold_ms=hold_ms)
            if semantic_target is not None:
                recipe_action = {
                    "tool": "long_press_anchor",
                    "label": _normalize(
                        redact_for_model(semantic_target.labels())
                    ),
                    "x": x,
                    "y": y,
                    "hold_ms": hold_ms,
                }
            else:
                recipe_action = {
                    "tool": "long_press_point",
                    "x": x,
                    "y": y,
                    "hold_ms": hold_ms,
                }
        elif name == "tap_point":
            x, y = int(payload.get("x", -1)), int(payload.get("y", -1))
            if not (10 <= x <= 740 and 30 <= y <= 1320):
                raise ValueError("tap point outside the safe canonical screen area")
            semantic_target = self._semantic_tap_target(current, x, y)
            blockers = self._tap_blockers(current, x, y, semantic_target)
            if blockers:
                raise ValueError("tap rejected: point intersects a sensitive, destructive, or text-entry control")
            self.stack.tap_xy(x, y)
            recipe_action = {"tool": "tap_point", "x": x, "y": y}
        elif name == "swipe":
            direction = str(payload.get("direction") or "")
            distance = str(payload.get("distance") or "")
            amount = {"small": 260, "medium": 500, "large": 760}.get(distance)
            if direction not in {"up", "down", "left", "right"} or amount is None:
                raise ValueError("invalid bounded swipe")
            x1, y1 = 375, 700
            dx = amount if direction == "right" else -amount if direction == "left" else 0
            dy = amount if direction == "down" else -amount if direction == "up" else 0
            x2 = max(90, min(660, x1 + dx))
            y2 = max(180, min(1160, y1 + dy))
            self.stack.swipe(x1, y1, x2, y2)
            recipe_action = {"tool": "swipe", "direction": direction, "distance": distance}
        elif name == "wait":
            seconds = max(1.0, min(15.0, float(payload.get("seconds", 2))))
            self._wait(seconds)
            recipe_action = {"tool": "wait", "seconds": seconds}
        elif name == "press_home":
            self.stack.press_home()
            recipe_action = {"tool": "press_home"}
        elif name == "launch_reddit":
            self.stack.open_bundle(REDDIT_BUNDLE)
            recipe_action = {"tool": "launch_reddit"}
        elif name == "launch_settings":
            self.stack.open_bundle(SETTINGS_BUNDLE)
            recipe_action = {"tool": "launch_settings"}
        elif name == "launch_ghost":
            self.stack.open_bundle(GHOST_BUNDLE)
            recipe_action = {"tool": "launch_ghost"}
        elif name in self.workflow_tools:
            spec = self.workflow_tools[name]
            handler_payload = {
                key: value for key, value in payload.items() if key != "frame_id"
            }
            result = spec.handler(handler_payload)
            if isinstance(result, RecoveryDirective):
                # Do not publish a supervisor directive until the mandatory
                # post-action observation succeeds.  Otherwise a transient
                # dump failure can leak a stale resume/reset request into a
                # later, unrelated tool turn.
                pending_directive = result
                result_for_log = result.detail or result.name
            else:
                result_for_log = result
            self.log(
                f"AI RECOVERY verified workflow tool {name}: "
                f"{redact_for_model(result_for_log)[:240]}"
            )
            if spec.cacheable:
                safe_input: dict[str, Any] = {}
                for key, value in handler_payload.items():
                    low_key = str(key).casefold()
                    if any(hint in low_key for hint in _SENSITIVE_HINTS):
                        continue
                    if isinstance(value, bool) or value is None:
                        safe_input[str(key)[:40]] = value
                    elif isinstance(value, (int, float)):
                        safe_input[str(key)[:40]] = value
                    elif isinstance(value, str):
                        safe_input[str(key)[:40]] = redact_for_model(value)[:120]
                recipe_action = {
                    "tool": "workflow",
                    "name": name,
                    "input": safe_input,
                }
        else:
            raise ValueError(f"unsupported recovery tool {name!r}")
        observed = self.observe(exc)
        if pending_directive is not None:
            self._requested_directive = pending_directive
        return observed, recipe_action

    def _replay_action(self, action: dict[str, Any], exc: BaseException) -> Observation:
        current = self.current or self.observe(exc)
        tool = str(action.get("tool") or "")
        if tool in {"tap_anchor", "long_press_anchor"}:
            label = str(action.get("label") or "")
            candidates = [
                (element_id, node) for element_id, node in current.elements.items()
                if _normalize(redact_for_model(node.labels())) == label and not _node_forbidden(node)
            ]
            if not candidates:
                raise RecipeDiverged("cached anchor is absent or no longer safe")
            target_x, target_y = int(action.get("x", 375)), int(action.get("y", 667))
            element_id, _node = min(
                candidates,
                key=lambda pair: abs(pair[1].center()[0] - target_x) + abs(pair[1].center()[1] - target_y),
            )
            payload = {"frame_id": current.frame_id, "element_id": element_id}
            if tool == "long_press_anchor":
                payload["hold_ms"] = action.get("hold_ms", 650)
                return self._execute_action("long_press_element", payload, exc)[0]
            return self._execute_action("tap_element", payload, exc)[0]
        if tool == "tap_point":
            payload = {"frame_id": current.frame_id, "x": action.get("x"), "y": action.get("y")}
            return self._execute_action("tap_point", payload, exc)[0]
        if tool == "long_press_point":
            payload = {
                "frame_id": current.frame_id,
                "x": action.get("x"),
                "y": action.get("y"),
                "hold_ms": action.get("hold_ms", 650),
            }
            return self._execute_action("long_press_point", payload, exc)[0]
        if tool == "swipe":
            payload = {
                "frame_id": current.frame_id,
                "direction": action.get("direction"),
                "distance": action.get("distance"),
            }
            return self._execute_action("swipe", payload, exc)[0]
        if tool in {
            "wait", "press_home", "launch_reddit", "launch_settings", "launch_ghost"
        }:
            return self._execute_action(tool, dict(action), exc)[0]
        if tool == "workflow":
            name = str(action.get("name") or "")
            if name not in self.workflow_tools:
                raise RecipeDiverged(
                    f"cached workflow tool {name!r} is not authorized in this scope"
                )
            payload = action.get("input") if isinstance(action.get("input"), dict) else {}
            payload = {**payload, "frame_id": current.frame_id}
            return self._execute_action(name, dict(payload), exc)[0]
        raise RecipeDiverged(f"unsupported cached tool {tool!r}")

    def _replay_recipe(
        self, recipe: dict[str, Any], initial: Observation, exc: BaseException
    ) -> RecoverySession:
        # A replay is a fresh verified transaction.  A directive from any
        # earlier failed tool/replay must never survive into it.
        self._requested_directive = None
        self.status("AI recovery — replaying local recipe")
        self.log(f"AI RECOVERY cache hit ({recipe['status']}); verifying every action.")
        expected = list(recipe.get("expected") or [])
        actions = list(recipe.get("actions") or [])
        if not actions or len(actions) != len(expected):
            raise RecipeDiverged("cached recipe is incomplete")
        try:
            observed = initial
            for index, action in enumerate(actions):
                self._check_stop()
                observed = self._replay_action(dict(action), exc)
                if observed.fingerprint != str(expected[index]):
                    raise RecipeDiverged(
                        f"cached action {index + 1} reached an unexpected screen"
                    )
            allowed, reason = self._check_resume_guard(observed)
            if not allowed:
                raise RecipeDiverged(
                    "cached recipe did not satisfy the deterministic resume guard: "
                    f"{reason}"
                )
            directive = ""
            if self._requested_directive is not None:
                directive = self._requested_directive.name
                self._requested_directive = None
            return RecoverySession(
                incident_fingerprint=initial.fingerprint,
                final_fingerprint=observed.fingerprint,
                incident_semantic_key=initial.semantic_key,
                final_semantic_key=observed.semantic_key,
                actions=actions,
                expected_fingerprints=expected,
                model=str(recipe.get("model") or ""),
                source="cache",
                directive=directive,
            )
        except BaseException:
            self._requested_directive = None
            raise

    def _post_stop_aware(self, headers: dict[str, str], body: dict[str, Any]) -> Any:
        self._check_stop()
        try:
            self.attempt_budget.reserve()
        except RecoveryBudgetExhausted:
            self.status("AI recovery stopped — API call limit reached")
            self.log(
                "AI RECOVERY stopped before another paid request: "
                f"attempt API limit {self.attempt_budget.max_calls} reached "
                "without a locally verified postcondition."
            )
            raise
        result: dict[str, Any] = {}
        finished = threading.Event()

        def request_worker() -> None:
            try:
                result["response"] = self._post(
                    self.api_url, headers=headers, json=body, timeout=(45, 90)
                )
            except Exception as request_exc:  # requests errors are retried by caller
                result["error"] = request_exc
            finally:
                finished.set()

        threading.Thread(target=request_worker, name="anthropic-ui-recovery-http", daemon=True).start()
        while not finished.wait(0.25):
            self._check_stop()
        self._check_stop()
        if "error" in result:
            raise result["error"]
        return result["response"]

    def _api_call(self, messages: list[dict[str, Any]], attempt: int) -> tuple[dict[str, Any], str]:
        delay = min(120.0, 5.0 * (2 ** min(5, max(0, attempt - 1))))
        malformed_responses = 0
        while True:
            self._check_stop()
            key, model = load_anthropic_settings()
            if not key:
                self.status("AI recovery — waiting for Anthropic API key")
                self.log(f"AI RECOVERY API key unavailable; retrying in {int(delay)}s.")
                self._wait(delay + random.uniform(0, min(2.0, delay / 10)))
                delay = min(120.0, delay * 2)
                continue
            workspace_id = load_anthropic_workspace_id()
            normalized_messages = _normalize_anthropic_messages(messages)
            if normalized_messages != messages:
                messages[:] = normalized_messages
                self.log(
                    "AI RECOVERY repaired outgoing Anthropic message schema "
                    "before the API call."
                )
            body = {
                "model": model,
                "max_tokens": 1200,
                "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                "tools": self._tool_definitions(),
                "messages": normalized_messages,
            }
            self.status("AI recovery — asking Anthropic")
            started = time.monotonic()
            try:
                prompt_hash = hashlib.sha256(
                    json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()[:16]
                headers = {
                    "Authorization": f"Bearer {key}",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }
                if workspace_id:
                    headers["anthropic-workspace-id"] = workspace_id
                response = self._post_stop_aware(headers, body)
                status_code = int(getattr(response, "status_code", 0))
                if status_code != 200:
                    detail = _anthropic_error_detail(response)
                    if status_code == 401:
                        raise RuntimeError(
                            f"Anthropic HTTP 401 authentication failed."
                            " Create a new active Claude API key in Console > Keys & access "
                            "and check that it belongs to this account."
                            f"{(' API: ' + detail) if detail else ''}"
                        )
                    if status_code == 400 and "workspace" in detail.casefold():
                        raise RuntimeError(
                            "Anthropic HTTP 400 workspace scope failed; set "
                            "anthropic_workspace_id in secrets.json."
                            f"{(' API: ' + detail) if detail else ''}"
                        )
                    if status_code == 400 and _conversation_schema_error(detail):
                        raise AnthropicConversationError(
                            "Anthropic rejected the recovery conversation schema"
                            f"{(': ' + detail) if detail else ''}"
                        )
                    raise RuntimeError(
                        f"Anthropic HTTP {status_code or 'unknown'}"
                        f"{(': ' + detail) if detail else ''}"
                    )
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                    raise AnthropicResponseError("Anthropic returned malformed content")
                data = dict(data)
                data["content"] = _validate_assistant_content(data.get("content"))
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                response_headers = getattr(response, "headers", {}) or {}
                request_id = redact_for_model(
                    response_headers.get("request-id")
                    or response_headers.get("x-request-id")
                    or data.get("id")
                    or "unavailable"
                )[:100]
                self.log(
                    "AI RECOVERY Anthropic response "
                    f"model={redact_for_model(data.get('model') or model)} "
                    f"request_id={request_id} prompt_hash={prompt_hash} "
                    f"latency={time.monotonic() - started:.1f}s "
                    f"input_tokens={int(usage.get('input_tokens') or 0)} "
                    f"output_tokens={int(usage.get('output_tokens') or 0)}"
                )
                return data, model
            except RecoveryStopped:
                raise
            except RecoveryBudgetExhausted:
                raise
            except AnthropicConversationError:
                # Re-sending identical rejected history cannot recover. The
                # recovery loop rebuilds it from a fresh sanitized frame.
                raise
            except AnthropicResponseError as response_exc:
                malformed_responses += 1
                safe = redact_for_model(
                    f"{type(response_exc).__name__}: {response_exc}"
                )[:240]
                self.status("AI recovery — requesting a fresh Anthropic tool turn")
                self.log(
                    "AI RECOVERY malformed Anthropic tool response "
                    f"({safe}); requesting a fresh response."
                )
                if malformed_responses > 1:
                    malformed_delay = min(
                        120.0, 5.0 * (2 ** min(5, malformed_responses - 2))
                    )
                    self._wait(
                        malformed_delay
                        + random.uniform(0, min(2.0, malformed_delay / 10))
                    )
                continue
            except Exception as api_exc:
                safe = redact_for_model(f"{type(api_exc).__name__}: {api_exc}")[:240]
                self.status(f"AI recovery — API retry in {int(delay)}s")
                self.log(f"AI RECOVERY API unavailable ({safe}); retrying in {int(delay)}s.")
                self._wait(delay + random.uniform(0, min(2.0, delay / 10)))
                delay = min(120.0, delay * 2)

    @staticmethod
    def _observation_content(observation: Observation, prefix: str = "") -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        text = (prefix + "\n" if prefix else "") + observation.text_payload()
        content.append({"type": "text", "text": text})
        if observation.redacted_png:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": base64.b64encode(observation.redacted_png).decode("ascii"),
                    },
                }
            )
        return content

    def recover(
        self, exc: BaseException, *, previous: RecoverySession | None = None
    ) -> RecoverySession:
        initial = self.observe(exc)
        deterministic_retry_repeated_incident = False
        if previous is not None:
            previous_semantic_key = str(
                getattr(previous, "incident_semantic_key", "") or ""
            )
            same_incident = (
                previous_semantic_key == initial.semantic_key
                if previous_semantic_key
                else initial.fingerprint == previous.incident_fingerprint
            )
            if same_incident:
                deterministic_retry_repeated_incident = True
                self.chain.pending_sessions[:] = [
                    pending
                    for pending in self.chain.pending_sessions
                    if not (
                        pending is previous
                        or (
                            pending.incident_fingerprint
                            == previous.incident_fingerprint
                            and pending.final_fingerprint
                            == previous.final_fingerprint
                            and pending.source == previous.source
                        )
                    )
                ]
                self.store.quarantine(
                    previous.incident_fingerprint,
                    "deterministic retry returned to the same semantic incident",
                )
                repeated_note = (
                    "deterministic retry returned to the same semantic incident; "
                    "the prior recovery remains unverified and was quarantined"
                )
                self._history.append(repeated_note)
                self.log(f"AI RECOVERY previous recipe quarantined: {repeated_note}.")
            else:
                self.log(
                    "AI RECOVERY prior recovery reached a different semantic "
                    "screen, but remains pending until deterministic code proves "
                    "its postcondition; no recipe was stored or promoted."
                )

        recipe = self.store.get(initial.fingerprint)
        if recipe is not None:
            try:
                return self._replay_recipe(recipe, initial, exc)
            except (RecoveryStopped, OperationCancelled):
                raise
            except Exception as replay_exc:
                self.store.quarantine(initial.fingerprint, str(replay_exc))
                self.log(f"AI RECOVERY cached recipe quarantined: {redact_for_model(replay_exc)}")
                initial = self.observe(exc)
        else:
            self.log("AI RECOVERY cache miss; escalating sanitized frame to Anthropic.")

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": self._observation_content(
                    initial,
                    prefix=(
                        f"A deterministic operation failed with: {initial.redacted_error}\n"
                        f"Sanitized pipeline context: {json.dumps(self.context, sort_keys=True)}\n"
                        + (
                            "A prior resume_pipeline request returned to this same semantic "
                            "incident. Do not repeat an earlier action. Satisfy the stated local "
                            "postcondition before requesting resume_pipeline again.\n"
                            if deterministic_retry_repeated_incident
                            else ""
                        )
                        + "Recover navigation only. Sensitive regions are redacted. "
                        "A changed screenshot alone is never proof of success."
                    ),
                ),
            }
        ]
        actions: list[dict[str, Any]] = []
        expected: list[str] = []
        model = ""
        api_attempt = 0
        conversation_rebuilds = 0
        empty_responses = 0
        while True:
            self._check_stop()
            api_attempt += 1
            removed_images = _compact_prior_images(messages)
            if removed_images:
                self.log(
                    "AI RECOVERY compacted "
                    f"{removed_images} stale screenshot(s) before the next API call."
                )
            try:
                data, model = self._api_call(messages, api_attempt)
            except AnthropicConversationError as conversation_exc:
                conversation_rebuilds += 1
                safe_schema_error = redact_for_model(conversation_exc)[:240]
                self.log(
                    "AI RECOVERY Anthropic conversation was rejected "
                    f"({safe_schema_error}); rebuilding it from a sanitized observation."
                )
                self.status("AI recovery — rebuilding Anthropic conversation")
                current = self.current or initial
                try:
                    current = self.observe(exc)
                except (RecoveryStopped, OperationCancelled):
                    raise
                except Exception as observe_exc:
                    self.log(
                        "AI RECOVERY fresh observation during conversation rebuild "
                        f"was unavailable ({redact_for_model(observe_exc)[:180]}); "
                        "using the last sanitized frame."
                    )
                summary = "; ".join(self._history[-20:]) or "No tool action completed."
                messages = [
                    {
                        "role": "user",
                        "content": self._observation_content(
                            current,
                            prefix=(
                                f"Recovery remains active for: {initial.redacted_error}\n"
                                f"Prior attempt summary: {redact_for_model(summary)}\n"
                                "The previous API conversation was structurally rejected and "
                                "has been safely reset. Continue with one valid recovery tool."
                            ),
                        ),
                    }
                ]
                if conversation_rebuilds > 1:
                    rebuild_delay = min(
                        120.0, 5.0 * (2 ** min(5, conversation_rebuilds - 2))
                    )
                    self._wait(
                        rebuild_delay
                        + random.uniform(0, min(2.0, rebuild_delay / 10))
                    )
                continue
            assistant_content = data.get("content") or []
            if not assistant_content:
                empty_responses += 1
                self.status("AI recovery — Anthropic returned no action; asking again")
                self.log(
                    "AI RECOVERY Anthropic returned empty content; requesting "
                    "another tool turn without adding the empty assistant message."
                )
                if empty_responses > 1:
                    empty_delay = min(
                        120.0, 5.0 * (2 ** min(5, empty_responses - 2))
                    )
                    self._wait(
                        empty_delay
                        + random.uniform(0, min(2.0, empty_delay / 10))
                    )
                continue
            empty_responses = 0
            messages.append({"role": "assistant", "content": assistant_content})
            tool_results: list[dict[str, Any]] = []
            saw_tool = False
            for block in assistant_content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                saw_tool = True
                tool_id = str(block.get("id") or "")
                name = str(block.get("name") or "")
                payload = block.get("input") if isinstance(block.get("input"), dict) else {}
                safe_payload = redact_for_model(json.dumps(payload, sort_keys=True))[:300]
                self.log(f"AI RECOVERY proposed {name} {safe_payload}")
                self._history.append(f"proposed {name} {safe_payload}")
                if name == "resume_pipeline":
                    # Resume always uses a new frame.  A changed screenshot is
                    # not sufficient: failure-specific deterministic code may
                    # impose a stronger postcondition through ``resume_guard``.
                    current = self.observe(exc)
                    resume_pair = (
                        current.semantic_key,
                        self._canonical_action(name, payload, current),
                    )
                    duplicate_resume = resume_pair in self._action_pairs
                    if not duplicate_resume:
                        self._action_pairs.add(resume_pair)
                    allowed, guard_reason = self._check_resume_guard(current)
                    repeated_same_semantic = (
                        deterministic_retry_repeated_incident
                        and current.semantic_key == initial.semantic_key
                    )
                    if duplicate_resume or repeated_same_semantic or not allowed:
                        if duplicate_resume:
                            safe_error = (
                                "resume_pipeline was already rejected on this same "
                                "semantic screen. Perform a different safe action before "
                                "requesting resume again."
                            )
                        elif repeated_same_semantic:
                            safe_error = (
                                "resume_pipeline was rejected because the previous "
                                "deterministic retry returned to the same semantic screen. "
                                "Perform a different safe action and satisfy the recovery goal."
                            )
                        else:
                            safe_error = (
                                "resume_pipeline was rejected by the local deterministic "
                                f"postcondition: {guard_reason}"
                            )
                        self.log(f"AI RECOVERY rejected {name}: {safe_error}")
                        self._history.append(f"rejected {name}: {safe_error}")
                        tool_results.append(
                            _build_tool_result(
                                tool_id,
                                (
                                    f"Action rejected: {safe_error}\n"
                                    "Latest sanitized observation:\n"
                                    f"{current.text_payload()}"
                                ),
                                is_error=True,
                            )
                        )
                        continue
                    self.status("AI recovery — retrying deterministic checkpoint")
                    self.log(
                        "AI RECOVERY resume guard passed; requesting deterministic "
                        "checkpoint retry. Pipeline success is not assumed."
                    )
                    return RecoverySession(
                        incident_fingerprint=initial.fingerprint,
                        final_fingerprint=current.fingerprint,
                        incident_semantic_key=initial.semantic_key,
                        final_semantic_key=current.semantic_key,
                        actions=actions,
                        expected_fingerprints=expected,
                        model=model,
                        source="anthropic",
                    )
                try:
                    observed, recipe_action = self._execute_action(name, payload, exc)
                    if recipe_action is not None:
                        actions.append(recipe_action)
                        expected.append(observed.fingerprint)
                    if self._requested_directive is not None:
                        directive = self._requested_directive
                        if directive.name == "resume_pipeline":
                            allowed, guard_reason = self._check_resume_guard(observed)
                            if not allowed:
                                self._requested_directive = None
                                safe_error = (
                                    "scoped resume directive was rejected by the "
                                    "fresh local deterministic postcondition: "
                                    f"{guard_reason}"
                                )
                                self.log(
                                    f"AI RECOVERY rejected {name}: {safe_error}"
                                )
                                self._history.append(
                                    f"rejected {name}: {safe_error}"
                                )
                                tool_results.append(
                                    _build_tool_result(
                                        tool_id,
                                        (
                                            f"Action rejected: {safe_error}\n"
                                            "Latest sanitized observation:\n"
                                            f"{observed.text_payload()}"
                                        ),
                                        is_error=True,
                                    )
                                )
                                continue
                        self._requested_directive = None
                        self.status(f"AI recovery — {name} verified")
                        self.log(
                            "AI RECOVERY handed a scoped directive to the "
                            f"deterministic supervisor: {directive.name}."
                        )
                        return RecoverySession(
                            incident_fingerprint=initial.fingerprint,
                            final_fingerprint=observed.fingerprint,
                            incident_semantic_key=initial.semantic_key,
                            final_semantic_key=observed.semantic_key,
                            actions=actions,
                            expected_fingerprints=expected,
                            model=model,
                            source="anthropic",
                            directive=directive.name,
                        )
                    content = self._observation_content(
                        observed,
                        prefix="Action accepted; fresh observation follows.",
                    )
                    tool_results.append(
                        _build_tool_result(tool_id, content, is_error=False)
                    )
                    self.status(f"AI recovery — {name} verified")
                except (RecoveryStopped, OperationCancelled):
                    raise
                except Exception as action_exc:
                    safe_error = redact_for_model(action_exc)[:300]
                    self.log(f"AI RECOVERY rejected {name}: {safe_error}")
                    self._history.append(f"rejected {name}: {safe_error}")
                    current = self.current or initial
                    tool_results.append(
                        _build_tool_result(
                            tool_id,
                            (
                                f"Action rejected: {safe_error}. Choose a different safe action.\n"
                                "Latest sanitized observation:\n"
                                f"{current.text_payload()}"
                            ),
                            is_error=True,
                        )
                    )
            if not saw_tool:
                messages.append(
                    {
                        "role": "user",
                        "content": "Use exactly one available tool. Continue recovery; do not merely describe an action.",
                    }
                )
            elif tool_results:
                messages.append({"role": "user", "content": tool_results})
            if len(messages) > 28:
                current = self.current or self.observe(exc)
                summary = "; ".join(self._history[-20:]) or "See rejected actions in this conversation."
                messages = [
                    {
                        "role": "user",
                        "content": self._observation_content(
                            current,
                            prefix=(
                                f"Recovery is still active for: {initial.redacted_error}\n"
                                f"Prior attempt summary: {redact_for_model(summary)}\n"
                                "Continue with a different safe navigation strategy."
                            ),
                        ),
                    }
                ]

    def confirm_success(self, session: RecoverySession | None) -> None:
        if session is None:
            return
        if not session.actions:
            self.log("AI RECOVERY succeeded without a reusable navigation action; no recipe stored.")
            return
        status = self.store.record_success(session)
        self.status(f"AI recovery — recipe {status}")
        self.log(f"AI RECOVERY deterministic checkpoint verified; recipe is now {status}.")


__all__ = [
    "DEFAULT_MODEL", "Observation", "RecipeStore", "RecoveryDirective",
    "RecoveryAttemptBudget", "RecoveryBudgetExhausted", "RecoveryChain",
    "RecoverySession", "RecoveryStopped", "RecoveryWorkflowTool",
    "UIRecoveryAgent", "UnknownVisualStateError", "is_ui_recoverable_error",
    "load_anthropic_settings",
    "load_anthropic_workspace_id",
    "recovery_configured", "redact_for_model",
]
