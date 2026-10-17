"""Crash-safe local queue for sequential Creator batches (no network calls)."""

from __future__ import annotations

import copy
import json
import ipaddress
import math
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from config import LOG_DIR
from ip_evidence import validate_attempt

STATE_PATH = Path(LOG_DIR) / "bulk_creation.json"
DRAFT_PATH = Path(LOG_DIR) / "bulk_draft.json"
STAGES = {
    "idle", "ip_pending", "may_have_triggered", "ip_verified", "creating",
    "between", "retry_ip", "retry_creation", "paused", "stopped",
}
RECOVERY_DELAYS_SECONDS = (5, 15, 30, 60, 120)
_LOCK = threading.RLock()
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,28}$")


class BulkError(RuntimeError):
    """A safe configuration or persistence error without pasted data."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def empty_state() -> dict:
    return {
        "version": 5, "revision": 0, "run_id": None, "writer_id": None,
        "recovery": None,
        "queue": [], "requested": 0, "remaining": 0,
        "stage": "idle", "slot_id": None, "baseline_ip": None,
        "verified_ip": None, "verified_at_utc": None,
        "trigger_deadline_utc": None, "ready_at_utc": None,
        "failure_phase": None, "failure_reason": None, "failure_code": None,
        "failures": [],
        "ip_attempt": None,
    }


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
        username = usernames[index - 1] if index <= len(usernames) else ""
        if USERNAME_RE.fullmatch(username) is not None:
            folded = username.casefold()
            if folded not in seen_usernames:
                seen_usernames.add(folded)
                item["username"] = username
        queue.append(item)
    return requested, queue


def _validate(state: object) -> dict:
    blank = empty_state()
    if not isinstance(state, dict) or set(state) != set(blank) or state.get("version") != 5:
        raise BulkError("Saved bulk configuration is malformed.")
    if type(state["revision"]) is not int or state["revision"] < 0:
        raise BulkError("Saved bulk configuration is malformed.")
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
            v4_fields = set(empty_state()) - {
                "revision", "run_id", "writer_id", "recovery",
            }
            if isinstance(state, dict) and state.get("version") == 4 and set(state) == v4_fields:
                migrated = {
                    **state, "version": 5, "revision": 0, "run_id": None,
                    "writer_id": None, "recovery": None,
                }
                _validate(migrated)
                save_state(migrated, path)
                return migrated
            v3_fields = v4_fields - {"failure_code", "failures"}
            v2_fields = v3_fields - {"failure_reason"}
            legacy_fields = v2_fields - {"ip_attempt"}
            if isinstance(state, dict) and state.get("version") == 1 and set(state) == legacy_fields:
                migrated = {
                    **state, "version": 5, "ip_attempt": None,
                    "failure_reason": None, "failure_code": None, "failures": [],
                    "revision": 0, "run_id": None, "writer_id": None,
                    "recovery": None,
                }
                _validate(migrated)
                save_state(migrated, path)
                return migrated
            if isinstance(state, dict) and state.get("version") == 2 and set(state) == v2_fields:
                migrated = {
                    **state, "version": 5, "failure_reason": None,
                    "failure_code": None, "failures": [],
                    "revision": 0, "run_id": None, "writer_id": None,
                    "recovery": None,
                }
                _validate(migrated)
                save_state(migrated, path)
                return migrated
            if isinstance(state, dict) and state.get("version") == 3 and set(state) == v3_fields:
                migrated = {
                    **state, "version": 5,
                    "failure_code": _failure_code_from_reason(state.get("failure_reason")),
                    "failures": [],
                    "revision": 0, "run_id": None, "writer_id": None,
                    "recovery": None,
                }
                _validate(migrated)
                save_state(migrated, path)
                return migrated
            return _validate(state)
    except (OSError, ValueError, TypeError):
        raise BulkError("Saved bulk configuration could not be read safely.") from None


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    _validate(state)
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
    except OSError:
        raise BulkError("Bulk configuration could not be saved.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


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


def archive_state(
    state: dict,
    path: Path = STATE_PATH,
    *,
    stamp: datetime | None = None,
) -> Path:
    """Durably preserve a stopped state before replacing its unused tail."""
    _validate(state)
    moment = (stamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
    suffix = moment.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.stem}.pre-reconcile-{suffix}{path.suffix}")
    serial = 1
    while candidate.exists():
        candidate = path.with_name(
            f"{path.stem}.pre-reconcile-{suffix}-{serial}{path.suffix}"
        )
        serial += 1
    save_state(copy.deepcopy(state), candidate)
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
    next_state["baseline_ip"] = None
    next_state["verified_ip"] = None
    next_state["verified_at_utc"] = None
    next_state["trigger_deadline_utc"] = None
    next_state["ready_at_utc"] = ready_at_utc if next_state["remaining"] else None
    next_state["failure_phase"] = None
    next_state["failure_reason"] = None
    next_state["failure_code"] = None
    next_state["ip_attempt"] = None
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
    next_state["baseline_ip"] = None
    next_state["verified_ip"] = None
    next_state["verified_at_utc"] = None
    next_state["trigger_deadline_utc"] = None
    next_state["ready_at_utc"] = ready_at_utc if next_state["remaining"] else None
    next_state["failure_phase"] = None
    next_state["failure_reason"] = None
    next_state["failure_code"] = None
    next_state["ip_attempt"] = None
    _validate(next_state)
    return next_state
