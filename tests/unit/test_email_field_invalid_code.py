# UTF-8
"""Terminal 873 email-field failure: dedicated code, no submit, bounded retype.

The terminal condition is the exact production shape logged as
``873_email_still_invalid``: Reddit's signup welcomes the user, the email field
does not hold the exact saved @icloud.com address, one initial type already
happened, and at most one force-clear/retype is allowed.
Reaching it must raise ``reddit_email_field_invalid`` without any Continue tap.
"""

from __future__ import annotations

import unittest
from unittest import mock

from friend_poster.device.ssh_device import PhoneError
from friend_poster.device.ui_tree import Node
from friend_poster.reddit import reddit_to_verify as validator

EXPECTED = "fixture_grands.6h@icloud.com"
TERMINAL_MARKER = "873_email_still_invalid"


def ocr(text: str, x: int, y: int, w: int, h: int = 30) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=y, w=w, h=h)


# Immutable a0061 shape: the typed field lost exactly one saved local-part letter.
MISSING_ONE = [
    ocr("Hi new friend,", 370, 200, 334, 41),
    ocr("welcome to Reddit", 368, 244, 444, 30),
    ocr("Create an account to get started", 370, 316, 509, 25),
    ocr("Email", 95, 424, 72, 15),
    ocr("fixture_rands.6h@icloud.com", 292, 455, 478, 30),
    ocr("done", 653, 1168, 81, 23),
]
# Two saved letters lost: the bounded force-clear/retype must still recover it.
MISSING_TWO = [
    *MISSING_ONE[:-2],
    ocr("fixture_rands.6@icloud.com", 292, 455, 470, 30),
    MISSING_ONE[-1],
]
EXACT = [
    *MISSING_ONE[:-2],
    ocr(EXPECTED, 292, 455, 478, 30),
    MISSING_ONE[-1],
]
EXACT_KEYBOARD_HIDDEN = EXACT[:-1]


class FakeStack:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.backspaces: list[int] = []
        self.typed: list[str] = []
        self.human_delays: list[int] = []

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def press_backspace(self, count: int) -> None:
        self.backspaces.append(count)

    def type_text(self, text: str) -> None:
        self.typed.append(text)

    def type_human(self, text: str, ms: int = 95) -> None:
        self.typed.append(text)
        self.human_delays.append(ms)


