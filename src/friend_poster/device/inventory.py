# UTF-8
"""SSH inventory: Crane/Shadow/Ghost, Reddit IPA, tap tools. No Reddit signup."""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from typing import Callable

from friend_poster.core.config import GHOST_APP, GHOST_DYLIB, REDDIT_BUNDLE
from friend_poster.device.ssh_device import Phone, PhoneError

LogFn = Callable[[str], None]

# Short steps. Never find(1) every Info.plist — that hangs as mobile on /var/containers.
STEPS: tuple[tuple[str, str, int], ...] = (
    ("id", "id; uname -a; ls -ld /var/jb /var/jb/usr/bin /cores/binpack 2>&1 | head -20", 15),
    (
        "ios version",
        "v=`sw_vers -productVersion 2>/dev/null`; "
        "[ -n \"$v\" ] || v=`plutil -extract ProductVersion raw "
        "/System/Library/CoreServices/SystemVersion.plist 2>/dev/null`; "
        "echo IOS_VERSION=$v",
        12,
    ),
    (
        "dpkg",
        "if command -v dpkg >/dev/null 2>&1; then dpkg -l 2>/dev/null | "
        "grep -Ei 'crane|shadow|ghost|choicy|libsandy|autotouch|xxtouch|zxtouch|"
        "simulatetouch|mobilesubstrate|ellekit|sileo|trollstore|openssh' || echo '(no dpkg rows)'; "
        "else echo 'dpkg not on PATH'; fi",
        25,
    ),
    (
        "jb apps/dylibs",
        "echo '--- Applications ---'; ls -1 /var/jb/Applications 2>&1 | head -80; "
        "echo '--- Substrate ---'; ls -1 /var/jb/Library/MobileSubstrate/DynamicLibraries 2>&1 | head -80; "
        "echo '--- TweakInject ---'; ls -1 /var/jb/usr/lib/TweakInject 2>&1 | head -40",
        20,
    ),
    (
        "ghost/crane/shadow paths",
        "ls -ld /var/jb/Applications/Ghost.app "
        "/var/jb/Library/MobileSubstrate/DynamicLibraries/Ghost.dylib "
        "/var/jb/Library/MobileSubstrate/DynamicLibraries/Ghost.plist "
        "/var/jb/Applications/Crane.app /var/jb/Applications/Shadow.app "
        "/var/jb/Library/MobileSubstrate/DynamicLibraries/Shadow.dylib "
        "/var/jb/Library/MobileSubstrate/DynamicLibraries/Shadow.dylib.plist "
        "2>&1",
        12,
    ),
    (
        "tap bins",
        "for c in uiopen open activator cycript python3 plutil dpkg sudo; do "
        "p=`command -v $c 2>/dev/null`; [ -n \"$p\" ] && echo BIN $c=$p; done",
        12,
    ),
    (
        "listen",
        "(netstat -an 2>/dev/null || true) | grep -E '\\.22 |\\.44 |\\.8100 |\\.6000 ' | head -20 || true",
        12,
    ),
    (
        "reddit app dir",
        "ls -d /var/containers/Bundle/Application/*/RedditApp.app "
        "/var/containers/Bundle/Application/*/Reddit.app "
        "/var/jb/Applications/RedditApp.app /var/jb/Applications/Reddit.app "
        "/Applications/RedditApp.app /Applications/Reddit.app 2>&1 | head -20",
        15,
    ),
    (
        "reddit prefs",
        f"echo '--- {REDDIT_BUNDLE} prefs ---'; "
        f"ls -d /var/mobile/Containers/Data/Application/*/Library/Preferences/{REDDIT_BUNDLE}.plist "
        f"/var/mobile/Library/Preferences/{REDDIT_BUNDLE}.plist "
        f"/var/mobile/Containers/Data/Application/*/Library/Caches/{REDDIT_BUNDLE} "
        "2>&1 | head -20; "
        f"for p in /var/mobile/Containers/Data/Application/*/Library/Preferences/{REDDIT_BUNDLE}.plist "
        f"/var/mobile/Library/Preferences/{REDDIT_BUNDLE}.plist; do "
        "[ -f \"$p\" ] && echo REDDIT_PREFS=$p; done; true",
        15,
    ),
)


