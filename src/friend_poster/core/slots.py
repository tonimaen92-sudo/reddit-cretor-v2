# UTF-8
"""Gitignored logs/slots.json: Ghost/Crane name + Hide My Email labeled the same.

Every name is padded: a0001, a0002, … a9999, then b10000. Legacy rN input is
still accepted (r1 -> a0001) but never produced.
"""

from __future__ import annotations

import json
import ipaddress
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from friend_poster.core.config import LOG_DIR

LogFn = Callable[[str], None]
SLOTS_PATH = os.path.join(LOG_DIR, "slots.json")
RETIRED_PATH = os.path.join(LOG_DIR, "retired.json")
ACCOUNTS_PATH = os.path.join(os.path.dirname(LOG_DIR), "accounts.txt")
R_NAME = re.compile(r"^r(\d+)$", re.I)
PADDED_NAME = re.compile(r"^([a-z])(\d+)$", re.I)
LOOSE_NAME = re.compile(r"^([a-z])?0*(\d+)$", re.I)
PAD_BASE = 4
LEGACY_R_MAX = 13
REDDIT_CLEANUP_STATES = ("pending", "app_stopped", "complete")
GHOST_CONTAINER_STATES = ("needed", "provisioning", "ready")
# Durable local-container deletion checkpoint.  ``pending`` is written before
# any delete gesture (so a crash can resume it) and ``complete`` only after a
# complete validated inventory proves the exact recorded container is gone.
CONTAINER_DELETION_STATES = ("pending", "complete")
# Separate from ``container_deletion`` (the post-account cleanup checkpoint):
# this record belongs only to a deliberately abandoned, incomplete bulk
# attempt.  Keeping the two records distinct prevents a failed signup reset
# from making an account look successfully completed and cleaned up.
BULK_RESET_CLEANUP_STATES = ("pending", "complete")
CREATION_FEED_SWIPE_MIN = 1
HME_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@icloud\.com$", re.I)
EMAIL_VERIFICATION_PHASES = (
    "submitted", "timed_out", "replacement_pending", "replacement_ready",
)

# Realistic labels for Hide My Email — typed on iOS "Label your address" field.
# User asked for normal names instead of raw rN. Slot id stays rN (Ghost container).
_REAL_FIRST = [
    "Emma", "Liam", "Olivia", "Noah", "Ava", "Ethan", "Sophia", "Mason",
    "Isabella", "Lucas", "Mia", "James", "Amelia", "Oliver", "Harper",
    "Elijah", "Evelyn", "Benjamin", "Abigail", "Henry", "Emily", "Alexander",
    "Charlotte", "Daniel", "Grace", "Michael", "Chloe", "William", "Lily",
    "David", "Aria", "Joseph", "Layla", "Matthew", "Sofia", "Samuel", "Avery",
    "John", "Victoria", "Wyatt", "Hannah", "Levi", "Zoe", "Jack", "Nora",
    "Luke", "Riley", "Owen", "Scarlett", "Gabriel", "Maya", "Carter", "Luna",
]
_REAL_LAST = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Thompson", "White", "Harris", "Clark", "Lewis", "Walker",
    "Hall", "Allen", "Young", "King", "Wright", "Scott", "Green", "Baker",
    "Adams", "Nelson", "Hill", "Campbell", "Mitchell", "Roberts", "Carter",
    "Phillips", "Evans", "Turner", "Parker", "Collins", "Edwards", "Stewart",
]


def realistic_label(seed: str | None = None) -> str:
    """Return a realistic 'First Last' label for Hide My Email."""
    import random

    rnd = random.Random(seed) if seed else random
    first = rnd.choice(_REAL_FIRST)
    last = rnd.choice(_REAL_LAST)
    # Occasionally add middle initial to look more natural / unique
    if rnd.random() < 0.15:
        mid = rnd.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        return f"{first} {mid}. {last}"
    return f"{first} {last}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CreationProgressClock:
    """Monotonic inactivity clock for one bulk creation attempt.

    :meth:`note` is called by a verified workflow transition, by a completed
    stage advance, and by every completed device round-trip of the attempt's
    own phone (``Phone.progress_note``).  A dump, tap, flick, screenshot or app
    probe that returns proves the attempt is alive, so a legitimately slow phase
    may run longer than any single total-duration limit.  A frozen attempt - a
    device call that never returns, or no device contact at all - stops noting
    and is therefore still detectable.  Re-observing the same screen through
    logs, and phone clock changes, never touch it.
    """

    def __init__(self, time_fn: Callable[[], float] | None = None) -> None:
        self._time_fn = time_fn or time.monotonic
        self._lock = threading.Lock()
        self._stamp = self._time_fn()

    def reset(self) -> None:
        with self._lock:
            self._stamp = self._time_fn()

    def note(self) -> None:
        self.reset()

    def idle_seconds(self) -> float:
        with self._lock:
            return max(0.0, self._time_fn() - self._stamp)


# Process-wide clock shared by the slot checkpoint writers and the bulk
# inactivity watchdog.  Tests may replace it with a fake-monotonic instance.
CREATION_PROGRESS = CreationProgressClock()


def _empty_slot(sid: str) -> dict[str, Any]:
    return {
        "id": sid.lower(),
        "ghost": False,
        "email": None,
        "email_label": sid.lower(),
        "username": None,
        "reddit": False,
        "day1": False,
        "created_at": _now(),
    }


def load_slots() -> list[dict[str, Any]]:
    os.makedirs(LOG_DIR, exist_ok=True)
    rows: list[dict[str, Any]] = []
    if os.path.isfile(SLOTS_PATH):
        try:
            with open(SLOTS_PATH, "r", encoding="utf-8") as f:
                rows = json.load(f) or []
        except (OSError, json.JSONDecodeError):
            rows = []
    if not rows:
        # Fresh file only. Never resurrect a deleted slot (e.g. a0001).
        seed = _empty_slot("a0001")
        seed["ghost_container_state"] = "needed"
        rows.insert(0, seed)
        save_slots(rows)
    return rows


