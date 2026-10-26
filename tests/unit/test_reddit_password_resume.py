"""Offline tests for the crash-safe Reddit password checkpoint."""

from __future__ import annotations

import io
import unittest
from datetime import date
from unittest import mock

from PIL import Image, ImageDraw

from friend_poster.reddit import reddit_create_day1 as create
from friend_poster.reddit import reddit_password as password
from friend_poster.device.ssh_device import PhoneError
from friend_poster.device.ui_tree import Node, OperationCancelled


def node(text: str, x: int, y: int, w: int = 180, h: int = 30) -> Node:
    return Node(ntype="ocr-line", text=text, x=x, y=y, w=w, h=h)


def password_nodes(*, retry_later: bool = False) -> list[Node]:
    nodes = [
        node("Set a password", 160, 110, 400),
        node("Password", 70, 300),
        node("Password must be at least 8 characters", 70, 390, 500),
        node("Continue", 280, 1180, 190, 50),
    ]
    if retry_later:
        nodes.append(node("Sorry, please try again later.", 170, 1100, 410))
    return nodes


def expired_code_nodes() -> list[Node]:
    return [
        node("Verification code is expired or has alreadv", 90, 160, 580),
        node("been used", 90, 200, 180),
        node("Try again", 570, 260, 150),
        *password_nodes(),
    ]


def selected_email_nodes(email: str = "fixture_grands.6h@icloud.com") -> list[Node]:
    return [
        Node(ntype="ocr", text="Hi new friend,", x=370, y=200, w=334, h=41),
        Node(ntype="ocr", text="welcome to Reddit", x=368, y=244, w=444, h=30),
        Node(
            ntype="ocr",
            text="Create an account to get started",
            x=370,
            y=316,
            w=509,
            h=25,
        ),
        Node(ntype="ocr", text="Email", x=95, y=424, w=72, h=15),
        Node(ntype="ocr", text=email, x=292, y=455, w=500, h=30),
        Node(ntype="ocr", text="Continue", x=300, y=800, w=140, h=40),
    ]


def selected_email_with_keyboard_nodes(
    email: str = "fixture_grands.6h@icloud.com",
) -> list[Node]:
    rows = [row for row in selected_email_nodes(email) if row.text != "Continue"]
    rows.append(Node(ntype="ocr", text="done", x=613, y=1157, w=81, h=23))
    return rows


