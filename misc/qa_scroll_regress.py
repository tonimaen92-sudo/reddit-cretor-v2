# UTF-8
"""Offline regressions for the standalone twelve-swipe QA test."""

from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_scroll_test as qa  # noqa: E402
from ssh_device import PhoneError  # noqa: E402
from ui_tree import Node  # noqa: E402


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(("PASS " if condition else "FAIL ") + name + ((" :: " + detail) if detail else ""), flush=True)


def node(text: str, x: int, center_y: int, *, w: int = 80, h: int = 20) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=center_y - h // 2, w=w, h=h)


def feed_nodes(*, shift: int = 0, navigation: bool = True) -> list[Node]:
    nodes = [
        node(f"Unique content anchor {index}", 45, 250 + index * 145 + shift, w=500)
        for index in range(4)
        if 150 <= 250 + index * 145 + shift < 900
    ]
    if navigation:
        nodes += [node("Home", 35, 1200, w=90), node("Inbox", 610, 1200, w=100)]
    return nodes


def springboard_nodes(*, with_reddit: bool = True) -> list[Node]:
    nodes = [
        node("Photos", 55, 300, w=90), node("Camera", 225, 300, w=100),
        node("Clock", 395, 500, w=80), node("Settings", 565, 500, w=110),
        node("Q Search", 315, 1115, w=120),
    ]
    if with_reddit:
        nodes.append(node("Reddit", 270, 820, w=100))
    return nodes


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeStack:
    def __init__(self, clock: FakeClock, *, fail_at: int | None = None, running=False) -> None:
        self.clock = clock
        self.fail_at = fail_at
        self.running = running
        self.flicks: list[tuple[tuple, dict]] = []
        self.actions: list[object] = []
        self.process_checks: list[str] = []

    def interp_flick(self, *args, **kwargs) -> None:
        self.flicks.append((args, kwargs))
        if self.fail_at == len(self.flicks):
            raise PhoneError("synthetic gesture failure")
        self.clock.sleep(float(kwargs.get("settle_seconds", 0.0)))

    def wait(self, seconds: float) -> None:
        self.clock.sleep(seconds)

    def clear_app_memory(self, bundle: str) -> str:
        self.actions.append(("clear", bundle))
        return "graceful-quit"

    def app_is_running(self, bundle: str) -> bool:
        self.process_checks.append(bundle)
        return self.running

    def press_home(self) -> None:
        self.actions.append("home")

    def tap(self, *_args, **_kwargs) -> None:
        raise AssertionError("QA test must not tap")

    def tap_xy(self, *_args, **_kwargs) -> None:
        self.actions.append(("tap", _args))


def run_case(
    initial, cycles, *, cleanup=None, fail_at=None, running=False,
    cleanup_on_error=False, launch_frames=None, slot_name="",
    container_opener=None, swipe_count=qa.QA_CYCLES,
    replacement_prover=None,
):
    clock = FakeClock()
    stack = FakeStack(clock, fail_at=fail_at, running=running)
    logs: list[str] = []
    dumps: list[tuple[tuple, dict]] = []
    launch = list(launch_frames) if launch_frames is not None else [initial]
    prefix = [] if slot_name else [springboard_nodes()]
    frames = prefix + launch + list(cycles) + [
        springboard_nodes() if cleanup is None else cleanup
    ]

    def fake_dump(*args, **kwargs):
        dumps.append((args, kwargs))
        return "synthetic", frames[min(len(dumps) - 1, len(frames) - 1)]

    result = None
    error = ""
    try:
        result = qa.run_qa_scroll_test(
            stack, object(), logs.append, clock=clock, sleeper=clock.sleep, dump_fn=fake_dump,
            cleanup_on_error=cleanup_on_error,
            slot_name=slot_name,
            container_opener=container_opener,
            swipe_count=swipe_count,
            replacement_prover=replacement_prover,
        )
    except PhoneError as exc:
        error = str(exc)
    return stack, clock, logs, dumps, result, error


