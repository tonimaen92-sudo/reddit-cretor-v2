# UTF-8
"""Focused immutable regressions for Reddit's logged-out signup doorway."""

from __future__ import annotations

import os
import sys
from pathlib import Path
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from friend_poster.reddit import reddit_to_verify as subject  # noqa: E402
from friend_poster.device.ssh_device import PhoneError  # noqa: E402
from friend_poster.device.ui_tree import Node, parse_nodes  # noqa: E402


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(("PASS " if condition else "FAIL ") + name + ((" :: " + detail) if detail else ""))


def expect_phone_error(name: str, call, message: str) -> None:
    try:
        call()
    except PhoneError as exc:
        check(name, message in str(exc), str(exc))
    else:
        check(name, False, "PhoneError was not raised")


def node(text: str, x: int, y: int, w: int, h: int = 28) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=y, w=w, h=h, visible=True, enabled=True)


def doorway_fixture(*, icon_prefix: str = "� ") -> list[Node]:
    return [
        node("Search Reddit", 203, 105, 256, 23),
        node("Log in", 597, 104, 106, 26),
        node("Sign up to customize Reddit", 0, 987, 540, 31),
        node("Sign up to customize your home feed, upvote", 6, 1052, 647, 25),
        node("content, and start joining conversations.", 22, 1082, 562, 23),
        node(icon_prefix + "Sign up / Log in", 35, 1146, 315, 28),
        node(icon_prefix + "Settings", 28, 1219, 216, 36),
        node("X", 659, 985, 44, 27),
    ]


def live_fixture(name: str) -> list[Node]:
    path = Path(__file__).parents[1] / "fixtures" / name
    return parse_nodes(path.read_text(encoding="utf-8"))


def home_fixture() -> list[Node]:
    return [
        node("Search Reddit", 203, 105, 256, 23),
        node("Log in", 603, 105, 100, 23),
        node("Home", 94, 1259, 62, 12),
        node("Inbox", 344, 1259, 59, 12),
        node("You", 603, 1259, 40, 12),
    ]


def home_fixture_without_you() -> list[Node]:
    return [item for item in home_fixture() if item.text != "You"]


def provider_fixture() -> list[Node]:
    return [
        node("Log in or sign up", 190, 300, 370),
        node("Continue with Google", 120, 520, 510),
        node("Continue with Apple", 120, 600, 510),
        node("Continue with email", 120, 680, 510),
    ]


def verify_fixture() -> list[Node]:
    return [node("Verify your email", 180, 240, 390)]


class FakeStack:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class ResolutionAwareStack(FakeStack):
    def __init__(self, size: tuple[int, int]) -> None:
        super().__init__()
        self.size = size
        self.refreshes = 0

    def refresh_screen_geometry(self) -> tuple[int, int]:
        self.refreshes += 1
        return self.size


