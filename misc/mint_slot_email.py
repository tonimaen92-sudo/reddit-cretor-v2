# UTF-8
"""Mint one Hide My Email onto a Ghost slot that has no address yet.

  python misc/mint_slot_email.py r11

Does not open Reddit. Does not replace an existing address.
If the user deleted the old HME, use replace_slot_email.py instead.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hidemyemail import create_one_hide_my_email  # noqa: E402
from slots import get_slot, set_slot_email  # noqa: E402
from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack  # noqa: E402

os.environ["SKIP_SHOT"] = "1"


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    sid = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    if not sid:
        print("ERROR Need a slot id, e.g. python misc/mint_slot_email.py r11")
        return 1
    slot = get_slot(sid)
    if not slot or not slot.get("ghost"):
        print(f"ERROR Slot {sid} needs a Ghost container first (ghost_live.py).")
        return 1
    old = str(slot.get("email") or "").strip()
    if old:
        print(
            f"ERROR Slot {sid} already has {old}. "
            "Do not mint over it. If the user deleted that address, "
            "python misc/replace_slot_email.py " + sid
        )
        return 1
    label = str(slot.get("email_label") or sid).lower()
    log(f"Minting Hide My Email for {sid} labeled {label}. No Reddit.")
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend. Open XXTouch Elite, Remote Service on.")
        addr = create_one_hide_my_email(phone, stack, log, label=label)
        set_slot_email(sid, addr, log, label=label)
        log(f"MINTED {sid} -> {addr}")
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
