"""Offline checks for exact usernames supplied by a Creator batch."""

from __future__ import annotations

import unittest
from unittest import mock

import reddit_username as usernames
from ssh_device import PhoneError
from ui_tree import Node


def node(text: str, x: int, y: int, w: int = 220) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=y, w=w, h=22)


def page(value: str = "", available: bool = False) -> list[Node]:
    rows = [
        node("Create your username", 110, 180, 520),
        node("Username", 60, 398, 135),
        node("Continue", 300, 1200, 140),
    ]
    if value:
        rows.append(node(value, 90, 430, 320))
    if available:
        rows.append(node("Great name! It's not taken, so it's all yours.", 80, 480, 500))
    return rows


class Stack:
    def __init__(self):
        self.taps = []
        self.backspaces = []
        self.typed = []

    def tap_xy(self, x, y):
        self.taps.append((x, y))

    def press_backspace(self, count):
        self.backspaces.append(count)

    def type_text(self, value):
        self.typed.append(value)


class RequestedUsernameTests(unittest.TestCase):
    def test_ios_selection_bubble_above_label_is_not_field_value(self):
        cleared = page()
        cleared.extend([
            node("Ever", 60, 352, 80),
            node("Even-Arrival6200", 40, 590, 240),
        ])
        self.assertIsNone(usernames.suggested_username(cleared))

    def test_username_value_below_label_is_still_detected(self):
        self.assertEqual(
            usernames.suggested_username(page("Chosen_Name9")),
            "Chosen_Name9",
        )

    def test_resume_with_empty_field_and_open_keyboard_types_without_reclearing(self):
        keyboard = page()
        continue_node = next(row for row in keyboard if row.text == "Continue")
        continue_node.y = 700
        keyboard.extend([
            node("123", 50, 1140, 80),
            node("space", 300, 1140, 100),
            node("return", 600, 1140, 100),
            node("Ever", 60, 352, 80),
        ])
        frames = iter([
            keyboard,
            page("Chosen_Name9", True),
            [node("Set a password", 180, 200, 340)],
        ])
        result, stack, written, names = self.run_case(frames)
        self.assertEqual(result, "Chosen_Name9")
        self.assertEqual(stack.backspaces, [])
        self.assertEqual(stack.typed, ["Chosen_Name9"])
        self.assertEqual(names, ["890_username", "890_username_requested", "891_after_username"])
        self.assertEqual(written, [("slot", "Chosen_Name9"), ("account", "Chosen_Name9")])

    def run_case(self, frames, saved=""):
        stack = Stack()
        written = []
        names = []

        def dump(_stack, _phone, stem, _log, shot=True):
            names.append(stem)
            return "", next(frames)

        with (
            mock.patch.object(usernames, "get_slot", return_value={"email": "test@icloud.com", "username": saved}),
            mock.patch.object(usernames, "dump_named", side_effect=dump),
            mock.patch.object(usernames, "set_slot_username", side_effect=lambda *args: written.append(("slot", args[1]))),
            mock.patch.object(usernames, "write_account_text", side_effect=lambda *args: written.append(("account", args[2])) or "account.txt"),
            mock.patch.object(usernames.time, "sleep", return_value=None),
            mock.patch.object(usernames, "log", return_value=None),
        ):
            try:
                result = usernames.use_requested_username(stack, object(), "a0049", "Chosen_Name9")
            except PhoneError as exc:
                result = exc
        return result, stack, written, names

    def test_exact_available_name_is_used_and_saved_after_navigation(self):
        frames = iter([
            page("RedditSuggestion"), page(), page("Chosen_Name9", True),
            [node("Set a password", 180, 200, 340)],
        ])
        result, stack, written, names = self.run_case(frames)
        self.assertEqual(result, "Chosen_Name9")
        self.assertEqual(stack.backspaces, [40])
        self.assertEqual(stack.typed, ["Chosen_Name9"])
        self.assertEqual(len(stack.taps), 2)
        self.assertEqual(written, [("slot", "Chosen_Name9"), ("account", "Chosen_Name9")])
        self.assertEqual(names, ["890_username", "890_username_cleared", "890_username_requested", "891_after_username"])

    def test_unavailable_name_pauses_without_suggestion_or_save(self):
        frames = iter([page("RedditSuggestion"), page(), page("Chosen_Name9", False)])
        result, stack, written, names = self.run_case(frames)
        self.assertIsInstance(result, PhoneError)
        self.assertIn("not confirmed", str(result))
        self.assertEqual(len(stack.taps), 1)
        self.assertEqual(written, [])

    def test_saved_mismatch_stops_before_any_phone_action(self):
        result, stack, written, names = self.run_case(iter([]), saved="OtherName")
        self.assertIsInstance(result, PhoneError)
        self.assertEqual((stack.taps, stack.typed, written, names), ([], [], [], []))


if __name__ == "__main__":
    unittest.main()
