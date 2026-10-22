"""Focused regressions for exact Crane selection and batch preservation."""

from __future__ import annotations

from datetime import date
import json
import unittest
from unittest import mock

from friend_poster.apps import creator
from friend_poster.core.paths import FIXTURES_DIR
from friend_poster.reddit import reddit_open
import test_bulk_creator as bulk_tests


class Node:
    def __init__(self, text: str, x: int, y: int, w: int = 150, h: int = 28):
        self._text = text
        self.x, self.y, self.w, self.h = x, y, w, h
        self.ntype = "accessibility"
        self.visible = True

    def labels(self):
        return self._text

    def on_screen(self):
        return True

    def center(self):
        return self.x + self.w // 2, self.y + self.h // 2


class Stack:
    def __init__(self):
        self.taps = []
        self.flicks = []

    def tap_xy(self, x, y):
        self.taps.append((x, y))

    def interp_flick(self, x1, y1, x2, y2, **kwargs):
        self.flicks.append(((x1, y1, x2, y2), kwargs))


def crane_list(*names: str):
    nodes = [Node("Select Container", 210, 100, 320, 28)]
    nodes.extend(Node(name, 220, 250 + index * 90) for index, name in enumerate(names))
    return nodes


def fixture_nodes(name: str):
    rows = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return [
        Node(
            str(row.get("text") or ""),
            int(row.get("x") or 0),
            int(row.get("y") or 0),
            int(row.get("w") or 0),
            int(row.get("h") or 0),
        )
        for row in rows
    ]


def live_compact_a0225_picker():
    return fixture_nodes("crane_730_container_picker_a0225.json")


def stock_haptic_picker():
    return fixture_nodes("crane_730_haptic_stock.json")


def legacy_compact_a0220_picker():
    return [
        Node("Remave Ann", 373, 289, 190, 17),
        Node("Container", 353, 331, 175, 25),
        Node("Default", 333, 366, 122, 26),
        Node("Default", 333, 437, 122, 26),
        Node("a0220", 329, 509, 110, 26),
        Node("New Container", 392, 580, 240, 25),
        Node("Settings", 343, 667, 132, 31),
    ]


def test_live_compact_crane_picker_proves_only_lower_exact_a0225():
    nodes = live_compact_a0225_picker()

    target = reddit_open.compact_crane_target_row(nodes, "a0225")

    assert target is not None
    assert target.labels() == "0225"
    assert target.center()[1] > next(
        node.center()[1] for node in nodes if node.labels() == "a0225"
    )
    # The live fixture deliberately contains corrupted ``Remove App`` OCR.
    assert any(node.labels() == "Remave Ann" for node in nodes)
    assert reddit_open.compact_crane_target_row(nodes, "a0224") is None


def test_compact_crane_picker_accepts_both_proven_layouts():
    legacy = legacy_compact_a0220_picker()
    assert reddit_open.compact_crane_target_row(legacy, "a0220").labels() == "a0220"

    canonical_lower = live_compact_a0225_picker()
    next(node for node in canonical_lower if node.labels() == "0225")._text = "a0225"
    target = reddit_open.compact_crane_target_row(canonical_lower, "a0225")
    assert target is not None
    assert target.labels() == "a0225"
    assert target.center()[1] > next(
        node.center()[1]
        for node in canonical_lower
        if node.labels() == "a0225" and node is not target
    )


def test_compact_crane_picker_rejects_incomplete_reordered_duplicate_and_hme():
    cases = []
    base = live_compact_a0225_picker()
    cases.append([node for node in base if node.labels() != "Settings"])

    reordered = live_compact_a0225_picker()
    next(node for node in reordered if node.labels() == "New Container").y = 470
    cases.append(reordered)

    duplicate = live_compact_a0225_picker()
    duplicate.append(Node("0225", 330, 535, 110, 24))
    cases.append(duplicate)

    wrong_bare_slot = live_compact_a0225_picker()
    next(node for node in wrong_bare_slot if node.labels() == "0225")._text = "0224"
    cases.append(wrong_bare_slot)

    hide_my_email = live_compact_a0225_picker()
    hide_my_email.append(Node("Hide My Email", 250, 110, 240, 25))
    cases.append(hide_my_email)

    for nodes in cases:
        assert reddit_open.compact_crane_target_row(nodes, "a0225") is None