def classify_regressions() -> None:
    observed = doorway_fixture()
    action = subject.signup_customize_doorway_node(observed)
    check(
        "doorway-exact-a0040-frame",
        action is not None and action.center() == (192, 1160),
        repr(action),
    )
    check("doorway-post-you-state", subject.post_you_state(observed) == "doorway")
    check(
        "doorway-no-icon-also-exact",
        subject.signup_customize_doorway_node(doorway_fixture(icon_prefix="")) is not None,
    )
    live_missing_close = live_fixture("a0108_reddit_signup_drawer_missing_close.json")
    live_action = subject.signup_customize_doorway_node(live_missing_close)
    check(
        "doorway-live-a0108-missing-close-recognized",
        live_action is not None
        and live_action.center() == (192, 1160)
        and subject.post_you_state(live_missing_close) == "doorway",
        repr(live_action),
    )
    shifted = live_fixture("a0109_reddit_signup_drawer_shifted.json")
    shifted_action = subject.signup_customize_doorway_node(shifted)
    check(
        "doorway-live-a0109-shifted-recognized",
        shifted_action is not None
        and shifted_action.center() == (190, 1104)
        and subject.post_you_state(shifted) == "doorway",
        repr(shifted_action),
    )

    required = [
        "Search Reddit",
        "Log in",
        "Sign up to customize Reddit",
        "� Sign up / Log in",
        "� Settings",
    ]
    for label in required:
        reduced = [item for item in observed if item.text != label]
        check(
            "doorway-missing-" + label.lower().replace(" ", "-").replace("/", "slash"),
            subject.signup_customize_doorway_node(reduced) is None,
        )

    for label in required:
        original = next(item for item in observed if item.text == label)
        duplicated = observed + [
            node(original.text, original.x + 1, original.y + 1, original.w, original.h)
        ]
        check(
            "doorway-duplicate-" + label.lower().replace(" ", "-").replace("/", "slash"),
            subject.signup_customize_doorway_node(duplicated) is None,
        )

    moved_action = doorway_fixture()
    moved_action[5].y = 900
    check("doorway-action-out-of-band", subject.signup_customize_doorway_node(moved_action) is None)
    no_body_or_close = [
        item
        for item in doorway_fixture()
        if item.text
        not in (
            "Sign up to customize your home feed, upvote",
            "content, and start joining conversations.",
            "X",
        )
    ]
    check(
        "doorway-decorative-copy-and-close-optional",
        subject.signup_customize_doorway_node(no_body_or_close) is not None,
    )
    malformed_action = doorway_fixture()
    malformed_action[5].text = "� Sign up / Login"
    check("doorway-malformed-action-refused", subject.signup_customize_doorway_node(malformed_action) is None)
    malformed_settings = doorway_fixture()
    malformed_settings[6].text = "� Account Settings"
    check("doorway-malformed-settings-refused", subject.signup_customize_doorway_node(malformed_settings) is None)
    arbitrary = [
        node("Search Reddit", 203, 105, 256),
        node("Log in", 597, 104, 106),
        node("Sign up for amazing things", 20, 990, 500),
        node("Sign up / Log in", 35, 1146, 315),
    ]
    check("doorway-arbitrary-signup-copy-refused", subject.signup_customize_doorway_node(arbitrary) is None)
    profile = doorway_fixture() + [node("Edit profile", 250, 310, 250)]
    check("doorway-existing-profile-refused", subject.post_you_state(profile) == "profile")

    inferred = subject.home_feed_you_node(home_fixture_without_you())
    check(
        "home-missing-you-infers-right-tab-from-four-exact-anchors",
        inferred is not None and inferred.center() == (621, 1265),
        repr(inferred),
    )
    no_login = [item for item in home_fixture_without_you() if item.text != "Log in"]
    check(
        "home-missing-you-without-login-refused",
        subject.home_feed_you_node(no_login) is None,
    )
    misaligned = home_fixture_without_you()
    next(item for item in misaligned if item.text == "Inbox").y = 1170
    check(
        "home-missing-you-misaligned-nav-refused",
        subject.home_feed_you_node(misaligned) is None,
    )


