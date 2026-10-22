"""Mocked navigation checks for Hide My Email's iCloud re-auth path.

Two real sanitized dumps drive the two supported modal layouts:

* ``a0048_icloud_pause.json`` -- the older layout where the empty secure field
  is exposed as a ``Password`` node.
* ``tests/fixtures/hme_second_row_tap_sheet.json`` -- the current layout where
  XXTouch omits the empty secure field, so its tap point must be derived from
  the verified modal geometry instead.

Frames that stand in for a *live* run come from ``tests/fixtures`` (immutable,
sanitized copies), because live runs overwrite ``dumps/03_hme_*`` in place.

No phone, no network: every tap/text/dump goes through a fake stack.
"""

from __future__ import annotations

import unittest
from datetime import date
from threading import Event
from unittest import mock

from friend_poster.device import hidemyemail
from friend_poster.device.ssh_device import PhoneError
from friend_poster.device.ui_tree import Node, TapStack, collapsed_text, parse_nodes
from friend_poster.core.paths import DUMP_DIR, FIXTURES_DIR

SECRET = "test-only-secret-pw"


def node(
    label: str,
    y: int,
    x: int = 100,
    w: int = 250,
    h: int = 40,
    ntype: str = "",
) -> Node:
    return Node(text=label, x=x, y=y, w=w, h=h, ntype=ntype)


def load(name: str) -> list[Node]:
    return parse_nodes((DUMP_DIR / name).read_text(encoding="utf-8"))


