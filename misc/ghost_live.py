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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import GHOST_BUNDLE  # noqa: E402
from slots import (  # noqa: E402
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
from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import (  # noqa: E402
    TapStack,
    collapsed_text,
    dump_named,
    find_label,
    find_label_exact,
)

os.environ["SKIP_SHOT"] = "1"

LogFn = Callable[[str], None]
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
    if find_label_exact(nodes, ("New Container",)) is not None:
        return True
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
    previous = _container_scan_signature(nodes)
    unchanged = 0
    # A 100-slot batch can require roughly twenty full upward flicks before
    # the first top frame.  Keep enough room for the two additional unchanged
    # frames that prove the boundary instead of failing exactly as the top is
    # reached on a long list.
    for index in range(32):
        stack.check_cancelled()
        list_flick(stack, toward_bottom=False, big=True)
        _blob, nodes = dump_named(stack, phone, f"{stem}_top_{index}", lg)
        if not on_containers_list(nodes):
            raise PhoneError("Ghost Containers list disappeared during complete scan.")
        _collect_all_instances(nodes, seen)
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
    previous = _container_scan_signature(nodes)
    unchanged = 0
    for index in range(32):
        stack.check_cancelled()
        list_flick(stack, toward_bottom=True, big=True)
        _blob, nodes = dump_named(stack, phone, f"{stem}_bottom_{index}", lg)
        if not on_containers_list(nodes):
            raise PhoneError("Ghost Containers list disappeared during complete scan.")
        frame_complete = _collect_all_instances(nodes, seen)
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
        raise PhoneError(
            f"Ghost create for {name} produced {len(new_ids)} uniquely reconciled "
            "new instances; expected exactly one. Did not select a duplicate."
        )
    return next(iter(new_ids))


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
) -> str:
    if baseline_ids is None:
        raise PhoneError(
            f"Refusing to create {name} without a complete pre-create UUID baseline."
        )
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
    stack.tap_xy(*must(nodes, ("Create",)).center())
    time.sleep(0.9)
    current, nodes = scan_named_container_instances(
        stack,
        phone,
        lg,
        nodes=dump_named(stack, phone, "627_after_create", lg)[1],
        name=name,
        stem="627_reconcile",
    )
    instance_id = reconciled_new_instance(current, baseline_ids, name)
    _select_named_node(stack, phone, lg, nodes, name, instance_id)
    return instance_id


