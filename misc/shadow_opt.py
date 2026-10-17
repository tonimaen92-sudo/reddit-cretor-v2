# UTF-8
"""Read every Shadow pane, then apply a Reddit-only profile. No Ghost. No Reddit signup."""

from __future__ import annotations

import os
import plistlib
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DUMP_DIR, LOG_DIR, SHADOW_SETTINGS_URL  # noqa: E402
from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import (  # noqa: E402
    TapStack,
    collapsed_text,
    dump_named,
    find_label_exact,
)

BUNDLE = "/var/jb/Library/PreferenceBundles/ShadowSettings.bundle"
PREFS = (
    "/var/jb/var/mobile/Library/Preferences/me.jjolano.shadow.plist",
    "/var/mobile/Library/Preferences/me.jjolano.shadow.plist",
)

# Drill-down rows (open the page). Everything else on Bypass/Reddit is a switch.
DRILL = ("bypass settings", "applications", "hooking library", "restart springboard")

# Want ON for Reddit / global bypass profile.
WANT_ON = (
    "enable shadow",
    "file system",
    "dynamic libraries",
    "url handlers",
    "environment variables",
    "detection frameworks",
    "foundation framework",
    "uikit framework",
    "mach services",
    "objc runtime",
    "dyld",
    "loaded libraries",
)