def valid_password_png() -> bytes:
    image = Image.new("RGB", (1125, 2436), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((820, 520, 870, 570), fill=(0, 130, 40))
    draw.rectangle((40, 2050, 1085, 2310), fill=(220, 55, 0))
    data = io.BytesIO()
    image.save(data, "PNG")
    return data.getvalue()


class Stack:
    def __init__(self, png: bytes, *, cancel_on_wait: int | None = None):
        self.png = png
        self.taps: list[tuple[int, int]] = []
        self.wait_calls = 0
        self.cancel_on_wait = cancel_on_wait

    def screenshot_png(self):
        return self.png

    def tap_xy(self, x: int, y: int):
        self.taps.append((x, y))

    def wait(self, _seconds: float):
        self.wait_calls += 1
        if self.wait_calls == self.cancel_on_wait:
            raise OperationCancelled("180-second inactivity watchdog fired")


class RedditPasswordResumeTests(unittest.TestCase):
    def test_new_password_uses_one_bounded_xxtouch_script(self):
        class TypingStack(Stack):
            def __init__(self):
                super().__init__(valid_password_png())
                self.typed: list[tuple[str, int]] = []

            def type_human(self, value: str, ms: int = 95):
                self.typed.append((value, ms))

        stack = TypingStack()
        requested = "Fixture-Password-7"
        frames = iter([
            password_nodes(),
            password_nodes(),
            [node("About you", 120, 150, 300)],
        ])
        with (
            mock.patch.object(
                password,
                "get_slot",
                return_value={"email": "saved@example.invalid", "username": "saved-user"},
            ),
            mock.patch.object(password, "set_slot_password"),
            mock.patch.object(password, "write_account_text", return_value="fixture.txt"),
            mock.patch.object(
                password,
                "dump_named",
                side_effect=lambda *_args, **_kwargs: ("", next(frames)),
            ),
            mock.patch.object(password.time, "sleep", return_value=None),
        ):
            result = password.type_password(
                stack, object(), "a0047", requested_password=requested
            )

        self.assertEqual(result, requested)
        self.assertEqual(stack.typed, [(requested, 95)])

    def test_expired_code_try_again_resubmits_selected_email_once(self):
        stack = Stack(valid_password_png())
        advanced = [node("Verify your email", 120, 150, 300)]
        frames = iter([selected_email_nodes(), advanced])
        logs: list[str] = []
        with mock.patch.object(
            password,
            "dump_named",
            side_effect=lambda *_args, **_kwargs: ("", next(frames)),
        ):
            result = password.recover_expired_verification(
                stack,
                object(),
                expired_code_nodes(),
                "fixture_grands.6h@icloud.com",
                logs.append,
            )
        self.assertIs(result, advanced)
        self.assertEqual(stack.taps, [(645, 275), (370, 820)])

    def test_expired_code_recovery_hides_keyboard_before_email_continue(self):
        stack = Stack(valid_password_png())
        selected = selected_email_nodes()
        advanced = [node("Verify your email", 120, 150, 300)]
        frames = iter([selected_email_with_keyboard_nodes(), selected, advanced])
        logs: list[str] = []
        with mock.patch.object(
            password,
            "dump_named",
            side_effect=lambda *_args, **_kwargs: ("", next(frames)),
        ):
            result = password.recover_expired_verification(
                stack,
                object(),
                expired_code_nodes(),
                "fixture_grands.6h@icloud.com",
                logs.append,
            )
        self.assertIs(result, advanced)
        self.assertEqual(stack.taps, [(645, 275), (653, 1168), (370, 820)])
        self.assertTrue(any("keyboard covering Continue" in line for line in logs))

    def test_keyboard_done_may_submit_selected_email_without_duplicate_continue(self):
        stack = Stack(valid_password_png())
        advanced = [node("Verify your email", 120, 150, 300)]
        frames = iter([selected_email_with_keyboard_nodes(), advanced])
        logs: list[str] = []
        with mock.patch.object(
            password,
            "dump_named",
            side_effect=lambda *_args, **_kwargs: ("", next(frames)),
        ):
            result = password.recover_expired_verification(
                stack,
                object(),
                expired_code_nodes(),
                "fixture_grands.6h@icloud.com",
                logs.append,
            )
        self.assertIs(result, advanced)
        self.assertEqual(stack.taps, [(645, 275), (653, 1168)])
        self.assertTrue(any("not tapping Continue again" in line for line in logs))

    def test_expired_code_email_continue_retries_exactly_once(self):
        stack = Stack(valid_password_png())
        selected = selected_email_nodes()
        advanced = [node("Verify your email", 120, 150, 300)]
        frames = iter([selected, selected, advanced])
        logs: list[str] = []
        with (
            mock.patch.object(password, "EXPIRED_EMAIL_TRANSITION_POLLS", 1),
            mock.patch.object(
                password,
                "dump_named",
                side_effect=lambda *_args, **_kwargs: ("", next(frames)),
            ),
        ):
            result = password.recover_expired_verification(
                stack,
                object(),
                expired_code_nodes(),
                "fixture_grands.6h@icloud.com",
                logs.append,
            )
        self.assertIs(result, advanced)
        self.assertEqual(stack.taps, [(645, 275), (370, 820), (370, 820)])
        self.assertTrue(any("retrying the exact Continue once" in line for line in logs))

    def test_expired_code_email_continue_never_taps_a_third_time(self):
        stack = Stack(valid_password_png())
        selected = selected_email_nodes()
        frames = iter([selected, selected, selected])
        with (
            mock.patch.object(password, "EXPIRED_EMAIL_TRANSITION_POLLS", 1),
            mock.patch.object(
                password,
                "dump_named",
                side_effect=lambda *_args, **_kwargs: ("", next(frames)),
            ),
        ):
            with self.assertRaisesRegex(PhoneError, "one allowed Continue retry"):
                password.recover_expired_verification(
                    stack,
                    object(),
                    expired_code_nodes(),
                    "fixture_grands.6h@icloud.com",
                    lambda _message: None,
                )
        self.assertEqual(stack.taps, [(645, 275), (370, 820), (370, 820)])

    def test_saved_password_taps_continue_once_and_reaches_about(self):
        stack = Stack(valid_password_png())
        about = [node("About you", 120, 150, 300)]
        with (
            mock.patch.object(password.time, "sleep", return_value=None),
            mock.patch.object(password, "dump_named", return_value=("", about)),
        ):
            result = password.resume_saved_password(
                stack, object(), password_nodes(), lambda _message: None
            )
        self.assertIs(result, about)
        self.assertEqual(stack.taps, [(375, 1205)])

    def test_saved_password_retry_later_retries_once_then_yields_to_watchdog(self):
        stack = Stack(valid_password_png(), cancel_on_wait=4)
        logs: list[str] = []
        with (
            mock.patch.object(password, "PASSWORD_TRANSITION_POLLS", 1),
            mock.patch.object(
                password,
                "dump_named",
                return_value=("", password_nodes(retry_later=True)),
            ),
        ):
            with self.assertRaisesRegex(OperationCancelled, "watchdog fired"):
                password.resume_saved_password(
                    stack, object(), password_nodes(), logs.append
                )
        self.assertEqual(stack.taps, [(375, 1205), (375, 1205)])
        self.assertTrue(any("retrying the exact enabled Continue once" in line for line in logs))
        self.assertTrue(any("180-second inactivity watchdog" in line for line in logs))

    def test_unrecognized_retry_toast_still_retries_once_then_yields_to_watchdog(self):
        stack = Stack(valid_password_png(), cancel_on_wait=4)
        with (
            mock.patch.object(password, "PASSWORD_TRANSITION_POLLS", 1),
            mock.patch.object(
                password,
                "dump_named",
                return_value=("", password_nodes()),
            ),
        ):
            with self.assertRaisesRegex(OperationCancelled, "watchdog fired"):
                password.resume_saved_password(
                    stack, object(), password_nodes(), lambda _message: None
                )
        self.assertEqual(stack.taps, [(375, 1205), (375, 1205)])

    def test_second_continue_can_reach_about(self):
        stack = Stack(valid_password_png())
        about = [node("About you", 120, 150, 300)]
        frames = iter([
            password_nodes(retry_later=True),
            password_nodes(),
            about,
        ])
        with (
            mock.patch.object(password, "PASSWORD_TRANSITION_POLLS", 1),
            mock.patch.object(
                password,
                "dump_named",
                side_effect=lambda *_args, **_kwargs: ("", next(frames)),
            ),
        ):
            result = password.resume_saved_password(
                stack, object(), password_nodes(), lambda _message: None
            )
        self.assertIs(result, about)
        self.assertEqual(stack.taps, [(375, 1205), (375, 1205)])

    def test_missing_green_and_orange_visual_proof_never_taps(self):
        empty = io.BytesIO()
        Image.new("RGB", (1125, 2436), "white").save(empty, "PNG")
        stack = Stack(empty.getvalue())
        with self.assertRaisesRegex(PhoneError, "could not prove the green validation"):
            password.resume_saved_password(
                stack, object(), password_nodes(), lambda _message: None
            )
        self.assertEqual(stack.taps, [])

    def test_expired_verification_banner_is_reported_without_a_tap(self):
        stack = Stack(valid_password_png())
        with self.assertRaisesRegex(
            PhoneError, "Verification code is expired or has already been used"
        ):
            password.resume_saved_password(
                stack, object(), expired_code_nodes(), lambda _message: None
            )
        self.assertEqual(stack.taps, [])

    def test_run_slot_resumes_without_replaying_signup_stages(self):
        events: list[str] = []
        slot = {
            "id": "a0047",
            "ghost": True,
            "email": "saved@example.invalid",
            "username": "saved-user",
            "password": "saved-password",
            "reddit": False,
        }
        about = [node("About you", 120, 150, 300)]
        interests = [node("Choose your interests", 90, 150, 400)]

        def forbidden(name):
            def call(*_args, **_kwargs):
                raise AssertionError(f"{name} must not run during password resume")

            return call

        with (
            mock.patch.object(create, "get_slot", return_value=slot),
            mock.patch.object(
                create, "dump_named", return_value=("", password_nodes())
            ),
            mock.patch.object(
                create,
                "resume_saved_password",
                side_effect=lambda *_a, **_k: events.append("resume-password") or about,
            ),
            mock.patch.object(create, "run_to_verify", side_effect=forbidden("email")),
            mock.patch.object(
                create, "poll_reddit_code", side_effect=forbidden("verification")
            ),
            mock.patch.object(
                create, "keep_suggested_username", side_effect=forbidden("username")
            ),
            mock.patch.object(
                create, "type_password", side_effect=forbidden("password typing")
            ),
            mock.patch.object(
                create,
                "tap_woman",
                side_effect=lambda *_a, **_k: events.append("about"),
            ),
            mock.patch.object(
                create,
                "wait_named",
                side_effect=lambda *_a, **_k: events.append("interests") or interests,
            ),
            mock.patch.object(
                create,
                "pick_interests",
                side_effect=lambda *_a, **_k: events.append("pick-interests"),
            ),
            mock.patch.object(
                create,
                "wait_feed",
                side_effect=lambda *_a, **_k: events.append("feed") or object(),
            ),
            mock.patch.object(
                create,
                "mark_reddit_created",
                side_effect=lambda *_a, **_k: events.append("mark-created"),
            ),
            mock.patch.object(
                create,
                "_close_and_finalize_cleanup",
                side_effect=lambda *_a, **_k: events.append("cleanup"),
            ),
        ):
            create.run_slot(
                "a0047",
                object(),
                object(),
                birth_date_override=date(2005, 4, 20),
                feed_swipes=0,
                log_fn=lambda _message: None,
            )

        self.assertEqual(
            events,
            [
                "resume-password",
                "about",
                "interests",
                "pick-interests",
                "feed",
                "mark-created",
                "cleanup",
            ],
        )


if __name__ == "__main__":
    unittest.main()
