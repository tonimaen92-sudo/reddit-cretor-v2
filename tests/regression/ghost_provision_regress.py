# UTF-8
"""Offline checks for newest-instance Ghost reconciliation; no phone I/O."""

from __future__ import annotations

import os
import plistlib
import sys
from friend_poster.device import ghost_live  # noqa: E402
from friend_poster.device.ui_tree import Node  # noqa: E402


def node(text: str, x: int, center_y: int, w: int = 120, h: int = 20) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=center_y - h // 2, w=w, h=h)


def check(name: str, condition: bool) -> None:
    print(("PASS " if condition else "FAIL ") + name, flush=True)
    if not condition:
        raise AssertionError(name)


class FakePhone:
    def __init__(self, prefs: dict):
        self.path = ghost_live.CRANE_PREFS_PATHS[0]
        self.files = {self.path: plistlib.dumps(prefs, fmt=plistlib.FMT_BINARY)}
        self.commands: list[str] = []

    def sftp_read_bytes(self, path: str) -> bytes:
        return self.files[path]

    def sftp_write_bytes(self, path: str, data: bytes) -> None:
        self.files[path] = data

    def exec_ok(self, command: str, timeout: int = 0):
        self.commands.append(command)
        return 0, ""


class FakeStack:
    def __init__(self):
        self.taps: list[tuple[int, int]] = []
        self.flicks: list[tuple[int, int, int, int]] = []
        self.cleared_bundles: list[str] = []

    def check_cancelled(self) -> None:
        return None

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((int(x), int(y)))

    def interp_flick(self, x1, y1, x2, y2, **_kwargs) -> None:
        self.flicks.append((x1, y1, x2, y2))

    def wait(self, _seconds: float) -> None:
        return None

    def clear_app_memory(self, bundle_id: str) -> str:
        self.cleared_bundles.append(bundle_id)
        return "already-stopped"