def load_fixture(name: str) -> list[Node]:
    """Immutable, sanitized copy of a real frame (``tests/fixtures``).

    Live runs rewrite ``dumps/03_hme_*`` in place -- a0079 overwrote the a0076
    focus frames this suite used to read, which silently changed what these
    tests asserted.  Anything asserted about a *real* frame therefore has to
    come from a fixture a live run cannot touch.
    """
    return parse_nodes((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# Real dumps: the two supported modal layouts.
SHEET_NO_PASSWORD = load_fixture("hme_second_row_tap_sheet.json")
SHEET_WITH_PASSWORD = load("a0048_icloud_pause.json")
# Real sanitized a0079 frames.  A0079_FOCUS_2 is the verified sheet whose empty
# secure field is exposed as a real ``Password`` node; A0079_FOCUS_1 is a0079's
# first post-tap frame (identical verified sheet, still no keyboard); the
# A0079_*_cancelled frame is the one recorded after the verified Cancel tap,
# where the sheet is gone entirely.
A0079_FOCUS_1 = load_fixture("a0079_password_focus_1.json")
A0079_FOCUS_2 = load_fixture("a0079_password_focus_2.json")
A0079_CANCELLED = load_fixture("a0079_icloud_cancelled.json")
# Real sanitized a0083 frames: a fully verified derived-layout sheet followed
# by the post-tap selection overlay that corrupted the instruction/Apple-ID OCR
# while leaving the title and both modal buttons in place.
A0083_BEFORE_FOCUS = load_fixture("a0083_before_create_retry.json")
A0083_SELECTION_OVERLAY = load_fixture(
    "a0083_password_selection_overlay.json"
)
# The on-screen keyboard is OCR-visible while a text field has focus.  A
# fresh post-tap frame still shows the verified sheet plus the keyboard.
KEYBOARD = SHEET_NO_PASSWORD + [node("space", 1170, x=256, w=231, h=25, ntype="ocr")]
KEYBOARD_WITH_PASSWORD = SHEET_WITH_PASSWORD + [
    node("space", 1170, x=256, w=231, h=25, ntype="ocr")
]
# An edit-menu "Select All" raised by a long-press over the secure field row.
MENU_SELECT_ALL = KEYBOARD + [
    node("Select All", 505, x=300, w=140, h=22, ntype="ocr")
]
# OCR-visible content sitting in the verified secure-field band.
CONTENT_IN_FIELD = SHEET_NO_PASSWORD + [
    node("••••••••", 500, x=310, w=120, h=18, ntype="ocr")
]

OVERVIEW = [
    node("Manage Account Storage", 300),
    node("APPS USING ICLOUD", 450),
    node("Hide My Email", 1050),
]
SHEET = [node("Sign in to iCloud", 300), node("Password", 500), node("OK", 700)]
HME_LIST = [node("Hide My Email", 100), node("Create New Address", 400)]


class Stack:
    def __init__(
        self,
        stop_after_create: bool = False,
        stop_on_tap: int | None = None,
        can_tap: bool = True,
        semantic_keyboard: bool | None = None,
    ):
        self.taps = []
        self.swipes = []
        self.typed = []
        self.long_taps = []
        self.opened = []
        self._can_tap = can_tap
        self.stop_after_create = stop_after_create
        self.stop_on_tap = stop_on_tap
        self.after_type = None
        self.semantic_keyboard = semantic_keyboard

    def open_url(self, url):
        self.opened.append(url)

    def can_tap(self):
        return self._can_tap

    def tap_xy(self, x, y):
        if not self._can_tap:
            raise AssertionError("tap_xy called while can_tap() is False")
        self.taps.append((x, y))
        stop_at = self.stop_on_tap or (3 if self.stop_after_create else None)
        if stop_at is not None and len(self.taps) == stop_at:
            raise StopAfterCreate()

    def long_tap(self, x, y, hold_ms=550):
        if not self._can_tap:
            raise AssertionError("long_tap called while can_tap() is False")
        self.long_taps.append((x, y))

    def swipe(self, *coords):
        self.swipes.append(coords)

    def type_text(self, value):
        self.typed.append(value)
        if self.after_type is not None:
            self.after_type()

    def semantic_keyboard_present(self):
        return self.semantic_keyboard


class StopAfterCreate(Exception):
    pass


def routed_dump(responses, fallback):
    """A dump_named double keyed by dump stem; returns the seen stems.

    A stem mapped to a list of frames returns them in order and then repeats the
    last one, so a stem requested twice -- a stuck sheet, then its fresh
    replacement -- can answer with different frames.  A plain list of nodes is
    a single frame and is returned unchanged.
    """
    seen: list[str] = []

    def dump(_stack, _phone, stem, _log):
        seen.append(stem)
        value = responses.get(stem, fallback)
        if isinstance(value, list) and value and isinstance(value[0], list):
            return None, (value.pop(0) if len(value) > 1 else value[0])
        return None, value

    return dump, seen


def unlock(stack, nodes, stop=None, log=None, responses=None, fallback=None):
    """Run the real unlock step against routed dumps."""
    log = log if log is not None else (lambda _message: None)
    router, seen = routed_dump(responses or {}, fallback or HME_LIST)
    with (
        mock.patch.object(
            hidemyemail, "load_secrets", return_value={"icloud_password": SECRET}
        ),
        mock.patch.object(hidemyemail, "dump_named", side_effect=router),
        mock.patch.object(hidemyemail.time, "sleep", return_value=None),
    ):
        result = hidemyemail._unlock_icloud_if_needed(stack, object(), log, nodes, stop)
    return result, seen


def recover(stack, nodes, stop=None, log=None, responses=None, fallback=None):
    """Run the real fresh-sheet recovery step against routed dumps."""
    log = log if log is not None else (lambda _message: None)
    router, seen = routed_dump(responses or {}, fallback or HME_LIST)
    with (
        mock.patch.object(
            hidemyemail, "load_secrets", return_value={"icloud_password": SECRET}
        ),
        mock.patch.object(hidemyemail, "dump_named", side_effect=router),
        mock.patch.object(hidemyemail.time, "sleep", return_value=None),
    ):
        result = hidemyemail._unlock_icloud_with_fresh_sheet_retry(
            stack, object(), log, nodes, stop
        )
    return result, seen


class SheetVerificationTests(unittest.TestCase):
    def test_missing_password_node_layout_derives_the_field_point(self):
        controls = hidemyemail._icloud_password_sheet_controls(SHEET_NO_PASSWORD)
        self.assertIsNotNone(controls)
        self.assertTrue(controls.derived)
        self.assertIsNone(controls.field)
        # Modal centre-x at the vertical middle of the Apple ID / buttons gap.
        self.assertEqual(controls.field_point, (371, 500))
        self.assertEqual(controls.ok.center(), (504, 576))
        self.assertEqual(controls.cancel.center(), (237, 578))

    def test_password_node_layout_keeps_using_the_placeholder(self):
        controls = hidemyemail._icloud_password_sheet_controls(SHEET_WITH_PASSWORD)
        self.assertIsNotNone(controls)
        self.assertFalse(controls.derived)
        self.assertEqual(controls.field_point, (207, 497))
        self.assertEqual(controls.ok.center(), (504, 576))

    def test_password_node_layout_survives_missing_instruction_labels(self):
        # Preserve the older layout's support: when the empty field is an
        # explicit Password node, the bounded title/Cancel/OK geometry is
        # enough even if the instruction/Apple-ID OCR is absent.
        minimal = [
            Node(ntype="ocr", text="Sign in to iCloud", x=237, y=349, w=268, h=31),
            Node(ntype="ocr", text="Password", x=146, y=488, w=122, h=18),
            Node(ntype="ocr", text="Cancel", x=178, y=566, w=118, h=25),
            Node(ntype="ocr", text="OK", x=472, y=560, w=65, h=33),
        ]
        controls = hidemyemail._icloud_password_sheet_controls(minimal)
        self.assertIsNotNone(controls)
        self.assertFalse(controls.derived)
        self.assertIsNone(controls.instruction)
        self.assertIsNone(controls.apple_id)
        self.assertEqual(controls.field_point, (207, 497))

    def test_derived_point_is_inside_the_verified_modal_band(self):
        controls = hidemyemail._icloud_password_sheet_controls(SHEET_NO_PASSWORD)
        fx, fy = controls.field_point
        self.assertLess(controls.apple_id.y, fy)  # below the Apple ID line
        self.assertLess(fy, controls.cancel.y)  # above the buttons
        self.assertGreater(fx, controls.title.x)  # inside the modal body

    def test_unrelated_or_incomplete_or_misplaced_sheet_is_rejected(self):
        extra_ok = SHEET_NO_PASSWORD + [Node(text="ОК", x=472, y=576, w=65, h=33, ntype="ocr")]
        misplaced = [
            n for n in SHEET_NO_PASSWORD if collapsed_text(n.labels()) != "ok"
        ] + [Node(text="OK", x=472, y=200, w=65, h=33, ntype="ocr")]
        unrelated = [
            node("Delete Address?", 300),
            node("Cancel", 400),
            node("Delete", 500),
        ]
        for candidate in (SHEET, extra_ok, misplaced, unrelated):
            with self.subTest(candidate=len(candidate)):
                self.assertIsNone(
                    hidemyemail._icloud_password_sheet_controls(candidate)
                )

    def test_transient_corrupted_sheet_gets_one_fresh_verification_before_typing(self):
        # Sanitized shape of the live a0236 first frame: the modal title and
        # Password row were present, but Settings OCR merged both button labels
        # with the underlying iCloud rows.  The next passive dump was complete.
        transient = [
            Node(ntype="ocr", text="Sign in to iCloud", x=232, y=408, w=281, h=28),
            Node(ntype="ocr", text="Password", x=130, y=551, w=132, h=27),
            Node(ntype="ocr", text="icfancelail", x=144, y=633, w=185, h=38),
        ]
        stack = Stack()
        result, seen = unlock(
            stack,
            transient,
            responses={
                "03_hme_password_controls_retry": SHEET_WITH_PASSWORD,
                "03_hme_password_focus_1": SHEET_WITH_PASSWORD,
                "03_hme_password_before_ok": SHEET_WITH_PASSWORD,
                "03_hme_after_icloud_submit": HME_LIST,
            },
        )
        self.assertIs(result, HME_LIST)
        self.assertEqual(stack.typed, [SECRET])
        self.assertEqual(
            seen,
            [
                "03_hme_password_controls_retry",
                "03_hme_password_focus_1",
                "03_hme_password_before_ok",
                "03_hme_after_icloud_submit",
            ],
        )


@unittest.skip(
    "legacy Select-All/backspace/retype contract removed; replaced by "
    "test_hidemyemail_single_submit.py"
)
class UnlockFlowTests(unittest.TestCase):
    def test_derived_layout_clears_then_types_once_after_first_focus_tap(self):
        # The derived layout has no Password placeholder, so emptiness needs
        # positive evidence: the first probe finds verified content (Select
        # All), it is cleared, and a fresh re-probe of the focused field
        # (keyboard retained) no longer offers Select All before typing once.
        stack = Stack()
        result, seen = unlock(
            stack,
            SHEET_NO_PASSWORD,
            responses={
                "03_hme_password_focus_1": KEYBOARD,
                "03_hme_password_field_menu_1": MENU_SELECT_ALL,  # content found
                "03_hme_password_field_menu_2": KEYBOARD,  # clean re-probe
                "03_hme_password_before_ok": KEYBOARD,
                "03_hme_after_icloud_submit": HME_LIST,
            },
        )
        self.assertIs(result, HME_LIST)
        self.assertEqual(stack.typed, ["\b", SECRET])
        # focus tap -> verified Select All tap -> fresh OK tap
        self.assertEqual(stack.taps, [(371, 500), (370, 516), (504, 576)])
        # The emptiness oracle long-presses only the verified field point.
        self.assertEqual(stack.long_taps, [(371, 500), (371, 500)])
        self.assertEqual(
            seen,
            [
                "03_hme_password_focus_1",
                "03_hme_password_field_menu_1",
                "03_hme_password_field_menu_2",
                "03_hme_password_before_ok",
                "03_hme_after_icloud_submit",
            ],
        )

    def test_focus_retry_taps_the_field_once_more_then_types_once(self):
        stack = Stack()
        result, seen = unlock(
            stack,
            SHEET_NO_PASSWORD,
            responses={
                "03_hme_password_focus_1": SHEET_NO_PASSWORD,  # keyboard absent
                "03_hme_password_focus_2": KEYBOARD,  # focus retry
                "03_hme_password_field_menu_1": MENU_SELECT_ALL,  # content found
                "03_hme_password_field_menu_2": KEYBOARD,  # clean re-probe
                "03_hme_password_before_ok": KEYBOARD,
                "03_hme_after_icloud_submit": HME_LIST,
            },
        )
        self.assertIs(result, HME_LIST)
        self.assertEqual(stack.typed, ["\b", SECRET])
        self.assertEqual(
            stack.taps, [(371, 500), (371, 500), (370, 516), (504, 576)]
        )
        self.assertEqual(
            seen,
            [
                "03_hme_password_focus_1",
                "03_hme_password_focus_2",
                "03_hme_password_field_menu_1",
                "03_hme_password_field_menu_2",
                "03_hme_password_before_ok",
                "03_hme_after_icloud_submit",
            ],
        )

    def test_missing_keyboard_after_two_taps_never_types(self):
        stack = Stack()
        with self.assertRaisesRegex(PhoneError, "keyboard did not appear"):
            unlock(
                stack,
                SHEET_NO_PASSWORD,
                responses={
                    "03_hme_password_focus_1": SHEET_NO_PASSWORD,
                    "03_hme_password_focus_2": SHEET_NO_PASSWORD,
                },
            )
        self.assertEqual(stack.typed, [])
        self.assertEqual(stack.taps, [(371, 500), (371, 500)])

    def test_wrong_password_is_detected_immediately_and_not_retyped(self):
        wrong = SHEET_NO_PASSWORD + [
            node("Incorrect Apple ID or password.", 470, x=200, w=350, h=20, ntype="ocr")
        ]
        stack = Stack()
        with self.assertRaises(hidemyemail.ICloudPasswordEntryError) as caught:
            unlock(
                stack,
                SHEET_NO_PASSWORD,
                responses={
                    "03_hme_password_focus_1": KEYBOARD,
                    "03_hme_password_field_menu_1": MENU_SELECT_ALL,
                    "03_hme_password_field_menu_2": KEYBOARD,
                    "03_hme_password_before_ok": KEYBOARD,
                    "03_hme_after_icloud_submit": wrong,
                },
            )
        self.assertEqual(caught.exception.failure_code, "icloud_password_entry_failed")
        self.assertIn("did not type it again", str(caught.exception))
        # A rejection is flagged so the controller pauses instead of retrying.
        self.assertIs(caught.exception.credentials_rejected, True)
        self.assertEqual(stack.typed, ["\b", SECRET])
        self.assertEqual(stack.taps, [(371, 500), (370, 516), (504, 576)])

    def test_timeout_without_empty_proof_fails_closed_without_a_retype(self):
        stack = Stack()
        with (
            mock.patch.object(hidemyemail, "_SHEET_SUBMIT_WAIT_SECONDS", 0.0),
            self.assertRaisesRegex(
                hidemyemail.ICloudPasswordEntryError, "could not be confirmed"
            ),
        ):
            unlock(
                stack,
                SHEET_NO_PASSWORD,
                responses={
                    "03_hme_password_focus_1": KEYBOARD,
                    "03_hme_password_field_menu_1": MENU_SELECT_ALL,  # content
                    "03_hme_password_field_menu_2": KEYBOARD,  # cleared -> typed
                    "03_hme_password_before_ok": KEYBOARD,
                    "03_hme_after_icloud_submit": SHEET_NO_PASSWORD,
                    # The retype emptiness probe keeps finding "Select All":
                    # clearing never confirms, so no retype may happen.
                    "03_hme_password_retype_field_menu_1": MENU_SELECT_ALL,
                    "03_hme_password_retype_field_menu_2": MENU_SELECT_ALL,
                },
            )
        # The saved password was typed exactly once; only clearing
        # backspaces followed, never a second password.
        self.assertEqual(stack.typed.count(SECRET), 1)
        self.assertNotIn(SECRET, stack.typed[2:])

    def test_empty_field_proof_allows_exactly_one_safe_retype(self):
        stack = Stack()
        with mock.patch.object(hidemyemail, "_SHEET_SUBMIT_WAIT_SECONDS", 0.0):
            result, seen = unlock(
                stack,
                SHEET_WITH_PASSWORD,
                responses={
                    "03_hme_password_focus_1": KEYBOARD_WITH_PASSWORD,
                    "03_hme_password_before_ok": KEYBOARD_WITH_PASSWORD,
                    "03_hme_after_icloud_submit": SHEET_WITH_PASSWORD,
                    "03_hme_password_retype_focus_1": KEYBOARD_WITH_PASSWORD,
                    "03_hme_after_icloud_retype_submit": HME_LIST,
                },
            )
        self.assertIs(result, HME_LIST)
        self.assertEqual(stack.typed, [SECRET, SECRET])
        self.assertEqual(
            stack.taps,
            [(207, 497), (504, 576), (207, 497), (504, 576)],
        )
        # The Password placeholder proves emptiness, so no long-press probe
        # is ever needed on the older layout.
        self.assertEqual(stack.long_taps, [])
        self.assertEqual(
            seen,
            [
                "03_hme_password_focus_1",
                "03_hme_password_before_ok",
                "03_hme_after_icloud_submit",
                "03_hme_password_retype_focus_1",
                "03_hme_password_before_ok",
                "03_hme_after_icloud_retype_submit",
            ],
        )

    def test_sheet_that_survives_the_retype_fails_closed(self):
        stack = Stack()
        with (
            mock.patch.object(hidemyemail, "_SHEET_SUBMIT_WAIT_SECONDS", 0.0),
            self.assertRaisesRegex(PhoneError, "remained after one retype"),
        ):
            unlock(
                stack,
                SHEET_WITH_PASSWORD,
                responses={
                    "03_hme_password_focus_1": KEYBOARD_WITH_PASSWORD,
                    "03_hme_password_before_ok": KEYBOARD_WITH_PASSWORD,
                    "03_hme_after_icloud_submit": SHEET_WITH_PASSWORD,
                    "03_hme_password_retype_focus_1": KEYBOARD_WITH_PASSWORD,
                    "03_hme_after_icloud_retype_submit": SHEET_WITH_PASSWORD,
                },
            )
        self.assertEqual(stack.typed, [SECRET, SECRET])  # never a third time

    def test_unrelated_dialog_is_left_untouched(self):
        stack = Stack()
        unrelated = [node("Delete Address?", 300), node("Cancel", 400), node("OK", 500)]
        with mock.patch.object(
            hidemyemail, "dump_named", side_effect=AssertionError("no dump expected")
        ):
            result = hidemyemail._unlock_icloud_if_needed(
                stack, object(), lambda _message: None, unrelated, None
            )
        self.assertIs(result, unrelated)
        self.assertEqual(stack.typed, [])
        self.assertEqual(stack.taps, [])

    def test_ambiguous_sheet_never_types(self):
        for candidate in (
            SHEET,
            SHEET_NO_PASSWORD + [Node(text="ОК", x=472, y=576, w=65, h=33, ntype="ocr")],
        ):
            stack = Stack()
            with self.assertRaises(PhoneError):
                hidemyemail._unlock_icloud_if_needed(
                    stack, object(), lambda _message: None, candidate, None
                )
            self.assertEqual(stack.typed, [])
            self.assertEqual(stack.taps, [])

    def test_user_stop_before_typing_is_honored(self):
        stack = Stack()
        with (
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
            mock.patch.object(
                hidemyemail, "load_secrets", return_value={"icloud_password": SECRET}
            ),
            mock.patch.object(
                hidemyemail, "dump_named", side_effect=AssertionError("no dump expected")
            ),
            self.assertRaisesRegex(PhoneError, "Stopped"),
        ):
            hidemyemail._unlock_icloud_if_needed(
                stack, object(), lambda _message: None, SHEET_NO_PASSWORD, StopFlag(True)
            )
        self.assertEqual(stack.typed, [])
        self.assertEqual(stack.taps, [])

    def test_user_stop_during_submit_poll_is_honored(self):
        flag = StopFlag(False)
        stack = Stack()
        # Stop is requested only once the real password is in (not during a
        # clearing backspace), so the poll itself must honor it.
        stack.after_type = (
            lambda: flag.set() if SECRET in stack.typed else None
        )
        with (
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
            mock.patch.object(
                hidemyemail, "load_secrets", return_value={"icloud_password": SECRET}
            ),
            mock.patch.object(
                hidemyemail,
                "dump_named",
                side_effect=lambda *_a: (None, MENU_SELECT_ALL)
                if _a[2] == "03_hme_password_field_menu_1"
                else (None, KEYBOARD)
                if _a[2].startswith(
                    (
                        "03_hme_password_focus",
                        "03_hme_password_field_menu",
                        "03_hme_password_before_ok",
                    )
                )
                else (_ for _ in ()).throw(AssertionError("poll after Stop")),
            ),
            self.assertRaisesRegex(PhoneError, "Stopped"),
        ):
            hidemyemail._unlock_icloud_if_needed(
                stack, object(), lambda _message: None, SHEET_NO_PASSWORD, flag
            )
        # Detected content was cleared, then the password typed exactly once.
        self.assertEqual(stack.typed, ["\b", SECRET])
        self.assertEqual(stack.taps, [(371, 500), (370, 516), (504, 576)])

    def test_password_value_is_never_logged(self):
        stack = Stack()
        lines: list[str] = []
        result, _seen = unlock(
            stack,
            SHEET_NO_PASSWORD,
            log=lines.append,
            responses={
                "03_hme_password_focus_1": KEYBOARD,
                "03_hme_password_field_menu_1": MENU_SELECT_ALL,
                "03_hme_password_field_menu_2": KEYBOARD,
                "03_hme_password_before_ok": KEYBOARD,
                "03_hme_after_icloud_submit": HME_LIST,
            },
        )
        self.assertIs(result, HME_LIST)
        self.assertEqual(stack.typed, ["\b", SECRET])
        self.assertTrue(lines)
        self.assertFalse(any(SECRET in line for line in lines))

    def test_failure_code_is_declared(self):
        self.assertTrue(
            issubclass(hidemyemail.ICloudPasswordEntryError, PhoneError)
        )
        self.assertEqual(
            hidemyemail.ICloudPasswordEntryError.failure_code,
            "icloud_password_entry_failed",
        )
        self.assertEqual(
            hidemyemail.ICloudPasswordEntryError("x").failure_code,
            "icloud_password_entry_failed",
        )
        # Only an explicit iCloud rejection is flagged as unrecoverable.
        self.assertIs(hidemyemail.ICloudPasswordEntryError("x").credentials_rejected, False)


class StructuredKeyboardObserverTests(unittest.TestCase):
    def _stack(self, hierarchy: str):
        stack = TapStack.__new__(TapStack)
        stack.backend = "xxtouch"
        stack.cancel_event = None
        stack.log = lambda _message: None
        stack.structured_dump_xml = lambda: hierarchy
        return stack

    def test_wda_keyboard_element_is_positive_focus_evidence(self):
        hierarchy = (
            '<XCUIElementTypeApplication visible="true">'
            '<XCUIElementTypeKeyboard visible="true" />'
            '</XCUIElementTypeApplication>'
        )
        self.assertTrue(self._stack(hierarchy).semantic_keyboard_present())

    def test_wda_hierarchy_without_keyboard_is_negative_evidence(self):
        hierarchy = (
            '<XCUIElementTypeApplication visible="true">'
            '<XCUIElementTypeSecureTextField focused="false" />'
            '</XCUIElementTypeApplication>'
        )
        self.assertFalse(self._stack(hierarchy).semantic_keyboard_present())


@unittest.skip(
    "legacy keyboard-OCR gate removed; replaced by deterministic single-submit tests"
)
class RealFocusFixtureTests(unittest.TestCase):
    """Real a0079 frames drive the secure-field focus contract: nothing typed.

    The frames are immutable copies of a0079's live dumps, so a later run that
    rewrites ``dumps/03_hme_*`` cannot silently change what is asserted here.
    """

    def test_derived_layout_frame_derives_the_field_point_from_modal_geometry(self):
        # When XXTouch omits the empty secure field the tap point is derived
        # from the verified modal geometry; no coordinate is hard-coded.
        controls = hidemyemail._icloud_password_sheet_controls(SHEET_NO_PASSWORD)
        self.assertIsNotNone(controls)
        self.assertTrue(controls.derived)
        self.assertEqual(controls.field_point, (371, 500))

    def test_a0079_frame_takes_the_field_point_from_the_real_placeholder(self):
        # a0079's own verified frame: the empty secure field is exposed as a
        # real ``Password`` node, and the tap point is read back from that
        # node instead of being assumed.
        controls = hidemyemail._icloud_password_sheet_controls(A0079_FOCUS_2)
        self.assertIsNotNone(controls)
        self.assertFalse(controls.derived)
        placeholder = hidemyemail.find_label_exact(A0079_FOCUS_2, ("Password",))
        self.assertIsNotNone(placeholder)
        self.assertEqual(controls.field_point, placeholder.center())
        # Live evidence from a0079: focus was never proven because the
        # keyboard never appeared, so nothing may be typed from this frame.
        self.assertFalse(hidemyemail._keyboard_present(A0079_FOCUS_2))

    def test_post_dismissal_frame_cannot_reverify_the_sheet(self):
        # a0079's fresh frame after the verified Cancel tap: the sheet is
        # gone, so there is no verified geometry left to tap.
        self.assertIsNone(hidemyemail._icloud_password_sheet_controls(A0079_CANCELLED))
        self.assertFalse(hidemyemail._keyboard_present(A0079_CANCELLED))

    def test_failed_focus_stops_after_one_tap_instead_of_a_stale_retap(self):
        stack = Stack()
        # The demonstrated a0079 failure names its real cause (the fresh frame
        # could not re-verify the sheet) instead of the misleading generic
        # keyboard-timeout message, and never types.
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError,
            "could not be re-verified from the fresh frame",
        ):
            unlock(
                stack,
                A0079_FOCUS_2,
                responses={"03_hme_password_focus_1": A0079_CANCELLED},
            )
        self.assertEqual(stack.typed, [])
        # The old flow tapped the stale coordinate a second time even though
        # the fresh frame could no longer verify the sheet.  The fix taps
        # only from a freshly verified observation, so exactly one tap.
        self.assertEqual(stack.taps, [(207, 497)])
        self.assertEqual(stack.swipes, [])

    def test_unfocused_field_is_never_typed_into(self):
        # Two freshly verified sheets and still no keyboard: the bounded
        # attempts run out and the password is never sent.
        stack = Stack()
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError,
            "keyboard did not appear within the bounded",
        ):
            unlock(
                stack,
                A0079_FOCUS_2,
                responses={
                    "03_hme_password_focus_1": A0079_FOCUS_1,
                    "03_hme_password_focus_2": A0079_FOCUS_2,
                },
            )
        self.assertEqual(stack.typed, [])
        self.assertEqual(len(stack.taps), hidemyemail._FOCUS_ATTEMPTS_TOTAL)


