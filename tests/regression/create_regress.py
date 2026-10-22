# UTF-8
"""Offline regressions for birthday, Crane selection, and feed navigation.

Synthetic nodes and fakes only: no phone, USB, SSH, network, or secrets file.
Run: python misc/create_regress.py
"""

from __future__ import annotations

import os
import sys
import inspect
from datetime import date
from friend_poster.reddit import reddit_about as about  # noqa: E402
from friend_poster.reddit import reddit_create_day1 as create  # noqa: E402
from friend_poster.reddit import reddit_open as open_reddit  # noqa: E402
from friend_poster.reddit import reddit_to_verify as to_verify  # noqa: E402
from friend_poster.device import ui_tree as ui_tree_module  # noqa: E402
from friend_poster.device.ssh_device import PhoneError  # noqa: E402
from friend_poster.device.ui_tree import Node, collapsed_text  # noqa: E402
from friend_poster.core.paths import FIXTURES_DIR


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(
        ("PASS " if condition else "FAIL ")
        + name
        + ((" :: " + detail) if detail else ""),
        flush=True,
    )


def expect_phone_error(name: str, call, contains: str = "", secret: str = "") -> None:
    try:
        call()
    except PhoneError as exc:
        message = str(exc)
        ok = (not contains or contains.lower() in message.lower()) and (
            not secret or secret not in message
        )
        check(name, ok, message)
    else:
        check(name, False, "PhoneError was not raised")


def node(text: str, x: int, center_y: int, *, w: int = 80, h: int = 20) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=center_y - h // 2, w=w, h=h)


def saved_dump_nodes(stem: str) -> list[Node]:
    with open(
        os.path.join(about.DUMP_DIR, stem + ".json"), encoding="utf-8"
    ) as handle:
        return ui_tree_module.parse_nodes(handle.read())


def fixture_nodes(stem: str) -> list[Node]:
    path = FIXTURES_DIR / (stem + ".json")
    with open(path, encoding="utf-8") as handle:
        return ui_tree_module.parse_nodes(handle.read())


def birthday_nodes(day: int, month: int, year: int) -> list[Node]:
    month_names = {value: key.title() for key, value in about.MONTHS.items()}
    days = [max(1, day - 1), day, min(31, day + 1)]
    months = [((month - 2) % 12) + 1, month, (month % 12) + 1]
    years = [year - 1, year, year + 1]
    rows = (625, about.BIRTHDAY_WHEEL_CENTER_Y, 725)
    result = [node("About you", 40, 110, w=180), node("Birthday", 40, 190, w=160)]
    result.extend(node(str(value), 90, y, w=60) for value, y in zip(days, rows))
    result.extend(
        node(month_names[value], 285, y, w=130) for value, y in zip(months, rows)
    )
    result.extend(node(str(value), 545, y, w=100) for value, y in zip(years, rows))
    return result


def birthday_nodes_with_day_window(
    day: int,
    month: int,
    year: int,
    visible_days: list[int],
) -> list[Node]:
    """Build a picker with a wider day window and one proven centered day."""
    if day not in visible_days:
        raise ValueError("centered day must be present in the synthetic window")
    result = [
        item
        for item in birthday_nodes(day, month, year)
        if not (
            item.center()[0] < 230
            and item.text.isdigit()
            and 1 <= int(item.text) <= 31
        )
    ]
    centered_index = visible_days.index(day)
    result.extend(
        node(
            str(value),
            90,
            about.BIRTHDAY_WHEEL_CENTER_Y + ((index - centered_index) * 50),
            w=60,
        )
        for index, value in enumerate(visible_days)
    )
    return result


def crane_list_nodes(*slot_ids: str) -> list[Node]:
    """Synthetic proven Crane list with selectable body rows."""
    result = [node("Select Container", 210, 150, w=320, h=28)]
    result.extend(
        node(slot_id, 220, 330 + (index * 95), w=150, h=28)
        for index, slot_id in enumerate(slot_ids)
    )
    return result


def feed_nodes() -> list[Node]:
    return [
        node("Home", 35, 1200, w=90),
        node("Inbox", 610, 1200, w=100),
    ]


def feed_frame(*, offset: int = 0, navigation: bool = True) -> list[Node]:
    """Synthetic feed frame with enough exact anchors to prove movement."""
    result = [
        node(f"Distinct feed content anchor {index}", 40, 300 + index * 150 + offset, w=500)
        for index in range(4)
    ]
    if navigation:
        result.extend(feed_nodes())
    return result


def sparse_feed_frame(
    *,
    offset: int = 0,
    navigation: bool = True,
    anchor_count: int = 2,
    x_offsets: tuple[int, ...] = (),
) -> list[Node]:
    """Synthetic Reddit feed with the a0022 minimum of two unique anchors."""
    anchor_texts = (
        "Sparse unique community anchor",
        "Sparse unique post title anchor",
    )
    result = [
        node(
            text,
            80 + (x_offsets[index] if index < len(x_offsets) else 0),
            330 + (index * 180) + offset,
            w=430,
        )
        for index, text in enumerate(anchor_texts[:anchor_count])
    ]
    # These mirror the structural Reddit evidence in the real a0022 frame.
    result.extend(
        (
            node("Search Reddit", 260, 115, w=225),
            node("For You", 135, 190, w=130),
            node("Following", 459, 193, w=166),
            node("Join", 568, 253, w=78),
        )
    )
    if navigation:
        result.extend(feed_nodes())
    return result


class FakeStack:
    def __init__(self, *, fail_gesture: bool = False) -> None:
        self.fail_gesture = fail_gesture
        self.flicks: list[tuple[tuple, dict]] = []
        self.taps: list[tuple[int, int]] = []
        self.cleanup_events: list[object] = []

    def interp_flick(self, *args, **kwargs) -> None:
        self.flicks.append((args, kwargs))
        if self.fail_gesture:
            raise PhoneError("synthetic gesture failure")

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def clear_app_memory(self, bundle_id: str) -> str:
        self.cleanup_events.append(("clear", bundle_id))
        return "force-close"

    def open_app_switcher(self) -> str:
        self.cleanup_events.append("switcher")
        return "double-home-nogap"

    def force_close_app(self, bundle_id: str) -> str:
        self.cleanup_events.append(("force", bundle_id))
        return "force-close"

    def app_is_running(self, bundle_id: str) -> bool:
        self.cleanup_events.append(("running", bundle_id))
        return False

    def press_home(self) -> None:
        self.cleanup_events.append("home")


class DeterministicRng:
    def __init__(self, mode: str, duration: int) -> None:
        self.mode = mode
        self.duration = duration
        self.randint_calls: list[tuple[int, int, int]] = []
        self.uniform_calls: list[tuple[float, float, float]] = []

    def _pick(self, low, high):
        if self.mode == "min":
            return low
        if self.mode == "max":
            return high
        return (low + high) / 2

    def randint(self, low: int, high: int) -> int:
        if (low, high) == (
            create.FEED_BROWSE_MIN_SECONDS,
            create.FEED_BROWSE_MAX_SECONDS,
        ):
            value = self.duration
        else:
            value = int(self._pick(low, high))
        self.randint_calls.append((low, high, value))
        return value

    def uniform(self, low: float, high: float) -> float:
        value = float(self._pick(low, high))
        self.uniform_calls.append((low, high, value))
        return value


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


class FakeCloseStack:
    def __init__(
        self,
        *,
        fail_clear: bool = False,
        close_mode: str = "force-close",
        fail_force: bool = False,
        running_after_force: bool = False,
        running_sequence: list[bool] | None = None,
    ) -> None:
        self.events: list[object] = []
        self.fail_clear = fail_clear
        self.close_mode = close_mode
        self.fail_force = fail_force
        self.running_after_force = running_after_force
        self.running_sequence = list(running_sequence or [])

    def press_home(self) -> None:
        self.events.append("home")

    def open_app_switcher(self) -> str:
        self.events.append("switcher")
        return "double-home-nogap"

    def interp_flick(self, *args, **kwargs) -> None:
        self.events.append(("flick", args, kwargs))

    def clear_app_memory(self, bundle_id: str) -> str:
        self.events.append(("clear", bundle_id))
        if self.fail_clear:
            raise PhoneError("synthetic clear failure")
        return self.close_mode

    def force_close_app(self, bundle_id: str) -> str:
        self.events.append(("force", bundle_id))
        if self.fail_force:
            raise PhoneError("synthetic force-close failure")
        return self.close_mode

    def app_is_running(self, bundle_id: str) -> bool:
        self.events.append(("running", bundle_id))
        if self.running_sequence:
            return self.running_sequence.pop(0)
        return self.running_after_force


def signup_home_recovery_regressions() -> None:
    """Logged-out feed recovery probes account state without guessing."""

    saved = saved_dump_nodes("871w_7")
    you = to_verify.home_feed_you_node(saved)
    check(
        "signup-home-saved-871w7-exact-you",
        you is not None
        and you.center() == (623, 1264)
        and (you.w, you.h) == (40, 15),
        repr(you),
    )
    check(
        "signup-home-feed-is-account-neutral",
        to_verify.post_you_state(saved) == "unknown",
    )

    no_content_ctas = [
        item
        for item in saved
        if collapsed_text(item.labels()) not in ("sign up", "join")
    ]
    check(
        "signup-home-does-not-rely-on-content-ctas",
        to_verify.home_feed_you_node(no_content_ctas) is not None,
    )
    content_you = [
        node("Search Reddit", 260, 115, w=225),
        node("Home", 80, 1265, w=60),
        node("Inbox", 345, 1265, w=60),
        node("You", 590, 760, w=40),
    ]
    check(
        "signup-home-content-you-not-bottom-nav",
        to_verify.home_feed_you_node(content_you) is None,
    )
    duplicate_bottom_you = saved + [node("You", 550, 1265, w=40)]
    check(
        "signup-home-ambiguous-bottom-you-refused",
        to_verify.home_feed_you_node(duplicate_bottom_you) is None,
    )

    provider = [
        node("Log in or sign up", 190, 300, w=370),
        node("Continue with Google", 120, 520, w=510),
        node("Continue with Apple", 120, 600, w=510),
        node("Continue with email", 120, 680, w=510),
    ]
    profile = [
        node("u/AlreadyHere", 230, 160, w=290),
        node("Edit profile", 250, 310, w=250),
        node("Overview", 90, 520, w=130),
        node("Posts", 300, 520, w=90),
    ]
    login_landing = [
        node("Log in to Reddit", 220, 260, w=310),
        node("Create a new account", 180, 980, w=390),
    ]
    check("signup-home-provider-proof", to_verify.post_you_state(provider) == "signup")
    check("signup-home-login-landing-proof", to_verify.post_you_state(login_landing) == "signup")
    check("signup-home-profile-proof", to_verify.post_you_state(profile) == "profile")
    check(
        "signup-home-lone-content-signup-refused",
        to_verify.post_you_state([node("SIGN UP", 430, 1017, w=112)]) == "unknown",
    )
    check(
        "signup-home-lone-email-cta-refused",
        to_verify.post_you_state(
            [node("Continue with email", 180, 1017, w=390)]
        )
        == "unknown",
    )
    check(
        "signup-home-lone-use-email-cta-refused",
        to_verify.post_you_state(
            [node("Use email or username", 180, 1017, w=390)]
        )
        == "unknown",
    )

    original_sleep = to_verify.time.sleep
    original_dump = to_verify.dump_named
    original_log = to_verify.log
    to_verify.time.sleep = lambda _seconds: None
    to_verify.log = lambda _message: None
    try:
        stack = FakeStack()
        home_you_taps = [0]
        result = to_verify.step(
            stack,
            object(),
            saved,
            "fixture@icloud.com",
            [True],
            "a0002",
            [0],
            [0],
            [0],
            [0],
            home_you_taps,
        )
        check(
            "signup-home-one-exact-you-tap",
            result == "YOU_PROBE"
            and you is not None
            and stack.taps == [you.center()]
            and home_you_taps == [1],
            repr(stack.taps),
        )
        expect_phone_error(
            "signup-home-you-never-retapped",
            lambda: to_verify.step(
                stack,
                object(),
                saved,
                "fixture@icloud.com",
                [True],
                "a0002",
                [0],
                [0],
                [0],
                [0],
                home_you_taps,
            ),
            "already tapped once",
        )
        check(
            "signup-home-repeat-refusal-has-no-second-tap",
            you is not None and stack.taps == [you.center()],
            repr(stack.taps),
        )

        dump_calls: list[tuple[tuple, dict]] = []

        def provider_dump(*args, **kwargs):
            dump_calls.append((args, kwargs))
            return "synthetic", provider

        to_verify.dump_named = provider_dump
        next_nodes = to_verify.dump_after_you(stack, object())
        check("signup-home-provider-continues", next_nodes is provider)
        check(
            "signup-home-next-state-one-fresh-dump",
            len(dump_calls) == 1
            and dump_calls[0][0][2] == "872_after_you"
            and dump_calls[0][1].get("shot") is True,
            repr(dump_calls),
        )

        profile_dumps = [0]

        def profile_dump(*_args, **_kwargs):
            profile_dumps[0] += 1
            return "synthetic", profile

        to_verify.dump_named = profile_dump
        taps_before_profile_stop = list(stack.taps)
        expect_phone_error(
            "signup-home-existing-profile-stops",
            lambda: to_verify.dump_after_you(stack, object()),
            "existing account/profile",
        )
        check(
            "signup-home-profile-stop-no-mutation",
            profile_dumps == [1] and stack.taps == taps_before_profile_stop,
            repr(stack.taps),
        )

        unknown_dumps = [0]

        def unknown_dump(*_args, **_kwargs):
            unknown_dumps[0] += 1
            return "synthetic", [node("Unknown profile-like screen", 90, 400, w=480)]

        to_verify.dump_named = unknown_dump
        expect_phone_error(
            "signup-home-unproven-post-you-stops",
            lambda: to_verify.dump_after_you(stack, object()),
            "did not prove",
        )
        check("signup-home-unproven-no-retry", unknown_dumps == [1])
    finally:
        to_verify.time.sleep = original_sleep
        to_verify.dump_named = original_dump
        to_verify.log = original_log


def orchestrator_account_neutral_feed_regressions() -> None:
    """An unfinished credential-less Home feed must use the safe You probe."""

    feed = saved_dump_nodes("871w_7")
    original = {
        "get_slot": create.get_slot,
        "dump_named": create.dump_named,
        "run_to_verify": create.run_to_verify,
        "poll_reddit_code": create.poll_reddit_code,
        "advance_post_interests": create.advance_post_interests,
        "mark_reddit_created": create.mark_reddit_created,
        "close_reddit": create.close_reddit,
        "log": create.log,
    }

    def run_case(username, password):
        events: list[object] = []
        create.get_slot = lambda _sid: {
            "id": "a0002",
            "ghost": True,
            "email": "fixture@icloud.com",
            "username": username,
            "password": password,
            "reddit": False,
        }

        def fake_dump(*args, **_kwargs):
            events.append(("dump", args[2]))
            return "synthetic", feed

        def safe_probe(_stack, _phone, sid, email, **_kwargs):
            events.append(("probe", sid, email))
            raise PhoneError(
                "Bottom-nav You opened an existing account/profile. "
                "Stopped without logging out or changing it."
            )

        def forbidden(label):
            def call(*_args, **_kwargs):
                events.append(label)
                raise AssertionError(label + " must not run")

            return call

        create.dump_named = fake_dump
        create.run_to_verify = safe_probe
        create.poll_reddit_code = forbidden("poll")
        create.advance_post_interests = forbidden("post-interests")
        create.mark_reddit_created = forbidden("mark-created")
        create.close_reddit = forbidden("close")
        create.log = lambda _message: None
        error = ""
        try:
            create.run_slot("a0002", object(), object())
        except Exception as exc:
            error = str(exc)
        return events, error

    try:
        for label, username, password in (
            ("missing-both", None, None),
            ("missing-password", "saved-user", None),
            ("missing-username", None, "saved-password"),
        ):
            events, error = run_case(username, password)
            check(
                "orchestrator-home-safe-probe-" + label,
                events
                == [
                    ("dump", "899_resume_probe"),
                    ("probe", "a0002", "fixture@icloud.com"),
                ]
                and "existing account/profile" in error,
                repr(events) + " :: " + error,
            )
            check(
                "orchestrator-home-no-mark-or-cleanup-" + label,
                "mark-created" not in events
                and "post-interests" not in events
                and "close" not in events
                and "poll" not in events,
                repr(events),
            )
    finally:
        for name, value in original.items():
            setattr(create, name, value)


