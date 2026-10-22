# UTF-8
"""Offline regressions for the Reddit generated-username fallback only.

Synthetic nodes, saved dumps, and fakes only: no phone, USB, SSH, or network.
Run: python misc/reddit_username_regress.py
"""

from __future__ import annotations

import inspect
import os
import sys
from friend_poster.reddit import reddit_username as username  # noqa: E402
from friend_poster.device.ssh_device import PhoneError  # noqa: E402
from friend_poster.device.ui_tree import Node  # noqa: E402


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


def node(text: str, center_y: int, *, x: int = 80, w: int = 260) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=center_y - 12, w=w, h=24)


def username_screen(
    choices: tuple[str, ...] = ("FirstName101", "Second_Name202", "Third-Name303"),
    *,
    refresh: str = "C See more options",
) -> list[Node]:
    nodes = [
        node("Create your username", 200, x=100, w=520),
        node("Username", 426, x=60, w=170),
        node("Need inspiration? Check out these usernames:", 534, x=30, w=660),
    ]
    nodes.extend(node(value, 602 + index * 80) for index, value in enumerate(choices))
    nodes.extend(
        [
            node(refresh, 890),
            node("Continue", 1213, x=300, w=140),
        ]
    )
    return nodes


def filled_username_screen(
    name: str = "AlreadyLoaded123",
    choices: tuple[str, ...] = ("FirstName101", "Second_Name202", "Third-Name303"),
) -> list[Node]:
    nodes = username_screen(choices)
    nodes.append(node(name, 446, x=250, w=240))
    return nodes


class FakeStack:
    def __init__(self, events: list[str] | None = None) -> None:
        self.taps: list[tuple[int, int]] = []
        self.typed: list[str] = []
        self.events = events

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        if self.events is not None:
            self.events.append(f"tap:{x},{y}")

    def type_text(self, text: str) -> None:
        self.typed.append(text)
        if self.events is not None:
            self.events.append("type:" + text)


def expect_phone_error(name: str, call, contains: str = "") -> None:
    try:
        call()
    except PhoneError as exc:
        check(name, not contains or contains.casefold() in str(exc).casefold(), str(exc))
    else:
        check(name, False, "PhoneError was not raised")


LIVE_CHOICES = ("Fun-Sherbert7144", "Professional-Art4070", "Due_Attention560")


def run_keep_case(
    after_nodes: list[Node],
    *,
    filled: bool = False,
    chip_populates: bool = False,
    typed_value: str | None = "Professional-Art4070",
    capture_error: bool = False,
):
    initial = filled_username_screen() if filled else username_screen(())
    refreshed = username_screen(LIVE_CHOICES)
    chip_result = (
        filled_username_screen("Professional-Art4070", LIVE_CHOICES)
        if chip_populates
        else list(refreshed)
    )
    frames = [initial, after_nodes] if filled else [initial, refreshed, chip_result]
    if not filled and not chip_populates:
        frames.append(
            filled_username_screen(typed_value, LIVE_CHOICES)
            if typed_value
            else list(refreshed)
        )
    if not filled:
        frames.append(after_nodes)
    events: list[str] = []
    writes: list[tuple] = []
    stack = FakeStack(events)

    originals = {
        "dump_named": username.dump_named,
        "get_slot": username.get_slot,
        "set_slot_username": username.set_slot_username,
        "write_account_text": username.write_account_text,
        "claim_username": username.claim_username,
        "choose": username._choose_generated_username,
        "sleep": username.time.sleep,
        "log": username.log,
    }

    def fake_dump(_stack, _phone, stem, _log, shot=True):
        events.append("dump:" + stem)
        return "fixture", frames.pop(0)

    try:
        username.dump_named = fake_dump
        username.get_slot = lambda _sid: {"email": "fixture@icloud.com"}
        username.set_slot_username = lambda sid, name, _log: (
            events.append("set:" + name),
            writes.append(("set", sid, name)),
        )
        username.write_account_text = lambda sid, email, name: (
            events.append("write:" + name),
            writes.append(("write", sid, email, name)),
            "fixture-account.txt",
        )[-1]
        username.claim_username = lambda name, sid: {
            "username": name, "slot_id": sid, "status": "processing",
        }
        username._choose_generated_username = lambda choices: choices[1]
        username.time.sleep = lambda _seconds: None
        username.log = lambda message: events.append("log:" + message)
        try:
            result = username.keep_suggested_username(stack, object(), "a0001")
        except PhoneError as exc:
            if not capture_error:
                raise
            result = exc
        return result, stack, events, writes
    finally:
        username.dump_named = originals["dump_named"]
        username.get_slot = originals["get_slot"]
        username.set_slot_username = originals["set_slot_username"]
        username.write_account_text = originals["write_account_text"]
        username.claim_username = originals["claim_username"]
        username._choose_generated_username = originals["choose"]
        username.time.sleep = originals["sleep"]
        username.log = originals["log"]