@unittest.skip(
    "legacy overlay retry contract removed; replaced by fresh-sheet recovery test"
)
class A0083SelectionOverlayTests(unittest.TestCase):
    """The exact a0083 transition gets one safe, bounded recovery tap."""

    def test_real_overlay_is_tied_to_the_immediately_prior_verified_sheet(self):
        sheet = hidemyemail._icloud_password_sheet_controls(A0083_BEFORE_FOCUS)
        self.assertIsNotNone(sheet)
        self.assertEqual(sheet.field_point, (371, 500))
        state, anchored = hidemyemail._classify_icloud_focus_frame(
            A0083_SELECTION_OVERLAY, sheet
        )
        self.assertEqual(state, hidemyemail._FOCUS_OVERLAY)
        self.assertEqual(anchored.field_point, sheet.field_point)
        self.assertFalse(hidemyemail._keyboard_present(A0083_SELECTION_OVERLAY))

    def test_overlay_then_verified_keyboard_types_the_password_once(self):
        stack = Stack()
        result, _seen = unlock(
            stack,
            A0083_BEFORE_FOCUS,
            responses={
                "03_hme_password_focus_1": A0083_SELECTION_OVERLAY,
                "03_hme_password_focus_2": KEYBOARD_WITH_PASSWORD,
                "03_hme_password_before_ok": KEYBOARD_WITH_PASSWORD,
                "03_hme_after_icloud_submit": HME_LIST,
            },
        )
        # Both bounded focus actions target the previously verified secure
        # field. Neither the merged Select All/Apple-ID OCR node nor its center
        # is ever tapped.
        self.assertEqual(stack.taps[:2], [(371, 500), (371, 500)])
        overlay_node = next(
            n for n in A0083_SELECTION_OVERLAY
            if "select all" in collapsed_text(n.labels())
        )
        self.assertNotIn(overlay_node.center(), stack.taps)
        self.assertEqual(stack.typed, [SECRET])
        self.assertIs(result, HME_LIST)

    def test_visible_keyboard_is_accepted_when_ocr_omits_key_words(self):
        # Live a0083 behavior: the keyboard is visible, while OCR contains
        # neither "space" nor "return". Accessibility is positive evidence.
        stack = Stack(semantic_keyboard=True)
        sheet = hidemyemail._icloud_password_sheet_controls(A0083_BEFORE_FOCUS)
        with (
            mock.patch.object(
                hidemyemail,
                "dump_named",
                return_value=(None, A0083_SELECTION_OVERLAY),
            ),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
        ):
            focused, nodes, used = hidemyemail._focus_icloud_password_field(
                stack,
                object(),
                lambda _message: None,
                sheet,
                None,
                remaining=2,
                stem_prefix="03_hme_password_focus",
            )
        self.assertEqual(used, 1)
        self.assertEqual(focused.field_point, (371, 500))
        self.assertIs(nodes, A0083_SELECTION_OVERLAY)
        self.assertEqual(stack.taps, [(371, 500)])
        self.assertEqual(stack.typed, [])

    def test_accessibility_absent_still_accepts_existing_ocr_evidence(self):
        stack = Stack(semantic_keyboard=False)
        sheet = hidemyemail._icloud_password_sheet_controls(A0083_BEFORE_FOCUS)
        with (
            mock.patch.object(
                hidemyemail, "dump_named", return_value=(None, KEYBOARD)
            ),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
        ):
            _focused, _nodes, used = hidemyemail._focus_icloud_password_field(
                stack,
                object(),
                lambda _message: None,
                sheet,
                None,
                remaining=2,
                stem_prefix="03_hme_password_focus",
            )
        self.assertEqual(used, 1)

    def test_no_accessibility_or_ocr_evidence_never_types(self):
        stack = Stack(semantic_keyboard=False)
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError,
            "keyboard did not appear within the bounded",
        ):
            unlock(
                stack,
                A0079_FOCUS_2,
                responses={
                    "03_hme_password_focus_1": A0079_FOCUS_1,
                    "03_hme_password_focus_2": A0079_FOCUS_2,
                },
            )
        self.assertEqual(stack.typed, [])
        self.assertEqual(len(stack.taps), 2)

    def test_overlay_that_remains_exhausts_the_budget_without_typing(self):
        stack = Stack()
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError,
            "verified selection overlay remained",
        ) as raised:
            unlock(
                stack,
                A0083_BEFORE_FOCUS,
                responses={
                    "03_hme_password_focus_1": A0083_SELECTION_OVERLAY,
                    "03_hme_password_focus_2": A0083_SELECTION_OVERLAY,
                },
            )
        self.assertTrue(raised.exception.focus_never_proven)
        self.assertTrue(raised.exception.selection_overlay)
        self.assertEqual(stack.taps, [(371, 500), (371, 500)])
        self.assertEqual(stack.typed, [])

    def test_similar_overlay_with_a_moved_anchor_is_unknown(self):
        sheet = hidemyemail._icloud_password_sheet_controls(A0083_BEFORE_FOCUS)
        moved = [
            Node(
                ntype=n.ntype,
                name=n.name,
                label=n.label,
                value=n.value,
                text=n.text,
                x=n.x + (80 if collapsed_text(n.labels()) == "cancel" else 0),
                y=n.y,
                w=n.w,
                h=n.h,
                visible=n.visible,
                enabled=n.enabled,
                raw=n.raw,
            )
            for n in A0083_SELECTION_OVERLAY
        ]
        state, anchored = hidemyemail._classify_icloud_focus_frame(moved, sheet)
        self.assertEqual(state, hidemyemail._FOCUS_UNKNOWN)
        self.assertIsNone(anchored)

    def test_stop_after_overlay_prevents_the_recovery_tap(self):
        stack = Stack()
        stop = Event()

        def dump_then_stop(_stack, _phone, _stem, _log):
            stop.set()
            return None, A0083_SELECTION_OVERLAY

        sheet = hidemyemail._icloud_password_sheet_controls(A0083_BEFORE_FOCUS)
        with (
            mock.patch.object(hidemyemail, "dump_named", side_effect=dump_then_stop),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
            self.assertRaisesRegex(PhoneError, "Stopped"),
        ):
            hidemyemail._focus_icloud_password_field(
                stack,
                object(),
                lambda _message: None,
                sheet,
                stop,
                remaining=2,
                stem_prefix="03_hme_password_focus",
            )
        self.assertEqual(stack.taps, [(371, 500)])
        self.assertEqual(stack.typed, [])

    def test_overlay_dismissal_reverifies_modal_before_cancel(self):
        stack = Stack()
        sheet = hidemyemail._icloud_password_sheet_controls(A0083_BEFORE_FOCUS)
        router, seen = routed_dump(
            {
                "03_hme_icloud_cancel_attempt": A0083_SELECTION_OVERLAY,
                "03_hme_icloud_overlay_closed": A0083_BEFORE_FOCUS,
                "03_hme_icloud_cancelled": A0079_CANCELLED,
            },
            A0079_CANCELLED,
        )
        with (
            mock.patch.object(hidemyemail, "dump_named", side_effect=router),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
        ):
            dismissed = hidemyemail.dismiss_icloud_password_sheet(
                stack,
                object(),
                lambda _message: None,
                known_sheet=sheet,
            )
        self.assertTrue(dismissed)
        self.assertEqual(stack.taps, [(371, 500), (237, 578)])
        self.assertEqual(
            seen,
            [
                "03_hme_icloud_cancel_attempt",
                "03_hme_icloud_overlay_closed",
                "03_hme_icloud_cancelled",
            ],
        )