def birthday_regressions() -> None:
    today = date(2026, 10, 8)
    for value in ("2002-01-01", "2006-12-31", "2004-02-29"):
        parsed = about.parse_reddit_birth_date(value, today=today)
        check("date-valid-" + value, parsed.isoformat() == value)

    bad = (
        (None, "missing"),
        ("", "missing"),
        ("02-01-2002", "format"),
        ("2003-02-29", "format"),
        ("2001-01-01", "between 2002 and 2006"),
        ("2007-01-01", "between 2002 and 2006"),
    )
    for index, (value, reason) in enumerate(bad):
        expect_phone_error(
            f"date-invalid-{index}",
            lambda value=value: about.parse_reddit_birth_date(value, today=today),
            reason,
            str(value or ""),
        )
    expect_phone_error(
        "date-underage",
        lambda: about.parse_reddit_birth_date(
            "2006-12-31", today=date(2023, 1, 1)
        ),
        "18",
        "2006-12-31",
    )
    configured = about.configured_reddit_birth_date(
        secrets={"reddit_birth_date": "2004-02-29"}, today=today
    )
    check("date-config-dict", configured == date(2004, 2, 29))
    expect_phone_error(
        "date-config-missing",
        lambda: about.configured_reddit_birth_date(secrets={}, today=today),
        "missing",
    )

    initial = birthday_nodes(8, 10, 2003)
    check("picker-centered", about.selected_birthday(initial) == (8, 10, 2003))

    live_birthdav = saved_dump_nodes("910_birthday_day_1")
    check(
        "picker-live-birthdav-screen",
        about.birthday_picker(live_birthdav)
        and about.birthday_screen(live_birthdav)
        and about.selected_birthday(live_birthdav) == (23, 7, 2003),
    )
    unrelated_birthdav = [
        node("Birthdav", 30, 180, w=150),
        node("23 July 2003", 80, 410, w=240),
    ]
    check(
        "picker-birthdav-rejects-unrelated-screen",
        not about.birthday_picker(unrelated_birthdav),
    )

    may_dumps = [
        saved_dump_nodes(stem)
        for stem in (
            "910_birthday_month_2",
            "910_birthday_month_2_retry_1",
            "910_birthday_month_2_retry_2",
        )
    ]
    check(
        "picker-cyrillic-may-saved-dump",
        about.selected_birthday(may_dumps[0]) == (8, 5, 2002),
    )
    check(
        "picker-cyrillic-may-retries-stable",
        all(about.selected_birthday(nodes) == (8, 5, 2002) for nodes in may_dumps),
    )
    check(
        "picker-cyrillic-may-axis-row",
        any(value == 5 and item.text == "\u041c\u0430\u0443" for value, item in about._axis_nodes(may_dumps[0], "month")),
    )
    target_may = about._visible_target_node(may_dumps[0], "month", 5)
    check(
        "picker-cyrillic-may-visible-target",
        target_may is not None and target_may.text == "\u041c\u0430\u0443",
    )
    check(
        "picker-ascii-may-still-supported",
        about.selected_birthday(birthday_nodes(8, 5, 2002)) == (8, 5, 2002),
    )

    may_base = birthday_nodes(8, 5, 2002)
    without_centered_may = [item for item in may_base if item.text != "May"]
    check(
        "picker-cyrillic-may-outside-column-rejected",
        about.selected_birthday(without_centered_may + [node("\u041c\u0430\u0443", 510, 675)]) is None,
    )
    check(
        "picker-cyrillic-may-off-center-rejected",
        about.selected_birthday(without_centered_may + [node("\u041c\u0430\u0443", 285, 730)]) is None,
    )
    check(
        "picker-unrelated-cyrillic-rejected",
        about.selected_birthday(without_centered_may + [node("\u043c\u0438\u0440", 285, 675)]) is None,
    )

    off_center = [
        item
        for item in initial
        if not (item.text == "October" and item.center()[1] == 675)
    ]
    check("picker-no-adjacent-inference", about.selected_birthday(off_center) is None)

    # Frozen copy of the a0119 crash frame: the centered day row is absent
    # because OCR drops the bold single-digit selection, and the day wheel
    # cycles 1..31 in every month (31 sits directly above 1 in February).
    missing_center_day = fixture_nodes("a0119_birthday_missing_centered_day")
    check(
        "picker-saved-dump-missing-centered-day-recovered",
        about.selected_birthday(missing_center_day) == (1, 2, 2005),
    )
    without_summary = [
        item for item in missing_center_day if item.text != "1 February 2005"
    ]
    check(
        "picker-missing-centered-day-without-summary-rejected",
        about.selected_birthday(without_summary) is None,
    )
    # The month and year wheel rows are the independent wheel evidence, so a
    # disagreeing month or year must still reject the summary. The day itself
    # has no wheel evidence while OCR hides the centered row; the exact-target
    # equality in select_configured_birthday still gates the Continue tap.
    wrong_month = [
        node("4 December 2005", 185, 426, w=259, h=27)
        if item.text == "1 February 2005" else item
        for item in missing_center_day
    ]
    check(
        "picker-missing-centered-day-disagreeing-month-rejected",
        about.selected_birthday(wrong_month) is None,
    )
    wrong_year = [
        node("1 February 2004", 185, 426, w=259, h=27)
        if item.text == "1 February 2005" else item
        for item in missing_center_day
    ]
    check(
        "picker-missing-centered-day-disagreeing-year-rejected",
        about.selected_birthday(wrong_year) is None,
    )
    without_continue = [item for item in missing_center_day if item.text != "Continue"]
    check(
        "picker-missing-centered-day-unbounded-screen-rejected",
        about.selected_birthday(without_continue) is None,
    )
    target = date(2004, 2, 29)
    check("distance-year", about._axis_distance("year", 1999, 2004, target) == 5)
    check("distance-month-wrap-forward", about._axis_distance("month", 12, 2, target) == 2)
    check("distance-month-wrap-back", about._axis_distance("month", 2, 12, date(2004, 12, 1)) == -2)
    check("distance-day-wrap", about._axis_distance("day", 28, 2, target) == 3)
    expect_phone_error(
        "distance-day-range-change",
        lambda: about._axis_distance("day", 31, 29, target),
        "invalid centered value",
    )

    # An exact visible row is tapped; no gesture is used.
    exact_stack = FakeStack()
    after_year = birthday_nodes(8, 10, 2004)
    original_dump = about.stable_birthday_dump
    about.stable_birthday_dump = lambda *args, **kwargs: after_year
    try:
        result = about._set_birthday_axis(
            exact_stack, object(), initial, "year", 2004, target
        )
    finally:
        about.stable_birthday_dump = original_dump
    check("picker-visible-row-tap", len(exact_stack.taps) == 1 and not exact_stack.flicks)
    check("picker-visible-row-result", about.selected_birthday(result)[2] == 2004)

    # Two unchanged verified results terminate instead of adding more gestures.
    stuck_stack = FakeStack()
    about.stable_birthday_dump = lambda *args, **kwargs: initial
    try:
        expect_phone_error(
            "picker-unchanged-bound",
            lambda: about._set_birthday_axis(
                stuck_stack, object(), initial, "year", 2006, date(2006, 10, 8)
            ),
            "two verified attempts",
        )
    finally:
        about.stable_birthday_dump = original_dump
    check("picker-unchanged-two-gestures", len(stuck_stack.flicks) == 2)

    # A near day target requests a one-row gesture rather than a three-row drag.
    day_target = date(2004, 2, 17)
    day_near = birthday_nodes_with_day_window(
        14, 2, 2004, [11, 12, 13, 14, 15, 16]
    )
    day_exact = birthday_nodes_with_day_window(
        17, 2, 2004, [14, 15, 16, 17, 18, 19, 20]
    )
    approach_stack = FakeStack()
    about.stable_birthday_dump = lambda *args, **kwargs: day_exact
    try:
        result = about._set_birthday_axis(
            approach_stack,
            object(),
            day_near,
            "day",
            17,
            day_target,
            locked_axes={"year": 2004, "month": 2},
        )
    finally:
        about.stable_birthday_dump = original_dump
    approach_drag = abs(
        approach_stack.flicks[0][0][1] - approach_stack.flicks[0][0][3]
    )
    check("picker-day-near-uses-one-row", approach_drag <= 80)
    check("picker-day-near-reaches-target", about.selected_birthday(result)[0] == 17)

    day_far = birthday_nodes_with_day_window(
        13, 2, 2004, [10, 11, 12, 13, 14, 15, 16]
    )
    far_stack = FakeStack()
    about.stable_birthday_dump = lambda *args, **kwargs: day_exact
    try:
        about._set_birthday_axis(
            far_stack,
            object(),
            day_far,
            "day",
            17,
            day_target,
            locked_axes={"year": 2004, "month": 2},
        )
    finally:
        about.stable_birthday_dump = original_dump
    far_drag = abs(far_stack.flicks[0][0][1] - far_stack.flicks[0][0][3])
    check("picker-day-distance-four-keeps-normal-gesture", far_drag > 100)

    observed_day_before = day_near
    observed_day_after = birthday_nodes_with_day_window(
        21, 2, 2004, [18, 19, 20, 21, 22, 23, 24]
    )
    observed_before_values = about.selected_birthday(observed_day_before)
    observed_after_values = about.selected_birthday(observed_day_after)
    check(
        "picker-day-overshoot-progression-classified",
        observed_before_values is not None
        and observed_after_values is not None
        and observed_before_values[0] == 14
        and observed_after_values[0] == 21
        and observed_before_values[1:] == observed_after_values[1:],
    )
    if observed_after_values is None:
        check("picker-day-target-one-row-outside", False)
    else:
        observed_target_date = date(
            observed_after_values[2], observed_after_values[1], 17
        )
        check(
            "picker-day-target-one-row-outside",
            about._day_target_just_outside_visible_window(
                observed_day_after,
                17,
                observed_target_date,
                about._axis_distance(
                    "day", observed_after_values[0], 17, observed_target_date
                ),
            ),
        )

    # A target exposed by the first overshoot is exact-tapped on the next loop.
    visible_overshoot = birthday_nodes_with_day_window(
        18, 2, 2004, [15, 16, 17, 18, 19, 20, 21]
    )
    visible_sequence = iter((visible_overshoot, day_exact))
    visible_stack = FakeStack()
    about.stable_birthday_dump = lambda *args, **kwargs: next(visible_sequence)
    try:
        result = about._set_birthday_axis(
            visible_stack,
            object(),
            day_near,
            "day",
            17,
            day_target,
            locked_axes={"year": 2004, "month": 2},
        )
    finally:
        about.stable_birthday_dump = original_dump
    check(
        "picker-day-visible-after-overshoot",
        len(visible_stack.flicks) == 1
        and len(visible_stack.taps) == 1
        and about.selected_birthday(result)[0] == 17,
    )

    # Reproduce the observed 14 -> 21 jump: 17 is one row above the fresh edge.
    far_overshoot = birthday_nodes_with_day_window(
        21, 2, 2004, [18, 19, 20, 21, 22, 23, 24]
    )
    recovery_visible = birthday_nodes_with_day_window(
        18, 2, 2004, [17, 18, 19, 20, 21, 22, 23]
    )
    recovery_sequence = iter((far_overshoot, recovery_visible, day_exact))
    recovery_stack = FakeStack()
    about.stable_birthday_dump = lambda *args, **kwargs: next(recovery_sequence)
    try:
        result = about._set_birthday_axis(
            recovery_stack,
            object(),
            day_near,
            "day",
            17,
            day_target,
            locked_axes={"year": 2004, "month": 2},
        )
    finally:
        about.stable_birthday_dump = original_dump
    recovery_drags = [
        abs(args[1] - args[3]) for args, _kwargs in recovery_stack.flicks
    ]
    check(
        "picker-day-one-bounded-reversal",
        len(recovery_stack.flicks) == 2
        and len(recovery_stack.taps) == 1
        and all(distance <= 80 for distance in recovery_drags)
        and about.selected_birthday(result)[0] == 17,
    )

    # The saved resume state can cross in the opposite direction from four rows away.
    reverse_recovery_visible = birthday_nodes_with_day_window(
        16, 2, 2004, [13, 14, 15, 16, 17, 18, 19]
    )
    reverse_sequence = iter((day_near, reverse_recovery_visible, day_exact))
    reverse_stack = FakeStack()
    about.stable_birthday_dump = lambda *args, **kwargs: next(reverse_sequence)
    try:
        result = about._set_birthday_axis(
            reverse_stack,
            object(),
            far_overshoot,
            "day",
            17,
            day_target,
            locked_axes={"year": 2004, "month": 2},
        )
    finally:
        about.stable_birthday_dump = original_dump
    check(
        "picker-day-normal-gesture-crossing-recovery",
        len(reverse_stack.flicks) == 2
        and len(reverse_stack.taps) == 1
        and about.selected_birthday(result)[0] == 17,
    )

    farther_with_target_visible = birthday_nodes_with_day_window(
        22, 2, 2004, [17, 18, 19, 20, 21, 22, 23]
    )
    nonreducing_sequence = iter((far_overshoot, farther_with_target_visible))
    nonreducing_stack = FakeStack()
    about.stable_birthday_dump = lambda *args, **kwargs: next(
        nonreducing_sequence
    )
    try:
        expect_phone_error(
            "picker-day-reversal-must-reduce-distance",
            lambda: about._set_birthday_axis(
                nonreducing_stack,
                object(),
                day_near,
                "day",
                17,
                day_target,
                locked_axes={"year": 2004, "month": 2},
            ),
            "did not reduce",
        )
    finally:
        about.stable_birthday_dump = original_dump
    check(
        "picker-day-nonreducing-reversal-does-not-tap",
        len(nonreducing_stack.flicks) == 2 and not nonreducing_stack.taps,
    )

    # A reversal that crosses back over the target is a terminal second overshoot.
    second_cross_sequence = iter((far_overshoot, day_near))
    second_cross_stack = FakeStack()
    about.stable_birthday_dump = lambda *args, **kwargs: next(
        second_cross_sequence
    )
    try:
        expect_phone_error(
            "picker-day-second-overshoot-stops",
            lambda: about._set_birthday_axis(
                second_cross_stack,
                object(),
                day_near,
                "day",
                17,
                day_target,
                locked_axes={"year": 2004, "month": 2},
            ),
            "crossed the configured value again",
        )
    finally:
        about.stable_birthday_dump = original_dump
    check("picker-day-only-one-reversal", len(second_cross_stack.flicks) == 2)

    # Only the contiguous wheel run around the center can prove an outside edge.
    nonadjacent_window = birthday_nodes_with_day_window(
        21, 2, 2004, [19, 20, 21, 22, 23, 24]
    ) + [node("18", 90, 200, w=60)]
    check(
        "picker-day-unrelated-number-cannot-prove-edge",
        not about._day_target_just_outside_visible_window(
            nonadjacent_window,
            17,
            day_target,
            about._axis_distance("day", 21, 17, day_target),
        ),
    )

    # A wrong-direction movement and a locked-axis mutation remain fatal.
    wrong_start = birthday_nodes_with_day_window(
        8, 2, 2004, [5, 6, 7, 8, 9, 10, 11]
    )
    wrong_fresh = birthday_nodes_with_day_window(
        5, 2, 2004, [2, 3, 4, 5, 6, 7, 8]
    )
    about.stable_birthday_dump = lambda *args, **kwargs: wrong_fresh
    try:
        expect_phone_error(
            "picker-day-wrong-direction-stops",
            lambda: about._set_birthday_axis(
                FakeStack(),
                object(),
                wrong_start,
                "day",
                17,
                day_target,
                locked_axes={"year": 2004, "month": 2},
            ),
            "moved away",
        )
    finally:
        about.stable_birthday_dump = original_dump

    # Year crossing behavior remains the original distance-based progression.
    year_start = birthday_nodes(17, 2, 2000)
    year_crossed = [
        item
        for item in birthday_nodes(17, 2, 2005)
        if not (
            item.center()[0] >= 480
            and item.text.isdigit()
            and len(item.text) == 4
        )
    ]
    year_crossed.extend(
        node(str(value), 545, center_y, w=100)
        for value, center_y in zip(
            (2002, 2003, 2005, 2006, 2007),
            (575, 625, 675, 725, 775),
        )
    )
    year_exact = birthday_nodes(17, 2, 2004)
    year_sequence = iter((year_crossed, year_exact))
    year_stack = FakeStack()
    about.stable_birthday_dump = lambda *args, **kwargs: next(year_sequence)
    try:
        result = about._set_birthday_axis(
            year_stack,
            object(),
            year_start,
            "year",
            2004,
            day_target,
        )
    finally:
        about.stable_birthday_dump = original_dump
    check(
        "picker-year-crossed-closer-remains-supported",
        len(year_stack.flicks) == 2
        and not year_stack.taps
        and about.selected_birthday(result)[2] == 2004,
    )

    changed_month = birthday_nodes_with_day_window(
        15, 3, 2004, [12, 13, 14, 15, 16]
    )
    about.stable_birthday_dump = lambda *args, **kwargs: changed_month
    try:
        expect_phone_error(
            "picker-day-locked-month-stops",
            lambda: about._set_birthday_axis(
                FakeStack(),
                object(),
                day_near,
                "day",
                17,
                day_target,
                locked_axes={"year": 2004, "month": 2},
            ),
            "month changed",
        )
    finally:
        about.stable_birthday_dump = original_dump

    # The public selector invokes axes in the required year -> month -> day order.
    target_nodes = birthday_nodes(29, 2, 2004)
    calls: list[tuple[str, int, dict[str, int]]] = []
    original_set = about._set_birthday_axis
    about.stable_birthday_dump = lambda *args, **kwargs: target_nodes

    def fake_set(stack, phone, nodes, axis, value, target_date, *, locked_axes=None):
        calls.append((axis, value, dict(locked_axes or {})))
        return target_nodes

    about._set_birthday_axis = fake_set
    try:
        selected = about.select_configured_birthday(
            FakeStack(), object(), initial, date(2004, 2, 29)
        )
    finally:
        about._set_birthday_axis = original_set
        about.stable_birthday_dump = original_dump
    check(
        "picker-axis-order",
        calls
        == [
            ("year", 2004, {}),
            ("month", 2, {"year": 2004}),
            ("day", 29, {"year": 2004, "month": 2}),
        ],
    )
    check("picker-final-exact", about.selected_birthday(selected) == (29, 2, 2004))

    confirmation = [
        node("About you", 30, 100, w=180),
        node("Birthday", 30, 180, w=150),
        node("Confirm your birthday?", 80, 300, w=420),
        node("29 February 2004", 150, 480, w=350),
        node("Yes, Confirm", 100, 800, w=220),
        node("Go Back", 420, 800, w=170),
    ]
    check(
        "confirmation-extract-exact",
        about.confirmation_birthday(confirmation) == date(2004, 2, 29),
    )
    check(
        "confirmation-match-exact",
        about.birthday_confirmation_matches(confirmation, date(2004, 2, 29)),
    )
    live_confirmation_ocr = [
        node("About vou", 30, 100, w=180),
        node("Birthdav", 30, 180, w=150),
        node("Confirm your birthday?", 80, 300, w=420),
        node("22 July 2005", 150, 480, w=350),
        node("Yes, Confirm", 100, 800, w=220),
        node("Go Back", 420, 800, w=170),
    ]
    check(
        "confirmation-live-birthdav-ocr",
        about.birthday_confirmation_screen(live_confirmation_ocr)
        and about.birthday_confirmation_matches(
            live_confirmation_ocr, date(2005, 7, 22)
        ),
    )
    check(
        "confirmation-mismatch",
        not about.birthday_confirmation_matches(confirmation, date(2004, 2, 28)),
    )
    ambiguous = confirmation + [node("28 February 2004", 150, 550, w=350)]
    check("confirmation-ambiguous", about.confirmation_birthday(ambiguous) is None)
    no_tap = FakeStack()
    expect_phone_error(
        "confirmation-refuses-wrong-date",
        lambda: about._finish_birthday_confirmation(
            no_tap, object(), confirmation, date(2004, 2, 28)
        ),
        "does not exactly match",
    )
    check("confirmation-no-tap-on-mismatch", no_tap.taps == [])


