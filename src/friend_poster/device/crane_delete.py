# UTF-8
"""Delete an exact Crane Reddit container from the list.

Edit (top right) -> red minus on the named row -> Delete. Not Delete Data.

  python misc/crane_delete.py r1
"""

from __future__ import annotations

import os
import plistlib
import sys
import time
from friend_poster.core.slots import (  # noqa: E402
    canonicalize_name,
    is_slot_name,
    loose_index,
)
from friend_poster.device.crane_backup import (  # noqa: E402
    dump,
    find_crane_name,
    flick_list,
    log,
    open_crane,
    open_reddit_containers,
    tap_label_center,
    visible_labels,
)
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import TapStack, collapsed_text, find_label_exact  # noqa: E402

# Crane's per-app state is the authority for the name<->UUID binding.  The
# Settings list itself shows only names, so read the binding before any
# destructive tap and prove the exact UUID disappeared afterwards.
CRANE_PREFS_PATHS = (
    "/var/jb/var/mobile/Library/Preferences/com.opa334.craneprefs.plist",
    "/var/mobile/Library/Preferences/com.opa334.craneprefs.plist",
)
CRANE_REDDIT_KEY = "appSettings_com.reddit.Reddit"


def _read_crane_reddit_app(phone: Phone, lg=log) -> tuple[str, dict]:
    """Read Crane's Reddit preferences without changing them."""
    raw = None
    path = ""
    for candidate in CRANE_PREFS_PATHS:
        try:
            raw = phone.sftp_read_bytes(candidate)
            path = candidate
            break
        except Exception as exc:
            lg(
                f"Crane prefs read failed for {candidate} "
                f"({type(exc).__name__}: {exc})"
            )
    if raw is None:
        raise PhoneError(
            "Could not read Crane Reddit preferences; refusing container deletion."
        )
    try:
        prefs = plistlib.loads(raw)
    except Exception as exc:
        raise PhoneError(
            f"Crane preferences are not a readable plist ({type(exc).__name__})."
        ) from exc
    app = prefs.get(CRANE_REDDIT_KEY)
    if not isinstance(app, dict):
        raise PhoneError(
            f"Crane preferences contain no {CRANE_REDDIT_KEY!r} app state."
        )
    return path, app


def _crane_container_rows(app: dict) -> list[tuple[str, str]]:
    """Return non-Default ``(canonical_name, uppercase UUID)`` pairs."""
    rows: list[tuple[str, str]] = []
    for row in app.get("Containers") or []:
        if not isinstance(row, dict):
            continue
        raw_name = str(row.get("name") or "").strip()
        name = canonicalize_name(raw_name)
        instance_id = str(row.get("identifier") or "").strip().upper()
        if not name or not instance_id:
            continue
        if name == "default" or instance_id == "DEFAULT":
            continue
        rows.append((name, instance_id))
    return rows


def resolve_crane_container_identity(
    phone: Phone,
    lg,
    name: str,
    recorded_instance: str = "",
    *,
    allow_absent: bool = False,
) -> tuple[str, str]:
    """Bind one canonical slot name to its exact Crane container UUID.

    The Crane Settings UI exposes names but not UUIDs.  This preflight reads
    Crane's own preferences, requires exactly one row with the canonical name,
    and refuses a recorded UUID that belongs to a different row.  That keeps
    the subsequent name-based UI tap fail-closed even when OCR is imperfect.
    """
    name = canonicalize_name(name)
    if not is_slot_name(name):
        raise PhoneError(f"Refusing Crane deletion for invalid container name {name!r}.")
    recorded = str(recorded_instance or "").strip().upper()
    if recorded and recorded == "DEFAULT":
        raise PhoneError("Refusing to delete Crane's built-in Default container.")

    _path, app = _read_crane_reddit_app(phone, lg)
    rows = _crane_container_rows(app)

    recorded_rows = [
        (row_name, row_id)
        for row_name, row_id in rows
        if row_id == recorded or (recorded and row_id.startswith(recorded))
    ]
    named_rows = [
        (row_name, row_id) for row_name, row_id in rows if row_name == name
    ]

    if not named_rows:
        if recorded_rows:
            other_name, other_id = recorded_rows[0]
            raise PhoneError(
                f"Crane binds UUID {other_id} to {other_name}, not {name}; "
                "refused to delete the wrong container."
            )
        if allow_absent:
            return "absent", ""
        raise PhoneError(
            f"Crane preferences contain no non-Default container named {name}."
        )

    if len(named_rows) != 1:
        raise PhoneError(
            f"Crane preferences contain {len(named_rows)} containers named {name}; "
            "name-based Settings deletion is ambiguous."
        )

    resolved_name, resolved_id = named_rows[0]
    if recorded:
        if not (
            resolved_id == recorded
            or resolved_id.startswith(recorded)
            or recorded.startswith(resolved_id)
        ):
            raise PhoneError(
                f"Crane container {resolved_name} is bound to UUID {resolved_id}, "
                f"not recorded {recorded}; refused to delete it."
            )
        if len(recorded_rows) != 1 or recorded_rows[0] != (resolved_name, resolved_id):
            raise PhoneError(
                f"Recorded Crane UUID {recorded} is not uniquely bound to "
                f"{resolved_name}; refused to delete it."
            )
        return "exact", resolved_id
    return "resolved", resolved_id


