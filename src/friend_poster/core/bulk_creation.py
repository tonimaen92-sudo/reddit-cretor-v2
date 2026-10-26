"""Crash-safe local queue for sequential Creator batches (no network calls)."""

from __future__ import annotations

import copy
import hashlib
import json
import errno
import ipaddress
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from friend_poster.core.config import LOG_DIR
from friend_poster.services.ip_evidence import validate_attempt

STATE_PATH = Path(LOG_DIR) / "bulk_creation.json"
DRAFT_PATH = Path(LOG_DIR) / "bulk_draft.json"
STAGES = {
    "idle", "ip_pending", "may_have_triggered", "ip_verified", "creating",
    "between", "retry_ip", "retry_creation", "paused", "stopped",
}
RECOVERY_DELAYS_SECONDS = (5, 15, 30, 60, 120)
# A distinct code for the new preserve-and-skip recovery.  It never deletes a
# container or retires an id, so it is deliberately not a "deleted" status.
AUTO_SKIP_FAILURE_CODE = "creation_preserved_skipped"
BULK_INACTIVITY_TIMEOUT_CODE = "bulk_inactivity_timeout"
LEGACY_CREATION_FAILURE_CODE = "legacy_creation_failure"
_LOCK = threading.RLock()
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,28}$")
REDDIT_PASSWORD_MIN_LENGTH = 8
REDDIT_PASSWORD_MAX_LENGTH = 128
RADIO_RECOVERY_VERSION = 1
RADIO_RECOVERY_STATES = frozenset({
    "none",
    "pending",
    "on_confirmed",
    "restore_pending",
    "off_confirmed",
    "verify_pending",
    "completed",
})


class BulkError(RuntimeError):
    """A safe configuration or persistence error without pasted data."""


class BulkSaveError(BulkError):
    """The bulk state file could not be saved; unsaved, so pause the workflow.

    Carries a sanitized OSError detail (errno/winerror/strerror, never a
    path).  ``save_state_cas``/``_persist_bulk`` leave in-memory state and the
    durable file unchanged on this error, so the caller must pause rather than
    keep acting on state that was not persisted.
    """


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def empty_radio_recovery() -> dict:
    """Return the credential-free, crash-safe Android radio journal."""
    return {
        "version": RADIO_RECOVERY_VERSION,
        "state": "none",
        "attempt": 0,
        "head_fingerprint": None,
        "operation_id": None,
        "updated_at_utc": None,
    }


def empty_state() -> dict:
    return {
        "version": 7, "revision": 0, "run_id": None, "writer_id": None,
        "recovery": None,
        "queue": [], "requested": 0, "remaining": 0,
        "stage": "idle", "slot_id": None, "baseline_ip": None,
        "verified_ip": None, "verified_at_utc": None,
        "trigger_deadline_utc": None, "ready_at_utc": None,
        "failure_phase": None, "failure_reason": None, "failure_code": None,
        "failures": [],
        "ip_attempt": None,
        # Number of durable reset-action phases already started for the
        # current queue head.  New runs allow fast ADB, MacroDroid fallback,
        # then one final ADB fallback.  Legacy migration still maps a spent
        # remote phase to one so Resume can finish without replaying it.
        "ip_reset_attempt_count": 0,
        "radio_recovery": empty_radio_recovery(),
        # Durable per-batch policy: newly started batches request automatic
        # deletion of each completed account's local container.  Any batch
        # saved before this flag existed keeps its previous (no-delete)
        # behavior and is never used to delete historical containers.
        "delete_containers": False,
    }


def radio_recovery_head_fingerprint(state: dict) -> str:
    """Return an opaque exact binding for the current slot/queue head.

    Slot ids are the durable, non-secret identities allocated to queue heads.
    Hashing the slot id keeps even that identifier out of the radio journal;
    usernames, passwords, birth dates and IP addresses are never inputs.
    """
    sid = state.get("slot_id") if isinstance(state, dict) else None
    if not isinstance(sid, str) or re.fullmatch(r"[a-z]\d{4,}", sid) is None:
        raise BulkError("Radio recovery requires an exact current slot.")
    return hashlib.sha256(f"bulk-radio-head-v1\0{sid}".encode("ascii")).hexdigest()


def empty_draft() -> dict:
    return {
        "version": 1,
        "count_text": "",
        "births_text": "",
        "swipes_text": "",
        "delays_text": "",
        "usernames_text": "",
        "baseline_queue": [],
    }


def _failure_code_from_reason(reason: object) -> str | None:
    text = str(reason or "").strip().casefold()
    if text.startswith("no 6-digit reddit code in icloud imap"):
        return "reddit_code_timeout"
    return None


def _lines(value: str) -> list[str]:
    return [part.strip() for part in str(value).splitlines()]


