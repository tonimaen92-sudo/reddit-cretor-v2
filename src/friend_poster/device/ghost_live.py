# UTF-8
"""Locked Ghost Reddit apply: next padded name (a0015…). Dump-first. No Reddit signup.

  python misc/ghost_live.py              one new name + organic close
  python misc/ghost_live.py --count 3    three new names, close once
  python misc/ghost_live.py --from a0014 --to a0020
"""

from __future__ import annotations

import os
import plistlib
import re
import sys
import time
from typing import Callable
from friend_poster.core.config import GHOST_BUNDLE  # noqa: E402
from friend_poster.core.slots import (  # noqa: E402
    CREATION_PROGRESS,
    canonicalize_name,
    ensure_ghost_container_needed,
    get_slot,
    ghost_container_state,
    is_slot_name,
    mark_ghost_container_provisioning,
    mark_ghost_container_ready,
    name_span,
    name_to_index,
    next_slot_id,
    slot_ids_used,
    unretire_slot_id,
    upsert_ghost,
)
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import (  # noqa: E402
    OperationCancelled,
    TapStack,
    collapsed_text,
    dump_named,
    find_label,
    find_label_exact,
)

os.environ["SKIP_SHOT"] = "1"

LogFn = Callable[[str], None]

ICLOUD_SHEET_TITLE = "Sign in to iCloud"
APPLE_ID_VERIFICATION_TITLE = "Apple ID Verification"


class ICloudModalBlockingError(PhoneError):
    """An iCloud re-auth modal is overlaying the screen and blocks Ghost.

    Raised instead of a misleading navigation failure (for example "Reddit
    not found") when the "Sign in to iCloud" sheet is on screen.  The
    pipeline routes this to a shared pause so the batch does not burn every
    slot against a modal that will block each of them.
    """

    failure_code = "icloud_modal_blocking"


class ContainerProvisioningReconcileError(PhoneError):
    """A newly-created Ghost container could not be proven unambiguously.

    This is a pause-only failure.  The durable ``provisioning`` checkpoint and
    its immutable pre-create UUID baseline are intentionally left intact so a
    later Retry / Resume can reconcile the same container without another
    Create or any model-assisted UI recovery.
    """

    failure_code = "container_provisioning_reconcile_failed"


def icloud_modal_present(nodes) -> bool:
    """True when a verified iCloud re-authentication modal overlays Ghost."""
    if find_label_exact(nodes, (ICLOUD_SHEET_TITLE,)) is not None:
        return True
    return apple_id_verification_modal(nodes) is not None


def apple_id_verification_modal(nodes):
    """Return the safe dismiss button for the exact observed system modal.

    The title alone is insufficient: require the instruction and both actions,
    with their expected vertical ordering, so an unrelated Settings button can
    never be tapped as modal recovery.
    """
    title = find_label_exact(nodes, (APPLE_ID_VERIFICATION_TITLE,))
    instruction = find_label_exact(nodes, ("Enter the password for",))
    not_now = find_label_exact(nodes, ("Not Now",))
    settings = find_label_exact(nodes, ("Settings",))
    if None in (title, instruction, not_now, settings):
        return None
    tx, ty = title.center()
    ix, iy = instruction.center()
    nx, ny = not_now.center()
    sx, sy = settings.center()
    if not (500 <= ty <= 720 and ty < iy < ny and abs(ny - sy) <= 45):
        return None
    if not (nx < tx < sx and 100 <= nx <= 350 and 400 <= sx <= 650):
        return None
    return not_now


def dismiss_apple_id_verification_modal(
    stack: TapStack, phone: Phone, lg: LogFn, nodes
):
    """Dismiss one exact Apple-ID overlay and freshly verify it is gone."""
    not_now = apple_id_verification_modal(nodes)
    if not_now is None:
        raise ICloudModalBlockingError(
            "An iCloud authentication sheet is blocking Ghost, but it did not "
            "match the verified Apple ID Verification layout; batch paused."
        )
    lg("Apple ID Verification blocks Ghost; tapping verified Not Now once.")
    stack.tap_xy(*not_now.center())
    time.sleep(0.8)
    _blob, fresh = dump_named(
        stack, phone, "621_after_apple_id_not_now", lg, persist=False, preview=False
    )
    if icloud_modal_present(fresh):
        raise ICloudModalBlockingError(
            "Apple ID Verification remained after one verified Not Now tap; "
            "batch paused without consuming the queue item."
        )
    return fresh
R_NAME = re.compile(r"^r(\d+)$", re.I)
INSTANCE_TEXT = re.compile(
    r"^([A-Z0-9]{7,8})(?:-|\s+)[A-Z0-9]{3,}(?:-[A-Z0-9]{3,}){2,}",
    re.I,
)
SHORT_INSTANCE_TEXT = re.compile(
    r"^([A-Z0-9]{5,6})(?:-|\s+)[A-Z0-9]{3,}(?:-[A-Z0-9]{3,}){2,}",
    re.I,
)
INSTANCE_OCR_TRANSLATION = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "С": "C",
        "Е": "E",
        "Д": "4",
    }
)
CRANE_PREFS_PATHS = (
    "/var/jb/var/mobile/Library/Preferences/com.opa334.craneprefs.plist",
    "/var/mobile/Library/Preferences/com.opa334.craneprefs.plist",
)
CRANE_REDDIT_KEY = "appSettings_com.reddit.Reddit"


def log(msg: str) -> None:
    print(msg, flush=True)


def repair_stale_crane_reddit_active(phone: Phone, lg: LogFn) -> bool:
    """Repair only Crane's impossible Reddit active-container reference.

    Erasing tweak settings can leave ``activeContainer`` pointing at a UUID
    that is no longer present in Crane's own ``Containers`` array.  Ghost 1.5
    aborts while opening that application's settings.  Preserve valid state;
    when the reference is stale, select the sole named container or DEFAULT.
    """
    prefs_path = None
    raw = None
    for candidate in CRANE_PREFS_PATHS:
        try:
            raw = phone.sftp_read_bytes(candidate)
            prefs_path = candidate
            break
        except Exception:
            continue
    if prefs_path is None or raw is None:
        return False
    try:
        prefs = plistlib.loads(raw)
        app = prefs.get(CRANE_REDDIT_KEY)
        containers = app.get("Containers") if isinstance(app, dict) else None
        if not isinstance(containers, list):
            return False
        rows = [row for row in containers if isinstance(row, dict)]
        valid_ids = {
            str(row.get("identifier") or "").strip()
            for row in rows
            if str(row.get("identifier") or "").strip()
        }
        active = str(app.get("activeContainer") or "").strip()
        if not active or active in valid_ids:
            return False
        named_ids = [
            str(row.get("identifier") or "").strip()
            for row in rows
            if str(row.get("identifier") or "").strip()
            and str(row.get("identifier") or "").strip() != "DEFAULT"
            and str(row.get("name") or "").strip()
        ]
        replacement = named_ids[0] if len(named_ids) == 1 else "DEFAULT"
        if replacement not in valid_ids:
            raise PhoneError(
                "Crane Reddit preferences contain a stale active container and no "
                "safe replacement. Stopped before opening Ghost."
            )
        app["activeContainer"] = replacement
        backup = f"{prefs_path}.backup-stale-active-{int(time.time())}"
        phone.sftp_write_bytes(backup, raw)
        phone.sftp_write_bytes(
            prefs_path,
            plistlib.dumps(prefs, fmt=plistlib.FMT_BINARY, sort_keys=False),
        )
        check = plistlib.loads(phone.sftp_read_bytes(prefs_path))
        if check.get(CRANE_REDDIT_KEY, {}).get("activeContainer") != replacement:
            raise PhoneError("Crane Reddit active-container repair did not persist.")
        phone.exec_ok("killall cfprefsd", timeout=10)
        lg("Repaired stale Crane Reddit active-container reference.")
        return True
    except PhoneError:
        raise
    except Exception as exc:
        raise PhoneError(
            f"Could not safely validate Crane Reddit preferences ({type(exc).__name__})."
        ) from exc


def must(nodes, names: tuple[str, ...]):
    n = find_label_exact(nodes, names)
    if n is None:
        raise PhoneError("not on screen: " + repr(names))
    return n


def stop_if_reset(nodes) -> None:
    if find_label_exact(nodes, ("Yes, I'm sure!",)):
        raise PhoneError("Ghost Home looks like Reset/Erase. Stopped. Do not swipe Ghost Home.")


def tap_container_name(stack: TapStack, node) -> None:
    """Tap strictly inside the freshly detected container-name rectangle."""
    if node.w < 3 or node.h < 3:
        raise PhoneError("Container-name OCR rectangle is too small to tap safely.")
    x = min(max(node.x + 80, node.x + 1), node.x + node.w - 1)
    y = min(max(node.center()[1], node.y + 1), node.y + node.h - 1)
    if not (node.x < x < node.x + node.w and node.y < y < node.y + node.h):
        raise PhoneError("Could not derive a tap strictly inside the container row.")
    stack.tap_xy(x, y)


def is_crane_settings(nodes) -> bool:
    """Any Crane pane. uiopen Ghost from Settings leaves Crane in front — Home first."""
    edit = find_label_exact(nodes, ("Edit",))
    apps = find_label_exact(nodes, ("Applications",))
    if edit is not None and apps is not None and edit.y < 120 and apps.y < 120:
        return True
    if find_label_exact(nodes, ("Make Default Container",)) is not None:
        return True
    if find_label_exact(nodes, ("Container Name",)) is not None:
        return True
    if find_label_exact(nodes, ("Protect Using Biometrics",)) is not None:
        return True
    return False


def on_containers_list(nodes) -> bool:
    if is_crane_settings(nodes):
        return False
    c = find_label_exact(nodes, ("Containers",))
    # Ghost title is y~63. Crane's CONTAINERS section header is y~163.
    return bool(c is not None and c.y < 120)


def on_application_picker(nodes) -> bool:
    """Ghost Applications sheet, including when it survives an app reopen."""
    title = find_label_exact(nodes, ("Applications",))
    if title is None or title.y >= 150:
        return False
    return (
        find_label_exact(nodes, ("All Apps", "System Apps", "User Apps")) is not None
        or find_label(nodes, ("Search apps",)) is not None
    )


def ghost_screen_state(nodes) -> str:
    """Classify only proven Ghost/Crane states; never infer tap coordinates."""
    if find_label_exact(nodes, ("Yes, I'm sure!",)) is not None:
        return "reset"
    if find_label_exact(nodes, ("Create",)) is not None and find_label_exact(
        nodes, ("Cancel",)
    ) is not None:
        return "name-dialog"
    if is_crane_settings(nodes):
        return "crane"
    if find_label_exact(nodes, ("Hide",)) is not None and find_label_exact(
        nodes, ("Pasteboard",)
    ) is not None:
        return "advanced"
    if on_containers_list(nodes):
        return "containers"
    if find_label_exact(nodes, ("Formulate",)) is not None or find_label_exact(
        nodes, ("Enable",)
    ) is not None:
        return "reddit-settings"
    if on_application_picker(nodes):
        return "applications"
    if find_label_exact(nodes, ("Application List",)) is not None:
        return "home"
    return "unknown"


def ghost_close_button(nodes):
    """Return only Ghost's proven top-right X; ignore unrelated X labels."""
    for node in nodes or []:
        if not node.on_screen() or not node.visible:
            continue
        if collapsed_text(node.labels()) != "x":
            continue
        cx, cy = node.center()
        if cx >= 600 and cy <= 160:
            return node
    return None


def route_ghost_screen(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    stop=None,
) -> tuple[str, list]:
    """Normalize any known entry state to Containers, Applications, or Home."""
    opened = False
    used_x_fallback = False
    for attempt in range(6):
        if stop and stop.is_set():
            raise PhoneError("Stopped.")
        state = ghost_screen_state(nodes)
        lg(f"Ghost screen state: {state}")
        if state == "reset":
            stop_if_reset(nodes)
        if state in ("containers", "applications", "home"):
            return state, nodes
        if state == "name-dialog":
            lg("Name sheet leftover -> Cancel")
            stack.tap_row(must(nodes, ("Cancel",)))
            time.sleep(0.7)
            _, nodes = dump_named(stack, phone, "621_after_cancel", lg)
            continue
        if state in ("advanced", "reddit-settings"):
            containers = find_label_exact(nodes, ("Containers",))
            used_x = False
            if containers is not None:
                lg(f"On Ghost {state} -> tap Containers")
                stack.tap_xy(*containers.center())
                stem = "621_after_route_containers"
            else:
                close = ghost_close_button(nodes)
                if close is None:
                    raise PhoneError(
                        f"Ghost {state} has neither Containers nor a proven top-right X; "
                        "stopped without guessing."
                    )
                if used_x_fallback:
                    raise PhoneError(
                        f"Ghost {state} still hides Containers after the X fallback; "
                        "stopped without repeating it."
                    )
                used_x_fallback = True
                used_x = True
                lg(f"Ghost {state} hides Containers -> tap proven top-right X")
                stack.tap_xy(*close.center())
                stem = "621_after_route_x"
            time.sleep(0.8)
            _, next_nodes = dump_named(stack, phone, stem, lg)
            if used_x and ghost_screen_state(next_nodes) == state:
                raise PhoneError(
                    f"Ghost top-right X did not leave {state}; stopped without guessing."
                )
            nodes = next_nodes
            continue
        if state == "crane":
            lg("On Crane. Home first, then Ghost. Do not tap Crane Reddit.")
            stack.press_home()
            time.sleep(0.8)
            stack.open_bundle(GHOST_BUNDLE)
            time.sleep(1.5)
            _, nodes = dump_named(
                stack, phone, "621_home", lg, persist=False, preview=False
            )
            opened = True
            continue
        if state == "unknown" and not opened:
            stack.open_bundle(GHOST_BUNDLE)
            time.sleep(1.3)
            _, nodes = dump_named(
                stack, phone, "621_home", lg, persist=False, preview=False
            )
            opened = True
            continue
        raise PhoneError(
            f"Unknown Ghost screen after open (route attempt {attempt + 1}); "
            "stopped without guessing."
        )
    raise PhoneError("Ghost screen did not settle after six routed states.")


