# UTF-8
"""Small swipe on Ghost tutorial to catch steps 4-5."""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack, collapsed_text, dump_named  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    os.environ["SKIP_SHOT"] = "1"
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        dump_named(stack, phone, "246_tut_top", log)
        stack.swipe(375, 1050, 375, 820)
        time.sleep(0.8)
        blob, nodes = dump_named(stack, phone, "247_tut_45", log)
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen() or n.y < 50:
                continue
            print("S45", collapsed_text(n.labels()))
        stack.swipe(375, 1050, 375, 820)
        time.sleep(0.8)
        blob, nodes = dump_named(stack, phone, "248_tut_45b", log)
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen() or n.y < 50:
                continue
            print("S45B", collapsed_text(n.labels()))
        print("TUTORIAL_45=OK")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