def parse_username_password(value: str) -> tuple[str, str]:
    """Parse one optional ``username`` or ``username:password`` editor row.

    Only the first colon is a separator, so a password may itself contain
    colons. Password-bearing rows fail closed instead of silently falling back
    to a generated Reddit username.
    """
    raw = str(value or "").strip()
    if ":" not in raw:
        return raw, ""
    username, _separator, password = raw.partition(":")
    username = username.strip()
    if USERNAME_RE.fullmatch(username) is None:
        raise BulkError("A username:password row has an invalid username.")
    if (
        len(password) < REDDIT_PASSWORD_MIN_LENGTH
        or len(password) > REDDIT_PASSWORD_MAX_LENGTH
        or any(ord(char) < 32 or ord(char) == 127 for char in password)
    ):
        raise BulkError(
            "A username:password row must contain a password of 8–128 printable characters."
        )
    return username, password


def parse_queue(
    count_text: str, births_text: str, swipes_text: str, delays_text: str,
    parse_birth_date: Callable[[str], date],
    *, usernames_text: str = "",
) -> tuple[int, list[dict]]:
    """Validate and return only the requested aligned prefix.

    Extra pasted rows remain editor-draft data for a later run. They neither
    enlarge nor invalidate the durable queue being started now.
    """
    if re.fullmatch(r"[1-9]\d*", str(count_text).strip()) is None:
        raise BulkError("Batch size must be a positive whole number.")
    requested = int(str(count_text).strip())
    births, swipes, delays = map(_lines, (births_text, swipes_text, delays_text))
    available = {
        "birth date": len(births),
        "swipe count": len(swipes),
        "delay": len(delays),
    }
    short = [name for name, count in available.items() if count < requested]
    if short:
        names = ", ".join(short)
        raise BulkError(
            f"Batch size {requested} requires at least {requested} row(s) in: {names}."
        )
    usernames = _lines(usernames_text) if str(usernames_text).strip() else []
    births = births[:requested]
    swipes = swipes[:requested]
    delays = delays[:requested]
    usernames = usernames[:requested]
    seen_usernames: set[str] = set()
    queue = []
    for index, (raw_birth, raw_swipes, raw_delay) in enumerate(zip(births, swipes, delays), 1):
        try:
            if not raw_birth or re.fullmatch(r"[1-9]\d*", raw_swipes) is None:
                raise ValueError()
            birth = parse_birth_date(raw_birth)
            delay = float(raw_delay)
            if not math.isfinite(delay) or not 2.0 <= delay <= 7.0:
                raise ValueError()
        except Exception:
            raise BulkError(f"Entry {index} has an invalid adult birth date, swipe count, or 2–7 second delay.") from None
        item = {
            "birth_date": birth.strftime("%d.%m.%Y"),
            "swipes": int(raw_swipes),
            "delay_seconds": delay,
        }
        # Usernames are optional per row. Missing, malformed, or repeated
        # values fall back to Reddit's suggestion instead of blocking the
        # whole batch. A valid unique value is still used exactly.
        raw_username = usernames[index - 1] if index <= len(usernames) else ""
        username, reddit_password = parse_username_password(raw_username)
        if USERNAME_RE.fullmatch(username) is not None:
            folded = username.casefold()
            if folded not in seen_usernames:
                seen_usernames.add(folded)
                item["username"] = username
                if reddit_password:
                    item["password"] = reddit_password
            elif reddit_password:
                raise BulkError(
                    f"Entry {index} repeats a username with a supplied password."
                )
        queue.append(item)
    return requested, queue