@unittest.skip(
    "legacy two-focus-attempt contract removed; replaced by single-submit recovery test"
)
class FreshSheetRecoveryTests(unittest.TestCase):
    """A secure field that never focuses gets a dismissed-and-reopened sheet.

    a0076/a0078/a0079 all died the same way: the sheet verified, the tap came
    from that fresh verification, and the field still never raised a keyboard.
    These tests pin the recovery contract -- dismiss with ONE verified Cancel
    tap, re-open Hide My Email for a *new* sheet, unlock it once -- and, just
    as importantly, pin what the recovery must never do.
    """

    def test_never_focused_sheet_is_dismissed_then_a_fresh_sheet_is_unlocked(self):
        stack = Stack()
        result, seen = recover(
            stack,
            A0079_FOCUS_2,
            responses={
                # The stuck sheet never focuses; the replacement does.
                "03_hme_password_focus_1": [A0079_FOCUS_1, KEYBOARD_WITH_PASSWORD],
                "03_hme_password_focus_2": [A0079_FOCUS_2, KEYBOARD_WITH_PASSWORD],
                "03_hme_icloud_cancel_attempt": A0079_FOCUS_2,
                "03_hme_icloud_cancelled": A0079_CANCELLED,
                "03_hme_after_fresh_sheet": SHEET_WITH_PASSWORD,
                "03_hme_password_before_ok": KEYBOARD_WITH_PASSWORD,
                "03_hme_after_icloud_submit": HME_LIST,
            },
        )
        # The stuck sheet was dismissed with exactly one verified Cancel tap.
        self.assertEqual(stack.taps.count((239, 578)), 1)
        # A fresh sheet was asked for, and only one.
        self.assertEqual(stack.opened, [hidemyemail.HME_SETTINGS_URL])
        self.assertIn("03_hme_after_fresh_sheet", seen)
        # The fresh sheet's field proved empty by placeholder, so no clearing
        # long-press was needed, and the password went in exactly once.
        self.assertEqual(stack.long_taps, [])
        self.assertEqual(stack.typed, [SECRET])
        self.assertIs(result, HME_LIST)

    def test_a_fresh_sheet_that_also_refuses_focus_still_types_nothing(self):
        stack = Stack()
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError,
            "keyboard did not appear within the bounded",
        ):
            recover(
                stack,
                A0079_FOCUS_2,
                responses={
                    "03_hme_password_focus_1": A0079_FOCUS_1,
                    "03_hme_password_focus_2": A0079_FOCUS_2,
                    "03_hme_icloud_cancel_attempt": A0079_FOCUS_2,
                    "03_hme_icloud_cancelled": A0079_CANCELLED,
                    "03_hme_after_fresh_sheet": A0079_FOCUS_2,
                },
            )
        self.assertEqual(stack.typed, [])
        # Exactly one recovery was attempted -- the retry must not loop.
        self.assertEqual(stack.opened, [hidemyemail.HME_SETTINGS_URL])
        self.assertEqual(stack.taps.count((239, 578)), 1)

    def test_an_unconfirmed_dismissal_never_opens_a_second_sheet(self):
        # The cancel frame comes back with the sheet already gone, so the
        # dismissal cannot be confirmed and nothing may be re-opened or tapped.
        stack = Stack()
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError,
            "keyboard did not appear within the bounded",
        ):
            recover(
                stack,
                A0079_FOCUS_2,
                responses={
                    "03_hme_password_focus_1": A0079_FOCUS_1,
                    "03_hme_password_focus_2": A0079_FOCUS_2,
                    "03_hme_icloud_cancel_attempt": A0079_CANCELLED,
                    "03_hme_icloud_cancelled": A0079_CANCELLED,
                },
            )
        self.assertEqual(stack.typed, [])
        self.assertEqual(stack.opened, [])
        self.assertNotIn((239, 578), stack.taps)

    def test_a_layout_change_is_not_mistaken_for_a_never_focused_field(self):
        # A sheet whose layout changed must NOT be retried: the recovery only
        # fires for "the keyboard never appeared", never for a stale-point risk.
        stack = Stack()
        # A post-tap frame whose sheet no longer verifies: the title is still
        # on screen but the bounded Cancel/OK controls are gone.
        broken = [
            n
            for n in A0079_FOCUS_2
            if collapsed_text(n.labels()) not in ("cancel", "ok")
        ]
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError,
            "could not be re-verified from the fresh frame",
        ):
            recover(
                stack,
                A0079_FOCUS_2,
                responses={"03_hme_password_focus_1": broken},
            )
        self.assertEqual(stack.typed, [])
        self.assertEqual(stack.opened, [])
        self.assertEqual(stack.taps.count((239, 578)), 0)


