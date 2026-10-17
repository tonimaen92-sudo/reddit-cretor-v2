# UTF-8
"""Reset Ghost, then apply the cataloged Reddit recipe from live dumps.

Local, Japan, Reddit Enable, Hide Crane paths, Generate Default.
No Ghost.dylib. No Reddit signup. No Misc/Battery/Screen/Network/Cellular/Locale/TimeZone.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import GHOST_BUNDLE, LOG_DIR  # noqa: E402
from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import (  # noqa: E402
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
    if dismiss_alert(stack, nodes):
        _, nodes = dump_named(stack, phone, stem + "_alert", log)
    return nodes


def dismiss_alert(stack: TapStack, nodes) -> bool:
    err = find_label_exact(nodes, ("Runtime Error",))
    btn = find_label_exact(nodes, ("Dismiss",))
    if err is None and btn is None:
        return False
    target = btn or err
    log(f"ALERT {target.labels()!r}")
    stack.tap_xy(*target.center())
    time.sleep(1.2)
    return True


def title_of(nodes) -> str:
    best, best_y = "", 9999
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if not (50 <= n.y <= 115):
            continue
        blob = collapsed_text(n.labels())
        if blob in ("x", "settings") or blob.startswith("no sim"):
            continue
        if n.y < best_y:
            best_y, best = n.y, blob
    return best


def tap_x(stack: TapStack, nodes) -> bool:
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) == "x" and n.y < 120:
            stack.tap_xy(*n.center())
            time.sleep(1.5)
            return True
    return False


def chevron(stack: TapStack, n) -> None:
    stack.tap_xy(max(stack.screen_w - 62, 680), n.center()[1])
    time.sleep(1.8)


def relaunch(phone: Phone) -> None:
    phone.exec_ok("killall Ghost 2>/dev/null; killall -9 Ghost 2>/dev/null", timeout=8)
    time.sleep(1.0)
    phone.exec_ok(f"uiopen --bundleid {GHOST_BUNDLE}", timeout=10)
    time.sleep(2.5)


def to_home(stack: TapStack, phone: Phone, nodes, stem: str):
    for i in range(8):
        title = title_of(nodes)
        log(f"nav {title!r}")
        okay = find_label_exact(nodes, ("Okay",))
        if okay is not None:
            stack.tap_xy(*okay.center())
            time.sleep(1.5)
            nodes = dump(stack, phone, f"{stem}_okay_{i}")
            continue
        if title == "tutorial":
            if not tap_x(stack, nodes):
                stack.tap_xy(674, 85)
                time.sleep(1.5)
            nodes = dump(stack, phone, f"{stem}_tut_{i}")
            continue
        if find_label_exact(nodes, ("Ghost Mode", "Local")) and title == "home":
            return nodes
        if find_label_exact(nodes, ("Changelog", "Respring", "Safemode", "Erase")):
            stack.swipe(375, 420, 375, 980)
            time.sleep(1.2)
            nodes = dump(stack, phone, f"{stem}_from_about_{i}")
            continue
        if tap_x(stack, nodes):
            nodes = dump(stack, phone, f"{stem}_x_{i}")
            continue
        break
    return nodes


def reset_ghost(stack: TapStack, phone: Phone, nodes):
    nodes = to_home(stack, phone, nodes, "310")
    if not find_label_exact(nodes, ("Ghost Mode",)):
        log("MISS home before reset")
        return nodes, False
    log("page to Ghost Reset")
    stack.swipe(375, 1050, 375, 620)
    time.sleep(1.2)
    nodes = dump(stack, phone, "311_about")
    erase = find_label(nodes, ("Erase", "All Ghost contents & settings to default"))
    if erase is None:
        log("MISS Erase")
        return nodes, False
    stack.tap_row(erase)
    time.sleep(1.8)
    nodes = dump(stack, phone, "312_erase")
    if find_label_exact(nodes, ("Changelog",)) and find_label_exact(nodes, ("Erase",)):
        chevron(stack, erase)
        nodes = dump(stack, phone, "312_erase2")
    sure = find_label(nodes, ("Yes, I'm sure!", "Yes, I'm sure", "I'm sure"))
    if sure is not None:
        stack.tap_xy(*sure.center())
        time.sleep(2.2)
        nodes = dump(stack, phone, "313_erased")
    okay = find_label_exact(nodes, ("Okay", "OK"))
    if okay is not None:
        stack.tap_xy(*okay.center())
        time.sleep(1.8)
        nodes = dump(stack, phone, "313_okay")
    nodes = to_home(stack, phone, nodes, "314")
    return nodes, True


def set_japan(stack: TapStack, phone: Phone, nodes):
    country = find_label_exact(nodes, ("Preferred Country",))
    if country is None:
        log("MISS Preferred Country")
        return nodes, False
    chevron(stack, country)
    nodes = dump(stack, phone, "320_country_sheet")
    choose = find_label(nodes, ("Choose your preferred country", "Choose a Country"))
    if choose is not None and find_label(nodes, ("Detail description", "When you generate random data")):
        stack.tap_row(choose)
        time.sleep(1.8)
        nodes = dump(stack, phone, "321_country_list")
    n = find_label(nodes, ("Japan",))
    for i in range(10):
        if n is not None:
            break
        log(f"country swipe {i}")
        stack.swipe(375, 1000, 375, 720)
        time.sleep(0.9)
        nodes = dump(stack, phone, f"322_country_{i}")
        if dismiss_alert(stack, nodes):
            nodes = dump(stack, phone, "322_country_alert")
            break
        n = find_label(nodes, ("Japan",))
    if n is None:
        log("MISS Japan")
        return nodes, False
    stack.tap_row(n)
    time.sleep(1.8)
    nodes = dump(stack, phone, "323_japan")
    nodes = to_home(stack, phone, nodes, "324")
    ok = find_label(nodes, ("Japan",)) is not None
    log("Japan on home" if ok else "Japan not visible on home")
    return nodes, ok


def reddit_enable_crane_generate(stack: TapStack, phone: Phone, nodes):
    if not find_label_exact(nodes, ("Application List",)):
        log("MISS Application List")
        return nodes, False
    stack.tap_row(find_label_exact(nodes, ("Application List",)))
    time.sleep(1.8)
    nodes = dump(stack, phone, "330_apps")
    if find_label_exact(nodes, ("User Apps",)):
        stack.tap_xy(*find_label_exact(nodes, ("User Apps",)).center())
        time.sleep(1.2)
        nodes = dump(stack, phone, "331_user")
    reddit = find_label_exact(nodes, ("Reddit",))
    if reddit is None:
        log("MISS Reddit")
        return nodes, False
    stack.tap_row(reddit)
    time.sleep(1.8)
    nodes = dump(stack, phone, "332_reddit")
    enable = find_label(nodes, ("Enable Ghost for Reddit", "Enable"))
    if enable is None:
        log("MISS Enable")
        return nodes, False
    stack.tap_switch(enable)
    time.sleep(1.2)
    nodes = dump(stack, phone, "333_enable")
    adv = find_label_exact(nodes, ("Advanced",))
    if adv is not None:
        stack.tap_xy(*adv.center())
        time.sleep(1.6)
        nodes = dump(stack, phone, "334_advanced")
    crane = find_label(nodes, ("Hide", "Crane", "Crane paths"))
    if crane is None:
        stack.swipe(375, 980, 375, 720)
        time.sleep(0.8)
        nodes = dump(stack, phone, "334_advanced2")
        crane = find_label(nodes, ("Hide", "Crane", "Crane paths"))
    if crane is not None:
        stack.tap_switch(crane)
        time.sleep(1.0)
        nodes = dump(stack, phone, "335_crane")
    else:
        log("MISS Crane hide")
    spoof = find_label_exact(nodes, ("Spoofer",))
    if spoof is not None:
        stack.tap_xy(*spoof.center())
        time.sleep(1.6)
        nodes = dump(stack, phone, "336_spoofer")
    gen = find_label(nodes, ("Formulate", "Generate"))
    if gen is None:
        log("MISS Generate")
        return nodes, False
    stack.tap_row(gen)
    time.sleep(2.2)
    nodes = dump(stack, phone, "337_generate")
    extra = find_label_exact(nodes, ("OK", "Confirm", "Done", "Continue"))
    if extra is not None:
        stack.tap_xy(*extra.center())
        time.sleep(1.5)
        nodes = dump(stack, phone, "338_generate_ok")
    return nodes, True


def main() -> int:
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            print("GHOST_APPLY=NO_TAP")
            return 1
        try:
            stack._xxt_post("/recycle", b"", timeout=5)
        except Exception:
            pass
        skip_reset = os.environ.get("GHOST_SKIP_RESET", "").strip() == "1"
        if skip_reset:
            phone.exec_ok(f"uiopen --bundleid {GHOST_BUNDLE}", timeout=10)
            time.sleep(2.5)
        else:
            relaunch(phone)
        nodes = dump(stack, phone, "310_now")
        if find_label_exact(nodes, ("Okay",)) and find_label(nodes, ("respring", "A respring is required")):
            stack.tap_xy(*find_label_exact(nodes, ("Okay",)).center())
            time.sleep(1.8)
            nodes = dump(stack, phone, "310_okay")
        sure = find_label(nodes, ("Yes, I'm sure!", "Yes, I'm sure"))
        if sure is not None:
            log("confirm still up from last run")
            stack.tap_xy(*sure.center())
            time.sleep(2.2)
            nodes = dump(stack, phone, "310_after_confirm")
        if skip_reset:
            did_reset = True
            nodes = to_home(stack, phone, nodes, "314")
        else:
            nodes, did_reset = reset_ghost(stack, phone, nodes)
            if not did_reset:
                print("GHOST_APPLY=NO_RESET")
                return 1
        local = find_label_exact(nodes, ("Local",))
        if local is not None:
            stack.tap_xy(*local.center())
            time.sleep(0.6)
            nodes = dump(stack, phone, "319_local")
        nodes, ok_jp = set_japan(stack, phone, nodes)
        if not ok_jp:
            print("GHOST_APPLY=NO_JAPAN")
            return 1
        nodes, ok_reddit = reddit_enable_crane_generate(stack, phone, nodes)
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "ghost_apply.txt"), "w", encoding="utf-8") as f:
            f.write(f"reset={did_reset} japan={ok_jp} reddit={ok_reddit}\n")
        print("GHOST_APPLY=" + ("OK" if ok_reddit else "PARTIAL"))
        return 0 if ok_reddit else 1
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