def _validate(state: object) -> dict:
    blank = empty_state()
    if not isinstance(state, dict) or set(state) != set(blank) or state.get("version") != 7:
        raise BulkError("Saved bulk configuration is malformed.")
    if type(state["delete_containers"]) is not bool:
        raise BulkError("Saved bulk configuration is malformed.")
    if type(state["revision"]) is not int or state["revision"] < 0:
        raise BulkError("Saved bulk configuration is malformed.")
    reset_count = state["ip_reset_attempt_count"]
    if type(reset_count) is not int or not 0 <= reset_count <= 3:
        raise BulkError("Saved bulk radio recovery metadata is malformed.")
    radio = state["radio_recovery"]
    if not isinstance(radio, dict) or set(radio) != {
        "version", "state", "attempt", "head_fingerprint", "operation_id",
        "updated_at_utc",
    }:
        raise BulkError("Saved bulk radio recovery metadata is malformed.")
    if (
        type(radio["version"]) is not int
        or radio["version"] != RADIO_RECOVERY_VERSION
        or not isinstance(radio["state"], str)
        or radio["state"] not in RADIO_RECOVERY_STATES
    ):
        raise BulkError("Saved bulk radio recovery metadata is malformed.")
    if type(radio["attempt"]) is not int or radio["attempt"] < 0:
        raise BulkError("Saved bulk radio recovery metadata is malformed.")
    if radio["state"] == "none":
        if any(radio[field] is not None for field in (
            "head_fingerprint", "operation_id", "updated_at_utc",
        )) or radio["attempt"] != 0:
            raise BulkError("Saved bulk radio recovery metadata is malformed.")
    else:
        if radio["attempt"] < 1 or radio["attempt"] != reset_count:
            raise BulkError("Saved bulk radio recovery metadata is malformed.")
        if (
            not isinstance(radio["head_fingerprint"], str)
            or re.fullmatch(r"[0-9a-f]{64}", radio["head_fingerprint"]) is None
            or not isinstance(radio["operation_id"], str)
        ):
            raise BulkError("Saved bulk radio recovery metadata is malformed.")
        try:
            uuid.UUID(radio["operation_id"])
        except (ValueError, TypeError, AttributeError):
            raise BulkError("Saved bulk radio recovery metadata is malformed.") from None
        try:
            stamp = datetime.fromisoformat(str(radio["updated_at_utc"]).replace("Z", "+00:00"))
            if (
                not str(radio["updated_at_utc"]).endswith("Z")
                or stamp.utcoffset() != timezone.utc.utcoffset(stamp)
            ):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise BulkError("Saved bulk radio recovery metadata is malformed.") from None
    for field in ("run_id", "writer_id"):
        value = state[field]
        if value is not None:
            try:
                uuid.UUID(str(value))
            except (ValueError, TypeError, AttributeError):
                raise BulkError("Saved bulk ownership is malformed.") from None
    recovery = state["recovery"]
    if recovery is not None:
        if not isinstance(recovery, dict) or set(recovery) != {
            "status", "attempt", "last_error", "next_retry_at_utc",
        }:
            raise BulkError("Saved bulk recovery metadata is malformed.")
        if recovery["status"] not in {"recovering", "bridge_retries_exhausted"}:
            raise BulkError("Saved bulk recovery metadata is malformed.")
        if type(recovery["attempt"]) is not int or not 1 <= recovery["attempt"] <= 5:
            raise BulkError("Saved bulk recovery metadata is malformed.")
        if not isinstance(recovery["last_error"], str) or len(recovery["last_error"]) > 300:
            raise BulkError("Saved bulk recovery metadata is malformed.")
        stamp = recovery["next_retry_at_utc"]
        if stamp is not None:
            try:
                parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if not str(stamp).endswith("Z") or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise BulkError("Saved bulk recovery metadata is malformed.") from None
    if not isinstance(state["queue"], list) or state["stage"] not in STAGES:
        raise BulkError("Saved bulk configuration is malformed.")
    if not isinstance(state["requested"], int) or not isinstance(state["remaining"], int):
        raise BulkError("Saved bulk configuration is malformed.")
    if state["requested"] < 0 or state["remaining"] < 0 or state["remaining"] > state["requested"]:
        raise BulkError("Saved bulk configuration is malformed.")
    for item in state["queue"]:
        if (
            not isinstance(item, dict) or set(item) not in (
                {"birth_date", "swipes", "delay_seconds"},
                {"birth_date", "swipes", "delay_seconds", "username"},
                {"birth_date", "swipes", "delay_seconds", "username", "password"},
            )
            or not isinstance(item["birth_date"], str)
            or re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", item["birth_date"]) is None
            or type(item["swipes"]) is not int or item["swipes"] < 1
            or not isinstance(item["delay_seconds"], (int, float))
            or not math.isfinite(item["delay_seconds"])
            or not 2 <= item["delay_seconds"] <= 7
            or ("username" in item and (
                not isinstance(item["username"], str)
                or USERNAME_RE.fullmatch(item["username"]) is None
            ))
            or ("password" in item and (
                "username" not in item
                or not isinstance(item["password"], str)
                or len(item["password"]) < REDDIT_PASSWORD_MIN_LENGTH
                or len(item["password"]) > REDDIT_PASSWORD_MAX_LENGTH
                or any(ord(char) < 32 or ord(char) == 127 for char in item["password"])
            ))
        ):
            raise BulkError("Saved bulk configuration is malformed.")
    if state["remaining"] > len(state["queue"]):
        raise BulkError("Saved bulk configuration is malformed.")
    if state["stage"] == "idle" and state["remaining"] != 0:
        raise BulkError("Saved bulk configuration is malformed.")
    if state["stage"] not in ("idle", "stopped") and state["remaining"] < 1:
        raise BulkError("Saved bulk configuration is malformed.")
    sid = state["slot_id"]
    if sid is not None and (not isinstance(sid, str) or re.fullmatch(r"[a-z]\d{4,}", sid) is None):
        raise BulkError("Saved bulk configuration is malformed.")
    if radio["state"] != "none":
        if state["remaining"] < 1 or not state["queue"]:
            raise BulkError("Saved bulk radio recovery metadata is malformed.")
        if radio["head_fingerprint"] != radio_recovery_head_fingerprint(state):
            raise BulkError("Saved bulk radio recovery metadata is malformed.")
    if state["remaining"] == 0 and (
        reset_count != 0 or radio != empty_radio_recovery()
    ):
        raise BulkError("Saved bulk radio recovery metadata is malformed.")
    for field in ("baseline_ip", "verified_ip"):
        value = state[field]
        if value is not None:
            try:
                if not isinstance(value, str) or str(ipaddress.ip_address(value)) != value:
                    raise ValueError()
            except ValueError:
                raise BulkError("Saved bulk configuration is malformed.") from None
    for field in ("verified_at_utc", "trigger_deadline_utc", "ready_at_utc"):
        value = state[field]
        if value is not None:
            try:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if not value.endswith("Z") or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise BulkError("Saved bulk configuration is malformed.") from None
    if state["failure_phase"] not in (None, "ip", "creation"):
        raise BulkError("Saved bulk configuration is malformed.")
    reason = state["failure_reason"]
    if reason is not None and (not isinstance(reason, str) or len(reason) > 300):
        raise BulkError("Saved bulk configuration is malformed.")
    code = state["failure_code"]
    if code is not None and (
        not isinstance(code, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None
    ):
        raise BulkError("Saved bulk configuration is malformed.")
    if not isinstance(state["failures"], list):
        raise BulkError("Saved bulk configuration is malformed.")
    for failure in state["failures"]:
        if not isinstance(failure, dict) or set(failure) != {
            "slot_id", "item", "code", "reason", "failed_at_utc",
        }:
            raise BulkError("Saved bulk configuration is malformed.")
        if re.fullmatch(r"[a-z]\d{4,}", str(failure["slot_id"] or "")) is None:
            raise BulkError("Saved bulk configuration is malformed.")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", str(failure["code"] or "")) is None:
            raise BulkError("Saved bulk configuration is malformed.")
        if not isinstance(failure["reason"], str) or len(failure["reason"]) > 300:
            raise BulkError("Saved bulk configuration is malformed.")
        item = failure["item"]
        if not isinstance(item, dict) or set(item) not in (
            {"birth_date", "swipes", "delay_seconds"},
            {"birth_date", "swipes", "delay_seconds", "username"},
            {"birth_date", "swipes", "delay_seconds", "username", "password"},
        ):
            raise BulkError("Saved bulk configuration is malformed.")
        try:
            stamp = datetime.fromisoformat(str(failure["failed_at_utc"]).replace("Z", "+00:00"))
            if not str(failure["failed_at_utc"]).endswith("Z") or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise BulkError("Saved bulk configuration is malformed.") from None
    if state["stage"] == "may_have_triggered" and (sid is None or state["trigger_deadline_utc"] is None):
        raise BulkError("Saved bulk configuration is malformed.")
    if state["stage"] in ("ip_verified", "creating", "retry_creation") and (
        sid is None or state["verified_ip"] is None or state["verified_at_utc"] is None
    ):
        raise BulkError("Saved bulk configuration is malformed.")
    if state["stage"] == "between" and state["ready_at_utc"] is None:
        raise BulkError("Saved bulk configuration is malformed.")
    if state["ip_attempt"] is not None:
        try:
            validate_attempt(state["ip_attempt"])
            baseline = state["ip_attempt"]["baseline"]
            if (baseline["ip"] if baseline else None) != state["baseline_ip"]:
                raise ValueError()
        except ValueError:
            raise BulkError("Saved bulk IP evidence is malformed.") from None
    return state


def load_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return empty_state()
    try:
        with _LOCK:
            with path.open(encoding="utf-8") as handle:
                state = json.load(handle)
            v6_fields = set(empty_state()) - {
                "ip_reset_attempt_count", "radio_recovery",
            }
            v5_fields = v6_fields - {
                "delete_containers",
            }
            v4_fields = v5_fields - {
                "revision", "run_id", "writer_id", "recovery",
            }
            v3_fields = v4_fields - {"failure_code", "failures"}
            v2_fields = v3_fields - {"failure_reason"}
            legacy_fields = v2_fields - {"ip_attempt"}
            if isinstance(state, dict) and state.get("version") == 6 and set(state) == v6_fields:
                # Version 6 had no durable distinction between a spent remote
                # MacroDroid phase and an available local ADB phase.  Preserve
                # a known spent phase as one (not two) so Resume cannot resend
                # the webhook, while still allowing one local recovery phase.
                reset_count = 1 if (
                    state.get("remaining", 0) > 0
                    and (
                        state.get("failure_code") in {
                            "ip_auto_retry_pending",
                            "ip_auto_retry_scheduled",
                            "ip_retry_exhausted",
                        }
                        or state.get("stage") in {"may_have_triggered", "retry_ip"}
                        or (
                            state.get("failure_phase") == "ip"
                            and state.get("baseline_ip")
                            and state.get("ip_attempt")
                        )
                    )
                ) else 0
                migrated = {
                    **state,
                    "version": 7,
                    "ip_reset_attempt_count": reset_count,
                    "radio_recovery": empty_radio_recovery(),
                }
                _validate(migrated)
                save_state(migrated, path)
                return migrated
            if isinstance(state, dict) and state.get("version") == 5 and set(state) == v5_fields:
                # A pre-policy batch keeps its previous behavior: no automatic
                # container deletion, and no retroactive deletion of anything.
                migrated = {
                    **state, "version": 7, "delete_containers": False,
                    "ip_reset_attempt_count": 0,
                    "radio_recovery": empty_radio_recovery(),
                }
                _validate(migrated)
                save_state(migrated, path)
                return migrated
            if isinstance(state, dict) and state.get("version") == 4 and set(state) == v4_fields:
                migrated = {
                    **state, "version": 7, "revision": 0, "run_id": None,
                    "writer_id": None, "recovery": None,
                    "delete_containers": False,
                    "ip_reset_attempt_count": 0,
                    "radio_recovery": empty_radio_recovery(),
                }
                _validate(migrated)
                save_state(migrated, path)
                return migrated
            if isinstance(state, dict) and state.get("version") == 1 and set(state) == legacy_fields:
                migrated = {
                    **state, "version": 7, "ip_attempt": None,
                    "failure_reason": None, "failure_code": None, "failures": [],
                    "revision": 0, "run_id": None, "writer_id": None,
                    "recovery": None,
                    "delete_containers": False,
                    "ip_reset_attempt_count": 0,
                    "radio_recovery": empty_radio_recovery(),
                }
                _validate(migrated)
                save_state(migrated, path)
                return migrated
            if isinstance(state, dict) and state.get("version") == 2 and set(state) == v2_fields:
                migrated = {
                    **state, "version": 7, "failure_reason": None,
                    "failure_code": None, "failures": [],
                    "revision": 0, "run_id": None, "writer_id": None,
                    "recovery": None,
                    "delete_containers": False,
                    "ip_reset_attempt_count": 0,
                    "radio_recovery": empty_radio_recovery(),
                }
                _validate(migrated)
                save_state(migrated, path)
                return migrated
            if isinstance(state, dict) and state.get("version") == 3 and set(state) == v3_fields:
                migrated = {
                    **state, "version": 7,
                    "failure_code": _failure_code_from_reason(state.get("failure_reason")),
                    "failures": [],
                    "revision": 0, "run_id": None, "writer_id": None,
                    "recovery": None,
                    "delete_containers": False,
                    "ip_reset_attempt_count": 0,
                    "radio_recovery": empty_radio_recovery(),
                }
                _validate(migrated)
                save_state(migrated, path)
                return migrated
            return _validate(state)
    except (OSError, ValueError, TypeError):
        raise BulkError("Saved bulk configuration could not be read safely.") from None


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    """Atomically save state; retry transient sharing errors, then fail loud.

    A recognized transient sharing/lock error (Windows sharing violation
    winerror 32/33, or errno EACCES/EAGAIN/EBUSY) is retried at most twice.
    Any unresolved failure raises :class:`BulkSaveError` (a ``BulkError``)
    carrying a sanitized OSError detail (errno/winerror/strerror only — never
    a filesystem path), so the caller can log the actual cause and pause the
    workflow instead of continuing on unsaved state.  The save stays atomic:
    a temp file is ``os.replace``-d over the target, so a failure never
    leaves a half-written state file.
    """
    _validate(state)
    transient = (PermissionError, BlockingIOError)
    transient_winerrors = {32, 33}  # ERROR_SHARING_VIOLATION / LOCK_VIOLATION
    transient_errnos = {errno.EACCES, errno.EAGAIN, errno.EBUSY}
    attempts = 0
    last_detail = ""
    while True:
        attempts += 1
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _LOCK, tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=".bulk-", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            return
        except OSError as exc:
            last_detail = _sanitized_os_error(exc)
            winerror = getattr(exc, "winerror", None)
            is_transient = (
                isinstance(exc, transient)
                or (winerror is not None and winerror in transient_winerrors)
                or (getattr(exc, "errno", None) in transient_errnos)
            )
            # Retry a recognized transient sharing error at most twice.
            if is_transient and attempts <= 2:
                time.sleep(0.25 * attempts)
                continue
            raise BulkSaveError(
                f"Bulk configuration could not be saved ({last_detail})."
            ) from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _sanitized_os_error(exc: OSError) -> str:
    """Machine-readable OSError detail with no filesystem path or secret."""
    parts = [type(exc).__name__]
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        parts.append(f"winerror={winerror}")
    errno_name = getattr(exc, "errno", None)
    if errno_name is not None:
        try:
            parts.append(f"errno={errno_name}({errno.errorcode.get(errno_name, '?')})")
        except Exception:
            parts.append(f"errno={errno_name}")
    strerror = getattr(exc, "strerror", None)
    if strerror:
        # strerror is OS text ("Access is denied"); it never carries a path.
        parts.append(str(strerror)[:120])
    return " | ".join(p for p in parts if p)


class _StateFileLock:
    """Small cross-process lock used only around revision compare-and-swap."""

    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        self.path = path.with_suffix(path.suffix + ".lock")
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, f"{os.getpid()}\n".encode("ascii"))
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise BulkError("Bulk state is busy; no stale write was made.") from None
                time.sleep(0.05)

    def __exit__(self, _kind, _value, _traceback) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def save_state_cas(
    state: dict,
    expected_revision: int,
    path: Path = STATE_PATH,
    *,
    allow_writer_takeover: bool = False,
) -> dict:
    """Atomically save one newer revision, rejecting stale or foreign writers."""
    _validate(state)
    if type(expected_revision) is not int or expected_revision < 0:
        raise BulkError("Bulk state revision is malformed.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _StateFileLock(path), _LOCK:
        current = load_state(path) if path.exists() else empty_state()
        if current["revision"] != expected_revision:
            raise BulkError(
                "Batch state changed in another Creator process; stale write rejected."
            )
        current_writer = current.get("writer_id")
        next_writer = state.get("writer_id")
        if (
            current_writer and next_writer and current_writer != next_writer
            and not allow_writer_takeover
        ):
            raise BulkError("Another Creator process owns this batch; stale write rejected.")
        updated = copy.deepcopy(state)
        updated["revision"] = expected_revision + 1
        _validate(updated)
        save_state(updated, path)
        return updated


def claim_state(state: dict, writer_id: str, *, new_run: bool = False) -> dict:
    """Return an ownership claim to persist with save_state_cas."""
    _validate(state)
    try:
        uuid.UUID(writer_id)
    except (ValueError, TypeError, AttributeError):
        raise BulkError("Creator writer identity is malformed.") from None
    updated = copy.deepcopy(state)
    updated["writer_id"] = writer_id
    if new_run or not updated.get("run_id"):
        updated["run_id"] = str(uuid.uuid4())
    return updated


def _validate_draft(draft: object) -> dict:
    blank = empty_draft()
    if not isinstance(draft, dict) or set(draft) != set(blank) or draft.get("version") != 1:
        raise BulkError("Saved batch draft is malformed.")
    for field in ("count_text", "births_text", "swipes_text", "delays_text", "usernames_text"):
        if not isinstance(draft[field], str):
            raise BulkError("Saved batch draft is malformed.")
    probe = empty_state()
    probe["queue"] = copy.deepcopy(draft["baseline_queue"])
    _validate(probe)
    return draft


def load_draft(path: Path = DRAFT_PATH) -> dict:
    if not path.exists():
        return empty_draft()
    try:
        with _LOCK, path.open(encoding="utf-8") as handle:
            return _validate_draft(json.load(handle))
    except (OSError, ValueError, TypeError):
        raise BulkError("Saved batch draft could not be read safely.") from None


def save_draft(draft: dict, path: Path = DRAFT_PATH) -> None:
    _validate_draft(draft)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=".bulk-draft-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(draft, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        raise BulkError("Batch draft could not be saved.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def extend_queue(state: dict, additions: list[dict]) -> dict:
    """Schedule every existing unused tail plus validated appended rows once."""
    _validate(state)
    if state["stage"] == "idle" or state["remaining"] < 1:
        raise BulkError("There is no active, paused, or stopped batch to extend.")
    if not isinstance(additions, list):
        raise BulkError("Batch additions are malformed.")

    probe = empty_state()
    probe["queue"] = copy.deepcopy(additions)
    _validate(probe)

    existing_names = {
        str(item.get("username") or "").casefold()
        for item in state["queue"]
        if str(item.get("username") or "").strip()
    }
    existing_names.update(
        str(failure.get("item", {}).get("username") or "").casefold()
        for failure in state["failures"]
        if str(failure.get("item", {}).get("username") or "").strip()
    )
    for index, item in enumerate(additions, 1):
        username = str(item.get("username") or "").strip()
        if username and username.casefold() in existing_names:
            raise BulkError(f"Added entry {index} repeats a queued or failed username.")
        if username:
            existing_names.add(username.casefold())

    unused_tail = len(state["queue"]) - state["remaining"]
    added_count = unused_tail + len(additions)
    if added_count < 1:
        raise BulkError("There are no new rows to add to this batch.")

    updated = copy.deepcopy(state)
    updated["queue"] = [*updated["queue"], *copy.deepcopy(additions)]
    updated["requested"] += added_count
    updated["remaining"] += added_count
    _validate(updated)
    return updated


def _unique_archive_path(path: Path, label: str, stamp: str) -> Path:
    candidate = path.with_name(f"{path.stem}.{label}-{stamp}{path.suffix}")
    serial = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}.{label}-{stamp}-{serial}{path.suffix}")
        serial += 1
    return candidate