def routing_regressions() -> None:
    original_wait = subject._wait
    original_log = subject.log
    original_warn = subject.warn_siwa
    original_dump = subject.dump_named
    original_open = subject.open_container
    subject._wait = lambda *_args, **_kwargs: None
    subject.log = lambda _message: None
    subject.warn_siwa = lambda *_args, **_kwargs: None
    try:
        live_action = subject.signup_customize_doorway_node(
            live_fixture("a0108_reddit_signup_drawer_missing_close.json")
        )
        scaled_stack = ResolutionAwareStack((1125, 2436))
        subject._tap_signup_doorway(scaled_stack, live_action)
        check(
            "doorway-scaled-portrait-refreshes-and-taps-detected-row",
            scaled_stack.refreshes == 1 and scaled_stack.taps == [(192, 1160)],
            repr(scaled_stack.taps),
        )
        landscape_stack = ResolutionAwareStack((2436, 1125))
        expect_phone_error(
            "doorway-landscape-geometry-stops",
            lambda: subject._tap_signup_doorway(landscape_stack, live_action),
            "requires verified portrait geometry",
        )
        check("doorway-landscape-geometry-no-tap", landscape_stack.taps == [])

        stack = FakeStack()
        you_taps = [0]
        doorway_taps = [0]
        result = subject.step(
            stack,
            object(),
            home_fixture(),
            "fixture@icloud.com",
            [True],
            "a0040",
            [0],
            [0],
            [0],
            [0],
            you_taps,
            signup_doorway_taps=doorway_taps,
        )
        check("doorway-home-one-you-tap", result == "YOU_PROBE" and len(stack.taps) == 1)

        inferred_stack = FakeStack()
        inferred_result = subject.step(
            inferred_stack,
            object(),
            home_fixture_without_you(),
            "fixture@icloud.com",
            [True],
            "a0040",
            [0],
            [0],
            [0],
            [0],
            [0],
            signup_doorway_taps=[0],
        )
        check(
            "doorway-home-missing-you-taps-inferred-right-tab-once",
            inferred_result == "YOU_PROBE" and inferred_stack.taps == [(621, 1265)],
            repr(inferred_stack.taps),
        )

        subject.dump_named = lambda *_args, **_kwargs: ("synthetic", doorway_fixture())
        after_you = subject.dump_after_you(stack, object())
        check("doorway-after-you-recognized-without-tap", subject.post_you_state(after_you) == "doorway" and len(stack.taps) == 1)

        result = subject.step(
            stack,
            object(),
            after_you,
            "fixture@icloud.com",
            [True],
            "a0040",
            [0],
            [0],
            [0],
            [0],
            you_taps,
            signup_doorway_taps=doorway_taps,
        )
        check(
            "doorway-one-signup-login-tap",
            result == "DOORWAY_TAP"
            and doorway_taps == [1]
            and stack.taps[-1] == (192, 1160),
            repr(stack.taps),
        )
        taps_after_doorway = list(stack.taps)
        expect_phone_error(
            "doorway-never-retapped",
            lambda: subject.step(
                stack,
                object(),
                after_you,
                "fixture@icloud.com",
                [True],
                "a0040",
                [0],
                [0],
                [0],
                [0],
                you_taps,
                signup_doorway_taps=doorway_taps,
            ),
            "already tapped once",
        )
        check("doorway-repeat-refusal-no-tap", stack.taps == taps_after_doorway)

        frames = iter([doorway_fixture(), provider_fixture()])
        subject.dump_named = lambda *_args, **_kwargs: ("synthetic", next(frames))
        routed = subject.wait_after_signup_doorway(stack, object())
        check("doorway-transition-reaches-provider", routed == provider_fixture())
        check("doorway-transition-does-not-tap", stack.taps == taps_after_doorway)

        subject.dump_named = lambda *_args, **_kwargs: ("synthetic", doorway_fixture())
        before_timeout = list(stack.taps)
        expect_phone_error(
            "doorway-transition-timeout-specific",
            lambda: subject.wait_after_signup_doorway(stack, object()),
            "after at most two verified taps",
        )
        check(
            "doorway-transition-one-verified-retry",
            stack.taps == before_timeout + [(192, 1160)],
            repr(stack.taps),
        )

        unknown = [node("Unrelated screen", 200, 400, 300)]
        subject.dump_named = lambda *_args, **_kwargs: ("synthetic", unknown)
        before_unknown = list(stack.taps)
        expect_phone_error(
            "doorway-transition-unknown-stops",
            lambda: subject.wait_after_signup_doorway(stack, object()),
            "No blind tap was attempted",
        )
        check("doorway-transition-unknown-no-retry", stack.taps == before_unknown)

        # A fresh process may start directly on the sheet. It must not reopen Crane.
        direct_stack = FakeStack()
        direct_frames = iter(
            [doorway_fixture(), provider_fixture(), verify_fixture(), verify_fixture()]
        )
        subject.dump_named = lambda *_args, **_kwargs: ("synthetic", next(direct_frames))
        open_calls: list[tuple] = []
        subject.open_container = lambda *args, **kwargs: open_calls.append((args, kwargs))
        subject.run_to_verify(
            direct_stack,
            object(),
            "a0040",
            "fixture@icloud.com",
        )
        check("doorway-direct-resume-does-not-open-crane", open_calls == [], repr(open_calls))
        check(
            "doorway-direct-resume-exact-two-actions",
            direct_stack.taps == [(192, 1160), (375, 694)],
            repr(direct_stack.taps),
        )
    finally:
        subject._wait = original_wait
        subject.log = original_log
        subject.warn_siwa = original_warn
        subject.dump_named = original_dump
        subject.open_container = original_open


def main() -> int:
    classify_regressions()
    routing_regressions()
    print(f"REDDIT SIGNUP DOORWAY REGRESS {len(PASS)} pass {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
