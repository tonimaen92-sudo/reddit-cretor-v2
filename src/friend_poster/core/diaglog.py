# UTF-8
"""One shared diagnostic stream for the Creator panel.

Every Creator execution message flows through :class:`DiagnosticStream`, which

* sanitizes the text (secrets/credentials/email bodies are redacted),
* classifies it (severity, workflow stage, slot, outcome),
* persists it to disk *independently of the GUI* so a busy panel cannot lose
  history, and
* fans it out to subscribers (the Log tab drains them on the Tk thread).

The module is deliberately import-safe: no phone, no GUI, no Tk at import time,
so it can be unit-tested offline and imported from ``core`` by ``device`` code.

Compatibility note: existing ``log(message)`` callers keep working; nothing here
changes control flow, retries, timeouts or queue decisions. Logging never
touches :data:`friend_poster.core.slots.CREATION_PROGRESS`, so it can never reset
the inactivity watchdog.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback as _traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from friend_poster.core.paths import LOG_DIR

# --------------------------------------------------------------------------- #
# Severity
# --------------------------------------------------------------------------- #

_SEVERITY_ORDER = {
    "DEBUG": 10,
    "STATUS": 15,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}
_SEVERITY_LABEL = {
    "DEBUG": "DBG",
    "STATUS": "STAT",
    "INFO": "INFO",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "CRITICAL": "CRIT",
}


def severity_order(severity: str) -> int:
    return _SEVERITY_ORDER.get(str(severity).upper(), 20)


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

_URL_CRED_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+)(?::([^@/\s]+))?@"
)
_AUTH_RE = re.compile(
    r"(?i)\b(authorization|auth|bearer|proxy-authorization)\b(\s*[:=]\s*)"
    r"([^\s,;]+(?:\s+[^\s,;]+)?)"
)
_TOKEN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(access[_-]?token|refresh[_-]?token|token|"
    r"api[_-]?key|apikey|secret|client[_-]?secret|private[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;'\"]+)"
)
_PASSWORD_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(password|passwd|passphrase|pwd)(\s*[:=]\s*)"
    r"([^\s,;'\"]+)"
)
_CODE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])((?:verification\s+|reddit\s+|email\s+)?code)"
    r"(\s*[:=]?\s*)(\d{4,8})\b"
)
_COOKIE_RE = re.compile(
    r"(?i)\b(cookie|set-cookie)(\s*[:=]\s*)([^\n]+)"
)
_BODY_RE = re.compile(
    r"(?i)\b(body|payload|request_body|response_body|response_text|"
    r"response_json|html)\b(\s*[:=]\s*)(.{1,})"
)
_EMAIL_RE = re.compile(
    r"(?<![\w.%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})"
)

_REDACTED = "<redacted>"


def _mask_email(match: re.Match) -> str:
    local = match.group(1)
    domain = match.group(2)
    kept = local[0] if local else "*"
    return f"{kept}***@{domain}"


def redact(text: object) -> str:
    """Return *text* with secrets and account emails masked.

    Order matters: URL credentials are removed before email masking so a
    ``socks5h://user:pass@host`` proxy URL never leaks the password and never
    half-masks as an address.
    """
    value = "" if text is None else str(text)
    if not value:
        return value
    value = _URL_CRED_RE.sub(lambda m: f"{m.group(1)}***:***@", value)
    value = _AUTH_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", value)
    value = _TOKEN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", value)
    value = _PASSWORD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", value)
    value = _CODE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", value)
    value = _COOKIE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", value)
    value = _BODY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted> body", value)
    value = _EMAIL_RE.sub(_mask_email, value)
    return value


def redact_traceback(text: object) -> str:
    """Redact a traceback string without adding local-variable dumps."""
    return redact(text)


# --------------------------------------------------------------------------- #
# Event classification (derived from existing log() text)
# --------------------------------------------------------------------------- #

_SLOT_RE = re.compile(r"(?<![A-Za-z0-9])([a-z]\d{4,5})(?![A-Za-z0-9])")

_STAGE_TOKENS = {
    "PIPELINE": "pipeline",
    "CONTAINER": "container",
    "EMAIL": "email",
    "REDDIT": "reddit",
    "DELETE": "delete",
    "COMPLETED": "pipeline",
    "FAILED": "pipeline",
    "CANCELLED": "pipeline",
    "TIMEOUT": "pipeline",
    "BATCH": "batch",
    "IP": "ip",
    "HEARTBEAT": "status",
}
_LEAD_TOKEN_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]+)\b")
_STATUS_RE = re.compile(
    r"\b(IN_PROGRESS|READY|FAILED|CANCELLED|TIMEOUT|COMPLETED)\b"
)
_ERROR_HINTS = ("ERROR", "FAILED", "CRITICAL", "TRACEBACK", "EXCEPTION")
_WARN_HINTS = ("WARNING", "WARN ", "CANCELLED", "TIMEOUT", "SKIPPED", "REFUS")


def classify(message: str) -> dict:
    """Derive severity/stage/status/slot from an existing log message."""
    text = str(message)
    upper = text.upper()
    severity = "INFO"
    if any(h in upper for h in _ERROR_HINTS):
        severity = "ERROR"
    elif any(h in upper for h in _WARN_HINTS):
        severity = "WARNING"

    stage = ""
    status = ""
    match = _LEAD_TOKEN_RE.match(text)
    if match and match.group(1) in _STAGE_TOKENS:
        token = match.group(1)
        stage = _STAGE_TOKENS[token]
        if token in {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}:
            status = token
    status_match = _STATUS_RE.search(upper)
    if status_match:
        status = status_match.group(1)
    slot_match = _SLOT_RE.search(text)
    slot_id = slot_match.group(1) if slot_match else ""
    return {
        "severity": severity,
        "stage": stage,
        "status": status,
        "slot_id": slot_id,
    }


# --------------------------------------------------------------------------- #
# Origin (module/function) capture
# --------------------------------------------------------------------------- #

_SKIP_ORIGIN_MODULES = {
    "friend_poster.core.diaglog",
    "<string>",
}


def _origin() -> tuple[str, str]:
    """Return (module, function) of the first frame outside this module."""
    try:
        frame = sys._getframe(2)
    except Exception:  # pragma: no cover - interpreter without frames
        return "unknown", "unknown"
    depth = 0
    while frame is not None and depth < 16:
        module = str(frame.f_globals.get("__name__") or "")
        name = frame.f_code.co_name
        if module not in _SKIP_ORIGIN_MODULES and name != "log":
            short = module.rsplit(".", 1)[-1] if module else "unknown"
            return short, str(name)
        frame = frame.f_back
        depth += 1
    return "unknown", "unknown"


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass
class DiagEvent:
    seq: int
    ts: str  # ISO-8601 UTC with milliseconds
    epoch: float
    severity: str
    message: str
    module: str = ""
    function: str = ""
    run_id: str = ""
    attempt: int = 0
    slot_id: str = ""
    stage: str = ""
    operation: str = ""
    duration_ms: Optional[float] = None
    outcome: str = ""
    retry: str = ""
    status: str = ""
    kind: str = ""
    frame: str = ""
    extra: dict = field(default_factory=dict)

    def context_label(self) -> str:
        parts = []
        if self.run_id:
            parts.append(f"run={self.run_id}")
        if self.attempt:
            parts.append(f"try={self.attempt}")
        if self.slot_id:
            parts.append(self.slot_id)
        if self.stage:
            parts.append(self.stage)
        return " ".join(parts)

    def render(self) -> str:
        """One sanitized panel line."""
        clock = self.ts[11:23].replace("T", " ") if "T" in self.ts else self.ts
        sev = _SEVERITY_LABEL.get(self.severity, self.severity[:4])
        origin = ""
        if self.module or self.function:
            origin = f"{self.module}:{self.function}"
        ctx = self.context_label()
        head = f"{clock} {sev:<5}"
        if ctx:
            head += f" [{ctx}]"
        if origin:
            head += f" {origin}"
        tail = self.message
        if self.duration_ms is not None:
            tail += f"  ({_fmt_duration(self.duration_ms)})"
        if self.retry:
            tail += f"  retry={self.retry}"
        if self.outcome and self.outcome not in {"info", "start", "ok"}:
            tail += f"  -> {self.outcome}"
        return f"{head}  {tail}"

    def to_json(self) -> str:
        payload = {
            "seq": self.seq,
            "ts": self.ts,
            "severity": self.severity,
            "message": self.message,
            "module": self.module,
            "function": self.function,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "slot_id": self.slot_id,
            "stage": self.stage,
            "operation": self.operation,
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
            "retry": self.retry,
            "status": self.status,
            "kind": self.kind,
            "frame": self.frame,
            "extra": self.extra,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _fmt_duration(ms: Optional[float]) -> str:
    if ms is None:
        return "unknown"
    if ms < 1000:
        return f"{ms:.0f}ms"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    return f"{minutes}m{sec:02d}s"


@dataclass
class FailureBlock:
    """A clearly marked failure report, safe to copy/paste."""

    when: str
    run_id: str = ""
    attempt: int = 0
    slot_id: str = ""
    stage: str = ""
    operation: str = ""
    kind: str = "unexpected"
    exception_type: str = ""
    exception_message: str = ""
    failure_code: str = ""
    disposition: str = "unknown"
    queue: str = "unknown"
    preservation: str = "unknown"
    last_checkpoint: str = "unknown"
    last_operation: str = "unknown"
    cleanup_errors: list[str] = field(default_factory=list)
    dumps: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    traceback_text: str = ""
    note: str = ""

    HEADER = "================ CREATOR FAILURE ================"
    FOOTER = "================================================="

    def text(self) -> str:
        lines = [self.HEADER, f"when: {self.when}"]
        ctx = []
        if self.run_id:
            ctx.append(f"run={self.run_id}")
        if self.attempt:
            ctx.append(f"attempt={self.attempt}")
        if self.slot_id:
            ctx.append(f"slot={self.slot_id}")
        if self.stage:
            ctx.append(f"stage={self.stage}")
        if self.operation:
            ctx.append(f"operation={self.operation}")
        lines.append("context: " + (" ".join(ctx) if ctx else "unknown"))
        lines.append(f"kind: {self.kind}")
        if self.exception_type or self.exception_message:
            lines.append(
                f"exception: {self.exception_type}: {self.exception_message}".rstrip(": ")
            )
        else:
            lines.append("exception: none (no exception raised)")
        lines.append(f"failure_code: {self.failure_code or 'unknown'}")
        lines.append(f"disposition: {self.disposition}")
        lines.append(f"queue: {self.queue}")
        lines.append(f"preservation: {self.preservation}")
        lines.append(f"last checkpoint: {self.last_checkpoint}")
        lines.append(f"last operation: {self.last_operation}")
        if self.cleanup_errors:
            lines.append("cleanup errors: " + " | ".join(self.cleanup_errors))
        if self.dumps:
            lines.append("dumps: " + ", ".join(self.dumps))
        if self.screenshots:
            lines.append("screenshots: " + ", ".join(self.screenshots))
        if self.note:
            lines.append(f"note: {self.note}")
        if self.traceback_text.strip():
            lines.append("traceback:")
            lines.append(self.traceback_text.rstrip("\n"))
        else:
            lines.append("traceback: none captured")
        lines.append(self.FOOTER)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Stage timing tracker
# --------------------------------------------------------------------------- #


class _StageTracker:
    """Derive per-slot stage durations from IN_PROGRESS/READY log lines."""

    def __init__(self) -> None:
        self._open: dict[tuple[str, str], float] = {}

    def note(self, slot_id: str, stage: str, status: str, epoch: float) -> Optional[float]:
        if not slot_id or not stage:
            return None
        key = (slot_id, stage)
        if status == "IN_PROGRESS":
            self._open[key] = epoch
            return None
        if status in {"READY", "FAILED", "CANCELLED", "TIMEOUT", "COMPLETED"}:
            started = self._open.pop(key, None)
            if started is not None:
                return max(0.0, (epoch - started) * 1000.0)
        return None


# --------------------------------------------------------------------------- #
# Stream
# --------------------------------------------------------------------------- #


class DiagnosticStream:
    """Thread-safe diagnostic sink shared by producers and the Log tab."""

    def __init__(
        self,
        *,
        log_path: Optional[str] = None,
        session_path: Optional[str] = None,
        capacity: Optional[int] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.log_path = log_path
        self.session_path = session_path
        self.clock = clock
        # ``None`` retains the complete process session. Creator uses this mode
        # so its visible panel, clipboard export and saved session can never
        # silently lose the oldest events. Other callers may still request a
        # bounded diagnostic buffer explicitly.
        self.capacity = None if capacity is None else max(1000, int(capacity))
        self._lock = threading.RLock()
        self._events: deque[DiagEvent] = deque(maxlen=self.capacity)
        self._subscribers: list[Callable[[DiagEvent], None]] = []
        self._seq = 0
        self._stages = _StageTracker()
        self._last_failure: Optional[FailureBlock] = None
        self._last_failure_seq: Optional[int] = None
        # live context, set by the controller
        self.run_id = ""
        self.attempt = 0
        self.slot_id = ""
        self.stage = ""
        self.operation = ""
        self._frame = ""
        self.persist_error = ""
        self._python_handler_installed = False
        self._hooks_installed = False

    # ---- context -------------------------------------------------------- #

    def set_context(
        self,
        *,
        run_id: Optional[str] = None,
        attempt: Optional[int] = None,
        slot_id: Optional[str] = None,
        stage: Optional[str] = None,
        operation: Optional[str] = None,
    ) -> None:
        with self._lock:
            if run_id is not None:
                self.run_id = str(run_id)
            if attempt is not None:
                self.attempt = int(attempt)
            if slot_id is not None:
                self.slot_id = str(slot_id)
            if stage is not None:
                self.stage = str(stage)
            if operation is not None:
                self.operation = str(operation)

    def clear_context(self) -> None:
        self.set_context(run_id="", attempt=0, slot_id="", stage="", operation="")

    def snapshot_context(self) -> dict:
        with self._lock:
            return {
                "run_id": self.run_id,
                "attempt": self.attempt,
                "slot_id": self.slot_id,
                "stage": self.stage,
                "operation": self.operation,
            }

    # ---- emit ----------------------------------------------------------- #

    def emit(
        self,
        message: object,
        *,
        severity: str = "INFO",
        module: str = "",
        function: str = "",
        run_id: Optional[str] = None,
        attempt: Optional[int] = None,
        slot_id: Optional[str] = None,
        stage: Optional[str] = None,
        operation: Optional[str] = None,
        duration_ms: Optional[float] = None,
        outcome: str = "",
        retry: str = "",
        status: str = "",
        kind: str = "",
        frame: str = "",
        extra: Optional[dict] = None,
        origin: bool = True,
    ) -> DiagEvent:
        safe = redact(message)
        with self._lock:
            self._seq += 1
            seq = self._seq
            now = self.clock()
            context = {
                "run_id": self.run_id if run_id is None else run_id,
                "attempt": self.attempt if attempt is None else attempt,
                "slot_id": self.slot_id if slot_id is None else slot_id,
                "stage": self.stage if stage is None else stage,
                "operation": self.operation if operation is None else operation,
            }
            if origin and not module:
                module, function = _origin()
        ts = _iso_ms(now)
        event = DiagEvent(
            seq=seq,
            ts=ts,
            epoch=now,
            severity=str(severity).upper(),
            message=safe,
            module=module,
            function=function,
            run_id=str(context["run_id"] or ""),
            attempt=int(context["attempt"] or 0),
            slot_id=str(context["slot_id"] or ""),
            stage=str(context["stage"] or ""),
            operation=str(context["operation"] or ""),
            duration_ms=duration_ms,
            outcome=outcome,
            retry=retry,
            status=status,
            kind=kind,
            frame=frame,
            extra=dict(extra or {}),
        )
        enriched = self._enrich(event)
        self._store(enriched)
        self._fanout(enriched)
        return enriched

    def _enrich(self, event: DiagEvent) -> DiagEvent:
        info = classify(event.message)
        if event.severity == "INFO":
            event.severity = info["severity"]
        if not event.stage:
            event.stage = info["stage"]
        if not event.slot_id:
            event.slot_id = info["slot_id"]
        if not event.status:
            event.status = info["status"]
        if event.kind == "heartbeat":
            event.severity = "STATUS"
        self._track_frame(event)
        duration = self._stages.note(
            event.slot_id, event.stage, event.status, event.epoch
        )
        if duration is not None and event.duration_ms is None:
            event.duration_ms = duration
        return event

    def _track_frame(self, event: DiagEvent) -> None:
        """Mark screen-dump frame boundaries and demote node lines to DEBUG."""
        message = event.message
        with self._lock:
            if message.startswith("== frame begin:"):
                title = message[len("== frame begin:"):].strip().rstrip("=").strip()
                self._frame = title
                event.frame = event.frame or title
                return
            if message.startswith("== frame end:"):
                self._frame = ""
                return
            if self._frame:
                event.frame = event.frame or self._frame
                if event.severity == "INFO" and message.startswith("  - "):
                    event.severity = "DEBUG"

    def log(self, message: object) -> DiagEvent:
        """Compatibility entry point for existing ``log(message)`` callers."""
        return self.emit(message)

    # ---- frames --------------------------------------------------------- #

    def frame(self, title: str, rows: Iterable[str], *, slot_id: str = "") -> None:
        """Emit a screen-dump frame with explicit boundaries.

        ``rows`` are already-captured node previews; nothing new is dumped here.
        """
        self.emit(f"== frame begin: {title} ==", severity="INFO",
                  frame=title, slot_id=slot_id, operation="frame")
        count = 0
        for row in rows:
            self.emit(row, severity="DEBUG", frame=title, slot_id=slot_id,
                      operation="frame-node", origin=False)
            count += 1
        self.emit(f"== frame end: {title} ({count} nodes) ==", severity="INFO",
                  frame=title, slot_id=slot_id, operation="frame")

    # ---- heartbeats ----------------------------------------------------- #

    def heartbeat(
        self,
        *,
        operation: str,
        elapsed_s: float,
        last_progress_s: float,
        timeout_s: float,
        slot_id: str = "",
    ) -> DiagEvent:
        """Status-only heartbeat. Never counts as workflow progress."""
        msg = (
            f"[HEARTBEAT] op={operation or 'unknown'} elapsed={elapsed_s:.0f}s "
            f"idle={last_progress_s:.0f}s timeout={timeout_s:.0f}s"
        )
        return self.emit(
            msg,
            severity="STATUS",
            kind="heartbeat",
            slot_id=slot_id,
            operation=operation,
            outcome="heartbeat",
        )

    # ---- failures ------------------------------------------------------- #

    def failure(
        self,
        *,
        kind: str,
        exception: Optional[BaseException] = None,
        exception_type: str = "",
        exception_message: str = "",
        failure_code: str = "",
        disposition: str = "unknown",
        queue: str = "unknown",
        preservation: str = "unknown",
        last_checkpoint: str = "unknown",
        last_operation: str = "unknown",
        dumps: Optional[list[str]] = None,
        screenshots: Optional[list[str]] = None,
        note: str = "",
        traceback_text: str = "",
        slot_id: Optional[str] = None,
        stage: Optional[str] = None,
        operation: Optional[str] = None,
    ) -> FailureBlock:
        """Record and emit a clearly marked failure report."""
        exc_type = str(exception_type or "")
        exc_msg = str(exception_message or "")
        tb_text = traceback_text
        if exception is not None:
            exc_type = type(exception).__name__
            exc_msg = str(exception)
            if not tb_text:
                tb_text = "".join(
                    _traceback.format_exception(
                        type(exception), exception, exception.__traceback__
                    )
                )
        if dumps is None:
            # Already-captured frame stems name the corresponding dump (and
            # optional screenshot) files without taking any new evidence.
            dumps = self.recent_frames()
        context = self.snapshot_context()
        block = FailureBlock(
            when=_iso_ms(self.clock()),
            run_id=context["run_id"],
            attempt=context["attempt"],
            slot_id=context["slot_id"] if slot_id is None else slot_id,
            stage=context["stage"] if stage is None else stage,
            operation=context["operation"] if operation is None else operation,
            kind=kind,
            exception_type=exc_type,
            exception_message=redact(exc_msg),
            failure_code=str(failure_code or ""),
            disposition=disposition,
            queue=queue,
            preservation=preservation,
            last_checkpoint=last_checkpoint,
            last_operation=last_operation,
            dumps=list(dumps or []),
            screenshots=list(screenshots or []),
            traceback_text=redact_traceback(tb_text),
            note=note,
        )
        with self._lock:
            self._last_failure = block
        event = self.emit(
            block.text(),
            severity="ERROR",
            kind=kind,
            slot_id=block.slot_id,
            stage=block.stage,
            operation=block.operation,
            outcome="failed",
        )
        with self._lock:
            self._last_failure_seq = event.seq
        return block

    def set_disposition(self, value: str) -> None:
        """Record what happened to the attempt/batch (pause, skip, stop, warn)."""
        self._record_failure_field("disposition", value, emit_label="DISPOSITION")

    def set_queue(self, value: str) -> None:
        """Record the queue outcome (preserved, advanced, consumed once)."""
        self._record_failure_field("queue", value, emit_label="QUEUE")

    def set_preservation(self, value: str) -> None:
        """Record what was preserved (account, container, credentials, email)."""
        self._record_failure_field("preservation", value, emit_label="PRESERVATION")

    def _record_failure_field(self, field_name: str, value: str, emit_label: str) -> None:
        text = str(value)
        with self._lock:
            if self._last_failure is not None:
                setattr(self._last_failure, field_name, text)
        self.emit(
            f"{emit_label}: {text}",
            severity="INFO",
            kind="disposition",
            operation=field_name,
            outcome="disposition",
        )

    def cleanup_error(self, message: object, *, exception: Optional[BaseException] = None) -> None:
        """Report a secondary cleanup error without replacing the original."""
        detail = redact(message)
        if exception is not None:
            detail += f" ({type(exception).__name__}: {redact(exception)})"
        with self._lock:
            if self._last_failure is not None:
                self._last_failure.cleanup_errors.append(detail)
        self.emit(
            "CLEANUP ERROR (original failure preserved): " + detail,
            severity="WARNING",
            kind="cleanup",
            operation="cleanup",
        )

    def crash(self, kind: str, text: str) -> None:
        """Route an uncaught/thread exception captured by crashlog."""
        exc_type, exc_msg = _split_exception(text)
        self.failure(
            kind=kind,
            exception_type=exc_type,
            exception_message=exc_msg,
            traceback_text=text,
            disposition="uncaught",
        )

    # ---- storage -------------------------------------------------------- #

    def _store(self, event: DiagEvent) -> None:
        with self._lock:
            self._events.append(event)
        self._persist(event)

    def _persist(self, event: DiagEvent) -> None:
        if self.session_path:
            self._append(self.session_path, event.to_json() + "\n", "session")
        if self.log_path:
            # Keep the historical creator.log line format for compatibility.
            self._append(
                self.log_path,
                f"{event.ts[11:19]}  {event.message}\n",
                "compat",
            )

    def _append(self, path: str, text: str, label: str) -> None:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text)
            with self._lock:
                if self.persist_error:
                    self.persist_error = ""
        except OSError as exc:
            with self._lock:
                first = not self.persist_error
                self.persist_error = f"{label}: {type(exc).__name__}: {exc}"
            if first:
                # Visible in the panel, but never recursive and never a decision.
                try:
                    self._store(
                        DiagEvent(
                            seq=self._next_seq(),
                            ts=_iso_ms(self.clock()),
                            epoch=self.clock(),
                            severity="ERROR",
                            message=(
                                "LOGGING ERROR: persistent log write failed "
                                f"({label}); see the Log tab. Automation is unaffected."
                            ),
                            module="diaglog",
                            function="_append",
                            kind="persist",
                            operation="persist",
                        )
                    )
                except Exception:
                    pass

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _fanout(self, event: DiagEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                # A broken subscriber must never break logging or automation.
                pass

    # ---- subscribers ---------------------------------------------------- #

    def subscribe(self, callback: Callable[[DiagEvent], None]) -> None:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[DiagEvent], None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

    # ---- readers -------------------------------------------------------- #

    def events(self) -> list[DiagEvent]:
        with self._lock:
            return list(self._events)

    def count(self) -> int:
        with self._lock:
            return len(self._events)

    def session_text(self) -> str:
        with self._lock:
            return "\n".join(e.render() for e in self._events)

    def session_json(self) -> str:
        with self._lock:
            return "\n".join(e.to_json() for e in self._events)

    def has_failure(self) -> bool:
        with self._lock:
            return self._last_failure is not None

    def last_failure(self) -> Optional[FailureBlock]:
        with self._lock:
            return self._last_failure

    def last_checkpoint_message(self, slot_id: str = "") -> str:
        """Most recent verified READY/COMPLETED message (else 'unknown')."""
        with self._lock:
            events = list(self._events)
        for event in reversed(events):
            if event.status in {"READY", "COMPLETED"} and (
                not slot_id or event.slot_id == slot_id
            ):
                return event.message
        return "unknown"

    def last_message(self, slot_id: str = "") -> str:
        """Most recent message for a slot (the last attempted operation)."""
        with self._lock:
            events = list(self._events)
        for event in reversed(events):
            if not slot_id or event.slot_id == slot_id:
                return event.message
        return "unknown"

    def recent_frames(self, limit: int = 8) -> list[str]:
        """Return stems of the most recent already-captured screen frames."""
        frames: list[str] = []
        with self._lock:
            events = list(self._events)
        for event in reversed(events):
            frame = str(getattr(event, "frame", "") or "")
            if not frame or frame in frames:
                continue
            frames.append(frame)
            if len(frames) >= max(0, int(limit)):
                break
        return frames

    def last_failure_text(self, preceding: int = 200) -> str:
        """Failure block plus up to *preceding* earlier events from the attempt."""
        with self._lock:
            block = self._last_failure
            seq = self._last_failure_seq
            events = list(self._events)
        if block is None:
            return "No failure has been recorded in this session."
        lines = []
        if seq is not None:
            window = [e for e in events if e.seq < seq]
            context = window[-max(0, int(preceding)):] if preceding else []
            if context:
                lines.append(
                    f"--- last {len(context)} event(s) before the failure ---"
                )
                lines.extend(e.render() for e in context)
                lines.append("--- failure report ---")
        lines.append(block.text())
        return "\n".join(lines)

    # ---- hook installation --------------------------------------------- #

    def install_python_logging(self, level: int = 20) -> bool:
        """Forward the standard logging module into this stream (once)."""
        import logging

        with self._lock:
            if self._python_handler_installed:
                return True
            self._python_handler_installed = True
        stream = self

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
                try:
                    message = record.getMessage()
                    if record.exc_info:
                        message += "\n" + "".join(
                            _traceback.format_exception(*record.exc_info)
                        )
                    stream.emit(
                        message,
                        severity=record.levelname,
                        module=str(record.module or ""),
                        function=str(record.funcName or ""),
                        origin=False,
                    )
                except Exception:
                    pass

        handler = _Handler()
        handler.setLevel(level)
        logging.getLogger().addHandler(handler)
        return True

    def install_process_hooks(self) -> bool:
        """Install sys/threading exception hooks that feed this stream (once)."""
        with self._lock:
            if self._hooks_installed:
                return True
            self._hooks_installed = True
        stream = self

        def _excepthook(etype, value, tb):
            try:
                stream.crash(
                    "uncaught",
                    "".join(_traceback.format_exception(etype, value, tb)),
                )
            except Exception:
                pass
            prev = getattr(sys, "__excepthook__", None)
            if prev is not None:
                try:
                    prev(etype, value, tb)
                except Exception:
                    pass

        def _thread_hook(args):
            try:
                stream.crash(
                    "thread",
                    "".join(
                        _traceback.format_exception(
                            args.exc_type, args.exc_value, args.exc_traceback
                        )
                    ),
                )
            except Exception:
                pass

        sys.excepthook = _excepthook
        threading.excepthook = _thread_hook
        return True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _iso_ms(epoch: float) -> str:
    dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}Z"


def _split_exception(text: str) -> tuple[str, str]:
    """Best-effort (type, message) extraction from a formatted traceback."""
    lines = [ln for ln in str(text).splitlines() if ln.strip()]
    for line in reversed(lines):
        stripped = line.strip()
        if ":" in stripped and not stripped.startswith(("File ", "Traceback")):
            head, _, tail = stripped.partition(":")
            if head and head[0].isupper() and " " not in head.strip():
                return head.strip(), tail.strip()
    return "", ""


# --------------------------------------------------------------------------- #
# Default stream
# --------------------------------------------------------------------------- #

DEFAULT_SESSION_PATH = os.path.join(str(LOG_DIR), "creator_diagnostics.log")


def new_default_stream(*, log_path: Optional[str] = None) -> DiagnosticStream:
    return DiagnosticStream(
        log_path=log_path,
        session_path=DEFAULT_SESSION_PATH,
        capacity=None,
    )


__all__ = [
    "DiagEvent",
    "DiagnosticStream",
    "FailureBlock",
    "classify",
    "new_default_stream",
    "redact",
    "redact_traceback",
    "severity_order",
]
