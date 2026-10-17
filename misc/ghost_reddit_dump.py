# UTF-8
"""Ghost Reddit Spoofer/Advanced/Misc dump + Ghost.app UI strings. No dylib. No Generate."""

from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DUMP_DIR, GHOST_BUNDLE, LOG_DIR  # noqa: E402
from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def extract_strings(blob: bytes) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for pat in (
        re.compile(rb"[\x20-\x7e]{6,80}"),
        re.compile(rb"(?:[\x20-\x7e]\x00){6,80}"),
    ):
        for m in pat.finditer(blob):
            raw = m.group()
            if b"\x00" in raw:
                s = raw.decode("utf-16le", errors="ignore")
            else:
                s = raw.decode("ascii", errors="ignore")
            s = s.strip()
            if s.lower() in seen or len(s) < 4:
                continue
            if re.search(
                r"Spoof|Enable|Device|IDFV|IDFA|IME|UDID|Serial|Carrier|Wi-?Fi|Cell|"
                r"Pasteboard|Appium|Album|Crane|Country|Generate|Formulate|Local|"
                r"Global|Storage|Model|Battery|Brightness|Disk|RAM|Locale|Time|"
                r"Language|VPN|Bluetooth|Adver|Identifier|Jailbreak|Sandbox|MAC|"
                r"Network|Hostname|Kernel|Fingerprint|DCDevice|DeviceCheck|"
                r"Hardware|Region|Keyboard|User-Agent|WebKit|Container|Misc|"
                r"Advanced|Spoofer|Clipboard|Photo|IDFA|ASIdent|Unique|Boot|"
                r"Capacity|IP |ssid|BSSID|IMEI|meid|ECID|Serial",
                s,
                re.I,
            ):
                seen.add(s.lower())
                found.append(s)
    return found[:300]


def labels(nodes) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen() or n.y < 50:
            continue
        blob = collapsed_text(n.labels())
        if not blob or blob in seen or len(blob) > 90:
            continue
        seen.add(blob)
        out.append(blob)
    return out


def tap_named(stack: TapStack, nodes, names: tuple[str, ...], *, row: bool = True) -> bool:
    n = find_label_exact(nodes, names)
    if n is None or not stack.can_tap():
        log(f"MISS {names}")
        return False
    if row and n.center()[1] < 1180:
        stack.tap_row(n)
    else:
        stack.tap_xy(*n.center())
    time.sleep(1.8)
    return True


def close_tutorial(stack: TapStack, nodes) -> list:
    title = find_label_exact(nodes, ("Tutorial",))
    xbtn = None
    for n in nodes:
        if collapsed_text(n.labels()) == "x" and n.on_screen() and n.y < 120:
            xbtn = n
            break
    if title and xbtn:
        log(f"Closing tutorial X {xbtn.center()}")
        stack.tap_xy(*xbtn.center())
        time.sleep(1.5)
        return dump_named(stack, phone_holder[0], "220_ghost_after_tutorial", log)[1]
    return nodes


phone_holder: list = []


def main() -> int:
    os.environ["SKIP_SHOT"] = "1"
    phone = Phone(log=log)
    phone_holder.append(phone)
    pages: dict[str, list[str]] = {}
    try:
        phone.connect("", also_wda=False)
        os.makedirs(DUMP_DIR, exist_ok=True)
        try:
            raw = phone.sftp_read_bytes("/var/jb/Applications/Ghost.app/Ghost")
            strs = extract_strings(raw)
            path = os.path.join(DUMP_DIR, "ghost_ui_strings.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(strs) + "\n")
            log(f"UI strings {len(strs)} -> {path}")
            pages["ui_strings"] = strs
        except Exception as e:
            log(f"strings skip: {e}")

        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        try:
            stack._xxt_post("/recycle", b"", timeout=5)
        except Exception:
            pass
        phone.exec_ok(f"uiopen --bundleid {GHOST_BUNDLE}", timeout=10)
        time.sleep(2.5)
        blob, nodes = dump_named(stack, phone, "220_ghost_now", log)
        nodes = close_tutorial(stack, nodes)

        if not tap_named(stack, nodes, ("Application List",)):
            print("GHOST_DUMP=NO_APPS")
            return 1
        blob, nodes = dump_named(stack, phone, "221_ghost_apps", log)
        tap_named(stack, nodes, ("User Apps",), row=False)
        blob, nodes = dump_named(stack, phone, "222_ghost_user", log)
        if not tap_named(stack, nodes, ("Reddit",)):
            print("GHOST_DUMP=NO_REDDIT")
            return 1

        blob, nodes = dump_named(stack, phone, "230_spoofer", log)
        pages["spoofer"] = labels(nodes)
        log("SPOOFER " + " | ".join(pages["spoofer"]))

        if tap_named(stack, nodes, ("Advanced",), row=False):
            blob, nodes = dump_named(stack, phone, "231_advanced", log)
            pages["advanced"] = labels(nodes)
            log("ADVANCED " + " | ".join(pages["advanced"]))
        if tap_named(stack, nodes, ("Misc",), row=False):
            blob, nodes = dump_named(stack, phone, "232_misc", log)
            pages["misc"] = labels(nodes)
            log("MISC " + " | ".join(pages["misc"]))
        if tap_named(stack, nodes, ("Spoofer",), row=False):
            blob, nodes = dump_named(stack, phone, "233_spoofer2", log)
        if tap_named(stack, nodes, ("Default",)):
            blob, nodes = dump_named(stack, phone, "234_container", log)
            pages["container"] = labels(nodes)
            log("CONTAINER " + " | ".join(pages["container"]))

        os.makedirs(LOG_DIR, exist_ok=True)
        cat = os.path.join(LOG_DIR, "ghost_catalog.txt")
        with open(cat, "w", encoding="utf-8") as f:
            for name, labs in pages.items():
                f.write(f"=== {name} ===\n")
                for lab in labs:
                    f.write(lab + "\n")
                f.write("\n")
        print("WROTE", cat)
        print("GHOST_DUMP=OK")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
