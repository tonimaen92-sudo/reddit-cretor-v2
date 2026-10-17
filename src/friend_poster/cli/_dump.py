# UTF-8
"""Live dump only. Usage: python misc/_dump.py 400_start"""

from __future__ import annotations

import os
import sys
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import TapStack, dump_named  # noqa: E402

os.environ["SKIP_SHOT"] = "1"


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    stem = sys.argv[1] if len(sys.argv) > 1 else "live"
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        dump_named(stack, phone, stem, log)
        print("DUMP=" + stem)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