def viewport_ids(nodes) -> tuple[str, ...]:
    return tuple(sorted(names_on_screen(nodes)))


def list_flick(stack: TapStack, toward_bottom: bool, big: bool = False) -> None:
    """Flick the list body. After Create, newest is at the BOTTOM (chronological)."""
    if toward_bottom:
        if big:
            stack.interp_flick(375, 700, 375, 340, steps=18, ms=18)
        else:
            stack.interp_flick(375, 720, 375, 360, steps=16, ms=20)
    else:
        if big:
            stack.interp_flick(375, 340, 375, 700, steps=18, ms=18)
        else:
            stack.interp_flick(375, 360, 375, 720, steps=16, ms=20)


def scroll_containers_up(stack: TapStack) -> None:
    list_flick(stack, toward_bottom=True, big=False)


def scroll_containers_down(stack: TapStack) -> None:
    list_flick(stack, toward_bottom=False, big=False)


def find_named_in_containers(
    stack: TapStack, phone: Phone, lg: LogFn, name: str, stem: str
):
    """Create first. Then scroll DOWN. Newest is chronological at the bottom."""
    last: tuple[str, ...] | None = None
    stuck = 0
    for attempt in range(12):
        blob, nodes = dump_named(stack, phone, f"{stem}_{attempt}", lg)
        if not on_containers_list(nodes):
            raise PhoneError("Not on Containers list. See dump.")
        named = find_label_exact(nodes, name_keys(name))
        ids = viewport_ids(nodes)
        lg("VIEW " + stem + " ids=" + ",".join(ids) + ((" HIT " + name) if named else " miss " + name))
        if named is not None:
            return nodes, named
        if ids == last:
            stuck += 1
        else:
            stuck = 0
        last = ids
        lg(f"{name} not visible -> scroll bottom")
        list_flick(stack, toward_bottom=True, big=stuck >= 2)
        time.sleep(0.5)
    blob, nodes = dump_named(stack, phone, stem + "_miss", lg, shot=True)
    named = find_label_exact(nodes, name_keys(name))
    if named is not None:
        return nodes, named
    raise PhoneError(f"{name} not at bottom after Create. See {stem}_miss.png")


def prevent_under(nodes, header_y: int):
    cands = [
        n
        for n in nodes
        if n.ntype != "ocr-line"
        and n.on_screen()
        and collapsed_text(n.labels()) == "prevent"
        and n.y > header_y
    ]
    if not cands:
        return None
    return min(cands, key=lambda n: n.y)


def names_on_screen(nodes) -> set[str]:
    """Ghost/Crane container names. Skip radio glyphs at x<130 (O / 9)."""
    used: set[str] = set()
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if n.x < 130:
            continue
        t = collapsed_text(n.labels()).lower()
        if is_slot_name(t):
            used.add(t)
    return used


def r_ids_on_screen(nodes) -> set[str]:
    return names_on_screen(nodes)


def next_name(nodes) -> str:
    return next_slot_id(names_on_screen(nodes) | slot_ids_used())


def allocate_new_names(count: int, unavailable: set[object]) -> list[str]:
    """Choose unique ids from the union of PC, retired, device, and run state."""
    extra = set(unavailable)
    out: list[str] = []
    for _ in range(max(0, int(count))):
        nxt = next_slot_id(extra)
        out.append(nxt)
        extra.add(nxt)
    return out


def name_keys(name: str) -> tuple[str, ...]:
    n = (name or "").strip()
    return (n, n.lower(), n.upper(), n[:1].upper() + n[1:].lower())


def _normalized_instance_text(text: str) -> str:
    return (
        str(text or "")
        .replace("…", "...")
        .upper()
        .translate(INSTANCE_OCR_TRANSLATION)
    )


def _instance_id(text: str) -> str | None:
    """Stable OCR key for a Ghost UUID subtitle (first UUID group only)."""
    normalized = _normalized_instance_text(text)
    match = INSTANCE_TEXT.match(normalized)
    if match is None:
        return None
    return match.group(1)


def _reconciled_short_instance_id(text: str, name: str) -> str | None:
    """Recover a 5–6 character OCR UUID prefix from one saved exact identity.

    Ghost occasionally drops one or two characters from the first UUID group
    while leaving the rest of the UUID-shaped subtitle intact.  A shortened
    token is never identity by itself: accept it only for the same named row,
    only when that slot has one durable ready 7–8 character instance id, and
    only when the observed token is the saved id with one or two deletions.
    The returned identity is always the saved full prefix.
    """
    match = SHORT_INSTANCE_TEXT.match(_normalized_instance_text(text))
    if match is None:
        return None
    observed = match.group(1)
    try:
        saved_row = get_slot(name)
    except Exception:
        return None
    if ghost_container_state(saved_row) != "ready":
        return None
    saved = _normalized_instance_text(
        str((saved_row or {}).get("ghost_container_instance") or "")
    ).split("-", 1)[0]
    if re.fullmatch(r"[A-Z0-9]{7,8}", saved) is None:
        return None
    if not 1 <= len(saved) - len(observed) <= 2:
        return None
    cursor = iter(saved)
    if not all(any(char == candidate for candidate in cursor) for char in observed):
        return None
    return saved


INSTANCE_SUBTITLE_MAX_CENTER_DELTA = 70


def _container_row_nodes(nodes) -> list:
    """Return exact visible slot-name rows on a proven Containers screen."""
    if not on_containers_list(nodes):
        return []
    return [
        node
        for node in nodes
        if node.ntype != "ocr-line"
        and node.on_screen()
        and node.visible
        and node.x >= 130
        and is_slot_name(collapsed_text(node.labels()).lower())
    ]


def _paired_instance_below(named, uuid_nodes, row_nodes):
    """Pair a row with its nearest strict UUID subtitle by downward center."""
    named_center_y = named.center()[1]
    next_row_center_y = min(
        (
            row.center()[1]
            for row in row_nodes
            if row is not named and row.center()[1] > named_center_y
        ),
        default=None,
    )
    candidates = []
    for node, instance_id in uuid_nodes:
        center_delta = node.center()[1] - named_center_y
        if not (0 < center_delta <= INSTANCE_SUBTITLE_MAX_CENTER_DELTA):
            continue
        if next_row_center_y is not None and node.center()[1] >= next_row_center_y:
            continue
        candidates.append((center_delta, node, instance_id))
    if not candidates:
        return None
    _delta, node, instance_id = min(candidates, key=lambda item: item[0])
    return node, instance_id


def container_instances(nodes, name: str) -> list[tuple[str, object]]:
    """Return exact named Ghost rows paired with their UUID-like subtitles.

    Bare legacy numbers are intentionally excluded.  Each accepted UUID must
    be immediately below the exact name row, keeping arbitrary screen text
    from becoming instance identity.
    """
    if not on_containers_list(nodes):
        return []
    wanted = {collapsed_text(value) for value in name_keys(name)}
    allow_migrated_one = name_to_index(name) == 1
    name_nodes = []
    for node in nodes:
        if (
            node.ntype == "ocr-line"
            or not node.on_screen()
            or not node.visible
            or node.x < 130
        ):
            continue
        text = collapsed_text(node.labels())
        if text in wanted or (allow_migrated_one and text == "1"):
            name_nodes.append(node)
    uuid_nodes = []
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        instance_id = _instance_id(node.labels())
        if instance_id is not None:
            uuid_nodes.append((node, instance_id))

    row_nodes = _container_row_nodes(nodes)
    if allow_migrated_one:
        row_nodes.extend(
            node
            for node in name_nodes
            if collapsed_text(node.labels()) == "1"
            and all(node is not existing for existing in row_nodes)
        )

    out: list[tuple[str, object]] = []
    for named in name_nodes:
        paired = _paired_instance_below(named, uuid_nodes, row_nodes)
        if paired is None:
            reconciled_nodes = []
            for node in nodes:
                if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
                    continue
                instance_id = _reconciled_short_instance_id(node.labels(), name)
                if instance_id is not None:
                    reconciled_nodes.append((node, instance_id))
            paired = _paired_instance_below(named, reconciled_nodes, row_nodes)
        if paired is None:
            continue
        _uuid_node, instance_id = paired
        out.append((instance_id, named))
    out.sort(key=lambda item: item[1].y)
    return out


def all_container_instances(nodes) -> list[tuple[str, str, object]]:
    """Return every visible normalized slot name paired with its UUID subtitle."""
    if not on_containers_list(nodes):
        return []
    row_nodes = _container_row_nodes(nodes)
    name_nodes = [
        (canonicalize_name(collapsed_text(node.labels()).lower()), node)
        for node in row_nodes
    ]
    uuid_nodes = []
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        instance_id = _instance_id(node.labels())
        if instance_id is not None:
            uuid_nodes.append((node, instance_id))
    out: list[tuple[str, str, object]] = []
    for name, named in name_nodes:
        paired = _paired_instance_below(named, uuid_nodes, row_nodes)
        if paired is None:
            reconciled_nodes = []
            for node in nodes:
                if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
                    continue
                instance_id = _reconciled_short_instance_id(node.labels(), name)
                if instance_id is not None:
                    reconciled_nodes.append((node, instance_id))
            paired = _paired_instance_below(named, reconciled_nodes, row_nodes)
        if paired is not None:
            _uuid_node, instance_id = paired
            out.append((name, instance_id, named))
    out.sort(key=lambda item: item[2].y)
    return out


_GHOST_CONTAINER_CHROME_LABELS = {
    "containers",
    "default",
    "new container",
    "edit",
    "done",
    "x",
}


def _all_user_container_instances(nodes) -> list[tuple[str, str, object]]:
    """Return every visible non-Default Ghost row with a strict UUID subtitle.

    Unlike :func:`all_container_instances`, this proof parser deliberately
    includes arbitrary container names.  It is used only by the destructive
    no-recorded-UUID fallback, where overlooking an old manually named
    container could make a different slot look like the sole user container.
    UUID recognition and name/UUID geometry remain just as strict: the UUID
    must be structurally valid and immediately below its row.
    """
    if not on_containers_list(nodes):
        return []
    row_nodes = _user_container_row_nodes(nodes)

    uuid_nodes = []
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        instance_id = _instance_id(node.labels())
        if instance_id is not None:
            uuid_nodes.append((node, instance_id))

    out: list[tuple[str, str, object]] = []
    for named in row_nodes:
        paired = _paired_instance_below(named, uuid_nodes, row_nodes)
        if paired is None:
            continue
        _uuid_node, instance_id = paired
        out.append((collapsed_text(named.labels()).strip(), instance_id, named))
    out.sort(key=lambda item: item[2].y)
    return out


def _user_container_row_nodes(nodes) -> list:
    """Return all visible non-Default row candidates, including unknown names."""
    if not on_containers_list(nodes):
        return []
    title = find_label_exact(nodes, ("Containers",))
    footer = find_label_exact(nodes, ("New Container",))
    title_y = title.center()[1] if title is not None else 120
    footer_y = footer.center()[1] if footer is not None else 1270
    rows = []
    for node in nodes:
        if (
            node.ntype == "ocr-line"
            or not node.on_screen()
            or not node.visible
            or node.x < 130
        ):
            continue
        text = collapsed_text(node.labels()).strip()
        if not text or text.casefold() in _GHOST_CONTAINER_CHROME_LABELS:
            continue
        center_y = node.center()[1]
        if center_y <= title_y + 35 or center_y >= footer_y - 20:
            continue
        if _instance_id(node.labels()) is not None:
            continue
        rows.append(node)
    return rows


