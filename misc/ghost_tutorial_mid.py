# UTF-8
"""Dump Ghost tutorial steps 4-5 by scrolling back up. No Generate."""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    os.environ["SKIP_SHOT"] = "1"
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        blob, nodes = dump_named(stack, phone, "243_tut_now", log)
        btn = find_label_exact(nodes, ("Dismiss",))
        if btn:
            stack.tap_xy(*btn.center())
            time.sleep(1.0)
        # Scroll down (content moves down) to recover steps 4-5.
        stack.swipe(375, 420, 375, 980)
        time.sleep(0.8)
        blob, nodes = dump_named(stack, phone, "244_tut_mid", log)
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen() or n.y < 50:
                continue
            print("MID", collapsed_text(n.labels()))
        stack.swipe(375, 420, 375, 900)
        time.sleep(0.8)
        blob, nodes = dump_named(stack, phone, "245_tut_mid2", log)
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen() or n.y < 50:
                continue
            print("MID2", collapsed_text(n.labels()))
        print("TUTORIAL_MID=OK")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
