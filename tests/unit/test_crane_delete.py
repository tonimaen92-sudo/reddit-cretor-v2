"""Offline safety tests for exact Crane container deletion."""

from __future__ import annotations

import unittest
from unittest import mock

from friend_poster.device import crane_delete
from friend_poster.device.ui_tree import Node


def node(label: str, *, x: int = 100, y: int = 300, w: int = 120, h: int = 40) -> Node:
    return Node(label=label, x=x, y=y, w=w, h=h)


class Stack:
    def __init__(self):
        self.taps = []

    def tap_xy(self, x, y):
        self.taps.append((x, y))


class CraneDeleteTests(unittest.TestCase):
    def test_delete_scrolls_edit_list_and_targets_exact_named_row(self):
        stack = Stack()
        target = node("a0069", x=110, y=410)
        delete = node("Delete", x=600, y=410)
        after = [node("Done", x=650, y=40)]

        with (
            mock.patch.object(crane_delete, "flick_list") as flick,
            mock.patch.object(
                crane_delete,
                "dump",
                side_effect=[
                    (b"", [target]),
                    (b"", [target, delete]),
                    (b"", after),
                ],
            ),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            result = crane_delete.tap_red_delete_r1(
                stack, object(), [node("a0068")], "a0069"
            )

        flick.assert_called_once_with(stack)
        self.assertEqual(stack.taps[0][1], target.center()[1])
        self.assertEqual(stack.taps[1], delete.center())
        self.assertIs(result, after)

    def test_delete_refuses_to_tap_when_exact_container_never_appears(self):
        stack = Stack()
        unchanged = [node("a0068")]

        with (
            mock.patch.object(crane_delete, "flick_list"),
            mock.patch.object(
                crane_delete,
                "dump",
                side_effect=[(b"", unchanged)] * 4,
            ),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            with self.assertRaisesRegex(crane_delete.PhoneError, "a0069 row"):
                crane_delete.tap_red_delete_r1(
                    stack, object(), unchanged, "a0069"
                )

        self.assertEqual(stack.taps, [])

    def test_delete_fails_if_target_row_remains_visible_after_tap(self):
        stack = Stack()
        target = node("a0069", x=110, y=410)
        delete = node("Delete", x=600, y=410)

        with (
            mock.patch.object(
                crane_delete,
                "dump",
                side_effect=[
                    (b"", [target, delete]),
                    (b"", [target]),
                ],
            ),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                crane_delete.PhoneError, "deletion was not confirmed"
            ):
                crane_delete.tap_red_delete_r1(
                    stack, object(), [target], "a0069"
                )


if __name__ == "__main__":
    unittest.main()
