"""OCR-decorated Crane Container rows must use Crane, never the Ghost fallback."""

from __future__ import annotations

import unittest
from contextlib import ExitStack, contextmanager
from unittest import mock

from friend_poster.core.paths import FIXTURES_DIR
from friend_poster.device.ui_tree import Node, parse_nodes
from friend_poster.reddit import reddit_open


def _fixture_nodes(name: str) -> list[Node]:
    return parse_nodes((FIXTURES_DIR / name).read_text(encoding="utf-8"))


HAPTIC = _fixture_nodes("crane_haptic_decorated_container.json")
COMPACT = _fixture_nodes("crane_640_select_scan_a0226.json")


class RecordingStack:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class DecoratedContainerDoorwayTests(unittest.TestCase):
    def test_decorated_container_row_is_a_safe_haptic_doorway(self):
        marker = reddit_open.crane_singular_parent_marker(HAPTIC)

        self.assertIsNotNone(marker)
        self.assertEqual(
            reddit_open.collapsed_text(marker.labels()), "� container"
        )
        self.assertFalse(
            reddit_open.stock_haptic_without_crane_doorway(HAPTIC, "a0226")
        )

    @contextmanager
    def _patch_open(self):
        stack = RecordingStack()
        phone = object()
        logs: list[str] = []
        icon = Node(ntype="ocr", text="Reddit", x=240, y=1015, w=96, h=25)
        with ExitStack() as cleanup:
            cleanup.enter_context(
                mock.patch.object(
                    reddit_open,
                    "get_slot",
                    return_value={
                        "ghost_container_instance": "4F1A9C93-2829-4237-9156-840BFA38CC50"
                    },
                )
            )
            cleanup.enter_context(
                mock.patch.object(
                    reddit_open,
                    "_transient_dump",
                    side_effect=[
                        ("home", []),
                        ("haptic", HAPTIC),
                        ("level2", COMPACT),
                    ],
                )
            )
            cleanup.enter_context(
                mock.patch.object(
                    reddit_open, "_find_reddit_from_home", return_value=icon
                )
            )
            cleanup.enter_context(
                mock.patch.object(
                    reddit_open, "long_press_home_icon", return_value=None
                )
            )
            cleanup.enter_context(
                mock.patch.object(reddit_open.time, "sleep", return_value=None)
            )
            cleanup.enter_context(
                mock.patch.object(
                    reddit_open.ghost_live,
                    "select_recorded_container",
                    side_effect=AssertionError(
                        "decorated Crane doorway must use the Crane picker"
                    ),
                )
            )
            confirm = cleanup.enter_context(
                mock.patch.object(
                    reddit_open,
                    "_confirm_container_launch",
                    return_value="reddit-proof",
                )
            )
            yield stack, phone, logs, confirm

    def test_open_container_taps_decorated_parent_then_exact_compact_row(self):
        with self._patch_open() as route:
            stack, phone, logs, confirm = route

            result = reddit_open.open_container(
                stack,
                phone,
                logs.append,
                "a0226",
                "fixture@icloud.com",
            )

        parent = reddit_open.crane_singular_parent_marker(HAPTIC)
        target = reddit_open.compact_crane_target_row(COMPACT, "a0226")
        self.assertIsNotNone(parent)
        self.assertIsNotNone(target)
        self.assertEqual(result, "reddit-proof")
        self.assertEqual(stack.taps, [parent.center(), target.center()])
        confirm.assert_called_once_with(
            stack,
            phone,
            logs.append,
            "a0226",
            allow_existing_sparse_feed=False,
        )
        self.assertIn("CRANE_SELECTION_PATH=list", logs)


if __name__ == "__main__":
    unittest.main()
