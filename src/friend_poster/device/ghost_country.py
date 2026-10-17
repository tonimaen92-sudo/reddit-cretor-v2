# UTF-8
"""Open Ghost Preferred Country from a live dump. Dump-first. No dylib. No home page-swipe."""

from __future__ import annotations

import os
import sys
import time
from friend_poster.core.config import GHOST_BUNDLE  # noqa: E402
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"


def log(msg: str) -> None:
    print(msg, flush=True)


def labs(nodes) -> str:
    out = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen() or n.y < 50:
            continue
        blob = collapsed_text(n.labels())
        if blob:
            out.append(blob)
    return " | ".join(out[:35])


def hold_tap(stack: TapStack, x: int, y: int) -> None:
    lua = (
        "screen.init(0)\n"
        f"touch.on(1, {int(x)}, {int(y)})\n"
        "sys.msleep(280)\n"
        "touch.off(1)\n"
    )
    log(f"hold-tap ({x},{y})")
    stack._run_xxt_lua("ra_hold.lua", lua, timeout=15)
    time.sleep(1.8)


def main() -> int:
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        code, out = phone.exec_ok(
            "echo '-- prefs --'; "
            "ls -l /var/jb/var/mobile/Library/Preferences/com.sgwc.ghost.plist "
            "/var/mobile/Library/Preferences/com.sgwc.ghost.plist "
            "/var/mobile/Library/Preferences/com.sgwc.Ghost.plist 2>&1; "
            "echo '-- grep binary --'; "
            "grep -a -o -E '.{0,20}(Country|Vietnam|United States|Preferred).{0,20}' "
            "/var/jb/Applications/Ghost.app/Ghost 2>/dev/null | head -40",
            timeout=20,
        )
        log(out or "")
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            print("GHOST_COUNTRY=NO_TAP")
            return 1
        phone.exec_ok(f"uiopen --bundleid {GHOST_BUNDLE}", timeout=10)
        time.sleep(2.0)
        _, nodes = dump_named(stack, phone, "270_now", log)
        log("NOW " + labs(nodes))

        if find_label_exact(nodes, ("Changelog", "Respring", "Safemode")):
            log("on about page; swipe back to Home")
            stack.swipe(375, 420, 375, 980)
            time.sleep(1.2)
            _, nodes = dump_named(stack, phone, "270_home", log)
            log("HOME " + labs(nodes))

        country = find_label_exact(nodes, ("Preferred Country",)) or find_label(
            nodes, ("Preferred Country", "Choose your preferred country")
        )
        if country is None:
            print("GHOST_COUNTRY=NO_ROW")
            return 1

        # Canary: Application List is the same card style and opened in earlier dumps.
        apps = find_label_exact(nodes, ("Application List",))
        if apps is not None:
            stack.tap_row(apps)
            time.sleep(1.8)
            _, nodes = dump_named(stack, phone, "270_canary_apps", log)
            log("CANARY " + labs(nodes))
            if find_label_exact(nodes, ("User Apps", "All Apps", "Applications")):
                log("Application List opens; closing")
                xbtn = find_label_exact(nodes, ("X",))
                if xbtn is not None and xbtn.y < 120:
                    stack.tap_xy(*xbtn.center())
                else:
                    stack.tap_xy(674, 85)
                time.sleep(1.5)
                _, nodes = dump_named(stack, phone, "270_back_home", log)
            else:
                log("Application List tap missed")

        country = find_label_exact(nodes, ("Preferred Country",)) or find_label(
            nodes, ("Preferred Country", "Choose your preferred country")
        )
        if country is None:
            print("GHOST_COUNTRY=NO_ROW_AFTER_CANARY")
            return 1
        x, y = min(max(country.x + 180, 240), 560), country.center()[1]
        hold_tap(stack, x, y)
        _, nodes = dump_named(stack, phone, "272_after_hold", log)
        log("HOLD " + labs(nodes))
        if find_label_exact(nodes, ("Ghost Mode",)):
            trail_x = max(stack.screen_w - 62, 680)
            hold_tap(stack, trail_x, y)
            _, nodes = dump_named(stack, phone, "273_after_trail", log)
            log("TRAIL " + labs(nodes))
        if find_label_exact(nodes, ("Ghost Mode",)):
            print("GHOST_COUNTRY=STILL_HOME")
            return 1
        print("GHOST_COUNTRY=OPEN")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