def test_compact_crane_picker_rejects_extra_slot_rows_across_full_menu_body():
    legacy_prefixed_between_defaults = legacy_compact_a0220_picker()
    legacy_prefixed_between_defaults.append(Node("a0219", 330, 405, 110, 24))

    legacy_numeric_above_defaults = legacy_compact_a0220_picker()
    legacy_numeric_above_defaults.append(Node("0219", 430, 346, 80, 16))

    legacy_numeric_after_new = legacy_compact_a0220_picker()
    legacy_numeric_after_new.append(Node("0219", 430, 620, 80, 16))

    live_numeric_before_active = live_compact_a0225_picker()
    live_numeric_before_active.append(Node("0999", 430, 346, 80, 16))

    live_prefixed_after_new = live_compact_a0225_picker()
    live_prefixed_after_new.append(Node("a0999", 430, 620, 90, 16))

    cases = (
        (legacy_prefixed_between_defaults, "a0220"),
        (legacy_numeric_above_defaults, "a0220"),
        (legacy_numeric_after_new, "a0220"),
        (live_numeric_before_active, "a0225"),
        (live_prefixed_after_new, "a0225"),
    )
    for nodes, name in cases:
        assert reddit_open.compact_crane_target_row(nodes, name) is None


def test_stock_ios_haptic_never_proves_or_taps_a_container():
    nodes = stock_haptic_picker()
    stack = Stack()

    assert reddit_open.compact_crane_target_row(nodes, "a0225") is None
    with mock.patch.object(
        reddit_open, "_transient_dump", return_value=("fixture", nodes)
    ):
        try:
            reddit_open.select_existing_container_from_compact_menu(
                stack, object(), lambda _line: None, "a0225"
            )
        except reddit_open.ContainerSelectionError:
            pass
        else:
            raise AssertionError("stock iOS haptic menu was accepted as Crane")

    assert stack.taps == []


def test_open_container_uses_live_compact_picker_even_when_home_menu_required():
    nodes = live_compact_a0225_picker()
    target = next(node for node in nodes if node.labels() == "0225")
    stack = Stack()
    logs = []

    with mock.patch.object(
        reddit_open,
        "get_slot",
        return_value={"ghost_container_instance": "fixture-instance"},
    ), mock.patch.object(
        reddit_open, "_transient_dump", return_value=("fixture", nodes)
    ), mock.patch.object(
        reddit_open,
        "_find_reddit_from_home",
        side_effect=AssertionError("must not return Home from proven compact picker"),
    ), mock.patch.object(
        reddit_open, "_confirm_container_launch", return_value=None
    ) as confirm:
        reddit_open.open_container(
            stack,
            object(),
            logs.append,
            "a0225",
            "fixture@icloud.com",
            require_newest_exact=True,
            require_home_menu=True,
        )

    assert stack.taps == [target.center()]
    assert stack.flicks == []
    confirm.assert_called_once()
    assert "CRANE_SELECTION_PATH=compact-live-list" in logs
    assert "CRANE_ACTIVE_CONFIRMED=a0225" in logs


def test_recovery_compact_selector_requires_and_confirms_exact_live_target():
    nodes = live_compact_a0225_picker()
    target = next(node for node in nodes if node.labels() == "0225")
    stack = Stack()
    proof = object()

    with mock.patch.object(
        reddit_open, "_transient_dump", return_value=("fixture", nodes)
    ), mock.patch.object(
        reddit_open, "_confirm_container_launch", return_value=proof
    ) as confirm:
        result = reddit_open.select_existing_container_from_compact_menu(
            stack, object(), lambda _line: None, "a0225"
        )

    assert result is proof
    assert stack.taps == [target.center()]
    assert stack.flicks == []
    confirm.assert_called_once()


