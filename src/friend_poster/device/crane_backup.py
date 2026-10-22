# UTF-8
"""Backup one Crane Reddit container to the PC. Dump-first. Do not delete.

Include Keychain must be on. Step 1 only: backup. Do not remove the container.

  python misc/crane_backup.py r1
"""

from __future__ import annotations

import os
import sys
import time
from typing import Callable
from friend_poster.core.config import CRANE_SETTINGS_URL, LOG_DIR  # noqa: E402
from friend_poster.core.slots import loose_index, name_to_index  # noqa: E402
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import (  # noqa: E402
    TapStack,
    collapsed_text,
    dump_named,
    find_label_exact,
)

os.environ["SKIP_SHOT"] = "1"

LogFn = Callable[[str], None]
BACKUP_DIR = os.path.join(LOG_DIR, "crane_backups")
# Crane row is "Backup Data to File" — not a lone "Backup".
BACKUP_ROW = (
    "Backup Data to File",
    "Backup data to file",
    "Backup Container",
    "Create Backup",
    "Export Container",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def must(nodes, names: tuple[str, ...]):
    n = find_label_exact(nodes, names)
    if n is None:
        raise PhoneError("not on screen: " + repr(names))
    return n


def dump(stack: TapStack, phone: Phone, stem: str, shot: bool = False):
    return dump_named(stack, phone, stem, log, shot=shot)


def visible_labels(nodes) -> str:
    seen: list[str] = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if t and t not in seen and len(t) < 80:
            seen.append(t)
    return " | ".join(seen[:40])


def flick_list(stack: TapStack) -> None:
    stack.interp_flick(375, 900, 375, 520, steps=16, ms=20)
    time.sleep(0.55)


def open_crane(stack: TapStack, phone: Phone):
    blob, nodes = dump(stack, phone, "710_now")
    log("NOW " + visible_labels(nodes))
    stack.open_url(CRANE_SETTINGS_URL)
    time.sleep(1.4)
    blob, nodes = dump(stack, phone, "711_crane")
    log("CRANE " + visible_labels(nodes))
    if find_label_exact(nodes, ("Applications",)) is None:
        raise PhoneError("Crane Applications not on screen. See 711_crane.")
    if find_label_exact(nodes, ("Ghost",)) and find_label_exact(nodes, ("Tutorial",)):
        raise PhoneError("Opened Ghost, not Crane. See 711_crane.")
    return nodes


def open_reddit_containers(stack: TapStack, phone: Phone, nodes):
    stack.tap_row(must(nodes, ("Applications",)))
    time.sleep(1.1)
    blob, nodes = dump(stack, phone, "712_apps")
    log("APPS " + visible_labels(nodes))
    reddit = find_label_exact(nodes, ("Reddit",))
    last = None
    stuck = 0
    for i in range(12):
        if reddit is not None:
            break
        log("Reddit off-screen -> scroll Crane app list")
        flick_list(stack)
        blob, nodes = dump(stack, phone, f"712_apps_{i}")
        ids = visible_labels(nodes)
        log("APPS " + ids)
        reddit = find_label_exact(nodes, ("Reddit",))
        stuck = stuck + 1 if ids == last else 0
        last = ids
        if stuck >= 3:
            break
    if reddit is None:
        raise PhoneError("Reddit not on Crane Applications list. See 712_apps dumps.")
    stack.tap_row(reddit)
    time.sleep(1.1)
    blob, nodes = dump(stack, phone, "713_reddit")
    log("REDDIT_CRANE " + visible_labels(nodes))
    if find_label_exact(nodes, ("Applications",)) is None and find_label_exact(
        nodes, ("CONTAINERS",)
    ) is None and find_label_exact(nodes, ("Add",)) is None:
        raise PhoneError("Not on Crane Reddit pane. See 713_reddit.")
    return nodes


def find_crane_name(nodes, name: str):
    """Crane container row. Tolerate OCR dropping zeros (a0001 read as a1)."""
    named = find_label_exact(nodes, (name, name.lower(), name.upper()))
    if named is not None:
        return named
    want = name_to_index(name)
    if want is None:
        return None
    best: tuple[bool, object] | None = None
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        # In Crane Edit mode the red remove control is OCR'd together with the
        # name as ``- a0103``.  Strip exactly one leading edit glyph before
        # matching; never strip arbitrary text or accept a partial slot name.
        candidate = t.lstrip()
        if candidate.startswith(("-", "−", "–", "—")):
            candidate = candidate[1:].strip()
        if candidate == collapsed_text(name):
            return n
        if len("".join(candidate.split())) < 2:
            continue
        if loose_index(candidate) != want:
            continue
        has_alpha = any(c.isalpha() for c in candidate)
        if best is None or (has_alpha and not best[0]) or (
            has_alpha == best[0] and n.y < best[1].y
        ):
            best = (has_alpha, n)
    if best is not None:
        log(f"{name} OCR read as {collapsed_text(best[1].labels())!r} -> tap that row")
        return best[1]
    return None


def tap_named_container(stack: TapStack, phone: Phone, nodes, name: str):
    named = find_crane_name(nodes, name)
    last = None
    stuck = 0
    for i in range(14):
        if named is not None:
            break
        log(f"{name} off-screen -> scroll Crane containers")
        flick_list(stack)
        blob, nodes = dump(stack, phone, f"714_list_{i}")
        ids = visible_labels(nodes)
        log("LIST " + ids)
        named = find_crane_name(nodes, name)
        stuck = stuck + 1 if ids == last else 0
        last = ids
        if stuck >= 3:
            break
    if named is None:
        raise PhoneError(f"Crane container {name} not on Reddit list. See 714_list dumps.")
    log(f"TAP Crane container {name}")
    stack.tap_row(named)
    time.sleep(1.0)
    blob, nodes = dump(stack, phone, "715_container")
    log("CONTAINER " + visible_labels(nodes))
    log("Scroll to BOTTOM of r1 page for Backup / Include Keychain")
    last = None
    stuck = 0
    for i in range(10):
        flick_list(stack)
        time.sleep(0.45)
        blob, nodes = dump(stack, phone, f"715_bottom_{i}")
        ids = visible_labels(nodes)
        log("BOTTOM " + ids)
        if find_label_exact(nodes, BACKUP_ROW) or find_label_exact(
            nodes, ("Include Keychain", "Include keychain", "Keychain")
        ):
            log("Options visible at bottom")
            break
        stuck = stuck + 1 if ids == last else 0
        last = ids
        if stuck >= 2:
            break
    return nodes


def turn_on_include_keychain(stack: TapStack, phone: Phone, nodes):
    """Settings UISwitch. Dump PNG. Do not guess ON/OFF from OCR."""
    key = find_label_exact(
        nodes,
        (
            "Include Keychain",
            "Include keychain",
            "Keychain",
        ),
    )
    if key is None:
        raise PhoneError("Include Keychain not on screen. See dump.")
    os.environ.pop("SKIP_SHOT", None)
    blob, nodes = dump(stack, phone, "716_keychain_before", shot=True)
    stack.tap_switch(key)
    time.sleep(0.45)
    blob, nodes = dump(stack, phone, "716_keychain_after", shot=True)
    os.environ["SKIP_SHOT"] = "1"
    log("Include Keychain tapped (PNG 716_keychain_before/after).")
    return nodes


def tap_backup(stack: TapStack, phone: Phone, nodes):
    btn = find_label_exact(nodes, BACKUP_ROW)
    last = None
    stuck = 0
    for i in range(8):
        if btn is not None:
            break
        log("Backup Data to File off-screen -> scroll container page")
        flick_list(stack)
        blob, nodes = dump(stack, phone, f"715_scroll_{i}")
        ids = visible_labels(nodes)
        log("CONTAINER " + ids)
        btn = find_label_exact(nodes, BACKUP_ROW)
        stuck = stuck + 1 if ids == last else 0
        last = ids
        if stuck >= 3:
            break
    if btn is None:
        raise PhoneError("Backup Data to File not on Crane container page. See 715 dumps.")
    log("TAP Backup Data to File")
    stack.tap_row(btn)
    time.sleep(1.1)
    blob, nodes = dump(stack, phone, "717_backup_sheet")
    log("SHEET " + visible_labels(nodes))
    return nodes


def tap_label_center(stack: TapStack, node, why: str) -> None:
    x, y = node.center()
    log(f"{why} center ({x},{y})")
    stack.tap_xy(x, y)


def dismiss_keychain_warning(stack: TapStack, phone: Phone, nodes):
    """Keychain ON fires a Warning. Always Ignore. Never Enable Encryption."""
    ign = find_label_exact(nodes, ("Ignore",))
    if ign is None:
        return nodes
    log("Warning -> Ignore")
    tap_label_center(stack, ign, "Ignore")
    time.sleep(0.7)
    blob, nodes = dump(stack, phone, "717_after_ignore")
    log("AFTER_IGNORE " + visible_labels(nodes))
    return nodes


def tap_create_backup(stack: TapStack, phone: Phone, nodes):
    go = find_label_exact(nodes, ("Create", "Done", "Save"))
    if go is None:
        raise PhoneError("Create not on New Backup sheet. See 717 dumps.")
    if collapsed_text(go.labels()) == "cancel":
        raise PhoneError("Refusing Cancel on New Backup.")
    log("TAP Create")
    tap_label_center(stack, go, "Create")
    time.sleep(2.2)
    blob, nodes = dump(stack, phone, "718_after_confirm")
    log("AFTER " + visible_labels(nodes))
    return nodes


def confirm_backup_sheet(stack: TapStack, phone: Phone, nodes):
    if find_label_exact(nodes, ("Ignore", "Warning")):
        nodes = dismiss_keychain_warning(stack, phone, nodes)
    elif find_label_exact(nodes, ("Include Keychain", "Include keychain", "Keychain")):
        nodes = turn_on_include_keychain(stack, phone, nodes)
        blob, nodes = dump(stack, phone, "717_after_keychain")
        log("SHEET2 " + visible_labels(nodes))
        nodes = dismiss_keychain_warning(stack, phone, nodes)
    return tap_create_backup(stack, phone, nodes)


def maybe_files_picker(stack: TapStack, phone: Phone, nodes):
    """Files: On My iPhone, then Save at the label (not tap_row)."""
    if find_label_exact(nodes, ("Save to Files",)):
        log("Share sheet -> Save to Files")
        tap_label_center(stack, find_label_exact(nodes, ("Save to Files",)), "Save to Files")
        time.sleep(1.2)
        blob, nodes = dump(stack, phone, "719_files")
        log("FILES " + visible_labels(nodes))
    on_mine = find_label_exact(nodes, ("On My iPhone", "On My iPhone..."))
    if on_mine is not None:
        log("Files -> On My iPhone")
        tap_label_center(stack, on_mine, "On My iPhone")
        time.sleep(1.1)
        blob, nodes = dump(stack, phone, "719_on_iphone")
        log("ON_IPHONE " + visible_labels(nodes))
    save = find_label_exact(nodes, ("Save",))
    if save is not None:
        log("TAP Save (label center)")
        tap_label_center(stack, save, "Save")
        time.sleep(2.0)
        blob, nodes = dump(stack, phone, "720_files_saved")
        log("SAVED_UI " + visible_labels(nodes))
    return nodes


def pull_newest_backup(phone: Phone, name: str) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    find_cmd = (
        "find /var/mobile /var/jb -type f \\( "
        "-iname '*.cranect' -o -iname '*.cranebackup' -o -iname '*Reddit_a0001*' "
        "-o -iname '*crane*backup*' \\) 2>/dev/null | head -40"
    )
    code, out = phone.exec_ok(find_cmd, timeout=40)
    log("FIND_BACKUP\n" + (out or "").strip())
    paths = [ln.strip() for ln in (out or "").splitlines() if ln.strip().startswith("/")]
    if not paths:
        raise PhoneError(
            "Backup UI may have run, but no new Crane backup file found under "
            "/var/mobile or /var/jb in the last 30 minutes. See FIND_BACKUP and 717/718 dumps."
        )
    remote = paths[0]
    raw = phone.sftp_read_bytes(remote)
    base = os.path.basename(remote) or f"{name}.crane"
    local = os.path.join(BACKUP_DIR, f"{name}_{base}")
    with open(local, "wb") as f:
        f.write(raw)
    log(f"PC_BACKUP {local} bytes={len(raw)} from {remote}")
    return local


def pull_all_cranect(phone: Phone) -> list[str]:
    """Copy every On My iPhone .cranect to logs/crane_backups. Do not delete anything."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    find_cmd = (
        "find /var/mobile/Containers/Shared/AppGroup /var/mobile/Media "
        "/var/mobile/Containers/Data /var/jb -type f -iname '*.cranect' 2>/dev/null"
    )
    code, out = phone.exec_ok(find_cmd, timeout=90)
    log("FIND_ALL_CRANECT\n" + (out or "").strip())
    paths = []
    seen = set()
    for ln in (out or "").splitlines():
        p = ln.strip()
        if not p.startswith("/") or not p.lower().endswith(".cranect"):
            continue
        if p in seen:
            continue
        seen.add(p)
        paths.append(p)
    if not paths:
        raise PhoneError("No .cranect files on the phone. See FIND_ALL_CRANECT.")
    saved: list[str] = []
    for remote in paths:
        base = os.path.basename(remote)
        local = os.path.join(BACKUP_DIR, base)
        log(f"PULL {remote}")
        raw = phone.sftp_read_bytes(remote)
        with open(local, "wb") as f:
            f.write(raw)
        log(f"PC {local} bytes={len(raw)}")
        saved.append(local)
    return saved


def backup_container(phone: Phone, stack: TapStack, name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise PhoneError("Need a container name (r1).")
    blob, nodes = dump(stack, phone, "710_now")
    log("NOW " + visible_labels(nodes))
    if find_label_exact(nodes, ("Ignore", "Warning")):
        log("Already on keychain Warning — Ignore then Create")
        nodes = confirm_backup_sheet(stack, phone, nodes)
    elif find_label_exact(nodes, ("On My iPhone", "Save to Files", "iCloud Drive")):
        log("Already on Files / share sheet")
    elif find_label_exact(nodes, ("New Backup", "Create")) and find_label_exact(
        nodes, ("Include Keychain", "Include keychain", "Keychain")
    ):
        log("Already on New Backup sheet")
        nodes = confirm_backup_sheet(stack, phone, nodes)
    else:
        if not find_label_exact(nodes, BACKUP_ROW):
            nodes = open_crane(stack, phone)
            nodes = open_reddit_containers(stack, phone, nodes)
            nodes = tap_named_container(stack, phone, nodes, name)
        else:
            log("Already on container bottom — Backup Data to File is on screen")
        if find_label_exact(
            nodes, ("Include Keychain", "Include keychain", "Keychain")
        ) and find_label_exact(nodes, BACKUP_ROW) is None:
            log("Include Keychain already on r1 bottom page")
            nodes = turn_on_include_keychain(stack, phone, nodes)
        if find_label_exact(nodes, BACKUP_ROW):
            nodes = tap_backup(stack, phone, nodes)
        nodes = confirm_backup_sheet(stack, phone, nodes)
    maybe_files_picker(stack, phone, nodes)
    return pull_newest_backup(phone, name)


def main() -> int:
    argv = [a for a in sys.argv[1:] if a]
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        if argv and argv[0] == "--pull-all":
            saved = pull_all_cranect(phone)
            log("CRANE_PULL_OK count=" + str(len(saved)))
            for p in saved:
                log("  " + p)
            log("Did not delete any Crane containers.")
            return 0
        name = "a0001"
        if argv:
            name = argv[0].strip()
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        local = backup_container(phone, stack, name)
        log("CRANE_BACKUP_OK " + local)
        log("Did not delete the container.")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