class EmailFieldInvalidCodeTests(unittest.TestCase):
    def terminal_step(self, nodes, typed_once):
        """Run the real step() through the terminal 873 raise."""
        stack = FakeStack()
        typed = list(typed_once)
        dumps: list[str] = []
        with (
            mock.patch.object(validator, "log", lambda *_args, **_kwargs: None),
            mock.patch.object(validator, "_wait", lambda *_args, **_kwargs: None),
            mock.patch.object(
                validator,
                "dump_named",
                side_effect=lambda _stack, _phone, stem, _log, **_kwargs: (
                    dumps.append(stem) or (None, [])
                ),
            ),
            self.assertRaises(validator.EmailFieldInvalid) as caught,
        ):
            validator.step(
                stack,
                object(),
                nodes,
                EXPECTED,
                [True],
                "a0061",
                typed,
                [1],
                [0],
                [0],
                email_done_taps=[0],
            )
        return stack, typed, dumps, caught.exception

    def test_one_retype_used_and_field_still_invalid_raises_dedicated_code(self):
        stack, typed, dumps, exc = self.terminal_step(MISSING_ONE, [2])
        self.assertIsInstance(exc, PhoneError)
        self.assertEqual(exc.failure_code, "reddit_email_field_invalid")
        self.assertIn(TERMINAL_MARKER, str(exc))
        self.assertEqual(stack.taps, [])  # no Continue tap on the invalid field
        self.assertEqual(stack.backspaces, [])
        self.assertEqual(stack.typed, [])  # no second retype
        self.assertEqual(typed, [2])  # the allowed retype was already used
        self.assertEqual(dumps, ["873_email_terminal_recheck", TERMINAL_MARKER])

    def test_terminal_recheck_accepts_late_exact_value_without_another_action(self):
        stack = FakeStack()
        typed = [2]
        dumps: list[str] = []

        def fresh_dump(_stack, _phone, stem, _log, **_kwargs):
            dumps.append(stem)
            return None, EXACT

        with (
            mock.patch.object(validator, "log", lambda *_args, **_kwargs: None),
            mock.patch.object(validator, "_wait", lambda *_args, **_kwargs: None),
            mock.patch.object(validator, "dump_named", side_effect=fresh_dump),
        ):
            result = validator.step(
                stack,
                object(),
                MISSING_ONE,
                EXPECTED,
                [True],
                "a0061",
                typed,
                [1],
                [0],
                [0],
                email_done_taps=[0],
            )

        self.assertEqual(result, "WAIT")
        self.assertEqual(dumps, ["873_email_terminal_recheck"])
        self.assertEqual(typed, [2])
        self.assertEqual(stack.taps, [])
        self.assertEqual(stack.backspaces, [])
        self.assertEqual(stack.typed, [])

    def test_multiple_missing_characters_force_clear_and_retype_once(self):
        stack = FakeStack()
        typed = [1]
        with (
            mock.patch.object(validator, "log", lambda *_args, **_kwargs: None),
            mock.patch.object(validator, "_wait", lambda *_args, **_kwargs: None),
            mock.patch.object(
                validator,
                "dump_named",
                side_effect=AssertionError("no terminal dump on the retype path"),
            ),
        ):
            result = validator.step(
                stack,
                object(),
                MISSING_TWO,
                EXPECTED,
                [True],
                "a0061",
                typed,
                [1],
                [0],
                [0],
                email_done_taps=[0],
            )
        self.assertEqual(result, "RETYPED")
        self.assertEqual(typed, [2])
        self.assertEqual(stack.taps, [(620, 431)])
        self.assertEqual(stack.backspaces, [60, 20])
        self.assertEqual(stack.typed, [EXPECTED])
        self.assertEqual(stack.human_delays, [160])

    def test_proven_single_missing_character_still_retypes_exactly_once(self):
        stack = FakeStack()
        typed = [1]
        with (
            mock.patch.object(validator, "log", lambda *_args, **_kwargs: None),
            mock.patch.object(validator, "_wait", lambda *_args, **_kwargs: None),
            mock.patch.object(
                validator,
                "dump_named",
                side_effect=AssertionError("no terminal dump on the retype path"),
            ),
        ):
            result = validator.step(
                stack,
                object(),
                MISSING_ONE,
                EXPECTED,
                [True],
                "a0061",
                typed,
                [1],
                [0],
                [0],
                email_done_taps=[0],
            )
        self.assertEqual(result, "RETYPED")
        self.assertEqual(typed, [2])
        self.assertEqual(stack.typed, [EXPECTED])
        self.assertEqual(stack.human_delays, [160])

    def test_terminal_failure_is_ai_eligible_only_after_local_retype(self):
        self.assertIs(validator.EmailFieldInvalid.ai_recoverable, True)

    def test_done_hide_then_keyboard_reappears_is_retry_later_not_second_tap(self):
        stack = FakeStack()
        waits = [0]
        done_taps = [1]
        with (
            mock.patch.object(validator, "log", lambda *_args, **_kwargs: None),
            mock.patch.object(validator, "_wait", lambda *_args, **_kwargs: None),
        ):
            hidden_result = validator.step(
                stack,
                object(),
                EXACT_KEYBOARD_HIDDEN,
                EXPECTED,
                [True],
                "a0061",
                [1],
                [1],
                [0],
                waits,
                email_done_taps=done_taps,
            )
            with self.assertRaises(validator.RedditEmailRetryLater) as caught:
                validator.step(
                    stack,
                    object(),
                    EXACT,
                    EXPECTED,
                    [True],
                    "a0061",
                    [1],
                    [1],
                    [0],
                    waits,
                    email_done_taps=done_taps,
                )

        self.assertEqual(hidden_result, "WAIT")
        self.assertEqual(waits, [1])
        self.assertEqual(stack.taps, [])
        self.assertEqual(caught.exception.failure_code, "reddit_email_retry_later")
        self.assertIn("Retry manually later", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
