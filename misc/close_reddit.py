# UTF-8
"""Close Reddit fully from recents. Not killall."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reddit_open import close_reddit
from slots import load_slots, slot_reddit_cleanup_pending
from ssh_device import Phone, PhoneError
from ui_tree import TapStack

os.environ["SKIP_SHOT"] = "1"


def log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        safe = str(msg).encode(encoding, errors="backslashreplace").decode(encoding)
        print(safe, flush=True)


def pending_cleanup_ids(rows=None) -> list[str]:
    """Return creation slots whose durable cleanup must stay in Creator."""
    if rows is None:
        rows = load_slots()
    return [
        str(row.get("id") or "").strip()
        for row in rows
        if slot_reddit_cleanup_pending(row) and str(row.get("id") or "").strip()
    ]


def main() -> int:
    try:
        pending = pending_cleanup_ids()
    except (OSError, ValueError) as exc:
        log(f"ERROR Could not verify Reddit cleanup checkpoints: {exc}")
        return 1
    if pending:
        log(
            "ERROR Verified creation cleanup is pending. Use Creator -> "
            "Finish cleanup so slot checkpoints remain synchronized."
        )
        return 1

    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend.")
        close_reddit(stack, phone, log)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
