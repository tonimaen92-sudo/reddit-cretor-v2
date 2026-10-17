"""Mocked navigation checks for Hide My Email's iCloud re-auth path."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import hidemyemail
from ssh_device import PhoneError
from ui_tree import Node, parse_nodes


def node(label: str, y: int) -> Node:
    return Node(text=label, x=100, y=y, w=250, h=40)


OVERVIEW = [
    node("Manage Account Storage", 300),
    node("APPS USING ICLOUD", 450),
    node("Hide My Email", 1050),
]
SHEET = [node("Sign in to iCloud", 300), node("Password", 500), node("OK", 700)]
HME_LIST = [node("Hide My Email", 100), node("Create New Address", 400)]


class Stack:
    def __init__(self, stop_after_create: bool = False, stop_on_tap: int | None = None):
        self.taps = []
        self.swipes = []
        self.typed = []
        self.stop_after_create = stop_after_create
        self.stop_on_tap = stop_on_tap

    def open_url(self, _url):
        pass

    def can_tap(self):
        return True

    def tap_xy(self, x, y):
        self.taps.append((x, y))
        stop_at = self.stop_on_tap or (3 if self.stop_after_create else None)
        if stop_at is not None and len(self.taps) == stop_at:
            raise StopAfterCreate()

    def swipe(self, *coords):
        self.swipes.append(coords)

    def type_text(self, value):
        self.typed.append(value)


class StopAfterCreate(Exception):
    pass


class HideMyEmailReentryTests(unittest.TestCase):
    def test_observed_cyrillic_ok_sheet_unlocks_once(self):
        dump = Path(__file__).resolve().parent.parent / "dumps" / "a0048_icloud_pause.json"
        nodes = parse_nodes(dump.read_text(encoding="utf-8"))
        controls = hidemyemail._icloud_password_sheet_controls(nodes)
        self.assertIsNotNone(controls)
        self.assertEqual(controls[1].center(), (504, 576))
        stack = Stack()
        with (
            mock.patch.object(hidemyemail, "load_secrets", return_value={"icloud_password": "test-only"}),
            mock.patch.object(hidemyemail, "dump_named", return_value=(None, HME_LIST)),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
        ):
            result = hidemyemail._unlock_icloud_if_needed(stack, object(), lambda _message: None, nodes, None)
        self.assertIs(result, HME_LIST)
        self.assertEqual(stack.typed, ["test-only"])
        self.assertEqual(stack.taps, [controls[0].center(), controls[1].center()])

    def test_ambiguous_or_unrelated_sheet_never_types(self):
        dump = Path(__file__).resolve().parent.parent / "dumps" / "a0048_icloud_pause.json"
        nodes = parse_nodes(dump.read_text(encoding="utf-8"))
        for candidate in (nodes + [Node(text="ОК", x=470, y=560, w=65, h=33, ntype="ocr")], SHEET):
            stack = Stack()
            with mock.patch.object(hidemyemail, "load_secrets", return_value={"icloud_password": "test-only"}):
                with self.assertRaises(PhoneError):
                    hidemyemail._unlock_icloud_if_needed(stack, object(), lambda _message: None, candidate, None)
            self.assertEqual(stack.typed, [])
            self.assertEqual(stack.taps, [])

    def test_persistent_sheet_does_not_repeat_password(self):
        dump = Path(__file__).resolve().parent.parent / "dumps" / "a0048_icloud_pause.json"
        nodes = parse_nodes(dump.read_text(encoding="utf-8"))
        stack = Stack()
        with (
            mock.patch.object(hidemyemail, "load_secrets", return_value={"icloud_password": "test-only"}),
            mock.patch.object(hidemyemail, "dump_named", return_value=(None, nodes)),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
        ):
            with self.assertRaisesRegex(PhoneError, "remained after one"):
                hidemyemail._unlock_icloud_if_needed(stack, object(), lambda _message: None, nodes, None)
        self.assertEqual(stack.typed, ["test-only"])

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
        # Initial overview -> unchanged after first row tap -> password sheet
        # after the bounded second row tap -> HME list after post-unlock re-entry.
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
            mock.patch.object(
                hidemyemail,
                "_unlock_icloud_if_needed",
                side_effect=lambda _s, _p, _l, nodes, _stop: scrolled if nodes is SHEET else nodes,
            ),
            mock.patch.object(hidemyemail.time, "sleep", return_value=None),
        ):
            with self.assertRaises(StopAfterCreate):
                hidemyemail.create_one_hide_my_email(
                    object(), stack, lambda _message: None
                )

        self.assertEqual(
            stack.taps,
            [scrolled[3].center(), scrolled[3].center(), scrolled[3].center(), HME_LIST[1].center()],
        )
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


if __name__ == "__main__":
    unittest.main()