def crane_scroll_regressions() -> None:
    original_dump = open_reddit._transient_dump
    original_sleep = open_reddit.time.sleep

    def run_locate(
        initial,
        target: str,
        responses=(),
        *,
        newest_bound: bool = False,
        allow_complete_visible_prefix: bool = False,
    ):
        stack = FakeStack()
        logs: list[str] = []
        remaining = iter(responses)

        def fake_dump(*_args, **_kwargs):
            return "synthetic", next(remaining)

        open_reddit._transient_dump = fake_dump
        open_reddit.time.sleep = lambda _seconds: None
        try:
            found = open_reddit._locate_container_in_list(
                stack,
                object(),
                logs.append,
                target,
                initial,
                newest_bound=newest_bound,
                allow_complete_visible_prefix=allow_complete_visible_prefix,
            )
            return stack, logs, found, ""
        except Exception as exc:
            return stack, logs, None, str(exc)
        finally:
            open_reddit._transient_dump = original_dump
            open_reddit.time.sleep = original_sleep

    direct = crane_list_nodes("a0021", "a0022", "a0023")
    stack, _logs, found, error = run_locate(direct, "a0023")
    check(
        "crane-bottom-direct-visible-no-scroll",
        not error and found is not None and not stack.flicks,
        error,
    )

    # Immutable fixture from Lily's live 870_now frame.  Hide My Email stores
    # prior aliases under exact aNNNN labels, but it is an Apple Settings list,
    # never an already-open Crane container list.
    hide_my_email_live = [
        node("< iCloud", 3, 106, w=152, h=29),
        node("Q Search", 38, 176, w=163, h=32),
        node("Hide My Email", 250, 110, w=240, h=25),
        node("+ Create new address", 68, 442, w=366, h=25),
        node("a0001", 50, 614, w=112, h=25),
        node("From Settings • roofers_critter6v@icloud.com", 59, 647, w=518),
        node("a0002", 56, 909, w=110, h=25),
        node("From Settings • firer-stadia5c@icloud.com", 60, 942, w=481),
        node("a0003", 49, 1205, w=116, h=29),
    ]
    check(
        "crane-live-hide-my-email-slot-labels-are-not-a-container-list",
        not open_reddit.crane_container_list(hide_my_email_live),
    )
    check(
        "crane-live-hide-my-email-cannot-continue-bound-scroll",
        not open_reddit._container_view_continues(hide_my_email_live),
    )

    compact_prefix = [node("Select Container", 210, 150, w=320, h=28)]
    compact_prefix.extend(
        node(f"a{index:04d}", 220, 250 + index * 75, w=150, h=28)
        for index in range(1, 8)
    )
    compact_older = node("a0008", 220, 850, w=150, h=28)
    compact_newer = node("a0008", 220, 900, w=150, h=28)
    compact_prefix.extend(
        [compact_older, compact_newer, node("a0009", 220, 975, w=150, h=28)]
    )
    stack, logs, found, error = run_locate(
        compact_prefix,
        "a0008",
        newest_bound=True,
        allow_complete_visible_prefix=True,
    )
    check(
        "crane-compact-complete-prefix-selects-bottommost-exact-without-dismiss-swipe",
        not error
        and found is compact_newer
        and stack.flicks == []
        and any("complete visible ID set" in item for item in logs),
        error,
    )

    incomplete_prefix = [
        node("Select Container", 210, 150, w=320, h=28),
        node("a0001", 220, 350, w=150, h=28),
        node("a0008", 220, 800, w=150, h=28),
        node("a0009", 220, 900, w=150, h=28),
    ]
    anchored, anchored_error = open_reddit._bottommost_exact_in_complete_visible_prefix(
        incomplete_prefix, "a0008"
    )
    check(
        "crane-compact-prefix-with-gap-is-not-proof",
        anchored is None and anchored_error == "unproven",
    )

    reordered_complete = [
        node("Select Container", 210, 150, w=320, h=28),
        node("a0001", 220, 300, w=150, h=28),
        node("a0004", 220, 350, w=150, h=28),
        node("a0007", 220, 400, w=150, h=28),
        node("a0002", 420, 300, w=150, h=28),
        node("a0005", 420, 350, w=150, h=28),
        node("a0008", 420, 400, w=150, h=28),
        node("a0003", 620, 300, w=150, h=28),
        node("a0006", 620, 350, w=150, h=28),
        node("a0009", 620, 450, w=150, h=28),
    ]
    anchored, anchored_error = open_reddit._bottommost_exact_in_complete_visible_prefix(
        reordered_complete, "a0008"
    )
    check(
        "crane-compact-complete-id-set-ignores-unstable-ocr-order",
        anchored is reordered_complete[6] and not anchored_error,
    )

    check(
        "crane-persisted-instance-enables-newest-bound-selection",
        open_reddit._bound_container_selection_required(
            "a0002", {"ghost_container_instance": "NEWUUID2"}
        ),
    )
    check(
        "crane-legacy-slot-without-instance-keeps-legacy-selection",
        not open_reddit._bound_container_selection_required("a0002", {"ghost": True}),
    )
    check(
        "crane-recovered-slot-keeps-newest-exact-selection-policy",
        open_reddit._bound_container_selection_required(
            "a0008", {"crane_selection_policy": "newest_exact"}
        ),
    )
    check(
        "crane-migrated-a0001-keeps-special-selection-with-instance",
        not open_reddit._bound_container_selection_required(
            "a0001", {"ghost_container_instance": "NEWUUID1"}
        ),
    )

    # A persisted Ghost instance means an exact visible a0002 is not enough:
    # it may be the older duplicate.  Only after two unchanged post-flick
    # frames prove the bottom may the final exact alpha row be returned.
    bound_initial = [
        node("Select Container", 210, 150, w=320, h=28),
        node("2", 220, 330, w=80, h=28),
        node("a0002", 220, 425, w=150, h=28),  # older duplicate
        node("a0003", 220, 520, w=150, h=28),
    ]
    bound_bottom = [
        node("Select Container", 210, 150, w=320, h=28),
        node("2", 220, 285, w=80, h=28),       # legacy row, never eligible
        node("a0002", 220, 375, w=150, h=28),  # older exact duplicate
        node("a0004", 220, 470, w=150, h=28),
        node("a0002", 220, 630, w=150, h=28),  # newest appended duplicate
    ]
    stack, logs, found, error = run_locate(
        bound_initial,
        "a0002",
        (bound_bottom, bound_bottom, bound_bottom),
        newest_bound=True,
    )
    check(
        "crane-bound-a0002-proves-bottom-then-selects-bottommost-exact",
        not error
        and found is bound_bottom[-1]
        and found.center()[1] == 630
        and len(stack.flicks) == 2
        and any("proven stable Crane bottom" in item for item in logs),
        error,
    )

    bound_route_stack = FakeStack()
    bound_route_logs: list[str] = []
    bound_route_dumps = iter(
        (bound_initial, bound_bottom, bound_bottom, bound_bottom)
    )
    original_get_slot = open_reddit.get_slot
    original_confirm_launch = open_reddit._confirm_container_launch
    open_reddit._transient_dump = lambda *_args, **_kwargs: (
        "synthetic",
        next(bound_route_dumps),
    )
    open_reddit.time.sleep = lambda _seconds: None
    open_reddit.get_slot = lambda _name: {
        "id": "a0002",
        "ghost_container_instance": "NEWUUID2",
    }
    open_reddit._confirm_container_launch = lambda *_args, **_kwargs: None
    bound_route_error = ""
    try:
        open_reddit.open_container(
            bound_route_stack,
            object(),
            bound_route_logs.append,
            "a0002",
            "fixture@icloud.com",
        )
    except Exception as exc:
        bound_route_error = str(exc)
    finally:
        open_reddit._transient_dump = original_dump
        open_reddit.time.sleep = original_sleep
        open_reddit.get_slot = original_get_slot
        open_reddit._confirm_container_launch = original_confirm_launch
    check(
        "crane-persisted-a0002-open-route-taps-only-newest-after-bottom-proof",
        not bound_route_error
        and len(bound_route_stack.flicks) == 2
        and bound_route_stack.taps == [bound_bottom[-1].center()]
        and any("requires newest-exact selection" in item for item in bound_route_logs),
        bound_route_error,
    )

    recovery_calls: list[tuple[bool, bool]] = []
    original_open_container = open_reddit.open_container
    open_reddit.open_container = lambda *_args, **kwargs: recovery_calls.append(
        (
            kwargs.get("require_newest_exact") is True,
            kwargs.get("require_home_menu") is True,
        )
    )
    try:
        open_reddit.recover_existing_container(
            object(), object(), lambda _message: None, "a0008", "fixture@icloud.com"
        )
    finally:
        open_reddit.open_container = original_open_container
    check(
        "crane-recovery-forces-newest-exact-route",
        recovery_calls == [(True, True)],
        repr(recovery_calls),
    )

    def fail_open(*_args, **_kwargs):
        raise PhoneError("stable list bottom could not be proven")

    open_reddit.open_container = fail_open
    try:
        expect_phone_error(
            "crane-recovery-failure-instructs-new-ghost-without-mutation",
            lambda: open_reddit.recover_existing_container(
                object(), object(), lambda _message: None, "a0008", "fixture@icloud.com"
            ),
            "Use New Ghost container",
        )
    finally:
        open_reddit.open_container = original_open_container

    ambiguous_bottom = bound_bottom + [node("a0002", 430, 630, w=150, h=28)]
    stack, _logs, found, error = run_locate(
        bound_initial,
        "a0002",
        (ambiguous_bottom, ambiguous_bottom, ambiguous_bottom),
        newest_bound=True,
    )
    check(
        "crane-bound-a0002-refuses-tied-bottommost-exact-rows",
        found is None
        and "ambiguous bottommost exact a0002" in error
        and len(stack.flicks) == 2,
        error,
    )

    later_slots_bottom = crane_list_nodes("a0004", "a0005", "a0006", "a0007")
    newest_a0002_window = [
        node("Select Container", 210, 150, w=320, h=28),
        node("2", 220, 300, w=80, h=28),
        node("a0002", 220, 390, w=150, h=28),
        node("a0002", 220, 550, w=150, h=28),
        node("a0003", 220, 645, w=150, h=28),
    ]
    stack, logs, found, error = run_locate(
        bound_initial,
        "a0002",
        (
            later_slots_bottom,
            later_slots_bottom,
            later_slots_bottom,
            newest_a0002_window,
        ),
        newest_bound=True,
    )
    check(
        "crane-bound-a0002-scans-back-from-bottom-after-later-slots",
        not error
        and found is newest_a0002_window[-2]
        and found.center()[1] == 550
        and len(stack.flicks) == 4
        and stack.flicks[-1]
        == ((375, 780, 375, 920), {"steps": 18, "ms": 30})
        and any("first bottommost exact row" in item for item in logs),
        error,
    )

    numeric_only_bottom = [
        node("Select Container", 210, 150, w=320, h=28),
        node("2", 220, 420, w=80, h=28),
        node("a0003", 220, 515, w=150, h=28),
        node("a0004", 220, 610, w=150, h=28),
    ]
    stack, _logs, found, error = run_locate(
        bound_initial,
        "a0002",
        (
            numeric_only_bottom,
            numeric_only_bottom,
            numeric_only_bottom,
            numeric_only_bottom,
            numeric_only_bottom,
        ),
        newest_bound=True,
    )
    check(
        "crane-bound-a0002-never-falls-back-to-legacy-bare-two",
        found is None
        and "stable bottom to a stable top without an exact alpha a0002 row" in error
        and len(stack.flicks) == 4,
        error,
    )

    moving_bottoms = tuple(
        [
            node("Select Container", 210, 150, w=320, h=28),
            node("a0002", 220, 310 + step, w=150, h=28),
            node(f"a{10 + step:04d}", 220, 650, w=150, h=28),
        ]
        for step in range(6)
    )
    stack, _logs, found, error = run_locate(
        bound_initial,
        "a0002",
        moving_bottoms,
        newest_bound=True,
    )
    check(
        "crane-bound-a0002-visible-exact-stops-without-stable-bottom",
        found is None
        and "Could not prove a stable Crane list bottom" in error
        and len(stack.flicks) == 6,
        error,
    )

    bound_parent = [
        node("Remove App", 370, 420, w=200, h=28),
        node("a0002", 300, 610, w=150, h=28),
    ]
    bound_parent_stack = FakeStack()
    expect_phone_error(
        "crane-bound-a0002-parent-row-is-not-a-direct-shortcut",
        lambda: open_reddit._tap_requested_container(
            bound_parent_stack,
            object(),
            lambda _message: None,
            "a0002",
            bound_parent,
            newest_bound=True,
        ),
        "requires an explicit Crane list doorway",
    )
    check(
        "crane-bound-a0002-parent-row-remains-untapped",
        bound_parent_stack.taps == [] and bound_parent_stack.flicks == [],
    )

    unproven_bound_parent = [
        node("Container", 347, 665, w=175, h=26),
        node("a0002", 289, 693, w=110, h=22),
        node("a0003", 289, 765, w=110, h=22),
    ]
    unproven_bound_stack = FakeStack()
    expect_phone_error(
        "crane-bound-low-container-with-two-slots-cannot-masquerade-as-list",
        lambda: open_reddit._tap_requested_container(
            unproven_bound_stack,
            object(),
            lambda _message: None,
            "a0002",
            unproven_bound_parent,
            newest_bound=True,
        ),
        "no safe list doorway",
    )
    check(
        "crane-bound-unproven-parent-never-scrolls-or-taps",
        not open_reddit.crane_container_list(unproven_bound_parent)
        and unproven_bound_stack.taps == []
        and unproven_bound_stack.flicks == [],
    )

    migrated_a0001 = [
        node("Container", 237, 96, w=204, h=39),
        node("a0023", 278, 131, w=94, h=23),
        node("Default", 269, 200, w=125, h=26),
        node("1", 278, 272, w=25, h=20),
        node("2", 272, 344, w=34, h=23),
        node("3", 281, 419, w=24, h=20),
        node("1", 278, 490, w=25, h=18),
        node("a0002", 274, 562, w=110, h=26),
        node("a0003", 256, 633, w=128, h=29),
    ]
    migrated_row = open_reddit.find_container_list_row(migrated_a0001, "a0001")
    stack, _logs, found, error = run_locate(migrated_a0001, "a0001")
    check(
        "crane-migrated-a0001-selects-second-bare-one-without-scroll",
        migrated_row is not None
        and migrated_row.center()[1] == 490
        and found is migrated_row
        and not error
        and not stack.flicks,
        error,
    )
    check(
        "crane-migrated-a0001-does-not-steal-exact-a0002",
        open_reddit.find_container_list_row(migrated_a0001, "a0002").center()[1]
        == 562,
    )
    ambiguous_a0001 = [
        item
        for item in migrated_a0001
        if not (item.text == "3" and item.center()[1] == 419)
    ]
    check(
        "crane-migrated-a0001-requires-complete-proven-order",
        open_reddit.find_container_list_row(ambiguous_a0001, "a0001") is None,
    )
    missing_anchor = [item for item in migrated_a0001 if item.text != "a0002"]
    check(
        "crane-migrated-a0001-requires-canonical-a0002-anchor",
        open_reddit.find_container_list_row(missing_anchor, "a0001") is None,
    )

    # The current iOS haptic parent uses singular ``Container`` and renders
    # the active migrated a0001 as a bare ``1``.  The exact system action is
    # required to prove that this is the haptic parent; bare numbers must not
    # become generally selectable.
    singular_parent = [
        node("Remove App", 376, 448, w=204, h=27),
        node("Share App", 359, 521, w=169, h=29),
        node("Edit Home Screen", 415, 590, w=287, h=25),
        node("Container", 347, 665, w=175, h=26),
        node("1", 289, 693, w=22, h=20),
    ]
    parent_kind, parent_node, _parent_slot = open_reddit.crane_parent_entry(
        singular_parent, "a0001"
    )
    check(
        "crane-singular-container-parent-is-safe-doorway",
        parent_node is not None
        and parent_node.text == "Container"
        and parent_kind != "unsafe",
    )
    chevron_parent = [
        node("Remove App", 376, 305, w=203, h=28),
        node("Share App", 357, 375, w=166, h=26),
        node("Edit Home Screen", 417, 446, w=284, h=25),
        node("> Container", 336, 534, w=197, h=43),
        node("a0023", 325, 552, w=94, h=23),
    ]
    chevron_kind, chevron_node, _chevron_slot = open_reddit.crane_parent_entry(
        chevron_parent, "a0001"
    )
    check(
        "crane-chevron-singular-container-parent-is-safe-doorway",
        chevron_node is not None
        and chevron_node.text == "> Container"
        and chevron_kind != "unsafe",
    )
    unproven_singular_parent = [
        item for item in singular_parent if item.text not in open_reddit.HAPTIC_SYSTEM_ACTIONS
    ]
    unsafe_kind, unsafe_node, _unsafe_slot = open_reddit.crane_parent_entry(
        unproven_singular_parent, "a0001"
    )
    check(
        "crane-singular-container-without-system-action-is-rejected",
        unsafe_kind == "unsafe" and unsafe_node is None,
    )

    # A newly provisioned duplicate a0001 is appended next to the previous
    # migrated entry.  Selection must choose the newest/final bare 1 before
    # the exact a0002 anchor, never the first legacy or earlier duplicate.
    newest_duplicate_a0001 = [
        node("Container", 237, 96, w=204, h=39),
        node("a0023", 278, 131, w=94, h=23),
        node("Default", 269, 200, w=125, h=26),
        node("1", 278, 272, w=25, h=20),
        node("2", 272, 344, w=34, h=23),
        node("3", 281, 419, w=24, h=20),
        node("1", 278, 470, w=25, h=18),
        node("1", 278, 520, w=25, h=18),
        node("a0002", 274, 582, w=110, h=26),
        node("a0003", 256, 653, w=128, h=29),
    ]
    newest_row = open_reddit.find_container_list_row(
        newest_duplicate_a0001, "a0001"
    )
    stack, _logs, found, error = run_locate(newest_duplicate_a0001, "a0001")
    check(
        "crane-duplicate-a0001-selects-newest-final-bare-one",
        newest_row is not None
        and newest_row.center()[1] == 520
        and found is newest_row
        and not error
        and not stack.flicks,
        error,
    )
    canonical_duplicate_a0001 = [
        node("Container", 237, 96, w=204, h=39),
        node("a0001", 278, 131, w=94, h=23),  # current header, never selectable
        node("Default", 269, 200, w=125, h=26),
        node("a0001", 274, 330, w=110, h=26),
        node("a0002", 274, 420, w=110, h=26),
        node("a0001", 274, 510, w=110, h=26),  # newest appended duplicate
    ]
    canonical_newest = open_reddit.find_container_list_row(
        canonical_duplicate_a0001, "a0001"
    )
    check(
        "crane-canonical-duplicate-a0001-selects-bottommost-body-row",
        canonical_newest is not None and canonical_newest.center()[1] == 510,
    )

    # Exercise the entire bounded parent -> list -> final row route.  This is
    # exactly two taps and never enters the scrolling code when the newest
    # requested duplicate is already visible.
    route_stack = FakeStack()
    route_logs: list[str] = []
    route_dumps = iter((("synthetic", newest_duplicate_a0001),))
    original_transient = open_reddit._transient_dump
    original_route_sleep = open_reddit.time.sleep
    open_reddit._transient_dump = lambda *_args, **_kwargs: next(route_dumps)
    open_reddit.time.sleep = lambda _seconds: None
    route_error = ""
    try:
        route_result = open_reddit._tap_requested_container(
            route_stack,
            object(),
            route_logs.append,
            "a0001",
            singular_parent,
        )
    except Exception as exc:
        route_result = ""
        route_error = str(exc)
    finally:
        open_reddit._transient_dump = original_transient
        open_reddit.time.sleep = original_route_sleep
    check(
        "crane-singular-parent-to-newest-a0001-is-exactly-two-taps",
        not route_error
        and route_result == "list"
        and route_stack.taps
        == [parent_node.center(), newest_row.center()]
        and route_stack.flicks == [],
        route_error,
    )

    # The live two-level Crane menu keeps iOS system actions visible after
    # opening the actual short container list.  Two distinct prefixed slots
    # prove that changed second level and allow only the requested final row.
    haptic_level2 = [
        node("Remove App", 376, 448, w=204, h=27),
        node("Edit Home Screen", 415, 590, w=287, h=25),
        node("a0001", 278, 690, w=110, h=26),
        node("a0002", 278, 762, w=110, h=26),
    ]
    haptic_level2_target = open_reddit.find_container_list_row(
        haptic_level2, "a0001"
    )
    route_stack = FakeStack()
    route_dumps = iter((("synthetic", haptic_level2),))
    open_reddit._transient_dump = lambda *_args, **_kwargs: next(route_dumps)
    open_reddit.time.sleep = lambda _seconds: None
    route_error = ""
    try:
        route_result = open_reddit._tap_requested_container(
            route_stack,
            object(),
            lambda _message: None,
            "a0001",
            singular_parent,
        )
    except Exception as exc:
        route_result = ""
        route_error = str(exc)
    finally:
        open_reddit._transient_dump = original_transient
        open_reddit.time.sleep = original_route_sleep
    check(
        "crane-haptic-second-level-two-slots-selects-requested-row",
        not route_error
        and route_result == "list"
        and haptic_level2_target is not None
        and route_stack.taps
        == [parent_node.center(), haptic_level2_target.center()]
        and route_stack.flicks == [],
        route_error,
    )

    # The live compact submenu repeats the active slot above ``Default`` and
    # renders the real selectable copy between ``Default`` and
    # ``New Container``.  The bounded body copy must win even for an
    # instance-bound slot; the repeated active label is not a duplicate row.
    compact_haptic_level2 = [
        node("Remove App", 275, 308, w=196, h=25),
        node("Share App", 272, 377, w=165, h=26),
        node("Edit Home Screen", 276, 443, w=271, h=18),
        node("~ Container", 234, 489, w=208, h=54),
        node("a0142", 278, 522, w=90, h=24),
        node("Default", 272, 592, w=124, h=23),
        node("a0142", 275, 664, w=103, h=23),
        node("New Container", 272, 737, w=237, h=25),
    ]
    compact_target = compact_haptic_level2[6]
    route_stack = FakeStack()
    route_logs = []
    route_dumps = iter((("synthetic", compact_haptic_level2),))
    open_reddit._transient_dump = lambda *_args, **_kwargs: next(route_dumps)
    open_reddit.time.sleep = lambda _seconds: None
    route_error = ""
    try:
        route_result = open_reddit._tap_requested_container(
            route_stack,
            object(),
            route_logs.append,
            "a0142",
            singular_parent,
            newest_bound=True,
        )
    except Exception as exc:
        route_result = ""
        route_error = str(exc)
    finally:
        open_reddit._transient_dump = original_transient
        open_reddit.time.sleep = original_route_sleep
    check(
        "crane-instance-bound-compact-haptic-selects-body-copy",
        not route_error
        and route_result == "list"
        and route_stack.taps == [parent_node.center(), compact_target.center()]
        and route_stack.flicks == []
        and any("bounded exact container a0142" in item for item in route_logs),
        route_error,
    )

    # Some Crane builds close the iOS haptic surface after the parent tap and
    # render a short list with a low singular Container label.  The global
    # classifier must remain strict, but this changed second level is proven
    # by multiple prefixed slots and an exact requested body row.
    non_haptic_level2 = [
        node("Container", 347, 360, w=175, h=26),
        node("a0001", 289, 520, w=110, h=22),
        node("a0002", 289, 610, w=110, h=22),
        node("a0003", 289, 700, w=110, h=22),
    ]
    non_haptic_target = open_reddit.find_container_list_row(
        non_haptic_level2, "a0002"
    )
    route_stack = FakeStack()
    route_logs = []
    route_dumps = iter((("synthetic", non_haptic_level2),))
    open_reddit._transient_dump = lambda *_args, **_kwargs: next(route_dumps)
    open_reddit.time.sleep = lambda _seconds: None
    route_error = ""
    try:
        route_result = open_reddit._tap_requested_container(
            route_stack,
            object(),
            route_logs.append,
            "a0002",
            singular_parent,
        )
    except Exception as exc:
        route_result = ""
        route_error = str(exc)
    finally:
        open_reddit._transient_dump = original_transient
        open_reddit.time.sleep = original_route_sleep
    check(
        "crane-non-haptic-second-level-selects-exact-requested-row",
        not route_error
        and route_result == "list"
        and non_haptic_target is not None
        and route_stack.taps
        == [parent_node.center(), non_haptic_target.center()]
        and route_stack.flicks == []
        and any(
            "non-haptic level2 unique exact container a0002" in item
            for item in route_logs
        ),
        route_error,
    )
    check(
        "crane-global-low-container-classifier-remains-strict",
        not open_reddit.crane_container_list(non_haptic_level2),
    )

    missing_target_level2 = [
        node("Container", 347, 360, w=175, h=26),
        node("a0001", 289, 520, w=110, h=22),
        node("a0003", 289, 610, w=110, h=22),
    ]
    route_stack = FakeStack()
    route_dumps = iter((("synthetic", missing_target_level2),))
    open_reddit._transient_dump = lambda *_args, **_kwargs: next(route_dumps)
    open_reddit.time.sleep = lambda _seconds: None
    route_error = ""
    try:
        open_reddit._tap_requested_container(
            route_stack, object(), lambda _message: None, "a0002", singular_parent
        )
    except Exception as exc:
        route_error = str(exc)
    finally:
        open_reddit._transient_dump = original_transient
        open_reddit.time.sleep = original_route_sleep
    check(
        "crane-non-haptic-second-level-missing-target-stops-after-parent",
        "unrecognized second level" in route_error
        and route_stack.taps == [parent_node.center()]
        and route_stack.flicks == [],
        route_error,
    )

    one_slot_level2 = [
        node("Container", 347, 360, w=175, h=26),
        node("a0002", 289, 610, w=110, h=22),
    ]
    route_stack = FakeStack()
    route_dumps = iter((("synthetic", one_slot_level2),))
    open_reddit._transient_dump = lambda *_args, **_kwargs: next(route_dumps)
    open_reddit.time.sleep = lambda _seconds: None
    route_error = ""
    try:
        open_reddit._tap_requested_container(
            route_stack, object(), lambda _message: None, "a0002", singular_parent
        )
    except Exception as exc:
        route_error = str(exc)
    finally:
        open_reddit._transient_dump = original_transient
        open_reddit.time.sleep = original_route_sleep
    check(
        "crane-non-haptic-second-level-one-exact-slot-selects",
        not route_error
        and route_result == "list"
        and route_stack.taps == [parent_node.center(), one_slot_level2[1].center()]
        and route_stack.flicks == [],
        route_error,
    )

    route_stack = FakeStack()
    route_dumps = iter((("synthetic", singular_parent),))
    open_reddit._transient_dump = lambda *_args, **_kwargs: next(route_dumps)
    open_reddit.time.sleep = lambda _seconds: None
    route_error = ""
    try:
        open_reddit._tap_requested_container(
            route_stack, object(), lambda _message: None, "a0002", singular_parent
        )
    except Exception as exc:
        route_error = str(exc)
    finally:
        open_reddit._transient_dump = original_transient
        open_reddit.time.sleep = original_route_sleep
    check(
        "crane-parent-unchanged-screen-still-stops-after-one-tap",
        "left the haptic menu unchanged" in route_error
        and route_stack.taps == [parent_node.center()]
        and route_stack.flicks == [],
        route_error,
    )

    route_stack = FakeStack()
    route_dumps = iter((("synthetic", non_haptic_level2),))
    open_reddit._transient_dump = lambda *_args, **_kwargs: next(route_dumps)
    open_reddit.time.sleep = lambda _seconds: None
    route_error = ""
    try:
        open_reddit._tap_requested_container(
            route_stack,
            object(),
            lambda _message: None,
            "a0002",
            singular_parent,
            newest_bound=True,
        )
    except Exception as exc:
        route_error = str(exc)
    finally:
        open_reddit._transient_dump = original_transient
        open_reddit.time.sleep = original_route_sleep
    check(
        "crane-instance-bound-non-haptic-unique-exact-row-selects",
        not route_error
        and route_result == "list"
        and route_stack.taps == [parent_node.center(), non_haptic_target.center()]
        and route_stack.flicks == [],
        route_error,
    )

    early = crane_list_nodes("a0001", "a0002", "a0003", "a0004")
    bottom = crane_list_nodes("a0020", "a0021", "a0022", "a0023", "a0024")
    stack, logs, found, error = run_locate(early, "a0024", (bottom,))
    check(
        "crane-bottom-newest-one-full-flick",
        not error
        and found is not None
        and len(stack.flicks) == 1
        and stack.flicks[0]
        == ((375, 1120, 375, 260), {"steps": 20, "ms": 18})
        and any("HIT after fast bottom 1" in item for item in logs),
        error,
    )

    middle = crane_list_nodes("a0010", "a0011", "a0012", "a0013")
    stack, _logs, found, error = run_locate(early, "a0024", (middle, bottom))
    check(
        "crane-bottom-newest-two-full-flicks",
        not error
        and found is not None
        and len(stack.flicks) == 2
        and all(
            args == (375, 1120, 375, 260)
            and kwargs == {"steps": 20, "ms": 18}
            for args, kwargs in stack.flicks
        ),
        error,
    )

    disappeared = [node("Reddit", 270, 1015, w=120)]
    stack, _logs, found, error = run_locate(early, "a0024", (disappeared,))
    check(
        "crane-bottom-disappearing-list-stops",
        found is None
        and "disappeared during fast-bottom" in error
        and len(stack.flicks) == 1,
        error,
    )

    target_like_unknown = [node("a0024", 220, 520, w=150, h=28)]
    stack, _logs, found, error = run_locate(early, "a0024", (target_like_unknown,))
    check(
        "crane-bottom-target-text-without-list-proof-stops",
        found is None
        and "disappeared during fast-bottom" in error
        and len(stack.flicks) == 1,
        error,
    )

    haptic_parent = [
        node("a0024", 220, 520, w=150, h=28),
        node("Remove App", 220, 760, w=240, h=28),
    ]
    stack, _logs, found, error = run_locate(early, "a0024", (haptic_parent,))
    check(
        "crane-bottom-haptic-parent-cannot-masquerade-as-list",
        found is None
        and "disappeared during fast-bottom" in error
        and len(stack.flicks) == 1,
        error,
    )

    older_target = crane_list_nodes("a0005", "a0006", "a0007", "a0008")
    stack, logs, found, error = run_locate(
        early,
        "a0007",
        (early, early, older_target),
    )
    check(
        "crane-bottom-older-slot-keeps-full-scan-fallback",
        not error
        and found is not None
        and len(stack.flicks) == 3
        and stack.flicks[:2]
        == [
            ((375, 1120, 375, 260), {"steps": 20, "ms": 18}),
            ((375, 1120, 375, 260), {"steps": 20, "ms": 18}),
        ]
        and stack.flicks[2]
        == ((375, 350, 375, 1050), {"steps": 14, "ms": 22})
        and any("bounded full-scan fallback" in item for item in logs),
        error,
    )

    bare_numbers = [
        node("8", 80, 300),
        node("123", 250, 480),
        node("1112", 430, 680),
    ]
    stack, _logs, found, error = run_locate(bare_numbers, "a0024")
    check(
        "crane-bottom-bare-numbers-never-authorize-scroll",
        found is None and "not a proven Crane" in error and not stack.flicks,
        error,
    )