@dataclass
class Inventory:
    raw: str = ""
    ios_version: str = ""
    crane: bool = False
    shadow: bool = False
    ghost_app: bool = False
    ghost_dylib: bool = False
    choicy: bool = False
    trollstore: bool = False
    reddit_found: bool = False
    reddit_version: str = ""
    reddit_min_ios: str = ""
    reddit_too_new: bool = False
    reddit_plist: str = ""
    tap_bins: list[str] = field(default_factory=list)
    wda_hint: bool = False
    autotouch: bool = False
    xxtouch: bool = False
    zxtouch: bool = False
    cycript: bool = False
    uiopen: bool = False
    notes: list[str] = field(default_factory=list)


def _plist_string(block: str, key: str) -> str:
    m = re.search(
        rf"<key>{re.escape(key)}</key>\s*<string>([^<]+)</string>",
        block,
        re.I,
    )
    return m.group(1).strip() if m else ""


def _version_tuple(s: str) -> tuple[int, ...]:
    parts = []
    for bit in re.split(r"[^\d]+", s or ""):
        if bit:
            parts.append(int(bit))
    return tuple(parts) or (0,)


def run_inventory(phone: Phone, log: LogFn | None = None) -> Inventory:
    chunks: list[str] = []
    for title, cmd, timeout in STEPS:
        if log:
            log(f"inventory: {title}…")
        try:
            code, out = phone.exec_ok(cmd, timeout=timeout)
        except (socket.timeout, TimeoutError, PhoneError) as e:
            out = f"(failed {title}: {e})\n"
            code = 1
        chunks.append(f"=== {title} (exit {code}) ===\n{out}")

    inv = Inventory()
    inv.raw = "\n".join(chunks)
    ios_match = re.search(r"^IOS_VERSION=(.*)$", inv.raw, re.M)
    if ios_match:
        inv.ios_version = ios_match.group(1).strip()
    inv.crane = _dpkg_ii(inv.raw, "crane") or _listed_name(inv.raw, "Crane.dylib")
    inv.shadow = _dpkg_ii(inv.raw, "shadow") or _listed_name(inv.raw, "Shadow.dylib")
    inv.ghost_app = _listed_name(inv.raw, "Ghost.app")
    inv.ghost_dylib = _listed_name(inv.raw, "Ghost.dylib")
    inv.choicy = _dpkg_ii(inv.raw, "choicy")
    inv.trollstore = _dpkg_ii(inv.raw, "trollstore") or _listed_name(inv.raw, "TrollStore.app")
    inv.wda_hint = _listed_name(inv.raw, "WebDriverAgent") or bool(
        re.search(r"(?m)^ii\s+\S*webdriver", inv.raw, re.I)
    )
    inv.autotouch = _dpkg_ii(inv.raw, "autotouch")
    inv.xxtouch = _dpkg_ii(inv.raw, "xxtouch")
    inv.zxtouch = _dpkg_ii(inv.raw, "zxtouch")
    inv.cycript = bool(re.search(r"^BIN cycript=", inv.raw, re.M))
    inv.uiopen = bool(re.search(r"^BIN (uiopen|open)=", inv.raw, re.M))
    for m in re.finditer(r"^BIN (\S+)=(\S+)", inv.raw, re.M):
        inv.tap_bins.append(f"{m.group(1)}={m.group(2)}")

    plist_match = re.search(r"^REDDIT_PLIST=(.*)$", inv.raw, re.M)
    if plist_match:
        inv.reddit_plist = plist_match.group(1).strip()
    if inv.reddit_plist and "no such file" not in inv.reddit_plist.lower():
        inv.reddit_found = True
        idx = inv.raw.find("REDDIT_PLIST=")
        chunk = inv.raw[idx : idx + 2500]
        inv.reddit_version = _plist_string(chunk, "CFBundleShortVersionString")
        inv.reddit_min_ios = _plist_string(chunk, "MinimumOSVersion")
        if (
            inv.reddit_min_ios
            and inv.ios_version
            and _version_tuple(inv.reddit_min_ios) > _version_tuple(inv.ios_version)
        ):
            inv.reddit_too_new = True
    elif re.search(r"^REDDIT_PREFS=", inv.raw, re.M):
        inv.reddit_found = True
        inv.notes.append(
            "Reddit data/prefs visible as mobile (App Store iOS 15 build). Bundle Info.plist not readable over this SSH."
        )
    elif re.search(r"(?m)^(?!.*no such file).*Reddit\.app", inv.raw, re.I):
        inv.notes.append("Reddit app path listed; Info.plist may be unreadable as mobile.")

    if inv.choicy:
        inv.notes.append("Choicy is installed but this phone does not use it. Scope with Shadow/Ghost/Crane only.")
    if not inv.shadow:
        inv.notes.append("Shadow is not installed yet. Fine for Hide My Email. Install before Reddit signup.")
    if not inv.ghost_app and not inv.ghost_dylib:
        inv.notes.append("Ghost is not installed yet. Fine for Hide My Email. Install before Reddit signup.")
    if not inv.reddit_found:
        inv.notes.append(
            f"Reddit ({REDDIT_BUNDLE}) not confirmed over SSH as mobile. "
            "If the icon is on the phone, a build compatible with this iOS version is enough for later signup. "
            "This slice still only does Hide My Email."
        )
    elif inv.reddit_too_new:
        inv.notes.append(
            f"Reddit MinimumOSVersion={inv.reddit_min_ios} (version {inv.reddit_version or '?'}). "
            f"Too new for iOS {inv.ios_version or '?'}. Signup later is blocked; Hide My Email still runs."
        )
    return inv