def _exact_named_nodes(nodes, name: str) -> list:
    wanted = {collapsed_text(value) for value in name_keys(name)}
    allow_migrated_one = name_to_index(name) == 1 and on_containers_list(nodes)
    return [
        node
        for node in nodes
        if node.ntype != "ocr-line"
        and node.on_screen()
        and node.visible
        and node.x >= 130
        and (
            collapsed_text(node.labels()) in wanted
            or (allow_migrated_one and collapsed_text(node.labels()) == "1")
        )
    ]


def _container_scan_signature(nodes) -> tuple:
    instances = sorted(
        value
        for node in nodes
        if node.ntype != "ocr-line"
        and node.on_screen()
        and (value := _instance_id(node.labels())) is not None
    )
    return (
        tuple(instances),
        bool(find_label_exact(nodes, ("New Container",))),
    )


def _collect_named_instances(nodes, name: str, seen: set[str]) -> bool:
    """Collect every fully observable exact-name row.

    A row entering at the top or sitting immediately above Ghost's fixed
    ``New Container`` footer can be visible while its UUID subtitle is still
    clipped.  That is not identity evidence and must not be accepted, but it
    also must not abort a bounded scan before one more list movement can bring
    the subtitle into view.  Missing UUIDs on interior rows remain fatal.

    Return ``True`` only when every exact-name row in this frame was paired.
    """
    exact = _exact_named_nodes(nodes, name)
    paired = container_instances(nodes, name)
    paired_node_ids = {id(node) for _instance_id, node in paired}
    missing = [node for node in exact if id(node) not in paired_node_ids]
    footer = find_label_exact(nodes, ("New Container",))

    def edge_clipped(node) -> bool:
        if node.y < 150:
            return True
        # The live Ghost list can omit a subtitle for the last row even when
        # its nominal bounds still fit just above the footer.  Require the name
        # to move into the fully observable body before judging that omission.
        if node.y > 1100:
            return True
        if footer is not None:
            # A complete UUID subtitle may extend up to 70 dump points below
            # the name.  Treat overlap with the fixed footer band as clipped.
            return node.y + node.h + 70 >= footer.y - 10
        return False

    interior_missing = [node for node in missing if not edge_clipped(node)]
    if interior_missing:
        raise PhoneError(
            f"Ghost could not read every exact {name} UUID subtitle; stopped before "
            "creating or selecting a duplicate."
        )
    seen.update(instance_id for instance_id, _node in paired)
    return not missing


def _collect_all_instances(nodes, seen: dict[str, set[str]]) -> bool:
    """Collect every visible slot row, rejecting unpaired interior names."""
    exact = []
    for node in nodes:
        if (
            node.ntype != "ocr-line" and node.on_screen() and node.visible
            and node.x >= 130 and is_slot_name(collapsed_text(node.labels()).lower())
        ):
            exact.append(node)
    paired = all_container_instances(nodes)
    paired_node_ids = {id(node) for _name, _instance, node in paired}
    missing = [node for node in exact if id(node) not in paired_node_ids]
    footer = find_label_exact(nodes, ("New Container",))

    def edge_clipped(node) -> bool:
        return (
            node.y < 150
            or node.y > 1100
            or (footer is not None and node.y + node.h + 70 >= footer.y - 10)
        )

    if any(not edge_clipped(node) for node in missing):
        raise PhoneError(
            "Ghost could not pair every visible slot name with a UUID; stopped "
            "before allocation or selection."
        )
    for name, instance_id, _node in paired:
        seen.setdefault(name, set()).add(instance_id)
    return not missing


def _collect_all_user_container_instances(
    nodes,
    seen: dict[str, set[str]],
    observed_uuid_ids: set[str],
    paired_uuid_ids: set[str],
) -> bool:
    """Collect every non-Default row for the no-UUID destructive proof.

    Arbitrary names are retained verbatim in ``seen`` so the sole-container
    resolver can count or reject them.  An interior row without a strict UUID
    is malformed and fails immediately.  UUIDs are tracked separately across
    the entire boundary-proven traversal so a subtitle whose name is never
    readable cannot silently disappear from the final inventory.
    """
    rows = _user_container_row_nodes(nodes)
    paired = _all_user_container_instances(nodes)
    paired_node_ids = {id(node) for _name, _instance, node in paired}
    missing = [node for node in rows if id(node) not in paired_node_ids]
    footer = find_label_exact(nodes, ("New Container",))

    def edge_clipped(node) -> bool:
        return (
            node.y < 150
            or node.y > 1100
            or (footer is not None and node.y + node.h + 70 >= footer.y - 10)
        )

    if any(not edge_clipped(node) for node in missing):
        raise PhoneError(
            "Ghost no-UUID inventory found an unrecognized or malformed user "
            "container row without a strict paired UUID; refused destructive "
            "reconciliation."
        )

    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        instance_id = _instance_id(node.labels())
        if instance_id is not None:
            observed_uuid_ids.add(instance_id)
    for raw_name, instance_id, _node in paired:
        seen.setdefault(raw_name, set()).add(instance_id)
        paired_uuid_ids.add(instance_id)
    return not missing


def _user_container_scan_signature(nodes) -> tuple:
    """Viewport signature that includes arbitrary and malformed row labels."""
    rows = tuple(
        sorted(
            collapsed_text(node.labels()).strip().casefold()
            for node in _user_container_row_nodes(nodes)
        )
    )
    uuids = tuple(
        sorted(
            instance_id
            for node in nodes
            if node.ntype != "ocr-line"
            and node.on_screen()
            and node.visible
            and (instance_id := _instance_id(node.labels())) is not None
        )
    )
    return rows, uuids, bool(find_label_exact(nodes, ("New Container",)))


def _scan_progress_ids(nodes) -> set[str]:
    """Validated container identities proven by one confirmed Containers frame.

    Only a slot-name row paired with its UUID-shaped subtitle counts, exactly as
    allocation does.  Raw OCR text, status-bar labels, coordinates and
    unpaired/clipped rows are never identity and so are never progress.
    """
    if not on_containers_list(nodes):
        return set()
    return {
        str(instance_id or "").strip().upper()
        for _name, instance_id, _row in all_container_instances(nodes)
        if str(instance_id or "").strip()
    }


def _note_scan_progress(nodes, reported: set[str]) -> None:
    """Count each newly traversed validated container identity once per direction.

    A long list legitimately needs many frames to walk; every identity newly
    proven by a fresh, confirmed Containers frame is real traversal progress,
    including for a target-name scan whose target does not exist yet.  The
    caller keeps one ``reported`` set per scan direction, so re-seeing rows on
    the way back down can count after the upward pass while a repeated or
    alternating already-seen frame never resets the inactivity clock.
    """
    current = _scan_progress_ids(nodes)
    fresh = current - reported
    if not fresh:
        return
    reported.update(fresh)
    CREATION_PROGRESS.note()


def scan_all_container_instances(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    stem: str = "624_all_instance_scan",
) -> tuple[dict[str, set[str]], list]:
    """Bounded complete Ghost scan used as the device allocation authority."""
    if not on_containers_list(nodes):
        raise PhoneError("Complete Ghost scan requires the proven Containers list.")
    seen: dict[str, set[str]] = {}
    _collect_all_instances(nodes, seen)
    upward_progress: set[str] = set()
    _note_scan_progress(nodes, upward_progress)

    # The caller passed a fresh top-of-list frame.  Count it as the first
    # boundary sample: if a single up-flick leaves the signature unchanged,
    # the list is already at the top and we can avoid a redundant flick.
    initial_sig = _container_scan_signature(nodes)
    initial_nodes = nodes
    previous = initial_sig
    unchanged = 0
    for index in range(32):
        stack.check_cancelled()
        if index == 0:
            list_flick(stack, toward_bottom=False, big=True)
            _blob, nodes_after = dump_named(stack, phone, f"{stem}_top_{index}", lg)
            if not on_containers_list(nodes_after):
                raise PhoneError("Ghost Containers list disappeared during complete scan.")
            sig_after = _container_scan_signature(nodes_after)
            if sig_after == initial_sig:
                nodes = initial_nodes
                previous = initial_sig
                unchanged = 2
                break
            nodes = nodes_after
            _collect_all_instances(nodes, seen)
            _note_scan_progress(nodes, upward_progress)
            signature = sig_after
            unchanged = 0 if signature != previous else 1
            previous = signature
            if unchanged >= 2:
                break
            continue
        list_flick(stack, toward_bottom=False, big=True)
        _blob, nodes = dump_named(stack, phone, f"{stem}_top_{index}", lg)
        if not on_containers_list(nodes):
            raise PhoneError("Ghost Containers list disappeared during complete scan.")
        _collect_all_instances(nodes, seen)
        _note_scan_progress(nodes, upward_progress)
        signature = _container_scan_signature(nodes)
        unchanged = unchanged + 1 if signature == previous else 0
        previous = signature
        if unchanged >= 2:
            break
    else:
        raise PhoneError("Complete Ghost scan could not prove the top boundary.")

    # New Container is a fixed footer and can remain visible while the list is
    # still at the top.  Prove the bottom from two unchanged downward-flick
    # results instead of treating that footer as a boundary marker.
    downward_progress: set[str] = set()
    _note_scan_progress(nodes, downward_progress)
    previous = _container_scan_signature(nodes)
    unchanged = 0
    for index in range(32):
        stack.check_cancelled()
        list_flick(stack, toward_bottom=True, big=True)
        _blob, nodes = dump_named(stack, phone, f"{stem}_bottom_{index}", lg)
        if not on_containers_list(nodes):
            raise PhoneError("Ghost Containers list disappeared during complete scan.")
        frame_complete = _collect_all_instances(nodes, seen)
        _note_scan_progress(nodes, downward_progress)
        signature = _container_scan_signature(nodes)
        unchanged = unchanged + 1 if signature == previous else 0
        previous = signature
        if unchanged >= 2:
            if not frame_complete:
                raise PhoneError(
                    "Ghost reached the list bottom with an unpaired container row."
                )
            lg(
                "Complete Ghost scan found "
                f"{len(seen)} occupied slot name(s) and "
                f"{sum(len(values) for values in seen.values())} instance(s)."
            )
            return seen, nodes
    raise PhoneError("Complete Ghost scan could not prove a stable bottom boundary.")


def scan_all_user_container_instances(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    stem: str = "741_all_user_container_scan",
) -> tuple[dict[str, set[str]], list]:
    """Prove every non-Default Ghost container, including arbitrary names.

    This stricter inventory exists for the destructive no-recorded-UUID
    fallback only.  The normal allocation scan intentionally remains limited
    to canonical slot names.  Here, any arbitrary paired name is retained,
    while malformed rows and UUID subtitles that never acquire a strict row
    pairing fail closed.
    """
    if not on_containers_list(nodes):
        raise PhoneError(
            "No-UUID Ghost inventory requires the proven Containers list."
        )
    seen: dict[str, set[str]] = {}
    observed_uuid_ids: set[str] = set()
    paired_uuid_ids: set[str] = set()
    _collect_all_user_container_instances(
        nodes, seen, observed_uuid_ids, paired_uuid_ids
    )

    initial_signature = _user_container_scan_signature(nodes)
    initial_nodes = nodes
    previous = initial_signature
    unchanged = 0
    for index in range(32):
        stack.check_cancelled()
        list_flick(stack, toward_bottom=False, big=True)
        _blob, fresh = dump_named(stack, phone, f"{stem}_top_{index}", lg)
        if not on_containers_list(fresh):
            raise PhoneError(
                "Ghost Containers list disappeared during no-UUID inventory proof."
            )
        signature = _user_container_scan_signature(fresh)
        if index == 0 and signature == initial_signature:
            # Preserve the original frame as the proven top.  It may contain a
            # less-clipped row than the equivalent post-flick OCR frame.
            nodes = initial_nodes
            unchanged = 2
            break
        nodes = fresh
        _collect_all_user_container_instances(
            nodes, seen, observed_uuid_ids, paired_uuid_ids
        )
        unchanged = unchanged + 1 if signature == previous else 0
        previous = signature
        if unchanged >= 2:
            break
    else:
        raise PhoneError(
            "No-UUID Ghost inventory could not prove the top boundary."
        )

    previous = _user_container_scan_signature(nodes)
    unchanged = 0
    frame_complete = True
    for index in range(32):
        stack.check_cancelled()
        list_flick(stack, toward_bottom=True, big=True)
        _blob, nodes = dump_named(stack, phone, f"{stem}_bottom_{index}", lg)
        if not on_containers_list(nodes):
            raise PhoneError(
                "Ghost Containers list disappeared during no-UUID inventory proof."
            )
        frame_complete = _collect_all_user_container_instances(
            nodes, seen, observed_uuid_ids, paired_uuid_ids
        )
        signature = _user_container_scan_signature(nodes)
        unchanged = unchanged + 1 if signature == previous else 0
        previous = signature
        if unchanged >= 2:
            break
    else:
        raise PhoneError(
            "No-UUID Ghost inventory could not prove the bottom boundary."
        )

    if not frame_complete:
        raise PhoneError(
            "No-UUID Ghost inventory reached the bottom with an unpaired user "
            "container row; refused destructive reconciliation."
        )
    never_paired = observed_uuid_ids - paired_uuid_ids
    if never_paired:
        raise PhoneError(
            "No-UUID Ghost inventory saw UUID subtitle(s) whose user-container "
            "names could not be proven; refused destructive reconciliation."
        )
    lg(
        "Complete no-UUID Ghost proof found "
        f"{sum(len(values) for values in seen.values())} non-Default "
        "container instance(s), including arbitrary names."
    )
    return seen, nodes