def main() -> int:
    strict = feed_nodes()
    proof = qa.make_qa_feed_proof(strict)
    check("strict-feed-builds-proof", proof is not None)
    check("strict-feed-advances", proof is not None and qa.advance_qa_feed_proof(proof, strict) is not None)

    shifted = feed_nodes(shift=-80, navigation=False)
    shifted_proof = qa.advance_qa_feed_proof(proof, shifted) if proof else None
    check("hidden-navigation-continuity", shifted_proof is not None)
    check("stable-frame-continuity", shifted_proof is not None and qa.advance_qa_feed_proof(shifted_proof, shifted) is not None)

    incoherent = [
        node("Unique content anchor 0", 250, 700, w=500),
        node("Unique content anchor 1", 45, 210, w=500),
    ]
    check("incoherent-motion-rejected", proof is not None and qa.advance_qa_feed_proof(proof, incoherent) is None)
    protected = strict + [node("Verify your email", 80, 400, w=400)]
    check("protected-screen-rejected", not qa.proven_qa_main_screen(protected))
    check("springboard-rejected-as-feed", proof is not None and qa.advance_qa_feed_proof(proof, springboard_nodes()) is None)
    icon = qa.qa_app_icon(springboard_nodes())
    check("exact-springboard-icon-found", icon is not None and icon.text == "Reddit")
    check("missing-springboard-icon-rejected", qa.qa_app_icon(springboard_nodes(with_reddit=False)) is None)

    parsed = qa.parse_qa_options([
        "--simulated-success", "--cleanup-on-error", "--noninteractive",
        "--slot", "A0001",
    ])
    check("slot-option-normalized", parsed == (True, True, "a0001", 12))
    invalid_options = 0
    for args in (
        ["--slot"], ["--slot", "bad"], ["--unknown"],
        ["--swipes", "13"], ["--swipes", "251"], ["--swipes", "many"],
    ):
        try:
            qa.parse_qa_options(list(args))
        except ValueError:
            invalid_options += 1
    check("invalid-slot-options-rejected", invalid_options == 6)
    check(
        "swipe-count-boundaries",
        qa.parse_qa_options(["--swipes", "14"])[3] == 14
        and qa.parse_qa_options(["--swipes", "250"])[3] == 250,
    )

    nominal = run_case(strict, [strict] * 3)
    stack, clock, logs, dumps, result, error = nominal
    check("nominal-no-error", not error, error)
    check("exactly-twelve-swipes", qa.QA_CYCLES == 12 and len(stack.flicks) == 12)
    check(
        "long-vertical-geometry",
        qa.QA_END_Y < qa.QA_START_Y and qa.QA_START_Y - qa.QA_END_Y == 450
        and all(args == (qa.QA_X, qa.QA_START_Y, qa.QA_X, qa.QA_END_Y)
                and kwargs == {
                    "steps": qa.QA_STEPS,
                    "ms": qa.QA_STEP_MS,
                    "settle_seconds": qa.QA_SETTLE_SECONDS,
                }
                for args, kwargs in stack.flicks),
    )
    check(
        "faster-sixteen-step-gesture",
        qa.QA_STEPS == 16 and qa.QA_STEP_MS == 18,
    )
    check(
        "launch-wait-plus-fixed-settle-count",
        clock.sleeps
        == [qa.QA_LAUNCH_POLL_SECONDS] + [qa.QA_SETTLE_SECONDS] * 12,
    )
    check(
        "five-swipe-checkpoints-only",
        len(dumps) == 6 and all(kwargs == {"shot": False, "persist": False, "preview": False}
                                 for _args, kwargs in dumps),
    )
    check(
        "exact-process-cleanup-and-home",
        stack.actions[0] == "home"
        and stack.actions[1][0] == "tap"
        and stack.actions[2:] == [("clear", qa.QA_APP_BUNDLE), "home"]
        and stack.process_checks == [qa.QA_APP_BUNDLE]
        and logs[-1] == "TEST COMPLETE — cleanup executed (graceful-quit)",
    )
    check(
        "launch-occurs-before-first-swipe",
        logs.index("Opening test app from its proven Home-screen icon")
        < logs.index("Starting feed swipe 1/12"),
    )
    check("result-count", result is not None and result.cycles_completed == 12)
    check(
        "qa-mode-progress-logs",
        "Starting feed swipe 1/12" in logs
        and "Starting feed swipe 12/12" in logs
        and "Feed checkpoint 5: Reddit feed structure proven." in logs
        and "Feed checkpoint 10: Reddit feed structure proven." in logs
        and "Feed checkpoint 12: Reddit feed structure proven." in logs
        and "Swipe sequence completed" in logs
        and "Closing app" in logs,
    )

    selected_calls: list[tuple] = []

    def selected_opener(*args, **kwargs):
        selected_calls.append((args, kwargs))

    selected = run_case(
        strict,
        [strict] * 3,
        slot_name="a0001",
        container_opener=selected_opener,
    )
    check(
        "selected-container-opener-called-once",
        not selected[-1]
        and len(selected_calls) == 1
        and selected_calls[0][0][3] == "a0001"
        and selected_calls[0][0][4] == ""
        and selected_calls[0][1] == {"allow_existing_sparse_feed": True},
        selected[-1],
    )
    check(
        "selected-container-before-swipes",
        selected[2].index("Selecting QA container a0001")
        < selected[2].index("Starting feed swipe 1/12")
        and len(selected[0].flicks) == qa.QA_CYCLES,
    )
    check(
        "selected-container-skips-direct-icon-tap",
        all(not (isinstance(action, tuple) and action[0] == "tap")
            for action in selected[0].actions),
    )

    fourteen = run_case(
        strict, [strict] * 3, swipe_count=14,
    )
    check(
        "configured-fourteen-swipes",
        not fourteen[-1]
        and len(fourteen[0].flicks) == 14
        and fourteen[4] is not None
        and fourteen[4].cycles_completed == 14,
        fourteen[-1],
    )

    replacement = [
        node(f"Replacement content anchor {index}", 45, 240 + index * 140, w=520)
        for index in range(4)
    ] + [node("Home", 35, 1200, w=90), node("Inbox", 610, 1200, w=100)]
    replacement_case = run_case(
        strict,
        [replacement, strict, strict],
        swipe_count=14,
    )
    check(
        "replacement-content-structure-continues",
        not replacement_case[-1]
        and len(replacement_case[0].flicks) == 14
        and any("Feed checkpoint 5" in line for line in replacement_case[2]),
        replacement_case[-1],
    )

    def failed_opener(*_args, **_kwargs):
        raise PhoneError("synthetic container selection failure")

    selected_failure = run_case(
        strict,
        [strict] * 3,
        slot_name="a0001",
        container_opener=failed_opener,
    )
    check(
        "container-failure-zero-swipes",
        "synthetic container selection failure" in selected_failure[-1]
        and not selected_failure[0].flicks,
    )

    continuity_chain = [strict, strict, strict]
    chained = run_case(strict, continuity_chain)
    check("twelve-frame-continuity-chain", not chained[-1] and len(chained[0].flicks) == 12, chained[-1])

    unknown = [node("Unknown screen", 100, 500, w=300)]
    initial_unknown = run_case(
        unknown, [strict] * 3,
        launch_frames=[unknown] * qa.QA_LAUNCH_POLLS,
    )
    check("unknown-initial-zero-gestures", "did not reach a proven main feed" in initial_unknown[-1] and not initial_unknown[0].flicks)
    cycle_unknown = run_case(
        strict,
        [unknown] * qa.QA_FRAME_RETRIES,
    )
    check(
        "unknown-checkpoint-stops-before-next-group",
        "not proven after gesture 5" in cycle_unknown[-1]
        and len(cycle_unknown[0].flicks) == 5
        and cycle_unknown[0].actions[0] == "home"
        and cycle_unknown[0].actions[1][0] == "tap"
        and len(cycle_unknown[0].actions) == 2,
    )

    forced_cleanup = run_case(
        strict, [unknown] * qa.QA_FRAME_RETRIES, cleanup_on_error=True,
    )
    check(
        "qa-mode-error-still-cleans-up",
        "not proven after gesture 5" in forced_cleanup[-1]
        and len(forced_cleanup[0].flicks) == 5
        and forced_cleanup[0].actions[0] == "home"
        and forced_cleanup[0].actions[1][0] == "tap"
        and forced_cleanup[0].actions[2:] == [("clear", qa.QA_APP_BUNDLE), "home"]
        and forced_cleanup[0].process_checks == [qa.QA_APP_BUNDLE]
        and any(line.startswith("QA swipe error:") for line in forced_cleanup[2]),
        forced_cleanup[-1],
    )

    failed = run_case(strict, [strict] * 3, fail_at=5)
    check(
        "gesture-failure-stops-immediately",
        "gesture 5 failed" in failed[-1]
        and len(failed[0].flicks) == 5
        and len(failed[3]) == 2
        and failed[0].actions[0] == "home"
        and failed[0].actions[1][0] == "tap"
        and len(failed[0].actions) == 2,
    )

    running = run_case(strict, [strict] * 3, running=True)
    check("running-process-blocks-home", "process is still running" in running[-1] and running[0].actions[-1] == ("clear", qa.QA_APP_BUNDLE) and running[0].actions.count("home") == 1)
    cleanup_unknown = run_case(strict, [strict] * 3, cleanup=unknown)
    check("home-proof-required", "Home screen was not proven" in cleanup_unknown[-1] and cleanup_unknown[0].actions[-2:] == [("clear", qa.QA_APP_BUNDLE), "home"])

    source = inspect.getsource(qa)
    forbidden = (
        "mark_reddit_created(", "reddit_create_day1", "reddit_interests",
        "SystemRandom", "import random",
    )
    check("qa-no-creation-coupling", all(token not in source for token in forbidden))
    check("no-tap-call-in-runner", ".tap(" not in inspect.getsource(qa.run_scroll_session) and ".tap_xy(" not in inspect.getsource(qa.run_scroll_session))

    print(f"QA REGRESS {len(PASS)} pass {len(FAIL)} fail", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