def archive_state(
    state: dict,
    path: Path = STATE_PATH,
    *,
    stamp: datetime | None = None,
    label: str = "pre-reconcile",
) -> Path:
    """Durably preserve a stopped state before replacing its unused tail."""
    _validate(state)
    if re.fullmatch(r"[a-z][a-z-]*", label) is None:
        raise BulkError("Archive label is malformed.")
    moment = (stamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidate = _unique_archive_path(path, label, moment.strftime("%Y%m%d-%H%M%S"))
    save_state(copy.deepcopy(state), candidate)
    return candidate


def archive_draft(
    path: Path = DRAFT_PATH,
    *,
    stamp: datetime | None = None,
    label: str = "superseded",
) -> Path | None:
    """Durably preserve the raw editor draft before a fresh batch replaces it.

    Returns ``None`` when no draft file exists yet.  A failed copy raises
    ``BulkError`` so callers never overwrite a draft they could not preserve.
    """
    if not path.exists():
        return None
    if re.fullmatch(r"[a-z][a-z-]*", label) is None:
        raise BulkError("Archive label is malformed.")
    moment = (stamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidate = _unique_archive_path(path, label, moment.strftime("%Y%m%d-%H%M%S"))
    temporary = None
    try:
        with path.open("rb") as source, tempfile.NamedTemporaryFile(
            "wb", dir=str(candidate.parent),
            prefix=".bulk-draft-archive-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            shutil.copyfileobj(source, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, candidate)
    except OSError:
        raise BulkError("Batch draft could not be archived.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return candidate


def reconcile_stopped_start(
    current: dict,
    new_items: list[dict],
    requested: int,
    *,
    slot_id: str,
    saved_birth: str,
    now: datetime | None = None,
) -> dict:
    """Prepend one stopped recovery head and replace its unused queue tail."""
    _validate(current)
    if current["stage"] != "stopped":
        raise BulkError("Batch reconciliation requires a stopped batch.")
    if current["remaining"] < 1 or not current["queue"]:
        raise BulkError("The stopped batch has no recovery head.")
    if current.get("slot_id") != slot_id:
        raise BulkError(
            f"Stopped batch points to {current.get('slot_id') or 'no slot'}, but the "
            f"unfinished slot is {slot_id}. No queue data was changed."
        )
    saved_birth = str(saved_birth or "").strip()
    head_birth = str(current["queue"][0].get("birth_date") or "").strip()
    if not saved_birth or head_birth != saved_birth:
        raise BulkError(
            f"Stopped queue head birth date {head_birth or 'missing'} does not match "
            f"{slot_id}'s saved birth date {saved_birth or 'missing'}. "
            "No queue data was changed."
        )
    if type(requested) is not int or requested < 1 or requested > len(new_items):
        raise BulkError("The new batch entries are incomplete.")

    appended = copy.deepcopy(new_items[:requested])
    probe = empty_state()
    probe.update(
        queue=appended,
        requested=requested,
        remaining=requested,
        stage="ip_pending",
    )
    _validate(probe)

    updated = copy.deepcopy(current)
    updated["queue"] = [copy.deepcopy(current["queue"][0]), *appended]
    updated["requested"] = len(updated["queue"])
    updated["remaining"] = len(updated["queue"])
    updated["ready_at_utc"] = None
    updated["failure_phase"] = None
    updated["failure_reason"] = None

    if updated.get("verified_ip") and updated.get("verified_at_utc"):
        updated["stage"] = "retry_creation"
    elif updated.get("ip_attempt") and updated.get("baseline_ip"):
        updated["stage"] = "retry_ip"
        attempt = updated["ip_attempt"]
        moment = now or datetime.now(timezone.utc)
        attempt.update(
            candidate=None,
            verification_evidence=None,
            confirmation_floor=attempt["check_sequence"],
            observation_deadline_utc=(moment + timedelta(seconds=180))
            .isoformat()
            .replace("+00:00", "Z"),
        )
        updated["trigger_deadline_utc"] = attempt["observation_deadline_utc"]
    else:
        updated["stage"] = "ip_pending"
        updated["baseline_ip"] = None
        updated["verified_ip"] = None
        updated["verified_at_utc"] = None
        updated["trigger_deadline_utc"] = None
        updated["ip_attempt"] = None

    _validate(updated)
    return updated


def prepare_retry(state: dict, *, now: datetime | None = None) -> dict:
    """Resume a paused stage without replaying an in-flight reset.

    A paused IP attempt with a durable baseline remains observation-only.  A
    legacy/exhausted attempt that never had a baseline cannot be observed for a
    change; after its deadline has ended, Retry starts a new baseline-first
    operation and clears the unusable attempt.
    """
    _validate(state)
    if state["stage"] != "paused":
        raise BulkError("There is no paused batch to retry.")
    updated = copy.deepcopy(state)
    updated["failure_reason"] = None
    attempt = updated["ip_attempt"]
    if updated["failure_phase"] != "ip":
        updated["stage"] = "retry_creation"
    elif updated.get("baseline_ip") and attempt and attempt.get("baseline"):
        updated["stage"] = "retry_ip"
        stamp = now or datetime.now(timezone.utc)
        attempt.update(candidate=None, verification_evidence=None,
                       confirmation_floor=attempt["check_sequence"],
                       observation_deadline_utc=(stamp + timedelta(seconds=180)).isoformat().replace("+00:00", "Z"))
        updated["trigger_deadline_utc"] = attempt["observation_deadline_utc"]
    else:
        updated["stage"] = "ip_pending"
        updated["baseline_ip"] = None
        updated["verified_ip"] = None
        updated["verified_at_utc"] = None
        updated["trigger_deadline_utc"] = None
        updated["ip_attempt"] = None
    _validate(updated)
    return updated


def consume_completed(state: dict, slot_id: str, *, ready_at_utc: str) -> dict:
    """Remove the one in-flight triple after verified slot completion."""
    _validate(state)
    if state["slot_id"] != slot_id or state["remaining"] < 1 or not state["queue"]:
        raise BulkError("Batch completion does not match its saved queue head.")
    next_state = dict(state)
    next_state["queue"] = state["queue"][1:]
    next_state["remaining"] -= 1
    next_state["stage"] = "between" if next_state["remaining"] else "idle"
    next_state["slot_id"] = None
    # A delete-enabled creation batch starts every next account only after the
    # prior container was removed and a new IP was verified.  Clearing this
    # proof makes the normal next ``ip_pending`` stage submit exactly one reset.
    # Legacy/non-deleting batches retain their original one-IP-per-batch rule.
    if not next_state["remaining"] or bool(next_state.get("delete_containers")):
        next_state["baseline_ip"] = None
        next_state["verified_ip"] = None
        next_state["verified_at_utc"] = None
    next_state["trigger_deadline_utc"] = None
    next_state["ready_at_utc"] = ready_at_utc if next_state["remaining"] else None
    next_state["failure_phase"] = None
    next_state["failure_reason"] = None
    next_state["failure_code"] = None
    next_state["ip_attempt"] = None
    next_state["ip_reset_attempt_count"] = 0
    next_state["radio_recovery"] = empty_radio_recovery()
    _validate(next_state)
    return next_state


def consume_failed(
    state: dict,
    slot_id: str,
    *,
    code: str,
    reason: str,
    ready_at_utc: str,
) -> dict:
    """Record and remove one terminally failed queue head, then continue safely."""
    _validate(state)
    if state["slot_id"] != slot_id or state["remaining"] < 1 or not state["queue"]:
        raise BulkError("Batch failure does not match its saved queue head.")
    code = str(code or "creation_failed")[:80]
    reason = str(reason or "Creation failed.")[:300]
    if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None:
        raise BulkError("Batch failure code is malformed.")
    next_state = dict(state)
    next_state["queue"] = state["queue"][1:]
    next_state["remaining"] -= 1
    next_state["failures"] = [
        *state["failures"],
        {
            "slot_id": slot_id,
            "item": copy.deepcopy(state["queue"][0]),
            "code": code,
            "reason": reason,
            "failed_at_utc": utc_now(),
        },
    ]
    next_state["stage"] = "between" if next_state["remaining"] else "idle"
    next_state["slot_id"] = None
    # A delete-enabled creation batch treats every successor as a genuinely
    # new account attempt.  Whether the prior head completed or was explicitly
    # preserved-and-skipped, the next slot must start at ip_pending and submit
    # its own verified reset.  Legacy/non-deleting batches retain the original
    # shared-IP behavior.
    if not next_state["remaining"] or bool(next_state.get("delete_containers")):
        next_state["baseline_ip"] = None
        next_state["verified_ip"] = None
        next_state["verified_at_utc"] = None
    next_state["trigger_deadline_utc"] = None
    next_state["ready_at_utc"] = ready_at_utc if next_state["remaining"] else None
    next_state["failure_phase"] = None
    next_state["failure_reason"] = None
    next_state["failure_code"] = None
    next_state["ip_attempt"] = None
    next_state["ip_reset_attempt_count"] = 0
    next_state["radio_recovery"] = empty_radio_recovery()
    _validate(next_state)
    return next_state


def consume_skipped(
    state: dict,
    slot_id: str,
    *,
    code: str,
    reason: str,
    ready_at_utc: str,
) -> dict:
    """Record one preserved "skipped by bulk" queue head and advance exactly once.

    This shares :func:`consume_failed`'s validation, writer-ownership guard and
    single-head consumption so a crash cannot consume twice or retry the slot.
    The preserved attempt's container, alias and checkpoints are untouched.
    """
    return consume_failed(
        state, slot_id, code=code, reason=reason, ready_at_utc=ready_at_utc
    )


def consume_stalled(
    state: dict,
    slot_id: str,
    *,
    ready_at_utc: str,
) -> dict:
    """Drop one stalled queue head without archiving its credentials.

    A confirmed inactivity stall discards the whole account attempt: the caller
    has already deleted that attempt's container and recorded the slot as
    abandoned.  Nothing from the attempt (username, password, birth date) is
    copied into ``failures``, so a later resume can never resurrect it.  The
    next queue head supplies a fresh identity.

    The IP proof is always cleared: a stall reset must start the next attempt
    with a freshly verified address, even for a legacy non-deleting batch.
    """
    _validate(state)
    if state["slot_id"] != slot_id or state["remaining"] < 1 or not state["queue"]:
        raise BulkError("Batch stall reset does not match its saved queue head.")
    next_state = dict(state)
    next_state["queue"] = state["queue"][1:]
    next_state["remaining"] -= 1
    next_state["stage"] = "between" if next_state["remaining"] else "idle"
    next_state["slot_id"] = None
    next_state["baseline_ip"] = None
    next_state["verified_ip"] = None
    next_state["verified_at_utc"] = None
    next_state["trigger_deadline_utc"] = None
    next_state["ready_at_utc"] = ready_at_utc if next_state["remaining"] else None
    next_state["failure_phase"] = None
    next_state["failure_reason"] = None
    next_state["failure_code"] = None
    next_state["ip_attempt"] = None
    next_state["ip_reset_attempt_count"] = 0
    next_state["radio_recovery"] = empty_radio_recovery()
    _validate(next_state)
    return next_state