def main() -> int:
    fixture = username_screen(())
    live_choices = username_screen(LIVE_CHOICES)
    refresh = username.suggestion_refresh(fixture)
    check(
        "live-layout-y890-icon-refresh-recognized",
        refresh is not None and refresh.center()[1] == 890,
    )
    check(
        "live-layout-three-generated-choices",
        [value for _node, value in username.generated_username_choices(live_choices)]
        == list(LIVE_CHOICES),
    )

    for label in (
        "See more options",
        "C See more options",
        "See more usernames",
        "R See more usernames",
        "Reload",
        "C Reload",
    ):
        check(
            "refresh-alias-" + label.casefold().replace(" ", "-"),
            username.suggestion_refresh(username_screen(refresh=label)) is not None,
        )

    missing_page = username_screen()[1:]
    check("refresh-requires-username-page", username.suggestion_refresh(missing_page) is None)
    outside = username_screen(refresh="Reload")
    outside[-2].y = 1240
    check("refresh-must-be-above-continue", username.suggestion_refresh(outside) is None)

    four = username_screen(("OneName101", "TwoName202", "ThreeName303", "FourName404"))
    check("four-generated-choices-accepted", len(username.generated_username_choices(four)) == 4)
    check(
        "two-generated-choices-rejected",
        username.generated_username_choices(username_screen(("OneName101", "TwoName202"))) == [],
    )
    five = username_screen(
        ("OneName101", "TwoName202", "ThreeName303", "FourName404", "FiveName505")
    )
    five[-2].y = 1060
    check("five-generated-choices-rejected", username.generated_username_choices(five) == [])
    malformed = username_screen(("ValidName101", "bad name", "https://bad", "AnotherName202"))
    check("malformed-choice-set-rejected", username.generated_username_choices(malformed) == [])
    duplicates = username_screen(("SameName101", "samename101", "ThirdName303"))
    check("duplicate-choice-set-rejected", username.generated_username_choices(duplicates) == [])

    stack = FakeStack()
    dumps: list[str] = []
    frames = [
        live_choices,
        list(live_choices),
        filled_username_screen("Due_Attention560", LIVE_CHOICES),
    ]
    original_dump = username.dump_named
    original_choose = username._choose_generated_username
    original_sleep = username.time.sleep
    original_log = username.log
    try:
        username.dump_named = lambda _s, _p, stem, _l, shot=True: (
            dumps.append(stem) or "fixture",
            frames.pop(0),
        )
        username._choose_generated_username = lambda choices: choices[2]
        username.time.sleep = lambda _seconds: None
        username.log = lambda _message: None
        loaded, fresh, selected = username._load_suggested_username(stack, object(), fixture)
    finally:
        username.dump_named = original_dump
        username._choose_generated_username = original_choose
        username.time.sleep = original_sleep
        username.log = original_log
    check("fallback-selected-requested-random-row", loaded == "Due_Attention560" and selected)
    check("fallback-refresh-choice-and-field-taps", len(stack.taps) == 3)
    check("fallback-types-exact-selected-name", stack.typed == ["Due_Attention560"])
    check(
        "fallback-uses-fresh-dumps",
        dumps == [
            "890_username_refresh_1",
            "890_username_choice_1",
            "890_username_typed_1",
        ]
        and fresh is not fixture,
    )

    happy = filled_username_screen()
    happy_stack = FakeStack()
    loaded, returned, selected = username._load_suggested_username(happy_stack, object(), happy)
    check(
        "filled-field-keeps-happy-path",
        loaded == "AlreadyLoaded123" and returned is happy and not selected and not happy_stack.taps,
    )

    invalid = username_screen(("OnlyOne101", "OnlyTwo202"))
    exhaustion_frames = [invalid, invalid]
    exhausted_stack = FakeStack()
    original_dump = username.dump_named
    original_sleep = username.time.sleep
    original_log = username.log
    try:
        username.dump_named = lambda *_args, **_kwargs: ("fixture", exhaustion_frames.pop(0))
        username.time.sleep = lambda _seconds: None
        username.log = lambda _message: None
        loaded, _fresh, selected = username._load_suggested_username(
            exhausted_stack, object(), username_screen()
        )
    finally:
        username.dump_named = original_dump
        username.time.sleep = original_sleep
        username.log = original_log
    check(
        "bounded-two-refresh-exhaustion",
        loaded is None and not selected and len(exhausted_stack.taps) == 2,
    )

    password = [node("Set a password", 210), node("Password", 345), node("Continue", 1220)]
    result, success_stack, events, writes = run_keep_case(password)
    password_dump_index = events.index("dump:891_after_username")
    first_write_index = min(index for index, event in enumerate(events) if event.startswith("set:"))
    check("fallback-returns-random-reddit-name", result == "Professional-Art4070")
    check("fallback-taps-refresh-choice-field-continue", len(success_stack.taps) == 4)
    check("fallback-types-selected-name-on-noop-chip", success_stack.typed == [result])
    check("fallback-claims-before-continue", first_write_index < password_dump_index)
    check("fallback-persists-slot-and-account-once", len(writes) == 2)

    chip_result, chip_stack, _chip_events, chip_writes = run_keep_case(
        password, chip_populates=True
    )
    check("successful-chip-selection-keeps-generated-name", chip_result == "Professional-Art4070")
    check("successful-chip-selection-does-not-type", chip_stack.typed == [])
    check("successful-chip-selection-taps-refresh-choice-continue", len(chip_stack.taps) == 3)
    check("successful-chip-selection-persists-once", len(chip_writes) == 2)

    typed_error, typed_stack, typed_events, typed_writes = run_keep_case(
        password, typed_value=None, capture_error=True
    )
    check(
        "typing-failure-stops-before-continue",
        isinstance(typed_error, PhoneError)
        and "did not tap continue" in str(typed_error).casefold()
        and "dump:891_after_username" not in typed_events
        and len(typed_stack.taps) == 3,
    )
    check("typing-failure-is-not-persisted", typed_writes == [])

    failed_result, _stack, _events, failed_writes = run_keep_case(
        live_choices, capture_error=True
    )
    failed_message = str(failed_result) if isinstance(failed_result, PhoneError) else ""
    check(
        "failed-navigation-keeps-processing-claim-only",
        "processing claim was preserved" in failed_message.casefold()
        and failed_writes == [("set", "a0001", "Professional-Art4070")],
        failed_message,
    )

    result, happy_keep_stack, happy_events, happy_writes = run_keep_case([], filled=True)
    check("keep-happy-path-still-returns-field-value", result == "AlreadyLoaded123")
    check("keep-happy-path-does-not-refresh-or-pick", len(happy_keep_stack.taps) == 1)
    check(
        "keep-happy-path-preserves-record-before-continue",
        len(happy_writes) == 2
        and happy_events.index("set:AlreadyLoaded123")
        < next(index for index, event in enumerate(happy_events) if event.startswith("tap:")),
    )

    check(
        "public-loader-signature-unchanged",
        tuple(inspect.signature(username.load_suggested_username).parameters)
        == ("stack", "phone", "nodes"),
    )
    check(
        "public-keep-signature-unchanged",
        tuple(inspect.signature(username.keep_suggested_username).parameters)
        == ("stack", "phone", "sid"),
    )

    print(f"REDDIT USERNAME REGRESS {len(PASS)} pass {len(FAIL)} fail", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