def legacy_feed_browse_regressions() -> None:
    feed = feed_frame()
    springboard = feed + [node("Reddit", 300, 1015, w=120)]
    in_app_title = [node("Reddit", 25, 100, w=160)]
    check("feed-state", create.feed_navigation_state(feed) == "feed")
    check("feed-springboard-wins", create.feed_navigation_state(springboard) == "springboard")
    check("feed-title-not-home-icon", create.feed_navigation_state(in_app_title) == "unknown")
    run_source = inspect.getsource(create.run_slot)
    mark_at = run_source.find("mark_reddit_created(")
    browse_at = run_source.find("browse_created_feed(")
    close_at = run_source.rfind("close_reddit(")
    check(
        "feed-created-before-browse-before-close",
        0 <= mark_at < browse_at < close_at,
    )
    check(
        "feed-proof-passed-to-close",
        "feed_proof = browse_created_feed(" in run_source
        and "feed_proof=feed_proof" in run_source,
    )
    check("feed-no-day1-call", "run_day1_session" not in run_source)

    original_dump = create.dump_named
    original_log = create.log
    def run_browse_case(
        after_factory,
        *,
        rng: DeterministicRng,
        initial_nodes=feed,
        fail_gesture: bool = False,
        dump_elapsed: float = 0.0,
    ):
        stack = FakeStack(fail_gesture=fail_gesture)
        clock = FakeClock()
        case_logs: list[str] = []
        case_dumps: list[tuple[tuple, dict]] = []
        after_index = 0

        def fake_dump(*args, **kwargs):
            nonlocal after_index
            case_dumps.append((args, kwargs))
            if args[2] == "928_feed_browse_start":
                return "", initial_nodes
            nodes = after_factory(after_index)
            after_index += 1
            clock.advance(dump_elapsed)
            return "", nodes

        create.dump_named = fake_dump
        create.log = case_logs.append
        result = None
        error = ""
        try:
            result = create.browse_created_feed(
                stack,
                object(),
                rng=rng,
                clock=clock,
                sleeper=clock.sleep,
            )
        except PhoneError as exc:
            error = str(exc)
        finally:
            create.dump_named = original_dump
            create.log = original_log
        return stack, result, error, case_dumps, case_logs, clock

    for mode, duration in (("min", 15), ("middle", 30), ("max", 45)):
        rng = DeterministicRng(mode, duration)
        stack, proof, error, dumps, logs, clock = run_browse_case(
            lambda _index: feed_frame(),
            rng=rng,
        )
        check(f"feed-{mode}-duration-selected", rng.randint_calls[0][2] == duration)
        check(f"feed-{mode}-browse-succeeds", not error and isinstance(proof, open_reddit.FeedCleanupProof))
        check(
            f"feed-{mode}-gesture-bounds",
            bool(stack.flicks)
            and all(
                create.FEED_BROWSE_X_MIN <= args[0] <= create.FEED_BROWSE_X_MAX
                and args[0] == args[2]
                and create.FEED_BROWSE_START_Y_MIN <= args[1] <= create.FEED_BROWSE_START_Y_MAX
                and create.FEED_BROWSE_DISTANCE_MIN <= args[1] - args[3] <= create.FEED_BROWSE_DISTANCE_MAX
                and args[3] < args[1]
                and create.FEED_BROWSE_STEPS_MIN <= kwargs["steps"] <= create.FEED_BROWSE_STEPS_MAX
                and create.FEED_BROWSE_MS_MIN <= kwargs["ms"] <= create.FEED_BROWSE_MS_MAX
                for args, kwargs in stack.flicks
            ),
        )
        check(f"feed-{mode}-no-taps", stack.taps == [])
        check(
            f"feed-{mode}-pause-bounds-and-deadline",
            bool(clock.sleeps)
            and all(0 < pause <= create.FEED_BROWSE_PAUSE_MAX for pause in clock.sleeps)
            and abs(clock.now - duration) < 0.000001,
        )
        check(
            f"feed-{mode}-transient-dumps",
            len(dumps) == len(stack.flicks) + 1
            and all(
                kwargs == {"shot": False, "persist": False, "preview": False}
                for _args, kwargs in dumps
            ),
        )
        check(
            f"feed-{mode}-completion-log",
            any("browsing complete" in item.casefold() for item in logs),
        )

    chain_rng = DeterministicRng("min", 15)
    chain_stack, chain_proof, chain_error, chain_dumps, _logs, _clock = run_browse_case(
        lambda index: feed_frame(offset=-70 * (index + 1), navigation=False),
        rng=chain_rng,
        dump_elapsed=8.0,
    )
    check(
        "feed-proof-chain-two-hidden-navigation-frames",
        not chain_error
        and isinstance(chain_proof, open_reddit.FeedCleanupProof)
        and len(chain_stack.flicks) == 2
        and not create.proven_home_feed(chain_proof.reference_nodes),
    )
    check("feed-proof-chain-transient", len(chain_dumps) == 3)

    failed_rng = DeterministicRng("min", 15)
    failed_stack, failed_proof, failed_error, _dumps, failed_logs, _clock = run_browse_case(
        lambda _index: feed_frame(),
        rng=failed_rng,
        fail_gesture=True,
        dump_elapsed=15.0,
    )
    check(
        "feed-failed-gesture-safe-noop",
        not failed_error
        and isinstance(failed_proof, open_reddit.FeedCleanupProof)
        and len(failed_stack.flicks) == 1
        and any("flick failed" in item.casefold() for item in failed_logs),
    )

    fresh_strict = [
        node(f"Entirely new strict feed anchor {index}", 45, 300 + index * 150, w=500)
        for index in range(4)
    ] + feed_nodes()
    reset_rng = DeterministicRng("min", 15)
    reset_stack, reset_proof, reset_error, reset_dumps, reset_logs, _clock = run_browse_case(
        lambda _index: fresh_strict,
        rng=reset_rng,
    )
    check(
        "feed-new-strict-frame-ends-browse-and-cleans",
        not reset_error
        and isinstance(reset_proof, open_reddit.FeedCleanupProof)
        and len(reset_stack.flicks) == 1
        and len(reset_dumps) == 2
        and any("independently re-proven" in item for item in reset_logs),
        reset_error,
    )

    terminal_structural = [
        node("u/example-account", 45, 320, w=260),
        node("Entirely new promoted card title", 45, 500, w=500),
        node("Another unique promoted detail", 45, 690, w=500),
    ]
    terminal_rng = DeterministicRng("min", 15)
    terminal_stack, terminal_proof, terminal_error, terminal_dumps, terminal_logs, _clock = run_browse_case(
        lambda _index: terminal_structural,
        rng=terminal_rng,
    )
    check(
        "feed-structural-video-frame-ends-browse-and-cleans",
        not terminal_error
        and isinstance(terminal_proof, open_reddit.FeedCleanupProof)
        and terminal_proof.sparse_continuity
        and len(terminal_stack.flicks) == 1
        and len(terminal_dumps) == 2
        and any("without another gesture" in item for item in terminal_logs),
        terminal_error,
    )

    one_anchor_structural = [node("u/example-account", 45, 320, w=260)]
    one_anchor_rng = DeterministicRng("min", 15)
    one_anchor_stack, _proof, one_anchor_error, _dumps, _logs, _clock = run_browse_case(
        lambda _index: one_anchor_structural,
        rng=one_anchor_rng,
    )
    check(
        "feed-terminal-structural-one-anchor-still-refused",
        "lost its verified" in one_anchor_error.casefold()
        and len(one_anchor_stack.flicks) == 1,
        one_anchor_error,
    )

    inconsistent = [
        node(
            f"Distinct feed content anchor {index}",
            40,
            300 + index * 150 - shift,
            w=500,
        )
        for index, shift in enumerate((40, 80, 120, 160))
    ]
    unsafe_cases = (
        ("springboard", springboard),
        ("protected", feed_frame(navigation=False) + [node("Verify your email", 80, 240, w=400)]),
        ("inconsistent", inconsistent),
    )
    for label, unsafe_nodes in unsafe_cases:
        unsafe_rng = DeterministicRng("min", 15)
        unsafe_stack, _proof, error, dumps, _logs, _clock = run_browse_case(
            lambda _index, unsafe_nodes=unsafe_nodes: unsafe_nodes,
            rng=unsafe_rng,
        )
        check("feed-unsafe-" + label + "-stops", "lost its verified" in error.casefold())
        check(
            "feed-unsafe-" + label + "-one-flick-no-tap",
            len(unsafe_stack.flicks) == 1
            and unsafe_stack.taps == []
            and len(dumps) == 2,
        )

    before_saved = fixture_nodes("feed_925_before")
    after_saved = fixture_nodes("feed_939_after")
    check("feed-fixture-before-proven", create.proven_home_feed(before_saved))
    check(
        "feed-fixture-after-bottom-nav-hidden",
        create.feed_navigation_state(after_saved) == "unknown",
    )
    initial_proof = open_reddit.make_feed_cleanup_proof(before_saved)
    advanced_proof = (
        open_reddit.advance_feed_cleanup_proof(initial_proof, after_saved)
        if initial_proof is not None
        else None
    )
    check(
        "feed-fixture-upward-content-shift",
        open_reddit.proven_scrolled_feed_transition(before_saved, after_saved),
    )
    check(
        "feed-fixture-proof-advances",
        isinstance(initial_proof, open_reddit.FeedCleanupProof)
        and isinstance(advanced_proof, open_reddit.FeedCleanupProof),
    )
    home_ocr = next(
        (
            item
            for item in before_saved
            if item.ntype == "ocr" and item.text.casefold() == "home"
        ),
        None,
    )
    check(
        "feed-fixture-production-parser-centers-and-lines",
        home_ocr is not None
        and home_ocr.center() == (125, 1265)
        and any(item.ntype == "ocr-line" for item in before_saved),
    )

    # The a0022 start frame is a real strict Reddit feed, but OCR exposed only
    # two unique long content anchors.  Sparse continuity must remain a
    # proof-chain exception, never a general relaxation of feed recognition.
    sparse_saved = fixture_nodes("feed_sparse_925_before")
    sparse_saved_anchors = open_reddit._feed_content_positions(sparse_saved)
    check("feed-sparse-fixture-strict-feed", create.proven_home_feed(sparse_saved))
    check(
        "feed-sparse-fixture-exactly-two-long-anchors",
        len(sparse_saved_anchors) == 2,
        ",".join(sorted(sparse_saved_anchors)),
    )
    sparse_saved_after: list[Node] = []
    for item in sparse_saved:
        if item.ntype != "ocr":
            continue
        text = item.text.casefold()
        if text not in {"r/croatia 2d", "where's pavlek?", "search reddit"}:
            continue
        sparse_saved_after.append(
            Node(
                ntype=item.ntype,
                text=item.text,
                x=item.x,
                y=item.y - (110 if text != "search reddit" else 0),
                w=item.w,
                h=item.h,
                visible=item.visible,
                enabled=item.enabled,
            )
        )
    sparse_saved_proof = open_reddit.make_feed_cleanup_proof(sparse_saved)
    sparse_saved_advanced = (
        open_reddit.advance_feed_cleanup_proof(
            sparse_saved_proof,
            sparse_saved_after,
        )
        if sparse_saved_proof is not None
        else None
    )
    check(
        "feed-sparse-real-fixture-two-anchor-transition",
        isinstance(sparse_saved_proof, open_reddit.FeedCleanupProof)
        and sparse_saved_proof.sparse_continuity
        and isinstance(sparse_saved_advanced, open_reddit.FeedCleanupProof),
    )

    sparse_before = sparse_feed_frame()
    sparse_proof = open_reddit.make_feed_cleanup_proof(sparse_before)
    sparse_shifted = sparse_feed_frame(offset=-110, navigation=False)
    sparse_stable = sparse_feed_frame(navigation=False)
    sparse_shifted_proof = (
        open_reddit.advance_feed_cleanup_proof(sparse_proof, sparse_shifted)
        if sparse_proof is not None
        else None
    )
    sparse_stable_proof = (
        open_reddit.advance_feed_cleanup_proof(sparse_proof, sparse_stable)
        if sparse_proof is not None
        else None
    )
    check(
        "feed-sparse-two-anchor-shift-advances",
        isinstance(sparse_proof, open_reddit.FeedCleanupProof)
        and isinstance(sparse_shifted_proof, open_reddit.FeedCleanupProof),
    )
    check(
        "feed-sparse-two-anchor-stable-advances",
        isinstance(sparse_stable_proof, open_reddit.FeedCleanupProof),
    )

    one_anchor_before = sparse_feed_frame(anchor_count=1)
    one_anchor_proof = open_reddit.make_feed_cleanup_proof(one_anchor_before)
    one_anchor_after = sparse_feed_frame(
        offset=-110,
        navigation=False,
        anchor_count=1,
    )
    check(
        "feed-sparse-one-anchor-factory-rejected",
        one_anchor_proof is None,
    )
    preflight_rng = DeterministicRng("min", 15)
    (
        preflight_stack,
        preflight_proof,
        preflight_error,
        preflight_dumps,
        _preflight_logs,
        _preflight_clock,
    ) = run_browse_case(
        lambda _index: one_anchor_after,
        rng=preflight_rng,
        initial_nodes=one_anchor_before,
    )
    check(
        "feed-sparse-one-anchor-preflight-stops",
        preflight_proof is None
        and "did not start from a proven" in preflight_error.casefold(),
        preflight_error,
    )
    check(
        "feed-sparse-one-anchor-preflight-no-flick",
        preflight_stack.flicks == []
        and preflight_stack.taps == []
        and len(preflight_dumps) == 1
        and preflight_dumps[0][0][2] == "928_feed_browse_start",
    )

    inconsistent_sparse = [
        node("Sparse unique community anchor", 80, 330 - 55, w=430),
        node("Sparse unique post title anchor", 80, 510 - 150, w=430),
        node("Search Reddit", 260, 115, w=225),
        node("For You", 135, 190, w=130),
        node("Following", 459, 193, w=166),
        node("Join", 568, 253, w=78),
    ]
    unrelated_sparse = [
        node("Unrelated sparse community text", 80, 220, w=430),
        node("Unrelated sparse post title", 80, 400, w=430),
        node("Search Reddit", 260, 115, w=225),
        node("For You", 135, 190, w=130),
        node("Following", 459, 193, w=166),
        node("Join", 568, 253, w=78),
    ]
    sparse_rejections = (
        ("inconsistent", inconsistent_sparse),
        (
            "protected",
            sparse_shifted + [node("Verify your email", 80, 760, w=400)],
        ),
        ("springboard", sparse_shifted + [node("Reddit", 280, 1015, w=120)]),
        ("unrelated", unrelated_sparse),
    )
    for label, rejected_nodes in sparse_rejections:
        check(
            "feed-sparse-" + label + "-rejected",
            isinstance(sparse_proof, open_reddit.FeedCleanupProof)
            and open_reddit.advance_feed_cleanup_proof(
                sparse_proof,
                rejected_nodes,
            )
            is None,
        )

    original_get_slot = create.get_slot
    original_slot_done = create.slot_reddit_done
    original_close = create.close_reddit
    original_browse = create.browse_created_feed
    completed_events: list[object] = []
    create.get_slot = lambda _sid: {"ghost": True, "email": "local", "reddit": True}
    create.slot_reddit_done = lambda _slot: True
    create.dump_named = lambda *args, **kwargs: ("", feed_frame())
    create.close_reddit = lambda *args, **kwargs: completed_events.append(("close", kwargs)) or True

    def completed_browse_forbidden(*_args, **_kwargs):
        completed_events.append("browse")
        raise AssertionError("completed slots must not browse again")

    create.browse_created_feed = completed_browse_forbidden
    create.log = lambda _message: None
    completed_error = ""
    try:
        create.run_slot("a0001", object(), object())
    except Exception as exc:
        completed_error = str(exc)
    finally:
        create.get_slot = original_get_slot
        create.slot_reddit_done = original_slot_done
        create.dump_named = original_dump
        create.close_reddit = original_close
        create.browse_created_feed = original_browse
        create.log = original_log
    check(
        "feed-completed-slot-skips-browse",
        not completed_error
        and completed_events == [("close", {})],
    )


