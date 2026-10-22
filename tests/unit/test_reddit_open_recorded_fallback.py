"""Offline coverage for the UUID-bound Ghost fallback from a stock haptic menu."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import unittest
from unittest import mock

from friend_poster.core.paths import FIXTURES_DIR
from friend_poster.reddit import reddit_open


class Node:
    def __init__(self, text: str, x: int, y: int, w: int, h: int) -> None:
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
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))


def stock_haptic_nodes():
    rows = json.loads(
        (FIXTURES_DIR / "crane_730_haptic_stock.json").read_text(
            encoding="utf-8"
        )
    )
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


class RecordedContainerFallbackTests(unittest.TestCase):
    @contextmanager
    def _patch_route(
        self,
        *,
        recorded_instance="RECORDED-0225",
        selector_side_effect=None,
        confirm_side_effect=None,
    ):
        nodes = stock_haptic_nodes()
        icon = Node("Reddit", 240, 1015, 96, 25)
        stack = Stack()
        phone = object()
        logs: list[str] = []
        with ExitStack() as cleanup:
            cleanup.enter_context(
                mock.patch.object(
                    reddit_open,
                    "get_slot",
                    return_value={"ghost_container_instance": recorded_instance},
                )
            )
            cleanup.enter_context(
                mock.patch.object(
                    reddit_open,
                    "_transient_dump",
                    side_effect=[("home", []), ("haptic", nodes)],
                )
            )
            find_home = cleanup.enter_context(
                mock.patch.object(
                    reddit_open, "_find_reddit_from_home", return_value=icon
                )
            )
            cleanup.enter_context(
                mock.patch.object(
                    reddit_open, "long_press_home_icon", return_value=None
                )
            )
            crane_tap = cleanup.enter_context(
                mock.patch.object(
                    reddit_open,
                    "_tap_requested_container",
                    side_effect=AssertionError("stock haptic must not be tapped"),
                )
            )
            cleanup.enter_context(
                mock.patch.object(reddit_open.time, "sleep", return_value=None)
            )
            selector = cleanup.enter_context(
                mock.patch.object(
                    reddit_open.ghost_live,
                    "select_recorded_container",
                    side_effect=selector_side_effect,
                    return_value="FULL-INSTANCE-0225",
                )
            )
            confirm = cleanup.enter_context(
                mock.patch.object(
                    reddit_open,
                    "_confirm_container_launch",
                    side_effect=confirm_side_effect,
                    return_value="reddit-proof",
                )
            )
            yield stack, phone, logs, selector, confirm, crane_tap, find_home

    def test_stock_haptic_selects_recorded_uuid_once_and_confirms_reddit(self):
        with self._patch_route() as route:
            stack, phone, logs, selector, confirm, crane_tap, _find_home = route

            result = reddit_open.open_container(
                stack,
                phone,
                logs.append,
                "a0225",
                "fixture@icloud.com",
            )

            self.assertEqual(result, "reddit-proof")
            selector.assert_called_once_with(
                stack,
                phone,
                logs.append,
                "a0225",
                "RECORDED-0225",
            )
            confirm.assert_called_once_with(
                stack,
                phone,
                logs.append,
                "a0225",
                allow_existing_sparse_feed=False,
            )
            crane_tap.assert_not_called()
            self.assertEqual(stack.taps, [])
            self.assertIn("CRANE_SELECTION_PATH=ghost-recorded-instance", logs)
            self.assertIn("CRANE_ACTIVE_CONFIRMED=a0225", logs)

    def test_absent_ambiguous_and_scanner_failure_are_fail_closed(self):
        failures = (
            reddit_open.PhoneError("recorded instance is absent"),
            reddit_open.PhoneError("recorded prefix is ambiguous"),
            RuntimeError("Ghost inventory scanner failed"),
        )
        for failure in failures:
            with self.subTest(failure=str(failure)):
                with self._patch_route(selector_side_effect=failure) as route:
                    stack, phone, logs, selector, confirm, crane_tap, _find_home = route
                    with self.assertRaisesRegex(
                        reddit_open.CraneDoorwayUnavailable,
                        "stock iOS haptic menu",
                    ):
                        reddit_open.open_container(
                            stack,
                            phone,
                            logs.append,
                            "a0225",
                            "fixture@icloud.com",
                        )

                    selector.assert_called_once()
                    confirm.assert_not_called()
                    crane_tap.assert_not_called()
                    self.assertEqual(stack.taps, [])

    def test_unproven_reddit_after_uuid_selection_does_not_retry_crane(self):
        launch_error = reddit_open.PhoneError("Reddit screen was not proven")
        with self._patch_route(confirm_side_effect=launch_error) as route:
            stack, phone, logs, selector, confirm, crane_tap, find_home = route
            with self.assertRaisesRegex(
                reddit_open.CraneDoorwayUnavailable,
                "Reddit launch was not locally proven",
            ):
                reddit_open.open_container(
                    stack,
                    phone,
                    logs.append,
                    "a0225",
                    "fixture@icloud.com",
                )

            selector.assert_called_once()
            confirm.assert_called_once()
            crane_tap.assert_not_called()
            find_home.assert_called_once()
            self.assertEqual(stack.taps, [])

    def test_missing_recorded_uuid_is_typed_and_never_taps_haptic_menu(self):
        with self._patch_route(recorded_instance="") as route:
            stack, phone, logs, selector, confirm, crane_tap, _find_home = route
            with self.assertRaisesRegex(
                reddit_open.CraneDoorwayUnavailable,
                "no recorded Ghost container identity",
            ):
                reddit_open.open_container(
                    stack,
                    phone,
                    logs.append,
                    "a0225",
                    "fixture@icloud.com",
                )

            selector.assert_not_called()
            confirm.assert_not_called()
            crane_tap.assert_not_called()
            self.assertEqual(stack.taps, [])

    def test_non_stock_screen_does_not_authorize_uuid_fallback(self):
        self.assertFalse(
            reddit_open.stock_haptic_without_crane_doorway(
                [Node("Search", 300, 680, 120, 26)], "a0225"
            )
        )


if __name__ == "__main__":
    unittest.main()