def resolve_sole_crane_container(
    _stack,
    phone: Phone,
    lg,
    stop=None,
    *,
    expected_name: str | None = None,
) -> tuple[str, str]:
    """Resolve exactly one non-Default Crane container from preferences.

    This is the legacy-cleanup precursor for checkpoint rows that predate the
    saved UUID.  It does not tap the phone.  Zero, multiple, or a mismatched
    expected slot name all fail closed.
    """
    if stop is not None and stop.is_set():
        raise PhoneError("Stopped.")
    expected = (
        canonicalize_name(expected_name)
        if str(expected_name or "").strip()
        else ""
    )
    if expected and not is_slot_name(expected):
        raise PhoneError(f"Bad expected container name {expected_name!r}.")

    _path, app = _read_crane_reddit_app(phone, lg)
    rows = _crane_container_rows(app)
    if len(rows) != 1:
        raise PhoneError(
            f"Crane preferences contain {len(rows)} non-Default containers; "
            "without a recorded UUID, exactly one is required."
        )
    name, instance_id = rows[0]
    if expected and name != expected:
        raise PhoneError(
            f"Crane's sole user container is {name}, not expected {expected}; "
            "refused to bind or delete it."
        )
    lg(
        f"RESOLVE SOLE: Crane preferences uniquely bind {name} to UUID "
        f"{instance_id}; no destructive action was taken."
    )
    return name, instance_id


def flick_list_toward_top(stack: TapStack) -> None:
    stack.interp_flick(375, 520, 375, 900, steps=16, ms=20)
    time.sleep(0.55)


def is_reddit_list(nodes) -> bool:
    edit = find_label_exact(nodes, ("Edit", "Done"))
    apps = find_label_exact(nodes, ("Applications",))
    if apps is None:
        for node in nodes:
            if node.ntype == "ocr-line" or not node.on_screen():
                continue
            text = collapsed_text(node.labels()).strip().lower()
            if text.lstrip("<‹ ").strip() == "applications" and node.y < 140:
                apps = node
                break
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