def main() -> int:
    check(
        "uuid-first-separator-space-is-normalized",
        ghost_live._instance_id(
            "A1929EFD 4545-41F7-8975-039A272144.."
        ) == "A1929EFD",
    )
    check(
        "uuid-first-separator-hyphen-remains-supported",
        ghost_live._instance_id(
            "A1929EFD-4545-41F7-8975-039A272144.."
        ) == "A1929EFD",
    )
    check(
        "uuid-cyrillic-homoglyphs-use-live-a0011-prefix",
        ghost_live._instance_id(
            "ДСАЕВА9А-2110-4913-B1BF-9AD5F687DF"
        ) == "4CAEBA9A",
    )
    check(
        "uuid-cyrillic-ve-uses-live-a0010-prefix",
        ghost_live._instance_id(
            "В23BCEA3-5A84-46F3-B097-8ADE242E1..."
        ) == "B23BCEA3",
    )
    rejected_uuid_text = (
        "A1929EFD4545-41F7-8975-039A272144..",
        "A1929 EFD-4545-41F7-8975-039A272144..",
        "arbitrary A1929EFD-4545-41F7-8975-039A272144",
        "A1929EFD-4545-41F7",
        "ПРИВЕТ-МИР-ЭТО-НЕ-UUID",
        "a0008",
    )
    check(
        "uuid-normalization-remains-structurally-bounded",
        all(ghost_live._instance_id(value) is None for value in rejected_uuid_text),
    )

    # Immutable copies of the live 21:07 Ghost frames. Runtime dumps use these
    # same filenames and must never be the regression suite's mutable fixtures.
    live_top = [
        node("Containers", 175, 106, 287, 38),
        node("a0001", 204, 362, 116, 27),
        node("55E31259-C54E-4FD1-8589-AFC56504D...", 389, 390, 471, 20),
        node("a0002", 207, 493, 115, 25),
        node("167ED10F-5BD7-42C2-9870-8E2F976788...", 392, 521, 478, 20),
        node("a0003", 207, 624, 115, 25),
        node("8C483F5F-4897-48DC-A7A6-6D483B05E...", 386, 651, 465, 18),
        node("a0004", 209, 757, 118, 23),
        node("5AB99B7D-D0A8-4DB4-A0E7-F46FB236E...", 394, 786, 481, 20),
        node("New Container", 375, 1263, 180, 18),
    ]
    live_failure = [
        node("Containers", 175, 106, 287, 38),
        node("a0005", 207, 156, 122, 25),
        node("5C943A74-D4CA-4F4C-BA01-BF5947CEB...", 395, 186, 478, 18),
        node("a0006", 208, 290, 116, 25),
        node("77949F13-DD66-45B3-BCO9-4D87E9170...", 389, 317, 472, 23),
        node("a0007", 206, 420, 118, 28),
        node("29F516DD-DD5-4BAC-92BA-34221909A...", 392, 449, 472, 20),
        node("a0008", 208, 552, 116, 27),
        node("09DCB502-5C8B-4C26-A63D-D78AE0F2...", 389, 581, 472, 20),
        node("a0008", 209, 683, 118, 25),
        node("A1929EFD-4545-41F7-8975-039A272144...", 392, 713, 478, 18),
        node("a0009", 207, 814, 115, 25),
        node("E1D2FF69-216B-4EOB-B2A5-43FB2BODD...", 386, 843, 465, 20),
        node("a0010", 206, 945, 112, 27),
        node("B23BCEA3-5A84-46F3-B097-8ADE242E1...", 389, 974, 471, 20),
        node("a0011", 204, 1078, 109, 23),
        node("ДСАЕВА9А-2110-4913-B1BF-9AD5F687DF", 387, 1103, 462, 18),
        node("New Container", 375, 1263, 180, 18),
    ]
    live_pairs = ghost_live.all_container_instances(live_failure)
    check(
        "live-cyrillic-frame-pairs-every-visible-row",
        len(live_pairs) == 8,
    )
    check(
        "live-cyrillic-frame-pairs-exact-a0011-instance",
        {
            instance_id
            for name, instance_id, _row in live_pairs
            if name == "a0011"
        }
        == {"4CAEBA9A"},
    )
    check(
        "live-cyrillic-frame-preserves-both-a0008-instances",
        {
            instance_id
            for name, instance_id, _row in live_pairs
            if name == "a0008"
        }
        == {"09DCB502", "A1929EFD"},
    )
    live_seen: dict[str, set[str]] = {}
    check(
        "live-cyrillic-frame-is-complete-for-all-instance-collection",
        ghost_live._collect_all_instances(live_failure, live_seen)
        and live_seen.get("a0011") == {"4CAEBA9A"}
        and live_seen.get("a0008") == {"09DCB502", "A1929EFD"},
    )

    # Immutable copy of the exact a0031-a0038 bottom frame.  The trailing
    # checkmark enlarged a0038's UUID OCR box upward so it overlaps the name
    # box, while its center remains the normal 20 points below the name.
    overlap_frame = [
        node("Containers", 175, 106, 287, 38),
        node("a0031", 206, 138, 112, 26),
        node("29737F2F-C229-45B9-A904-AE71FAA55300", 394, 169, 481, 20),
        node("a0032", 207, 272, 115, 23),
        node("7892070A-45F8-4641-9CD7-574103CAE000", 389, 303, 471, 18),
        node("a0033", 209, 401, 118, 26),
        node("8C2C7011-3CAD-47AB-932E-5F739DEC9000", 386, 434, 459, 15),
        node("a0034", 207, 534, 119, 24),
        node("472667CC-6C19-4E33-8E45-97FD55456000", 389, 575, 478, 25),
        node("a0035", 208, 665, 116, 24),
        node("35353535-1111-4222-8333-444455556666", 389, 695, 478, 20),
        node("a0036", 208, 797, 116, 24),
        node("36363636-1111-4222-8333-444455556666", 389, 827, 478, 20),
        node("a0037", 208, 929, 116, 24),
        node("37373737-1111-4222-8333-444455556666", 389, 959, 478, 20),
        Node(ntype="ocr", text="a0038", x=150, y=1059, w=115, h=25),
        Node(
            ntype="ocr",
            text="7F97FC7C-FE1C-4011-8147-4EC05FE77072 V",
            x=150, y=1072, w=537, h=38,
        ),
        node("New Container", 375, 1263, 180, 18),
    ]
    overlap_named = ghost_live.container_instances(overlap_frame, "a0038")
    overlap_all = ghost_live.all_container_instances(overlap_frame)
    check(
        "overlapping-a0038-box-pairs-by-downward-center",
        {instance_id for instance_id, _row in overlap_named} == {"7F97FC7C"},
    )
    check(
        "all-instances-pairs-complete-a0031-through-a0038-frame",
        len(overlap_all) == 8
        and {
            instance_id for name, instance_id, _row in overlap_all
            if name == "a0038"
        } == {"7F97FC7C"},
    )
    overlap_seen: dict[str, set[str]] = {}
    check(
        "collector-accepts-complete-overlapping-a0038-frame",
        ghost_live._collect_all_instances(overlap_frame, overlap_seen)
        and overlap_seen.get("a0038") == {"7F97FC7C"},
    )

    # Exact preserved a0055 failure shape: OCR dropped C0 from the saved
    # CCABAC05 first UUID group.  It is safe only when reconciled against that
    # same slot's durable ready identity.
    short_uuid_frame = [
        node("Containers", 175, 106, 287, 38),
        node("a0054", 207, 809, 115, 25),
        node("A0935F10-7D3E-4C7F-A713-DD6418B5E000", 390, 837, 462, 20),
        node("a0055", 208, 940, 116, 26),
        node("CCABA5-0EE5-49FA-B81A-7A125E404000", 390, 970, 474, 18),
        node("a0056", 208, 1071, 116, 26),
        node("6CC64642-A161-434B-B7EA-86CFF9992000", 381, 1098, 456, 18),
        node("New Container", 375, 1263, 180, 18),
    ]
    original_get_slot = ghost_live.get_slot
    try:
        ghost_live.get_slot = lambda name: (
            {
                "id": "a0055", "ghost": True,
                "ghost_container_state": "ready",
                "ghost_container_instance": "CCABAC05",
            }
            if name == "a0055" else None
        )
        short_pairs = ghost_live.all_container_instances(short_uuid_frame)
        check(
            "saved-identity-reconciles-two-character-uuid-ocr-deletion",
            {
                instance_id for name, instance_id, _row in short_pairs
                if name == "a0055"
            } == {"CCABAC05"},
        )
        short_seen: dict[str, set[str]] = {}
        check(
            "collector-accepts-reconciled-a0055-short-uuid-frame",
            ghost_live._collect_all_instances(short_uuid_frame, short_seen)
            and short_seen.get("a0055") == {"CCABAC05"},
        )
        ghost_live.get_slot = lambda _name: None
        check(
            "short-uuid-is-rejected-without-saved-exact-identity",
            not any(name == "a0055" for name, _instance, _row in (
                ghost_live.all_container_instances(short_uuid_frame)
            )),
        )
        ghost_live.get_slot = lambda name: (
            {
                "id": "a0055", "ghost": True,
                "ghost_container_state": "ready",
                "ghost_container_instance": "ZZZZZZZZ",
            }
            if name == "a0055" else None
        )
        check(
            "short-uuid-is-rejected-when-not-a-saved-id-deletion",
            not any(name == "a0055" for name, _instance, _row in (
                ghost_live.all_container_instances(short_uuid_frame)
            )),
        )
    finally:
        ghost_live.get_slot = original_get_slot

    def pairing_frame(uuid_center_y: int, uuid_text: str, *, intervening=False):
        frame = [
            node("Containers", 175, 106, 287, 38),
            node("a0038", 207, 500, 115, 25),
        ]
        if intervening:
            frame.append(node("a0039", 207, 515, 115, 25))
        frame.extend([
            node(uuid_text, 389, uuid_center_y, 478, 20),
            node("New Container", 375, 1263, 180, 18),
        ])
        return frame

    strict_uuid = "7F97FC7C-FE1C-4011-8147-4EC05FE77072"
    check(
        "uuid-center-equal-to-name-is-rejected",
        ghost_live.container_instances(pairing_frame(500, strict_uuid), "a0038") == [],
    )
    check(
        "uuid-center-above-name-is-rejected",
        ghost_live.container_instances(pairing_frame(499, strict_uuid), "a0038") == [],
    )
    check(
        "uuid-center-more-than-seventy-below-is-rejected",
        ghost_live.container_instances(pairing_frame(571, strict_uuid), "a0038") == [],
    )
    check(
        "malformed-subtitle-is-rejected",
        ghost_live.container_instances(
            pairing_frame(520, "7F97FC7C malformed arbitrary text"), "a0038"
        ) == [],
    )
    check(
        "intervening-slot-row-blocks-uuid-pairing",
        ghost_live.container_instances(
            pairing_frame(530, strict_uuid, intervening=True), "a0038"
        ) == [],
    )
    check(
        "absent-subtitle-is-rejected",
        ghost_live.container_instances(
            [node("Containers", 175, 106, 287, 38),
             node("a0038", 207, 500, 115, 25),
             node("New Container", 375, 1263, 180, 18)],
            "a0038",
        ) == [],
    )

    duplicate_overlap = overlap_frame[:-1] + [
        node("a0038", 207, 1140, 115, 25),
        node("38383838-1111-4222-8333-444455556666", 389, 1160, 478, 20),
        overlap_frame[-1],
    ]
    check(
        "duplicate-name-preserves-both-distinct-instance-ids",
        {value for value, _row in ghost_live.container_instances(
            duplicate_overlap, "a0038"
        )} == {"7F97FC7C", "38383838"},
    )
    clipped = [
        node("Containers", 175, 106, 287, 38),
        node("a0038", 207, 1140, 115, 25),
        node("New Container", 375, 1263, 180, 18),
    ]
    clipped_seen: dict[str, set[str]] = {}
    check(
        "clipped-edge-missing-subtitle-remains-incomplete-not-fatal",
        not ghost_live._collect_all_instances(clipped, clipped_seen)
        and "a0038" not in clipped_seen,
    )

    overlap_scan_frames = iter(
        (live_top, live_top, overlap_frame, overlap_frame, overlap_frame)
    )
    overlap_scan_stack = FakeStack()
    original_live_dump = ghost_live.dump_named
    try:
        ghost_live.dump_named = (
            lambda *_args, **_kwargs: ("", next(overlap_scan_frames))
        )
        overlap_inventory, overlap_final_nodes = (
            ghost_live.scan_all_container_instances(
                overlap_scan_stack,
                object(),
                lambda _message: None,
                live_top,
                stem="overlap_allocation_scan",
            )
        )
    finally:
        ghost_live.dump_named = original_live_dump
    check(
        "overlap-allocation-scan-reaches-bottom-without-row-tap",
        overlap_final_nodes is overlap_frame
        and overlap_inventory.get("a0038") == {"7F97FC7C"}
        and overlap_scan_stack.taps == [],
    )

    scan_frames = iter(
        (live_top, live_top, live_failure, live_failure, live_failure)
    )
    scan_stack = FakeStack()
    original_live_dump = ghost_live.dump_named
    try:
        ghost_live.dump_named = lambda *_args, **_kwargs: ("", next(scan_frames))
        live_inventory, live_final_nodes = ghost_live.scan_all_container_instances(
            scan_stack,
            object(),
            lambda _message: None,
            live_top,
            stem="live_allocation_scan",
        )
    finally:
        ghost_live.dump_named = original_live_dump
    check(
        "live-allocation-scan-reaches-stable-bottom-without-tapping",
        live_final_nodes is live_failure
        and live_inventory.get("a0011") == {"4CAEBA9A"}
        and live_inventory.get("a0008") == {"09DCB502", "A1929EFD"}
        and scan_stack.taps == []
        and len(scan_stack.flicks) == 5,
    )

    low = node("a0007", 150, 1148, 140, 24)
    tap_stack = FakeStack()
    ghost_live.tap_container_name(tap_stack, low)
    tx, ty = tap_stack.taps[-1]
    check(
        "low-row-tap-stays-strictly-inside-ocr-rectangle",
        low.x < tx < low.x + low.w and low.y < ty < low.y + low.h,
    )
    check("low-row-is-never-shifted-to-1098", ty != 1098 and ty == 1148)
    preceding = node("a0006", 150, 1048, 140, 24)
    check(
        "preceding-row-cannot-receive-low-row-tap",
        not (preceding.y < ty < preceding.y + preceding.h),
    )

    before = [
        node("Containers", 170, 107, 280, 42),
        node("1", 160, 360, 28),
        node("916BDD66-346D-4B75-9FA5-C28FEFE7E...", 380, 390, 470),
        node("1", 160, 492, 28),
        node("232ACFF9-342D-4E15-99F7-28418DCE5...", 380, 521, 470),
        node("New Container", 370, 1263, 180),
    ]
    after = before[:-1] + [
        node("1", 160, 620, 28),
        node("A1B2C3D4-1111-2222-3333-444455556666", 380, 649, 470),
        before[-1],
    ]
    baseline = {value for value, _row in ghost_live.container_instances(before, "a0001")}
    current = {value for value, _row in ghost_live.container_instances(after, "a0001")}
    check("bare-one-pairs-only-on-proven-ghost-list", baseline == {"916BDD66", "232ACFF9"})
    check("one-new-instance-is-unambiguous", current - baseline == {"A1B2C3D4"})
    newest = ghost_live._instance_node(after, "a0001", "A1B2C3D4")
    check("reconciled-instance-selects-newest-row", newest is not None and newest.center()[1] == 620)

    unproven = [
        item for item in after if item.text not in ("Containers", "New Container")
    ]
    check(
        "bare-one-is-not-global-slot-proof",
        ghost_live.container_instances(unproven, "a0001") == [],
    )

    wrong_settings = [
        node("Ghost for Reddit", 180, 70, 320, 30),
        node("Containers", 170, 250, 220, 30),
        node("a0006", 180, 300, 140, 24),
        node("Generate", 180, 500, 180, 30),
    ]
    check(
        "wrong-settings-active-value-is-rejected",
        not ghost_live._proven_reddit_settings(wrong_settings, "a0007"),
    )
    select_frame = [
        node("Containers", 170, 107, 280, 42),
        node("a0007", 160, 600, 140, 24),
        node("A7A7A7A7-1111-2222-3333-444455556666", 370, 635, 470),
        node("New Container", 370, 1263, 180),
    ]
    original_dump = ghost_live.dump_named
    try:
        ghost_live.dump_named = lambda *_a, **_k: ("", wrong_settings)
        mismatch_stack = FakeStack()
        mismatch_failed = False
        try:
            ghost_live._select_named_node(
                mismatch_stack, object(), lambda _m: None,
                select_frame, "a0007", "A7A7A7A7",
            )
        except ghost_live.PhoneError:
            mismatch_failed = True
        check(
            "post-selection-mismatch-fails-after-one-exact-row-tap",
            mismatch_failed and len(mismatch_stack.taps) == 1,
        )
    finally:
        ghost_live.dump_named = original_dump

    missing_pair_failed = False
    try:
        ghost_live._fresh_safe_instance_node(
            FakeStack(), object(), lambda _m: None,
            select_frame, "a0007", "DOESNOT1",
        )
    except ghost_live.PhoneError:
        missing_pair_failed = True
    check("missing-uuid-name-pairing-fails-closed", missing_pair_failed)

    # Exercise the provisioning boundary: a failed post-selection identity
    # proof must prevent every configuration and persistence callback.
    originals = {
        name: getattr(ghost_live, name)
        for name in (
            "get_slot", "close_ghost_organic", "open_reddit_containers",
            "dump_named", "scan_all_container_instances",
            "_select_named_node", "formulate_named", "advanced_named",
            "mark_ghost_container_ready",
        )
    }
    later: list[str] = []
    try:
        ghost_live.get_slot = lambda _name: {
            "id": "a0007", "ghost_container_state": "provisioning",
            "ghost_container_baseline": ["OLD00001"],
        }
        ghost_live.close_ghost_organic = lambda *_a, **_k: True
        ghost_live.open_reddit_containers = lambda *_a, **_k: None
        ghost_live.dump_named = lambda *_a, **_k: ("", select_frame)
        ghost_live.scan_all_container_instances = (
            lambda *_a, **_k: (
                {"a0007": {"OLD00001", "A7A7A7A7"}}, select_frame
            )
        )
        ghost_live._select_named_node = (
            lambda *_a, **_k: (_ for _ in ()).throw(
                ghost_live.PhoneError("wrong active container")
            )
        )
        ghost_live.formulate_named = lambda *_a, **_k: later.append("formulate")
        ghost_live.advanced_named = lambda *_a, **_k: later.append("advanced")
        ghost_live.mark_ghost_container_ready = (
            lambda *_a, **_k: later.append("persist")
        )
        boundary_failed = False
        try:
            ghost_live.provision_slot_container(
                object(), FakeStack(), lambda _m: None, "a0007"
            )
        except ghost_live.PhoneError:
            boundary_failed = True
        check(
            "identity-mismatch-blocks-formulate-advanced-and-persistence",
            boundary_failed and later == [],
        )
    finally:
        for key, value in originals.items():
            setattr(ghost_live, key, value)

    moved_frame = [
        node("Containers", 170, 107, 280, 42),
        node("a0007", 160, 700, 140, 24),
        node("A7A7A7A7-1111-2222-3333-444455556666", 370, 735, 470),
        node("New Container", 370, 1263, 180),
    ]
    low_frame = [
        node("Containers", 170, 107, 280, 42),
        low,
        node("A7A7A7A7-1111-2222-3333-444455556666", 370, 1183, 470),
        node("New Container", 370, 1263, 180),
    ]
    try:
        ghost_live.dump_named = lambda *_a, **_k: ("", moved_frame)
        fresh_stack = FakeStack()
        _fresh_nodes, fresh_row = ghost_live._fresh_safe_instance_node(
            fresh_stack, object(), lambda _m: None,
            low_frame, "a0007", "A7A7A7A7",
        )
        ghost_live.tap_container_name(fresh_stack, fresh_row)
        check(
            "movement-discards-old-coordinate-and-reacquires-uuid-row",
            len(fresh_stack.flicks) == 1 and fresh_stack.taps[-1][1] == 700,
        )
    finally:
        ghost_live.dump_named = original_dump

    top_frame = [
        node("Containers", 170, 107, 280, 42),
        node("a0001", 160, 300, 140, 24),
        node("11111111-1111-2222-3333-444455556666", 370, 335, 470),
    ]
    offscreen_frame = [
        node("Containers", 170, 107, 280, 42),
        node("a0007", 160, 600, 140, 24),
        node("77777777-1111-2222-3333-444455556666", 370, 635, 470),
    ]
    bottom_frame = [
        node("Containers", 170, 107, 280, 42),
        node("New Container", 370, 1263, 180),
    ]
    try:
        def fake_scan_dump(_stack, _phone, stem, _lg, **_kwargs):
            if "_top_" in stem:
                return "", top_frame
            if stem.endswith("_bottom_0"):
                return "", offscreen_frame
            return "", bottom_frame
        ghost_live.dump_named = fake_scan_dump
        inventory, _ = ghost_live.scan_all_container_instances(
            FakeStack(), object(), lambda _m: None, top_frame, stem="scan"
        )
        check("complete-scan-finds-offscreen-a0007", "a0007" in inventory)
    finally:
        ghost_live.dump_named = original_dump

    # New Container is a fixed footer.  Its presence in the top frame must
    # not let a named scan conclude that an off-screen a0009 is absent.
    fixed_footer_top = top_frame + [node("New Container", 370, 1263, 180)]
    a0009_bottom = [
        node("Containers", 170, 107, 280, 42),
        node("a0009", 160, 700, 140, 24),
        node("E1D2FF69-216B-4E0B-B2A5-43FB2B0DD000", 370, 735, 470),
        node("New Container", 370, 1263, 180),
    ]
    named_bottom_calls: list[str] = []
    try:
        def fake_named_dump(_stack, _phone, stem, _lg, **_kwargs):
            if "_top_" in stem:
                return "", fixed_footer_top
            named_bottom_calls.append(stem)
            return "", a0009_bottom
        ghost_live.dump_named = fake_named_dump
        exact_ids, exact_nodes = ghost_live.scan_named_container_instances(
            FakeStack(), object(), lambda _m: None,
            fixed_footer_top, "a0009", stem="named_fixed_footer",
        )
        check(
            "fixed-footer-does-not-hide-offscreen-a0009",
            exact_ids == {"E1D2FF69"}
            and exact_nodes is a0009_bottom
            and len(named_bottom_calls) >= 3,
        )
    finally:
        ghost_live.dump_named = original_dump

    # A long list may need twelve movements before the first stable top frame.
    # The old 12-frame cap failed after the first unchanged result; the scan
    # must retain room for a second unchanged result before reversing toward
    # the bottom.
    long_top_calls: list[str] = []
    stable_top = [
        node("Containers", 170, 107, 280, 42),
        node("a0001", 160, 300, 140, 24),
        node("11111111-1111-2222-3333-444455556666", 370, 335, 470),
    ]
    moving_frames = [
        [
            node("Containers", 170, 107, 280, 42),
            node(f"a{i + 2:04d}", 160, 300, 140, 24),
            node(f"{i + 2:08X}-1111-2222-3333-444455556666", 370, 335, 470),
        ]
        for i in range(11)
    ]
    original_sleep = ghost_live.time.sleep
    try:
        def fake_long_dump(_stack, _phone, stem, _lg, **_kwargs):
            if "_top_" in stem:
                long_top_calls.append(stem)
                index = int(stem.rsplit("_", 1)[1])
                return "", moving_frames[index] if index < len(moving_frames) else stable_top
            return "", stable_top
        ghost_live.dump_named = fake_long_dump
        ghost_live.time.sleep = lambda _seconds: None
        long_ids, _long_nodes = ghost_live.scan_named_container_instances(
            FakeStack(), object(), lambda _m: None,
            moving_frames[0], "a0065", stem="long_named",
        )
        check(
            "long-named-scan-keeps-two-top-proof-frames",
            long_ids == set() and len(long_top_calls) >= 14,
        )
    finally:
        ghost_live.dump_named = original_dump
        ghost_live.time.sleep = original_sleep

    reserved = {f"a{i:04d}" for i in range(1, 8)}
    # Allocation normally unions the phone inventory with durable project
    # slots.  Keep this phone-only regression independent of the operator's
    # current slots.json contents.
    original_next_slot_id = ghost_live.next_slot_id
    ghost_live.next_slot_id = lambda extra: next(
        f"a{i:04d}" for i in range(1, 10_001) if f"a{i:04d}" not in extra
    )
    try:
        allocated = ghost_live.allocate_new_names(2, reserved)
    finally:
        ghost_live.next_slot_id = original_next_slot_id
    check("phone-only-a0007-forces-a0008", allocated[0] == "a0008")
    check("multi-create-never-reuses-run-id", allocated == ["a0008", "a0009"])

    # Restore the original behavior for a single exact orphan: configure it
    # instead of skipping it or creating a duplicate.
    existing_patch_names = (
        "close_ghost_organic", "open_reddit_containers", "dump_named",
        "stop_if_reset", "scan_all_container_instances",
        "ensure_ghost_container_needed", "ghost_container_state",
        "configure_existing_slot_container", "provision_slot_container",
        "unretire_slot_id",
    )
    existing_originals = {
        name: getattr(ghost_live, name) for name in existing_patch_names
    }
    existing_calls: list[tuple] = []
    proven_a0009_nodes = object()
    try:
        ghost_live.close_ghost_organic = lambda *_a, **_k: None
        ghost_live.open_reddit_containers = lambda *_a, **_k: None
        ghost_live.dump_named = lambda *_a, **_k: ("", [])
        ghost_live.stop_if_reset = lambda *_a, **_k: None
        ghost_live.scan_all_container_instances = lambda *_a, **_k: (
            {"a0009": {"E1D2FF69-216B-4E0B-B2A5-43FB2B0DD"}},
            proven_a0009_nodes,
        )
        ghost_live.ensure_ghost_container_needed = (
            lambda name, _lg: {"id": name, "ghost_container_state": "needed"}
        )
        ghost_live.ghost_container_state = lambda row: row["ghost_container_state"]
        ghost_live.configure_existing_slot_container = (
            lambda _phone, _stack, _lg, nodes, name, instance_id, **_kwargs:
            existing_calls.append(
                ("configure", name, instance_id, nodes is proven_a0009_nodes)
            )
            or "E1D2FF69-216B-4E0B-B2A5-43FB2B0DD"
        )
        ghost_live.provision_slot_container = (
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("existing a0009 attempted new-container creation")
            )
        )
        ghost_live.unretire_slot_id = (
            lambda name: existing_calls.append(("unretire", name)) or True
        )
        reused = ghost_live.apply_reddit_containers(
            object(), FakeStack(), lambda _m: None, count=1,
            names=["a0009"], close=True,
        )
        check(
            "single-existing-a0009-is-configured-not-skipped-or-duplicated",
            reused == ["a0009"]
            and existing_calls == [
                (
                    "configure", "a0009",
                    "E1D2FF69-216B-4E0B-B2A5-43FB2B0DD", True,
                ),
                ("unretire", "a0009"),
            ],
        )

        existing_calls.clear()
        ghost_live.scan_all_container_instances = lambda *_a, **_k: (
            {"a0009": {"OLD-A0009", "NEW-A0009"}}, []
        )
        ambiguous_failed = False
        try:
            ghost_live.apply_reddit_containers(
                object(), FakeStack(), lambda _m: None, count=1,
                names=["a0009"], close=True,
            )
        except ghost_live.PhoneError:
            ambiguous_failed = True
        check(
            "duplicate-existing-a0009-stops-without-selection",
            ambiguous_failed and existing_calls == [],
        )
    finally:
        for name, value in existing_originals.items():
            setattr(ghost_live, name, value)

    # The exact bottom frame must flow directly into selection.  Reopening or
    # rescanning here recreates the live failure from 19:50.
    direct_names = (
        "_select_named_node", "formulate_named", "advanced_named",
        "upsert_ghost", "mark_ghost_container_ready", "close_ghost_organic",
        "open_reddit_containers", "scan_named_container_instances",
    )
    direct_originals = {name: getattr(ghost_live, name) for name in direct_names}
    direct_calls: list[tuple] = []
    direct_nodes = object()
    direct_uuid = "E1D2FF69-216B-4E0B-B2A5-43FB2B0DD"
    try:
        ghost_live._select_named_node = (
            lambda _stack, _phone, _lg, nodes, name, instance_id:
            direct_calls.append(("select", nodes is direct_nodes, name, instance_id))
        )
        ghost_live.formulate_named = (
            lambda *_a, **_k: direct_calls.append(("formulate",))
        )
        ghost_live.advanced_named = (
            lambda *_a, **_k: direct_calls.append(("advanced",))
        )
        ghost_live.upsert_ghost = (
            lambda *_a, **_k: direct_calls.append(("upsert",))
        )
        ghost_live.mark_ghost_container_ready = (
            lambda *_a, **_k: direct_calls.append(("ready",))
        )
        ghost_live.close_ghost_organic = (
            lambda *_a, **_k: direct_calls.append(("close",))
        )
        ghost_live.open_reddit_containers = (
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("existing container path reopened Ghost")
            )
        )
        ghost_live.scan_named_container_instances = (
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("existing container path rescanned after proof")
            )
        )
        configured_uuid = ghost_live.configure_existing_slot_container(
            object(), FakeStack(), lambda _m: None,
            direct_nodes, "a0009", direct_uuid,
        )
        check(
            "proven-bottom-frame-selects-a0009-before-formulate-and-advanced",
            configured_uuid == direct_uuid
            and direct_calls == [
                ("select", True, "a0009", direct_uuid),
                ("formulate",), ("advanced",), ("upsert",),
                ("ready",), ("close",),
            ],
        )
    finally:
        for name, value in direct_originals.items():
            setattr(ghost_live, name, value)

    # An explicitly requested rebuild may bypass the allocation reservation,
    # but the reservation is released only after provisioning succeeds.
    patch_names = (
        "close_ghost_organic", "open_reddit_containers", "dump_named",
        "stop_if_reset", "scan_all_container_instances",
        "ensure_ghost_container_needed", "ghost_container_state",
        "provision_slot_container", "unretire_slot_id",
    )
    originals = {name: getattr(ghost_live, name) for name in patch_names}
    released: list[str] = []
    try:
        ghost_live.close_ghost_organic = lambda *_a, **_k: None
        ghost_live.open_reddit_containers = lambda *_a, **_k: None
        ghost_live.dump_named = lambda *_a, **_k: ("", [])
        ghost_live.stop_if_reset = lambda *_a, **_k: None
        ghost_live.scan_all_container_instances = lambda *_a, **_k: ({}, [])
        ghost_live.ensure_ghost_container_needed = (
            lambda name, _lg: {"id": name, "ghost_container_state": "needed"}
        )
        ghost_live.ghost_container_state = lambda row: row["ghost_container_state"]
        ghost_live.provision_slot_container = (
            lambda *_a, **_k: "NEW-A0009-INSTANCE"
        )
        ghost_live.unretire_slot_id = (
            lambda name: released.append(name) or True
        )
        rebuilt = ghost_live.apply_reddit_containers(
            object(), FakeStack(), lambda _m: None, count=1,
            names=["a0009"], close=True,
        )
        check(
            "explicit-a0009-rebuild-releases-reservation-after-success",
            rebuilt == ["a0009"] and released == ["a0009"],
        )

        released.clear()
        ghost_live.provision_slot_container = (
            lambda *_a, **_k: (_ for _ in ()).throw(
                ghost_live.PhoneError("mock provisioning failure")
            )
        )
        failed = False
        try:
            ghost_live.apply_reddit_containers(
                object(), FakeStack(), lambda _m: None, count=1,
                names=["a0009"], close=True,
            )
        except ghost_live.PhoneError:
            failed = True
        check(
            "failed-a0009-rebuild-keeps-retired-reservation",
            failed and released == [],
        )
    finally:
        for name, value in originals.items():
            setattr(ghost_live, name, value)

    zero_failed = many_failed = False
    try:
        ghost_live.reconciled_new_instance({"OLD00001"}, {"OLD00001"}, "a0007")
    except ghost_live.PhoneError:
        zero_failed = True
    try:
        ghost_live.reconciled_new_instance(
            {"OLD00001", "NEW00001", "NEW00002"}, {"OLD00001"}, "a0007"
        )
    except ghost_live.PhoneError:
        many_failed = True
    check("zero-new-uuid-fails-closed", zero_failed)
    check("multiple-new-uuids-fail-closed", many_failed)

    class StoppedStack(FakeStack):
        def check_cancelled(self) -> None:
            raise ghost_live.PhoneError("Stopped.")

        def press_home(self) -> None:
            self.check_cancelled()

    stopped_before_phone_action = False
    try:
        ghost_live.apply_reddit_containers(
            object(), StoppedStack(), lambda _m: None, count=1
        )
    except ghost_live.PhoneError:
        stopped_before_phone_action = True
    check("stop-blocks-ghost-scan-create-select-config-and-write", stopped_before_phone_action)

    stale = {
        ghost_live.CRANE_REDDIT_KEY: {
            "Containers": [
                {"identifier": "DEFAULT", "name": ""},
                {"identifier": "A1B2C3D4", "name": "a0001"},
            ],
            "activeContainer": "DELETED",
        }
    }
    phone = FakePhone(stale)
    check(
        "stale-active-is-repaired",
        ghost_live.repair_stale_crane_reddit_active(phone, lambda _m: None),
    )
    repaired = plistlib.loads(phone.files[phone.path])
    check(
        "sole-named-container-becomes-active",
        repaired[ghost_live.CRANE_REDDIT_KEY]["activeContainer"] == "A1B2C3D4",
    )
    check("repair-refreshes-preferences", phone.commands == ["killall cfprefsd"])

    valid = FakePhone(
        {
            ghost_live.CRANE_REDDIT_KEY: {
                "Containers": [{"identifier": "DEFAULT", "name": ""}],
                "activeContainer": "DEFAULT",
            }
        }
    )
    check(
        "valid-active-is-untouched",
        not ghost_live.repair_stale_crane_reddit_active(valid, lambda _m: None),
    )
    check("valid-active-has-no-writes", len(valid.files) == 1 and not valid.commands)

    ambiguous = FakePhone(
        {
            ghost_live.CRANE_REDDIT_KEY: {
                "Containers": [
                    {"identifier": "DEFAULT", "name": ""},
                    {"identifier": "ONE", "name": "a0001"},
                    {"identifier": "TWO", "name": "a0002"},
                ],
                "activeContainer": "DELETED",
            }
        }
    )
    check(
        "ambiguous-stale-active-is-repaired",
        ghost_live.repair_stale_crane_reddit_active(ambiguous, lambda _m: None),
    )
    ambiguous_result = plistlib.loads(ambiguous.files[ambiguous.path])
    check(
        "ambiguous-repair-uses-default",
        ambiguous_result[ghost_live.CRANE_REDDIT_KEY]["activeContainer"] == "DEFAULT",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
