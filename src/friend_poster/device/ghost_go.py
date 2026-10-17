# UTF-8
"""Apply Ghost recipe from live dumps. Never tap the info i. Never Reset/Erase.

Local, Japan, Reddit Enable, Hide Crane paths, Generate Default.
"""

from __future__ import annotations

import os
import sys
import time
from friend_poster.core.config import GHOST_BUNDLE, LOG_DIR  # noqa: E402
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import (  # noqa: E402
    TapStack,
    collapsed_text,
    dump_named,
    find_label,
    find_label_exact,
)

os.environ["SKIP_SHOT"] = "1"


def log(msg: str) -> None:
    print(msg, flush=True)


def dump(stack: TapStack, phone: Phone, stem: str):
    _, nodes = dump_named(stack, phone, stem, log)
    btn = find_label_exact(nodes, ("Dismiss",))
    err = find_label_exact(nodes, ("Runtime Error",))
    if btn or err:
        stack.tap_xy(*(btn or err).center())
        time.sleep(1.0)
        _, nodes = dump_named(stack, phone, stem + "_alert", log)
    return nodes


def is_info_sheet(nodes) -> bool:
    return find_label(nodes, ("Detail description",)) is not None and find_label(
        nodes, ("When you generate random data",)
    ) is not None


def is_country_list(nodes) -> bool:
    return find_label(nodes, ("Choose a Country", "Australia", "Japan")) is not None


def is_about(nodes) -> bool:
    return find_label_exact(nodes, ("Changelog", "Respring", "Erase")) is not None


def skip_i(n) -> bool:
    return collapsed_text(n.labels()) == "i"


def tap_choose_main(stack: TapStack, nodes) -> bool:
    """Trailing edge of Choose your preferred country — not the left i."""
    choose = None
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen() or skip_i(n):
            continue
        if collapsed_text(n.labels()).startswith("choose your preferred country"):
            choose = n
            break
    if choose is None:
        log("MISS Choose your preferred country")
        return False
    x = max(stack.screen_w - 62, 680)
    y = choose.center()[1]
    log(f"main Choose chevron ({x},{y}) skip i")
    stack.tap_xy(x, y)
    time.sleep(1.8)
    return True


def set_japan(stack: TapStack, phone: Phone, nodes):
    if find_label(nodes, ("Japan",)) and find_label_exact(nodes, ("Ghost Mode",)):
        log("Japan already on Home")
        return nodes, True
    country = find_label_exact(nodes, ("Preferred Country",))
    if country is None:
        log("MISS Preferred Country")
        return nodes, False
    stack.tap_xy(max(stack.screen_w - 62, 680), country.center()[1])
    time.sleep(1.8)
    nodes = dump(stack, phone, "351_sheet")
    if is_info_sheet(nodes):
        if not tap_choose_main(stack, nodes):
            return nodes, False
        nodes = dump(stack, phone, "352_after_choose")
    if is_info_sheet(nodes):
        log("STOP still on info sheet — will not swipe")
        return nodes, False
    if is_about(nodes):
        log("STOP on about/reset — will not swipe")
        return nodes, False
    n = find_label(nodes, ("Japan",))
    for i in range(8):
        if n is not None:
            break
        if not is_country_list(nodes):
            log("STOP not a country list")
            return nodes, False
        log(f"country list swipe {i}")
        stack.swipe(375, 1000, 375, 720)
        time.sleep(0.9)
        nodes = dump(stack, phone, f"353_list_{i}")
        if is_about(nodes) or is_info_sheet(nodes):
            log("STOP swipe left country list")
            return nodes, False
        n = find_label(nodes, ("Japan",))
    if n is None:
        log("MISS Japan")
        return nodes, False
    stack.tap_row(n)
    time.sleep(1.8)
    nodes = dump(stack, phone, "354_japan")
    return nodes, find_label(nodes, ("Japan",)) is not None


def reddit_rest(stack: TapStack, phone: Phone, nodes):
    if find_label_exact(nodes, ("Ghost Mode",)) and find_label_exact(nodes, ("Application List",)):
        stack.tap_row(find_label_exact(nodes, ("Application List",)))
        time.sleep(1.8)
        nodes = dump(stack, phone, "360_apps")
    if find_label_exact(nodes, ("User Apps",)):
        stack.tap_xy(*find_label_exact(nodes, ("User Apps",)).center())
        time.sleep(1.2)
        nodes = dump(stack, phone, "361_user")
    reddit = find_label_exact(nodes, ("Reddit",))
    if reddit is None:
        log("MISS Reddit")
        return nodes, False
    stack.tap_row(reddit)
    time.sleep(1.8)
    nodes = dump(stack, phone, "362_reddit")
    enable = find_label(nodes, ("Enable Ghost for Reddit", "Enable"))
    if enable is None:
        log("MISS Enable")
        return nodes, False
    stack.tap_switch(enable)
    time.sleep(1.2)
    nodes = dump(stack, phone, "363_enable")
    adv = find_label_exact(nodes, ("Advanced",))
    if adv is not None:
        stack.tap_xy(*adv.center())
        time.sleep(1.6)
        nodes = dump(stack, phone, "364_advanced")
    crane = find_label(nodes, ("Hide", "Crane paths", "Crane"))
    if crane is not None:
        stack.tap_switch(crane)
        time.sleep(1.0)
        nodes = dump(stack, phone, "365_crane")
    else:
        log("MISS Crane hide")
    spoof = find_label_exact(nodes, ("Spoofer",))
    if spoof is not None:
        stack.tap_xy(*spoof.center())
        time.sleep(1.6)
        nodes = dump(stack, phone, "366_spoofer")
    gen = find_label(nodes, ("Formulate", "Generate"))
    if gen is None:
        log("MISS Generate")
        return nodes, False
    stack.tap_row(gen)
    time.sleep(2.2)
    nodes = dump(stack, phone, "367_generate")
    return nodes, True


def main() -> int:
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            print("GHOST_GO=NO_TAP")
            return 1
        phone.exec_ok(f"uiopen --bundleid {GHOST_BUNDLE}", timeout=10)
        time.sleep(2.0)
        nodes = dump(stack, phone, "350_now")
        if is_about(nodes):
            stack.swipe(375, 420, 375, 980)
            time.sleep(1.2)
            nodes = dump(stack, phone, "350_home")
        local = find_label_exact(nodes, ("Local",))
        if local is not None:
            stack.tap_xy(*local.center())
            time.sleep(0.5)
        nodes, ok_jp = set_japan(stack, phone, nodes)
        if not ok_jp:
            print("GHOST_GO=NO_JAPAN")
            return 1
        nodes, ok = reddit_rest(stack, phone, nodes)
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "ghost_apply.txt"), "w", encoding="utf-8") as f:
            f.write(f"japan={ok_jp} reddit={ok}\n")
        print("GHOST_GO=" + ("OK" if ok else "PARTIAL"))
        return 0 if ok else 1
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
