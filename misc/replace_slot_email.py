# UTF-8
"""Mint a new Hide My Email onto an existing slot (r1 after the old address was deactivated).

  python misc/replace_slot_email.py r1
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hidemyemail import create_one_hide_my_email  # noqa: E402
from slots import clear_slot_email, get_slot, set_slot_email  # noqa: E402
from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack  # noqa: E402

os.environ["SKIP_SHOT"] = "1"


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    sid = (sys.argv[1] if len(sys.argv) > 1 else "a0001").strip().lower()
    slot = get_slot(sid)
    if not slot or not slot.get("ghost"):
        print(f"ERROR Slot {sid} needs a Ghost container first.")
        return 1
    old = str(slot.get("email") or "")
    label = str(slot.get("email_label") or sid).lower()
    log(f"Replacing {sid} email {old or '(empty)'} with a new Hide My Email labeled {label}")
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend. Open XXTouch Elite, Remote Service on.")
        if old:
            clear_slot_email(sid, log)
        addr = create_one_hide_my_email(phone, stack, log, label=label)
        set_slot_email(sid, addr, log, label=label)
        log(f"REPLACED {sid} -> {addr}")
        log("Did not open Reddit.")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    except ValueError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
