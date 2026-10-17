"""Offline tests for the crash-safe Reddit password checkpoint."""

from __future__ import annotations

import io
import unittest
from datetime import date
from unittest import mock

from PIL import Image, ImageDraw

import reddit_create_day1 as create
import reddit_password as password
from ssh_device import PhoneError
from ui_tree import Node


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


def valid_password_png() -> bytes:
    image = Image.new("RGB", (1125, 2436), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((820, 520, 870, 570), fill=(0, 130, 40))
    draw.rectangle((40, 2050, 1085, 2310), fill=(220, 55, 0))
    data = io.BytesIO()
    image.save(data, "PNG")
    return data.getvalue()


class Stack:
    def __init__(self, png: bytes):
        self.png = png
        self.taps: list[tuple[int, int]] = []

    def screenshot_png(self):
        return self.png

    def tap_xy(self, x: int, y: int):
        self.taps.append((x, y))


class RedditPasswordResumeTests(unittest.TestCase):
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

    def test_saved_password_rejection_is_explicit_and_not_retried(self):
        stack = Stack(valid_password_png())
        with (
            mock.patch.object(password.time, "sleep", return_value=None),
            mock.patch.object(
                password,
                "dump_named",
                return_value=("", password_nodes(retry_later=True)),
            ),
        ):
            with self.assertRaisesRegex(PhoneError, "Sorry, please try again later"):
                password.resume_saved_password(
                    stack, object(), password_nodes(), lambda _message: None
                )
        self.assertEqual(stack.taps, [(375, 1205)])

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