def feed_regressions() -> None:
    """Creation now checkpoints a strict feed and immediately cleans up."""

    feed = feed_frame()
    lone_reddit = [node("Reddit", 280, 500, w=120)]
    in_app_title = [node("Reddit", 25, 100, w=160)]
    alternate_home = [
        node("Weather", 40, 240, w=100),
        node("Books", 220, 400, w=90),
        node("Photos", 400, 550, w=90),
        node("Settings", 580, 700, w=110),
        node("Q Search", 310, 1114, w=120),
    ]
    springboard = alternate_home
    check("feed-state", create.feed_navigation_state(feed) == "feed")
    check(
        "feed-springboard-wins",
        create.feed_navigation_state(springboard) == "springboard",
    )
    check(
        "feed-title-not-home-icon",
        create.feed_navigation_state(in_app_title) == "unknown",
    )
    check(
        "feed-lone-body-reddit-not-springboard",
        not open_reddit.proven_springboard(lone_reddit)
        and create.feed_navigation_state(lone_reddit) == "unknown",
    )
    feed_with_incidental_signup_word = feed + [
        node("Interests", 90, 560, w=170),
        node("A post about you and your interests", 90, 650, w=430),
    ]
    check(
        "feed-incidental-protected-words-do-not-break-proof",
        create.proven_home_feed(feed_with_incidental_signup_word)
        and isinstance(
            open_reddit.make_feed_cleanup_proof(feed_with_incidental_signup_word),
            open_reddit.FeedCleanupProof,
        ),
    )

    run_source = inspect.getsource(create.run_slot)
    cleanup_source = inspect.getsource(create._close_and_finalize_cleanup)
    proof_at = run_source.find("feed_proof = wait_feed(")
    mark_at = run_source.find("mark_reddit_created(")
    finalize_at = run_source.rfind("_close_and_finalize_cleanup(")
    close_at = cleanup_source.find("closed = close_reddit(")
    checkpoint_guard_at = cleanup_source.find("if not app_stopped_checkpointed")
    complete_at = cleanup_source.rfind('mark_reddit_cleanup(sid, "complete"')
    check(
        "feed-proof-mark-close-complete-order",
        0 <= proof_at < mark_at < finalize_at
        and 0 <= close_at < checkpoint_guard_at < complete_at,
    )
    check(
        "feed-creation-has-no-browse-or-flick",
        "browse_created_feed" not in run_source
        and "interp_flick" not in run_source
        and "random" not in run_source
        and "qa_scroll" not in run_source,
    )
    check(
        "feed-proof-and-checkpoint-passed-to-close",
        "feed_proof=feed_proof" in cleanup_source
        and "on_app_stopped=checkpoint_app_stopped" in cleanup_source
        and "closed is not True" in cleanup_source,
    )
    check("feed-no-day1-call", "run_day1_session" not in run_source)

    original_dump = create.dump_named
    original_sleep = create.time.sleep
    original_log = create.log
    create.log = lambda _message: None
    create.time.sleep = lambda _seconds: None
    try:
        create.dump_named = lambda *args, **kwargs: ("", feed)
        strict_proof = create.wait_feed(FakeStack(), object())
        check(
            "feed-wait-returns-strict-proof",
            isinstance(strict_proof, open_reddit.FeedCleanupProof)
            and strict_proof.reference_nodes == tuple(feed),
        )

        create.dump_named = lambda *args, **kwargs: (
            "",
            [node("Unknown screen", 80, 500, w=300)],
        )
        expect_phone_error(
            "feed-wait-unknown-does-not-authorize-creation",
            lambda: create.wait_feed(FakeStack(), object()),
            "did not mark the account created",
        )
    finally:
        create.dump_named = original_dump
        create.time.sleep = original_sleep
        create.log = original_log

    proof = open_reddit.make_feed_cleanup_proof(feed)
    check("feed-synthetic-proof-created", isinstance(proof, open_reddit.FeedCleanupProof))

    switcher_reddit = [
        node("Reddit", 272, 175, w=106, h=24),
        node("Reddit preview content", 80, 620, w=520),
    ]
    adjacent_switcher_cleanup = [
        node("Settings", 275, 152, w=125, h=25),
        node("Reddit preview one", 616, 314, w=131, h=15),
        node("Reddit preview two", 620, 380, w=125, h=15),
        node("Reddit preview three", 625, 450, w=120, h=15),
    ]
    switcher_titleless = [node("Preview content without title", 80, 620, w=520)]
    switcher_other = [
        node("Safari", 272, 175, w=106, h=24),
        node("Different preview content", 80, 620, w=520),
    ]
    blank_home_transition = [node("OCR_EMPTY:table", 0, 0, w=750, h=1334)]

    def run_exact_bundle_cleanup(
        close_mode: str = "force-close",
        *,
        fresh_from_reddit: bool = False,
        switcher_nodes=None,
        after_nodes=None,
        centered_nodes=None,
        fail_force: bool = False,
        running_after_force: bool = False,
        running_sequence=None,
        home_nodes=None,
        home_frames=None,
        already_complete: bool = False,
    ):
        originals = {
            "dump_named": create.dump_named,
            "mark_reddit_cleanup": create.mark_reddit_cleanup,
            "log": create.log,
            "sleep": create.time.sleep,
        }
        checkpoints: list[str] = []
        stack = FakeCloseStack(
            close_mode=close_mode,
            fail_force=fail_force,
            running_after_force=running_after_force,
            running_sequence=running_sequence,
        )
        final_home_frames = list(
            home_frames
            if home_frames is not None
            else [alternate_home if home_nodes is None else home_nodes]
        )

        def cleanup_dump(*args, **_kwargs):
            stem = args[2]
            if stem.startswith("899_completed_swipes_home_verify"):
                if len(final_home_frames) > 1:
                    return "", final_home_frames.pop(0)
                return "", final_home_frames[0]
            frames = {
                "897_completed_swipes_pre_switcher": feed,
                "898_completed_swipes_switcher": (
                    switcher_reddit if switcher_nodes is None else switcher_nodes
                ),
                "898_after_completed_swipes_reddit_flick": (
                    switcher_other if after_nodes is None else after_nodes
                ),
                "898_completed_swipes_reddit_centered": (
                    switcher_reddit if centered_nodes is None else centered_nodes
                ),
            }
            return "", frames[stem]

        create.dump_named = cleanup_dump
        create.mark_reddit_cleanup = (
            lambda _sid, state, *_args, **_kwargs:
            checkpoints.append(state) or {}
        )
        create.log = lambda _message: None
        create.time.sleep = lambda _seconds: None
        error = ""
        try:
            create._close_exact_bundle_after_completed_swipes(
                "a0012",
                object(),
                stack,
                fresh_from_reddit=fresh_from_reddit,
                already_complete=already_complete,
            )
        except PhoneError as exc:
            error = str(exc)
        finally:
            for name, value in originals.items():
                if name == "sleep":
                    create.time.sleep = value
                else:
                    setattr(create, name, value)
        return stack.events, checkpoints, error

    for mode in ("force-close", "already-stopped"):
        exact_events, exact_checkpoints, exact_error = run_exact_bundle_cleanup(mode)
        check(
            f"feed-strict-close-{mode}-removes-card-and-proves-home",
            not exact_error
            and exact_events[0] == "switcher"
            and exact_events[1][0] == "flick"
            and exact_events[2:] == [
                ("force", create.REDDIT_BUNDLE),
                ("running", create.REDDIT_BUNDLE),
                "home",
                ("running", create.REDDIT_BUNDLE),
            ]
            and exact_checkpoints == ["app_stopped", "complete"],
            f"{exact_events!r} {exact_checkpoints!r} {exact_error}",
        )

    failed_events, failed_checkpoints, failed_error = run_exact_bundle_cleanup(
        fail_force=True,
    )
    check(
        "feed-force-close-failure-keeps-pending-and-attempts-home",
        "synthetic force-close failure" in failed_error
        and failed_events[-3:] == [
            ("force", create.REDDIT_BUNDLE),
            "home",
            ("running", create.REDDIT_BUNDLE),
        ]
        and failed_checkpoints == [],
        f"{failed_events!r} {failed_checkpoints!r} {failed_error}",
    )

    titleless_events, titleless_checkpoints, titleless_error = run_exact_bundle_cleanup(
        fresh_from_reddit=True,
        switcher_nodes=switcher_titleless,
    )

    adjacent_events, adjacent_checkpoints, adjacent_error = run_exact_bundle_cleanup(
        switcher_nodes=adjacent_switcher_cleanup,
        centered_nodes=switcher_reddit,
    )
    check(
        "feed-adjacent-card-centers-exact-reddit-before-dismissal",
        not adjacent_error
        and [
            event
            for event in adjacent_events
            if isinstance(event, tuple) and event[0] == "flick"
        ]
        == [
            ("flick", (650, 700, 250, 700), {"steps": 16, "ms": 22}),
            ("flick", (325, 780, 325, 60), {"steps": 16, "ms": 22}),
        ]
        and adjacent_checkpoints == ["app_stopped", "complete"],
        f"{adjacent_events!r} {adjacent_checkpoints!r} {adjacent_error}",
    )
    check(
        "feed-titleless-fresh-provenance-dismisses-centered-reddit-once",
        not titleless_error
        and len([event for event in titleless_events if isinstance(event, tuple) and event[0] == "flick"]) == 1
        and titleless_checkpoints == ["app_stopped", "complete"],
        f"{titleless_events!r} {titleless_checkpoints!r} {titleless_error}",
    )

    unknown_events, unknown_checkpoints, unknown_error = run_exact_bundle_cleanup(
        switcher_nodes=switcher_titleless,
    )
    check(
        "feed-titleless-resume-force-close-is-sufficient",
        not unknown_error
        and not any(isinstance(event, tuple) and event[0] == "flick" for event in unknown_events)
        and ("force", create.REDDIT_BUNDLE) in unknown_events
        and unknown_checkpoints == ["app_stopped", "complete"],
        f"{unknown_events!r} {unknown_checkpoints!r} {unknown_error}",
    )

    running_events, running_checkpoints, running_error = run_exact_bundle_cleanup(
        running_after_force=True,
    )
    check(
        "feed-surviving-process-keeps-cleanup-pending",
        "remained running" in running_error.casefold()
        and running_checkpoints == [],
        f"{running_events!r} {running_checkpoints!r} {running_error}",
    )

    bad_home_events, bad_home_checkpoints, bad_home_error = run_exact_bundle_cleanup(
        home_nodes=[node("Unknown screen", 80, 500, w=300)],
    )
    check(
        "feed-exact-bundle-home-ocr-failure-completes-locally",
        not bad_home_error
        and bad_home_events[-4:]
        == ["home"] + [("running", create.REDDIT_BUNDLE)] * 3
        and bad_home_checkpoints == ["app_stopped", "complete"],
        f"{bad_home_events!r} {bad_home_checkpoints!r} {bad_home_error}",
    )

    transition_events, transition_checkpoints, transition_error = (
        run_exact_bundle_cleanup(
            home_frames=[blank_home_transition, alternate_home],
        )
    )
    check(
        "feed-final-home-blank-transition-retries-without-second-force-close",
        not transition_error
        and transition_events.count(("force", create.REDDIT_BUNDLE)) == 1
        and transition_events.count("home") == 1
        and transition_checkpoints == ["app_stopped", "complete"],
        f"{transition_events!r} {transition_checkpoints!r} {transition_error}",
    )

    relaunched_events, relaunched_checkpoints, relaunched_error = (
        run_exact_bundle_cleanup(
            home_frames=[blank_home_transition, alternate_home],
            running_sequence=[False, True, False, False],
        )
    )
    check(
        "feed-final-home-relaunch-force-closes-exact-reddit-once",
        not relaunched_error
        and relaunched_events.count(("force", create.REDDIT_BUNDLE)) == 2
        and relaunched_events.count("home") == 2
        and all(
            event[1] == create.REDDIT_BUNDLE
            for event in relaunched_events
            if isinstance(event, tuple) and event[:1] == ("force",)
        )
        and relaunched_checkpoints == ["app_stopped", "complete"],
        f"{relaunched_events!r} {relaunched_checkpoints!r} {relaunched_error}",
    )

    repeated_blank_events, repeated_blank_checkpoints, repeated_blank_error = (
        run_exact_bundle_cleanup(home_frames=[blank_home_transition])
    )
    check(
        "feed-final-home-three-unproven-frames-complete-after-force-close",
        not repeated_blank_error
        and repeated_blank_events.count(("force", create.REDDIT_BUNDLE)) == 1
        and repeated_blank_checkpoints == ["app_stopped", "complete"],
        f"{repeated_blank_events!r} {repeated_blank_checkpoints!r} "
        f"{repeated_blank_error}",
    )

    repeated_restart_events, repeated_restart_checkpoints, repeated_restart_error = (
        run_exact_bundle_cleanup(
            home_frames=[blank_home_transition, alternate_home],
            running_sequence=[False, True, False, True],
        )
    )
    check(
        "feed-final-home-second-restart-completes-after-exact-force-close",
        not repeated_restart_error
        and repeated_restart_events.count(("force", create.REDDIT_BUNDLE)) == 2
        and repeated_restart_checkpoints == ["app_stopped", "complete"],
        f"{repeated_restart_events!r} {repeated_restart_checkpoints!r} "
        f"{repeated_restart_error}",
    )

    def run_fresh_flow(
        close_mode: str,
        *,
        configured_swipes: int | None = None,
        swipe_final_proof=proof,
    ):
        originals = {
            "get_slot": create.get_slot,
            "dump_named": create.dump_named,
            "advance_post_interests": create.advance_post_interests,
            "wait_feed": create.wait_feed,
            "mark_reddit_created": create.mark_reddit_created,
            "mark_creation_feed_swipe_completed": create.mark_creation_feed_swipe_completed,
            "run_feed_swipes": create.run_feed_swipes,
            "mark_reddit_cleanup": create.mark_reddit_cleanup,
            "close_reddit": create.close_reddit,
            "log": create.log,
        }
        events: list[object] = []
        create.get_slot = lambda _sid: {
            "id": "a0001",
            "ghost": True,
            "email": "local@example.invalid",
            "username": "saved-user",
            "password": "saved-password",
            "reddit": False,
        }
        def fresh_flow_dump(*args, **_kwargs):
            stem = args[2] if len(args) > 2 else ""
            if stem == "898_completed_swipes_switcher":
                return "", switcher_reddit
            if stem == "898_after_completed_swipes_reddit_flick":
                return "", switcher_other
            if stem.startswith("899_completed_swipes_home_verify"):
                return "", alternate_home
            return "", feed

        create.dump_named = fresh_flow_dump
        create.advance_post_interests = (
            lambda *args, **kwargs: events.append("advance") or feed
        )
        create.wait_feed = lambda *args, **kwargs: events.append("proof") or proof
        create.mark_reddit_created = lambda *args, **kwargs: events.append(
            ("mark-pending", kwargs.get("feed_swipes"))
            if "feed_swipes" in kwargs
            else "mark-pending"
        ) or {}
        create.mark_creation_feed_swipe_completed = (
            lambda _sid, completed, *_args, **_kwargs:
            events.append(("persist", completed)) or {}
        )

        def fake_swipes(*_args, **kwargs):
            target = kwargs["target"]
            completed = kwargs.get("completed", 0)
            events.append(("swipes", target, completed))
            progress_fn = kwargs.get("progress_fn")
            if progress_fn is not None:
                for value in range(completed + 1, target + 1):
                    progress_fn(value)
            return type(
                "SyntheticSwipeResult",
                (),
                {"completed": target, "final_proof": swipe_final_proof},
            )()

        create.run_feed_swipes = fake_swipes
        create.mark_reddit_cleanup = (
            lambda _sid, state, *_args, **_kwargs: events.append(("cleanup", state)) or {}
        )

        def fake_close(*_args, **kwargs):
            events.append(("close", kwargs.get("feed_proof")))
            if close_mode == "before-checkpoint":
                raise PhoneError("synthetic pre-checkpoint cleanup failure")
            if close_mode == "false-result":
                return False
            callback = kwargs.get("on_app_stopped")
            if callback is not None and close_mode != "missing-callback":
                callback()
            if close_mode == "after-checkpoint":
                raise PhoneError("synthetic final Home proof failure")
            return True

        create.close_reddit = fake_close
        create.log = lambda _message: None
        error = ""
        flow_stack = FakeStack()
        try:
            create.run_slot(
                "a0001",
                object(),
                flow_stack,
                feed_swipes=configured_swipes,
            )
        except PhoneError as exc:
            error = str(exc)
        finally:
            for name, value in originals.items():
                setattr(create, name, value)
        return events, error, flow_stack

    fresh_events, fresh_error, fresh_stack = run_fresh_flow("success")
    check("feed-fresh-flow-no-error", not fresh_error, fresh_error)
    check(
        "feed-fresh-flow-runtime-sends-no-feed-gesture",
        fresh_stack.flicks == [] and fresh_stack.taps == [],
    )
    check(
        "feed-fresh-flow-checkpoints-in-order",
        fresh_events
        == [
            "advance",
            "proof",
            "mark-pending",
            ("close", proof),
            ("cleanup", "app_stopped"),
            ("cleanup", "complete"),
        ],
        repr(fresh_events),
    )

    configured_events, configured_error, configured_stack = run_fresh_flow(
        "success", configured_swipes=30
    )
    configured_persists = [
        item[1]
        for item in configured_events
        if isinstance(item, tuple) and item[:1] == ("persist",)
    ]
    check(
        "feed-configured-creation-runs-before-strict-cleanup",
        not configured_error
        and configured_events[:4]
        == ["advance", "proof", ("mark-pending", 30), ("swipes", 30, 0)]
        and configured_persists == list(range(1, 31))
        and ("close", proof) not in configured_events
        and configured_events[-2:]
        == [("cleanup", "app_stopped"), ("cleanup", "complete")]
        and len(configured_stack.flicks) == 1
        and configured_stack.cleanup_events
        == [
            "switcher",
            ("force", create.REDDIT_BUNDLE),
            ("running", create.REDDIT_BUNDLE),
            "home",
            ("running", create.REDDIT_BUNDLE),
        ],
        f"{configured_events!r} {configured_stack.cleanup_events!r}",
    )

    unclear_events, unclear_error, unclear_stack = run_fresh_flow(
        "success",
        configured_swipes=25,
        swipe_final_proof=None,
    )
    check(
        "feed-configured-unclear-final-removes-card-and-force-closes",
        not unclear_error
        and len(unclear_stack.flicks) == 1
        and unclear_stack.taps == []
        and unclear_stack.cleanup_events
        == [
            "switcher",
            ("force", create.REDDIT_BUNDLE),
            ("running", create.REDDIT_BUNDLE),
            "home",
            ("running", create.REDDIT_BUNDLE),
        ]
        and ("close", None) not in unclear_events
        and unclear_events[-2:]
        == [("cleanup", "app_stopped"), ("cleanup", "complete")],
        f"{unclear_events!r} {unclear_stack.cleanup_events!r} {unclear_error}",
    )

    before_events, before_error, _before_stack = run_fresh_flow("before-checkpoint")
    check(
        "feed-cleanup-failure-preserves-created-pending",
        "pre-checkpoint" in before_error
        and before_events[-2:] == ["mark-pending", ("close", proof)]
        and not any(
            isinstance(item, tuple) and item[:1] == ("cleanup",)
            for item in before_events
        ),
        repr(before_events),
    )

    after_events, after_error, _after_stack = run_fresh_flow("after-checkpoint")
    check(
        "feed-home-failure-preserves-app-stopped",
        "final Home" in after_error
        and after_events[-3:]
        == [
            "mark-pending",
            ("close", proof),
            ("cleanup", "app_stopped"),
        ]
        and ("cleanup", "complete") not in after_events,
        repr(after_events),
    )

    false_events, false_error, _false_stack = run_fresh_flow("false-result")
    check(
        "feed-false-close-result-cannot-complete",
        "verified success result" in false_error
        and ("cleanup", "complete") not in false_events
        and ("cleanup", "app_stopped") not in false_events,
        repr(false_events),
    )

    missing_events, missing_error, _missing_stack = run_fresh_flow("missing-callback")
    check(
        "feed-missing-app-stopped-callback-cannot-complete",
        "durable app-stopped checkpoint" in missing_error
        and ("cleanup", "complete") not in missing_events
        and ("cleanup", "app_stopped") not in missing_events,
        repr(missing_events),
    )

    def run_resume(
        slot,
        nodes,
        *,
        process_running: bool | list[bool] = False,
        final_home_nodes=None,
        final_home_frames=None,
    ):
        originals = {
            "get_slot": create.get_slot,
            "dump_named": create.dump_named,
            "mark_reddit_cleanup": create.mark_reddit_cleanup,
            "close_reddit": create.close_reddit,
            "log": create.log,
        }
        events: list[object] = []
        running_values = (
            list(process_running)
            if isinstance(process_running, list)
            else []
        )
        home_frames = list(final_home_frames or [])

        class ResumeStack:
            def app_is_running(self, bundle_id):
                events.append(("running", bundle_id))
                if running_values:
                    return running_values.pop(0)
                return process_running

            def clear_app_memory(self, bundle_id):
                events.append(("clear", bundle_id))
                return "force-close"

            def force_close_app(self, bundle_id):
                events.append(("force", bundle_id))
                return "force-close"

            def open_app_switcher(self):
                events.append("switcher")
                return "double-home-nogap"

            def interp_flick(self, *args, **kwargs):
                events.append(("flick", args, kwargs))

            def press_home(self):
                events.append("home")

        create.get_slot = lambda _sid: dict(slot)

        def resume_dump(*args, **_kwargs):
            stem = args[2] if len(args) > 2 else ""
            if stem == "898_completed_swipes_switcher":
                return "", switcher_reddit
            if stem == "898_after_completed_swipes_reddit_flick":
                return "", switcher_other
            if (
                stem.startswith("899_completed_swipes_home_verify")
                or stem.startswith("899_cleanup_home_verify")
            ):
                if home_frames:
                    if len(home_frames) > 1:
                        return "", home_frames.pop(0)
                    return "", home_frames[0]
                if final_home_nodes is not None:
                    return "", final_home_nodes
            return "", nodes

        create.dump_named = resume_dump
        create.mark_reddit_cleanup = (
            lambda _sid, state, *_args, **_kwargs: events.append(("cleanup", state)) or {}
        )

        def fake_close(*_args, **kwargs):
            events.append(
                (
                    "close",
                    isinstance(kwargs.get("feed_proof"), open_reddit.FeedCleanupProof),
                    kwargs.get("resume_from_home", False),
                )
            )
            callback = kwargs.get("on_app_stopped")
            if callback is not None:
                callback()
            return True

        create.close_reddit = fake_close
        create.log = lambda _message: None
        error = ""
        try:
            create.run_slot("a0001", object(), ResumeStack())
        except PhoneError as exc:
            error = str(exc)
        finally:
            for name, value in originals.items():
                setattr(create, name, value)
        return events, error

    pending = {
        "id": "a0001",
        "reddit": True,
        "reddit_cleanup": "pending",
        "creation_feed_swipes_target": 25,
        "creation_feed_swipes_completed": 25,
    }
    pending_feed_events, pending_feed_error = run_resume(pending, feed)
    check(
        "feed-pending-complete-swipes-home-ocr-failure-completes-locally",
        not pending_feed_error
        and pending_feed_events[0] == "switcher"
        and pending_feed_events[1][0] == "flick"
        and pending_feed_events[2:]
        == [
            ("force", create.REDDIT_BUNDLE),
            ("running", create.REDDIT_BUNDLE),
            ("cleanup", "app_stopped"),
            "home",
            ("running", create.REDDIT_BUNDLE),
            ("running", create.REDDIT_BUNDLE),
            ("running", create.REDDIT_BUNDLE),
            ("cleanup", "complete"),
        ],
        repr(pending_feed_events),
    )

    pending_home_events, pending_home_error = run_resume(pending, alternate_home)
    check(
        "feed-pending-25-of-25-runs-exact-bundle-cleanup-without-more-swipes",
        not pending_home_error
        and pending_home_events[0] == "switcher"
        and pending_home_events[1][0] == "flick"
        and pending_home_events[2:]
        == [
            ("force", create.REDDIT_BUNDLE),
            ("running", create.REDDIT_BUNDLE),
            ("cleanup", "app_stopped"),
            "home",
            ("running", create.REDDIT_BUNDLE),
            ("cleanup", "complete"),
        ],
        repr(pending_home_events),
    )

    pending_31 = dict(pending)
    pending_31.update(
        {
            "id": "a0018",
            "creation_feed_swipes_target": 31,
            "creation_feed_swipes_completed": 31,
        }
    )
    pending_31_events, pending_31_error = run_resume(pending_31, alternate_home)
    check(
        "feed-pending-a0018-31-of-31-runs-cleanup-only",
        not pending_31_error
        and pending_31_events[0] == "switcher"
        and ("force", create.REDDIT_BUNDLE) in pending_31_events
        and pending_31_events[-1] == ("cleanup", "complete")
        and not any(
            isinstance(event, tuple) and event[:1] == ("swipes",)
            for event in pending_31_events
        ),
        repr(pending_31_events),
    )

    resume_originals = {
        "get_slot": create.get_slot,
        "dump_named": create.dump_named,
        "open_container": create.open_container,
        "run_feed_swipes": create.run_feed_swipes,
        "mark_creation_feed_swipe_completed": create.mark_creation_feed_swipe_completed,
        "_close_exact_bundle_after_completed_swipes": (
            create._close_exact_bundle_after_completed_swipes
        ),
        "log": create.log,
    }
    resume_events: list[object] = []
    resume_frames = iter((alternate_home, feed))
    incomplete_slot = {
        "id": "a0001",
        "email": "local@example.invalid",
        "reddit": True,
        "reddit_cleanup": "pending",
        "creation_feed_swipes_target": 30,
        "creation_feed_swipes_completed": 12,
    }
    create.get_slot = lambda _sid: dict(incomplete_slot)
    create.dump_named = lambda *_args, **_kwargs: ("", next(resume_frames))
    create.open_container = lambda *_args, **kwargs: resume_events.append(
        ("open", _args[3], _args[4], kwargs)
    )
    create.mark_creation_feed_swipe_completed = (
        lambda _sid, completed, *_args, **_kwargs:
        resume_events.append(("persist", completed)) or {}
    )

    def fake_resume_swipes(*_args, **kwargs):
        resume_events.append(
            ("swipes", kwargs["completed"], kwargs["target"])
        )
        for value in range(kwargs["completed"] + 1, kwargs["target"] + 1):
            kwargs["progress_fn"](value)
        return type(
            "SyntheticSwipeResult",
            (),
            {"completed": kwargs["target"], "final_proof": proof},
        )()

    create.run_feed_swipes = fake_resume_swipes
    create._close_exact_bundle_after_completed_swipes = (
        lambda _sid, _phone, _stack, **kwargs:
        resume_events.append(("strict-cleanup", kwargs.get("fresh_from_reddit")))
    )
    create.log = lambda _message: None
    resume_error = ""
    try:
        # A changed panel value is intentionally ignored for an already-started slot.
        create.run_slot("a0001", object(), FakeStack(), feed_swipes=40)
    except PhoneError as exc:
        resume_error = str(exc)
    finally:
        for name, value in resume_originals.items():
            setattr(create, name, value)
    resume_persists = [
        item[1]
        for item in resume_events
        if isinstance(item, tuple) and item[:1] == ("persist",)
    ]
    check(
        "feed-incomplete-cleanup-reopens-exact-container-and-resumes-thirteen",
        not resume_error
        and resume_events[0]
        == (
            "open",
            "a0001",
            "local@example.invalid",
            {"allow_existing_sparse_feed": True},
        )
        and ("swipes", 12, 30) in resume_events
        and resume_persists == list(range(13, 31))
        and resume_events[-1] == ("strict-cleanup", True),
        repr(resume_events),
    )

    no_swipe_pending = dict(pending)
    no_swipe_pending["creation_feed_swipes_target"] = 0
    no_swipe_pending["creation_feed_swipes_completed"] = 0
    unknown_events, unknown_error = run_resume(
        no_swipe_pending,
        [node("Unknown screen", 80, 500, w=300)],
    )
    check(
        "feed-no-swipe-pending-unknown-stops-before-cleanup",
        "neither a proven" in unknown_error and unknown_events == [],
        unknown_error,
    )

    stopped = {"id": "a0001", "reddit": True, "reddit_cleanup": "app_stopped"}
    stopped_events, stopped_error = run_resume(stopped, alternate_home)
    check(
        "feed-app-stopped-resume-only-proves-process-and-home",
        not stopped_error
        and stopped_events
        == [
            ("running", create.REDDIT_BUNDLE),
            "home",
            ("running", create.REDDIT_BUNDLE),
            ("cleanup", "complete"),
        ],
        repr(stopped_events),
    )

    stopped_transition_events, stopped_transition_error = run_resume(
        stopped,
        alternate_home,
        final_home_frames=[blank_home_transition, alternate_home],
    )
    check(
        "feed-app-stopped-resume-retries-blank-home-transition",
        not stopped_transition_error
        and stopped_transition_events
        == [
            ("running", create.REDDIT_BUNDLE),
            "home",
            ("running", create.REDDIT_BUNDLE),
            ("running", create.REDDIT_BUNDLE),
            ("cleanup", "complete"),
        ],
        repr(stopped_transition_events),
    )

    running_events, running_error = run_resume(
        stopped,
        feed,
        process_running=[True, False, False],
        final_home_nodes=alternate_home,
    )
    check(
        "feed-app-stopped-running-process-runs-strict-cleanup-only",
        not running_error
        and running_events[0] == ("running", create.REDDIT_BUNDLE)
        and "switcher" in running_events
        and ("force", create.REDDIT_BUNDLE) in running_events
        and running_events[-1] == ("cleanup", "complete"),
        repr(running_events) + " :: " + running_error,
    )

    complete = {"id": "a0001", "reddit": True, "reddit_cleanup": "complete"}
    complete_events, complete_error = run_resume(complete, alternate_home)
    check(
        "feed-complete-slot-does-not-touch-phone",
        not complete_error and complete_events == [],
        repr(complete_events),
    )

    completed_swipes = {
        "id": "a0013",
        "reddit": True,
        "reddit_cleanup": "complete",
        "creation_feed_swipes_target": 28,
        "creation_feed_swipes_completed": 28,
    }
    completed_swipe_events, completed_swipe_error = run_resume(
        completed_swipes,
        alternate_home,
    )
    check(
        "feed-complete-a0013-retry-runs-strict-cleanup-audit-only",
        not completed_swipe_error
        and completed_swipe_events[0] == "switcher"
        and completed_swipe_events[1][0] == "flick"
        and completed_swipe_events[2:]
        == [
            ("force", create.REDDIT_BUNDLE),
            ("running", create.REDDIT_BUNDLE),
            "home",
            ("running", create.REDDIT_BUNDLE),
        ]
        and not any(
            isinstance(event, tuple) and event[:1] == ("cleanup",)
            for event in completed_swipe_events
        ),
        f"{completed_swipe_events!r} {completed_swipe_error}",
    )