def test_unique_newest_visible_exact_row_is_selected_without_scrolling():
    nodes = crane_list("a0091", "a0093", "a0094", "a0095")
    stack = Stack()
    logs = []

    found = reddit_open._locate_container_in_list(
        stack, object(), logs.append, "a0095", nodes, newest_bound=True
    )

    assert found.labels() == "a0095"
    assert stack.flicks == []
    assert any("selecting it immediately without a scroll" in line for line in logs)


def test_duplicate_visible_exact_rows_do_not_use_immediate_shortcut():
    nodes = crane_list("a0094", "a0095", "a0095")
    stack = Stack()
    foreign = [Node("Home", 20, 110)]

    with mock.patch.object(
        reddit_open, "_transient_dump", return_value=("synthetic", foreign)
    ), mock.patch.object(reddit_open.time, "sleep", return_value=None):
        try:
            reddit_open._locate_container_in_list(
                stack, object(), lambda _line: None, "a0095", nodes, newest_bound=True
            )
        except reddit_open.PhoneError as exc:
            assert "list disappeared" in str(exc)
        else:
            raise AssertionError("ambiguous duplicate unexpectedly selected")

    assert len(stack.flicks) == 1


def test_instance_bound_changed_level2_selects_unique_requested_row():
    parent = [
        Node("Remove App", 350, 420),
        Node("Container", 300, 600),
    ]
    level2 = [
        Node("Container", 300, 360),
        Node("a0099", 280, 520),
        Node("a0100", 280, 610),
        Node("a0101", 280, 700),
    ]
    stack = Stack()

    with mock.patch.object(
        reddit_open, "_transient_dump", return_value=("synthetic", level2)
    ), mock.patch.object(reddit_open.time, "sleep", return_value=None):
        path = reddit_open._tap_requested_container(
            stack,
            object(),
            lambda _line: None,
            "a0101",
            parent,
            newest_bound=True,
        )

    assert path == "list"
    assert stack.taps == [parent[1].center(), level2[3].center()]
    assert stack.flicks == []


def test_post_tap_launch_must_be_confirmed_before_open_returns():
    nodes = crane_list("a0094", "a0095")
    stack = Stack()

    with mock.patch.object(reddit_open, "get_slot", return_value={}), mock.patch.object(
        reddit_open, "_transient_dump", return_value=("synthetic", nodes)
    ), mock.patch.object(
        reddit_open, "_find_reddit_from_home", return_value=Node("Reddit", 20, 20)
    ), mock.patch.object(
        reddit_open, "long_press_home_icon", return_value=None
    ), mock.patch.object(
        reddit_open, "_tap_requested_container", return_value="list"
    ), mock.patch.object(
        reddit_open,
        "_confirm_container_launch",
        side_effect=reddit_open.PhoneError("Reddit screen was not proven"),
    ):
        try:
            reddit_open.open_container(
                stack, object(), lambda _line: None, "a0095", "fixture@icloud.com"
            )
        except reddit_open.ContainerSelectionError as exc:
            assert exc.failure_code == "reddit_container_selection_failed"
            assert "after one bounded fresh-route retry" in str(exc)
        else:
            raise AssertionError("unconfirmed launch unexpectedly continued")


def test_unconfirmed_first_click_reopens_crane_and_succeeds_once():
    nodes = crane_list("a0094", "a0095")
    stack = Stack()
    logs = []

    with mock.patch.object(reddit_open, "get_slot", return_value={}), mock.patch.object(
        reddit_open, "_transient_dump", return_value=("synthetic", nodes)
    ), mock.patch.object(
        reddit_open, "_find_reddit_from_home", return_value=Node("Reddit", 20, 20)
    ) as reopen, mock.patch.object(
        reddit_open, "long_press_home_icon", return_value=None
    ), mock.patch.object(
        reddit_open, "_tap_requested_container", return_value="list"
    ) as select, mock.patch.object(
        reddit_open,
        "_confirm_container_launch",
        side_effect=[reddit_open.PhoneError("first click missed"), None],
    ) as confirm:
        reddit_open.open_container(
            stack, object(), logs.append, "a0095", "fixture@icloud.com"
        )

    assert reopen.call_count == 1
    assert select.call_count == 2
    assert confirm.call_count == 2
    assert any("retrying the verified Home -> Crane route once" in line for line in logs)
    assert "CRANE_ACTIVE_CONFIRMED=a0095" in logs