# Want OFF. DeviceCheck extra fights Ghost's client DCDevice spoof.
WANT_OFF = (
    "always-on",
    "device check",
    "devicecheck",
    "anti debugging",
    "anti-debugging",
    "low-level c",
    "symbolic lookups",
    "syscall",
    "sysctl",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def pull_plist(phone: Phone, remote: str) -> object | None:
    try:
        data = phone.sftp_read_bytes(remote)
    except Exception as e:
        log(f"MISS {remote} ({e})")
        return None
    local = os.path.join(DUMP_DIR, remote.replace("/", "_").strip("_") + ".txt")
    os.makedirs(DUMP_DIR, exist_ok=True)
    try:
        parsed = plistlib.loads(data)
        text = repr(parsed)
    except Exception:
        parsed = None
        text = data.decode("utf-8", errors="replace")[:8000]
    with open(local, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    log(f"OK {remote} -> {os.path.basename(local)} ({len(text)} chars)")
    return parsed


def specifier_defaults(obj: object) -> dict[str, object]:
    out: dict[str, object] = {}
    if isinstance(obj, dict):
        items = obj.get("items") or obj.get("Items")
        if isinstance(items, list):
            for it in items:
                out.update(specifier_defaults(it))
        else:
            label = collapsed_text(str(obj.get("label") or obj.get("Label") or ""))
            default = obj.get("default", obj.get("DefaultValue", obj.get("defaultValue")))
            if label:
                out[label] = default
    elif isinstance(obj, list):
        for it in obj:
            out.update(specifier_defaults(it))
    return out


def specifier_lines(obj: object, prefix: str = "") -> list[str]:
    lines: list[str] = []
    if isinstance(obj, dict):
        items = obj.get("items") or obj.get("Items")
        if isinstance(items, list):
            for it in items:
                lines.extend(specifier_lines(it, prefix))
        else:
            label = obj.get("label") or obj.get("Label") or ""
            key = obj.get("id") or obj.get("key") or obj.get("Key") or ""
            cell = obj.get("cell") or obj.get("Cell") or ""
            default = obj.get("default") if "default" in obj else obj.get("DefaultValue")
            footer = obj.get("footerText") or obj.get("FooterText") or ""
            if label or key:
                lines.append(
                    f"{prefix}{cell} label={label!r} id={key!r} default={default!r} footer={footer!r}"
                )
    elif isinstance(obj, list):
        for it in obj:
            lines.extend(specifier_lines(it, prefix))
    return lines


def dump_scroll(stack: TapStack, phone: Phone, stem: str, swipes: int) -> list:
    blob, nodes = dump_named(stack, phone, f"{stem}_0", log)
    all_nodes = list(nodes)
    btn = find_label_exact(nodes, ("Dismiss",))
    if btn:
        log(f"Dismissing alert {btn.center()}")
        stack.tap_xy(*btn.center())
        time.sleep(1.0)
        blob, nodes = dump_named(stack, phone, f"{stem}_0b", log)
        all_nodes = list(nodes)
    for i in range(1, swipes + 1):
        stack.swipe_up()
        blob, nodes = dump_named(stack, phone, f"{stem}_{i}", log)
        btn = find_label_exact(nodes, ("Dismiss",))
        if btn:
            log(f"Dismissing alert {btn.center()}")
            stack.tap_xy(*btn.center())
            time.sleep(1.0)
            continue
        all_nodes.extend(nodes)
    return all_nodes


def open_shadow(stack: TapStack, phone: Phone):
    phone.exec_ok("uiopen '" + SHADOW_SETTINGS_URL.replace("'", "") + "'", timeout=10)
    time.sleep(2.2)
    return dump_named(stack, phone, "110_shadow_root", log)[1]


def tap_label_row(stack: TapStack, nodes, names: tuple[str, ...]) -> bool:
    n = find_label_exact(nodes, names)
    if n is None or not stack.can_tap():
        log(f"MISS row {names}")
        return False
    stack.tap_row(n)
    time.sleep(1.8)
    return True


def find_pref(phone: Phone) -> dict:
    for path in PREFS:
        try:
            data = phone.sftp_read_bytes(path)
        except Exception:
            continue
        try:
            parsed = plistlib.loads(data)
        except Exception:
            continue
        if isinstance(parsed, dict):
            log(f"PREFS {path} keys={list(parsed)[:40]}")
            return parsed
    log("PREFS none yet")
    return {}


def apply_switches(
    stack: TapStack,
    nodes,
    want_on: set[str],
    want_off: set[str],
    defaults: dict[str, object],
) -> None:
    """Tap trailing UISwitch for rows we care about. Skip drill-downs."""
    seen: set[str] = set()
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        blob = collapsed_text(n.labels())
        if blob in seen or blob in DRILL:
            continue
        seen.add(blob)
        desired = True if blob in want_on else False if blob in want_off else None
        if desired is None:
            continue
        if blob == "enable shadow":
            log(f"SWITCH ON '{blob}' y={n.center()[1]} (per-app was off)")
            stack.tap_switch(n)
            time.sleep(0.7)
            continue
        cur = defaults.get(blob)
        cur_on = cur in (True, 1, "1", True)
        if cur is None:
            log(f"SKIP '{blob}' (no plist default; not flipping)")
            continue
        if bool(cur_on) == desired:
            log(f"KEEP '{blob}' already desired={desired} default={cur!r}")
            continue
        log(f"SWITCH {'ON' if desired else 'OFF'} '{blob}' y={n.center()[1]} default={cur!r}")
        stack.tap_switch(n)
        time.sleep(0.7)


def main() -> int:
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        os.makedirs(DUMP_DIR, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        catalog: list[str] = []
        defaults: dict[str, object] = {}
        for name in ("Root.plist", "App.plist", "Hooks.plist", "About.plist"):
            parsed = pull_plist(phone, f"{BUNDLE}/{name}")
            if parsed is not None:
                catalog.extend(specifier_lines(parsed, prefix=f"{name}: "))
                defaults.update(specifier_defaults(parsed))
        log(f"DEFAULTS {defaults}")
        for name in (
            "en.lproj/Root.strings",
            "en.lproj/App.strings",
            "en.lproj/Hooks.strings",
        ):
            pull_plist(phone, f"{BUNDLE}/{name}")
        log("SPECIFIERS")
        for line in catalog:
            log("  " + line)
        with open(os.path.join(LOG_DIR, "shadow_specifiers.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(catalog) + "\n")

        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        try:
            stack._xxt_post("/recycle", b"", timeout=5)
        except Exception:
            pass

        nodes = open_shadow(stack, phone)
        log("ROOT " + ", ".join(collapsed_text(n.labels()) for n in nodes if n.ntype != "ocr-line")[:800])

        if tap_label_row(stack, nodes, ("Bypass Settings",)):
            dump_scroll(stack, phone, "111_bypass", swipes=3)

        nodes = open_shadow(stack, phone)
        if tap_label_row(stack, nodes, ("Applications",)):
            blob, nodes = dump_named(stack, phone, "112_apps", log)
            if tap_label_row(stack, nodes, ("Reddit",)):
                dump_scroll(stack, phone, "113_reddit", swipes=4)

        prefs_before = find_pref(phone)
        log(f"BEFORE {prefs_before}")

        # Apply global: Always-On off. Leave Bypass defaults except we set them on Reddit.
        nodes = open_shadow(stack, phone)
        always = find_label_exact(nodes, ("Always-On",))
        if always:
            # Only tap if prefs say it is on. Missing key = Shadow default (off on this 3.7.6b1).
            val = prefs_before.get("Global_Enabled", prefs_before.get("Always-On"))
            log(f"Always-On pref={val!r}")
            if val is True or val == 1 or val == "1":
                stack.tap_switch(always)
                time.sleep(0.8)

        # Reddit per-app: Enable + essential + recommended.
        nodes = open_shadow(stack, phone)
        if not tap_label_row(stack, nodes, ("Applications",)):
            print("SHADOW_OPT=NO_APPS")
            return 1
        blob, nodes = dump_named(stack, phone, "114_apps_apply", log)
        if not tap_label_row(stack, nodes, ("Reddit",)):
            print("SHADOW_OPT=NO_REDDIT")
            return 1

        want_on = set(WANT_ON)
        want_off = set(WANT_OFF)
        done: set[str] = set()
        for swipe_i in range(0, 5):
            blob, nodes = dump_named(stack, phone, f"115_apply_{swipe_i}", log)
            remaining_on = want_on - done
            remaining_off = want_off - done
            apply_switches(stack, nodes, remaining_on, remaining_off, defaults)
            for n in nodes:
                blobt = collapsed_text(n.labels())
                if blobt in want_on or blobt in want_off:
                    done.add(blobt)
            if swipe_i < 4:
                stack.swipe_up()

        time.sleep(1.0)
        prefs_after = find_pref(phone)
        log(f"AFTER {prefs_after}")
        dump_named(stack, phone, "116_shadow_done", log)
        print("SHADOW_OPT=OK")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