def close_reddit_regressions() -> None:
    switcher = [
        node("Reddit", 272, 175, w=106, h=24),
        node("Feed preview content", 80, 600, w=560),
    ]
    adjacent_switcher = [
        node("Settings", 337, 177, w=125, h=26),
        node("r/example", 650, 320, w=110),
        node("Adjacent preview line", 640, 410, w=105),
        node("Home", 650, 1070, w=70),
    ]
    clipped_video_switcher = [
        node("Settings", 272, 164, w=128, h=25),
        node("109", 604, 368, w=87, h=18),
        node("u/example", 628, 411, w=118, h=15),
        node("Promoted title", 584, 442, w=162, h=20),
        node("Promoted detail", 584, 466, w=162, h=18),
        node("Card footer", 584, 488, w=84, h=16),
        node("Replay", 704, 798, w=43, h=12),
        node("Vote", 603, 1106, w=128, h=19),
    ]
    ordinary_centered_title = [
        node("Settings", 337, 177, w=125, h=26),
        node("Ordinary app body", 90, 430, w=400),
    ]
    next_card = [
        node("Photos", 272, 175, w=106, h=24),
        node("Photo preview content", 80, 600, w=560),
    ]
    home = [
        node("Reddit", 40, 300, w=120),
        node("Weather", 220, 300, w=100),
        node("Photos", 400, 500, w=90),
        node("Settings", 580, 700, w=110),
        node("Q Search", 310, 1114, w=120),
    ]
    alternate_home = [
        node("Weather", 40, 240, w=100),
        node("Books", 220, 400, w=90),
        node("Photos", 400, 550, w=90),
        node("Settings", 580, 700, w=110),
        node("Q Search", 310, 1114, w=120),
    ]
    check(
        "close-switcher-title-proven",
        open_reddit.switcher_app_card(switcher, "Reddit") is not None,
    )
    check(
        "close-home-icon-not-a-card",
        open_reddit.switcher_app_card(home, "Reddit") is None,
    )
    check("close-springboard-reddit-page", open_reddit.proven_springboard(home))
    check(
        "close-springboard-alternate-page",
        open_reddit.proven_springboard(alternate_home),
    )
    lone_body_reddit = [node("Reddit", 280, 500, w=120)]
    check(
        "close-lone-body-reddit-not-springboard",
        not open_reddit.proven_springboard(lone_body_reddit),
    )
    check(
        "close-switcher-not-springboard",
        not open_reddit.proven_springboard(switcher),
    )
    check(
        "close-switcher-title-not-home-icon",
        open_reddit.springboard_reddit(switcher) is None,
    )
    check(
        "close-adjacent-switcher-proven",
        open_reddit.switcher_centered_title(adjacent_switcher) is not None,
    )
    check(
        "close-clipped-video-adjacent-switcher-proven",
        open_reddit.switcher_centered_title(clipped_video_switcher) is not None,
    )
    check(
        "close-ordinary-centered-title-rejected",
        open_reddit.switcher_centered_title(ordinary_centered_title) is None,
    )
    hidden = node("Reddit", 272, 175, w=106, h=24)
    hidden.visible = False
    check(
        "close-hidden-title-not-a-card",
        open_reddit.switcher_app_card([hidden], "Reddit") is None,
    )
    check(
        "close-removal-proof",
        open_reddit.switcher_app_removed(switcher, next_card, "Reddit"),
    )
    check(
        "close-removal-to-springboard-proof",
        open_reddit.switcher_app_removed_or_home(
            switcher,
            alternate_home,
            "Reddit",
        ),
    )
    check(
        "close-removal-to-lone-reddit-rejected",
        not open_reddit.switcher_app_removed_or_home(
            switcher,
            lone_body_reddit,
            "Reddit",
        ),
    )
    check(
        "close-unchanged-frame-rejected",
        not open_reddit.switcher_app_removed(switcher, switcher, "Reddit"),
    )
    check(
        "close-changed-frame-without-next-title-rejected",
        not open_reddit.switcher_app_removed(
            switcher,
            [node("Changed preview content", 80, 620, w=560)],
            "Reddit",
        ),
    )

    live_adjacent = saved_dump_nodes("diag_nogap_after")
    live_centered = saved_dump_nodes("diag_switcher_reddit_centered")
    live_removed = saved_dump_nodes("diag_switcher_reddit_removed")
    check(
        "close-live-adjacent-recents-fixture",
        open_reddit.switcher_centered_title(live_adjacent) is not None
        and open_reddit.switcher_app_card(live_adjacent, "Reddit") is None,
    )
    check(
        "close-live-centered-reddit-fixture",
        open_reddit.switcher_app_card(live_centered, "Reddit") is not None,
    )
    check(
        "close-live-removal-fixture",
        open_reddit.switcher_app_removed(live_centered, live_removed, "Reddit"),
    )

    class FakeMemoryPhone:
        def exec_ok(self, _command, timeout=0):
            return 0, ""

    memory_stack = open_reddit.TapStack(FakeMemoryPhone(), lambda _message: None)
    memory_stack.backend = "xxtouch"
    captured: dict[str, str] = {}

    def memory_result(raw: str):
        def fake_run(_name, source, timeout=0, wait_path=""):
            captured["source"] = source
            captured["path"] = wait_path
            return raw

        memory_stack._run_xxt_lua = fake_run

    memory_result("true true false nil 0")
    check(
        "close-memory-force-fallback",
        memory_stack.clear_app_memory(open_reddit.REDDIT_BUNDLE) == "force-close",
    )
    check(
        "close-memory-graceful-before-force",
        captured["source"].index("app.quit(bid)")
        < captured["source"].index("app.close(bid)"),
    )
    check(
        "close-memory-exact-target-only",
        'app.quit("*")' not in captured["source"]
        and open_reddit.REDDIT_BUNDLE in captured["source"],
    )
    memory_result("true false false nil skipped")
    check(
        "close-memory-graceful-result",
        memory_stack.clear_app_memory(open_reddit.REDDIT_BUNDLE) == "graceful-quit",
    )
    memory_result("false false false skipped skipped")
    check(
        "close-memory-already-stopped",
        memory_stack.clear_app_memory(open_reddit.REDDIT_BUNDLE) == "already-stopped",
    )
    memory_result("true true true nil 1")
    expect_phone_error(
        "close-memory-still-running-refused",
        lambda: memory_stack.clear_app_memory(open_reddit.REDDIT_BUNDLE),
        "remained in memory",
    )
    expect_phone_error(
        "close-memory-wildcard-refused",
        lambda: memory_stack.clear_app_memory("*"),
        "broad",
    )

    memory_result("true false")
    check(
        "close-force-exact-running-bundle",
        memory_stack.force_close_app(open_reddit.REDDIT_BUNDLE) == "force-close",
    )
    check(
        "close-force-always-uses-app-close-not-quit",
        "app.close(bid)" in captured["source"]
        and "app.quit" not in captured["source"]
        and open_reddit.REDDIT_BUNDLE in captured["source"],
    )
    memory_result("false false")
    check(
        "close-force-already-stopped-still-verifies",
        memory_stack.force_close_app(open_reddit.REDDIT_BUNDLE) == "already-stopped",
    )
    memory_result("true true")
    expect_phone_error(
        "close-force-surviving-process-refused",
        lambda: memory_stack.force_close_app(open_reddit.REDDIT_BUNDLE),
        "remained in memory",
    )
    expect_phone_error(
        "close-force-wildcard-refused",
        lambda: memory_stack.force_close_app("*"),
        "broad",
    )

    switcher_source = inspect.getsource(open_reddit.TapStack.open_app_switcher)
    check(
        "close-faceid-immediate-double-home-static",
        switcher_source.count('key.press("HOMEBUTTON")') == 2
        and "sys.msleep" not in switcher_source
        and 'return "double-home-nogap"' in switcher_source,
    )
    check(
        "close-homebutton-device-keeps-bounded-gap",
        "self.double_home(280)" in switcher_source,
    )

    face_stack = open_reddit.TapStack(FakeMemoryPhone(), lambda _message: None)
    face_stack.backend = "xxtouch"
    face_stack._xxt_screen_size = lambda: (1125, 2436)
    face_capture: dict[str, str] = {}

    def capture_face_switcher(name, source, timeout=0, wait_path=""):
        face_capture["name"] = name
        face_capture["source"] = source
        return ""

    face_stack._run_xxt_lua = capture_face_switcher
    original_sleep = ui_tree_module.time.sleep
    ui_tree_module.time.sleep = lambda _seconds: None
    try:
        face_mode = face_stack.open_app_switcher()
    finally:
        ui_tree_module.time.sleep = original_sleep
    face_lua = face_capture.get("source", "")
    check(
        "close-faceid-immediate-double-home-behavior",
        face_mode == "double-home-nogap"
        and face_capture.get("name") == "ra_switcher.lua"
        and face_lua.count('key.press("HOMEBUTTON")') == 2
        and "sys.msleep" not in face_lua,
    )

    original_dump = open_reddit.dump_named
    logs: list[str] = []
    dumps: list[tuple[tuple, dict]] = []

    def run_close(
        responses,
        stack=None,
        *,
        feed_proof=None,
        on_app_stopped=None,
        resume_from_home=False,
    ):
        stack = stack or FakeCloseStack()
        remaining = iter(responses)

        def fake_dump(*args, **kwargs):
            dumps.append((args, kwargs))
            return "synthetic", next(remaining)

        open_reddit.dump_named = fake_dump
        try:
            result = open_reddit.close_reddit(
                stack,
                object(),
                logs.append,
                feed_proof=feed_proof,
                on_app_stopped=on_app_stopped,
                resume_from_home=resume_from_home,
            )
        finally:
            open_reddit.dump_named = original_dump
        return stack, result

    def run_close_error(
        name,
        responses,
        contains,
        stack=None,
        *,
        feed_proof=None,
        on_app_stopped=None,
        resume_from_home=False,
    ):
        stack = stack or FakeCloseStack()
        remaining = iter(responses)

        def fake_dump(*args, **kwargs):
            dumps.append((args, kwargs))
            return "synthetic", next(remaining)

        open_reddit.dump_named = fake_dump
        try:
            expect_phone_error(
                name,
                lambda: open_reddit.close_reddit(
                    stack,
                    object(),
                    logs.append,
                    feed_proof=feed_proof,
                    on_app_stopped=on_app_stopped,
                    resume_from_home=resume_from_home,
                ),
                contains,
            )
        finally:
            open_reddit.dump_named = original_dump
        return stack

    stack, result = run_close((feed_nodes(), switcher, next_card, home))
    check("close-success-result", result is True)
    check(
        "close-success-sequence",
        stack.events
        == [
            "switcher",
            ("flick", (325, 780, 325, 60), {"steps": 16, "ms": 22}),
            ("force", open_reddit.REDDIT_BUNDLE),
            ("running", open_reddit.REDDIT_BUNDLE),
            "home",
            ("running", open_reddit.REDDIT_BUNDLE),
        ],
    )
    check("close-success-log", any("close_reddit=ok" in item.casefold() for item in logs))
    check(
        "close-home-probes-transient",
        all(
            kwargs.get("persist") is False and kwargs.get("preview") is False
            for args, kwargs in dumps
            if args[2] != "939_preclose_check"
        ),
    )

    dumps.clear()
    logs.clear()
    checkpoint_stack = FakeCloseStack()
    checkpoint_stack, checkpoint_result = run_close(
        (feed_nodes(), switcher, alternate_home, alternate_home),
        checkpoint_stack,
        on_app_stopped=lambda: checkpoint_stack.events.append("app-stopped"),
    )
    check("close-only-card-to-home-result", checkpoint_result is True)
    check(
        "close-only-card-checkpoint-order",
        checkpoint_stack.events
        == [
            "switcher",
            ("flick", (325, 780, 325, 60), {"steps": 16, "ms": 22}),
            ("force", open_reddit.REDDIT_BUNDLE),
            ("running", open_reddit.REDDIT_BUNDLE),
            "home",
            ("running", open_reddit.REDDIT_BUNDLE),
            "app-stopped",
        ],
        repr(checkpoint_stack.events),
    )

    dumps.clear()
    logs.clear()
    resume_stack, resume_result = run_close(
        (alternate_home, switcher, next_card, alternate_home),
        resume_from_home=True,
    )
    check(
        "close-home-resume-exact-card-only",
        resume_result is True
        and resume_stack.events[0] == "switcher"
        and resume_stack.events.count(("force", open_reddit.REDDIT_BUNDLE)) == 1,
    )

    dumps.clear()
    logs.clear()
    resume_adjacent_stack, resume_adjacent_result = run_close(
        (alternate_home, adjacent_switcher, alternate_home),
        resume_from_home=True,
    )
    check(
        "close-home-resume-unrelated-card-uses-exact-bundle-no-flick",
        resume_adjacent_result is True
        and resume_adjacent_stack.events
        == [
            "switcher",
            ("force", open_reddit.REDDIT_BUNDLE),
            ("running", open_reddit.REDDIT_BUNDLE),
            "home",
            ("running", open_reddit.REDDIT_BUNDLE),
        ],
        repr(resume_adjacent_stack.events),
    )

    dumps.clear()
    logs.clear()
    incidental_feed = feed_nodes() + [node("Interests", 90, 540, w=170)]
    incidental_stack, incidental_result = run_close(
        (incidental_feed, switcher, next_card, home),
    )
    check(
        "close-strict-feed-ignores-incidental-protected-word",
        incidental_result is True
        and incidental_stack.events.count(("force", open_reddit.REDDIT_BUNDLE)) == 1,
    )

    dumps.clear()
    logs.clear()
    refused_home_stack = run_close_error(
        "close-home-without-resume-flag-refused",
        (alternate_home,),
        "home feed was not proven",
    )
    check("close-home-without-resume-no-action", refused_home_stack.events == [])

    fixture_before = fixture_nodes("feed_925_before")
    fixture_after = fixture_nodes("feed_939_after")
    fixture_initial_proof = open_reddit.make_feed_cleanup_proof(fixture_before)
    fixture_final_proof = (
        open_reddit.advance_feed_cleanup_proof(fixture_initial_proof, fixture_after)
        if fixture_initial_proof is not None
        else None
    )
    dumps.clear()
    logs.clear()
    proof_stack, proof_result = run_close(
        (fixture_after, switcher, next_card, home),
        feed_proof=fixture_final_proof,
    )
    check(
        "close-shifted-feed-accepts-matching-proof",
        proof_result is True
        and isinstance(fixture_final_proof, open_reddit.FeedCleanupProof)
        and proof_stack.events[0] == "switcher",
    )

    dumps.clear()
    logs.clear()
    missing_proof_stack = run_close_error(
        "close-shifted-feed-without-proof-refused",
        (fixture_after,),
        "home feed was not proven",
    )
    check("close-shifted-feed-without-proof-no-action", missing_proof_stack.events == [])

    live_preclose = fixture_nodes("feed_preclose_numeric_prefix")
    check(
        "close-numeric-prefix-fixture-strict-home-navigation-absent",
        not open_reddit.proven_home_feed(live_preclose),
    )
    check(
        "close-numeric-prefix-fixture-safe-with-many-anchors",
        open_reddit._feed_cleanup_screen_safe(live_preclose)
        and len(
            open_reddit._unique_feed_content_positions(
                live_preclose,
                min_text_length=open_reddit.FEED_SPARSE_MIN_TEXT_LENGTH,
            )
        )
        >= 2,
    )
    check(
        "close-numeric-prefix-attribution-proves-terminal-feed",
        open_reddit._reddit_feed_structural_evidence(live_preclose)
        and isinstance(
            open_reddit.make_terminal_feed_cleanup_proof(live_preclose),
            open_reddit.FeedCleanupProof,
        ),
    )
    for label in (
        "3 reasons r/example is useful",
        "abc r/example 4d",
        "123 r/example 4d",
        "x r/example 4d",
        "ordinary prose mentioning r/example later",
    ):
        check(
            "close-numeric-prefix-rejects-" + label.split()[0].casefold(),
            not open_reddit._reddit_feed_structural_evidence(
                [node(label, 80, 420, w=480)]
            ),
            label,
        )
    protected_numeric_prefix = [
        node("3 r/example 4d", 80, 420, w=220),
        node("Unique feed anchor one", 80, 520, w=360),
        node("Unique feed anchor two", 80, 620, w=360),
        node("Verify your email", 80, 240, w=400),
    ]
    check(
        "close-numeric-prefix-protected-signup-still-rejected",
        open_reddit.make_terminal_feed_cleanup_proof(protected_numeric_prefix) is None,
    )
    check(
        "close-numeric-prefix-springboard-still-rejected",
        open_reddit.make_terminal_feed_cleanup_proof(
            alternate_home
            + [
                node("3 r/example 4d", 80, 420, w=220),
                node("Unique feed anchor one", 80, 520, w=360),
                node("Unique feed anchor two", 80, 620, w=360),
            ]
        )
        is None,
    )
    check(
        "close-unrelated-content-without-reddit-structure-rejected",
        open_reddit.make_terminal_feed_cleanup_proof(
            [
                node("Unrelated application content one", 80, 420, w=420),
                node("Unrelated application content two", 80, 520, w=420),
            ]
        )
        is None,
    )
    check(
        "close-numeric-prefix-content-changed-from-trusted-proof",
        isinstance(fixture_initial_proof, open_reddit.FeedCleanupProof)
        and not open_reddit._stable_feed_frame(
            fixture_initial_proof.reference_nodes,
            live_preclose,
        ),
    )
    dumps.clear()
    logs.clear()
    fresh_terminal_stack = FakeCloseStack()
    fresh_terminal_stack, fresh_terminal_result = run_close(
        (live_preclose, switcher, next_card, home),
        fresh_terminal_stack,
        feed_proof=fixture_initial_proof,
        on_app_stopped=lambda: fresh_terminal_stack.events.append("app-stopped"),
    )
    check(
        "close-numeric-prefix-trusted-proof-authorizes-shutdown",
        fresh_terminal_result is True
        and fresh_terminal_stack.events
        == [
            "switcher",
            ("flick", (325, 780, 325, 60), {"steps": 16, "ms": 22}),
            ("force", open_reddit.REDDIT_BUNDLE),
            ("running", open_reddit.REDDIT_BUNDLE),
            "home",
            ("running", open_reddit.REDDIT_BUNDLE),
            "app-stopped",
        ],
        repr(fresh_terminal_stack.events),
    )
    check(
        "close-numeric-prefix-content-change-is-logged",
        any(
            "post content changed" in item.casefold()
            and "terminal reddit feed" in item.casefold()
            for item in logs
        ),
    )

    dumps.clear()
    logs.clear()
    live_without_proof_stack = run_close_error(
        "close-numeric-prefix-without-trusted-proof-refused",
        (live_preclose,),
        "home feed was not proven",
    )
    check(
        "close-numeric-prefix-without-proof-no-action",
        live_without_proof_stack.events == [],
    )

    dumps.clear()
    logs.clear()
    refreshed_proof_stack, refreshed_proof_result = run_close(
        (fixture_after, switcher, next_card, home),
        feed_proof=fixture_initial_proof,
    )
    check(
        "close-shifted-feed-fresh-terminal-proof-accepts-content-change",
        refreshed_proof_result is True
        and refreshed_proof_stack.events[0] == "switcher",
    )

    unrelated_proof = open_reddit.make_feed_cleanup_proof(feed_frame())
    dumps.clear()
    logs.clear()
    unrelated_proof_stack, unrelated_proof_result = run_close(
        (fixture_after, switcher, next_card, home),
        feed_proof=unrelated_proof,
    )
    check(
        "close-shifted-feed-any-trusted-completed-proof-authorizes-fresh-proof",
        unrelated_proof_result is True
        and unrelated_proof_stack.events[0] == "switcher",
    )

    sparse_before = sparse_feed_frame()
    sparse_after = sparse_feed_frame(offset=-110, navigation=False)
    sparse_initial_proof = open_reddit.make_feed_cleanup_proof(sparse_before)
    sparse_final_proof = (
        open_reddit.advance_feed_cleanup_proof(sparse_initial_proof, sparse_after)
        if sparse_initial_proof is not None
        else None
    )
    dumps.clear()
    logs.clear()
    sparse_proof_stack, sparse_proof_result = run_close(
        (sparse_after, switcher, next_card, home),
        feed_proof=sparse_final_proof,
    )
    check(
        "close-sparse-feed-accepts-matching-final-proof",
        sparse_proof_result is True
        and isinstance(sparse_final_proof, open_reddit.FeedCleanupProof)
        and sparse_proof_stack.events[0] == "switcher",
    )

    dumps.clear()
    logs.clear()
    sparse_without_proof_stack = run_close_error(
        "close-sparse-feed-without-proof-refused",
        (sparse_after,),
        "home feed was not proven",
    )
    check(
        "close-sparse-feed-without-proof-no-action",
        sparse_without_proof_stack.events == [],
    )

    dumps.clear()
    logs.clear()
    sparse_refreshed_proof_stack, sparse_refreshed_proof_result = run_close(
        (sparse_after, switcher, next_card, home),
        feed_proof=sparse_initial_proof,
    )
    check(
        "close-sparse-feed-fresh-terminal-proof-accepts-content-change",
        sparse_refreshed_proof_result is True
        and sparse_refreshed_proof_stack.events[0] == "switcher",
    )

    dumps.clear()
    logs.clear()
    adjacent_stack, adjacent_result = run_close(
        (feed_nodes(), adjacent_switcher, switcher, next_card, alternate_home),
        FakeCloseStack(),
    )
    check("close-adjacent-result", adjacent_result is True)
    adjacent_flicks = [event for event in adjacent_stack.events if isinstance(event, tuple) and event[0] == "flick"]
    check(
        "close-adjacent-one-center-one-dismiss",
        adjacent_flicks
        == [
            ("flick", (650, 700, 250, 700), {"steps": 16, "ms": 22}),
            ("flick", (325, 780, 325, 60), {"steps": 16, "ms": 22}),
        ],
    )
    check(
        "close-force-log",
        any("force close verified stopped" in item.casefold() for item in logs),
    )

    dumps.clear()
    logs.clear()
    failed_clear_stack = run_close_error(
        "close-force-error-propagates",
        (feed_nodes(), switcher, next_card, home),
        "synthetic force-close failure",
        FakeCloseStack(fail_force=True),
    )
    check(
        "close-clear-error-still-home",
        failed_clear_stack.events
        == [
            "switcher",
            ("flick", (325, 780, 325, 60), {"steps": 16, "ms": 22}),
            ("force", open_reddit.REDDIT_BUNDLE),
            "home",
        ],
    )

    dumps.clear()
    logs.clear()
    unchanged_stack = run_close_error(
        "close-switcher-unchanged-refused",
        (feed_nodes(), feed_nodes(), home),
        "left Reddit unchanged",
    )
    check(
        "close-switcher-unchanged-no-flick",
        unchanged_stack.events == ["switcher", "home"],
    )

    dumps.clear()
    logs.clear()
    returned_home_stack = run_close_error(
        "close-switcher-returned-home-refused",
        (feed_nodes(), home, alternate_home),
        "returned Home",
    )
    check(
        "close-switcher-returned-home-no-flick",
        returned_home_stack.events == ["switcher", "home"],
    )

    dumps.clear()
    logs.clear()
    unknown_recents_stack = FakeCloseStack()
    unknown_recents_stack, unknown_recents_result = run_close(
        (feed_nodes(), [node("Unknown", 40, 430, w=160)], home),
        unknown_recents_stack,
        on_app_stopped=lambda: unknown_recents_stack.events.append("app-stopped"),
    )
    check(
        "close-titleless-recents-exact-bundle-no-flick-checkpoints-once",
        unknown_recents_result is True
        and unknown_recents_stack.events
        == [
            "switcher",
            ("force", open_reddit.REDDIT_BUNDLE),
            ("running", open_reddit.REDDIT_BUNDLE),
            "home",
            ("running", open_reddit.REDDIT_BUNDLE),
            "app-stopped",
        ],
        repr(unknown_recents_stack.events),
    )
    check(
        "close-titleless-recents-fallback-is-logged",
        any("title ocr unavailable" in item.casefold() for item in logs),
    )

    dumps.clear()
    logs.clear()
    titleless_failed_checkpoint: list[str] = []
    titleless_failed_stack = run_close_error(
        "close-titleless-exact-bundle-failure-propagates",
        (feed_nodes(), [node("Unknown", 40, 430, w=160)], home),
        "synthetic force-close failure",
        FakeCloseStack(fail_force=True),
        on_app_stopped=lambda: titleless_failed_checkpoint.append("app-stopped"),
    )
    check(
        "close-titleless-failure-keeps-checkpoint-pending-and-returns-home",
        titleless_failed_checkpoint == []
        and titleless_failed_stack.events
        == ["switcher", ("force", open_reddit.REDDIT_BUNDLE), "home"],
        repr(titleless_failed_stack.events),
    )

    dumps.clear()
    logs.clear()
    failed_center_stack = run_close_error(
        "close-adjacent-centering-must-prove-reddit",
        (feed_nodes(), adjacent_switcher, next_card, home),
        "did not become the exact centered",
    )
    check(
        "close-adjacent-centering-only-one-horizontal-flick",
        [event for event in failed_center_stack.events if isinstance(event, tuple) and event[0] == "flick"]
        == [("flick", (650, 700, 250, 700), {"steps": 16, "ms": 22})],
    )

    dumps.clear()
    logs.clear()
    retained_card_stack = run_close_error(
        "close-retained-card-no-retry",
        (feed_nodes(), switcher, switcher, home),
        "removal was not confirmed",
    )
    check(
        "close-retained-card-one-dismiss-flick",
        [event for event in retained_card_stack.events if isinstance(event, tuple) and event[0] == "flick"]
        == [("flick", (325, 780, 325, 60), {"steps": 16, "ms": 22})]
        and ("force", open_reddit.REDDIT_BUNDLE) not in retained_card_stack.events,
    )

    dumps.clear()
    logs.clear()
    final_unverified_stack = run_close_error(
        "close-final-home-must-be-proven",
        (feed_nodes(), switcher, next_card, switcher),
        "final springboard",
    )
    check(
        "close-final-home-unverified-cleared-once",
        final_unverified_stack.events.count(("force", open_reddit.REDDIT_BUNDLE)) == 1,
    )

    dumps.clear()
    logs.clear()
    lone_final_stack = run_close_error(
        "close-lone-reddit-cannot-prove-final-home",
        (feed_nodes(), switcher, next_card, lone_body_reddit),
        "final springboard",
    )
    check(
        "close-lone-reddit-final-proof-cleared-but-not-accepted",
        lone_final_stack.events.count(("force", open_reddit.REDDIT_BUNDLE)) == 1,
    )

    dumps.clear()
    logs.clear()
    restarted_stack = FakeCloseStack(
        running_sequence=[False, True, False],
    )
    restarted_stack, restarted_result = run_close(
        (feed_nodes(), switcher, next_card, home, alternate_home),
        restarted_stack,
        on_app_stopped=lambda: restarted_stack.events.append("app-stopped"),
    )
    check(
        "close-home-transition-restart-is-force-closed-once-more",
        restarted_result is True
        and restarted_stack.events
        == [
            "switcher",
            ("flick", (325, 780, 325, 60), {"steps": 16, "ms": 22}),
            ("force", open_reddit.REDDIT_BUNDLE),
            ("running", open_reddit.REDDIT_BUNDLE),
            "home",
            ("running", open_reddit.REDDIT_BUNDLE),
            ("force", open_reddit.REDDIT_BUNDLE),
            "home",
            ("running", open_reddit.REDDIT_BUNDLE),
            "app-stopped",
        ],
        repr(restarted_stack.events),
    )

    protected_stack = FakeCloseStack()
    open_reddit.dump_named = lambda *args, **kwargs: (
        "synthetic",
        [node("Verify your email", 80, 240, w=400)],
    )
    try:
        expect_phone_error(
            "close-protected-signup-refused",
            lambda: open_reddit.close_reddit(
                protected_stack,
                object(),
                logs.append,
                feed_proof=fixture_final_proof,
            ),
            "refuse close",
        )
    finally:
        open_reddit.dump_named = original_dump
    check("close-protected-no-action", protected_stack.events == [])

    unknown_stack = FakeCloseStack()
    open_reddit.dump_named = lambda *args, **kwargs: (
        "synthetic",
        [node("Reddit", 20, 90, w=160)],
    )
    try:
        expect_phone_error(
            "close-unproven-feed-refused",
            lambda: open_reddit.close_reddit(unknown_stack, object(), logs.append),
            "home feed was not proven",
        )
    finally:
        open_reddit.dump_named = original_dump
    check("close-unproven-feed-no-action", unknown_stack.events == [])


def main() -> int:
    signup_home_recovery_regressions()
    orchestrator_account_neutral_feed_regressions()
    birthday_regressions()
    crane_scroll_regressions()
    feed_regressions()
    close_reddit_regressions()
    print(f"REGRESS {len(PASS)} pass {len(FAIL)} fail", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
