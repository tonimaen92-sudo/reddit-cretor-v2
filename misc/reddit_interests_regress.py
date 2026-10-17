# UTF-8
"""Immutable regressions for Reddit Customize-your-feed recognition and routing."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reddit_interests as interests  # noqa: E402
from ui_tree import Node, parse_nodes  # noqa: E402


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool) -> None:
    (PASS if condition else FAIL).append(name)
    print(("PASS " if condition else "FAIL ") + name, flush=True)


def ocr(text: str, x: int, y: int, w: int, h: int = 20) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=y, w=w, h=h)


def customize_fixture(
    anchor: str = "Show More Topics",
    *,
    anchor_x: int = 46,
    anchor_y: int = 1025,
    include_search: bool = False,
) -> list[Node]:
    rows = [
        ocr("Customize your feed", 125, 180, 490, 35),
    ]
    if include_search:
        rows.append(ocr("Find more of your interests", 53, 364, 468, 28))
    rows.extend([
        ocr("Artificial Intelligence", 50, 447, 262),
        ocr("Smartphones", 369, 449, 168, 18),
        ocr("World News", 222, 511, 156, 18),
        ocr("Football", 431, 511, 106, 19),
        ocr(anchor, anchor_x, anchor_y, 228, 18),
        ocr("Skip", 159, 1229, 68, 23),
        ocr("Continue", 484, 1227, 132, 22),
    ])
    return rows


def grid_fixture() -> list[Node]:
    return [
        ocr("Choose your interests", 116, 180, 518, 35),
        ocr("Art", 106, 483, 56),
        ocr("Beauty", 318, 483, 106),
        ocr("News", 328, 893, 87),
        ocr("Technology", 43, 1097, 178),
        ocr("Skip", 159, 1229, 68, 23),
        ocr("Continue", 484, 1227, 140, 23),
    ]


class FakeStack:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class FixedRandom:
    def sample(self, population, count):
        return list(population)[:count]

    def choice(self, population):
        return list(population)[0]


def main() -> int:
    live_a0038 = customize_fixture(anchor_x=46, anchor_y=1025)
    saved_a0038 = customize_fixture(anchor_x=160, anchor_y=1034)
    observed_a0035 = customize_fixture(
        "Shaw More Tonirc", anchor_x=156, anchor_y=1159
    )
    selected_layout = customize_fixture(anchor_x=160, anchor_y=970)

    check("live-a0038-layout-recognized", interests.customize_screen(live_a0038))
    saved_dump = Path(__file__).resolve().parents[1] / "dumps" / "925_customize_2.json"
    actual_failed_nodes = parse_nodes(saved_dump.read_text(encoding="utf-8"))
    check(
        "a0047-failed-dump-still-customize",
        interests.customize_screen(actual_failed_nodes)
        and len(interests.customize_topics(actual_failed_nodes)) >= 4,
    )
    current_dump = Path(__file__).resolve().parents[1] / "dumps" / "924_after_interests.json"
    current_nodes = parse_nodes(current_dump.read_text(encoding="utf-8"))
    check(
        "a0059-search-anchored-topic-list-recognized",
        interests.customize_screen(current_nodes)
        and interests.customize_search_node(current_nodes) is not None
        and len(interests.customize_topics(current_nodes)) >= 4,
    )
    checked_a0059_path = (
        Path(__file__).resolve().parents[1] / "dumps" / "925_customize_verified_2.json"
    )
    checked_a0059_nodes = parse_nodes(checked_a0059_path.read_text(encoding="utf-8"))
    check(
        "a0059-clipped-slash-checkmark-maps-only-to-pending-tv-dramas",
        interests._selected_customize_topics(
            checked_a0059_nodes,
            "925_customize_verified_2",
            expected_labels={"hair care", "tv dramas"},
            pending={"label": "tv dramas", "bounds": [37, 1148, 156, 18]},
        ) == {"hair care", "tv dramas"},
    )
    check(
        "search-anchor-requires-both-actions",
        not interests.customize_screen(
            [node for node in current_nodes if node.text != "Skip"]
        ),
    )
    check(
        "search-anchor-outside-band-rejected",
        not interests.customize_screen([
            ocr(node.text, node.x, 500 if "Find more" in node.text else node.y, node.w, node.h)
            for node in current_nodes
        ]),
    )
    check(
        "observed-chaw-more-tonic-recognized",
        interests.customize_screen(customize_fixture("Chaw More Tonic")),
    )
    check(
        "selected-checkmark-ocr-normalized",
        interests.customize_topic_label(ocr("~ TV Dramas", 350, 850, 125)) == "tv dramas",
    )
    check("saved-a0038-layout-recognized", interests.customize_screen(saved_a0038))
    check("selected-layout-y970-recognized", interests.customize_screen(selected_layout))
    check(
        "observed-a0035-shaw-more-tonirc-recognized",
        interests.customize_screen(observed_a0035),
    )
    check(
        "observed-anchor-is-bounded",
        interests.show_more_topics_node(observed_a0035) is not None,
    )
    check(
        "missing-customize-title-rejected",
        not interests.customize_screen(observed_a0035[1:]),
    )
    check(
        "missing-skip-rejected",
        not interests.customize_screen(
            [node for node in observed_a0035 if node.text != "Skip"]
        ),
    )
    check(
        "missing-continue-rejected",
        not interests.customize_screen(
            [node for node in observed_a0035 if node.text != "Continue"]
        ),
    )
    check(
        "title-outside-band-rejected",
        not interests.customize_screen(
            [ocr(node.text, node.x, 300 if node.text == "Customize your feed" else node.y, node.w, node.h) for node in live_a0038]
        ),
    )
    check(
        "skip-outside-action-band-rejected",
        not interests.customize_screen(
            [ocr(node.text, node.x, 1100 if node.text == "Skip" else node.y, node.w, node.h) for node in live_a0038]
        ),
    )
    check(
        "continue-outside-action-band-rejected",
        not interests.customize_screen(
            [ocr(node.text, node.x, 1100 if node.text == "Continue" else node.y, node.w, node.h) for node in live_a0038]
        ),
    )
    check(
        "anchor-y819-rejected",
        not interests.customize_screen(customize_fixture(anchor_y=819)),
    )
    check(
        "anchor-y1196-rejected",
        not interests.customize_screen(customize_fixture(anchor_y=1196)),
    )
    check(
        "anchor-x19-rejected",
        not interests.customize_screen(customize_fixture(anchor_x=19)),
    )
    check(
        "anchor-x351-rejected",
        not interests.customize_screen(customize_fixture(anchor_x=351)),
    )
    check(
        "unlisted-malformed-anchor-rejected",
        not interests.customize_screen(customize_fixture("Shaw More Topics")),
    )
    duplicate = live_a0038 + [ocr("Show More Topics", 50, 1050, 228, 18)]
    check("duplicate-anchor-rejected", not interests.customize_screen(duplicate))
    check(
        "recognized-screen-exposes-four-topics",
        len(interests.customize_topics(live_a0038)) == 4,
    )
    moving_topic = customize_fixture(anchor_y=1159)
    moving_topic.insert(-3, ocr("Late Topic", 50, 1080, 150, 18))
    check(
        "topic-bound-follows-anchor",
        "late topic"
        in [interests.collapsed_text(node.labels()) for node in interests.customize_topics(moving_topic)],
    )

    routed: list[tuple[object, object, str, object]] = []
    original_dump = interests.dump_named
    original_pick = interests.pick_customize_feed
    try:
        interests.dump_named = lambda *_args, **_kwargs: ("fixture", live_a0038)

        def fake_pick(stack, phone, sid, *, initial_nodes=None, prior=None):
            routed.append((stack, phone, sid, initial_nodes))
            return ["fixture-topic"]

        interests.pick_customize_feed = fake_pick
        stack = object()
        phone = object()
        result = interests.pick_interests(stack, phone, "a0035")
    finally:
        interests.dump_named = original_dump
        interests.pick_customize_feed = original_pick
    check(
        "observed-frame-routes-to-customize-picker-once",
        result == ["fixture-topic"]
        and routed == [(stack, phone, "a0035", live_a0038)],
    )

    transition_routes: list[object] = []
    original_dump = interests.dump_named
    original_pick = interests.pick_customize_feed
    original_continue = interests.tap_continue_bar
    original_wait = interests._wait_screen_close
    original_sleep = interests.time.sleep
    original_load_checkpoint = interests._load_customize_checkpoint
    original_save_checkpoint = interests._save_customize_checkpoint
    original_record = interests._record_interests
    try:
        initial = grid_fixture()
        interests.dump_named = lambda *_args, **_kwargs: ("fixture", initial)
        interests.tap_continue_bar = lambda *_args, **_kwargs: True
        interests._wait_screen_close = lambda *_args, **_kwargs: live_a0038
        interests.time.sleep = lambda *_args, **_kwargs: None
        interests._load_customize_checkpoint = lambda sid: {
            "version": 1, "slot_id": sid, "initial": [], "customize": [],
        }
        interests._save_customize_checkpoint = lambda *_args: None
        interests._record_interests = lambda *_args: None

        def fake_transition_pick(stack, phone, sid, *, initial_nodes=None, prior=None):
            transition_routes.append((sid, initial_nodes, tuple(prior or ())))
            return list(prior or ()) + ["customize-topic"]

        interests.pick_customize_feed = fake_transition_pick
        transition_stack = FakeStack()
        transition_result = interests.pick_interests(
            transition_stack, object(), "a0038"
        )
    finally:
        interests.dump_named = original_dump
        interests.pick_customize_feed = original_pick
        interests.tap_continue_bar = original_continue
        interests._wait_screen_close = original_wait
        interests.time.sleep = original_sleep
        interests._load_customize_checkpoint = original_load_checkpoint
        interests._save_customize_checkpoint = original_save_checkpoint
        interests._record_interests = original_record
    check(
        "post-first-stage-transition-routes-to-customize-picker",
        len(transition_stack.taps) == 4
        and len(transition_routes) == 1
        and transition_routes[0][0] == "a0038"
        and transition_routes[0][1] is live_a0038
        and len(transition_routes[0][2]) == 4
        and len(transition_result) == 5,
    )

    strict_error = ""
    strict_stack = FakeStack()
    original_dump = interests.dump_named
    try:
        malformed = customize_fixture("Shaw More Topics")
        interests.dump_named = lambda *_args, **_kwargs: ("fixture", malformed)
        try:
            interests.pick_interests(strict_stack, object(), "a0038")
        except interests.PhoneError as exc:
            strict_error = str(exc)
    finally:
        interests.dump_named = original_dump
    check(
        "unproven-customize-stops-locally-without-tap",
        "title is visible" in strict_error and strict_stack.taps == [],
    )

    with tempfile.TemporaryDirectory() as temporary:
        stem = "selected_fixture"
        Path(temporary, stem + ".json").write_text("[]", encoding="utf-8")
        image = Image.new("RGB", (750, 1334), "white")
        draw = ImageDraw.Draw(image)
        for node in interests.customize_topics(live_a0038):
            if node.text in {"Artificial Intelligence", "World News"}:
                cx, cy = node.center()
                draw.rectangle((cx - 40, cy - 22, cx + 40, cy + 22), fill=(201, 215, 222))
        image.save(Path(temporary, stem + ".png"))
        original_dir = interests.DUMP_DIR
        try:
            interests.DUMP_DIR = temporary
            detected = interests._selected_customize_topics(live_a0038, stem)
            draw.rectangle((140, 430, 225, 455), fill=(130, 130, 130))
            image.save(Path(temporary, stem + ".png"))
            ambiguous = ""
            try:
                interests._selected_customize_topics(live_a0038, stem)
            except interests.PhoneError as exc:
                ambiguous = str(exc)

            v_stem = "ascii_v_selected_fixture"
            Path(temporary, v_stem + ".json").write_text("[]", encoding="utf-8")
            v_nodes = customize_fixture(anchor_y=970)
            v_nodes = [
                ocr("V Football", node.x, node.y, node.w + 12, node.h)
                if node.text == "Football" else node
                for node in v_nodes
            ]
            v_image = Image.new("RGB", (750, 1334), "white")
            v_draw = ImageDraw.Draw(v_image)
            for node in interests.customize_topics(v_nodes):
                if node.text in {"Artificial Intelligence", "V Football"}:
                    cx, cy = node.center()
                    v_draw.rectangle(
                        (cx - 40, cy - 22, cx + 40, cy + 22),
                        fill=(201, 215, 222),
                    )
            v_image.save(Path(temporary, v_stem + ".png"))
            v_reconciled = interests._selected_customize_topics(
                v_nodes, v_stem, {"artificial intelligence", "football"}
            )
            v_literal = interests._selected_customize_topics(v_nodes, v_stem)
        finally:
            interests.DUMP_DIR = original_dir
    check(
        "screenshot-identifies-existing-selected-pills",
        detected == {"artificial intelligence", "world news"},
    )
    check(
        "a0047-two-selections-verified-from-stable-screenshot",
        interests._selected_customize_topics(
            parse_nodes((Path(__file__).resolve().parents[1] / "dumps" / "a0047_pre_resume.json").read_text(encoding="utf-8")),
            "a0047_pre_resume",
        ) == {"budgeting & personal finance", "homebrewing"},
    )
    a0048_nodes = parse_nodes(
        (Path(__file__).resolve().parents[1] / "dumps" / "a0048_customize_pre_resume.json").read_text(encoding="utf-8")
    )
    check(
        "a0048-public-transit-unselected-with-tall-glyphs",
        interests._selected_customize_topics(a0048_nodes, "a0048_customize_pre_resume") == set(),
    )
    check(
        "a0051-ascii-v-checkmark-reconciled-from-checkpoint",
        v_reconciled == {"artificial intelligence", "football"},
    )
    check(
        "ascii-v-prefix-not-stripped-without-exact-checkpoint-match",
        "v football" in v_literal,
    )
    check("ambiguous-pill-color-stops", "ambiguous" in ambiguous)

    a0055_after_path = Path(interests.DUMP_DIR) / "a0055_recovery_fresh.json"
    a0055_after = parse_nodes(a0055_after_path.read_text(encoding="utf-8"))
    smartphone = next(
        node for node in interests.customize_topics(a0055_after)
        if interests.customize_topic_label(node) == "smartohones"
    )
    check(
        "a0055-recovery-evidence-retains-smartohones-confusion",
        interests.customize_screen(a0055_after)
        and interests.customize_topic_label(smartphone) == "smartohones",
    )

    a0056_before_path = Path(interests.DUMP_DIR) / "a0056_formula1_before.json"
    a0056_after_path = Path(interests.DUMP_DIR) / "a0056_formula1_after.json"
    a0056_before = parse_nodes(a0056_before_path.read_text(encoding="utf-8"))
    a0056_after = parse_nodes(a0056_after_path.read_text(encoding="utf-8"))
    formula_one = next(
        node for node in interests.customize_topics(a0056_before)
        if interests.customize_topic_label(node) == "formula 1"
    )
    a0056_selected = interests._selected_customize_topics(
        a0056_after,
        "a0056_formula1_after",
        {"makeup", "nba", "tennis", "formula 1"},
        pending={
            "label": "formula 1",
            "bounds": [formula_one.x, formula_one.y, formula_one.w, formula_one.h],
        },
    )
    check(
        "a0056-exact-formula1-survives-selected-pill-reflow",
        a0056_selected == {"makeup", "nba", "tennis", "formula 1"},
    )

    original_capture = interests._capture_customize_state
    original_continue = interests.tap_continue_bar
    original_wait = interests._wait_screen_close
    original_record = interests._record_interests
    original_advance = interests.advance_post_interests
    original_sleep = interests.time.sleep
    original_random = interests.random.SystemRandom
    original_load = interests._load_customize_checkpoint
    original_save = interests._save_customize_checkpoint
    continue_calls: list[bool] = []
    saved_states: list[dict] = []
    try:
        interests.tap_continue_bar = lambda *_args, **_kwargs: continue_calls.append(True) or True
        interests._wait_screen_close = lambda *_args, **_kwargs: []
        interests._record_interests = lambda *_args, **_kwargs: None
        interests.advance_post_interests = lambda *_args, **_kwargs: []
        interests.time.sleep = lambda *_args, **_kwargs: None
        interests.random.SystemRandom = FixedRandom
        interests._load_customize_checkpoint = lambda sid: {
            "version": 1, "slot_id": sid, "initial": [], "customize": [],
        }
        interests._save_customize_checkpoint = lambda _sid, state: saved_states.append(dict(state, customize=list(state["customize"])))
        names_by_tap = {
            node.center(): interests.collapsed_text(node.labels())
            for node in interests.customize_topics(live_a0038)
        }
        interests._capture_customize_state = lambda stack, *_args, **_kwargs: (
            live_a0038, {names_by_tap[tap] for tap in stack.taps},
        )
        complete_stack = FakeStack()
        complete_result = interests.pick_customize_feed(complete_stack, object(), "a0038")
        partial_stack = FakeStack()
        previously_selected = {"artificial intelligence", "smartphones"}
        interests._capture_customize_state = lambda stack, *_args, **_kwargs: (
            live_a0038,
            previously_selected | {names_by_tap[tap] for tap in stack.taps},
        )
        partial_result = interests.pick_customize_feed(partial_stack, object(), "a0038")
    finally:
        interests._capture_customize_state = original_capture
        interests.tap_continue_bar = original_continue
        interests._wait_screen_close = original_wait
        interests._record_interests = original_record
        interests.advance_post_interests = original_advance
        interests.time.sleep = original_sleep
        interests.random.SystemRandom = original_random
        interests._load_customize_checkpoint = original_load
        interests._save_customize_checkpoint = original_save
    check(
        "four-verified-taps-and-one-continue",
        len(complete_stack.taps) == 4
        and len(complete_result) == 4
        and len(continue_calls) >= 1
        and any(
            state["customize"] == sorted(complete_result)
            for state in saved_states
        ),
    )
    check(
        "partial-resume-only-taps-two-new-pills",
        len(partial_stack.taps) == 2
        and len(partial_result) == 4
        and all(names_by_tap[tap] not in previously_selected for tap in partial_stack.taps)
        and len(continue_calls) == 2,
    )

    original_capture = interests._capture_customize_state
    original_load = interests._load_customize_checkpoint
    try:
        interests._load_customize_checkpoint = lambda sid: {
            "version": 1, "slot_id": sid, "initial": [], "customize": [],
        }
        interests._capture_customize_state = lambda *_args: (_ for _ in ()).throw(
            interests.PhoneError("Customize screenshot is ambiguous; no topic was tapped.")
        )
        no_tap_stack = FakeStack()
        no_tap_error = ""
        try:
            interests.pick_customize_feed(no_tap_stack, object(), "a0038")
        except interests.PhoneError as exc:
            no_tap_error = str(exc)
    finally:
        interests._capture_customize_state = original_capture
        interests._load_customize_checkpoint = original_load
    check(
        "ambiguous-live-screen-does-not-tap",
        "ambiguous" in no_tap_error and no_tap_stack.taps == [],
    )

    print(f"REDDIT INTERESTS REGRESS {len(PASS)} pass {len(FAIL)} fail", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