def test_confirmed_exact_selection_logs_active_confirmation():
    nodes = crane_list("a0094", "a0095")
    logs = []

    with mock.patch.object(reddit_open, "get_slot", return_value={}), mock.patch.object(
        reddit_open, "_transient_dump", return_value=("synthetic", nodes)
    ), mock.patch.object(
        reddit_open, "_tap_requested_container", return_value="list"
    ), mock.patch.object(
        reddit_open, "_confirm_container_launch", return_value=None
    ):
        reddit_open.open_container(
            Stack(), object(), logs.append, "a0095", "fixture@icloud.com"
        )

    assert "CRANE_ACTIVE_CONFIRMED=a0095" in logs


def test_blank_reddit_gets_double_wait_then_one_verified_app_relaunch():
    stack = Stack()
    logs = []
    loading_nodes = []
    reddit_nodes = [Node("Search Reddit", 200, 100)]
    icon = Node("Reddit", 240, 1000, 90, 20)
    dumps = [("blank", loading_nodes)] * 16 + [("reddit", reddit_nodes)]

    with mock.patch.object(
        reddit_open, "_transient_dump", side_effect=dumps
    ) as dump, mock.patch.object(
        reddit_open, "_find_reddit_from_home", return_value=icon
    ) as find_home, mock.patch.object(
        reddit_open, "_save_confirmed_reddit_dump", return_value=None
    ) as save, mock.patch.object(
        reddit_open.time, "sleep", return_value=None
    ):
        result = reddit_open._confirm_container_launch(
            stack, object(), logs.append, "a0128"
        )

    assert result is None
    assert dump.call_count == 17
    find_home.assert_called_once()
    save.assert_called_once_with("reddit", logs.append)
    assert stack.taps == [reddit_open.reddit_icon_point(icon)]
    assert any("doubled loading window" in line for line in logs)


def test_blank_reddit_fails_after_only_one_verified_app_relaunch():
    stack = Stack()
    logs = []
    icon = Node("Reddit", 240, 1000, 90, 20)

    with mock.patch.object(
        reddit_open, "_transient_dump", return_value=("blank", [])
    ) as dump, mock.patch.object(
        reddit_open, "_find_reddit_from_home", return_value=icon
    ) as find_home, mock.patch.object(
        reddit_open.time, "sleep", return_value=None
    ):
        try:
            reddit_open._confirm_container_launch(
                stack, object(), logs.append, "a0128"
            )
        except reddit_open.PhoneError as exc:
            assert "doubled loading window" in str(exc)
            assert "one verified Home -> Reddit relaunch" in str(exc)
        else:
            raise AssertionError("permanently blank Reddit unexpectedly continued")

    assert dump.call_count == 32
    find_home.assert_called_once()
    assert stack.taps == [reddit_open.reddit_icon_point(icon)]