def scan_named_container_instances(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    name: str,
    stem: str = "624_instance_scan",
) -> tuple[set[str], list]:
    """Bounded full Ghost-list scan, returning at the list bottom.

    The caller uses the pre-create set as a durable checkpoint.  No row is
    tapped here; every gesture is followed by a fresh proven Containers dump.
    """
    if not on_containers_list(nodes):
        raise PhoneError("Ghost instance scan requires the proven Containers list.")
    seen: set[str] = set()
    _collect_named_instances(nodes, name, seen)
    upward_progress: set[str] = set()
    _note_scan_progress(nodes, upward_progress)

    previous = _container_scan_signature(nodes)
    unchanged = 0
    # Match the complete-scan bound: long lists need traversal plus two
    # unchanged top frames before a missing/existing exact name is trusted.
    for index in range(32):
        list_flick(stack, toward_bottom=False, big=True)
        time.sleep(0.45)
        _blob, nodes = dump_named(stack, phone, f"{stem}_top_{index}", lg)
        if not on_containers_list(nodes):
            raise PhoneError("Ghost Containers list disappeared during instance scan.")
        _collect_named_instances(nodes, name, seen)
        _note_scan_progress(nodes, upward_progress)
        signature = _container_scan_signature(nodes)
        unchanged = unchanged + 1 if signature == previous else 0
        previous = signature
        if unchanged >= 2:
            break
    else:
        raise PhoneError("Ghost instance scan could not prove the top boundary.")

    # The fixed New Container footer is visible in top frames too.  Always
    # drive the scroll to a stable bottom before concluding that a name is
    # absent or counting its exact UUID rows.
    downward_progress: set[str] = set()
    _note_scan_progress(nodes, downward_progress)
    previous = _container_scan_signature(nodes)
    unchanged = 0
    for index in range(32):
        stack.check_cancelled()
        list_flick(stack, toward_bottom=True, big=True)
        time.sleep(0.45)
        _blob, nodes = dump_named(stack, phone, f"{stem}_bottom_{index}", lg)
        if not on_containers_list(nodes):
            raise PhoneError("Ghost Containers list disappeared during instance scan.")
        frame_complete = _collect_named_instances(nodes, name, seen)
        _note_scan_progress(nodes, downward_progress)
        signature = _container_scan_signature(nodes)
        unchanged = unchanged + 1 if signature == previous else 0
        previous = signature
        if unchanged >= 2:
            if not frame_complete:
                raise PhoneError(
                    f"Ghost reached the list bottom without a complete {name} UUID row."
                )
            lg(
                f"Ghost instance scan complete for {name}: "
                f"{len(seen)} existing exact row(s)."
            )
            return seen, nodes
    raise PhoneError("Ghost instance scan could not prove a stable bottom boundary.")


def scan_named_container_instances_down(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    name: str,
    stem: str = "624_instance_scan_down",
) -> tuple[set[str], list]:
    """Bounded *downward-only* Ghost scan for one exact slot name.

    Ghost appends the newest container at the bottom, so a caller that has
    already proven the list top (the fresh-create fast path) or only needs the
    newest row (post-create capture) never has to reverse toward the top.  Every
    gesture moves toward the bottom until two unchanged frames prove the
    boundary.  The caller keeps the complete ``scan_named_container_instances``
    as the fallback whenever this bounded pass cannot prove a complete result.
    """
    if not on_containers_list(nodes):
        raise PhoneError("Ghost instance scan requires the proven Containers list.")
    seen: set[str] = set()
    _collect_named_instances(nodes, name, seen)
    downward_progress: set[str] = set()
    _note_scan_progress(nodes, downward_progress)
    previous = _container_scan_signature(nodes)
    unchanged = 0
    for index in range(32):
        stack.check_cancelled()
        list_flick(stack, toward_bottom=True, big=True)
        time.sleep(0.45)
        _blob, nodes = dump_named(stack, phone, f"{stem}_bottom_{index}", lg)
        if not on_containers_list(nodes):
            raise PhoneError("Ghost Containers list disappeared during instance scan.")
        frame_complete = _collect_named_instances(nodes, name, seen)
        _note_scan_progress(nodes, downward_progress)
        signature = _container_scan_signature(nodes)
        unchanged = unchanged + 1 if signature == previous else 0
        previous = signature
        if unchanged >= 2:
            if not frame_complete:
                raise PhoneError(
                    f"Ghost reached the list bottom without a complete {name} UUID row."
                )
            lg(
                f"Bounded downward Ghost scan for {name}: "
                f"{len(seen)} existing exact row(s)."
            )
            return seen, nodes
    raise PhoneError("Ghost instance scan could not prove a stable bottom boundary.")


def _containers_list_at_top(nodes) -> bool:
    """Cheap proof that the visible Containers list starts at its first row.

    ``Default`` is Ghost's built-in first row, so seeing it (at the top of the
    list body) means no real slot row is scrolled above the viewport and a
    downward-only pass covers the whole list.  Anything else keeps the complete
    scan in charge.
    """
    if not on_containers_list(nodes):
        return False
    default = find_label_exact(nodes, ("Default",))
    return default is not None and default.y < 300


def _instance_node(nodes, name: str, instance_id: str):
    wanted = str(instance_id or "").strip().upper()
    matches = [node for found, node in container_instances(nodes, name) if found == wanted]
    return max(matches, key=lambda node: node.y) if matches else None


def reconciled_new_instance(
    current_ids: set[str], baseline_ids: set[str], name: str
) -> str:
    """Return the sole post-create UUID or fail closed on zero/many."""
    baseline = {str(value or "").strip().upper() for value in baseline_ids}
    current = {str(value or "").strip().upper() for value in current_ids}
    new_ids = current - baseline
    if len(new_ids) != 1:
        raise ContainerProvisioningReconcileError(
            f"Ghost create for {name} produced {len(new_ids)} uniquely reconciled "
            "new instances; expected exactly one. Did not select a duplicate."
        )
    return next(iter(new_ids))


def resolve_recorded_instance(instances, recorded: str) -> tuple[str, str]:
    """Resolve a recorded (possibly truncated) UUID prefix against one name.

    The saved identity is a UUID *prefix* (the first group), so it is only
    trusted after being matched against a complete, validated inventory:

    ``("exact", value)``     the recorded token equals exactly one instance
    ``("prefix", value)``    the recorded token is a unique strict prefix
    ``("absent", "")``       the recorded token matches no instance
    ``("ambiguous", "")``    the recorded token matches more than one instance

    An ambiguous prefix is never treated as an exact UUID.
    """
    want = str(recorded or "").strip().upper()
    values = {
        str(value or "").strip().upper()
        for value in (instances or set())
        if str(value or "").strip()
    }
    if not want:
        return "absent", ""
    if want in values:
        return "exact", want
    prefixed = sorted(value for value in values if value.startswith(want))
    if len(prefixed) == 1:
        return "prefix", prefixed[0]
    if len(prefixed) > 1:
        return "ambiguous", ""
    return "absent", ""