def _is_expanded_haptic_container_picker(nodes) -> bool:
    """Recognize the live expanded Crane submenu without trusting one OCR title.

    On this iOS/Crane build the expanded chevron is rendered as ``~ Container``
    rather than a stable ``Containers`` title.  Require the surrounding iOS
    haptic action plus all three Crane anchors before Settings can be tapped.
    """
    from friend_poster.reddit.reddit_open import haptic_system_action

    settings = find_label_exact(nodes, ("Settings",))
    default = find_label_exact(nodes, ("Default",))
    new_container = find_label_exact(nodes, ("New Container",))
    if (
        settings is None
        or default is None
        or new_container is None
        or not (650 <= settings.center()[1] <= 1000)
    ):
        return False
    container_header = None
    user_container = None
    for node in nodes:
        if not node.on_screen():
            continue
        text = collapsed_text(node.labels()).strip().lower()
        normalized = text.lstrip(">~⌄∨v ").strip()
        if normalized == "container" and 300 <= node.center()[1] <= 700:
            container_header = node
        candidate = text.lstrip("-−–— ").strip()
        if loose_index(candidate) is not None:
            user_container = node
    if container_header is None:
        return False
    if haptic_system_action(nodes) is not None:
        return True
    # OCR occasionally corrupts the iOS ``Remove App`` row, so the haptic
    # recognizer cannot prove that anchor.  The Crane submenu itself still has
    # a strong, ordered signature: Container -> Default -> one aNNNN row ->
    # New Container -> Settings.  Accept only that complete geometry.
    if user_container is None:
        return False
    ordered_y = (
        container_header.center()[1],
        default.center()[1],
        user_container.center()[1],
        new_container.center()[1],
        settings.center()[1],
    )
    return list(ordered_y) == sorted(ordered_y) and len(set(ordered_y)) == 5


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
    # Cleanup must enter Crane from Reddit's own SpringBoard shortcut.  This
    # preserves the same per-app context used during creation instead of
    # opening Ghost and searching its application list.
    from friend_poster.reddit.reddit_open import (  # local: avoids import cycle
        _find_reddit_from_home,
        crane_container_list,
        crane_singular_parent_marker,
        crane_submenu_marker,
        haptic_system_action,
        long_press_home_icon,
    )

    icon = _find_reddit_from_home(stack, phone, log, "730_delete")
    long_press_home_icon(stack, icon, log)
    time.sleep(0.8)
    blob, nodes = dump(stack, phone, "730_haptic")
    crane_marker = (
        crane_submenu_marker(nodes)
        or crane_singular_parent_marker(nodes)
        or find_label_exact(nodes, ("Container", "Containers"))
    )
    if haptic_system_action(nodes) is None or crane_marker is None:
        raise PhoneError(
            "Reddit long-press did not show a proven Crane container doorway; "
            "did not guess a tap. See 730_haptic."
        )
    log("TAP Crane container doorway from Reddit long-press menu")
    tap_label_center(stack, crane_marker, "Crane container doorway")
    time.sleep(0.8)
    blob, nodes = dump(stack, phone, "730_container_picker")
    settings = find_label_exact(nodes, ("Settings",))
    if settings is None or not (
        crane_container_list(nodes)
        or _is_expanded_haptic_container_picker(nodes)
        or find_label_exact(
            nodes, ("Select Container", "Switch Container", "Containers")
        )
    ):
        raise PhoneError(
            "Crane container picker did not expose its verified Settings row; "
            "did not guess a tap. See 730_container_picker."
        )
    log("TAP Settings at the bottom of the Crane container picker")
    tap_label_center(stack, settings, "Crane Settings")
    time.sleep(1.1)
    blob, nodes = dump(stack, phone, "730_reddit_settings")
    if not is_reddit_list(nodes):
        # Some Crane versions open their root pane first.  Follow the existing
        # verified Applications -> Reddit path, never a coordinate guess.
        if find_label_exact(nodes, ("Applications",)) is None:
            raise PhoneError(
                "Crane Settings did not open the Reddit container pane or Crane root. "
                "See 730_reddit_settings."
            )
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


def _red_minus_target(row, name: str) -> tuple[int, int, str]:
    """Return the verified edit-control point for one Crane container row.

    XXTouch can OCR the red control together with the slot name (for example
    ``- a0103``).  In that layout ``row.x`` is the left edge of the minus
    glyph, not the left edge of the name, so subtracting from it misses the
    control.  When OCR omits the glyph, derive the same point immediately to
    the left of the name.  The subsequent fresh frame still has to expose the
    row-local ``Delete`` action before deletion can continue.
    """
    label = collapsed_text(row.labels()).strip().lower()
    wanted = name.strip().lower()
    if wanted not in label:
        raise PhoneError(
            f"Refusing red-minus tap: OCR row does not contain exact {name}."
        )
    leading_minus = label.startswith(("-", "−", "–", "—"))
    if leading_minus:
        # Live Crane edit frame: Node(x=69,h=26,text='- a0103') and the
        # visible red circle is centred at x~=89 in the 750-point UI space.
        mx = row.x + max(12, min(row.h, round(row.h * 0.75)))
        source = "leading-minus OCR"
    else:
        # Some frames OCR only the name.  The control is one icon-width to
        # its left; scale from the observed row height rather than using a
        # container-specific absolute coordinate.
        mx = row.x - max(20, min(42, round(row.h * 1.15)))
        source = "name-only OCR"
    my = row.center()[1]
    if not (20 <= mx < row.center()[0] and 140 <= my <= 1250):
        raise PhoneError(
            f"Refusing implausible red-minus geometry for {name}: ({mx},{my})."
        )
    return mx, my, source


def _exact_container_rows_in_section(nodes, name: str):
    """Find exact name rows under CONTAINERS, excluding the active row.

    The live Settings pane repeats the active container at the top (right side)
    and lists the editable containers under ``CONTAINERS``.  Deleting the
    title/active row would target the wrong control, so section geometry is
    required whenever the header is visible.
    """
    header = find_label_exact(nodes, ("CONTAINERS", "Containers"))
    rows = []
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        if node.y < 140 or node.x >= 400:
            continue
        if header is not None and node.y <= header.y:
            continue
        text = collapsed_text(node.labels()).strip()
        candidate = text.lstrip()
        if candidate.startswith(("-", "−", "–", "—")):
            candidate = candidate[1:].strip()
        if candidate.lower() == name.lower():
            rows.append(node)
    return rows, header


