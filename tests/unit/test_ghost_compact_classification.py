"""Ghost routing must never confuse the compact Crane menu with Ghost."""

from __future__ import annotations

import unittest
from unittest import mock

from friend_poster.core.config import GHOST_BUNDLE
from friend_poster.core.paths import FIXTURES_DIR
from friend_poster.device import ghost_live
from friend_poster.device.ui_tree import Node, parse_nodes
from friend_poster.reddit import reddit_open


def _fixture_nodes(name: str) -> list[Node]:
    return parse_nodes((FIXTURES_DIR / name).read_text(encoding="utf-8"))


COMPACT_A0226 = _fixture_nodes("crane_640_select_scan_a0226.json")
EMPTY_GHOST_INVENTORY = [
    Node(ntype="ocr", text="Containers", x=175, y=87, w=287, h=38),
    Node(ntype="ocr", text="New Container", x=375, y=1253, w=180, h=18),
]


class RecordingStack:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.taps: list[tuple[int, int]] = []
        self.row_taps: list[str] = []
        self.flicks: list[tuple] = []

    def open_bundle(self, bundle: str) -> None:
        self.opened.append(bundle)

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def tap_row(self, node: Node) -> None:
        self.row_taps.append(node.labels())

    def interp_flick(self, *args, **kwargs) -> None:
        self.flicks.append((args, kwargs))

    def press_home(self) -> None:
        raise AssertionError("compact Crane menu must not be treated as Crane settings")


def test_live_a0226_compact_fixture_still_selects_exact_slot() -> None:
    target = reddit_open.compact_crane_target_row(COMPACT_A0226, "a0226")

    assert target is not None
    assert target.text == "a0226"
    assert target.center()[1] > max(
        node.center()[1] for node in COMPACT_A0226 if node.text == "Default"
    )


def test_compact_and_new_container_only_do_not_prove_ghost_inventory() -> None:
    new_only = [
        Node(ntype="ocr", text="New Container", x=392, y=580, w=240, h=25)
    ]

    assert not ghost_live.on_containers_list(COMPACT_A0226)
    assert ghost_live.ghost_screen_state(COMPACT_A0226) == "unknown"
    assert not ghost_live.on_containers_list(new_only)
    assert ghost_live.ghost_screen_state(new_only) == "unknown"


def test_genuine_empty_ghost_inventory_is_accepted() -> None:
    assert ghost_live.on_containers_list(EMPTY_GHOST_INVENTORY)
    assert ghost_live.ghost_screen_state(EMPTY_GHOST_INVENTORY) == "containers"


def test_route_from_compact_menu_opens_ghost_once_without_touching_menu() -> None:
    stack = RecordingStack()

    with (
        mock.patch.object(
            ghost_live,
            "dump_named",
            return_value=("fixture", EMPTY_GHOST_INVENTORY),
        ) as dump,
        mock.patch.object(ghost_live.time, "sleep", return_value=None),
    ):
        state, nodes = ghost_live.route_ghost_screen(
            stack, object(), lambda _message: None, COMPACT_A0226
        )

    assert state == "containers"
    assert nodes is EMPTY_GHOST_INVENTORY
    assert stack.opened == [GHOST_BUNDLE]
    assert stack.taps == []
    assert stack.row_taps == []
    assert stack.flicks == []
    dump.assert_called_once()


class GhostCompactClassificationTests(unittest.TestCase):
    def test_live_a0226_compact_fixture_still_selects_exact_slot(self):
        test_live_a0226_compact_fixture_still_selects_exact_slot()

    def test_compact_and_new_container_only_do_not_prove_ghost_inventory(self):
        test_compact_and_new_container_only_do_not_prove_ghost_inventory()

    def test_genuine_empty_ghost_inventory_is_accepted(self):
        test_genuine_empty_ghost_inventory_is_accepted()

    def test_route_from_compact_menu_opens_ghost_once_without_touching_menu(self):
        test_route_from_compact_menu_opens_ghost_once_without_touching_menu()


if __name__ == "__main__":
    unittest.main()
