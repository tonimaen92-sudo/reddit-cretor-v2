# UTF-8
"""Offline fixture regressions for email-pill OCR validation only."""

from __future__ import annotations

import os
import sys
from friend_poster.reddit import reddit_to_verify as validator  # noqa: E402
from friend_poster.device.ssh_device import PhoneError  # noqa: E402
from friend_poster.device.ui_tree import Node  # noqa: E402


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool) -> None:
    (PASS if condition else FAIL).append(name)
    print(("PASS " if condition else "FAIL ") + name, flush=True)


def ocr(text: str, x: int, y: int, w: int, h: int = 30) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=y, w=w, h=h)


def synthetic_email(text: str, *, width: int = 500) -> list[Node]:
    return [
        ocr("Hi new friend,", 250, 280, 340),
        ocr("Enter your email to get started", 250, 410, 480),
        ocr("Email", 90, 515, 70, 18),
        ocr(text, 80, 540, width),
    ]


def main() -> int:
    expected = "fixture_name9h@icloud.com"

    check("valid-full-address", validator.email_ready(synthetic_email(expected), expected))
    check(
        "truncated-domain-rejected",
        not validator.email_ready(synthetic_email("fixture_name9h@icloud.co"), expected),
    )
    check(
        "narrow-width-is-diagnostic-not-validity",
        validator.email_ready(synthetic_email(expected, width=329), expected),
    )
    check(
        "isolated-address-outside-proven-form-rejected",
        not validator.email_ready([ocr(expected, 80, 530, 500)], expected),
    )
    warning = synthetic_email(expected) + [
        ocr("Please enter a valid email", 90, 600, 420),
    ]
    check("invalid-warning-rejected", not validator.email_ready(warning, expected))

    # Immutable a0279 OCR shape: the exact prepared address contains one
    # underscore, while live OCR renders only that underscore as whitespace.
    underscore_expected = "pebble_42fixture@icloud.com"
    underscore_as_space = synthetic_email("pebble 42fixture@icloud.com")
    underscore_report = validator._email_validation_report(
        underscore_as_space, underscore_expected
    )
    check("underscore-read-as-space-is-accepted", underscore_report.valid)
    check(
        "underscore-space-equivalence-is-classified",
        underscore_report.ocr_equivalence == "underscore-read-as-space",
    )
    check(
        "unrelated-space-difference-remains-rejected",
        not validator.email_ready(
            synthetic_email("pebble x2fixture@icloud.com"),
            underscore_expected,
        ),
    )
    check(
        "underscore-space-with-invalid-warning-is-rejected",
        not validator.email_ready(
            underscore_as_space + [ocr("Please enter a valid email", 90, 600, 420)],
            underscore_expected,
        ),
    )

    form_context = synthetic_email(expected)[:-1]
    split = form_context + [
        ocr("fixture_name9h@", 70, 530, 270),
        ocr("icloud.com", 350, 531, 220),
    ]
    check("adjacent-split-tokens-merged", validator.email_ready(split, expected))
    separated = form_context + [
        ocr("fixture_name9h@", 70, 530, 270),
        ocr("icloud.com", 500, 590, 220),
    ]
    check("nonadjacent-tokens-not-merged", not validator.email_ready(separated, expected))

    check(
        "unrelated-i-l-confusion-rejected",
        not validator.email_ready(synthetic_email("fixture_namelh@icloud.com"), expected),
    )
    check(
        "unlisted-o-zero-confusion-rejected",
        not validator.email_ready(synthetic_email("fixture_name9h@icl0ud.com"), expected),
    )
    check(
        "single-eight-s-allowance-retained",
        validator.email_ready(
            synthetic_email("fixture_namesh@icloud.com"),
            "fixture_name8h@icloud.com",
        ),
    )

    # Immutable capture of the a0054 failure shape. The live dump showed the
    # saved digit 1 as lowercase l, inside the bounded Reddit field, with the
    # email keyboard open and no invalid-email warning.
    one_expected = "fixture_draftee.1e@icloud.com"
    one_as_l_nodes = [
        ocr("Hi new friend,", 370, 200, 334, 41),
        ocr("welcome to Reddit", 368, 244, 444, 30),
        ocr("Create an account to get started", 368, 316, 506, 25),
        ocr("Email", 95, 424, 72, 15),
        ocr("fixture_draftee.le@icloud.com", 303, 455, 500, 30),
        ocr("done", 653, 1168, 81, 23),
    ]
    one_report = validator._email_validation_report(one_as_l_nodes, one_expected)
    check("a0054-digit-one-read-as-l-is-accepted", one_report.valid)
    check(
        "a0054-confusion-is-classified-without-address",
        one_report.ocr_equivalence == "digit-1-read-as-letter-l-or-i",
    )
    check(
        "uppercase-i-normalizes-to-approved-one-confusion",
        validator.email_ready(
            synthetic_email("fixture_draftee.Ie@icloud.com"), one_expected
        ),
    )
    check(
        "reverse-letter-l-read-as-one-is-rejected",
        not validator.email_ready(
            synthetic_email("fixture_draftee.1e@icloud.com"),
            "fixture_draftee.le@icloud.com",
        ),
    )
    check(
        "two-one-letter-confusions-are-rejected",
        not validator.email_ready(
            synthetic_email("fixture_l_draftee.le@icloud.com"),
            "fixture_1_draftee.1e@icloud.com",
        ),
    )

    # Immutable shape from a0061: the saved address contained one additional
    # local-part letter, but the field/dump showed the rest of the exact value
    # and domain.  It must remain invalid and may authorize only one clear +
    # retype; it must never be accepted as the saved address.
    missing_expected = "fixture_grands.6h@icloud.com"
    missing_nodes = [
        ocr("Hi new friend,", 370, 200, 334, 41),
        ocr("welcome to Reddit", 368, 244, 444, 30),
        ocr("Create an account to get started", 370, 316, 509, 25),
        ocr("Email", 95, 424, 72, 15),
        ocr("fixture_rands.6h@icloud.com", 292, 455, 478, 30),
        ocr("done", 653, 1168, 81, 23),
    ]
    check(
        "a0061-one-missing-character-is-never-accepted",
        not validator.email_ready(missing_nodes, missing_expected),
    )
    check(
        "a0061-one-missing-character-is-proven-for-retype-only",
        validator._single_missing_saved_email_char(missing_nodes, missing_expected)
        == (8, "g"),
    )
    check(
        "two-missing-characters-do-not-authorize-retype",
        validator._single_missing_saved_email_char(
            [
                *missing_nodes[:-2],
                ocr("fixture_rands.6@icloud.com", 292, 455, 470, 30),
                missing_nodes[-1],
            ],
            missing_expected,
        )
        is None,
    )

    class RetypeStack:
        def __init__(self) -> None:
            self.taps: list[tuple[int, int]] = []
            self.backspaces: list[int] = []
            self.typed: list[str] = []

        def tap_xy(self, x: int, y: int) -> None:
            self.taps.append((x, y))

        def press_backspace(self, count: int) -> None:
            self.backspaces.append(count)

        def type_text(self, text: str) -> None:
            self.typed.append(text)

    retype_stack = RetypeStack()
    retype_count = [1]
    original_wait = validator._wait
    try:
        validator._wait = lambda *_args, **_kwargs: None
        retype_result = validator.step(
            retype_stack, object(), missing_nodes, missing_expected,
            [True], "a0061", retype_count, [1], [0], [0],
            email_done_taps=[0],
        )
    finally:
        validator._wait = original_wait
    check(
        "a0061-missing-character-clears-and-retypes-exactly-once",
        retype_result == "RETYPED"
        and retype_count == [2]
        and retype_stack.taps == [(620, 431)]
        and retype_stack.backspaces == [60, 20]
        and retype_stack.typed == [missing_expected],
    )

    # Immutable replacement for the former mutable dumps/873_* dependency.
    # Runtime diagnostic dumps are overwritten by later failures and therefore
    # cannot be regression fixtures.
    fixture_expected = "fixture.saved_8h@icloud.com"
    nodes = [
        ocr("Hi new friend,", 367, 278, 340, 38),
        ocr("Enter your email to get started", 368, 407, 474, 28),
        ocr("Email", 93, 415, 68, 18),
        ocr(fixture_expected, 281, 440, 453, 33),
        ocr("done", 653, 1168, 81, 23),
    ]
    report = validator._email_validation_report(nodes, fixture_expected)
    check("immutable-shifted-keyboard-fixture-passes", report.valid)
    check("immutable-fixture-width-is-diagnostic-only", report.width == 453)
    check("immutable-fixture-has-structural-field-context", report.field_context)
    check("immutable-fixture-confidence-unavailable", report.confidence is None)
    check(
        "immutable-fixture-canonical-ocr-contract",
        report.bbox is not None and report.bbox[1] == 440,
    )

    live_expected = "liners.gelcap.0q@icloud.com"
    live_observed = "liners.gelcap.Oq@icloud.com"
    live_nodes = synthetic_email(live_observed, width=450) + [
        ocr("Continue", 370, 739, 140, 23),
        ocr("done", 653, 1168, 81, 23),
    ]
    check(
        "live-a0034-single-zero-read-as-o-is-accepted",
        validator.email_ready(live_nodes, live_expected),
    )
    check(
        "reverse-zero-for-letter-o-is-rejected",
        not validator.email_ready(
            synthetic_email("liners.gelcap.0q@icloud.com"),
            "liners.gelcap.oq@icloud.com",
        ),
    )
    check(
        "two-zero-read-as-o-differences-are-rejected",
        not validator.email_ready(
            synthetic_email("linersOgelcap.Oq@icloud.com"),
            "liners0gelcap.0q@icloud.com",
        ),
    )
    check(
        "unrelated-single-local-part-difference-is-rejected",
        not validator.email_ready(
            synthetic_email("liners.gelcap.xq@icloud.com"),
            live_expected,
        ),
    )

    class FakeStack:
        def __init__(self) -> None:
            self.taps: list[tuple[int, int]] = []

        def tap_xy(self, x: int, y: int) -> None:
            self.taps.append((x, y))

    one_resume_stack = FakeStack()
    one_done_count = [0]
    one_submit_count = [0]
    original_wait = validator._wait
    try:
        validator._wait = lambda *_args, **_kwargs: None
        one_done_result = validator.step(
            one_resume_stack,
            object(),
            one_as_l_nodes,
            one_expected,
            [True],
            "a0054",
            [1],
            [1],
            one_submit_count,
            [0],
            email_done_taps=one_done_count,
        )
        one_after_done = [
            node
            for node in one_as_l_nodes
            if str(node.text or "").casefold() != "done"
        ] + [ocr("Continue", 300, 790, 150, 23)]
        one_continue_result = validator.step(
            one_resume_stack,
            object(),
            one_after_done,
            one_expected,
            [True],
            "a0054",
            [1],
            [1],
            one_submit_count,
            [0],
            email_done_taps=one_done_count,
        )
    finally:
        validator._wait = original_wait
    one_done = validator.keyboard_done_btn(one_as_l_nodes)
    one_continue = validator.continue_btn(one_after_done)
    check(
        "a0054-resume-does-not-retype-and-taps-done-then-continue-once",
        one_done_result == "KEYBOARD_DONE"
        and one_continue_result == "TAP"
        and one_done is not None
        and one_continue is not None
        and one_resume_stack.taps == [one_done.center(), one_continue.center()]
        and one_done_count == [1]
        and one_submit_count == [1],
    )

    bottom_continue_nodes = [
        node
        for node in one_as_l_nodes
        if str(node.text or "").casefold() != "done"
    ] + [ocr("Continue", 300, 1211, 137, 23)]
    bottom_continue = validator.continue_btn(bottom_continue_nodes)
    check(
        "a0054-bottom-continue-layout-is-bounded-and-accepted",
        bottom_continue is not None and bottom_continue.center() == (368, 1222),
    )
    check(
        "continue-below-email-form-action-band-is-rejected",
        validator.continue_btn(
            synthetic_email(one_expected) + [ocr("Continue", 300, 1260, 137, 23)]
        )
        is None,
    )
    check(
        "continue-outside-horizontal-action-band-is-rejected",
        validator.continue_btn(
            synthetic_email(one_expected) + [ocr("Continue", 40, 1211, 137, 23)]
        )
        is None,
    )

    live_stack = FakeStack()
    live_submit_count = [0]
    original_wait = validator._wait
    try:
        validator._wait = lambda *_args, **_kwargs: None
        live_result = validator.step(
            live_stack,
            object(),
            live_nodes,
            live_expected,
            [True],
            "a0034",
            [1],
            [1],
            live_submit_count,
            [0],
            email_done_taps=[0],
        )
    finally:
        validator._wait = original_wait
    live_continue = validator.continue_btn(live_nodes)
    check(
        "live-a0034-resume-taps-only-exact-continue-once",
        live_result == "TAP"
        and live_continue is not None
        and live_stack.taps == [live_continue.center()]
        and live_submit_count == [1],
    )

    stack = FakeStack()
    submit_count = [0]
    done_count = [0]
    original_wait = validator._wait
    try:
        validator._wait = lambda *_args, **_kwargs: None
        result = validator.step(
            stack,
            object(),
            nodes,
            fixture_expected,
            [True],
            "a0008",
            [1],
            [1],
            submit_count,
            [0],
            email_done_taps=done_count,
        )
    finally:
        validator._wait = original_wait
    done_node = validator.keyboard_done_btn(nodes)
    check(
        "keyboard-done-is-not-misclassified-as-prior-submit-transition",
        not validator.email_transition_pending(nodes, fixture_expected),
    )
    check(
        "saved-valid-email-taps-keyboard-done-once",
        result == "KEYBOARD_DONE"
        and done_node is not None
        and stack.taps == [done_node.center()]
        and done_count == [1]
        and submit_count == [0],
    )

    repeat_done_stack = FakeStack()
    repeat_done_error = ""
    original_wait = validator._wait
    try:
        validator._wait = lambda *_args, **_kwargs: None
        validator.step(
            repeat_done_stack, object(), nodes, fixture_expected,
            [True], "a0014", [1], [1], [0], [0],
            email_done_taps=[1],
        )
    except PhoneError as exc:
        repeat_done_error = str(exc)
    finally:
        validator._wait = original_wait
    check(
        "keyboard-done-is-never-tapped-twice",
        "already tapped once" in repeat_done_error.casefold()
        and repeat_done_stack.taps == [],
    )

    continue_frame = [item for item in nodes if item.y < 900] + [
        ocr("Continue", 300, 790, 150, 23)
    ]
    continue_node = validator.continue_btn(continue_frame)
    original_wait = validator._wait
    try:
        validator._wait = lambda *_args, **_kwargs: None
        continue_result = validator.step(
            stack, object(), continue_frame, fixture_expected,
            [True], "a0014", [1], [1], submit_count, [0],
            email_done_taps=done_count,
        )
    finally:
        validator._wait = original_wait
    check(
        "after-done-valid-email-taps-exact-continue-once",
        continue_result == "TAP"
        and continue_node is not None
        and stack.taps == [done_node.center(), continue_node.center()]
        and submit_count == [1],
    )

    ambiguous = continue_frame + [ocr("Continue", 340, 790, 140, 23)]
    check(
        "ambiguous-continue-is-not-authorized",
        validator.continue_btn(ambiguous) is None,
    )

    without_continue = [
        item
        for item in nodes
        if item.y < 900 and str(item.text or "").casefold() != "continue"
    ]
    check(
        "valid-email-with-keyboard-down-is-submit-transition",
        validator.email_transition_pending(without_continue, fixture_expected),
    )
    missing_stack = FakeStack()
    missing_waits = [0]
    original_wait = validator._wait
    try:
        validator._wait = lambda *_args, **_kwargs: None
        missing_result = validator.step(
            missing_stack, object(), without_continue, fixture_expected,
            [True], "a0008", [1], [1], [0], missing_waits,
        )
    finally:
        validator._wait = original_wait
    check(
        "valid-email-with-missing-continue-waits-without-tap",
        missing_result == "WAIT" and missing_stack.taps == [] and missing_waits == [1],
    )

    duplicate_stack = FakeStack()
    duplicate_waits = [0]
    original_wait = validator._wait
    try:
        validator._wait = lambda *_args, **_kwargs: None
        duplicate_result = validator.step(
            duplicate_stack, object(), continue_frame, fixture_expected,
            [True], "a0008", [1], [1], [1], duplicate_waits,
        )
    finally:
        validator._wait = original_wait
    check(
        "submitted-email-never-taps-continue-twice",
        duplicate_result == "WAIT"
        and duplicate_stack.taps == []
        and duplicate_waits == [1],
    )

    diagnostic_lines: list[str] = []
    original_log = validator.log
    try:
        validator.log = diagnostic_lines.append
        validator._log_email_validation_failure(
            synthetic_email(expected, width=329) + [
                ocr("Please enter a valid email", 90, 600, 420)
            ],
            expected,
        )
    finally:
        validator.log = original_log
    check(
        "failure-diagnostics-are-independent-and-redacted",
        len(diagnostic_lines) == 4
        and expected not in " ".join(diagnostic_lines)
        and any("OCR=***@icloud.com" in line for line in diagnostic_lines)
        and any("suffix_match=true" in line for line in diagnostic_lines)
        and any("width=329 width_ok=false" in line for line in diagnostic_lines)
        and any("field_context=true" in line for line in diagnostic_lines)
        and any("invalid_warning=true" in line for line in diagnostic_lines)
        and any("confidence=unavailable" in line for line in diagnostic_lines),
    )

    print(f"EMAIL VALIDATOR REGRESS {len(PASS)} pass {len(FAIL)} fail", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