def _find_exact_container_row(nodes, name: str):
    """Resolve one exact editable row, never the active/title row."""
    rows, header = _exact_container_rows_in_section(nodes, name)
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        raise PhoneError(
            f"Crane Settings contains {len(rows)} editable rows named {name}; "
            "refused an ambiguous red-minus tap."
        )
    if header is not None:
        return None
    # Offline/legacy frames may omit the CONTAINERS header.  Keep the existing
    # bounded OCR tolerance only in that case.
    return find_crane_name(nodes, name)


def tap_red_delete_r1(
    stack: TapStack,
    phone: Phone,
    nodes,
    name: str,
    *,
    absent_ok: bool = False,
):
    """Tap the red minus and Delete button for one exact container row."""
    row = _find_exact_container_row(nodes, name)
    last = None
    stuck = 0
    for i in range(14):
        if row is not None:
            break
        log(f"{name} off-screen in Edit -> scroll Crane containers")
        flick_list(stack)
        blob, nodes = dump(stack, phone, f"731_edit_{i}")
        labels = visible_labels(nodes)
        log("EDIT " + labels)
        row = _find_exact_container_row(nodes, name)
        stuck = stuck + 1 if labels == last else 0
        last = labels
        if stuck >= 3:
            break
    if row is None:
        # Crane can reopen at its previous scroll offset.  Scan the opposite
        # direction too before deciding that the exact named row is absent.
        last = None
        stuck = 0
        for i in range(14):
            log(f"{name} not found toward bottom -> scan back toward top")
            flick_list_toward_top(stack)
            blob, nodes = dump(stack, phone, f"731_edit_top_{i}")
            labels = visible_labels(nodes)
            log("EDIT " + labels)
            row = _find_exact_container_row(nodes, name)
            if row is not None:
                break
            stuck = stuck + 1 if labels == last else 0
            last = labels
            if stuck >= 3:
                break
    if row is None:
        if absent_ok:
            log(
                f"{name} is absent after the bounded exact-row scan; "
                "treating the pending deletion as already complete."
            )
            return None
        raise PhoneError(
            f"{name} row not on Edit list. See 731_edit dumps."
        )
    if row.y < 140:
        raise PhoneError(
            f"Refusing title-bar {name}. Need the CONTAINERS row."
        )
    mx, my, target_source = _red_minus_target(row, name)
    log(
        f"TAP red minus for {name} at ({mx},{my}) "
        f"row={collapsed_text(row.labels())} y={row.y} source={target_source}"
    )
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
        raise PhoneError(f"Red Delete button not on {name} row. See 732_minus.")
    # Do not tap a Delete that sits on Default / r2.
    if abs(delete.center()[1] - my) > 70:
        raise PhoneError(f"Delete button is not on the {name} row. See 732_minus.")
    log(f"TAP red Delete on {name} row")
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
    remaining, header = _exact_container_rows_in_section(nodes, name)
    if remaining:
        raise PhoneError(
            f"Crane container {name} is still visible after Delete; deletion "
            "was not confirmed."
        )
    if header is None and find_crane_name(nodes, name) is not None:
        raise PhoneError(
            f"Crane container {name} is still visible after Delete; deletion "
            "was not confirmed."
        )
    return nodes


def _check_stopped(stack: TapStack, stop) -> None:
    stack.check_cancelled()
    if stop is not None and stop.is_set():
        raise PhoneError("Stopped.")


def _active_container_label(nodes) -> str:
    """Return the live Settings ``Active Container`` value, minus its chevron."""
    header = find_label_exact(nodes, ("Active Container",))
    if header is None:
        return ""
    candidates = []
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        if abs(node.center()[1] - header.center()[1]) > 40:
            continue
        if node.center()[0] <= header.center()[0]:
            continue
        text = collapsed_text(node.labels()).strip()
        if not text or text.lower() == "active container":
            continue
        candidates.append((abs(node.center()[1] - header.center()[1]), node))
    if not candidates:
        return ""
    text = collapsed_text(min(candidates, key=lambda item: item[0])[1].labels()).strip()
    return text.rstrip("> ").strip()


