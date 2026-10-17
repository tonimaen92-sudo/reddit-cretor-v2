# UTF-8
"""Restore Crane Reddit r1 from the On My iPhone .cranect backup.

r1 was deleted. Path: Done -> new r1 if missing -> container bottom
-> Restore Data from Backup File -> On My iPhone -> that file.

  python misc/crane_restore.py r1
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crane_backup import (  # noqa: E402
    dump,
    find_crane_name,
    flick_list,
    log,
    open_crane,
    open_reddit_containers,
    tap_label_center,
    visible_labels,
)
from crane_delete import back_to_reddit_list, is_r1_detail, is_reddit_list  # noqa: E402
from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack, collapsed_text, find_label_exact  # noqa: E402

def dialog_add_button(nodes):
    """Add Container sheet: Add sits next to Cancel. Not the list Add under the nav."""
    cancel = find_label_exact(nodes, ("Cancel",))
    adds = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) != "add":
            continue
        adds.append(n)
    if cancel is not None:
        cy = cancel.center()[1]
        for n in adds:
            if abs(n.center()[1] - cy) < 50 and n.x > cancel.x:
                return n
    for n in adds:
        if n.y > 280:
            return n
    return None
RESTORE_ROW = ("Restore Data from Backup File",)
ADD_ROW = (
    "Add Container",
    "New Container",
    "Add",
    "Create Container",
    "Create New Container",
)
FILE_HINTS = (
    "Reddit_a0001_09.15.2026_05-25-14.cranect",
    "reddit_a0001_09.15.2026_05-25-14.cranect",
    "Reddit_r1_09.15.2026_05-25-14.cranect",
    "Reddit_a0001",
    "Reddit_r1",
)


def exit_edit(stack: TapStack, phone: Phone, nodes):
    done = find_label_exact(nodes, ("Done",))
    if done is not None and done.y < 130:
        log("TAP Done (leave Edit)")
        tap_label_center(stack, done, "Done")
        time.sleep(0.8)
        blob, nodes = dump(stack, phone, "740_after_done")
        log("AFTER_DONE " + visible_labels(nodes))
    return nodes


def flick_to_bottom(stack: TapStack) -> None:
    stack.interp_flick(375, 1020, 375, 380, steps=18, ms=18)
    time.sleep(0.5)


def scroll_list(stack: TapStack, phone: Phone, stem: str, needles: tuple[str, ...], n: int = 22):
    last = None
    stuck = 0
    blob, nodes = dump(stack, phone, f"{stem}_0")
    hit = find_label_exact(nodes, needles)
    if hit is not None:
        return nodes, hit
    for i in range(n):
        log("Scroll container list toward BOTTOM")
        flick_to_bottom(stack)
        blob, nodes = dump(stack, phone, f"{stem}_{i + 1}")
        ids = visible_labels(nodes)
        log("LIST " + ids)
        hit = find_label_exact(nodes, needles)
        if hit is not None:
            return nodes, hit
        stuck = stuck + 1 if ids == last else 0
        last = ids
        if stuck >= 3:
            break
    return nodes, None


def ensure_r1(stack: TapStack, phone: Phone, nodes, name: str):
    row = find_crane_name(nodes, name)
    if row is not None and row.y > 110:
        if find_label_exact(nodes, ("Add Container",)) is None:
            log("r1 already on list")
            return nodes
    log("r1 missing -> scroll BOTTOM, tap Add")
    nodes, add = scroll_list(stack, phone, "741_add", ADD_ROW)
    if add is None:
        os.environ.pop("SKIP_SHOT", None)
        dump(stack, phone, "741_add_end", shot=True)
        os.environ["SKIP_SHOT"] = "1"
        raise PhoneError("Add not at bottom of Crane Reddit list. See 741_add dumps.")
    log("TAP Add " + collapsed_text(add.labels()))
    tap_label_center(stack, add, "Add")
    time.sleep(1.0)
    blob, nodes = dump(stack, phone, "742_name")
    log("NAME " + visible_labels(nodes))
    field = find_label_exact(nodes, ("Name", "Container Name", "Optional"))
    if field is not None:
        tap_label_center(stack, field, "Name field")
        time.sleep(0.4)
    stack.type_text_block(name)
    time.sleep(0.5)
    blob, nodes = dump(stack, phone, "742_named")
    log("NAMED " + visible_labels(nodes))
    go = dialog_add_button(nodes)
    if go is None:
        go = find_label_exact(nodes, ("Create", "Save"))
    if go is None or collapsed_text(go.labels()) == "cancel":
        raise PhoneError("Add confirm not on Add Container sheet. See 742_name.")
    tap_label_center(stack, go, "Add Container confirm")
    time.sleep(1.2)
    blob, nodes = dump(stack, phone, "743_after_create")
    log("AFTER_CREATE " + visible_labels(nodes))
    return nodes


def open_r1_restore_row(stack: TapStack, phone: Phone, nodes, name: str):
    row = find_crane_name(nodes, name)
    if row is None or row.y < 110:
        log("r1 not in this dump -> scroll BOTTOM (newest sits above Add)")
        nodes, row = scroll_list(stack, phone, "744_find", (name, name.lower()))
        if row is None:
            row = find_crane_name(nodes, name)
    if row is None:
        raise PhoneError("r1 not on list after create. See 744 dumps.")
    log("TAP r1")
    stack.tap_row(row)
    time.sleep(1.0)
    blob, nodes = dump(stack, phone, "745_container")
    log("CONTAINER " + visible_labels(nodes))
    return scroll_to_restore(stack, phone, nodes)


def scroll_to_restore(stack: TapStack, phone: Phone, nodes):
    hit = find_label_exact(nodes, RESTORE_ROW)
    last = None
    stuck = 0
    for i in range(10):
        if hit is not None:
            break
        log("Restore Data from Backup File off-screen -> scroll")
        flick_list(stack)
        blob, nodes = dump(stack, phone, f"745_bottom_{i}")
        log("BOTTOM " + visible_labels(nodes))
        hit = find_label_exact(nodes, RESTORE_ROW)
        ids = visible_labels(nodes)
        stuck = stuck + 1 if ids == last else 0
        last = ids
        if stuck >= 2:
            break
    if hit is None:
        raise PhoneError("Restore Data from Backup File not on r1 page. See 745 dumps.")
    log("TAP Restore Data from Backup File")
    tap_label_center(stack, hit, "Restore Data from Backup File")
    time.sleep(1.3)
    blob, nodes = dump(stack, phone, "746_picker")
    log("PICKER " + visible_labels(nodes))
    return nodes


def nav_browse(nodes):
    """Top-left Browse back, not the tab bar Browse at y~1319."""
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) == "browse" and n.y < 180:
            return n
    return None


def pick_cranect(stack: TapStack, phone: Phone, nodes):
    if find_label_exact(nodes, ("iCloud Drive", "Recents")) and find_label_exact(
        nodes, ("On My iPhone",)
    ) is None:
        back = nav_browse(nodes)
        if back is not None:
            log("Files -> top-left Browse (locations)")
            tap_label_center(stack, back, "Browse back")
            time.sleep(1.0)
            blob, nodes = dump(stack, phone, "746_locations")
            log("LOCATIONS " + visible_labels(nodes))
    on_mine = find_label_exact(nodes, ("On My iPhone", "On My iPhone..."))
    if on_mine is not None:
        log("Files -> On My iPhone")
        tap_label_center(stack, on_mine, "On My iPhone")
        time.sleep(1.1)
        blob, nodes = dump(stack, phone, "747_on_iphone")
        log("ON_IPHONE " + visible_labels(nodes))
    file_hit = None
    for label in FILE_HINTS:
        file_hit = find_label_exact(nodes, (label,))
        if file_hit is not None:
            break
    if file_hit is None:
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            blob = collapsed_text(n.labels())
            if "reddit_r1" in blob or "cranect" in blob:
                file_hit = n
                break
    if file_hit is None:
        os.environ.pop("SKIP_SHOT", None)
        dump(stack, phone, "747_no_file", shot=True)
        os.environ["SKIP_SHOT"] = "1"
        raise PhoneError("r1 cranect not in Files picker. See 747 dumps.")
    log("TAP backup file " + collapsed_text(file_hit.labels()))
    tap_label_center(stack, file_hit, "cranect")
    time.sleep(1.4)
    blob, nodes = dump(stack, phone, "748_after_file")
    log("AFTER_FILE " + visible_labels(nodes))
    go = find_label_exact(
        nodes, ("Restore", "Replace", "Open", "Done", "Yes", "Confirm")
    )
    if go is not None and collapsed_text(go.labels()) != "cancel":
        log("TAP " + collapsed_text(go.labels()))
        tap_label_center(stack, go, "Restore confirm")
        time.sleep(2.5)
        blob, nodes = dump(stack, phone, "749_after_restore")
        log("AFTER_RESTORE " + visible_labels(nodes))
    return nodes


def finish_add_sheet(stack: TapStack, phone: Phone, nodes):
    if find_label_exact(nodes, ("Add Container",)) is None:
        return nodes
    go = dialog_add_button(nodes)
    if go is None:
        raise PhoneError("Add Container sheet has no Add next to Cancel. See 730_now.")
    log("Add Container sheet already up -> tap dialog Add")
    tap_label_center(stack, go, "Add Container confirm")
    time.sleep(1.2)
    blob, nodes = dump(stack, phone, "743_after_create")
    log("AFTER_CREATE " + visible_labels(nodes))
    return nodes


def restore_r1(stack: TapStack, phone: Phone, name: str):
    blob, nodes = dump(stack, phone, "730_now")
    log("NOW " + visible_labels(nodes))
    if find_label_exact(nodes, ("Cancel",)) and find_label_exact(
        nodes, ("Browse", "Recents", "iCloud Drive", "On My iPhone", "Search")
    ):
        log("Already in Files picker")
        return pick_cranect(stack, phone, nodes)
    if is_r1_detail(nodes):
        log("Already on r1 container page")
        nodes = scroll_to_restore(stack, phone, nodes)
        return pick_cranect(stack, phone, nodes)
    nodes = back_to_reddit_list(stack, phone)
    nodes = finish_add_sheet(stack, phone, nodes)
    nodes = exit_edit(stack, phone, nodes)
    if not is_reddit_list(nodes):
        nodes = open_crane(stack, phone)
        nodes = open_reddit_containers(stack, phone, nodes)
    if find_label_exact(nodes, ("Add Container",)) is None:
        row = find_crane_name(nodes, name)
        if row is None or row.y < 110:
            nodes = ensure_r1(stack, phone, nodes, name)
    nodes = open_r1_restore_row(stack, phone, nodes, name)
    return pick_cranect(stack, phone, nodes)


def main() -> int:
    name = "a0001"
    add_only = False
    argv = [a for a in sys.argv[1:] if a]
    if "--add-only" in argv:
        add_only = True
        argv = [a for a in argv if a != "--add-only"]
    if argv:
        name = argv[0].strip()
    if name.lower() not in ("a0001", "r1"):
        print("ERROR restore is locked to a0001 (r1) for this test.")
        return 1
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if add_only:
            nodes = back_to_reddit_list(stack, phone)
            nodes = finish_add_sheet(stack, phone, nodes)
            if find_label_exact(nodes, ("Add Container",)):
                nodes = finish_add_sheet(stack, phone, nodes)
            log("CRANE_ADD_OK " + name)
            log(visible_labels(nodes))
            return 0
        restore_r1(stack, phone, name)
        log("CRANE_RESTORE_OK " + name)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
