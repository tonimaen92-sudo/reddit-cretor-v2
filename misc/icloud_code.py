# UTF-8
"""Fetch Reddit 6-digit code from iCloud IMAP (Hide My Email forwards here). Never print passwords."""

from __future__ import annotations

import email
import imaplib
import os
import re
import sys
import time
from email.utils import parsedate_to_datetime
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ssh_device import load_secrets  # noqa: E402

CODE_RE = re.compile(r"\b(\d{6})\b")
INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"', re.IGNORECASE)
LATEST_ALL_LIMIT = 40
LATEST_CANDIDATE_LIMIT = 40
IMAP_IO_TIMEOUT_SECONDS = 10.0
IMAP_PASS_TIMEOUT_SECONDS = 20.0
POLL_INTERVAL_SECONDS = 6.0
# Apple ID used on this phone (Hide My Email password sheet). Override via secrets icloud_user.
DEFAULT_USER = ""
LogFn = Callable[[str], None]


class PollCancelled(RuntimeError):
    """The caller requested cancellation while IMAP polling was active."""


class VerificationCodeTimeout(RuntimeError):
    """Reddit's verification message did not arrive before the IMAP deadline."""

    def __init__(self, last_error_type: str = ""):
        self.last_error_type = str(last_error_type or "")
        extra = f" Last error: {self.last_error_type}." if self.last_error_type else ""
        super().__init__("No 6-digit Reddit code in iCloud IMAP." + extra)


