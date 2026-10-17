# UTF-8
"""Dump every Shadow Settings and Ghost app pane. Catalog only — do not flip toggles."""

from __future__ import annotations

import os
import sys
import time
from friend_poster.core.config import GHOST_BUNDLE, LOG_DIR, SHADOW_SETTINGS_URL  # noqa: E402
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import (  # noqa: E402
    Node,
    TapStack,
    collapsed_text,
    dump_named,
    find_label_exact,
)

SKIP = {
    "no sim",
    "no sim c",
    "no sim r",
    "no sim o",
    "settings",
    "shadow",
    "applications",
    "reddit",
    "home",
    "ghost",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def short_rows(nodes: list[Node]) -> list[Node]:
    out: list[Node] = []
    seen: set[str] = set()
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if n.y < 50 or n.y > 1280:
            continue
        blob = collapsed_text(n.labels())
        if not blob or blob in SKIP or blob in seen:
            continue
        if len(blob) > 70:
            continue
        if blob.replace(":", "").replace(".", "").isdigit():
            continue
        seen.add(blob)
        out.append(n)
    return out


def labels_of(nodes: list[Node]) -> list[str]:
    return [collapsed_text(n.labels()) for n in short_rows(nodes)]


def tap_named(stack: TapStack, nodes: list[Node], names: tuple[str, ...], *, row: bool = True) -> bool:
    n = find_label_exact(nodes, names)
    if n is None or not stack.can_tap():
        log(f"MISS {names}")
        return False
    if row:
        stack.tap_row(n)
    else:
        stack.tap_xy(*n.center())
    time.sleep(1.8)
    return True


def tap_back(stack: TapStack, nodes: list[Node]) -> None:
    cands = [
        n
        for n in nodes
        if n.on_screen() and n.ntype != "ocr-line" and 50 <= n.y <= 110
    ]
    if cands:
        back = min(cands, key=lambda n: n.x)
        stack.tap_xy(min(back.x + 12, 160), back.center()[1])
    else:
        stack.tap_xy(140, 75)
    time.sleep(1.6)


def dismiss_if_alert(stack: TapStack, nodes: list[Node]) -> None:
    btn = find_label_exact(nodes, ("Dismiss", "OK", "Close"))
    err = find_label_exact(nodes, ("Runtime Error",))
    if (btn is None and err is None) or not stack.can_tap():
        return
    target = btn or err
    log(f"Dismissing alert from dump: {target.labels()!r} {target.center()}")
    stack.tap_xy(*target.center())
    time.sleep(1.0)


def dump_scroll(stack: TapStack, phone: Phone, stem: str, extra_swipes: int = 2) -> list[str]:
    seen: list[str] = []
    blob, nodes = dump_named(stack, phone, f"{stem}_0", log)
    dismiss_if_alert(stack, nodes)
    for lab in labels_of(nodes):
        if lab not in seen:
            seen.append(lab)
            log(f"  [{stem}] {lab}")
    for i in range(1, extra_swipes + 1):
        stack.swipe_up()
        time.sleep(0.6)
        blob, nodes = dump_named(stack, phone, f"{stem}_{i}", log)
        dismiss_if_alert(stack, nodes)
        for lab in labels_of(nodes):
            if lab not in seen:
                seen.append(lab)
                log(f"  [{stem}] {lab}")
    return seen


def open_shadow(stack: TapStack, phone: Phone) -> list[Node]:
    phone.exec_ok("uiopen '" + SHADOW_SETTINGS_URL.replace("'", "") + "'", timeout=10)
    time.sleep(2.2)
    blob, nodes = dump_named(stack, phone, "70_shadow_root", log)
    return nodes


def open_shadow_reddit(stack: TapStack, phone: Phone) -> list[Node]:
    nodes = open_shadow(stack, phone)
    if not tap_named(stack, nodes, ("Applications",)):
        return []
    blob, nodes = dump_named(stack, phone, "71_shadow_apps", log)
    if not tap_named(stack, nodes, ("Reddit",)):
        return []
    blob, nodes = dump_named(stack, phone, "72_shadow_reddit", log)
    return nodes


def catalog_files(phone: Phone) -> str:
    code, out = phone.exec_ok(
        "echo '-- shadow prefloader --'; "
        "ls -la /var/jb/Library/PreferenceLoader/Preferences 2>&1 | grep -i shadow; "
        "echo '-- shadow bundles --'; "
        "ls -d /var/jb/Library/PreferenceBundles/Shadow* "
        "/var/jb/Library/PreferenceBundles/me.jjolano* 2>&1; "
        "echo '-- shadow bundle ls --'; "
        "ls -la /var/jb/Library/PreferenceBundles/ShadowSettings.bundle "
        "/var/jb/Library/PreferenceBundles/ShadowSettings.bundle/Resources 2>&1 | head -40; "
        "echo '-- shadow prefs --'; "
        "ls -l /var/jb/var/mobile/Library/Preferences/me.jjolano.shadow.plist "
        "/var/mobile/Library/Preferences/me.jjolano.shadow.plist 2>&1; "
        "echo '-- ghost app --'; "
        "ls -1 /var/jb/Applications/Ghost.app 2>&1 | head -40; "
        "echo '-- ghost strings files --'; "
        "ls -1 /var/jb/Applications/Ghost.app/*.lproj "
        "/var/jb/Applications/Ghost.app/*/*.strings 2>&1 | head -40; "
        "echo '-- ghost prefs --'; "
        "ls -l /var/jb/var/mobile/Library/Preferences/com.sgwc.ghost.plist "
        "/var/mobile/Library/Preferences/com.sgwc.ghost.plist "
        "/var/mobile/Library/Preferences/com.sgwc.Ghost.plist 2>&1; "
        "echo '-- shadow root plist --'; "
        "for p in "
        "/var/jb/Library/PreferenceBundles/ShadowSettings.bundle/Root.plist "
        "/var/jb/Library/PreferenceBundles/ShadowSettings.bundle/Resources/Root.plist "
        "/var/jb/Library/PreferenceBundles/ShadowSettings.bundle/Resources/App.plist; do "
        "echo FILE=$p; plutil -p \"$p\" 2>&1 | head -120; echo; done; "
        "echo '-- ghost ui strings --'; "
        "EXE=`ls -1 /var/jb/Applications/Ghost.app/Ghost /var/jb/Applications/Ghost.app/GhostApp 2>/dev/null | head -1`; "
        "echo EXE=$EXE; "
        "if [ -n \"$EXE\" ]; then strings \"$EXE\" 2>/dev/null | "
        "grep -E 'Spoof|Enable|Device|IDFV|IDFA|Country|Crane|Appium|Pasteboard|Album|Generate|Formulate|Local|Global|Carrier|WiFi|Cellular' | "
        "head -80; fi",
        timeout=25,
    )
    log(out)
    return out


def catalog_shadow(stack: TapStack, phone: Phone) -> dict[str, list[str]]:
    pages: dict[str, list[str]] = {}
    nodes = open_shadow(stack, phone)
    pages["root"] = dump_scroll(stack, phone, "73_shadow_root", extra_swipes=1)

    nodes = open_shadow(stack, phone)
    if tap_named(stack, nodes, ("Bypass Settings",)):
        pages["bypass"] = dump_scroll(stack, phone, "74_shadow_bypass", extra_swipes=3)
        blob, nodes = dump_named(stack, phone, "75_shadow_bypass_now", log)
        drill = [
            "File System",
            "Dynamic Libraries",
            "URL Handlers",
            "Environment Variables",
            "Hooking Library",
            "Mach Services",
            "ObjC Runtime",
            "Anti Debugging",
            "DeviceCheck",
            "Device Check",
            "Symbolic Lookups",
            "Low-Level C",
            "Sandbox",
            "Sysctl",
            "Foundation",
            "UIKit",
            "dyld",
            "Loaded Libraries",
            "Application Enumeration",
            "URL Schemes",
        ]
        seen_pages = set(pages["bypass"])
        for name in drill:
            if collapsed_text(name) not in {collapsed_text(x) for x in seen_pages}:
                continue
            blob, nodes = dump_named(stack, phone, "76_pre_" + name.replace(" ", "_").lower(), log)
            if tap_named(stack, nodes, (name,)):
                key = "bypass:" + name
                pages[key] = dump_scroll(stack, phone, "77_" + name.replace(" ", "_").lower(), extra_swipes=2)
                blob, nodes = dump_named(stack, phone, "78_back_" + name.replace(" ", "_").lower(), log)
                tap_back(stack, nodes)

    nodes = open_shadow_reddit(stack, phone)
    if nodes:
        pages["reddit"] = dump_scroll(stack, phone, "80_shadow_reddit", extra_swipes=4)
        blob, nodes = dump_named(stack, phone, "81_shadow_reddit_top", log)
        for name in (
            "Hooking Library",
            "File System",
            "Dynamic Libraries",
            "URL Handlers",
            "Environment Variables",
            "Mach Services",
            "ObjC Runtime",
            "Anti Debugging",
            "DeviceCheck",
            "Device Check",
            "Symbolic Lookups",
            "Sandbox",
            "Sysctl",
            "Foundation",
            "UIKit",
            "Application Enumeration",
        ):
            blob, nodes = dump_named(stack, phone, "82_pre_" + name.replace(" ", "_").lower(), log)
            hit = find_label_exact(nodes, (name,))
            if hit is None:
                stack.swipe_up()
                time.sleep(0.5)
                blob, nodes = dump_named(stack, phone, "83_pre2_" + name.replace(" ", "_").lower(), log)
                hit = find_label_exact(nodes, (name,))
            if hit is None:
                continue
            stack.tap_row(hit)
            time.sleep(1.8)
            pages["reddit:" + name] = dump_scroll(
                stack, phone, "84_" + name.replace(" ", "_").lower(), extra_swipes=2
            )
            blob, nodes = dump_named(stack, phone, "85_back_" + name.replace(" ", "_").lower(), log)
            tap_back(stack, nodes)
    return pages


def catalog_ghost(stack: TapStack, phone: Phone) -> dict[str, list[str]]:
    pages: dict[str, list[str]] = {}
    phone.exec_ok(f"uiopen --bundleid {GHOST_BUNDLE}", timeout=10)
    time.sleep(2.5)
    blob, nodes = dump_named(stack, phone, "90_ghost_home", log)
    pages["home"] = dump_scroll(stack, phone, "91_ghost_home", extra_swipes=1)

    blob, nodes = dump_named(stack, phone, "92_ghost_home_now", log)
    if tap_named(stack, nodes, ("Preferred Country",)):
        pages["country"] = dump_scroll(stack, phone, "93_ghost_country", extra_swipes=3)
        blob, nodes = dump_named(stack, phone, "94_ghost_country_now", log)
        tap_back(stack, nodes)

    blob, nodes = dump_named(stack, phone, "95_ghost_home2", log)
    if tap_named(stack, nodes, ("Tutorial",)):
        pages["tutorial"] = dump_scroll(stack, phone, "96_ghost_tutorial", extra_swipes=2)
        blob, nodes = dump_named(stack, phone, "97_ghost_tutorial_now", log)
        tap_back(stack, nodes)

    blob, nodes = dump_named(stack, phone, "98_ghost_home3", log)
    if tap_named(stack, nodes, ("Application List",)):
        blob, nodes = dump_named(stack, phone, "99_ghost_apps", log)
        tap_named(stack, nodes, ("User Apps",), row=False)
        blob, nodes = dump_named(stack, phone, "100_ghost_user", log)
        if tap_named(stack, nodes, ("Reddit",)):
            pages["reddit_spoofer"] = dump_scroll(stack, phone, "101_ghost_spoofer", extra_swipes=4)
            blob, nodes = dump_named(stack, phone, "102_ghost_tabs", log)
            if tap_named(stack, nodes, ("Advanced",), row=False):
                pages["reddit_advanced"] = dump_scroll(stack, phone, "103_ghost_advanced", extra_swipes=4)
            blob, nodes = dump_named(stack, phone, "104_ghost_tabs2", log)
            if tap_named(stack, nodes, ("Misc",), row=False):
                pages["reddit_misc"] = dump_scroll(stack, phone, "105_ghost_misc", extra_swipes=4)
            blob, nodes = dump_named(stack, phone, "106_ghost_reddit_now", log)
            if tap_named(stack, nodes, ("Default",)):
                pages["reddit_container"] = dump_scroll(stack, phone, "107_ghost_container", extra_swipes=2)
    return pages


def write_catalog(pages: dict[str, dict[str, list[str]]], files: str) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "tweak_catalog.txt")
    lines = ["Shadow + Ghost live catalog (dump-first). Toggles were not flipped.", ""]
    lines.append("=== files ===")
    lines.append(files.strip() or "(none)")
    lines.append("")
    for app, groups in pages.items():
        lines.append(f"=== {app} ===")
        for name, labs in groups.items():
            lines.append(f"[{name}]")
            for lab in labs:
                lines.append(f"  - {lab}")
            lines.append("")
    text = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log(f"WROTE {path}")
    return text


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        files = catalog_files(phone)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        pages: dict[str, dict[str, list[str]]] = {}
        if which in ("both", "shadow"):
            pages["shadow"] = catalog_shadow(stack, phone)
        if which in ("both", "ghost"):
            pages["ghost"] = catalog_ghost(stack, phone)
        print(write_catalog(pages, files))
        print("CATALOG_OK")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