def enter_container_edit(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    """Put the Ghost Containers list into the proven iOS Edit (red-minus) mode."""
    done = find_label_exact(nodes, ("Done",))
    if done is not None and done.y < 130:
        return nodes
    edit = find_label_exact(nodes, ("Edit",))
    if edit is None or edit.y >= 130:
        raise PhoneError("Ghost Containers list has no Edit button. See dump.")
    stack.tap_xy(*edit.center())
    time.sleep(0.9)
    _blob, nodes = dump_named(stack, phone, "744_ghost_edit", lg)
    if find_label_exact(nodes, ("Edit", "Done")) is None:
        raise PhoneError("Ghost Edit mode did not open. See 744_ghost_edit.")
    return nodes


def _locate_instance_row(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    name: str,
    instance_id: str,
    stem: str = "743_delete_locate",
):
    """Reacquire one exact (name, UUID) row anywhere in the list, bounded."""
    node = _instance_node(nodes, name, instance_id)
    if node is not None:
        return nodes, node
    for toward_bottom in (False, True):
        last: tuple[str, ...] | None = None
        stuck = 0
        for attempt in range(24):
            stack.check_cancelled()
            list_flick(stack, toward_bottom=toward_bottom, big=True)
            time.sleep(0.4)
            _blob, nodes = dump_named(
                stack,
                phone,
                f"{stem}_{'down' if toward_bottom else 'up'}_{attempt}",
                lg,
            )
            if not on_containers_list(nodes):
                raise PhoneError(
                    "Ghost Containers list disappeared while locating a delete row."
                )
            node = _instance_node(nodes, name, instance_id)
            if node is not None:
                return nodes, node
            ids = viewport_ids(nodes)
            stuck = stuck + 1 if ids == last else 0
            last = ids
            if stuck >= 2:
                break
    raise PhoneError(
        f"Could not relocate the exact {name} UUID {instance_id} row for deletion."
    )


def _delete_row_in_edit(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    name: str,
    instance_id: str,
):
    """Tap the red minus and Delete for one exact (name, UUID) Ghost row."""
    stack.check_cancelled()
    nodes = enter_container_edit(stack, phone, lg, nodes)
    nodes, row = _locate_instance_row(stack, phone, lg, nodes, name, instance_id)
    if row.y < 140:
        raise PhoneError(f"Refusing to delete the {name} title-bar row.")
    mx = max(36, min(row.x - 24, 70))
    my = row.center()[1]
    lg(f"TAP red minus to delete {name} at ({mx},{my}) y={row.y}")
    stack.tap_xy(mx, my)
    time.sleep(0.7)
    _blob, nodes = dump_named(stack, phone, "745_ghost_minus", lg)
    delete = None
    for candidate in nodes:
        if candidate.ntype == "ocr-line" or not candidate.on_screen():
            continue
        if collapsed_text(candidate.labels()).lower() != "delete":
            continue
        if abs(candidate.center()[1] - my) < 50:
            delete = candidate
            break
    if delete is None:
        delete = find_label_exact(nodes, ("Delete",))
    if delete is None or abs(delete.center()[1] - my) > 70:
        raise PhoneError(
            f"Red Delete button is not on the {name} row. See 745_ghost_minus."
        )
    lg(f"TAP red Delete on the {name} row")
    stack.tap_xy(*delete.center())
    time.sleep(1.2)
    _blob, nodes = dump_named(stack, phone, "746_ghost_after_delete", lg)
    alert = find_label_exact(nodes, ("Delete", "Remove", "Yes"))
    cancel = find_label_exact(nodes, ("Cancel",))
    if (
        alert is not None
        and cancel is not None
        and collapsed_text(alert.labels()).lower() != "cancel"
    ):
        lg("Alert -> confirm Delete")
        stack.tap_xy(*alert.center())
        time.sleep(1.2)
        _blob, nodes = dump_named(stack, phone, "747_ghost_after_alert", lg)
    return nodes


def delete_named_instance(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    name: str,
    instance_id: str,
    stop=None,
) -> str:
    """Delete one exact Ghost container and verify it against a full inventory.

    Returns ``"deleted"`` when this call removed the recorded container, or
    ``"absent"`` when a complete, validated inventory already proves the
    recorded identity is gone (safe for finishing a pending deletion).  A
    duplicate name or an ambiguous UUID prefix fails closed without deleting.
    The disappearance of a row from one viewport is never accepted as proof.

    The current screen is intentionally ignored: this always closes Ghost,
    reopens the Containers list and proves a complete inventory first, so it is
    safe to call from a pipeline or a deletion-only resume.
    """
    name = canonicalize_name(name)
    recorded = str(instance_id or "").strip().upper()
    if not is_slot_name(name):
        raise PhoneError(f"Bad container name {name!r}")
    if not recorded:
        raise PhoneError(f"Refusing to delete {name} without a recorded container UUID.")

    close_ghost_organic(stack, phone, lg)
    stack.check_cancelled()
    open_reddit_containers(stack, phone, lg, stop=stop)
    _blob, nodes = dump_named(stack, phone, f"742_delete_scan_{name}", lg)
    stop_if_reset(nodes)
    inventory, nodes = scan_all_container_instances(
        stack, phone, lg, nodes, stem=f"742_delete_inventory_{name}"
    )
    status, matched = resolve_recorded_instance(inventory.get(name) or set(), recorded)
    if status == "absent":
        lg(
            f"DELETE {name}: recorded {recorded} is already absent from a complete "
            "validated inventory."
        )
        return "absent"
    if status == "ambiguous":
        raise PhoneError(
            f"Recorded UUID {recorded} for {name} matches multiple containers; "
            "refused to delete an ambiguous prefix."
        )
    lg(f"DELETE {name}: complete inventory uniquely identifies {matched}; deleting.")
    nodes = _delete_row_in_edit(stack, phone, lg, nodes, name, matched)

    # A fresh, complete inventory — not one viewport — must prove the exact
    # container is gone before the deletion may be recorded as complete.
    close_ghost_organic(stack, phone, lg)
    stack.check_cancelled()
    open_reddit_containers(stack, phone, lg, stop=stop)
    _blob, nodes = dump_named(stack, phone, f"748_delete_verify_{name}", lg)
    if not on_containers_list(nodes):
        raise PhoneError("Ghost Containers list is unavailable for deletion verification.")
    after, _nodes = scan_all_container_instances(
        stack, phone, lg, nodes, stem=f"748_delete_verify_scan_{name}"
    )
    after_ids = {
        str(value or "").strip().upper() for value in (after.get(name) or set())
    }
    if matched in after_ids:
        raise PhoneError(
            f"Ghost container {name} UUID {matched} is still present in a complete "
            "inventory after deletion; deletion was not confirmed."
        )
    lg(f"DELETE {name}: {matched} verified absent from a complete inventory.")
    return "deleted"


def _sole_user_container(inventory) -> tuple[str, str] | None:
    """Return one unambiguous Ghost user-container identity, or fail closed.

    ``scan_all_user_container_instances`` is the authority for this input.  It
    includes arbitrary non-Default names so they count against sole ownership.
    Keep the validation here as well so callers cannot accidentally turn a
    partial or hand-built inventory into a destructive action.
    """
    if not isinstance(inventory, dict):
        raise PhoneError(
            "Ghost sole-container deletion requires a complete validated inventory."
        )

    instances: list[tuple[str, str]] = []
    for raw_name, raw_ids in inventory.items():
        displayed_name = str(raw_name or "").strip()
        if displayed_name.casefold() == "default":
            continue
        if not displayed_name or not isinstance(raw_ids, (set, frozenset)):
            raise PhoneError(
                "Ghost inventory contains a malformed user-container row; "
                "refused sole-container deletion."
            )
        for raw_id in raw_ids:
            instance_id = str(raw_id or "").strip().upper()
            if re.fullmatch(r"[A-Z0-9]{7,8}", instance_id) is None:
                raise PhoneError(
                    f"Ghost inventory has no full UUID identity for "
                    f"{displayed_name}; "
                    "refused sole-container deletion."
                )
            instances.append((displayed_name, instance_id))

    if not instances:
        return None
    if len(instances) != 1:
        raise PhoneError(
            "Ghost inventory contains "
            f"{len(instances)} user-container instances; expected exactly one and "
            "did not delete any container."
        )
    displayed_name, instance_id = instances[0]
    lowered = displayed_name.lower()
    if not is_slot_name(lowered) or canonicalize_name(lowered) != lowered:
        raise PhoneError(
            f"Ghost's sole non-Default container has unrecognized name "
            f"{displayed_name!r}; refused to bind or delete it."
        )
    return canonicalize_name(lowered), instance_id


def resolve_sole_user_container(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    stop=None,
    *,
    expected_name: str | None = None,
) -> tuple[str, str]:
    """Resolve exactly one user container from a complete Ghost inventory.

    This is the non-destructive precursor for legacy cleanup checkpoints that
    have no saved UUID.  It performs a fresh boundary-proven inventory, refuses
    zero/multiple/malformed rows, optionally binds the sole row to the expected
    slot name, and returns its full identity without entering Edit mode.
    """
    expected = (
        canonicalize_name(expected_name)
        if str(expected_name or "").strip()
        else ""
    )
    if expected and not is_slot_name(expected):
        raise PhoneError(f"Bad expected container name {expected_name!r}.")

    close_ghost_organic(stack, phone, lg)
    ghost_opened = False
    try:
        stack.check_cancelled()
        if stop is not None and stop.is_set():
            raise PhoneError("Stopped.")
        ghost_opened = True
        open_reddit_containers(stack, phone, lg, stop=stop)
        _blob, nodes = dump_named(stack, phone, "741_resolve_sole_scan", lg)
        stop_if_reset(nodes)
        inventory, _nodes = scan_all_user_container_instances(
            stack, phone, lg, nodes, stem="741_resolve_sole_inventory"
        )
        identity = _sole_user_container(inventory)
        if identity is None:
            raise PhoneError(
                "Ghost inventory contains no user container; without a recorded "
                "UUID, exact deletion ownership cannot be proven."
            )
        name, instance_id = identity
        if expected and name != expected:
            raise PhoneError(
                f"Ghost's sole user container is {name}, not expected {expected}; "
                "refused to bind or delete it."
            )
        lg(
            f"RESOLVE SOLE: complete inventory uniquely binds {name} to UUID "
            f"{instance_id}; no destructive action was taken."
        )
        return name, instance_id
    finally:
        if ghost_opened:
            if sys.exc_info()[0] is None:
                close_ghost_organic(stack, phone, lg)
            else:
                try:
                    close_ghost_organic(stack, phone, lg)
                except Exception as close_exc:
                    lg(
                        "RESOLVE SOLE: organic Ghost cleanup after failure also "
                        f"failed ({type(close_exc).__name__})."
                    )


def delete_sole_user_container(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    stop=None,
) -> str:
    """Delete Ghost's sole user container and prove the exact UUID disappeared.

    This recovery-only operation never trusts the current viewport or a saved
    slot name.  It starts from Home, opens Ghost's Reddit Containers list and
    performs a complete validated inventory.  Zero user instances is the only
    state that returns ``"absent"``.  Exactly one canonical ``(name, UUID)``
    pair may be deleted; multiple or malformed identities fail closed without
    a destructive tap.  A second close/open and complete inventory must then
    prove both that exact pair is gone and that the user-container list remains
    empty before ``"deleted"`` is returned.
    """

    def check_stopped() -> None:
        stack.check_cancelled()
        if stop is not None and stop.is_set():
            raise PhoneError("Stopped.")

    # Ignore any inherited Ghost screen/scroll state.  The full scan below is
    # the only authority for deciding whether a destructive gesture is safe.
    close_ghost_organic(stack, phone, lg)
    ghost_opened = False
    try:
        check_stopped()
        ghost_opened = True
        open_reddit_containers(stack, phone, lg, stop=stop)
        _blob, nodes = dump_named(stack, phone, "742_delete_sole_scan", lg)
        stop_if_reset(nodes)
        before, nodes = scan_all_user_container_instances(
            stack, phone, lg, nodes, stem="742_delete_sole_inventory"
        )
        identity = _sole_user_container(before)
        if identity is None:
            lg(
                "DELETE SOLE: complete validated Ghost inventory proves that "
                "no user container remains."
            )
            return "absent"

        name, instance_id = identity
        check_stopped()
        lg(
            "DELETE SOLE: complete inventory uniquely identifies "
            f"{name} UUID {instance_id}; deleting that exact row."
        )
        _delete_row_in_edit(stack, phone, lg, nodes, name, instance_id)

        # Discard every post-tap viewport assumption.  Reopen Ghost and prove
        # the full list again before reporting a destructive action as done.
        close_ghost_organic(stack, phone, lg)
        ghost_opened = False
        check_stopped()
        ghost_opened = True
        open_reddit_containers(stack, phone, lg, stop=stop)
        _blob, nodes = dump_named(stack, phone, "748_delete_sole_verify", lg)
        stop_if_reset(nodes)
        after, _nodes = scan_all_user_container_instances(
            stack, phone, lg, nodes, stem="748_delete_sole_verify_inventory"
        )
        remaining = _sole_user_container(after)
        if remaining == identity:
            raise PhoneError(
                f"Ghost container {name} UUID {instance_id} is still present in "
                "the complete inventory after deletion; deletion was not confirmed."
            )
        if remaining is not None:
            other_name, other_id = remaining
            raise PhoneError(
                f"Ghost container {name} UUID {instance_id} disappeared, but "
                f"unexpected user container {other_name} UUID {other_id} remains; "
                "sole-container cleanup was not confirmed."
            )
        lg(
            f"DELETE SOLE: {name} UUID {instance_id} verified absent and the "
            "complete user-container inventory is empty."
        )
        return "deleted"
    finally:
        if ghost_opened:
            if sys.exc_info()[0] is None:
                # A successful/absent outcome is not complete until Ghost is
                # organically closed; surface a close failure to the caller.
                close_ghost_organic(stack, phone, lg)
            else:
                # Preserve the primary safety failure if best-effort cleanup
                # also has trouble with the live UI.
                try:
                    close_ghost_organic(stack, phone, lg)
                except Exception as close_exc:
                    lg(
                        "DELETE SOLE: organic Ghost cleanup after failure also "
                        f"failed ({type(close_exc).__name__})."
                    )


def _fresh_safe_instance_node(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    name: str,
    instance_id: str,
):
    """Reacquire one UUID-bound row, with at most one edge-avoidance move."""
    wanted = str(instance_id or "").strip().upper()

    def unique(frame):
        matches = [
            node
            for found, node in container_instances(frame, name)
            if found == wanted
        ]
        if len(matches) != 1:
            raise PhoneError(
                f"Ghost could not pair {name} UUID {wanted} with exactly one visible row."
            )
        return matches[0]

    named = unique(nodes)
    center_y = named.center()[1]
    safe = named.y >= 150 and named.y + named.h <= 1100
    if safe:
        return nodes, named

    # One bounded move only.  The old coordinate is discarded immediately;
    # the exact UUID/name pair must be freshly proven after the gesture.
    stack.check_cancelled()
    list_flick(stack, toward_bottom=center_y > 625, big=False)
    _blob, fresh = dump_named(stack, phone, "641_reacquire_safe_row", lg)
    if not on_containers_list(fresh):
        raise PhoneError("Ghost Containers screen disappeared while making the row safe.")
    named = unique(fresh)
    if named.y < 150 or named.y + named.h > 1100:
        raise PhoneError(f"Exact {name} UUID row remains clipped or edge-unsafe.")
    return fresh, named


def _reacquire_recorded_instance_frame(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    name: str,
    instance_id: str,
):
    """Return a fresh Containers frame containing one exact recorded row.

    :func:`scan_all_container_instances` deliberately finishes at the stable
    list bottom.  A previously recorded container can therefore be present in
    the validated inventory without remaining in the final viewport.  Walk
    back toward the proven top, accepting only the exact name/UUID pair that
    was resolved from that inventory.  No same-name or coordinate-only row is
    ever accepted.
    """
    wanted = str(instance_id or "").strip().upper()
    if _instance_node(nodes, name, wanted) is not None:
        return nodes

    previous = _container_scan_signature(nodes)
    unchanged = 0
    for index in range(32):
        stack.check_cancelled()
        list_flick(stack, toward_bottom=False, big=True)
        _blob, fresh = dump_named(
            stack, phone, f"641_recorded_reacquire_{index}", lg
        )
        if not on_containers_list(fresh):
            raise PhoneError(
                "Ghost Containers list disappeared while reacquiring the "
                "recorded container."
            )
        if _instance_node(fresh, name, wanted) is not None:
            return fresh
        signature = _container_scan_signature(fresh)
        unchanged = unchanged + 1 if signature == previous else 0
        previous = signature
        nodes = fresh
        if unchanged >= 2:
            break

    raise PhoneError(
        f"Ghost complete inventory contained {name} UUID {wanted}, but its exact "
        "row could not be freshly reacquired; did not select another row."
    )


def _proven_reddit_settings(nodes, name: str) -> bool:
    """Require Reddit's Ghost settings page and its exact active container value."""
    if on_containers_list(nodes) or is_crane_settings(nodes):
        return False
    containers = find_label_exact(nodes, ("Containers",))
    if containers is None:
        return False
    reddit_page = (
        find_label_exact(nodes, ("Ghost for Reddit",)) is not None
        or find_label_exact(nodes, ("Formulate",)) is not None
        or find_label_exact(nodes, ("Generate",)) is not None
        or find_label_exact(nodes, ("Enable",)) is not None
    )
    if not reddit_page:
        return False
    requested = _exact_named_nodes(nodes, name)
    if (
        len(requested) != 1
        or requested[0].center()[1] <= containers.center()[1]
        or requested[0].center()[1] - containers.center()[1] > 220
    ):
        return False
    # A settings page has one active slot value.  Seeing a different canonical
    # slot anywhere on it makes the identity proof ambiguous and fail-closed.
    visible_slots = set()
    for node in nodes:
        text = collapsed_text(node.labels()).lower()
        if (
            node.ntype != "ocr-line" and node.on_screen() and node.visible
            and text[:1].isalpha() and is_slot_name(text)
        ):
            visible_slots.add(canonicalize_name(text))
    return visible_slots == {canonicalize_name(name)}


def close_ghost_organic(stack: TapStack, phone: Phone, lg: LogFn) -> bool:
    """Home, recents, flick the Ghost card. Never killall / force-stop."""
    stack.press_home()
    dump_named(stack, phone, "690_home", lg, persist=False, preview=False)
    stack.open_app_switcher()
    blob, nodes = dump_named(
        stack, phone, "691_switcher", lg, persist=False, preview=False
    )
    ghost = find_label_exact(nodes, ("Ghost",))
    closed = False
    if ghost is not None and ghost.y < 400:
        stack.interp_flick(375, 780, 375, 60)
        time.sleep(0.8)
        _, after = dump_named(
            stack, phone, "692_after_flick", lg, persist=False, preview=False
        )
        closed = find_label_exact(after, ("Ghost",)) is None
    else:
        lg("Ghost card not proven in app switcher; not guessing a card.")
    stack.press_home()
    dump_named(stack, phone, "693_springboard", lg, persist=False, preview=False)
    if closed:
        lg("CLOSE_GHOST=OK")
    else:
        lg("CLOSE_GHOST=HOME_ONLY")
    return closed


def _select_named_node(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    name: str,
    instance_id: str,
) -> None:
    nodes, named = _fresh_safe_instance_node(
        stack, phone, lg, nodes, name, instance_id
    )
    stack.check_cancelled()
    lg(f"CLICK {name} to change identifiers")
    tap_container_name(stack, named)
    stack.wait(0.9)
    blob, nodes = dump_named(stack, phone, "642_after_select", lg)
    if not _proven_reddit_settings(nodes, name):
        raise PhoneError(
            f"Reddit page does not prove exact active container {name}; "
            "stopped before Formulate, Advanced, or slot persistence."
        )


def select_recorded_container(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    name: str,
    instance_id: str,
    stop=None,
) -> str:
    """Select and locally verify one saved Ghost container identity.

    The saved identity can be the short UUID prefix persisted by older runs.
    It is trusted only after a complete, validated Ghost inventory resolves it
    to one exact instance.  Selection then reacquires that exact name/UUID row
    and relies on :func:`_select_named_node` to prove the resulting Reddit
    settings page names the requested active container.  Ghost is exited
    organically on success and after every post-open failure.
    """
    name = canonicalize_name(name)
    recorded = str(instance_id or "").strip().upper()
    if not is_slot_name(name):
        raise PhoneError(f"Bad container name {name!r}")
    if not recorded:
        raise PhoneError(
            f"Refusing to select {name} without a recorded container UUID."
        )

    # Start from a known Home state exactly like the UUID-bound deletion SOP.
    close_ghost_organic(stack, phone, lg)
    ghost_opened = False
    try:
        stack.check_cancelled()
        if stop is not None and stop.is_set():
            raise PhoneError("Stopped.")
        ghost_opened = True
        open_reddit_containers(stack, phone, lg, stop=stop)
        _blob, nodes = dump_named(stack, phone, f"640_select_scan_{name}", lg)
        stop_if_reset(nodes)
        inventory, nodes = scan_all_container_instances(
            stack, phone, lg, nodes, stem=f"640_select_inventory_{name}"
        )
        status, matched = resolve_recorded_instance(
            inventory.get(name) or set(), recorded
        )
        if status == "absent":
            raise PhoneError(
                f"Recorded UUID {recorded} for {name} is absent from the complete "
                "validated Ghost inventory; did not select another container."
            )
        if status == "ambiguous":
            raise PhoneError(
                f"Recorded UUID {recorded} for {name} matches multiple containers; "
                "refused to select an ambiguous prefix."
            )

        stack.check_cancelled()
        if stop is not None and stop.is_set():
            raise PhoneError("Stopped.")
        nodes = _reacquire_recorded_instance_frame(
            stack, phone, lg, nodes, name, matched
        )
        lg(
            f"SELECT {name}: complete inventory uniquely resolved recorded "
            f"identity to {matched}; selecting that exact row."
        )
        _select_named_node(stack, phone, lg, nodes, name, matched)
        lg(f"SELECT {name}: exact active container {matched} locally verified.")
    except Exception:
        if ghost_opened:
            try:
                close_ghost_organic(stack, phone, lg)
            except Exception as close_exc:
                lg(
                    f"SELECT {name}: organic Ghost cleanup after failure also "
                    f"failed ({type(close_exc).__name__})."
                )
        raise

    close_ghost_organic(stack, phone, lg)
    return matched


def select_named(stack: TapStack, phone: Phone, lg: LogFn, nodes, name: str) -> None:
    current, nodes = scan_named_container_instances(
        stack, phone, lg, nodes, name, stem="641_existing_scan"
    )
    if len(current) != 1:
        raise PhoneError(
            f"Existing {name} has {len(current)} UUID instances; refused ambiguous selection."
        )
    _select_named_node(stack, phone, lg, nodes, name, next(iter(current)))


def formulate_named(stack: TapStack, phone: Phone, lg: LogFn, name: str) -> None:
    stack.check_cancelled()
    blob, nodes = dump_named(stack, phone, "650_before_formulate", lg)
    stop_if_reset(nodes)
    if find_label_exact(nodes, ("Formulate",)) is None:
        spoof = find_label_exact(nodes, ("Spoofer",))
        if spoof is not None:
            lg("Formulate not on this tab -> tap Spoofer (change identifiers)")
            stack.tap_xy(*spoof.center())
            time.sleep(0.8)
            blob, nodes = dump_named(stack, phone, "650_spoofer", lg)
    if find_label_exact(nodes, ("Formulate",)) is None:
        raise PhoneError("Formulate not on screen.")
    if find_label_exact(nodes, name_keys(name)) is None:
        raise PhoneError(f"Formulate page does not show {name}.")
    stack.swipe(375, 820, 375, 780)
    time.sleep(0.4)
    blob, nodes = dump_named(stack, phone, "651_after_nudge", lg)
    form = must(nodes, ("Formulate",))
    x = min(max(form.x + 180, 240), 560)
    y = form.center()[1]
    stack.long_tap(x, y, 550)
    time.sleep(0.8)
    dump_named(stack, phone, "652_after_formulate", lg)


def advanced_named(stack: TapStack, phone: Phone, lg: LogFn) -> None:
    stack.check_cancelled()
    blob, nodes = dump_named(stack, phone, "660_before_advanced", lg)
    stop_if_reset(nodes)
    adv = must(nodes, ("Advanced",))
    stack.tap_xy(*adv.center())
    time.sleep(0.8)
    blob, nodes = dump_named(stack, phone, "661_advanced", lg, shot=True)
    if find_label_exact(nodes, ("Hide",)) is None:
        raise PhoneError("Advanced did not open (Hide not on screen).")
    stack.tap_ghost_knob(must(nodes, ("Hide",)))
    time.sleep(0.35)
    blob, nodes = dump_named(stack, phone, "662_after_hide", lg)
    pb = must(nodes, ("Pasteboard",))
    prev = prevent_under(nodes, pb.y)
    if prev is None:
        raise PhoneError("Pasteboard Prevent not on screen.")
    stack.tap_ghost_knob(prev)
    time.sleep(0.35)
    blob, nodes = dump_named(stack, phone, "663_after_pasteboard", lg)
    ap = find_label_exact(nodes, ("Appium",))
    if ap is None:
        stack.swipe(375, 1050, 375, 720)
        time.sleep(0.5)
        blob, nodes = dump_named(stack, phone, "664_advanced_scroll", lg)
        ap = find_label_exact(nodes, ("Appium",))
    if ap is None:
        raise PhoneError("Appium not on screen.")
    prev2 = prevent_under(nodes, ap.y)
    if prev2 is None:
        raise PhoneError("Appium Prevent not on screen.")
    stack.tap_ghost_knob(prev2)
    time.sleep(0.35)
    dump_named(stack, phone, "665_advanced_done", lg, shot=True)
    lg("ADVANCED=OK Hide + Pasteboard Prevent + Appium Prevent. Albums left alone.")


def create_named(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    name: str,
    *,
    baseline_ids: set[str] | None = None,
    stop=None,
) -> str:
    if baseline_ids is None:
        raise PhoneError(
            f"Refusing to create {name} without a complete pre-create UUID baseline."
        )
    # Each finite, verified container-preparation milestone counts at most once
    # per create.  Issuing a tap or text command alone is never progress; only a
    # freshly dumped screen that proves the step does.
    milestones: set[str] = set()

    def mark_milestone(label: str) -> None:
        if label in milestones:
            return
        milestones.add(label)
        CREATION_PROGRESS.note()

    nc = must(nodes, ("New Container",))
    nx, ny = nc.center()
    stack.tap_xy(nx, min(ny - 50, 1200))
    time.sleep(1.1)
    blob, nodes = dump_named(stack, phone, "625_name", lg)
    if find_label_exact(nodes, ("Create",)) is None:
        raise PhoneError("New Container tap missed; name sheet not open.")
    field = find_label(nodes, ("Enter the name",))
    if field is None:
        raise PhoneError("Name field not on screen.")
    mark_milestone("naming_dialog")
    stack.tap_row(field)
    time.sleep(0.3)
    stack.type_text_block(name)
    time.sleep(0.5)
    blob, nodes = dump_named(stack, phone, "626_named", lg)
    if find_label_exact(nodes, name_keys(name)) is None:
        lg(f"{name} not in field after type -> clear and retype once")
        stack.press_backspace(12)
        time.sleep(0.2)
        stack.type_text_block(name)
        time.sleep(0.5)
        blob, nodes = dump_named(stack, phone, "626_named_retry", lg)
    if find_label_exact(nodes, name_keys(name)) is None:
        raise PhoneError("Typed name did not appear in the field.")
    mark_milestone("name_confirmed")
    try:
        stack.tap_xy(*must(nodes, ("Create",)).center())
        time.sleep(0.9)
        # Ghost normally leaves the newly-created row visible.  Inspect that
        # fresh frame first and select the sole new UUID immediately; do not
        # issue boundary-proof flicks when the exact row is already on screen.
        after_create = dump_named(stack, phone, "627_after_create", lg)[1]
    except OperationCancelled:
        raise
    except PhoneError as exc:
        if stop is not None and stop.is_set():
            raise OperationCancelled("Stopped.") from exc
        raise ContainerProvisioningReconcileError(
            f"Ghost Create was issued for {name}, but its result could not be "
            f"locally inspected: {exc}. Preserved provisioning state."
        ) from exc
    if not on_containers_list(after_create):
        # Ghost 1.5 can return to SpringBoard after a successful Create.  That
        # frame says nothing about whether the new instance exists, so never
        # scroll it and never hand it to generic/AI recovery.  Re-enter Ghost
        # through the verified local router, capture a fresh Containers frame,
        # and compare a complete exact-name inventory with the immutable
        # baseline that was persisted before the single Create tap.
        lg(
            f"{name}: post-create frame is not proven Containers; "
            "deterministically reopening Ghost for exact UUID reconciliation."
        )
        try:
            stack.check_cancelled()
            open_reddit_containers(stack, phone, lg, stop=stop)
            stack.check_cancelled()
            _blob, reopened = dump_named(
                stack, phone, "627_reconcile_reopened", lg
            )
            stop_if_reset(reopened)
            if not on_containers_list(reopened):
                raise ContainerProvisioningReconcileError(
                    f"Ghost reopened after creating {name}, but a fresh frame did "
                    "not prove the Containers list. Preserved provisioning state."
                )
            current, reconciled_nodes = scan_named_container_instances(
                stack,
                phone,
                lg,
                reopened,
                name,
                stem="627_reconcile_reopened_scan",
            )
            instance_id = reconciled_new_instance(current, baseline_ids, name)
            if _instance_node(reconciled_nodes, name, instance_id) is None:
                raise ContainerProvisioningReconcileError(
                    f"Ghost reconciled UUID {instance_id} for {name} after reopen, "
                    "but its exact row is not visible in the verified final frame. "
                    "Preserved provisioning state without selecting or guessing."
                )
            mark_milestone("instance_reconciled")
            _select_named_node(
                stack, phone, lg, reconciled_nodes, name, instance_id
            )
            return instance_id
        except (OperationCancelled, ICloudModalBlockingError):
            raise
        except ContainerProvisioningReconcileError:
            raise
        except PhoneError as exc:
            if stop is not None and stop.is_set():
                raise OperationCancelled("Stopped.") from exc
            raise ContainerProvisioningReconcileError(
                f"Could not deterministically reopen and reconcile the newly-created "
                f"Ghost container {name}: {exc}. Preserved provisioning state."
            ) from exc

    visible_ids = {
        str(value or "").strip().upper()
        for value, _node in container_instances(after_create, name)
        if str(value or "").strip()
    }
    visible_new = visible_ids - {
        str(value or "").strip().upper()
        for value in baseline_ids
        if str(value or "").strip()
    }
    if len(visible_new) > 1:
        raise ContainerProvisioningReconcileError(
            f"Ghost create for {name} showed multiple new UUID rows in the first "
            "post-create frame; did not scroll or select an ambiguous container."
        )
    if len(visible_new) == 1:
        instance_id = next(iter(visible_new))
        if _instance_node(after_create, name, instance_id) is None:
            raise ContainerProvisioningReconcileError(
                f"Ghost create for {name} exposed UUID {instance_id} without one "
                "verified matching row; did not scroll or guess."
            )
        lg(f"{name}: new UUID row already visible -> selecting immediately without a scroll.")
        mark_milestone("instance_reconciled")
        try:
            _select_named_node(stack, phone, lg, after_create, name, instance_id)
        except OperationCancelled:
            raise
        except PhoneError as exc:
            raise ContainerProvisioningReconcileError(
                f"Ghost could not locally verify selection of newly-created {name} "
                f"UUID {instance_id}: {exc}. Preserved provisioning state."
            ) from exc
        return instance_id
    try:
        current, nodes = scan_named_container_instances_down(
            stack, phone, lg, after_create, name, stem="627_reconcile"
        )
        instance_id = reconciled_new_instance(current, baseline_ids, name)
    except OperationCancelled:
        raise
    except PhoneError as exc:
        lg(
            f"{name}: bounded post-create capture inconclusive ({exc}); "
            "re-running the complete named scan."
        )
        try:
            _blob, nodes = dump_named(stack, phone, "627_reconcile_restart", lg)
            current, nodes = scan_named_container_instances(
                stack, phone, lg, nodes, name, stem="627_reconcile_full"
            )
            instance_id = reconciled_new_instance(current, baseline_ids, name)
        except OperationCancelled:
            raise
        except ContainerProvisioningReconcileError:
            raise
        except PhoneError as full_exc:
            raise ContainerProvisioningReconcileError(
                f"Complete post-create reconciliation failed for {name}: "
                f"{full_exc}. Preserved provisioning state."
            ) from full_exc
    if _instance_node(nodes, name, instance_id) is None:
        raise ContainerProvisioningReconcileError(
            f"Ghost reconciled UUID {instance_id} for {name}, but its exact row is "
            "not visible in the verified final frame; did not select or guess."
        )
    mark_milestone("instance_reconciled")
    try:
        _select_named_node(stack, phone, lg, nodes, name, instance_id)
    except OperationCancelled:
        raise
    except PhoneError as exc:
        raise ContainerProvisioningReconcileError(
            f"Ghost could not locally verify selection of newly-created {name} "
            f"UUID {instance_id}: {exc}. Preserved provisioning state."
        ) from exc
    return instance_id


def open_reddit_containers(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    stop=None,
    *,
    _modal_recovery_used: bool = False,
) -> None:
    """Route any known Ghost entry screen to Reddit's Containers list."""
    _, nodes = dump_named(
        stack, phone, "621_now", lg, persist=False, preview=False
    )
    if apple_id_verification_modal(nodes) is not None and not _modal_recovery_used:
        nodes = dismiss_apple_id_verification_modal(stack, phone, lg, nodes)
        _modal_recovery_used = True
    elif icloud_modal_present(nodes):
        raise ICloudModalBlockingError(
            "An iCloud authentication sheet is overlaying the screen and blocks "
            "Ghost navigation; batch paused without consuming the queue item."
        )
    state, nodes = route_ghost_screen(stack, phone, lg, nodes, stop=stop)
    if state == "containers":
        lg("Already on Ghost Containers list.")
        return
    if state == "applications":
        lg("Ghost Applications picker already open.")
        blob, nodes = dump_named(stack, phone, "622_apps", lg)
    elif state == "home":
        stack.tap_row(must(nodes, ("Application List",)))
        time.sleep(0.9)
        blob, nodes = dump_named(stack, phone, "622_apps", lg)
    else:
        raise PhoneError(f"Ghost router returned unsupported state {state!r}.")
    reddit = find_label_exact(nodes, ("Reddit",))
    if reddit is None:
        user_apps = find_label_exact(nodes, ("User Apps",))
        if user_apps is not None:
            lg("Reddit not visible -> open User Apps")
            stack.tap_xy(*user_apps.center())
            time.sleep(1.2)
            blob, nodes = dump_named(stack, phone, "622_user_apps", lg)
            reddit = find_label_exact(nodes, ("Reddit",))
    if reddit is None:
        search = find_label(nodes, ("Search apps",))
        if search is not None:
            lg("Reddit not visible in User Apps -> search for Reddit")
            stack.tap_xy(*search.center())
            time.sleep(0.4)
            stack.type_text_block("Reddit")
            time.sleep(1.0)
            blob, nodes = dump_named(stack, phone, "622_search_reddit", lg)
            reddit = find_label_exact(nodes, ("Reddit",))
    if reddit is None:
        if icloud_modal_present(nodes):
            if apple_id_verification_modal(nodes) is not None and not _modal_recovery_used:
                dismiss_apple_id_verification_modal(stack, phone, lg, nodes)
                return open_reddit_containers(
                    stack,
                    phone,
                    lg,
                    stop=stop,
                    _modal_recovery_used=True,
                )
            raise ICloudModalBlockingError(
                "An iCloud authentication sheet is overlaying Ghost and blocked "
                "Reddit navigation; batch paused without consuming the queue item."
            )
        raise PhoneError("Reddit not found in Ghost Application List, User Apps, or search.")
    repair_stale_crane_reddit_active(phone, lg)
    stack.tap_row(reddit)
    time.sleep(1.1)
    blob, nodes = dump_named(stack, phone, "623_reddit", lg)
    if find_label_exact(nodes, ("Applications",)) and not find_label_exact(
        nodes, ("Containers",)
    ):
        raise PhoneError("Ghost crashed or did not open Reddit.")
    if find_label_exact(nodes, ("Weather",)) or find_label_exact(nodes, ("Find My",)):
        raise PhoneError("Ghost crashed to SpringBoard after Reddit tap. See 623_reddit.")
    stack.tap_row(must(nodes, ("Containers",)))
    time.sleep(0.8)


def back_to_containers(stack: TapStack, phone: Phone, lg: LogFn) -> None:
    blob, nodes = dump_named(stack, phone, "670_before_containers", lg)
    c = find_label_exact(nodes, ("Containers",))
    if c is None:
        raise PhoneError("Containers not on screen to create the next name.")
    stack.tap_row(c)
    time.sleep(0.8)


def ensure_new_container(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    """Get New Container on screen so we can tap Create. No name scan."""
    if find_label_exact(nodes, ("New Container",)) is not None:
        return nodes
    last: tuple[str, ...] | None = None
    stuck = 0
    for i in range(10):
        lg("New Container off-screen -> scroll bottom")
        list_flick(stack, toward_bottom=True, big=stuck >= 2)
        time.sleep(0.5)
        blob, nodes = dump_named(stack, phone, f"624_nc_{i}", lg)
        if find_label_exact(nodes, ("New Container",)) is not None:
            return nodes
        ids = viewport_ids(nodes)
        stuck = stuck + 1 if ids == last else 0
        last = ids
    raise PhoneError("New Container not on screen. See 624_nc dumps.")


def apply_one_container(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    name: str,
    stop=None,
    create: bool = True,
) -> str:
    stack.check_cancelled()
    instance_id = ""
    if create:
        baseline_ids, nodes = scan_named_container_instances(
            stack, phone, lg, nodes, name, stem=f"624_apply_scan_{name}"
        )
        nodes = ensure_new_container(stack, phone, lg, nodes)
        lg(f"Creating Ghost container {name}")
        instance_id = create_named(
            stack, phone, lg, nodes, name, baseline_ids=baseline_ids, stop=stop
        )
    else:
        lg(f"{name} already on list -> tap it, Formulate identifiers, Advanced")
        current_ids, nodes = scan_named_container_instances(
            stack, phone, lg, nodes, name, stem=f"624_apply_existing_{name}"
        )
        if len(current_ids) != 1:
            raise PhoneError(
                f"Existing {name} has {len(current_ids)} UUID instances; refused selection."
            )
        instance_id = next(iter(current_ids))
        _select_named_node(stack, phone, lg, nodes, name, instance_id)
    lg("ENABLE left alone (do not toggle).")
    stack.check_cancelled()
    formulate_named(stack, phone, lg, name)
    stack.check_cancelled()
    advanced_named(stack, phone, lg)
    stack.check_cancelled()
    upsert_ghost(name, lg)
    stack.check_cancelled()
    mark_ghost_container_ready(name, instance_id, lg)
    return instance_id


def configure_existing_slot_container(
    phone: Phone,
    stack: TapStack,
    lg: LogFn,
    nodes,
    name: str,
    instance_id: str,
    stop=None,
) -> str:
    """Finish one exact orphan container using the proven original workflow.

    The old Creator selected an existing row, formulated identifiers, and
    applied Advanced protections instead of silently skipping it.  Preserve
    that behavior while using the exact UUID and bottom frame already proven
    by the complete allocation scan.  Do not close, reopen, or rescan between
    proof and selection.
    """
    stack.check_cancelled()
    if stop and stop.is_set():
        raise OperationCancelled("Stopped.")
    try:
        _select_named_node(stack, phone, lg, nodes, name, instance_id)
        lg("ENABLE left alone (do not toggle).")
        stack.check_cancelled()
        formulate_named(stack, phone, lg, name)
        stack.check_cancelled()
        advanced_named(stack, phone, lg)
        stack.check_cancelled()
    except (OperationCancelled, ICloudModalBlockingError):
        raise
    except ContainerProvisioningReconcileError:
        raise
    except PhoneError as exc:
        raise ContainerProvisioningReconcileError(
            f"Could not deterministically configure exact existing {name} UUID "
            f"{instance_id}: {exc}. Preserved the container and slot without "
            "using Anthropic."
        ) from exc
    try:
        upsert_ghost(name, lg)
        stack.check_cancelled()
        mark_ghost_container_ready(name, instance_id, lg)
    except OperationCancelled:
        raise
    except Exception as exc:
        raise ContainerProvisioningReconcileError(
            f"Could not persist verified Ghost container {name} UUID "
            f"{instance_id}: {exc}. Preserved the container and slot without "
            "using Anthropic."
        ) from exc
    close_ghost_organic(stack, phone, lg)
    return instance_id


def provision_slot_container(
    phone: Phone,
    stack: TapStack,
    lg: LogFn,
    name: str,
    stop=None,
    *,
    inventory: dict[str, set[str]] | None = None,
    nodes=None,
) -> str:
    """Create or reconcile exactly one pending Ghost instance for a slot.

    ``needed`` saves the complete pre-create instance set before pressing
    Create.  ``provisioning`` reuses that immutable baseline after an
    interrupted run: zero new instances resumes Create, one resumes its exact
    UUID row, and any ambiguity stops without selecting an older duplicate.

    A Create is allowed only after a fresh, boundary-proven *complete* Ghost
    inventory proves that the exact name is absent.  The caller may pass that
    inventory and its proven bottom ``nodes`` frame while it is still
    unmutated.  Otherwise this function closes/reopens Ghost and obtains a new
    complete inventory itself; a cheaper exact-name-only scan is deliberately
    insufficient for creation.
    """
    name = canonicalize_name(name)
    row = get_slot(name)
    if row is None:
        raise ContainerProvisioningReconcileError(
            f"No durable slot row exists for Ghost provisioning of {name}; "
            "did not create a container or use Anthropic."
        )
    state = ghost_container_state(row)
    if state == "ready":
        return str(row.get("ghost_container_instance") or "")
    if state not in ("needed", "provisioning"):
        raise ContainerProvisioningReconcileError(
            f"Unsupported durable Ghost provisioning state {state!r} for {name}; "
            "did not create a container or use Anthropic."
        )
    if stop and stop.is_set():
        raise OperationCancelled("Stopped.")

    lg(f"{name}: reconcile newest Ghost container before Reddit creation.")
    if inventory is None:
        try:
            close_ghost_organic(stack, phone, lg)
            open_reddit_containers(stack, phone, lg, stop=stop)
            _blob, nodes = dump_named(stack, phone, f"624_provision_{name}", lg)
            stop_if_reset(nodes)
            inventory, nodes = scan_all_container_instances(
                stack, phone, lg, nodes, stem=f"624_provision_scan_{name}"
            )
        except (OperationCancelled, ICloudModalBlockingError):
            raise
        except ContainerProvisioningReconcileError:
            raise
        except PhoneError as exc:
            raise ContainerProvisioningReconcileError(
                f"Could not obtain a fresh boundary-proven complete Ghost "
                f"inventory before provisioning {name}: {exc}. Did not create "
                "a container or use Anthropic."
            ) from exc
        current_ids = {
            str(value or "").strip().upper()
            for value in (inventory.get(name) or set())
            if str(value or "").strip()
        }
    else:
        if nodes is None:
            raise ContainerProvisioningReconcileError(
                f"Reusing a complete inventory for {name} requires its proven "
                "list frame; did not create a container or use Anthropic."
            )
        current_ids = {
            str(value or "").strip().upper()
            for value in (inventory.get(name) or set())
            if str(value or "").strip()
        }
        lg(
            f"{name}: reusing the complete allocation inventory "
            f"({len(current_ids)} existing exact row(s)); skipped a second full scan."
        )

    if state == "needed":
        if current_ids:
            raise ContainerProvisioningReconcileError(
                f"Fresh complete Ghost inventory already contains {len(current_ids)} "
                f"exact {name} instance(s); refused to Create over an existing "
                "container. Preserved the slot without using Anthropic."
            )
        baseline_ids = set(current_ids)
        stack.check_cancelled()
        try:
            mark_ghost_container_provisioning(name, baseline_ids, lg)
        except Exception as exc:
            raise ContainerProvisioningReconcileError(
                f"Could not persist the pre-Create Ghost inventory proof for "
                f"{name}: {exc}. Did not create a container or use Anthropic."
            ) from exc
        new_ids: set[str] = set()
    else:
        raw_baseline = row.get("ghost_container_baseline", [])
        if not isinstance(raw_baseline, (list, tuple, set, frozenset)):
            raise ContainerProvisioningReconcileError(
                f"Durable Ghost provisioning baseline for {name} is malformed; "
                "did not create a container or use Anthropic."
            )
        baseline_ids = {
            str(value or "").strip().upper()
            for value in raw_baseline
            if str(value or "").strip()
        }
        new_ids = current_ids - baseline_ids
        if len(new_ids) > 1:
            raise ContainerProvisioningReconcileError(
                f"{name} provisioning found multiple new Ghost instances; "
                "did not choose an ambiguous duplicate or use Anthropic."
            )

    if new_ids:
        instance_id = next(iter(new_ids))
        named = _instance_node(nodes, name, instance_id)
        if named is None:
            raise ContainerProvisioningReconcileError(
                f"Newest {name} Ghost instance is not visible at the proven list bottom; "
                "did not select an older duplicate or use Anthropic."
            )
        lg(f"{name}: resume the one reconciled newest Ghost instance.")
        try:
            _select_named_node(stack, phone, lg, nodes, name, instance_id)
        except OperationCancelled:
            raise
        except PhoneError as exc:
            raise ContainerProvisioningReconcileError(
                f"Could not locally verify exact resumed {name} UUID {instance_id}: "
                f"{exc}. Preserved provisioning state without using Anthropic."
            ) from exc
    else:
        if current_ids:
            raise ContainerProvisioningReconcileError(
                f"Fresh complete Ghost inventory contains exact {name} rows but "
                "none is a uniquely reconciled new instance; refused another "
                "Create and preserved provisioning state without using Anthropic."
            )
        try:
            nodes = ensure_new_container(stack, phone, lg, nodes)
            lg(
                f"{name}: fresh complete inventory proves exact name absent; "
                "issuing the single permitted Create."
            )
            instance_id = create_named(
                stack,
                phone,
                lg,
                nodes,
                name,
                baseline_ids=baseline_ids,
                stop=stop,
            )
        except (OperationCancelled, ICloudModalBlockingError):
            raise
        except ContainerProvisioningReconcileError:
            raise
        except PhoneError as exc:
            raise ContainerProvisioningReconcileError(
                f"Could not safely complete the single local Create attempt for "
                f"{name}: {exc}. Preserved provisioning state without using "
                "Anthropic."
            ) from exc

    lg("ENABLE left alone (do not toggle).")
    stack.check_cancelled()
    try:
        formulate_named(stack, phone, lg, name)
        stack.check_cancelled()
        advanced_named(stack, phone, lg)
        stack.check_cancelled()
    except (OperationCancelled, ICloudModalBlockingError):
        raise
    except ContainerProvisioningReconcileError:
        raise
    except PhoneError as exc:
        raise ContainerProvisioningReconcileError(
            f"Could not deterministically finish Ghost configuration for exact "
            f"{name} UUID {instance_id}: {exc}. Preserved provisioning state "
            "without using Anthropic."
        ) from exc
    try:
        mark_ghost_container_ready(name, instance_id, lg)
    except Exception as exc:
        raise ContainerProvisioningReconcileError(
            f"Could not persist verified Ghost container {name} UUID "
            f"{instance_id}: {exc}. Preserved provisioning state without using "
            "Anthropic."
        ) from exc
    close_ghost_organic(stack, phone, lg)
    return instance_id


def invalidate_ghost_container_cache(stack: TapStack, lg: LogFn) -> str:
    """Stop the exact Ghost process before trusting its container inventory.

    Crane Settings can delete a container while Ghost remains alive in the
    background.  On the live device Ghost then continued to render its cached
    deleted row even though Crane's preferences had already proved that UUID
    absent.  ``TapStack.clear_app_memory`` is bundle-bound and verifies that
    the exact process stopped, so a subsequent Ghost open must reload Crane's
    current inventory instead of carrying that stale view into Create.

    This is deliberately local and deterministic.  A failed or unavailable
    cache clear is a hard provisioning failure; it is never delegated to AI.
    """
    stack.check_cancelled()
    clear = getattr(stack, "clear_app_memory", None)
    if not callable(clear):
        raise ContainerProvisioningReconcileError(
            "Ghost inventory refresh requires verified exact-bundle process control; "
            "preserved the provisioning checkpoint without using Anthropic."
        )
    try:
        outcome = str(clear(GHOST_BUNDLE) or "").strip().lower()
    except OperationCancelled:
        raise
    except Exception as exc:
        raise ContainerProvisioningReconcileError(
            "Ghost's exact process cache could not be invalidated before reading "
            "the container inventory; preserved the provisioning checkpoint "
            "without using Anthropic."
        ) from exc
    if outcome not in {"already-stopped", "graceful-quit", "force-close"}:
        raise ContainerProvisioningReconcileError(
            f"Ghost inventory refresh returned unverified outcome {outcome!r}; "
            "preserved the provisioning checkpoint without using Anthropic."
        )
    lg(
        "GHOST_CACHE_INVALIDATED=" + outcome
        + "; next container inventory will be loaded by a fresh Ghost process."
    )
    return outcome


def apply_reddit_containers(
    phone: Phone,
    stack: TapStack,
    lg: LogFn,
    count: int = 1,
    stop=None,
    close: bool = True,
    names: list[str] | None = None,
) -> list[str]:
    """Allocate and create Ghost containers from a complete device inventory."""
    created: list[str] = []
    skipped: list[str] = []
    reused: list[str] = []
    count = max(1, min(int(count), 10))

    # A preceding cleanup deletes through Crane Settings, not through Ghost.
    # Invalidate Ghost's process cache before the mandatory complete inventory
    # read.  This also makes Retry / Resume of a provisioning slot
    # safe when Stop left Crane's red Delete action exposed: only Ghost's exact
    # bundle is stopped here, then normal routing leaves Crane via Home without
    # tapping the exposed action.
    invalidate_ghost_container_cache(stack, lg)

    lg(
        "Close leftover Ghost, then prove a fresh complete Reddit container "
        "inventory before any Create."
    )
    try:
        close_ghost_organic(stack, phone, lg)
        stack.check_cancelled()
        open_reddit_containers(stack, phone, lg, stop=stop)
        _blob, nodes = dump_named(stack, phone, "624_all_containers", lg)
        stop_if_reset(nodes)
        inventory, _nodes = scan_all_container_instances(
            stack, phone, lg, nodes, stem="624_allocation_scan"
        )
    except (OperationCancelled, ICloudModalBlockingError):
        raise
    except ContainerProvisioningReconcileError:
        raise
    except PhoneError as exc:
        raise ContainerProvisioningReconcileError(
            "Could not obtain a fresh boundary-proven complete Ghost inventory "
            f"before container allocation: {exc}. Did not create a container or "
            "use Anthropic."
        ) from exc
    device_names = set(inventory)

    if names:
        todo = [canonicalize_name(n) for n in names if str(n).strip()]
    else:
        # Phone-side names reserve ids even when slots.json and retired.json do
        # not know about them (for example a PC-only deletion made in the past).
        extra: set[object] = set(slot_ids_used()) | device_names
        todo = allocate_new_names(count, extra)

    # The complete allocation scan above is a valid, fresh inventory: it visited
    # every row and ended at the proven bottom, and nothing has mutated the list
    # yet.  The first unmutated target may reuse it instead of closing/reopening
    # Ghost and running a second complete inventory scan.  As soon as one target
    # mutates the list the inventory is invalidated and a new complete scan is
    # required before another Create.
    reuse_inventory = inventory is not None

    for name in todo:
        stack.check_cancelled()
        if not is_slot_name(name):
            raise ContainerProvisioningReconcileError(
                f"Bad deterministic container name {name!r}; did not create a "
                "container or use Anthropic."
            )
        if name in device_names:
            reuse_inventory = False
            current_ids = set(inventory.get(name) or set())
            if len(current_ids) != 1:
                raise ContainerProvisioningReconcileError(
                    f"Existing {name} has {len(current_ids)} UUID instances; "
                    "refused to guess between duplicate containers or use Anthropic."
                )
            try:
                row = ensure_ghost_container_needed(name, lg)
            except Exception as exc:
                raise ContainerProvisioningReconcileError(
                    f"Could not persist/reconcile the durable slot for existing "
                    f"{name}: {exc}. Preserved the container and queue without "
                    "using Anthropic."
                ) from exc
            saved_instance = str(
                row.get("ghost_container_instance") or ""
            ).strip().upper()
            only_instance = next(iter(current_ids))
            if ghost_container_state(row) == "ready" and saved_instance == only_instance:
                lg(f"{name} exact UUID is already configured and ready; left untouched.")
                skipped.append(name)
                if unretire_slot_id(name):
                    lg(f"{name}: retired reservation released for verified ready container.")
                continue

            lg(
                f"{name} exists as one incomplete Ghost container; selecting its "
                "exact UUID and completing Formulate + Advanced."
            )
            instance_id = configure_existing_slot_container(
                phone, stack, lg, _nodes, name, only_instance, stop=stop
            )
            if instance_id != only_instance:
                raise ContainerProvisioningReconcileError(
                    f"Existing {name} UUID changed during configuration; preserved "
                    "the container and slot without using Anthropic."
                )
            inventory[name] = {instance_id}
            reused.append(name)
            created.append(name)
            if unretire_slot_id(name):
                lg(f"{name}: retired PC-only reservation released after verified setup.")
            continue

        # Persist a resumable needed/provisioning checkpoint before Create.
        # No phone container is deleted, renamed, merged, or guessed here.
        stack.check_cancelled()
        try:
            row = ensure_ghost_container_needed(name, lg)
        except Exception as exc:
            raise ContainerProvisioningReconcileError(
                f"Could not persist/reconcile the durable slot before provisioning "
                f"{name}: {exc}. Did not create a container or use Anthropic."
            ) from exc
        state = ghost_container_state(row)
        if state == "ready":
            raise ContainerProvisioningReconcileError(
                f"Slot {name} is marked Ghost-ready but was absent from the complete "
                "phone scan; stopped without creating a duplicate or using Anthropic."
            )
        stack.check_cancelled()
        instance_id = provision_slot_container(
            phone,
            stack,
            lg,
            name,
            stop=stop,
            inventory=inventory if reuse_inventory else None,
            nodes=_nodes if reuse_inventory else None,
        )
        reuse_inventory = False
        inventory.setdefault(name, set()).add(instance_id)
        device_names.add(name)
        created.append(name)
        if unretire_slot_id(name):
            lg(f"{name}: retired PC-only reservation released after verified creation.")

    if close:
        stack.check_cancelled()
        close_ghost_organic(stack, phone, lg)
    lg("GHOST_SKIP=" + ",".join(skipped))
    lg("GHOST_REUSED=" + ",".join(reused))
    lg("GHOST_CONTAINERS=" + ",".join(created))
    lg("Do not open Reddit signup.")
    return created


def parse_names(argv: list[str]) -> list[str] | None:
    if "--names" in argv:
        i = argv.index("--names")
        return [canonicalize_name(p) for p in argv[i + 1].split(",") if p.strip()]
    if "--from" in argv and "--to" in argv:
        a = str(argv[argv.index("--from") + 1]).strip().lower()
        b = str(argv[argv.index("--to") + 1]).strip().lower()
        try:
            return name_span(a, b)
        except ValueError as e:
            raise PhoneError(str(e)) from e
    return None


def main() -> int:
    argv = [a for a in sys.argv[1:] if a]
    count = 1
    names = None
    try:
        names = parse_names(argv)
    except PhoneError as e:
        print("ERROR", e)
        return 1
    if names is None:
        if "--count" in argv:
            count = int(argv[argv.index("--count") + 1])
        elif argv and argv[0].isdigit():
            count = int(argv[0])
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        apply_reddit_containers(phone, stack, log, count=count, names=names)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