def save_slots(rows: list[dict[str, Any]]) -> None:
    """Atomically persist the slot database in its existing JSON format."""
    os.makedirs(LOG_DIR, exist_ok=True)
    target_dir = os.path.dirname(os.path.abspath(SLOTS_PATH))
    os.makedirs(target_dir, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target_dir,
            prefix=".slots-",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_path = f.name
            json.dump(rows, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                os.replace(temp_path, SLOTS_PATH)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(min(0.25 * (attempt + 1), 1.0))
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def set_slot_falcon_proxy_ip(sid: str, address: str, observed_at_utc: str) -> None:
    """Attach verified proxy egress to one slot; never rewrite a completed account."""
    canonical = str(ipaddress.ip_address(address))
    stamp = datetime.fromisoformat(observed_at_utc.replace("Z", "+00:00"))
    if not observed_at_utc.endswith("Z") or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
        raise ValueError("FalconProxy verification time must be UTC.")
    rows = load_slots()
    for row in rows:
        if row.get("id") != sid:
            continue
        prior = row.get("falcon_proxy_verified_ip")
        if prior and prior != canonical and slot_reddit_ready(row):
            raise ValueError("Completed account proxy IP cannot be overwritten.")
        row["falcon_proxy_verified_ip"] = canonical
        row["falcon_proxy_verified_at_utc"] = observed_at_utc
        save_slots(rows)
        return
    raise ValueError("FalconProxy IP target slot does not exist.")


def padded_name(n: int) -> str:
    """1→a0001, 14→a0014, 9999→a9999, 10000→b10000."""
    if n < 1:
        raise ValueError(f"Bad slot index {n}")
    digits = max(PAD_BASE, len(str(n)))
    prefix_i = digits - PAD_BASE
    if prefix_i >= 26:
        raise ValueError(f"Slot index {n} needs more than z-width digits")
    return chr(ord("a") + prefix_i) + str(n).zfill(digits)


def index_to_name(n: int) -> str:
    """a0001, a0002, … a9999, then b10000. Always padded."""
    if n < 1:
        raise ValueError(f"Bad slot index {n}")
    return padded_name(n)


def loose_index(text: str) -> int | None:
    """Best-effort slot number from OCR text: a0001 / a1 / 0001 / r1 -> 1.

    Used to match a container whose label OCR may have truncated. Not an exact
    identity check — callers must also guard against stray digits.
    """
    t = re.sub(r"\s+", "", (text or "")).lower()
    if not t:
        return None
    m = LOOSE_NAME.fullmatch(t)
    if not m:
        return None
    n = int(m.group(2))
    return n if n >= 1 else None


def name_to_index(sid: str) -> int | None:
    sid = (sid or "").strip().lower()
    if not sid:
        return None
    m = R_NAME.fullmatch(sid)
    if m:
        return int(m.group(1))
    if sid.isdigit():
        n = int(sid)
        return n if n >= 1 else None
    m = PADDED_NAME.fullmatch(sid)
    if not m:
        return None
    n = int(m.group(2))
    if n < 1:
        return None
    if padded_name(n) != sid:
        return None
    return n


def canonicalize_name(sid: str) -> str:
    n = name_to_index(sid)
    if n is None:
        return (sid or "").strip().lower()
    return index_to_name(n)


def is_slot_name(sid: str) -> bool:
    return name_to_index(sid) is not None


def name_span(start: str, end: str) -> list[str]:
    a = name_to_index(start)
    b = name_to_index(end)
    if a is None or b is None or b < a or b - a > 50:
        raise ValueError(f"Bad name range {start!r} .. {end!r}")
    return [index_to_name(i) for i in range(a, b + 1)]


def _row_is_real(row: dict[str, Any], n: int) -> bool:
    if str(row.get("ghost_container_state") or "").strip().lower() == "provisioning":
        return True
    if (
        str(row.get("ghost_container_state") or "").strip().lower() == "needed"
        and not row.get("ghost")
        and not row.get("email")
        and not row.get("reddit")
        and not row.get("day1")
        and not slot_bulk_skipped(row)
        and not slot_creation_terminal_failure(row)
        and "container_deletion" not in row
        and "bulk_reset_cleanup" not in row
    ):
        return False
    if n <= LEGACY_R_MAX:
        return True
    return bool(
        row.get("ghost")
        or row.get("email")
        or row.get("reddit")
        or row.get("day1")
        or slot_bulk_skipped(row)
        or slot_creation_terminal_failure(row)
        # A completed automatic container deletion keeps the slot row (and its
        # number) reserved even after the local container is gone, so a0001 is
        # never handed out again once it has held a container.
        or "container_deletion" in row
        or "bulk_reset_cleanup" in row
    )


def _absorb_index(used: set[int], token: object) -> None:
    if isinstance(token, int):
        if token >= 1:
            used.add(token)
        return
    n = name_to_index(str(token or ""))
    if n is not None:
        used.add(n)


def retired_ids() -> set[str]:
    """Slot ids deleted on purpose (Crane profile gone). Never reuse them."""
    try:
        with open(RETIRED_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x or "").strip().lower() for x in data if str(x or "").strip()}


def retired_indices() -> set[int]:
    out: set[int] = set()
    for sid in retired_ids():
        n = name_to_index(sid)
        if n is not None:
            out.add(n)
    return out


def retire_slot_id(sid: str, *, backup: bool = True) -> bool:
    """Atomically reserve a PC-deleted slot id so it can never be allocated again.

    The phone-side container and ``used/<slot>/`` data are deliberately outside
    this operation.  A timestamped copy of an existing retired file is kept
    before the atomic replacement, matching the project's recoverable JSON
    update convention.  Return ``True`` only when a new entry was added.
    """
    canonical = canonicalize_name(sid)
    if not is_slot_name(canonical):
        raise ValueError(f"Bad slot id {sid!r}")
    os.makedirs(LOG_DIR, exist_ok=True)
    current: list[str] = []
    if os.path.isfile(RETIRED_PATH):
        try:
            with open(RETIRED_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            raise ValueError("retired.json is unreadable; refusing to replace it") from exc
        if not isinstance(data, list):
            raise ValueError("retired.json must contain a JSON list")
        for value in data:
            normalized = canonicalize_name(str(value or ""))
            if normalized and normalized not in current:
                current.append(normalized)
    if canonical in current:
        return False
    if backup and os.path.isfile(RETIRED_PATH):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        shutil.copyfile(RETIRED_PATH, RETIRED_PATH + ".bak-" + stamp)
    current.append(canonical)
    target_dir = os.path.dirname(os.path.abspath(RETIRED_PATH))
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target_dir,
            prefix=".retired-", suffix=".tmp", delete=False,
        ) as f:
            temp_path = f.name
            json.dump(current, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, RETIRED_PATH)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return True


def unretire_slot_id(sid: str, *, backup: bool = True) -> bool:
    """Atomically release one retired id after its phone container is rebuilt.

    This is deliberately not part of allocation.  Callers may use it only
    after the exact named Ghost container has been created and verified, so a
    failed or skipped rebuild continues to reserve the id.
    """
    canonical = canonicalize_name(sid)
    if not is_slot_name(canonical):
        raise ValueError(f"Bad slot id {sid!r}")
    if not os.path.isfile(RETIRED_PATH):
        return False
    try:
        with open(RETIRED_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise ValueError("retired.json is unreadable; refusing to replace it") from exc
    if not isinstance(data, list):
        raise ValueError("retired.json must contain a JSON list")

    current: list[str] = []
    for value in data:
        normalized = canonicalize_name(str(value or ""))
        if normalized and normalized not in current:
            current.append(normalized)
    if canonical not in current:
        return False

    if backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        shutil.copyfile(RETIRED_PATH, RETIRED_PATH + ".bak-" + stamp)
    remaining = [value for value in current if value != canonical]
    target_dir = os.path.dirname(os.path.abspath(RETIRED_PATH))
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target_dir,
            prefix=".retired-", suffix=".tmp", delete=False,
        ) as f:
            temp_path = f.name
            json.dump(remaining, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, RETIRED_PATH)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return True


def indices_used() -> set[int]:
    """Taken slot numbers. Empty placeholders above the legacy range do not reserve IDs."""
    used: set[int] = set()
    for row in load_slots():
        n = name_to_index(str(row.get("id") or ""))
        if n is None:
            continue
        if _row_is_real(row, n):
            used.add(n)
    used |= retired_indices()
    return used


def names_used() -> set[str]:
    return {index_to_name(n) for n in indices_used()} | {
        f"r{n}" for n in indices_used() if n <= LEGACY_R_MAX
    }


def slot_ids_used() -> set[str]:
    return names_used()


def next_slot_id(extra: set | None = None) -> str:
    used = indices_used()
    if extra:
        for token in extra:
            _absorb_index(used, token)
    i = 1
    while i in used:
        i += 1
    return index_to_name(i)


def find_slot_row(rows: list[dict[str, Any]], sid: str) -> dict[str, Any] | None:
    sid = (sid or "").strip().lower()
    for row in rows:
        if str(row.get("id") or "").lower() == sid:
            return row
    want = name_to_index(sid)
    if want is None:
        return None
    for row in rows:
        if name_to_index(str(row.get("id") or "")) == want:
            return row
    return None


def upsert_ghost(sid: str, log: LogFn | None = None) -> dict[str, Any]:
    sid = canonicalize_name(sid)
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is not None:
        row["ghost"] = True
        if ghost_container_state(row) != "provisioning":
            row["ghost_container_state"] = "ready"
            row.pop("ghost_container_baseline", None)
        save_slots(rows)
        if log:
            log(f"Slot {row.get('id')}: Ghost container recorded as {sid}.")
        return row
    row = _empty_slot(sid)
    row["ghost"] = True
    row["ghost_container_state"] = "ready"
    rows.append(row)
    rows.sort(key=_slot_sort_key)
    save_slots(rows)
    if log:
        log(f"Slot {sid}: created, Ghost container recorded.")
    return row


def ghost_container_state(row: dict[str, Any] | None) -> str:
    """Return the durable Ghost provisioning state for one slot.

    Existing slots predate the checkpoint and remain ready when ``ghost`` is
    true.  A fresh or explicitly reset slot is needed.  This compatibility
    rule prevents the new a0001 repair from recreating every legacy container.
    """
    if not row:
        return "needed"
    explicit = str(row.get("ghost_container_state") or "").strip().lower()
    if explicit in GHOST_CONTAINER_STATES:
        return explicit
    return "ready" if row.get("ghost") else "needed"


def mark_ghost_container_needed(
    sid: str, log: LogFn | None = None
) -> dict[str, Any]:
    """Request exactly one new Ghost instance while preserving slot data."""
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    if row.get("reddit"):
        raise ValueError(f"Slot {sid} already has a created Reddit account")
    row["ghost_container_state"] = "needed"
    row.pop("ghost_container_baseline", None)
    row.pop("ghost_container_instance", None)
    row.pop("crane_container_instance", None)
    row.pop("crane_selection_policy", None)
    save_slots(rows)
    if log:
        log(f"Slot {row.get('id')}: new Ghost container required.")
    return row


def ensure_ghost_container_needed(
    sid: str, log: LogFn | None = None
) -> dict[str, Any]:
    """Create a resumable slot checkpoint before a standalone Ghost create."""
    sid = canonicalize_name(sid)
    if not is_slot_name(sid):
        raise ValueError(f"Bad slot id {sid!r}")
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is not None:
        return row
    row = _empty_slot(sid)
    row["ghost_container_state"] = "needed"
    rows.append(row)
    rows.sort(key=_slot_sort_key)
    save_slots(rows)
    if log:
        log(f"Slot {sid}: resumable Ghost creation checkpoint created.")
    return row


def mark_ghost_container_provisioning(
    sid: str,
    baseline_ids: set[str] | list[str] | tuple[str, ...],
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Save the pre-create instance set before touching Ghost's Create button."""
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    if row.get("reddit"):
        raise ValueError(f"Slot {sid} already has a created Reddit account")
    current = ghost_container_state(row)
    if current not in ("needed", "provisioning"):
        raise ValueError(f"Slot {sid} Ghost container is already ready")
    clean = sorted({str(value or "").strip().upper() for value in baseline_ids if str(value or "").strip()})
    if current == "provisioning":
        saved = sorted(
            {
                str(value or "").strip().upper()
                for value in row.get("ghost_container_baseline", [])
                if str(value or "").strip()
            }
        )
        if clean != saved:
            raise ValueError(
                f"Slot {sid} Ghost provisioning baseline cannot be replaced"
            )
        return row
    row["ghost_container_state"] = "provisioning"
    row["ghost_container_baseline"] = clean
    row.pop("ghost_container_instance", None)
    row.pop("crane_container_instance", None)
    row.pop("crane_selection_policy", None)
    save_slots(rows)
    # Only the persisted needed -> provisioning transition is progress.  Calling
    # this again with the same saved baseline returns above without touching the
    # clock, so a repeated call cannot keep a stalled attempt alive.
    CREATION_PROGRESS.note()
    if log:
        log(f"Slot {row.get('id')}: Ghost provisioning checkpoint saved.")
    return row


def mark_ghost_container_ready(
    sid: str,
    instance_id: str,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Commit the one reconciled Ghost instance after configuration succeeds."""
    instance_id = str(instance_id or "").strip().upper()
    if not instance_id:
        raise ValueError("Missing Ghost container instance identifier")
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    row["ghost"] = True
    row["ghost_container_state"] = "ready"
    row["ghost_container_instance"] = instance_id
    row.pop("ghost_container_baseline", None)
    row.pop("crane_selection_policy", None)
    save_slots(rows)
    CREATION_PROGRESS.note()
    if log:
        log(f"Slot {row.get('id')}: newest Ghost container is ready.")
    return row


def mark_ghost_container_ready_from_crane(
    sid: str,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Commit a container proven through Crane without inventing a Ghost UUID.

    Recovery is allowed only for an unfinished provisioning checkpoint.  The
    policy is durable because Crane may contain duplicate names and every
    later opener must keep choosing the newest exact row at a proven bottom.
    """
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    if row.get("reddit"):
        raise ValueError(f"Slot {sid} already has a created Reddit account")
    current = ghost_container_state(row)
    if current not in ("needed", "provisioning"):
        raise ValueError(f"Slot {sid} is not awaiting Ghost container recovery")
    row["ghost"] = True
    row["ghost_container_state"] = "ready"
    row.pop("ghost_container_baseline", None)
    row.pop("ghost_container_instance", None)
    row["crane_selection_policy"] = "newest_exact"
    save_slots(rows)
    CREATION_PROGRESS.note()
    if log:
        log(
            f"Slot {row.get('id')}: existing Crane container proven; "
            "future runs will select the newest exact row."
        )
    return row


def _slot_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    sid = str(row.get("id") or "").lower()
    n = name_to_index(sid)
    if n is not None:
        return (n, "")
    return (10_000_000, sid)


def container_deletion(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return one slot's validated local-container deletion checkpoint.

    A malformed checkpoint fails closed as absent so it can never be mistaken
    for a verified deletion.
    """
    value = (row or {}).get("container_deletion")
    if not isinstance(value, dict):
        return None
    state = str(value.get("state") or "").strip().lower()
    instance_id = str(value.get("instance_id") or "").strip().upper()
    if state not in CONTAINER_DELETION_STATES or not instance_id:
        return None
    return value


def container_deletion_state(row: dict[str, Any] | None) -> str:
    value = container_deletion(row)
    return str(value.get("state") or "").strip().lower() if value else ""


def container_deletion_instance(row: dict[str, Any] | None) -> str:
    value = container_deletion(row)
    return str(value.get("instance_id") or "").strip().upper() if value else ""


def crane_container_instance(row: dict[str, Any] | None) -> str:
    """Return the full UUID proven from Crane preferences, never Ghost OCR."""
    return str((row or {}).get("crane_container_instance") or "").strip().upper()


def reconcile_crane_container_identity(
    sid: str,
    instance_id: str,
    log: LogFn | None = None,
    *,
    allow_pending_rebind: bool = False,
) -> dict[str, Any]:
    """Atomically bind a slot to the full UUID proven by Crane preferences.

    Ghost's displayed identifier is only a short UI prefix and remains in
    ``ghost_container_instance``.  A pending post-account deletion may be
    migrated once when a fresh Crane inventory uniquely proves the same exact
    slot name.  Completed deletion identities are immutable.
    """
    instance_id = str(instance_id or "").strip().upper()
    if not instance_id or instance_id == "DEFAULT":
        raise ValueError("Missing or unsafe Crane container UUID")
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    stored = str(row.get("id") or sid)
    existing_full = crane_container_instance(row)
    deletion_state = container_deletion_state(row)
    deletion_id = container_deletion_instance(row)
    if deletion_state == "complete" and deletion_id != instance_id:
        raise ValueError(
            f"Slot {stored} completed deletion identity cannot be replaced"
        )
    if existing_full and existing_full != instance_id:
        raise ValueError(
            f"Slot {stored} Crane UUID is already bound to a different container"
        )
    if deletion_state == "pending" and deletion_id != instance_id:
        if not allow_pending_rebind:
            raise ValueError(
                f"Slot {stored} pending deletion identity cannot be replaced"
            )
        deletion = dict(row["container_deletion"])
        deletion["instance_id"] = instance_id
        row["container_deletion"] = deletion
    row["crane_container_instance"] = instance_id
    save_slots(rows)
    if log:
        log(f"Slot {stored}: full Crane container identity verified and saved.")
    return row


def slot_container_deleted(row: dict[str, Any] | None) -> bool:
    """True once the account's local container was verified deleted."""
    return container_deletion_state(row) == "complete"


def slot_container_available(row: dict[str, Any] | None) -> bool:
    """Whether the slot's local container still exists and may be opened.

    A verified automatic deletion removes the container permanently, so manual
    and posting workflows must not silently treat the slot's session as usable
    or silently rebuild the container under the same name.
    """
    return not slot_container_deleted(row)


def mark_container_deletion_pending(
    sid: str,
    instance_id: str,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Durably record the intent to delete one slot's exact local container.

    The ``(slot id, exact instance id)`` pair is written *before* any delete
    gesture, so a crash mid-deletion can be resumed by matching the same exact
    container against a complete inventory instead of re-running signup.  The
    recorded identity is never allowed to change once written.
    """
    instance_id = str(instance_id or "").strip().upper()
    if not instance_id:
        raise ValueError("Missing local container instance identifier")
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    if not row.get("reddit"):
        raise ValueError(f"Slot {sid} has no created Reddit account to clean up")
    stored = str(row.get("id") or sid)
    recorded = crane_container_instance(row)
    if recorded and recorded != instance_id:
        raise ValueError(
            f"Slot {stored} recorded container {recorded} cannot be replaced "
            f"by {instance_id}"
        )
    existing = container_deletion(row)
    if existing is not None and container_deletion_instance(row) != instance_id:
        raise ValueError(
            f"Slot {stored} already has a pending deletion for a different container"
        )
    if container_deletion_state(row) == "complete":
        raise ValueError(f"Slot {stored} container is already recorded as deleted")
    if (
        container_deletion_state(row) == "pending"
        and container_deletion_instance(row) == instance_id
    ):
        # Idempotent re-issue of the same durable intent: never counted as new
        # progress, so a retry cannot keep a stalled attempt alive.
        return row
    row["container_deletion"] = {
        "slot_id": stored,
        "instance_id": instance_id,
        "state": "pending",
        "requested_at_utc": _now(),
    }
    save_slots(rows)
    CREATION_PROGRESS.note()
    if log:
        log(f"Slot {stored}: local container deletion recorded as pending.")
    return row


def mark_container_deletion_complete(
    sid: str,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Commit a verified local-container deletion for one slot.

    The slot row, credentials, iCloud alias, account history and Reddit account
    are preserved; only the local container is marked removed.  The slot number
    stays reserved for allocation.
    """
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    pending = container_deletion(row)
    if pending is None:
        raise ValueError(f"Slot {sid} has no pending local container deletion")
    stored = str(row.get("id") or sid)
    row["container_deletion"] = {
        "slot_id": stored,
        "instance_id": str(pending.get("instance_id") or "").strip().upper(),
        "state": "complete",
        "requested_at_utc": pending.get("requested_at_utc"),
        "completed_at_utc": _now(),
    }
    save_slots(rows)
    CREATION_PROGRESS.note()
    if log:
        log(f"Slot {stored}: local container deletion verified complete.")
    return row


def bulk_reset_cleanup(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a validated cleanup checkpoint for an abandoned bulk attempt.

    This is intentionally independent of :func:`container_deletion`, which is
    reserved for the normal post-account cleanup of a successfully created
    Reddit account.  Malformed data never counts as a completed reset.
    """
    value = (row or {}).get("bulk_reset_cleanup")
    if not isinstance(value, dict):
        return None
    state = str(value.get("state") or "").strip().lower()
    instance_id = str(value.get("instance_id") or "").strip().upper()
    if state not in BULK_RESET_CLEANUP_STATES or not instance_id:
        return None
    return value


def bulk_reset_cleanup_state(row: dict[str, Any] | None) -> str:
    value = bulk_reset_cleanup(row)
    return str(value.get("state") or "").strip().lower() if value else ""


def bulk_reset_cleanup_instance(row: dict[str, Any] | None) -> str:
    value = bulk_reset_cleanup(row)
    return str(value.get("instance_id") or "").strip().upper() if value else ""


def mark_bulk_reset_cleanup_pending(
    sid: str,
    instance_id: str,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Persist an incomplete-attempt cleanup intent before its first delete.

    Once recorded, the exact UUID is immutable.  Reissuing the same pending
    intent is idempotent and deliberately does not count as workflow progress.
    """
    instance_id = str(instance_id or "").strip().upper()
    if not instance_id:
        raise ValueError("Missing bulk-reset container instance identifier")
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    stored = str(row.get("id") or sid)
    if row.get("reddit") is True:
        raise ValueError(
            f"Slot {stored} has a created Reddit account; bulk reset is forbidden"
        )
    if "container_deletion" in row:
        raise ValueError(
            f"Slot {stored} already belongs to post-account container cleanup"
        )
    recorded = crane_container_instance(row)
    if recorded and recorded != instance_id:
        raise ValueError(
            f"Slot {stored} recorded container {recorded} cannot be replaced "
            f"by {instance_id}"
        )
    existing_raw = row.get("bulk_reset_cleanup")
    existing = bulk_reset_cleanup(row)
    if existing_raw is not None and existing is None:
        raise ValueError(
            f"Slot {stored} has a malformed bulk reset cleanup checkpoint"
        )
    if existing is not None:
        existing_id = bulk_reset_cleanup_instance(row)
        if existing_id != instance_id:
            raise ValueError(
                f"Slot {stored} already has bulk reset cleanup for a different "
                "container"
            )
        if bulk_reset_cleanup_state(row) == "complete":
            raise ValueError(f"Slot {stored} bulk reset cleanup is already complete")
        return row
    row["bulk_reset_cleanup"] = {
        "slot_id": stored,
        "instance_id": instance_id,
        "state": "pending",
        "requested_at_utc": _now(),
    }
    save_slots(rows)
    CREATION_PROGRESS.note()
    if log:
        log(f"Slot {stored}: incomplete-attempt cleanup recorded as pending.")
    return row


def mark_bulk_reset_cleanup_complete(
    sid: str,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Commit a locally verified deletion for an incomplete bulk attempt."""
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    stored = str(row.get("id") or sid)
    if row.get("reddit") is True:
        raise ValueError(
            f"Slot {stored} has a created Reddit account; bulk reset is forbidden"
        )
    raw = row.get("bulk_reset_cleanup")
    checkpoint = bulk_reset_cleanup(row)
    if raw is not None and checkpoint is None:
        raise ValueError(
            f"Slot {stored} has a malformed bulk reset cleanup checkpoint"
        )
    if checkpoint is None:
        raise ValueError(f"Slot {stored} has no pending bulk reset cleanup")
    if bulk_reset_cleanup_state(row) == "complete":
        return row
    row["bulk_reset_cleanup"] = {
        "slot_id": stored,
        "instance_id": bulk_reset_cleanup_instance(row),
        "state": "complete",
        "requested_at_utc": checkpoint.get("requested_at_utc"),
        "completed_at_utc": _now(),
    }
    save_slots(rows)
    CREATION_PROGRESS.note()
    if log:
        log(f"Slot {stored}: incomplete-attempt cleanup verified complete.")
    return row


def clear_slot_email(sid: str, log: LogFn | None = None) -> dict[str, Any]:
    """Drop the address on a slot so a new Hide My Email can be minted onto it."""
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    old = row.get("email")
    row["email"] = None
    row["reddit"] = False
    row.pop("reddit_cleanup", None)
    row.pop("creation_feed_swipes_target", None)
    row.pop("creation_feed_swipes_completed", None)
    save_slots(rows)
    if log:
        log(f"Slot {row.get('id')}: cleared email {old}")
    return row


def set_slot_email(
    sid: str, email: str, log: LogFn | None = None, label: str | None = None
) -> dict[str, Any]:
    sid_canon = canonicalize_name(sid)
    email = (email or "").strip().lower()
    eff_label = (label or "").strip() or sid_canon
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is not None:
        if row.get("email"):
            raise ValueError(f"Slot {row.get('id')} already has email {row['email']}.")
        row["email"] = email
        row["email_label"] = eff_label
        save_slots(rows)
        CREATION_PROGRESS.note()
        if log:
            log(f"Slot {row.get('id')}: email {email} label={eff_label!r}")
        return row
    row = _empty_slot(sid_canon)
    row["email"] = email
    row["email_label"] = eff_label
    rows.append(row)
    rows.sort(key=_slot_sort_key)
    save_slots(rows)
    CREATION_PROGRESS.note()
    if log:
        log(f"Slot {sid_canon}: created with email {email} label={eff_label!r}")
    return row


def email_verification_state(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a validated copy of the resumable Reddit email-verification state."""
    value = (row or {}).get("email_verification")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "replacement_index", "label", "phase", "updated_at",
    }:
        raise ValueError("Malformed email verification checkpoint")
    index = value.get("replacement_index")
    label = str(value.get("label") or "").strip().lower()
    phase = str(value.get("phase") or "").strip().lower()
    updated = str(value.get("updated_at") or "").strip()
    if type(index) is not int or index < 0 or not label or phase not in EMAIL_VERIFICATION_PHASES:
        raise ValueError("Malformed email verification checkpoint")
    try:
        datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("Malformed email verification checkpoint") from None
    return {
        "replacement_index": index,
        "label": label,
        "phase": phase,
        "updated_at": updated,
    }


def mark_email_verification(
    sid: str,
    replacement_index: int,
    label: str,
    phase: str,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Persist one crash-safe verification transition for an unfinished slot."""
    sid_canon = canonicalize_name(sid)
    replacement_index = int(replacement_index)
    label = str(label or "").strip().lower()
    phase = str(phase or "").strip().lower()
    expected = sid_canon if replacement_index == 0 else f"{sid_canon}-{replacement_index}"
    if replacement_index < 0 or label != expected or phase not in EMAIL_VERIFICATION_PHASES:
        raise ValueError("Invalid email verification checkpoint")
    rows = load_slots()
    row = find_slot_row(rows, sid_canon)
    if row is None:
        raise ValueError(f"No slot {sid_canon}")
    if row.get("reddit"):
        raise ValueError(f"Slot {sid_canon} already has a created Reddit account")
    previous = row.get("email_verification")
    unchanged = (
        isinstance(previous, dict)
        and previous.get("replacement_index") == replacement_index
        and str(previous.get("label") or "").strip().lower() == label
        and str(previous.get("phase") or "").strip().lower() == phase
    )
    row["email_verification"] = {
        "replacement_index": replacement_index,
        "label": label,
        "phase": phase,
        "updated_at": _now(),
    }
    save_slots(rows)
    if not unchanged:
        # Only a new verification phase is real progress; re-polling the same
        # phase must not reset the bulk inactivity clock.
        CREATION_PROGRESS.note()
    if log:
        log(
            f"Slot {sid_canon}: email verification {phase} "
            f"for replacement {replacement_index}."
        )
    return row


def replace_slot_email(
    sid: str,
    expected_old: str,
    email: str,
    *,
    label: str,
    replacement_index: int,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Atomically activate one newly-created HME address for an unfinished slot."""
    sid_canon = canonicalize_name(sid)
    expected_old = str(expected_old or "").strip().lower()
    email = str(email or "").strip().lower()
    label = str(label or "").strip().lower()
    replacement_index = int(replacement_index)
    expected_label = f"{sid_canon}-{replacement_index}"
    if replacement_index < 1 or label != expected_label or HME_EMAIL_RE.fullmatch(email) is None:
        raise ValueError("Invalid replacement Hide My Email checkpoint")
    rows = load_slots()
    row = find_slot_row(rows, sid_canon)
    if row is None:
        raise ValueError(f"No slot {sid_canon}")
    current = str(row.get("email") or "").strip().lower()
    if current != expected_old:
        raise ValueError(
            f"Slot {sid_canon} email changed before replacement was committed"
        )
    for other in rows:
        if other is row:
            continue
        if str(other.get("email") or "").strip().lower() == email:
            raise ValueError("Replacement Hide My Email belongs to another slot")
    history = row.get("email_alias_history")
    if history is None:
        history = []
    if not isinstance(history, list):
        raise ValueError("Slot email alias history is malformed")
    prior_label = str(row.get("email_label") or sid_canon).strip().lower()
    if expected_old and not any(
        isinstance(item, dict)
        and str(item.get("email") or "").strip().lower() == expected_old
        for item in history
    ):
        history.append({"email": expected_old, "label": prior_label})
    row["email_alias_history"] = history
    row["email"] = email
    row["email_label"] = label
    row["email_replacement_index"] = replacement_index
    row["email_verification"] = {
        "replacement_index": replacement_index,
        "label": label,
        "phase": "replacement_ready",
        "updated_at": _now(),
    }
    row.pop("creation_terminal_failure", None)
    save_slots(rows)
    CREATION_PROGRESS.note()
    if log:
        log(
            f"Slot {sid_canon}: replacement Hide My Email activated "
            f"with label {label!r}."
        )
    return row


def clear_email_verification(sid: str, log: LogFn | None = None) -> dict[str, Any]:
    """Clear retry-only state after Reddit accepts a verification code."""
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    row.pop("email_verification", None)
    save_slots(rows)
    CREATION_PROGRESS.note()
    if log:
        log(f"Slot {row.get('id')}: email verification checkpoint cleared.")
    return row


def mark_creation_terminal_failure(
    sid: str, code: str, reason: str, log: LogFn | None = None
) -> dict[str, Any]:
    """Make an unfinished slot terminal for automatic allocation without deleting it."""
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    if row.get("reddit"):
        raise ValueError(f"Slot {sid} already has a created Reddit account")
    row["creation_terminal_failure"] = {
        "code": str(code or "creation_failed")[:80],
        "reason": str(reason or "Creation failed.")[:300],
        "failed_at_utc": _now(),
    }
    save_slots(rows)
    if log:
        log(f"Slot {row.get('id')}: terminal creation failure recorded ({code}).")
    return row


def slot_creation_terminal_failure(row: dict[str, Any] | None) -> bool:
    value = (row or {}).get("creation_terminal_failure")
    return isinstance(value, dict) and bool(str(value.get("code") or "").strip())


def slot_bulk_skipped(row: dict[str, Any] | None) -> bool:
    """True when bulk creation preserved this attempt and must never retry it."""
    value = (row or {}).get("bulk_skip")
    return isinstance(value, dict) and bool(str(value.get("code") or "").strip())


def slot_creation_unavailable(row: dict[str, Any] | None) -> bool:
    """True when automatic creation/resume must allocate the next id instead.

    Covers the older terminal-creation record and the newer bulk-skip record;
    neither ever reuses the slot id or its saved email.
    """
    return slot_creation_terminal_failure(row) or slot_bulk_skipped(row)


def mark_bulk_skipped(
    sid: str, code: str, reason: str, log: LogFn | None = None
) -> dict[str, Any]:
    """Preserve one account-specific failed attempt for manual follow-up.

    Unlike :func:`retire_slot_id` this keeps the Crane container, the iCloud
    alias, the slot row, credentials and every successful checkpoint.  Unlike
    :func:`mark_creation_terminal_failure` it also accepts a slot whose Reddit
    account was already created, so a later-stage failure is recorded beside a
    preserved successful account instead of replacing it.
    """
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    stored = str(row.get("id") or sid)
    row["bulk_skip"] = {
        "slot_id": stored,
        "code": str(code or "creation_failed")[:80],
        "reason": str(reason or "Creation failed.")[:300],
        "skipped_at_utc": _now(),
        "created_account": bool(row.get("reddit")),
    }
    save_slots(rows)
    if log:
        log(
            f"Slot {stored}: skipped by bulk "
            f"({row['bulk_skip']['code']}); container, alias and checkpoints preserved."
        )
    return row


def set_slot_username(sid: str, username: str, log: LogFn | None = None) -> dict[str, Any]:
    username = (username or "").strip()
    if not username:
        raise ValueError("Empty username")
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    changed = str(row.get("username") or "") != username
    row["username"] = username
    save_slots(rows)
    if changed:
        CREATION_PROGRESS.note()
    if log:
        log(f"Slot {row.get('id')}: username {username}")
    return row


_USERNAME_VARIANT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,28}$")


def username_variant_attempt(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a validated durable requested-username candidate checkpoint."""
    raw = (row or {}).get("username_variant_attempt")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "version", "base", "candidate", "index", "updated_at_utc",
    }:
        raise ValueError("Malformed username variant checkpoint")
    base = str(raw.get("base") or "").strip()
    candidate = str(raw.get("candidate") or "").strip()
    index = raw.get("index")
    if (
        raw.get("version") != 1
        or _USERNAME_VARIANT_RE.fullmatch(base) is None
        or _USERNAME_VARIANT_RE.fullmatch(candidate) is None
        or type(index) is not int
        or index < 0
        or not isinstance(raw.get("updated_at_utc"), str)
    ):
        raise ValueError("Malformed username variant checkpoint")
    return dict(raw)


def mark_username_variant_attempt(
    sid: str,
    base: str,
    candidate: str,
    index: int,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Persist one candidate before it is typed or interpreted on Reddit."""
    base = str(base or "").strip()
    candidate = str(candidate or "").strip()
    if (
        _USERNAME_VARIANT_RE.fullmatch(base) is None
        or _USERNAME_VARIANT_RE.fullmatch(candidate) is None
        or type(index) is not int
        or index < 0
    ):
        raise ValueError("Invalid username variant checkpoint")
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    existing = username_variant_attempt(row)
    if existing is not None:
        existing_base = str(existing.get("base") or "")
        existing_index = int(existing.get("index") or 0)
        if existing_base != base and str(existing.get("candidate") or "") != base:
            raise ValueError("Username variant checkpoint belongs to another base")
        if existing_base == base and index < existing_index:
            raise ValueError("Refused username variant checkpoint regression")
    row["username_variant_attempt"] = {
        "version": 1,
        "base": base,
        "candidate": candidate,
        "index": index,
        "updated_at_utc": _now(),
    }
    save_slots(rows)
    CREATION_PROGRESS.note()
    if log:
        log(
            f"Slot {row.get('id')}: username candidate checkpoint "
            f"{index} saved (value redacted)."
        )
    return row


def clear_username_variant_attempt(
    sid: str, log: LogFn | None = None
) -> dict[str, Any]:
    """Clear the candidate checkpoint only after the accepted name is saved."""
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    changed = "username_variant_attempt" in row
    row.pop("username_variant_attempt", None)
    save_slots(rows)
    if changed:
        CREATION_PROGRESS.note()
    if changed and log:
        log(f"Slot {row.get('id')}: username candidate checkpoint cleared.")
    return row


def set_slot_password(sid: str, password: str, log: LogFn | None = None) -> dict[str, Any]:
    password = password or ""
    if len(password) < 8:
        raise ValueError("Password too short")
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    changed = str(row.get("password") or "") != password
    row["password"] = password
    save_slots(rows)
    if changed:
        CREATION_PROGRESS.note()
    if log:
        log(f"Slot {row.get('id')}: password saved (not printed)")
    return row


def set_slot_birth_date(sid: str, birth_date: str) -> dict[str, Any]:
    """Atomically save one slot's canonical DD.MM.YYYY Reddit birth date."""
    value = str(birth_date or "").strip()
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value) is None:
        raise ValueError("Reddit birth date must use DD.MM.YYYY format")
    try:
        datetime.strptime(value, "%d.%m.%Y")
    except ValueError as exc:
        raise ValueError("Reddit birth date must be a real calendar date") from exc
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    changed = str(row.get("reddit_birth_date") or "") != value
    row["reddit_birth_date"] = value
    save_slots(rows)
    if changed:
        CREATION_PROGRESS.note()
    return row


def mark_reddit_created(
    sid: str,
    log: LogFn | None = None,
    *,
    feed_swipes: int = 0,
) -> dict[str, Any]:
    """Mark account creation complete without claiming that Day 1 ran."""
    feed_swipes = int(feed_swipes)
    if feed_swipes != 0 and feed_swipes < CREATION_FEED_SWIPE_MIN:
        raise ValueError(
            "Creation feed swipe target must be zero or a positive whole number"
        )
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    was_created = bool(row.get("reddit"))
    progress_started = bool(row.get("reddit")) and (
        "creation_feed_swipes_target" in row
        or "creation_feed_swipes_completed" in row
    )
    if progress_started:
        # A panel setting changed after creation started must never rewrite the
        # slot's persisted target or completed checkpoint.
        saved_target, saved_completed = creation_feed_swipe_progress(row)
        feed_swipes = saved_target
    row["reddit"] = True
    row["day1"] = False
    row["reddit_cleanup"] = "pending"
    row["creation_feed_swipes_target"] = feed_swipes
    row["creation_feed_swipes_completed"] = (
        saved_completed if progress_started else 0
    )
    row.pop("day1_at", None)
    row["reddit_at"] = _now()
    save_slots(rows)
    if not was_created:
        CREATION_PROGRESS.note()
    email = str(row.get("email") or "")
    username = str(row.get("username") or "")
    pw = str(row.get("password") or "") or None
    stored = str(row.get("id") or sid)
    if email and username:
        write_account_text(
            stored,
            email,
            username,
            pw,
            created=True,
            day1=False,
        )
    if not was_created and username and pw:
        with open(ACCOUNTS_PATH, "a", encoding="utf-8") as f:
            f.write(f"{username}:{pw}\n")
    if log:
        log(f"Slot {stored}: Reddit created; Day 1 not run.")
    return row


def creation_feed_swipe_progress(row: dict[str, Any] | None) -> tuple[int, int]:
    """Return a validated persisted creation-swipe target and completion count.

    Legacy slots have neither field and therefore require no creation swipes.
    Partially written or malformed state fails closed instead of silently
    skipping a configured swipe sequence.
    """
    if not row or (
        "creation_feed_swipes_target" not in row
        and "creation_feed_swipes_completed" not in row
    ):
        return 0, 0
    try:
        target = int(row.get("creation_feed_swipes_target"))
        completed = int(row.get("creation_feed_swipes_completed"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid creation feed swipe progress") from exc
    if target == 0 and completed == 0:
        return 0, 0
    if target < CREATION_FEED_SWIPE_MIN:
        raise ValueError("Invalid creation feed swipe target")
    if completed < 0 or completed > target:
        raise ValueError("Invalid completed creation feed swipe count")
    return target, completed


def mark_creation_feed_swipe_completed(
    sid: str,
    completed: int,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Atomically advance one slot's completed creation-feed swipe count."""
    completed = int(completed)
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    if not row.get("reddit"):
        raise ValueError(f"Slot {sid} has no created Reddit account")
    target, current = creation_feed_swipe_progress(row)
    if target == 0:
        raise ValueError(f"Slot {sid} has no creation feed swipe target")
    if completed < current:
        raise ValueError(
            f"Refused creation feed swipe regression {current} -> {completed}"
        )
    if completed > target:
        raise ValueError(
            f"Creation feed swipe count {completed} exceeds target {target}"
        )
    advanced = completed > current
    row["creation_feed_swipes_completed"] = completed
    save_slots(rows)
    if advanced:
        # A verified swipe advance is real progress; re-writing the same count
        # is a retry and must not reset the bulk inactivity clock.
        CREATION_PROGRESS.note()
    if log:
        log(f"Slot {row.get('id')}: creation feed swipes {completed}/{target}.")
    return row


def mark_create_day1(sid: str, log: LogFn | None = None) -> dict[str, Any]:
    """Mark legacy Day 1 only after verified creation cleanup is complete."""
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    if not slot_reddit_ready(row):
        raise ValueError(
            f"Slot {sid} must have created Reddit and completed cleanup before Day 1"
        )
    row["day1"] = True
    row["day1_at"] = _now()
    save_slots(rows)
    email = str(row.get("email") or "")
    username = str(row.get("username") or "")
    pw = str(row.get("password") or "") or None
    stored = str(row.get("id") or sid)
    if email and username:
        write_account_text(stored, email, username, pw, created=True, day1=True)
    if log:
        log(f"Slot {stored}: create + Day 1 locked.")
    return row


def slot_create_day1_done(row: dict[str, Any] | None) -> bool:
    return bool(row and row.get("reddit") and row.get("day1"))


def slot_reddit_done(row: dict[str, Any] | None) -> bool:
    return bool(row and row.get("reddit"))


def reddit_cleanup_state(row: dict[str, Any] | None) -> str:
    """Return the effective cleanup checkpoint for a slot.

    Historical created slots predate the checkpoint field and are therefore
    treated as complete.  Explicit missing/invalid state on an uncreated or
    newly-created slot fails closed as pending.
    """
    if not row or not row.get("reddit"):
        return "pending"
    if "reddit_cleanup" not in row:
        return "complete"
    state = str(row.get("reddit_cleanup") or "").strip().lower()
    return state if state in REDDIT_CLEANUP_STATES else "pending"


def slot_reddit_cleanup_pending(row: dict[str, Any] | None) -> bool:
    """Whether a created slot still needs verified app cleanup."""
    return bool(
        row
        and row.get("reddit")
        and reddit_cleanup_state(row) in ("pending", "app_stopped")
    )


def slot_reddit_ready(row: dict[str, Any] | None) -> bool:
    """Whether a slot is created and safe to expose to posting workflows."""
    return bool(
        row
        and row.get("reddit")
        and reddit_cleanup_state(row) == "complete"
    )


def mark_reddit_cleanup(
    sid: str,
    state: str,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Persist one valid cleanup checkpoint for an already-created slot."""
    state = str(state or "").strip().lower()
    if state not in REDDIT_CLEANUP_STATES:
        raise ValueError(
            "Bad Reddit cleanup state; expected pending, app_stopped, or complete"
        )
    rows = load_slots()
    row = find_slot_row(rows, sid)
    if row is None:
        raise ValueError(f"No slot {sid}")
    if not row.get("reddit"):
        raise ValueError(f"Slot {sid} has no created Reddit account")
    current = reddit_cleanup_state(row)
    allowed = {
        "pending": {"pending", "app_stopped"},
        "app_stopped": {"app_stopped", "complete"},
        "complete": {"complete"},
    }
    if state not in allowed[current]:
        raise ValueError(
            f"Refused Reddit cleanup checkpoint regression {current!r} -> {state!r}"
        )
    row["reddit_cleanup"] = state
    save_slots(rows)
    if state != current:
        CREATION_PROGRESS.note()
    if log:
        log(f"Slot {row.get('id')}: Reddit cleanup {state}.")
    return row


def write_account_text(
    sid: str,
    email: str,
    username: str,
    password: str | None = None,
    created: bool | None = None,
    day1: bool | None = None,
    interests: str | None = None,
) -> str:
    """Plain text row in logs/accounts.txt. Never print password."""
    sid = sid.lower()
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "accounts.txt")
    line = f"{sid}\t{username}\t{email}"
    if password:
        line += f"\t{password}"
    existing = ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
    lines = [
        ln
        for ln in existing.splitlines()
        if ln.strip() and not ln.lower().startswith(sid + "\t")
    ]
    lines.append(line)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    one = os.path.join(LOG_DIR, f"{sid}.txt")
    old = ""
    if os.path.isfile(one):
        with open(one, "r", encoding="utf-8") as f:
            old = f.read()
    interests_line = ""
    if interests:
        blob = ",".join(p.strip() for p in interests.split(",") if p.strip())
        if blob:
            interests_line = "interests=" + blob
    else:
        for ln in old.splitlines():
            if ln.startswith("interests="):
                interests_line = ln.rstrip()
                break
    if created is None:
        created = "created=yes" in old
    if day1 is None:
        day1 = "day1=yes" in old
    with open(one, "w", encoding="utf-8") as f:
        f.write(f"slot={sid}\nusername={username}\nemail={email}\n")
        if password:
            f.write(f"password={password}\n")
        if interests_line:
            f.write(interests_line.rstrip() + "\n")
        f.write(f"created={'yes' if created else 'no'}\n")
        f.write(f"day1={'yes' if day1 else 'no'}\n")
    return path


def get_slot(sid: str) -> dict[str, Any] | None:
    return find_slot_row(load_slots(), sid)