def _dpkg_ii(raw: str, token: str) -> bool:
    return bool(re.search(rf"(?m)^ii\s+\S*{re.escape(token)}", raw, re.I))


def _listed_name(raw: str, name: str) -> bool:
    needle = name.casefold()
    for line in raw.splitlines():
        s = line.strip()
        if not s or "no such file" in s.casefold():
            continue
        base = s.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if base.casefold() == needle or s.casefold() == needle:
            return True
    return False


def _line_for(raw: str, token: str) -> str:
    for line in raw.splitlines():
        if token.lower() in line.lower():
            return line.lower()
    return ""


def format_inventory(inv: Inventory) -> str:
    lines = [
        f"Crane: {'yes' if inv.crane else 'NO'}",
        f"Shadow: {'yes' if inv.shadow else 'NO (ok for this slice)'}",
        f"Ghost.app: {'yes' if inv.ghost_app else 'NO (ok for this slice)'}",
        f"Ghost.dylib: {'yes' if inv.ghost_dylib else 'NO (ok for this slice)'}",
        f"Choicy: unused ({'installed anyway' if inv.choicy else 'not installed'})",
        f"TrollStore: {'yes' if inv.trollstore else 'no'}",
        f"WDA hint: {'yes' if inv.wda_hint else 'no'}",
        f"uiopen/open: {'yes' if inv.uiopen else 'no'}",
        f"AutoTouch: {'yes' if inv.autotouch else 'no'}  XXTouch: {'yes' if inv.xxtouch else 'no'}  "
        f"ZXTouch: {'yes' if inv.zxtouch else 'no'}  cycript: {'yes' if inv.cycript else 'no'}",
        "tap bins: " + (", ".join(inv.tap_bins) if inv.tap_bins else "(none)"),
        f"iOS: {inv.ios_version or '?'}",
    ]
    if inv.reddit_found:
        lines.append(
            f"Reddit: YES version={inv.reddit_version or '?'} "
            f"MinimumOSVersion={inv.reddit_min_ios or '?'} plist={inv.reddit_plist}"
        )
        if inv.reddit_too_new:
            lines.append(f"Reddit IPA is too new for iOS {inv.ios_version or '?'}.")
    else:
        lines.append("Reddit: NOT CONFIRMED")
    for n in inv.notes:
        lines.append("NOTE: " + n)
    return "\n".join(lines)


def assert_first_slice_phone(inv: Inventory) -> None:
    """Hide My Email does not require Reddit, Shadow, or Ghost."""
    if inv.reddit_too_new:
        raise PhoneError(
            f"Stop: Reddit IPA MinimumOSVersion={inv.reddit_min_ios or '?'}, "
            f"but this iPhone is iOS {inv.ios_version or '?'}. "
            "Hide My Email will still run this slice, but signup is blocked."
        )