def test_selection_failure_pauses_same_slot_without_consuming_queue():
    harness = bulk_tests.BatchCreatorTests(methodName="runTest")
    app = harness.app(1)
    app.bulk_state.update(
        stage="retry_creation",
        slot_id="a0095",
        verified_ip="198.51.100.9",
        verified_at_utc="2026-10-18T12:00:00Z",
    )
    app._bulk_slot_and_entry = lambda state: (
        "a0095",
        state["queue"][0],
        date(2005, 4, 20),
    )
    original_head = dict(app.bulk_state["queue"][0])
    pauses = []
    app._pause_bulk = lambda phase, reason, code=None: pauses.append(
        (phase, reason, code)
    )
    app._skip_failed_bulk_slot = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("selection failure consumed/skipped the queue head")
    )

    def selection_failed(*_args, **_kwargs):
        app._last_pipeline_failure_code = reddit_open.ContainerSelectionError.failure_code
        app._last_pipeline_failure_reason = "Exact container was not confirmed active."
        return False

    app._run_creation_pipeline = selection_failed
    with mock.patch.object(creator, "save_bulk_state", lambda _state: None), mock.patch.object(
        creator,
        "run_macrodroid_change",
        side_effect=AssertionError("IP reset on same-slot creation retry"),
    ), mock.patch.object(
        creator,
        "observe_proxy_without_trigger",
        return_value=bulk_tests.verified_result("198.51.100.9", "198.51.100.9"),
    ), mock.patch.object(
        creator, "get_slot", return_value={"reddit": False}
    ), mock.patch.object(
        creator, "_pipeline_slot_complete", return_value=False
    ):
        creator.App._run_bulk_batch(app)

    assert pauses == [
        (
            "creation",
            "Exact container was not confirmed active.",
            "reddit_container_selection_failed",
        )
    ]
    assert app.bulk_state["slot_id"] == "a0095"
    assert app.bulk_state["remaining"] == 1
    assert app.bulk_state["queue"] == [original_head]


class ContainerSelectionPauseTests(unittest.TestCase):
    def test_live_compact_crane_picker_proves_only_lower_exact_a0225(self):
        test_live_compact_crane_picker_proves_only_lower_exact_a0225()

    def test_compact_crane_picker_accepts_both_proven_layouts(self):
        test_compact_crane_picker_accepts_both_proven_layouts()

    def test_compact_crane_picker_rejects_incomplete_reordered_duplicate_and_hme(self):
        test_compact_crane_picker_rejects_incomplete_reordered_duplicate_and_hme()

    def test_compact_crane_picker_rejects_extra_slot_rows_across_full_menu_body(self):
        test_compact_crane_picker_rejects_extra_slot_rows_across_full_menu_body()

    def test_stock_ios_haptic_never_proves_or_taps_a_container(self):
        test_stock_ios_haptic_never_proves_or_taps_a_container()

    def test_open_container_uses_live_compact_picker_even_when_home_menu_required(self):
        test_open_container_uses_live_compact_picker_even_when_home_menu_required()

    def test_recovery_compact_selector_requires_and_confirms_exact_live_target(self):
        test_recovery_compact_selector_requires_and_confirms_exact_live_target()

    def test_unique_newest_visible_exact_row_is_selected_without_scrolling(self):
        test_unique_newest_visible_exact_row_is_selected_without_scrolling()

    def test_duplicate_visible_exact_rows_do_not_use_immediate_shortcut(self):
        test_duplicate_visible_exact_rows_do_not_use_immediate_shortcut()

    def test_instance_bound_changed_level2_selects_unique_requested_row(self):
        test_instance_bound_changed_level2_selects_unique_requested_row()

    def test_post_tap_launch_must_be_confirmed_before_open_returns(self):
        test_post_tap_launch_must_be_confirmed_before_open_returns()

    def test_confirmed_exact_selection_logs_active_confirmation(self):
        test_confirmed_exact_selection_logs_active_confirmation()

    def test_blank_reddit_gets_double_wait_then_one_verified_app_relaunch(self):
        test_blank_reddit_gets_double_wait_then_one_verified_app_relaunch()

    def test_blank_reddit_fails_after_only_one_verified_app_relaunch(self):
        test_blank_reddit_fails_after_only_one_verified_app_relaunch()

    def test_unconfirmed_first_click_reopens_crane_and_succeeds_once(self):
        test_unconfirmed_first_click_reopens_crane_and_succeeds_once()

    def test_selection_failure_pauses_same_slot_without_consuming_queue(self):
        test_selection_failure_pauses_same_slot_without_consuming_queue()


if __name__ == "__main__":
    unittest.main()
