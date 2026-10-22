"""Offline tests for optional username:password batch rows."""

from __future__ import annotations

import unittest
from unittest import mock

from friend_poster.apps import creator
from friend_poster.core import bulk_creation
from friend_poster.device.ui_tree import Node
from friend_poster.reddit import reddit_password


def parse(lines: str) -> list[dict]:
    return bulk_creation.parse_queue(
        "1", "01.01.2004", "10", "2.5",
        lambda _value: __import__("datetime").date(2004, 1, 1),
        usernames_text=lines,
    )[1]


class Stack:
    def __init__(self):
        self.typed: list[str] = []
        self.taps: list[tuple[int, int]] = []

    def tap_xy(self, x, y):
        self.taps.append((x, y))

    def type_text(self, value):
        self.typed.append(value)


class BulkCredentialTests(unittest.TestCase):
    def test_username_only_keeps_generated_password_behavior(self):
        self.assertEqual(parse("FreshName7")[0], {
            "birth_date": "01.01.2004", "swipes": 10,
            "delay_seconds": 2.5, "username": "FreshName7",
        })

    def test_username_password_is_kept_as_one_queue_item(self):
        item = parse("FreshName7:Strong:Pass9!")[0]
        self.assertEqual(item["username"], "FreshName7")
        self.assertEqual(item["password"], "Strong:Pass9!")
        bulk_creation._validate({
            **bulk_creation.empty_state(), "queue": [item],
        })

    def test_invalid_password_fails_without_echoing_secret(self):
        with self.assertRaises(bulk_creation.BulkError) as caught:
            parse("FreshName7:short")
        self.assertNotIn("short", str(caught.exception))

    def test_editor_round_trip_preserves_credential_pair(self):
        item = parse("FreshName7:StrongPass9!")[0]
        payload = creator.App._bulk_rows_payload([item], count_text="1")
        self.assertEqual(payload["usernames_text"], "FreshName7:StrongPass9!")
        reparsed = creator.App._parse_bulk_text_rows(
            payload["births_text"], payload["swipes_text"],
            payload["delays_text"], payload["usernames_text"],
        )
        self.assertEqual(reparsed, [item])

    def test_queue_normalization_moves_username_and_password_together(self):
        app = creator.App.__new__(creator.App)
        state = bulk_creation.empty_state()
        first = parse("UsedName7:UsedPass9!")[0]
        second = parse("FreshName8:FreshPass9!")[0]
        state.update(queue=[first, second], requested=2, remaining=2, stage="creating")
        app.bulk_state = state
        app._username_claim_owners = lambda: {
            "usedname7": {"slot_id": "a0001", "status": "success"},
        }
        app._persist_bulk = lambda updated: setattr(app, "bulk_state", dict(updated))
        app.log = lambda _message: None

        updated = app._normalize_bulk_queue_usernames(state, "a0002")

        self.assertEqual(updated["queue"][0]["username"], "FreshName8")
        self.assertEqual(updated["queue"][0]["password"], "FreshPass9!")
        self.assertNotIn("username", updated["queue"][1])
        self.assertNotIn("password", updated["queue"][1])

    def test_saved_password_mismatch_stops_without_exposing_either_secret(self):
        app = creator.App.__new__(creator.App)
        app._username_claim_owners = lambda: {}
        with self.assertRaises(creator.BulkError) as caught:
            app._assert_bulk_requested_username(
                "a0002",
                {"username": "FreshName8", "password": "RequestedPass9!"},
                {"username": "FreshName8", "password": "DifferentPass9!"},
            )
        message = str(caught.exception)
        self.assertNotIn("RequestedPass9!", message)
        self.assertNotIn("DifferentPass9!", message)

    def test_password_entry_uses_exact_requested_value_without_logging_it(self):
        stack = Stack()
        secret = "StrongPass9!"
        logs: list[str] = []
        frames = iter([
            [
                Node(ntype="ocr", text="Set a password", x=100, y=180, w=300, h=25),
                Node(ntype="ocr", text="Password", x=80, y=360, w=180, h=25),
            ],
            [Node(ntype="ocr", text="Continue", x=280, y=1100, w=180, h=40)],
            [Node(ntype="ocr", text="About you", x=180, y=180, w=300, h=25)],
        ])
        with (
            mock.patch.object(reddit_password, "get_slot", return_value={
                "email": "fixture@icloud.com", "username": "FreshName7",
            }),
            mock.patch.object(reddit_password, "set_slot_password"),
            mock.patch.object(reddit_password, "write_account_text", return_value="fixture.txt"),
            mock.patch.object(
                reddit_password, "dump_named",
                side_effect=lambda *_args, **_kwargs: ("", next(frames)),
            ),
            mock.patch.object(reddit_password, "wait_after_password", return_value=[]),
            mock.patch.object(reddit_password.time, "sleep", return_value=None),
            mock.patch.object(reddit_password, "log", side_effect=logs.append),
        ):
            result = reddit_password.type_password(stack, object(), "a0001", secret)

        self.assertEqual(result, secret)
        self.assertEqual(stack.typed, [secret])
        self.assertFalse(any(secret in line for line in logs))


if __name__ == "__main__":
    unittest.main()
