# UTF-8
"""On Choose a Country list: scroll to Japan, tap it, then Reddit Enable/Crane/Generate.

Do not tap i. Stop if the screen leaves the country list.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack, dump_named, find_label, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"


def log(msg: str) -> None:
    print(msg, flush=True)


def dump(stack: TapStack, phone: Phone, stem: str):
    _, nodes = dump_named(stack, phone, stem, log)
    return nodes


def on_list(nodes) -> bool:
    return find_label(nodes, ("Choose a Country", "Australia", "Japan", "China", "Canada")) is not None


def dead(nodes) -> bool:
    return find_label_exact(nodes, ("Changelog", "Erase", "Detail description")) is not None


def main() -> int:
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        nodes = dump(stack, phone, "381_now")
        if not on_list(nodes):
            print("GHOST=NOT_COUNTRY_LIST")
            return 1
        n = find_label(nodes, ("Japan",))
        for i in range(10):
            if n is not None:
                break
            if dead(nodes) or not on_list(nodes):
                print("GHOST=LEFT_LIST")
                return 1
            log(f"list swipe {i}")
            stack.swipe(375, 1000, 375, 740)
            time.sleep(0.9)
            nodes = dump(stack, phone, f"382_list_{i}")
            n = find_label(nodes, ("Japan",))
        if n is None:
            print("GHOST=NO_JAPAN")
            return 1
        stack.tap_row(n)
        time.sleep(1.8)
        nodes = dump(stack, phone, "383_after_japan")

        if find_label_exact(nodes, ("Application List",)):
            stack.tap_row(find_label_exact(nodes, ("Application List",)))
            time.sleep(1.8)
            nodes = dump(stack, phone, "384_apps")
        if find_label_exact(nodes, ("User Apps",)):
            stack.tap_xy(*find_label_exact(nodes, ("User Apps",)).center())
            time.sleep(1.2)
            nodes = dump(stack, phone, "385_user")
        reddit = find_label_exact(nodes, ("Reddit",))
        if reddit is None:
            print("GHOST=NO_REDDIT")
            return 1
        stack.tap_row(reddit)
        time.sleep(1.8)
        nodes = dump(stack, phone, "386_reddit")
        enable = find_label(nodes, ("Enable Ghost for Reddit", "Enable"))
        if enable is not None:
            stack.tap_switch(enable)
            time.sleep(1.2)
            nodes = dump(stack, phone, "387_enable")
        adv = find_label_exact(nodes, ("Advanced",))
        if adv is not None:
            stack.tap_xy(*adv.center())
            time.sleep(1.6)
            nodes = dump(stack, phone, "388_advanced")
        crane = find_label(nodes, ("Hide", "Crane paths", "Crane"))
        if crane is not None:
            stack.tap_switch(crane)
            time.sleep(1.0)
            nodes = dump(stack, phone, "389_crane")
        spoof = find_label_exact(nodes, ("Spoofer",))
        if spoof is not None:
            stack.tap_xy(*spoof.center())
            time.sleep(1.6)
            nodes = dump(stack, phone, "390_spoofer")
        gen = find_label(nodes, ("Formulate", "Generate"))
        if gen is None:
            print("GHOST=NO_GENERATE")
            return 1
        stack.tap_row(gen)
        time.sleep(2.2)
        dump(stack, phone, "391_generate")
        print("GHOST=OK")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
