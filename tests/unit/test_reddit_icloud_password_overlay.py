"""Regressions for the iCloud password overlay above Reddit signup."""

from __future__ import annotations

import unittest
from unittest import mock

from friend_poster.device.ssh_device import PhoneError
from friend_poster.device.ui_tree import Node
from friend_poster.reddit import reddit_to_verify as signup


def ocr(text: str, x: int, y: int, w: int = 180, h: int = 30) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=y, w=w, h=h)


def overlay_nodes() -> list[Node]:
    return [
        ocr("Hi new friend,", 206, 179, 331, 42),
        ocr("welcome to Reddit", 150, 229, 440, 30),
        ocr("Cancel Cancel Cancel", 22, 123, 118, 23),
        ocr("Sign in to iCloud Sign in to iCloud", 237, 345, 273, 36),
        ocr(
            "Enter the password for your Apple ID Enter the password for your Apple ID",
            147,
            390,
            453,
            20,
        ),
        ocr("Ema Ema Ema", 59, 414, 50, 18),
        ocr("Password Password Password", 140, 488, 128, 19),
        ocr("Cancel Cancel Cancel", 182, 566, 115, 25),
        ocr("OK OK OK", 475, 560, 62, 28),
        ocr("Continue Continue Continue", 300, 1210, 140, 24),
    ]


class ICloudPasswordOverlayTests(unittest.TestCase):
    def test_step_cancels_only_the_proven_modal_action_once(self):
        class Stack:
            def __init__(self):
                self.taps = []

            def tap_xy(self, x, y):
                self.taps.append((x, y))

        stack = Stack()
        cancel_taps = [0]
        with mock.patch.object(signup, "_wait", return_value=None):
            result = signup.step(
                stack,
                object(),
                overlay_nodes(),
                "saved@icloud.com",
                [False],
                "a0001",
                [0],
                [0],
                [0],
                [0],
                icloud_prompt_cancel_taps=cancel_taps,
            )

        self.assertEqual(result, "ICLOUD_PASSWORD_CANCEL")
        self.assertEqual(stack.taps, [(239, 578)])
        self.assertEqual(cancel_taps, [1])

    def test_persistent_overlay_fails_closed_without_second_tap(self):
        class Stack:
            def __init__(self):
                self.taps = []

            def tap_xy(self, x, y):
                self.taps.append((x, y))

        stack = Stack()
        with self.assertRaisesRegex(PhoneError, "remained after one bounded"):
            signup.step(
                stack,
                object(),
                overlay_nodes(),
                "saved@icloud.com",
                [False],
                "a0001",
                [0],
                [0],
                [0],
                [0],
                icloud_prompt_cancel_taps=[1],
            )
        self.assertEqual(stack.taps, [])


if __name__ == "__main__":
    unittest.main()
