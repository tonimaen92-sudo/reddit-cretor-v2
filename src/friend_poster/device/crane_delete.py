# UTF-8
"""Delete Crane Reddit container r1 from the list.

Edit (top right) -> red minus on the r1 row -> Delete. Not Delete Data.

  python misc/crane_delete.py r1
"""

from __future__ import annotations

import os
import sys
import time
from friend_poster.device.crane_backup import (  # noqa: E402
    dump,
    find_crane_name,
    log,
    open_crane,
    open_reddit_containers,
    tap_label_center,
    visible_labels,
)
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import TapStack, collapsed_text, find_label_exact  # noqa: E402


def is_reddit_list(nodes) -> bool:
    edit = find_label_exact(nodes, ("Edit", "Done"))
    apps = find_label_exact(nodes, ("Applications",))
    reddit = find_label_exact(nodes, ("Reddit",))
    if edit is None or apps is None or reddit is None:
        return False
    if find_label_exact(nodes, ("Crane",)) and find_label_exact(nodes, ("Settings",)):
        return False
    return apps.x < 220 and edit.y < 130


def is_r1_detail(nodes) -> bool:
    title = find_label_exact(nodes, ("a0001", "r1", "r", "R"))
    if title is None or title.y > 130:
        return False
    if find_label_exact(nodes, ("Container Name", "Backup Data to File", "Delete Data")):
        return True
    reddit = find_label_exact(nodes, ("Reddit",))
    return reddit is not None and reddit.x < 200


def back_to_reddit_list(stack: TapStack, phone: Phone):
    blob, nodes = dump(stack, phone, "730_now")
    log("NOW " + visible_labels(nodes))
    if find_label_exact(nodes, ("Add Container",)):
        log("Add Container sheet is up")
        return nodes
    if is_reddit_list(nodes):
        return nodes
    if is_r1_detail(nodes):
        back = find_label_exact(nodes, ("Reddit",))
        if back is None:
            raise PhoneError("r1 detail has no Reddit back. See 730_now.")
        log("r1 detail -> Reddit back to list")
        tap_label_center(stack, back, "Reddit back")
        time.sleep(1.0)
        blob, nodes = dump(stack, phone, "730_list")
        log("LIST " + visible_labels(nodes))
        if is_reddit_list(nodes):
            return nodes
    nodes = open_crane(stack, phone)
    nodes = open_reddit_containers(stack, phone, nodes)
    if not is_reddit_list(nodes):
        raise PhoneError("Not on Crane Reddit list. See 730 dumps.")
    return nodes


def tap_edit(stack: TapStack, phone: Phone, nodes):
    done = find_label_exact(nodes, ("Done",))
    if done is not None and done.y < 130:
        log("Already in Edit (Done visible)")
        return nodes
    edit = find_label_exact(nodes, ("Edit",))
    if edit is None:
        raise PhoneError("Edit not on top right. See 730 dumps.")
    log("TAP Edit top right")
    tap_label_center(stack, edit, "Edit")
    time.sleep(0.9)
    os.environ.pop("SKIP_SHOT", None)
    blob, nodes = dump(stack, phone, "731_edit", shot=True)
    os.environ["SKIP_SHOT"] = "1"
    log("EDIT " + visible_labels(nodes))
    return nodes


def tap_red_delete_r1(stack: TapStack, phone: Phone, nodes, name: str):
    """Red minus is left of the r1 row. Then the row's Delete button."""
    row = find_crane_name(nodes, name)
    if row is None:
        raise PhoneError("r1 row not on Edit list. See 731_edit.")
    if row.y < 140:
        raise PhoneError("Refusing title-bar r1. Need the CONTAINERS row.")
    # Red minus sits left of the name, same row center.
    mx = max(36, min(row.x - 24, 70))
    my = row.center()[1]
    log(f"TAP red minus for r1 at ({mx},{my}) row={collapsed_text(row.labels())} y={row.y}")
    stack.tap_xy(mx, my)
    time.sleep(0.7)
    blob, nodes = dump(stack, phone, "732_minus")
    log("MINUS " + visible_labels(nodes))
    delete = None
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) != "delete":
            continue
        if abs(n.center()[1] - my) < 50:
            delete = n
            break
    if delete is None:
        delete = find_label_exact(nodes, ("Delete",))
    if delete is None:
        raise PhoneError("Red Delete button not on r1 row. See 732_minus.")
    # Do not tap a Delete that sits on Default / r2.
    if abs(delete.center()[1] - my) > 70:
        raise PhoneError("Delete button is not on the r1 row. See 732_minus.")
    log("TAP red Delete on r1 row")
    tap_label_center(stack, delete, "Delete")
    time.sleep(1.2)
    os.environ.pop("SKIP_SHOT", None)
    blob, nodes = dump(stack, phone, "733_after_delete", shot=True)
    os.environ["SKIP_SHOT"] = "1"
    log("AFTER_DELETE " + visible_labels(nodes))
    # Alert confirm if Crane asks again.
    alert = find_label_exact(nodes, ("Delete", "Remove", "Yes"))
    cancel = find_label_exact(nodes, ("Cancel",))
    if alert is not None and cancel is not None:
        if collapsed_text(alert.labels()) == "cancel":
            raise PhoneError("Refusing Cancel.")
        log("Alert -> confirm Delete")
        tap_label_center(stack, alert, "Alert Delete")
        time.sleep(1.2)
        blob, nodes = dump(stack, phone, "734_after_alert")
        log("AFTER_ALERT " + visible_labels(nodes))
    return nodes


def delete_container(stack: TapStack, phone: Phone, name: str):
    nodes = back_to_reddit_list(stack, phone)
    nodes = tap_edit(stack, phone, nodes)
    return tap_red_delete_r1(stack, phone, nodes, name)


def main() -> int:
    name = "a0001"
    argv = [a for a in sys.argv[1:] if a]
    if argv:
        name = argv[0].strip()
    if name.lower() not in ("a0001", "r1"):
        print("ERROR this wipe is locked to a0001 (r1) for the restore test.")
        return 1
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        delete_container(stack, phone, name)
        log("CRANE_DELETE_OK " + name)
        log("Did not restore yet.")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