@unittest.skip(
    "long-press Select-All clearing was removed; replacement asserts zero long taps"
)
class FieldClearingTests(unittest.TestCase):
    """Detected or uncertain secure-field content is cleared, never appended to."""

    _CLEARED = {
        "03_hme_password_focus_1": KEYBOARD,
        "03_hme_password_field_menu_1": MENU_SELECT_ALL,
        "03_hme_password_field_menu_2": KEYBOARD,  # clean after clearing
        "03_hme_password_before_ok": KEYBOARD,
        "03_hme_after_icloud_submit": HME_LIST,
    }

    def test_ocr_visible_content_is_cleared_before_typing_once(self):
        stack = Stack()
        result, _seen = unlock(stack, CONTENT_IN_FIELD, responses=self._CLEARED)
        self.assertIs(result, HME_LIST)
        # One clearing backspace, then exactly one password submission.
        self.assertEqual(stack.typed, [SECRET])
        # field tap -> verified Select All tap -> fresh OK tap
        self.assertEqual(stack.taps, [(371, 500), (370, 516), (504, 576)])
        self.assertEqual(stack.long_taps, [(371, 500), (371, 500)])

    def test_secure_content_without_ocr_text_is_detected_by_the_probe(self):
        # Missing OCR text is never treated as proof of emptiness: the
        # verified long-press finds the Select All that proves content,
        # clears the whole field, and only then types once.
        stack = Stack()
        result, _seen = unlock(stack, SHEET_NO_PASSWORD, responses=self._CLEARED)
        self.assertIs(result, HME_LIST)
        self.assertEqual(stack.typed, [SECRET])

    def test_clearing_that_never_confirms_never_types_the_password(self):
        stack = Stack()
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError, "could not be confirmed"
        ):
            unlock(
                stack,
                SHEET_NO_PASSWORD,
                responses={
                    "03_hme_password_focus_1": KEYBOARD,
                    "03_hme_password_field_menu_1": MENU_SELECT_ALL,
                    "03_hme_password_field_menu_2": MENU_SELECT_ALL,
                },
            )
        self.assertNotIn(SECRET, stack.typed)
        self.assertEqual(stack.typed, ["\b", "\b"])  # only bounded clearing

    def test_probe_frame_without_a_keyboard_fails_closed(self):
        # a0079's real post-tap frame: the sheet re-verifies but no keyboard
        # is visible, so the probe cannot confirm the correctly focused
        # secure field.  It must fail closed without ever typing the password.
        stack = Stack()
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError, "correctly focused field"
        ):
            unlock(
                stack,
                SHEET_NO_PASSWORD,
                responses={
                    "03_hme_password_focus_1": KEYBOARD,
                    "03_hme_password_field_menu_1": A0079_FOCUS_1,
                    "03_hme_password_before_ok": KEYBOARD,
                    "03_hme_after_icloud_submit": HME_LIST,
                },
            )
        self.assertEqual(stack.typed, [])
        self.assertEqual(stack.long_taps, [(371, 500)])
        self.assertNotIn((301, 426), stack.taps)  # the Apple ID row's menu
        self.assertNotIn("\b", stack.typed)

    def test_outside_band_select_all_is_never_tapped_and_proves_nothing(self):
        # A keyboard-present probe whose only Select All sits over the Apple
        # ID row (outside the verified secure-field band) is another field's
        # menu: it is never tapped or deleted from, and it positively proves
        # nothing about this field, so the uncertain field fails closed.
        outside_band = KEYBOARD + [
            node("Select All", 426, x=75, w=452, h=20, ntype="ocr")
        ]
        stack = Stack()
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError, "positive evidence"
        ):
            unlock(
                stack,
                SHEET_NO_PASSWORD,
                responses={
                    "03_hme_password_focus_1": KEYBOARD,
                    "03_hme_password_field_menu_1": outside_band,
                    "03_hme_password_before_ok": KEYBOARD,
                    "03_hme_after_icloud_submit": HME_LIST,
                },
            )
        self.assertEqual(stack.typed, [])
        self.assertEqual(stack.long_taps, [(371, 500)])
        self.assertNotIn((301, 426), stack.taps)  # centre of the outside menu
        self.assertNotIn("\b", stack.typed)

    def test_absent_select_all_on_the_first_probe_never_types(self):
        # Removing the old "Select All absent means empty" oracle: with no
        # Password placeholder and no prior positive detection+clearing on
        # this field, absence proves nothing, so the password is withheld.
        stack = Stack()
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError, "positive evidence"
        ):
            unlock(
                stack,
                SHEET_NO_PASSWORD,
                responses={
                    "03_hme_password_focus_1": KEYBOARD,
                    "03_hme_password_field_menu_1": KEYBOARD,  # no Select All
                    "03_hme_password_before_ok": KEYBOARD,
                    "03_hme_after_icloud_submit": HME_LIST,
                },
            )
        self.assertEqual(stack.typed, [])
        self.assertEqual(stack.taps, [(371, 500)])  # focus tap only, no OK
        self.assertEqual(stack.long_taps, [(371, 500)])