def _wait_for_absent_crane_identity(
    phone: Phone,
    lg,
    name: str,
    recorded_instance: str,
    *,
    attempts: int = 6,
) -> None:
    """Wait for Crane's prefs to drop the exact UUID after the UI Delete."""
    last = recorded_instance
    for attempt in range(attempts):
        status, resolved = resolve_crane_container_identity(
            phone,
            lg,
            name,
            recorded_instance,
            allow_absent=True,
        )
        if status == "absent":
            return
        last = resolved
        time.sleep(0.45)
    raise PhoneError(
        f"Crane preferences still contain {name} UUID {last} after Delete; "
        "deletion was not persisted."
    )


def delete_named_crane_instance(
    stack: TapStack,
    phone: Phone,
    lg,
    name: str,
    instance_id: str,
    stop=None,
) -> str:
    """Delete one exact Crane Reddit container through its Settings UI.

    Verified sequence: long-press Reddit -> Container -> Settings -> Edit ->
    red minus on the named row -> red Delete.  Crane's preferences are read
    before the destructive tap to bind name to UUID, and read again afterwards
    to prove that exact UUID disappeared and is no longer active.  This never
    opens Ghost.
    """
    name = canonicalize_name(name)
    recorded = str(instance_id or "").strip().upper()
    if not is_slot_name(name):
        raise PhoneError(f"Refusing Crane deletion for invalid name {name!r}.")
    if not recorded or recorded == "DEFAULT":
        raise PhoneError(
            f"Refusing Crane deletion for {name} without a recorded non-Default UUID."
        )

    _check_stopped(stack, stop)
    status, preflight_id = resolve_crane_container_identity(
        phone, lg, name, recorded, allow_absent=True
    )
    if status == "absent":
        lg(
            f"DELETE {name}: Crane prefs already show recording UUID {recorded} "
            "absent; no destructive tap."
        )
        stack.press_home()
        time.sleep(0.7)
        dump(stack, phone, "735_delete_already_absent")
        return "absent"
    lg(
        f"DELETE {name}: Crane prefs bind {name} to UUID {preflight_id} "
        f"({status}); opening Settings."
    )

    nodes = back_to_reddit_list(stack, phone)
    _check_stopped(stack, stop)

    # Navigation can take several seconds.  Re-read Crane's authority before
    # Edit; if another actor already removed it, do not tap anything.
    status, current_id = resolve_crane_container_identity(
        phone, lg, name, recorded, allow_absent=True
    )
    if status == "absent":
        lg(
            f"DELETE {name}: Crane prefs show the container already absent "
            "before Edit; no destructive tap."
        )
        stack.press_home()
        time.sleep(0.7)
        dump(stack, phone, "735_delete_absent")
        return "absent"
    if current_id != preflight_id:
        raise PhoneError(
            f"Crane UUID for {name} changed from {preflight_id} to {current_id} "
            "during navigation; refused to delete."
        )

    nodes = tap_edit(stack, phone, nodes)
    _check_stopped(stack, stop)
    after_nodes = tap_red_delete_r1(
        stack,
        phone,
        nodes,
        name,
        absent_ok=True,
    )
    if after_nodes is None:
        lg(
            f"DELETE {name}: exact row was already absent in Edit; "
            "verifying Crane prefs."
        )
    else:
        lg(f"DELETE {name}: red Delete confirmed in the live frame.")

    _wait_for_absent_crane_identity(phone, lg, name, recorded)
    _path, app = _read_crane_reddit_app(phone, lg)
    active = str(app.get("activeContainer") or "").strip().upper()
    if active == recorded or (recorded and active.startswith(recorded)):
        # Live Crane builds can leave activeContainer pointing at the UUID they
        # just removed even though the Settings pane has already switched to
        # Default.  The dangling pointer cannot select a container that no
        # longer exists, but require the live frame to prove the switch before
        # accepting it.
        if after_nodes is None:
            _blob, after_nodes = dump(stack, phone, "734_verify_absent")
        active_label = _active_container_label(after_nodes)
        if not active_label or canonicalize_name(active_label) == name:
            raise PhoneError(
                f"Crane still marks deleted {name} UUID {recorded} as "
                "activeContainer and the live Settings pane does not prove "
                "another active container; the deletion is not clean."
            )
        lg(
            f"DELETE {name}: Crane left a stale activeContainer pointer to the "
            f"removed UUID; live Settings shows {active_label!r} active and the "
            "CONTAINERS row is gone. Treating the deletion as clean."
        )
    else:
        lg(
            f"DELETE {name}: Crane prefs prove UUID {recorded} is gone and is no "
            "longer active."
        )
    stack.press_home()
    time.sleep(0.7)
    dump(stack, phone, "735_delete_home")
    return "absent" if after_nodes is None else "deleted"


