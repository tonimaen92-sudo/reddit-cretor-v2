# UTF-8
"""Dump every Ghost app pane. No Ghost.dylib. No Generate. No toggle flips."""

from __future__ import annotations

import os
import sys
import time
from friend_poster.core.config import DUMP_DIR, GHOST_BUNDLE, LOG_DIR  # noqa: E402
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def labels(nodes) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if n.y < 50:
            continue
        blob = collapsed_text(n.labels())
        if not blob or blob in seen or len(blob) > 80:
            continue
        seen.add(blob)
        out.append(blob)
    return out


def dismiss_if_alert(stack: TapStack, nodes) -> bool:
    btn = find_label_exact(nodes, ("Dismiss", "OK", "Close"))
    err = find_label_exact(nodes, ("Runtime Error",))
    if btn is None and err is None:
        return False
    target = btn or err
    log(f"ALERT {target.labels()!r} {target.center()} — dismissing, no more swipes")
    stack.tap_xy(*target.center())
    time.sleep(1.0)
    return True


def tap_named(stack: TapStack, nodes, names: tuple[str, ...], *, row: bool = True) -> bool:
    n = find_label_exact(nodes, names)
    if n is None or not stack.can_tap():
        log(f"MISS {names}")
        return False
    if row:
        stack.tap_row(n)
    else:
        cx, cy = n.center()
        # Bottom tabs: keep the dump x so Spoofer/Advanced/Misc do not collide.
        if cy > 1180:
            stack.tap_xy(*n.center())
        else:
            stack.tap_row(n)
    time.sleep(1.8)
    return True


def tap_back(stack: TapStack, nodes) -> None:
    cands = [n for n in nodes if n.on_screen() and n.ntype != "ocr-line" and 50 <= n.y <= 110]
    if cands:
        back = min(cands, key=lambda n: n.x)
        stack.tap_xy(min(back.x + 12, 160), back.center()[1])
    else:
        stack.tap_xy(140, 75)
    time.sleep(1.5)


def pull_app_strings(phone: Phone) -> str:
    code, out = phone.exec_ok(
        "echo '-- ghost app ls --'; ls -la /var/jb/Applications/Ghost.app; "
        "echo '-- lproj --'; ls -la /var/jb/Applications/Ghost.app/Base.lproj "
        "/var/jb/Applications/Ghost.app/*.lproj 2>&1 | head -40; "
        "echo '-- strings --'; "
        "strings /var/jb/Applications/Ghost.app/Ghost 2>/dev/null | "
        "grep -E 'Spoof|Enable|Disable|Device|IDFV|IDFA|IMEI|UDID|Serial|Carrier|"
        "Wi-?Fi|Cellular|Pasteboard|Appium|Album|Crane|Country|Generate|Formulate|"
        "Local|Global|Storage|Model|Battery|Brightness|Disk|RAM|Locale|Timezone|"
        "Language|VPN|Bluetooth|Advertising|Identifier|Jailbreak|Sandbox|MAC|"
        "Network|IP Address|Hostname|Kernel|Boot|Unique|Fingerprint|DCDevice|"
        "DeviceCheck|Hardware|Capacity|Region|Keyboard|User Agent|WebKit' | "
        "grep -v '^[0-9]*$' | sort -u | head -250",
        timeout=25,
    )
    os.makedirs(DUMP_DIR, exist_ok=True)
    path = os.path.join(DUMP_DIR, "ghost_app_strings.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out or "")
    log(f"WROTE {path} ({len(out or '')} chars)")
    return out or ""


def dump_page(stack: TapStack, phone: Phone, stem: str) -> tuple[list, list[str]]:
    blob, nodes = dump_named(stack, phone, stem, log)
    if dismiss_if_alert(stack, nodes):
        blob, nodes = dump_named(stack, phone, stem + "_after_alert", log)
    labs = labels(nodes)
    for lab in labs:
        log(f"  [{stem}] {lab}")
    return nodes, labs


def main() -> int:
    phone = Phone(log=log)
    pages: dict[str, list[str]] = {}
    try:
        phone.connect("", also_wda=False)
        pages["app_strings"] = [pull_app_strings(phone)]
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        try:
            stack._xxt_post("/recycle", b"", timeout=5)
        except Exception:
            pass

        phone.exec_ok(f"uiopen --bundleid {GHOST_BUNDLE}", timeout=10)
        time.sleep(2.5)
        nodes, labs = dump_page(stack, phone, "200_ghost_home")
        pages["home"] = labs

        if tap_named(stack, nodes, ("Preferred Country",)):
            nodes, labs = dump_page(stack, phone, "201_ghost_country")
            pages["country"] = labs
            tap_back(stack, nodes)

        nodes, labs = dump_page(stack, phone, "202_ghost_home2")
        if tap_named(stack, nodes, ("Tutorial",)):
            nodes, labs = dump_page(stack, phone, "203_ghost_tutorial")
            pages["tutorial"] = labs
            tap_back(stack, nodes)

        nodes, labs = dump_page(stack, phone, "204_ghost_home3")
        if not tap_named(stack, nodes, ("Application List",)):
            print("GHOST_CATALOG=NO_APPS")
            return 1
        nodes, labs = dump_page(stack, phone, "205_ghost_apps")
        tap_named(stack, nodes, ("User Apps",), row=False)
        nodes, labs = dump_page(stack, phone, "206_ghost_user")
        if not tap_named(stack, nodes, ("Reddit",)):
            print("GHOST_CATALOG=NO_REDDIT")
            return 1

        nodes, labs = dump_page(stack, phone, "210_ghost_reddit_spoofer")
        pages["reddit_spoofer"] = labs

        if tap_named(stack, nodes, ("Advanced",), row=False):
            nodes, labs = dump_page(stack, phone, "211_ghost_reddit_advanced")
            pages["reddit_advanced"] = labs
        if tap_named(stack, nodes, ("Misc",), row=False):
            nodes, labs = dump_page(stack, phone, "212_ghost_reddit_misc")
            pages["reddit_misc"] = labs
        if tap_named(stack, nodes, ("Spoofer",), row=False):
            nodes, labs = dump_page(stack, phone, "213_ghost_reddit_spoofer2")
            pages["reddit_spoofer2"] = labs

        if tap_named(stack, nodes, ("Default",)):
            nodes, labs = dump_page(stack, phone, "214_ghost_container")
            pages["reddit_container"] = labs
            tap_back(stack, nodes)

        os.makedirs(LOG_DIR, exist_ok=True)
        cat = os.path.join(LOG_DIR, "ghost_catalog.txt")
        lines = ["Ghost 1.5 live catalog. Generate was not tapped. Toggles not flipped.", ""]
        for name, labs in pages.items():
            lines.append(f"=== {name} ===")
            for lab in labs:
                lines.append("  " + lab)
            lines.append("")
        text = "\n".join(lines) + "\n"
        with open(cat, "w", encoding="utf-8") as f:
            f.write(text)
        print(text)
        print("WROTE", cat)
        print("GHOST_CATALOG=OK")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