class DestinationVerificationTests(unittest.TestCase):
    """The dismissed sheet must land on authenticated Settings, once."""

    def _responses(self, submit_nodes):
        return {
            "03_hme_password_focus_1": KEYBOARD,
            "03_hme_password_field_menu_1": MENU_SELECT_ALL,
            "03_hme_password_field_menu_2": KEYBOARD,
            "03_hme_password_before_ok": KEYBOARD,
            "03_hme_after_icloud_submit": submit_nodes,
        }

    def test_sheet_gone_without_a_settings_marker_fails_closed(self):
        elsewhere = [node("Weather", 300), node("Find My", 500)]
        stack = Stack()
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError, "destination could not be verified"
        ):
            unlock(stack, SHEET_NO_PASSWORD, responses=self._responses(elsewhere))
        # Submitted exactly once; the unverified destination never triggers
        # another submission or a continue.
        self.assertEqual(stack.typed, [SECRET])

    def test_full_signin_wizard_is_never_treated_as_success(self):
        wizard = [
            node("Apple ID", 250),
            node("Sign in with your Apple ID", 300),
            node("Apple ID Email or Phone", 400),
            node("Next", 600),
        ]
        stack = Stack()
        with self.assertRaisesRegex(
            hidemyemail.ICloudPasswordEntryError, "destination could not be verified"
        ):
            unlock(stack, SHEET_NO_PASSWORD, responses=self._responses(wizard))
        self.assertEqual(stack.typed, [SECRET])

    def test_settings_markers_pass_the_destination_check(self):
        stack = Stack()
        result, _seen = unlock(
            stack, SHEET_NO_PASSWORD, responses=self._responses(HME_LIST)
        )
        self.assertIs(result, HME_LIST)


class DismissSheetTests(unittest.TestCase):
    """One verified Cancel tap with confirmed dismissal, or no tap at all."""

    def _dismiss(self, responses, fallback):
        stack = Stack()
        router, seen = routed_dump(responses, fallback)
        with (
            mock.patch.object(hidemyemail, "dump_named", side_effect=router),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
        ):
            ok = hidemyemail.dismiss_icloud_password_sheet(
                stack, object(), lambda _message: None, None
            )
        return ok, stack, seen

    def test_verified_cancel_is_tapped_once_and_dismissal_is_confirmed(self):
        ok, stack, seen = self._dismiss(
            {"03_hme_icloud_cancel_attempt": SHEET_NO_PASSWORD}, HME_LIST
        )
        self.assertIs(ok, True)
        self.assertEqual(stack.taps, [(237, 578)])  # the verified Cancel centre
        self.assertEqual(
            seen, ["03_hme_icloud_cancel_attempt", "03_hme_icloud_cancelled"]
        )

    def test_unverifiable_frame_gets_no_cancel_tap(self):
        ok, stack, seen = self._dismiss({}, SHEET)  # ambiguous controls
        self.assertIs(ok, False)
        self.assertEqual(stack.taps, [])
        self.assertEqual(seen, ["03_hme_icloud_cancel_attempt"])

    def test_unconfirmed_dismissal_is_never_retried(self):
        ok, stack, seen = self._dismiss(
            {"03_hme_icloud_cancel_attempt": SHEET_NO_PASSWORD}, SHEET_NO_PASSWORD
        )
        self.assertIs(ok, False)
        self.assertEqual(stack.taps, [(237, 578)])  # exactly one tap, no retry
        self.assertEqual(
            seen, ["03_hme_icloud_cancel_attempt", "03_hme_icloud_cancelled"]
        )