def _is_cancelled(stop) -> bool:
    if stop is None:
        return False
    is_set = getattr(stop, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if callable(stop):
        return bool(stop())
    return bool(stop)


def _check_cancelled(stop) -> None:
    if _is_cancelled(stop):
        raise PollCancelled("Reddit verification-code polling was stopped.")


def _operation_timeout(deadline: float | None) -> float:
    if deadline is None:
        return IMAP_IO_TIMEOUT_SECONDS
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("IMAP polling deadline expired")
    return max(0.001, min(IMAP_IO_TIMEOUT_SECONDS, remaining))


def _set_socket_timeout(mail, deadline: float | None) -> None:
    sock = getattr(mail, "sock", None)
    setter = getattr(sock, "settimeout", None)
    if callable(setter):
        setter(_operation_timeout(deadline))


def _abort_connection(mail) -> None:
    if mail is None:
        return
    shutdown = getattr(mail, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown()
            return
        except Exception:
            pass
    sock = getattr(mail, "sock", None)
    close = getattr(sock, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _close_connection(mail, *, deadline: float | None, graceful: bool) -> None:
    if mail is None:
        return
    if graceful:
        try:
            _set_socket_timeout(mail, deadline)
            mail.logout()
            return
        except Exception:
            pass
    _abort_connection(mail)


def _connect(
    *,
    deadline: float | None = None,
    stop=None,
) -> imaplib.IMAP4_SSL:
    sec = load_secrets()
    user = str(sec.get("icloud_user") or DEFAULT_USER).strip()
    pwd = str(sec.get("icloud_app_password") or sec.get("icloud_password") or "").strip()
    if not pwd:
        raise RuntimeError("Missing icloud_app_password in secrets.json")
    _check_cancelled(stop)
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(
            "imap.mail.me.com",
            993,
            timeout=_operation_timeout(deadline),
        )
        _check_cancelled(stop)
        _set_socket_timeout(mail, deadline)
        mail.login(user, pwd)
        _check_cancelled(stop)
        _set_socket_timeout(mail, deadline)
        mail.select("INBOX", readonly=True)
        return mail
    except Exception:
        _abort_connection(mail)
        raise


def _numeric_message_id(value: bytes) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _candidate_message_ids(
    mail: imaplib.IMAP4_SSL,
    *,
    deadline: float | None = None,
    stop=None,
) -> list[bytes]:
    """Merge every targeted result with recent mail, newest sequence first."""
    found: set[bytes] = set()
    for query in (
        '(FROM "reddit")',
        '(SUBJECT "verification code")',
        '(SUBJECT "reddit")',
        "(UNSEEN)",
    ):
        _check_cancelled(stop)
        _set_socket_timeout(mail, deadline)
        typ, data = mail.search(None, query)
        if typ == "OK" and data and data[0]:
            found.update(data[0].split())

    _check_cancelled(stop)
    _set_socket_timeout(mail, deadline)
    typ, data = mail.search(None, "ALL")
    if typ == "OK" and data and data[0]:
        all_ids = sorted(data[0].split(), key=_numeric_message_id)
        found.update(all_ids[-LATEST_ALL_LIMIT:])
    # Targeted searches can contain years of Reddit mail and thousands of
    # unread messages.  Fetch only the newest bounded union so one scan pass
    # cannot consume the whole polling deadline before a newly arrived code
    # gets another chance to appear.
    return sorted(found, key=_numeric_message_id, reverse=True)[
        :LATEST_CANDIDATE_LIMIT
    ]


def _fetched_message(payload) -> tuple[bytes, bytes] | None:
    for item in payload or ():
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        metadata, raw = item[0], item[1]
        if isinstance(metadata, bytes) and isinstance(raw, (bytes, bytearray)):
            return metadata, bytes(raw)
    return None


def _received_timestamp(metadata: bytes, msg) -> float | None:
    """Prefer server receipt time; use the sender-controlled Date only as fallback."""
    match = INTERNALDATE_RE.search(metadata)
    if match is not None:
        try:
            return parsedate_to_datetime(
                match.group(1).decode("ascii", errors="strict")
            ).timestamp()
        except (TypeError, ValueError, OverflowError, UnicodeError):
            pass
    try:
        return parsedate_to_datetime(str(msg.get("Date") or "")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_reddit_code(
    after_ts: float | None = None,
    email_hint: str | None = None,
    *,
    deadline: float | None = None,
    stop=None,
) -> str | None:
    mail = None
    graceful = False
    try:
        mail = _connect(deadline=deadline, stop=stop)
        ids = _candidate_message_ids(mail, deadline=deadline, stop=stop)
        seen: set[str] = set()
        hint = (email_hint or "").strip().lower()
        floor = after_ts or 0
        for uid in ids:
            _check_cancelled(stop)
            _set_socket_timeout(mail, deadline)
            typ, payload = mail.fetch(uid, "(INTERNALDATE BODY.PEEK[])")
            if typ != "OK":
                continue
            fetched = _fetched_message(payload)
            if fetched is None:
                continue
            metadata, raw = fetched
            msg = email.message_from_bytes(raw)
            if after_ts:
                received_ts = _received_timestamp(metadata, msg)
                if received_ts is None or received_ts < floor:
                    continue
            subj = str(msg.get("Subject") or "")
            frm = str(msg.get("From") or "")
            blob = subj + "\n" + frm + "\n" + str(msg.get("To") or "") + "\n"
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype not in ("text/plain", "text/html"):
                    continue
                try:
                    blob += part.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    continue
            # Forwarding metadata (for example X-Original-To) may be outside
            # the small header subset above, so retain it for exact hint
            # matching without logging any of it.
            low = (blob + "\n" + raw.decode("utf-8", errors="replace")).casefold()
            if "reddit" not in low and "verification code" not in low:
                continue
            if hint and hint not in low:
                continue
            for m in CODE_RE.findall(subj + " " + blob):
                if m not in seen and m not in ("000000", "111111"):
                    seen.add(m)
                    graceful = True
                    return m
        graceful = True
        return None
    finally:
        _close_connection(mail, deadline=deadline, graceful=graceful)


def _wait_for_retry(seconds: float, stop) -> None:
    if seconds <= 0:
        return
    waiter = getattr(stop, "wait", None)
    if callable(waiter):
        if waiter(seconds):
            raise PollCancelled("Reddit verification-code polling was stopped.")
        return
    time.sleep(seconds)
    _check_cancelled(stop)


def poll_reddit_code(
    log: LogFn | None = None,
    timeout: int = 120,
    after_ts: float | None = None,
    email_hint: str | None = None,
    stop=None,
) -> str:
    timeout = max(0, int(timeout))
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    if log:
        log(
            f"IMAP polling started ({timeout}s total, "
            f"{int(IMAP_IO_TIMEOUT_SECONDS)}s per network operation)."
        )
    while time.monotonic() < deadline:
        try:
            _check_cancelled(stop)
            pass_deadline = min(
                deadline,
                time.monotonic() + IMAP_PASS_TIMEOUT_SECONDS,
            )
            code = fetch_reddit_code(
                after_ts=after_ts,
                email_hint=email_hint,
                deadline=pass_deadline,
                stop=stop,
            )
            if code:
                if log:
                    log("IMAP verification message received.")
                return code
        except PollCancelled:
            if log:
                log("IMAP polling stopped; Reddit remains on Verify your email.")
            raise
        except Exception as e:
            last = e
            if log:
                left = max(0, int(deadline - time.monotonic()))
                log(f"IMAP retry ({type(e).__name__}); {left}s left")
        else:
            if log:
                left = max(0, int(deadline - time.monotonic()))
                log(f"IMAP no code yet, {left}s left")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            _wait_for_retry(min(POLL_INTERVAL_SECONDS, remaining), stop)
        except PollCancelled:
            if log:
                log("IMAP polling stopped; Reddit remains on Verify your email.")
            raise
    if log:
        log("IMAP polling timed out; Reddit remains on Verify your email.")
    raise VerificationCodeTimeout(type(last).__name__ if last else "")


if __name__ == "__main__":
    poll_reddit_code(timeout=20)
    print("IMAP verification code received.", flush=True)