def delete_sole_crane_container(
    stack: TapStack,
    phone: Phone,
    lg,
    stop=None,
) -> str:
    """Delete the sole non-Default Crane container for legacy checkpoints."""
    _check_stopped(stack, stop)
    _path, app = _read_crane_reddit_app(phone, lg)
    rows = _crane_container_rows(app)
    if not rows:
        lg(
            "DELETE SOLE: Crane preferences prove that no non-Default "
            "container remains."
        )
        stack.press_home()
        time.sleep(0.7)
        dump(stack, phone, "735_delete_sole_absent")
        return "absent"
    name, instance_id = resolve_sole_crane_container(
        stack, phone, lg, stop=stop
    )
    return delete_named_crane_instance(
        stack, phone, lg, name, instance_id, stop=stop
    )


def _visible_user_container_name(nodes) -> str | None:
    """Return the first visible non-Default Crane container row.

    Bulk creation keeps exactly one user container in Reddit.  Stall recovery
    therefore must delete the container that is present, not depend on the
    failed slot name still matching OCR or durable PC state.
    """
    for row in nodes:
        if row.ntype == "ocr-line" or not row.on_screen() or row.y < 140:
            continue
        label = collapsed_text(row.labels()).strip()
        candidate = label.lstrip()
        if candidate.startswith(("-", "−", "–", "—")):
            candidate = candidate[1:].strip()
        if loose_index(candidate) is not None:
            return candidate
    return None


def delete_only_container(stack: TapStack, phone: Phone):
    """Delete the sole user container, regardless of its recorded slot name.

    This is intentionally reserved for failed-attempt recovery.  The normal
    completed-account cleanup continues to use :func:`delete_container` and
    its exact slot checkpoint.
    """
    nodes = back_to_reddit_list(stack, phone)
    nodes = tap_edit(stack, phone, nodes)
    name = _visible_user_container_name(nodes)
    last = None
    stuck = 0
    for i in range(14):
        if name is not None:
            break
        log("No user container visible in Edit -> scroll Crane containers")
        flick_list(stack)
        _blob, nodes = dump(stack, phone, f"731_only_{i}")
        labels = visible_labels(nodes)
        name = _visible_user_container_name(nodes)
        stuck = stuck + 1 if labels == last else 0
        last = labels
        if stuck >= 3:
            break
    if name is None:
        # Crane remembers its last list offset.  Sweep back to the top as well
        # before concluding that no user container remains.
        last = None
        stuck = 0
        for i in range(14):
            log("No user container found toward bottom -> scan back toward top")
            flick_list_toward_top(stack)
            _blob, nodes = dump(stack, phone, f"731_only_top_{i}")
            labels = visible_labels(nodes)
            name = _visible_user_container_name(nodes)
            if name is not None:
                break
            stuck = stuck + 1 if labels == last else 0
            last = labels
            if stuck >= 3:
                break
    if name is None:
        # Only the visible built-in Default row proves the user-container list
        # is empty.  If OCR cannot prove that state, fail so the outer recovery
        # loop retries instead of advancing on an assumption.
        if find_label_exact(nodes, ("Default",)) is None:
            raise PhoneError(
                "Could not confirm an empty Reddit Crane container list; "
                "recovery deletion must be retried."
            )
        log("No user container remains in the Reddit Crane list.")
        stack.press_home()
        time.sleep(0.7)
        dump(stack, phone, "735_delete_only_home")
        return "absent"
    log(f"STALL recovery: deleting the sole user container visible as {name!r}.")
    after_nodes = tap_red_delete_r1(stack, phone, nodes, name)
    if _visible_user_container_name(after_nodes) is not None:
        raise PhoneError(
            "A user container is still visible after recovery Delete; "
            "deletion was not confirmed."
        )
    stack.press_home()
    time.sleep(0.7)
    dump(stack, phone, "735_delete_only_home")
    return "deleted"


def delete_container(stack: TapStack, phone: Phone, name: str):
    nodes = back_to_reddit_list(stack, phone)
    nodes = tap_edit(stack, phone, nodes)
    nodes = tap_red_delete_r1(
        stack, phone, nodes, name, absent_ok=True
    )
    stack.press_home()
    time.sleep(0.7)
    dump(stack, phone, "735_delete_home")
    return "absent" if nodes is None else "deleted"


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