class StopFlag:
    """Minimal cancellation view (``threading.Event`` stand-in)."""

    def __init__(self, flag: bool):
        self._flag = flag

    def is_set(self) -> bool:
        return self._flag

    def set(self) -> None:
        self._flag = True


class HideMyEmailReentryTests(unittest.TestCase):
    def test_observed_cyrillic_ok_sheet_layout_derives_from_the_real_dump(self):
        controls = hidemyemail._icloud_password_sheet_controls(SHEET_WITH_PASSWORD)
        self.assertIsNotNone(controls)
        stack = Stack()
        with (
            mock.patch.object(
                hidemyemail, "load_secrets", return_value={"icloud_password": SECRET}
            ),
            mock.patch.object(
                hidemyemail,
                "dump_named",
                side_effect=lambda *_a: (None, KEYBOARD_WITH_PASSWORD)
                if _a[2]
                in ("03_hme_password_focus_1", "03_hme_password_before_ok")
                else (None, HME_LIST),
            ),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
        ):
            result = hidemyemail._unlock_icloud_if_needed(
                stack, object(), lambda _message: None, SHEET_WITH_PASSWORD, None
            )
        self.assertIs(result, HME_LIST)
        self.assertEqual(stack.typed, [SECRET])
        self.assertEqual(stack.taps, [controls.field_point, controls.ok.center()])

    def test_reenters_verified_row_once_after_password_sheet(self):
        stack = Stack(stop_after_create=True)
        dumps = iter([(None, OVERVIEW), (None, SHEET), (None, HME_LIST)])
        names = []

        def dump(_stack, _phone, name, _log):
            names.append(name)
            return next(dumps)

        with (
            mock.patch.object(hidemyemail, "dump_named", side_effect=dump),
            mock.patch.object(hidemyemail, "_unlock_icloud_if_needed", side_effect=lambda _s, _p, _l, nodes, _stop: OVERVIEW if nodes is SHEET else nodes),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
        ):
            with self.assertRaises(StopAfterCreate):
                hidemyemail.create_one_hide_my_email(object(), stack, lambda _message: None)

        self.assertEqual(stack.taps[:2], [OVERVIEW[2].center(), OVERVIEW[2].center()])
        self.assertEqual(stack.taps[2], HME_LIST[1].center())
        self.assertEqual(stack.swipes, [])
        self.assertEqual(names, ["03_hme_before_create", "03_hme_before_create_retry", "03_hme_after_second_row_tap"])

    def test_still_on_overview_after_second_tap_fails_closed(self):
        stack = Stack()
        dumps = iter([(None, OVERVIEW), (None, SHEET), (None, OVERVIEW)])
        with (
            mock.patch.object(hidemyemail, "dump_named", side_effect=lambda *_a: next(dumps)),
            mock.patch.object(hidemyemail, "_unlock_icloud_if_needed", side_effect=lambda _s, _p, _l, nodes, _stop: OVERVIEW if nodes is SHEET else nodes),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
        ):
            with self.assertRaisesRegex(PhoneError, "after bounded verified navigation"):
                hidemyemail.create_one_hide_my_email(object(), stack, lambda _message: None)
        self.assertEqual(stack.taps, [OVERVIEW[2].center(), OVERVIEW[2].center()])
        self.assertEqual(stack.swipes, [])

    def test_delayed_password_sheet_reenters_overview_row_before_create(self):
        scrolled = [
            node("iCloud Backup", 380),
            node("ICLOUD+", 480),
            node("Private Relay", 540),
            node("Hide My Email", 620),
        ]
        stack = Stack(stop_on_tap=4)
        # Overview -> unchanged after the first row tap -> the password sheet
        # appears only after the bounded second row tap -> HME list after the
        # one verified post-unlock re-entry.
        dumps = iter([
            (None, scrolled),
            (None, scrolled),
            (None, SHEET),
            (None, HME_LIST),
        ])
        names = []

        def dump(_stack, _phone, name, _log):
            names.append(name)
            return next(dumps)

        with (
            mock.patch.object(hidemyemail, "dump_named", side_effect=dump),
            mock.patch.object(hidemyemail, "_unlock_icloud_if_needed", side_effect=lambda _s, _p, _l, nodes, _stop: scrolled if nodes is SHEET else nodes),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
        ):
            with self.assertRaises(StopAfterCreate):
                hidemyemail.create_one_hide_my_email(object(), stack, lambda _message: None)

        self.assertEqual(stack.taps[:3], [scrolled[3].center()] * 3)
        self.assertEqual(stack.taps[3], HME_LIST[1].center())
        self.assertEqual(stack.swipes, [])
        self.assertEqual(
            names,
            [
                "03_hme_before_create",
                "03_hme_before_create_retry",
                "03_hme_after_second_row_tap",
                "03_hme_after_unlock_row_tap",
            ],
        )


