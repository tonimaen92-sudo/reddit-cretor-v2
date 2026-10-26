"""Offline checks for exact usernames supplied by a Creator batch."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from friend_poster.reddit import reddit_username as usernames
from friend_poster.device.ssh_device import PhoneError
from friend_poster.device.ui_tree import Node, OperationCancelled


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


def cleared_page_with_occluded_title() -> list[Node]:
    """Focused username page while a transient banner covers only its title."""
    return [
        node("Username", 60, 398, 135),
        node("Need inspiration? Check out these usernames:", 40, 520, 520),
        node("Beautiful_Tough7544", 80, 590, 300),
        node("Continue", 300, 700, 140),
        node("123", 50, 1140, 80),
        node("space", 300, 1140, 100),
        node("return", 600, 1140, 100),
    ]


def available_page_with_occluded_title(value="Chosen_Name9") -> list[Node]:
    """Available requested value while a transient banner covers the title."""
    return [
        node("Username", 60, 398, 135),
        node(value, 90, 430, 320),
        node("Great name! It's not taken, so it's all yours.", 80, 475, 500),
        node("Need inspiration? Check out these usernames:", 40, 520, 520),
        node("Continue", 300, 700, 140),
        node("123", 50, 1140, 80),
        node("space", 300, 1140, 100),
        node("return", 600, 1140, 100),
    ]


def cleared_page_with_clipped_field_label() -> list[Node]:
    """Focused field where an iOS bubble covers the left edge of Username."""
    return [
        node("Create your username", 110, 180, 520),
        node("Odds", 60, 350, 90),
        node("sername", 100, 398, 105),
        node("Need inspiration? Check out these usernames:", 40, 520, 520),
        node("ActivePollution8399", 80, 590, 300),
        node("Continue", 300, 700, 140),
        node("123", 50, 1140, 80),
        node("space", 300, 1140, 100),
        node("return", 600, 1140, 100),
    ]


def cleared_page_with_real_bubble_ocr() -> list[Node]:
    """Real a0245 shape: selection bubble plus ``bername`` OCR remainder."""
    rows = cleared_page_with_clipped_field_label()
    for row in rows:
        if row.text == "Odds":
            row.text = "Bit"
        elif row.text == "sername":
            row.text = "bername"
    return rows


def taken_page(value: str) -> list[Node]:
    rows = page(value)
    rows.append(node("Username already taken! Try another", 80, 480, 500))
    return rows


def available_username_png() -> bytes:
    image = Image.new("RGB", (1125, 2436), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((820, 700, 870, 760), fill=(0, 130, 40))
    draw.rectangle((90, 800, 900, 900), fill=(0, 130, 40))
    data = io.BytesIO()
    image.save(data, "PNG")
    return data.getvalue()


class Stack:
    def __init__(self, png: bytes | None = None, *, cancel_on_wait: int | None = None):
        self.taps = []
        self.backspaces = []
        self.typed = []
        self.png = png
        self.wait_calls = 0
        self.cancel_on_wait = cancel_on_wait

    def tap_xy(self, x, y):
        self.taps.append((x, y))

    def press_backspace(self, count):
        self.backspaces.append(count)

    def type_text(self, value):
        self.typed.append(value)

    def screenshot_png(self):
        return self.png

    def wait(self, _seconds):
        self.wait_calls += 1
        if self.wait_calls == self.cancel_on_wait:
            raise OperationCancelled("180-second inactivity watchdog fired")


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
        self.assertEqual(
            written,
            [("claim", "Chosen_Name9"), ("slot", "Chosen_Name9"), ("account", "Chosen_Name9")],
        )

    def run_case(
        self,
        frames,
        saved="",
        stack=None,
        *,
        requested="Chosen_Name9",
        committer=None,
        checkpoint=None,
        owners=None,
    ):
        stack = stack or Stack()
        written = []
        names = []

        def dump(_stack, _phone, stem, _log, shot=True, **_kwargs):
            names.append(stem)
            return "", next(frames)

        with (
            mock.patch.object(usernames, "get_slot", return_value={"email": "test@icloud.com", "username": saved}),
            mock.patch.object(usernames, "dump_named", side_effect=dump),
            mock.patch.object(
                usernames, "username_variant_attempt", return_value=checkpoint
            ),
            mock.patch.object(usernames, "mark_username_variant_attempt"),
            mock.patch.object(usernames, "clear_username_variant_attempt"),
            mock.patch.object(
                usernames, "username_owners", return_value=owners or {}
            ),
            mock.patch.object(usernames, "set_slot_username", side_effect=lambda *args: written.append(("slot", args[1]))),
            mock.patch.object(usernames, "write_account_text", side_effect=lambda *args: written.append(("account", args[2])) or "account.txt"),
            mock.patch.object(
                usernames, "claim_username",
                side_effect=lambda name, _sid: written.append(("claim", name))
                or {"status": "processing"},
            ),
            mock.patch.object(usernames.time, "sleep", return_value=None),
            mock.patch.object(usernames, "log", return_value=None),
        ):
            try:
                result = usernames.use_requested_username(
                    stack,
                    object(),
                    "a0049",
                    requested,
                    username_committer=committer,
                )
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
        self.assertEqual(
            written,
            [("claim", "Chosen_Name9"), ("slot", "Chosen_Name9"), ("account", "Chosen_Name9")],
        )
        self.assertEqual(names, ["890_username", "890_username_cleared", "890_username_requested", "891_after_username"])

    def test_available_name_survives_notification_occluding_title(self):
        frames = iter([
            page("RedditSuggestion"),
            page(),
            available_page_with_occluded_title(),
            [node("Set a password", 180, 200, 340)],
        ])
        result, stack, written, names = self.run_case(frames)
        self.assertEqual(result, "Chosen_Name9")
        self.assertEqual(stack.typed, ["Chosen_Name9"])
        self.assertEqual(len(written), 3)
        self.assertEqual(
            names,
            ["890_username", "890_username_cleared", "890_username_requested", "891_after_username"],
        )

    def test_xxtouch_multiplication_glyph_is_normalized_to_lowercase_x(self):
        self.assertEqual(
            usernames.suggested_username(page("peachysoft×44")),
            "peachysoftx44",
        )

    def test_ocr_x_confusion_does_not_retype_an_available_username(self):
        frames = iter([
            page("RedditSuggestion"),
            page(),
            page("peachysoft×44", True),
            [node("Set a password", 180, 200, 340)],
        ])
        result, stack, written, names = self.run_case(
            frames,
            requested="peachysoftx44",
        )
        self.assertEqual(result, "peachysoftx44")
        self.assertEqual(stack.typed, ["peachysoftx44"])
        self.assertNotIn("890_username_requested_retype_1", names)
        self.assertEqual(len(written), 3)

    def test_ocr_uppercase_i_for_expected_lowercase_l_does_not_retype(self):
        requested = "calmtrail9"
        ocr_value = "caImtrail9"
        frames = iter([
            page("RedditSuggestion"),
            page(),
            page(ocr_value, True),
            [node("Set a password", 180, 200, 340)],
        ])
        result, stack, written, names = self.run_case(
            frames,
            requested=requested,
        )
        self.assertEqual(result, requested)
        self.assertEqual(stack.typed, [requested])
        self.assertNotIn("890_username_requested_retype_1", names)
        self.assertEqual(len(written), 3)

    def test_lowercase_l_ocr_equivalence_is_expected_value_aware(self):
        nodes = page("caImtrail9")
        field = next(row for row in nodes if row.text == "Username")
        self.assertTrue(
            usernames._username_field_matches_candidate(
                nodes, field, "calmtrail9"
            )
        )
        self.assertFalse(
            usernames._username_field_matches_candidate(
                nodes, field, "calmtrai19"
            )
        )

    def test_unreadable_field_token_is_not_mistaken_for_empty_before_retype(self):
        corrupt = page()
        corrupt.append(node("peachysoft§44", 90, 430, 320))
        frames = iter([page("RedditSuggestion"), corrupt])
        result, stack, written, _names = self.run_case(frames)
        self.assertIsInstance(result, usernames.UsernameAvailabilityUnclear)
        self.assertEqual(stack.typed, [])
        self.assertEqual(written, [])

    def test_transient_typed_value_mismatch_uses_fresh_read_without_retyping(self):
        frames = iter([
            page("RedditSuggestion"),
            page(),
            page("Chosen_Xame9"),
            page("Chosen_Name9", True),
            [node("Set a password", 180, 200, 340)],
        ])
        result, stack, written, names = self.run_case(frames)
        self.assertEqual(result, "Chosen_Name9")
        self.assertEqual(stack.typed, ["Chosen_Name9"])
        self.assertEqual(stack.backspaces, [40])
        self.assertIn("890_username_requested_verify_1", names)
        self.assertEqual(len(written), 3)

    def test_persistent_typed_value_mismatch_clears_and_retypes_once(self):
        frames = iter([
            page("RedditSuggestion"),
            page(),
            page("Chosen_Xame9"),
            page("Chosen_Xame9"),
            page("Chosen_Xame9"),
            page(),
            page("Chosen_Name9", True),
            [node("Set a password", 180, 200, 340)],
        ])
        result, stack, written, names = self.run_case(frames)
        self.assertEqual(result, "Chosen_Name9")
        self.assertEqual(stack.typed, ["Chosen_Name9", "Chosen_Name9"])
        self.assertEqual(stack.backspaces, [40, 40])
        self.assertIn("890_username_requested_retype_1_cleared", names)
        self.assertIn("890_username_requested_retype_1", names)
        self.assertEqual(len(written), 3)

    def test_persistent_mismatch_after_safe_retype_still_pauses(self):
        frames = iter([
            page("RedditSuggestion"),
            page(),
            page("Chosen_Xame9"),
            page("Chosen_Xame9"),
            page("Chosen_Xame9"),
            page(),
            page("Chosen_Xame9"),
            page("Chosen_Xame9"),
            page("Chosen_Xame9"),
        ])
        result, stack, written, _names = self.run_case(frames)
        self.assertIsInstance(result, usernames.UsernameAvailabilityUnclear)
        self.assertEqual(stack.typed, ["Chosen_Name9", "Chosen_Name9"])
        self.assertEqual(stack.backspaces, [40, 40])
        self.assertEqual(written, [])

    def test_transient_banner_may_occlude_title_after_field_is_proven_empty(self):
        frames = iter([
            page("RedditSuggestion"),
            cleared_page_with_occluded_title(),
            page("Chosen_Name9", True),
            [node("Set a password", 180, 200, 340)],
        ])
        result, stack, written, names = self.run_case(frames)
        self.assertEqual(result, "Chosen_Name9")
        self.assertEqual(stack.backspaces, [40])
        self.assertEqual(stack.typed, ["Chosen_Name9"])
        self.assertEqual(
            written,
            [("claim", "Chosen_Name9"), ("slot", "Chosen_Name9"), ("account", "Chosen_Name9")],
        )
        self.assertEqual(
            names,
            ["890_username", "890_username_cleared", "890_username_requested", "891_after_username"],
        )

    def test_ios_bubble_may_clip_username_label_after_field_is_proven_empty(self):
        frames = iter([
            page("RedditSuggestion"),
            cleared_page_with_clipped_field_label(),
            page("Chosen_Name9", True),
            [node("Set a password", 180, 200, 340)],
        ])
        result, stack, written, names = self.run_case(frames)
        self.assertEqual(result, "Chosen_Name9")
        self.assertEqual(stack.backspaces, [40])
        self.assertEqual(stack.typed, ["Chosen_Name9"])
        self.assertEqual(
            written,
            [("claim", "Chosen_Name9"), ("slot", "Chosen_Name9"), ("account", "Chosen_Name9")],
        )
        self.assertEqual(
            names,
            ["890_username", "890_username_cleared", "890_username_requested", "891_after_username"],
        )

    def test_real_a0245_bubble_ocr_proves_empty_field(self):
        frames = iter([
            page("RedditSuggestion"),
            cleared_page_with_real_bubble_ocr(),
            page("Chosen_Name9", True),
            [node("Set a password", 180, 200, 340)],
        ])
        result, stack, written, _names = self.run_case(frames)
        self.assertEqual(result, "Chosen_Name9")
        self.assertEqual(stack.typed, ["Chosen_Name9"])
        self.assertEqual(len(written), 3)

    def test_resume_on_real_a0245_empty_focused_field_types_without_clear(self):
        frames = iter([
            cleared_page_with_real_bubble_ocr(),
            page("Chosen_Name9", True),
            [node("Set a password", 180, 200, 340)],
        ])
        result, stack, written, names = self.run_case(frames)
        self.assertEqual(result, "Chosen_Name9")
        self.assertEqual(stack.backspaces, [])
        self.assertEqual(stack.typed, ["Chosen_Name9"])
        self.assertEqual(names, ["890_username", "890_username_requested", "891_after_username"])
        self.assertEqual(len(written), 3)

    def test_clipped_label_with_field_value_still_pauses(self):
        cleared = cleared_page_with_real_bubble_ocr()
        cleared.append(node("StillPresent9", 100, 430, 220))
        frames = iter([page("RedditSuggestion"), cleared])
        result, stack, written, _names = self.run_case(frames)
        self.assertIsInstance(result, usernames.UsernameAvailabilityUnclear)
        self.assertEqual(stack.typed, [])
        self.assertEqual(written, [])

    def test_clipped_username_label_without_keyboard_still_pauses(self):
        cleared = [
            row
            for row in cleared_page_with_clipped_field_label()
            if row.text not in {"123", "space", "return"}
        ]
        frames = iter([page("RedditSuggestion"), cleared])
        result, stack, written, _names = self.run_case(frames)
        self.assertIsInstance(result, usernames.UsernameAvailabilityUnclear)
        self.assertEqual(stack.typed, [])
        self.assertEqual(written, [])

    def test_occluded_title_without_page_local_anchors_still_pauses(self):
        cleared = cleared_page_with_occluded_title()
        cleared = [row for row in cleared if not row.text.startswith("Need inspiration?")]
        frames = iter([page("RedditSuggestion"), cleared])
        result, stack, written, _names = self.run_case(frames)
        self.assertIsInstance(result, usernames.UsernameAvailabilityUnclear)
        self.assertEqual(stack.typed, [])
        self.assertEqual(written, [])

    def test_unavailable_name_pauses_without_suggestion_or_save(self):
        frames = iter([page("RedditSuggestion"), page(), page("Chosen_Name9", False)])
        with mock.patch.object(usernames, "USERNAME_AVAILABILITY_POLLS", 0):
            result, stack, written, names = self.run_case(frames)
        self.assertIsInstance(result, PhoneError)
        self.assertIn("not confirmed", str(result))
        self.assertEqual(len(stack.taps), 1)
        self.assertEqual(written, [])

    def test_taken_name_uses_next_numeric_variant_and_commits_before_claim(self):
        frames = iter([
            taken_page("Chosen_Name9"),
            page(),
            page("Chosen_Name0", True),
            [node("Set a password", 180, 200, 340)],
        ])
        commits = []
        result, stack, written, names = self.run_case(
            frames,
            committer=lambda old, new: commits.append((old, new)),
        )
        self.assertEqual(result, "Chosen_Name0")
        self.assertEqual(commits, [("Chosen_Name9", "Chosen_Name0")])
        self.assertEqual(stack.typed, ["Chosen_Name0"])
        self.assertEqual(
            written,
            [
                ("claim", "Chosen_Name0"),
                ("slot", "Chosen_Name0"),
                ("account", "Chosen_Name0"),
            ],
        )
        self.assertEqual(names[-1], "891_after_username")

    def test_variant_generator_preserves_length_then_adds_suffix(self):
        self.assertEqual(usernames.requested_username_variant("Name19", 1), "Name20")
        self.assertEqual(len(usernames.requested_username_variant("Name19", 99)), 6)
        self.assertEqual(usernames.requested_username_variant("Name19", 100), "Oame19")
        self.assertEqual(usernames.requested_username_variant("Name", 101), "Name1")

    def test_resume_reuses_checkpointed_candidate_without_retyping(self):
        frames = iter([
            page("Chosen_Name0", True),
            [node("Set a password", 180, 200, 340)],
        ])
        commits = []
        checkpoint = {
            "version": 1,
            "base": "Chosen_Name9",
            "candidate": "Chosen_Name0",
            "index": 1,
            "updated_at_utc": "2026-10-22T00:00:00Z",
        }
        result, stack, written, _names = self.run_case(
            frames,
            committer=lambda old, new: commits.append((old, new)),
            checkpoint=checkpoint,
        )
        self.assertEqual(result, "Chosen_Name0")
        self.assertEqual(commits, [("Chosen_Name9", "Chosen_Name0")])
        self.assertEqual(stack.typed, [])
        self.assertEqual(written[0], ("claim", "Chosen_Name0"))

    def test_queue_commit_failure_stops_before_claim_and_continue(self):
        frames = iter([
            taken_page("Chosen_Name9"),
            page(),
            page("Chosen_Name0", True),
        ])

        def fail_commit(_old, _new):
            raise OSError("synthetic private storage detail")

        result, stack, written, _names = self.run_case(
            frames, committer=fail_commit
        )
        self.assertIsInstance(result, usernames.UsernameVariantPersistenceError)
        self.assertIn("could not be saved to the queue", str(result))
        self.assertNotIn("synthetic private", str(result))
        self.assertEqual(written, [])
        self.assertEqual(len(stack.taps), 1)

    def test_visual_green_confirmation_recovers_when_ocr_omits_sentence(self):
        frames = iter([
            page("Chosen_Name9", False),
            [node("Set a password", 180, 200, 340)],
        ])
        stack = Stack(available_username_png())
        result, stack, written, names = self.run_case(frames, stack=stack)
        self.assertEqual(result, "Chosen_Name9")
        self.assertEqual(len(stack.taps), 1)
        self.assertEqual(
            written,
            [("claim", "Chosen_Name9"), ("slot", "Chosen_Name9"), ("account", "Chosen_Name9")],
        )

    def test_continue_is_retried_once_then_reaches_password(self):
        frames = iter([
            page("Chosen_Name9", True),
            page("Chosen_Name9", True),
            page("Chosen_Name9", True),
            [node("Set a password", 180, 200, 340)],
        ])
        with mock.patch.object(usernames, "USERNAME_TRANSITION_POLLS", 1):
            result, stack, written, names = self.run_case(frames)
        self.assertEqual(result, "Chosen_Name9")
        self.assertEqual(len(stack.taps), 2)
        self.assertEqual(names[-1], "892_username_retry_stall_0")

    def test_second_continue_stall_yields_to_watchdog_and_restores_progress_hook(self):
        frames = iter([
            page("Chosen_Name9", True),
            page("Chosen_Name9", True),
            page("Chosen_Name9", True),
            page("Chosen_Name9", True),
        ])
        stack = Stack(cancel_on_wait=4)
        phone = type("Phone", (), {})()
        progress_hook = lambda: None
        phone.progress_note = progress_hook
        logs = []
        written = []
        names = []

        def dump(_stack, _phone, stem, _log, shot=True, **_kwargs):
            names.append(stem)
            return "", next(frames)

        with (
            mock.patch.object(usernames, "USERNAME_TRANSITION_POLLS", 1),
            mock.patch.object(usernames, "get_slot", return_value={"email": "test@icloud.com"}),
            mock.patch.object(usernames, "dump_named", side_effect=dump),
            mock.patch.object(usernames, "username_variant_attempt", return_value=None),
            mock.patch.object(usernames, "mark_username_variant_attempt"),
            mock.patch.object(usernames, "clear_username_variant_attempt"),
            mock.patch.object(usernames, "username_owners", return_value={}),
            mock.patch.object(usernames, "claim_username", return_value={"status": "processing"}),
            mock.patch.object(usernames, "set_slot_username", side_effect=lambda *_args: None),
            mock.patch.object(usernames, "write_account_text", side_effect=lambda *_args: "account.txt"),
            mock.patch.object(usernames, "log", side_effect=logs.append),
        ):
            with self.assertRaisesRegex(OperationCancelled, "watchdog fired"):
                usernames.use_requested_username(stack, phone, "a0049", "Chosen_Name9")

        self.assertEqual(len(stack.taps), 2)
        self.assertIs(phone.progress_note, progress_hook)
        self.assertTrue(any("180-second inactivity watchdog" in line for line in logs))

    def test_saved_mismatch_stops_before_any_phone_action(self):
        result, stack, written, names = self.run_case(iter([]), saved="OtherName")
        self.assertIsInstance(result, PhoneError)
        self.assertEqual((stack.taps, stack.typed, written, names), ([], [], [], []))

    def test_claim_failure_stops_before_continue_and_other_persistence(self):
        frames = iter([
            page("Chosen_Name9", True),
        ])
        stack = Stack()
        written = []

        with (
            mock.patch.object(usernames, "get_slot", return_value={"email": "test@icloud.com"}),
            mock.patch.object(usernames, "dump_named", return_value=("", next(frames))),
            mock.patch.object(usernames, "username_variant_attempt", return_value=None),
            mock.patch.object(usernames, "mark_username_variant_attempt"),
            mock.patch.object(usernames, "clear_username_variant_attempt"),
            mock.patch.object(usernames, "username_owners", return_value={}),
            mock.patch.object(
                usernames, "claim_username",
                side_effect=usernames.UsernameLedgerError("synthetic private storage detail"),
            ),
            mock.patch.object(usernames, "set_slot_username", side_effect=lambda *args: written.append("slot")),
            mock.patch.object(usernames, "write_account_text", side_effect=lambda *args: written.append("account")),
        ):
            with self.assertRaises(PhoneError) as caught:
                usernames.use_requested_username(stack, object(), "a0049", "Chosen_Name9")

        self.assertIn("durable processing state", str(caught.exception))
        self.assertNotIn("synthetic private", str(caught.exception))
        self.assertEqual(stack.taps, [])
        self.assertEqual(written, [])


if __name__ == "__main__":
    unittest.main()
