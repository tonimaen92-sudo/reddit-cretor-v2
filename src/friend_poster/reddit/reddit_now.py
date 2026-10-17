# UTF-8
"""Dump live Reddit screen after r1 open. Optional: tap Not now on the notifications sheet."""

from __future__ import annotations

import os
import sys
import time
from friend_poster.core.slots import get_slot
from friend_poster.device.ssh_device import Phone, PhoneError
from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label_exact

os.environ["SKIP_SHOT"] = "1"


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    tap_not_now = "--not-now" in sys.argv
    tap_create = "--create" in sys.argv
    type_email = "--type-email" in sys.argv
    tap_continue = "--continue" in sys.argv
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        blob, nodes = dump_named(stack, phone, "840_now", log, shot=True)
        labels = [collapsed_text(n.labels()) for n in nodes if n.on_screen() and n.labels()]
        for s in labels:
            log("  LABEL " + s[:80])
        if any("continue with apple" in s or "sign in with apple" in s for s in labels):
            log("SIWA visible — will not tap Apple.")
        if tap_create:
            n = find_label_exact(
                nodes,
                ("Create a new account", "Create new account", "Create a New Account"),
            )
            if n is None:
                raise PhoneError("Create a new account not on screen. See 830_login.png.")
            log(f"Create a new account [{n.x},{n.y} {n.w}x{n.h}]")
            stack.tap_xy(*n.center())
            time.sleep(1.3)
            dump_named(stack, phone, "831_after_create", log, shot=True)
            return 0
        if type_email:
            if find_label_exact(nodes, ("Hi new friend,", "welcome to Reddit")) is None and find_label_exact(
                nodes, ("Enter your email to get started",)
            ) is None:
                raise PhoneError("Not on welcome email screen. See 840_now.png.")
            field = find_label_exact(nodes, ("Email",))
            if field is None:
                raise PhoneError("Email field not on screen. See 840_now.png.")
            slot = get_slot("a0001")
            addr = str((slot or {}).get("email") or "").strip()
            if not addr or "@icloud.com" not in addr:
                raise PhoneError("r1 has no Hide My Email in slots.json.")
            # Placeholder sits left in the pill. Tap field center x, not the word.
            x = 375
            y = field.center()[1]
            log(f"Email field tap ({x},{y}) then type r1 address")
            stack.tap_xy(x, y)
            time.sleep(0.5)
            stack.type_text(addr)
            time.sleep(0.8)
            dump_named(stack, phone, "841_email_typed", log, shot=True)
            log(f"TYPED r1 {addr}. Did not tap Continue.")
            return 0
        if tap_continue:
            n = find_label_exact(nodes, ("Continue",))
            if n is None:
                raise PhoneError("Continue not on screen. See 840_now.png.")
            if "apple" in collapsed_text(n.labels()):
                raise PhoneError("Refusing Continue with Apple.")
            log(f"Continue [{n.x},{n.y} {n.w}x{n.h}]")
            stack.tap_xy(*n.center())
            time.sleep(2.0)
            dump_named(stack, phone, "842_after_continue", log, shot=True)
            return 0
        if not tap_not_now:
            return 0
        n = find_label_exact(nodes, ("Not now", "Not Now"))
        if n is None:
            n = next(
                (x for x in nodes if "not now" in collapsed_text(x.labels())),
                None,
            )
        if n is None:
            raise PhoneError("Not now not in dump. See 830_login.png. Did not guess.")
        log(f"Not now [{n.x},{n.y} {n.w}x{n.h}]")
        stack.tap_xy(*n.center())
        time.sleep(0.9)
        dump_named(stack, phone, "822_after_not_now", log, shot=True)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