class ICloudPasswordPipelineTests(unittest.TestCase):
    """The controller records the dedicated code and keeps the Ghost container."""

    def _app(self):
        from test_bulk_preserve_skip import make_app

        app = make_app(self, 1)
        app.status = StatusSpy()
        app._connect = lambda _password: (FakePhone(), FakeStack())
        return app

    @staticmethod
    def _ghost_slot():
        return {
            "id": "a0047",
            "ghost": True,
            "ghost_container_state": "ready",
            "ghost_container_instance": "ghost-instance-proof",
            "crane_container_instance": "GHOST-INSTANCE-PROOF-FULL",
        }

    @staticmethod
    def _no_watch():
        class NoWatch:
            def __init__(self, *_a, **_k):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        return NoWatch

    def test_pipeline_records_code_and_reuses_the_ready_container(self):
        from friend_poster.apps import creator

        app = self._app()
        ghost_slot = self._ghost_slot()
        calls = {"create": 0}

        def create(*_args, **_kwargs):
            calls["create"] += 1
            raise hidemyemail.ICloudPasswordEntryError(
                "iCloud password sheet: the keyboard did not appear; did not type."
            )

        with (
            mock.patch.object(creator, "_InactivityWatch", self._no_watch()),
            mock.patch.object(creator, "create_one_hide_my_email", side_effect=create),
            # One verified Cancel confirms dismissal, so the batch may continue.
            mock.patch.object(creator, "dismiss_icloud_password_sheet", return_value=True) as dismiss,
            mock.patch.object(creator, "set_slot_birth_date", lambda *_a: None),
            mock.patch.object(creator, "get_slot", lambda _sid: ghost_slot),
            mock.patch.object(creator, "load_slots", lambda: [ghost_slot]),
            mock.patch.object(creator, "_load_pipeline_email_records", lambda: []),
            mock.patch.object(creator, "_reconcile_pipeline_email", lambda *_a: None),
            mock.patch.object(creator, "set_slot_email") as set_email,
        ):
            ok = creator.App._run_creation_pipeline(
                app,
                "offline-password",
                "a0047",
                date(2005, 4, 20),
                10,
                manage_worker=False,
            )

        self.assertFalse(ok)
        # The READY Ghost container was reused, so the email stage was reached.
        self.assertEqual(calls["create"], 1)
        self.assertEqual(dismiss.call_count, 1)
        self.assertEqual(app._last_pipeline_failure_code, "icloud_password_entry_failed")
        self.assertIs(app._last_pipeline_failure_shared, False)
        self.assertFalse(app._last_pipeline_attempt_timed_out)
        self.assertIn("keyboard did not appear", app._last_pipeline_failure_reason)
        set_email.assert_not_called()
        # The verified Ghost container checkpoint is preserved untouched.
        self.assertEqual(ghost_slot["ghost_container_state"], "ready")
        self.assertEqual(ghost_slot["ghost_container_instance"], "ghost-instance-proof")
        self.assertNotIn("email", ghost_slot)

    def test_visible_icloud_sheet_uses_local_dismissal_without_ai(self):
        from friend_poster.apps import creator

        app = self._app()
        app.anthropic_recovery_enabled = True
        app._set_ai_recovery_status = lambda _message: None
        app._pending_ui_recovery = None
        app._pending_ui_recovery_agent = None
        ghost_slot = self._ghost_slot()
        recovered_session = object()
        agent_instances = []

        class RecoveryAgent:
            def __init__(self, stack, phone, **kwargs):
                self.stack = stack
                self.phone = phone
                self.kwargs = kwargs
                self.errors = []
                agent_instances.append(self)

            def recover(self, error, previous=None):
                self.errors.append((error, previous))
                return recovered_session

        def create(*_args, **_kwargs):
            raise hidemyemail.ICloudPasswordEntryError(
                "iCloud password sheet controls could not be verified."
            )

        with (
            mock.patch.object(creator, "_InactivityWatch", self._no_watch()),
            mock.patch.object(creator, "UIRecoveryAgent", RecoveryAgent),
            mock.patch.object(creator, "create_one_hide_my_email", side_effect=create),
            mock.patch.object(
                creator, "dismiss_icloud_password_sheet", return_value=True
            ) as dismiss,
            mock.patch.object(creator, "set_slot_birth_date", lambda *_a: None),
            mock.patch.object(creator, "get_slot", lambda _sid: ghost_slot),
            mock.patch.object(creator, "load_slots", lambda: [ghost_slot]),
            mock.patch.object(creator, "_load_pipeline_email_records", lambda: []),
            mock.patch.object(creator, "_reconcile_pipeline_email", lambda *_a: None),
            mock.patch.object(creator, "set_slot_email"),
        ):
            result = creator.App._run_creation_pipeline_attempt(
                app,
                "offline-password",
                "a0047",
                date(2005, 4, 20),
                10,
                manage_worker=False,
            )

        self.assertFalse(result)
        dismiss.assert_called_once()
        self.assertEqual(agent_instances, [])
        self.assertEqual(
            app._last_pipeline_failure_code, "icloud_password_entry_failed"
        )
        self.assertIsNone(app._pending_ui_recovery)
        # The agent sees the live sheet and may request deterministic local
        # password entry, but never receives the stored password value.
        self.assertEqual(ghost_slot["ghost_container_state"], "ready")
        self.assertNotIn("email", ghost_slot)

    def test_batch_pauses_same_slot_after_password_entry_failure(self):
        from friend_poster.apps import creator
        from test_bulk_creator import verified_result

        app = self._app()
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        ghost_slot = self._ghost_slot()
        calls = {"create": 0}
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )

        def create(*_args, **_kwargs):
            calls["create"] += 1
            raise hidemyemail.ICloudPasswordEntryError(
                "iCloud password sheet: the keyboard did not appear; did not type."
            )

        with (
            mock.patch.object(creator, "_InactivityWatch", self._no_watch()),
            mock.patch.object(creator, "create_one_hide_my_email", side_effect=create),
            # One verified Cancel clears the blocking sheet, but the failed
            # queue item must still remain selected for Retry / Resume.
            mock.patch.object(creator, "dismiss_icloud_password_sheet", return_value=True) as dismiss,
            mock.patch.object(creator, "set_slot_birth_date", lambda *_a: None),
            mock.patch.object(creator, "get_slot", lambda _sid: ghost_slot),
            mock.patch.object(creator, "load_slots", lambda: [ghost_slot]),
            mock.patch.object(creator, "_load_pipeline_email_records", lambda: []),
            mock.patch.object(creator, "_reconcile_pipeline_email", lambda *_a: None),
            mock.patch.object(creator, "set_slot_email"),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", lambda _sid, _log: None
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(calls["create"], 1)
        self.assertEqual(dismiss.call_count, 1)
        skipped.assert_not_called()
        self.assertEqual(pauses[0][2], "icloud_password_entry_failed")
        self.assertEqual(app.bulk_state["failures"], [])
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["slot_id"], "a0047")
        self.assertEqual(len(app.bulk_state["queue"]), 1)
        self.assertEqual(ghost_slot["ghost_container_state"], "ready")
        self.assertEqual(ghost_slot["ghost_container_instance"], "ghost-instance-proof")

    def test_rejected_credentials_pause_the_batch_with_shared_code(self):
        from friend_poster.apps import creator
        from test_bulk_creator import verified_result

        app = self._app()
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        ghost_slot = self._ghost_slot()
        calls = {"create": 0}

        def create(*_args, **_kwargs):
            calls["create"] += 1
            raise hidemyemail.ICloudPasswordEntryError(
                "iCloud rejected the saved password; did not type it again.",
                credentials_rejected=True,
            )

        with (
            mock.patch.object(creator, "_InactivityWatch", self._no_watch()),
            mock.patch.object(creator, "create_one_hide_my_email", side_effect=create),
            # A rejection must never trigger the Cancel dismissal.
            mock.patch.object(creator, "dismiss_icloud_password_sheet") as dismiss,
            mock.patch.object(creator, "set_slot_birth_date", lambda *_a: None),
            mock.patch.object(creator, "get_slot", lambda _sid: ghost_slot),
            mock.patch.object(creator, "load_slots", lambda: [ghost_slot]),
            mock.patch.object(creator, "_load_pipeline_email_records", lambda: []),
            mock.patch.object(creator, "_reconcile_pipeline_email", lambda *_a: None),
            mock.patch.object(creator, "set_slot_email"),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", lambda _sid, _log: None
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(calls["create"], 1)
        dismiss.assert_not_called()
        # Paused with the specific shared-failure code; no slot was skipped
        # and the remaining queue item is preserved for a resume.
        self.assertEqual(
            pauses, [("creation", "iCloud rejected the saved password; did not type it again.", "icloud_password_rejected")]
        )
        skipped.assert_not_called()
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["failures"], [])

    def test_dismissal_failure_pauses_the_batch_with_shared_code(self):
        from friend_poster.apps import creator
        from test_bulk_creator import verified_result

        app = self._app()
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        ghost_slot = self._ghost_slot()
        calls = {"create": 0}

        def create(*_args, **_kwargs):
            calls["create"] += 1
            raise hidemyemail.ICloudPasswordEntryError(
                "iCloud password sheet: the keyboard did not appear; did not type."
            )

        with (
            mock.patch.object(creator, "_InactivityWatch", self._no_watch()),
            mock.patch.object(creator, "create_one_hide_my_email", side_effect=create),
            # The single verified Cancel could not dismiss the stuck sheet.
            mock.patch.object(creator, "dismiss_icloud_password_sheet", return_value=False) as dismiss,
            mock.patch.object(creator, "set_slot_birth_date", lambda *_a: None),
            mock.patch.object(creator, "get_slot", lambda _sid: ghost_slot),
            mock.patch.object(creator, "load_slots", lambda: [ghost_slot]),
            mock.patch.object(creator, "_load_pipeline_email_records", lambda: []),
            mock.patch.object(creator, "_reconcile_pipeline_email", lambda *_a: None),
            mock.patch.object(creator, "set_slot_email"),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", lambda _sid, _log: None
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(calls["create"], 1)
        self.assertEqual(dismiss.call_count, 1)
        self.assertEqual(
            pauses,
            [
                (
                    "creation",
                    "iCloud password sheet: the keyboard did not appear; did not type.",
                    "icloud_sheet_dismiss_failed",
                )
            ],
        )
        skipped.assert_not_called()
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(ghost_slot["ghost_container_state"], "ready")


class FakeStack:
    cancel_event = None


class FakePhone:
    serial = "offline"
    ssh_remote_port = 1

    def close(self):
        pass


class StatusSpy:
    def __init__(self):
        self.values = []

    def set(self, value):
        self.values.append(value)


if __name__ == "__main__":
    unittest.main()