def open_reddit_containers(stack: TapStack, phone: Phone, lg: LogFn, stop=None) -> None:
    """Route any known Ghost entry screen to Reddit's Containers list."""
    _, nodes = dump_named(
        stack, phone, "621_now", lg, persist=False, preview=False
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
            stack, phone, lg, nodes, name, baseline_ids=baseline_ids
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
        raise PhoneError("Stopped.")
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
    close_ghost_organic(stack, phone, lg)
    return instance_id


def provision_slot_container(
    phone: Phone,
    stack: TapStack,
    lg: LogFn,
    name: str,
    stop=None,
) -> str:
    """Create or reconcile exactly one pending Ghost instance for a slot.

    ``needed`` saves the complete pre-create instance set before pressing
    Create.  ``provisioning`` reuses that immutable baseline after an
    interrupted run: zero new instances resumes Create, one resumes its exact
    UUID row, and any ambiguity stops without selecting an older duplicate.
    """
    name = canonicalize_name(name)
    row = get_slot(name)
    if row is None:
        raise PhoneError(f"No slot {name}.")
    state = ghost_container_state(row)
    if state == "ready":
        return str(row.get("ghost_container_instance") or "")
    if state not in ("needed", "provisioning"):
        raise PhoneError(f"Unsupported Ghost provisioning state {state!r} for {name}.")
    if stop and stop.is_set():
        raise PhoneError("Stopped.")

    lg(f"{name}: reconcile newest Ghost container before Reddit creation.")
    close_ghost_organic(stack, phone, lg)
    open_reddit_containers(stack, phone, lg, stop=stop)
    _blob, nodes = dump_named(stack, phone, f"624_provision_{name}", lg)
    stop_if_reset(nodes)
    current_ids, nodes = scan_named_container_instances(
        stack, phone, lg, nodes, name, stem=f"624_provision_scan_{name}"
    )

    if state == "needed":
        baseline_ids = set(current_ids)
        stack.check_cancelled()
        try:
            mark_ghost_container_provisioning(name, baseline_ids, lg)
        except ValueError as exc:
            raise PhoneError(str(exc)) from exc
        new_ids: set[str] = set()
    else:
        baseline_ids = {
            str(value or "").strip().upper()
            for value in row.get("ghost_container_baseline", [])
            if str(value or "").strip()
        }
        new_ids = current_ids - baseline_ids
        if len(new_ids) > 1:
            raise PhoneError(
                f"{name} provisioning found multiple new Ghost instances; "
                "did not choose an ambiguous duplicate."
            )

    if new_ids:
        instance_id = next(iter(new_ids))
        named = _instance_node(nodes, name, instance_id)
        if named is None:
            raise PhoneError(
                f"Newest {name} Ghost instance is not visible at the proven list bottom; "
                "did not select an older duplicate."
            )
        lg(f"{name}: resume the one reconciled newest Ghost instance.")
        _select_named_node(stack, phone, lg, nodes, name, instance_id)
    else:
        nodes = ensure_new_container(stack, phone, lg, nodes)
        lg(f"Creating one new Ghost container {name} despite older duplicates")
        instance_id = create_named(
            stack,
            phone,
            lg,
            nodes,
            name,
            baseline_ids=baseline_ids,
        )

    lg("ENABLE left alone (do not toggle).")
    stack.check_cancelled()
    formulate_named(stack, phone, lg, name)
    stack.check_cancelled()
    advanced_named(stack, phone, lg)
    stack.check_cancelled()
    try:
        mark_ghost_container_ready(name, instance_id, lg)
    except ValueError as exc:
        raise PhoneError(str(exc)) from exc
    close_ghost_organic(stack, phone, lg)
    return instance_id


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

    lg("Close leftover Ghost, then scan the complete Reddit container list.")
    close_ghost_organic(stack, phone, lg)
    stack.check_cancelled()
    open_reddit_containers(stack, phone, lg, stop=stop)
    _blob, nodes = dump_named(stack, phone, "624_all_containers", lg)
    stop_if_reset(nodes)
    inventory, _nodes = scan_all_container_instances(
        stack, phone, lg, nodes, stem="624_allocation_scan"
    )
    device_names = set(inventory)

    if names:
        todo = [canonicalize_name(n) for n in names if str(n).strip()]
    else:
        # Phone-side names reserve ids even when slots.json and retired.json do
        # not know about them (for example a PC-only deletion made in the past).
        extra: set[object] = set(slot_ids_used()) | device_names
        todo = allocate_new_names(count, extra)

    for name in todo:
        stack.check_cancelled()
        if not is_slot_name(name):
            raise PhoneError(f"Bad container name {name!r}")
        if name in device_names:
            current_ids = set(inventory.get(name) or set())
            if len(current_ids) != 1:
                raise PhoneError(
                    f"Existing {name} has {len(current_ids)} UUID instances; "
                    "refused to guess between duplicate containers."
                )
            try:
                row = ensure_ghost_container_needed(name, lg)
            except ValueError as exc:
                raise PhoneError(str(exc)) from exc
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
                raise PhoneError(
                    f"Existing {name} UUID changed during configuration; stopped."
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
        except ValueError as exc:
            raise PhoneError(str(exc)) from exc
        state = ghost_container_state(row)
        if state == "ready":
            raise PhoneError(
                f"Slot {name} is marked Ghost-ready but was absent from the complete "
                "phone scan; stopped without creating a duplicate."
            )
        stack.check_cancelled()
        instance_id = provision_slot_container(
            phone, stack, lg, name, stop=stop
        )
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
