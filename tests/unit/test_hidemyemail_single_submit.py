"""Regression tests for deterministic, single-submit iCloud password entry."""

from __future__ import annotations

import unittest
from threading import Event
from unittest import mock

from friend_poster.device import hidemyemail
from friend_poster.device.ui_tree import Node


SECRET = "test-only-runtime-secret"


def _node(text, x, y, w, h):
    return Node(ntype="ocr", text=text, x=x, y=y, w=w, h=h)


SHEET = [
    _node("Sign in to iCloud", 237, 349, 268, 31),
    _node("Password", 146, 488, 122, 18),
    _node("Cancel", 178, 566, 118, 25),
    _node("OK", 472, 560, 65, 33),
]
DESTINATION = [_node("Hide My Email", 100, 100, 200, 30)]


class Stack:
    def __init__(self):
        self.taps = []
        self.typed = []
        self.long_taps = []
        self.opened = []

    def can_tap(self):
        return True

    def tap_xy(self, x, y):
        self.taps.append((x, y))

    def type_text(self, value):
        self.typed.append(value)

    def semantic_keyboard_present(self):
        return None

    def open_url(self, url):
        self.opened.append(url)


def _run(responses):
    stack = Stack()
    seen = []

    def dump(_stack, _phone, stem, _log):
        seen.append(stem)
        return None, responses[stem]

    with (
        mock.patch.object(hidemyemail, "load_secrets", return_value={"icloud_password": SECRET}),
        mock.patch.object(hidemyemail, "dump_named", side_effect=dump),
        mock.patch.object(hidemyemail.time, "sleep", return_value=None),
    ):
        result = hidemyemail._unlock_icloud_if_needed(
            stack, object(), lambda _message: None, SHEET, None
        )
    return result, stack, seen


class SingleSubmitTests(unittest.TestCase):
    def test_types_runtime_secret_once_without_long_press_or_keyboard_ocr(self):
        result, stack, _seen = _run(
            {
                "03_hme_password_focus_1": SHEET,
                "03_hme_password_before_ok": SHEET,
                "03_hme_after_icloud_submit": DESTINATION,
            }
        )
        self.assertIs(result, DESTINATION)
        self.assertEqual(stack.typed, [SECRET])
        self.assertEqual(stack.long_taps, [])
        self.assertEqual(stack.taps, [(207, 497), (504, 576)])

    def test_selection_overlay_is_never_clicked_or_typed_into(self):
        controls = hidemyemail._icloud_password_sheet_controls(SHEET)
        overlay = [
            controls.title,
            controls.cancel,
            controls.ok,
            _node("Select All user@icloud.com", 210, 410, 320, 25),
        ]
        stack = Stack()
        with (
            mock.patch.object(hidemyemail, "load_secrets", return_value={"icloud_password": SECRET}),
            mock.patch.object(hidemyemail, "dump_named", return_value=(None, overlay)),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
            self.assertRaises(hidemyemail.ICloudPasswordEntryError),
        ):
            hidemyemail._unlock_icloud_if_needed(
                stack, object(), lambda _message: None, SHEET, None
            )
        self.assertEqual(stack.typed, [])
        self.assertEqual(stack.taps, [(207, 497)])
        self.assertEqual(stack.long_taps, [])

    def test_sheet_remaining_after_submit_never_retypes(self):
        stack = Stack()

        def dump(_stack, _phone, stem, _log):
            return None, SHEET

        with (
            mock.patch.object(hidemyemail, "load_secrets", return_value={"icloud_password": SECRET}),
            mock.patch.object(hidemyemail, "dump_named", side_effect=dump),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
            mock.patch.object(hidemyemail, "_SHEET_SUBMIT_WAIT_SECONDS", 0.0),
            self.assertRaisesRegex(
                hidemyemail.ICloudPasswordEntryError, "one password submission"
            ),
        ):
            hidemyemail._unlock_icloud_if_needed(
                stack, object(), lambda _message: None, SHEET, None
            )
        self.assertEqual(stack.typed, [SECRET])
        self.assertEqual(stack.long_taps, [])

    def test_overlay_is_cancelled_then_fresh_sheet_types_once(self):
        controls = hidemyemail._icloud_password_sheet_controls(SHEET)
        overlay = [
            controls.title,
            controls.cancel,
            controls.ok,
            _node("Select All user@icloud.com", 210, 410, 320, 25),
        ]
        frames = {
            "03_hme_icloud_cancel_attempt": overlay,
            "03_hme_icloud_overlay_closed": SHEET,
            "03_hme_icloud_cancelled": DESTINATION,
            hidemyemail._FRESH_SHEET_STEM: SHEET,
            "03_hme_password_before_ok": SHEET,
            "03_hme_after_icloud_submit": DESTINATION,
        }
        stack = Stack()
        focus_frames = [overlay, SHEET]

        def dump(_stack, _phone, stem, _log):
            if stem == "03_hme_password_focus_1":
                return None, focus_frames.pop(0)
            return None, frames[stem]

        with (
            mock.patch.object(hidemyemail, "load_secrets", return_value={"icloud_password": SECRET}),
            mock.patch.object(hidemyemail, "dump_named", side_effect=dump),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
        ):
            result = hidemyemail._unlock_icloud_with_fresh_sheet_retry(
                stack, object(), lambda _message: None, SHEET, None
            )
        self.assertIs(result, DESTINATION)
        self.assertEqual(stack.typed, [SECRET])
        self.assertEqual(stack.long_taps, [])
        self.assertEqual(stack.opened, [hidemyemail.HME_SETTINGS_URL])

    def test_password_never_appears_in_logs_or_exception(self):
        messages = []
        stack = Stack()
        with (
            mock.patch.object(hidemyemail, "load_secrets", return_value={"icloud_password": SECRET}),
            mock.patch.object(hidemyemail, "dump_named", return_value=(None, [])),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
            self.assertRaises(hidemyemail.ICloudPasswordEntryError) as caught,
        ):
            hidemyemail._unlock_icloud_if_needed(
                stack, object(), messages.append, SHEET, None
            )
        self.assertNotIn(SECRET, "\n".join(messages))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_unknown_post_tap_frame_never_types(self):
        stack = Stack()
        with (
            mock.patch.object(hidemyemail, "load_secrets", return_value={"icloud_password": SECRET}),
            mock.patch.object(hidemyemail, "dump_named", return_value=(None, [])),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
            self.assertRaisesRegex(
                hidemyemail.ICloudPasswordEntryError, "unknown screen"
            ),
        ):
            hidemyemail._unlock_icloud_if_needed(
                stack, object(), lambda _message: None, SHEET, None
            )
        self.assertEqual(stack.typed, [])
        self.assertEqual(stack.taps, [(207, 497)])

    def test_stop_after_focus_tap_prevents_typing(self):
        stop = Event()
        stack = Stack()
        original_tap = stack.tap_xy

        def tap_then_stop(x, y):
            original_tap(x, y)
            stop.set()

        stack.tap_xy = tap_then_stop
        with (
            mock.patch.object(hidemyemail, "load_secrets", return_value={"icloud_password": SECRET}),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
            self.assertRaisesRegex(Exception, "Stopped"),
        ):
            hidemyemail._unlock_icloud_if_needed(
                stack, object(), lambda _message: None, SHEET, stop
            )
        self.assertEqual(stack.typed, [])


if __name__ == "__main__":
    unittest.main()
