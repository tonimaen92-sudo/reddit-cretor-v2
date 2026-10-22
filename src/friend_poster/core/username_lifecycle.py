"""Crash-safe username claim ledger for Creator batches.

The editor/draft is only an input pool.  Once Reddit has positively shown the
exact requested username and confirmed availability, the name is durably bound
to that slot here before Continue is tapped.  A stopped/crashed attempt keeps
that claim for the same slot; another slot may never reuse it.  Proven account
cleanup promotes the claim to success.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from friend_poster.core.config import LOG_DIR


LEDGER_PATH = Path(LOG_DIR) / "username_ledger.json"
_LOCK = threading.RLock()
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,28}$")
_SLOT_RE = re.compile(r"^[a-z]\d{4,}$")


class UsernameLedgerError(RuntimeError):
    """Username ownership could not be proven or persisted safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def empty_ledger() -> dict:
    return {"version": 1, "claims": {}}


def _validate(data: object) -> dict:
    if not isinstance(data, dict) or set(data) != {"version", "claims"}:
        raise UsernameLedgerError("Username claim ledger is malformed.")
    if data.get("version") != 1 or not isinstance(data.get("claims"), dict):
        raise UsernameLedgerError("Username claim ledger is malformed.")
    for folded, claim in data["claims"].items():
        if not isinstance(folded, str) or not isinstance(claim, dict):
            raise UsernameLedgerError("Username claim ledger is malformed.")
        if set(claim) != {
            "username", "slot_id", "status", "claimed_at_utc", "completed_at_utc",
        }:
            raise UsernameLedgerError("Username claim ledger is malformed.")
        username = str(claim.get("username") or "")
        sid = str(claim.get("slot_id") or "")
        if (
            _USERNAME_RE.fullmatch(username) is None
            or username.casefold() != folded
            or _SLOT_RE.fullmatch(sid) is None
            or claim.get("status") not in {"processing", "success"}
            or not isinstance(claim.get("claimed_at_utc"), str)
            or (
                claim.get("completed_at_utc") is not None
                and not isinstance(claim.get("completed_at_utc"), str)
            )
        ):
            raise UsernameLedgerError("Username claim ledger is malformed.")
    return data


def load_ledger(path: Path = LEDGER_PATH) -> dict:
    if not path.exists():
        return empty_ledger()
    try:
        with _LOCK, path.open(encoding="utf-8") as handle:
            return _validate(json.load(handle))
    except UsernameLedgerError:
        raise
    except (OSError, ValueError, TypeError):
        raise UsernameLedgerError("Username claim ledger could not be read safely.") from None


def save_ledger(data: dict, path: Path = LEDGER_PATH) -> None:
    _validate(data)
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=".username-ledger-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        raise UsernameLedgerError("Username claim ledger could not be saved safely.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def claim_username(username: str, slot_id: str, path: Path = LEDGER_PATH) -> dict:
    """Atomically bind one exact username to one slot, idempotently."""
    username = str(username or "").strip()
    slot_id = str(slot_id or "").strip().lower()
    if _USERNAME_RE.fullmatch(username) is None or _SLOT_RE.fullmatch(slot_id) is None:
        raise UsernameLedgerError("Username claim is malformed.")
    folded = username.casefold()
    with _LOCK:
        data = load_ledger(path)
        existing = data["claims"].get(folded)
        if existing is not None and existing["slot_id"] != slot_id:
            raise UsernameLedgerError(
                f"Username {username!r} is already bound to {existing['slot_id']}."
            )
        if existing is not None:
            return dict(existing)
        claim = {
            "username": username,
            "slot_id": slot_id,
            "status": "processing",
            "claimed_at_utc": _now(),
            "completed_at_utc": None,
        }
        data["claims"][folded] = claim
        save_ledger(data, path)
        return dict(claim)


def mark_username_success(username: str, slot_id: str, path: Path = LEDGER_PATH) -> dict:
    """Promote a same-slot claim only after verified account cleanup."""
    claim = claim_username(username, slot_id, path)
    folded = str(username).strip().casefold()
    with _LOCK:
        data = load_ledger(path)
        stored = data["claims"][folded]
        if stored["slot_id"] != str(slot_id).strip().lower():
            raise UsernameLedgerError("Username success owner does not match its claim.")
        if stored["status"] != "success":
            stored["status"] = "success"
            stored["completed_at_utc"] = _now()
            save_ledger(data, path)
        return dict(stored)


def username_owners(path: Path = LEDGER_PATH) -> dict[str, dict]:
    """Return case-folded claims for duplicate prevention."""
    return {key: dict(value) for key, value in load_ledger(path)["claims"].items()}


def reconcile_from_slots(rows: list[dict], path: Path = LEDGER_PATH) -> dict[str, dict]:
    """Import historical processing/success ownership from durable slots."""
    with _LOCK:
        data = load_ledger(path)
        changed = False
        for row in rows:
            username = str((row or {}).get("username") or "").strip()
            sid = str((row or {}).get("id") or "").strip().lower()
            if _USERNAME_RE.fullmatch(username) is None or _SLOT_RE.fullmatch(sid) is None:
                continue
            folded = username.casefold()
            existing = data["claims"].get(folded)
            if existing is not None and existing["slot_id"] != sid:
                raise UsernameLedgerError(
                    f"Username {username!r} appears in both {existing['slot_id']} and {sid}."
                )
            success = bool(row.get("reddit")) and str(row.get("reddit_cleanup") or "") == "complete"
            wanted = "success" if success else "processing"
            if existing is None:
                data["claims"][folded] = {
                    "username": username,
                    "slot_id": sid,
                    "status": wanted,
                    "claimed_at_utc": str(row.get("reddit_at") or row.get("created_at") or _now()),
                    "completed_at_utc": _now() if success else None,
                }
                changed = True
            elif success and existing["status"] != "success":
                existing["status"] = "success"
                existing["completed_at_utc"] = _now()
                changed = True
        if changed:
            save_ledger(data, path)
        return {key: dict(value) for key, value in data["claims"].items()}
