# UTF-8
"""Offline regressions for the shared creation/Combined-QA swipe engine."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feed_swipe as feed  # noqa: E402
from ssh_device import PhoneError  # noqa: E402
from ui_tree import Node, OperationCancelled  # noqa: E402


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(("PASS " if condition else "FAIL ") + name + ((" :: " + detail) if detail else ""), flush=True)


def node(text: str, x: int, center_y: int, *, w: int = 120, h: int = 20) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=center_y - h // 2, w=w, h=h)


def feed_nodes(prefix: str = "Post") -> list[Node]:
    return [
        node(f"{prefix} content alpha", 45, 260, w=520),
        node(f"{prefix} content beta", 45, 480, w=520),
        node(f"{prefix} content gamma", 45, 700, w=520),
        node("Home", 35, 1200, w=90),
        node("Inbox", 610, 1200, w=100),
    ]


def springboard_nodes() -> list[Node]:
    return [
        node("Photos", 55, 300), node("Camera", 225, 300),
        node("Clock", 395, 500), node("Settings", 565, 500),
        node("Q Search", 315, 1115),
    ]


class Stop:
    def __init__(self) -> None:
        self.value = False

    def is_set(self) -> bool:
        return self.value


class Stack:
    def __init__(self) -> None:
        self.flicks: list[tuple[tuple, dict]] = []
        self.waits: list[float] = []

    def interp_flick(self, *args, **kwargs) -> None:
        self.flicks.append((args, kwargs))

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)


class PredictableRandom:
    def sample(self, population, count: int):
        assert count == 3
        available = list(population)
        assert all(position in available for position in (2, 5, 9))
        return [2, 5, 9]

    def uniform(self, low: float, high: float) -> float:
        return (low + high) / 2


def run_engine(
    *, target: int, completed: int = 0, frames=None, stop=None,
    progress_fn=None, strict=True, continue_on_unclear=False,
):
    stack = Stack()
    logs: list[str] = []
    stems: list[str] = []
    supplied = list(frames or [])

    def dump_fn(_stack, _phone, stem, _logger, **_kwargs):
        stems.append(stem)
        index = min(len(stems) - 1, len(supplied) - 1)
        return "synthetic", supplied[index]

    result = None
    error = ""
    try:
        result = feed.run_feed_swipes(
            stack,
            object(),
            logs.append,
            target=target,
            completed=completed,
            stop=stop,
            progress_fn=progress_fn,
            require_final_proof=strict,
            continue_on_unclear=continue_on_unclear,
            dump_fn=dump_fn,
        )
    except (PhoneError, OperationCancelled) as exc:
        error = str(exc)
    return stack, logs, stems, result, error


def main() -> int:
    checkpoint_frames = [feed_nodes(f"Group {n}") for n in (5, 10, 15, 20, 25, 30)]
    thirty = run_engine(target=30, frames=checkpoint_frames)
    stack, logs, stems, result, error = thirty
    check("thirty-swipes-complete", not error and len(stack.flicks) == 30, error)
    check(
        "checkpoint-cadence-5-through-30",
        stems == [f"creation_feed_swipe_{n:03d}" for n in (5, 10, 15, 20, 25, 30)],
        repr(stems),
    )
    check(
        "faster-longer-gesture-exact",
        all(
            args == (375, 970, 375, 520)
            and kwargs == {"steps": 16, "ms": 18, "settle_seconds": 0.35}
            for args, kwargs in stack.flicks
        ),
    )
    check("strict-final-proof-returned", result is not None and result.final_proof is not None)

    timed_stack = Stack()
    timed_logs: list[str] = []

    def timed_dump(_stack, _phone, _stem, _logger, **_kwargs):
        return "synthetic", feed_nodes("Timed")

    feed.run_feed_swipes(
        timed_stack,
        object(),
        timed_logs.append,
        target=10,
        pause_count=3,
        rng=PredictableRandom(),
        require_final_proof=False,
        dump_fn=timed_dump,
    )
    check(
        "dynamic-random-pauses-and-natural-delays",
        timed_stack.waits.count(5.0) == 3
        and timed_stack.waits.count(2.5) == 6
        and sum("🛑 Taking a random pause" in line for line in timed_logs) == 3,
        repr(timed_stack.waits),
    )
    fixed_stack = Stack()
    feed.run_feed_swipes(
        fixed_stack, object(), lambda _message: None,
        target=3, fixed_delay_seconds=4.0, pause_count=0,
        require_final_proof=False,
        dump_fn=timed_dump,
    )
    check("bulk-fixed-delay-only-between-swipes", fixed_stack.waits == [4.0, 4.0], repr(fixed_stack.waits))
    rejected_pause_count = False
    try:
        feed.normalize_feed_pause_count(11, available_swipes=10)
    except ValueError:
        rejected_pause_count = True
    check("pause-count-cannot-exceed-session-swipes", rejected_pause_count)

    replacement = run_engine(
        target=14,
        frames=[feed_nodes("Entirely new 5"), feed_nodes("Entirely new 10"), feed_nodes("Entirely new 14")],
        strict=False,
    )
    check(
        "replaced-post-text-does-not-abort-feed",
        not replacement[-1] and len(replacement[0].flicks) == 14,
        replacement[-1],
    )

    protected = run_engine(
        target=30,
        frames=[[node("Verify your email", 80, 400, w=400)]],
        strict=False,
    )
    check(
        "protected-screen-stops-at-checkpoint",
        "protected Reddit screen" in protected[-1] and len(protected[0].flicks) == 5,
        protected[-1],
    )
    home = run_engine(target=30, frames=[springboard_nodes()], strict=False)
    check(
        "springboard-stops-at-checkpoint",
        "iPhone Home appeared" in home[-1] and len(home[0].flicks) == 5,
        home[-1],
    )
    feed_with_slot_like_text = feed_nodes("Ordinary Reddit") + [
        node("a0003", 80, 360, w=120),
        node("a0008", 420, 620, w=120),
    ]
    false_crane = run_engine(
        target=5,
        frames=[feed_with_slot_like_text],
        strict=False,
    )
    check(
        "incidental-slot-like-feed-text-is-not-crane",
        not false_crane[-1] and len(false_crane[0].flicks) == 5,
        false_crane[-1],
    )
    compact_crane = [
        node(f"a{index:04d}", 220, 220 + index * 80, w=150)
        for index in range(1, 10)
    ]
    proven_crane = run_engine(target=20, frames=[compact_crane], strict=False)
    check(
        "dense-canonical-crane-list-still-stops",
        "Crane container UI appeared" in proven_crane[-1]
        and len(proven_crane[0].flicks) == 5,
        proven_crane[-1],
    )
    titled_crane = run_engine(
        target=20,
        frames=[[node("Select Container", 210, 150, w=320)]],
        strict=False,
    )
    check(
        "explicit-crane-title-still-stops",
        "Crane container UI appeared" in titled_crane[-1]
        and len(titled_crane[0].flicks) == 5,
        titled_crane[-1],
    )
    unclear = run_engine(
        target=10,
        frames=(
            [[node("Unknown screen", 100, 500, w=300)]] * 3
            + [feed_nodes("Ten")]
        ),
        strict=False,
        continue_on_unclear=True,
    )
    check(
        "ambiguous-checkpoint-warns-and-continues",
        not unclear[-1]
        and len(unclear[0].flicks) == 10
        and len(unclear[2]) == 4
        and any("configured swipe sequence will continue" in line for line in unclear[1]),
        unclear[-1],
    )

    unclear_final = run_engine(
        target=5,
        frames=[[node("Unknown screen", 100, 500, w=300)]] * 5,
        continue_on_unclear=True,
    )
    check(
        "unclear-final-after-completion-requires-exact-bundle-cleanup",
        not unclear_final[-1]
        and unclear_final[3] is not None
        and unclear_final[3].completed == 5
        and unclear_final[3].final_proof is None
        and len(unclear_final[0].flicks) == 5
        and any("exact-bundle Reddit cleanup is required" in line for line in unclear_final[1]),
        unclear_final[-1],
    )

    strict_unclear_final = run_engine(
        target=5,
        frames=[[node("Unknown screen", 100, 500, w=300)]] * 5,
        continue_on_unclear=False,
    )
    check(
        "strict-unclear-final-still-refuses",
        "feed structure was not proven" in strict_unclear_final[-1].casefold()
        and strict_unclear_final[3] is None
        and len(strict_unclear_final[0].flicks) == 5,
        strict_unclear_final[-1],
    )

    unlimited = run_engine(
        target=251,
        completed=250,
        frames=[feed_nodes("Large target")],
        strict=False,
    )
    check(
        "creation-target-has-no-fixed-maximum",
        not unlimited[-1]
        and len(unlimited[0].flicks) == 1
        and unlimited[3] is not None
        and unlimited[3].completed == 251,
        unlimited[-1],
    )

    stopped = Stop()
    stopped.value = True
    before = run_engine(target=30, frames=checkpoint_frames, stop=stopped, strict=False)
    check("stop-before-first-gesture", "Stopped by user" in before[-1] and not before[0].flicks)

    progress: list[int] = []
    interrupted_stop = Stop()

    def interrupt_at_twelve(completed: int) -> None:
        progress.append(completed)
        if completed == 12:
            interrupted_stop.value = True

    interrupted = run_engine(
        target=30,
        frames=[feed_nodes("Five"), feed_nodes("Ten")],
        stop=interrupted_stop,
        progress_fn=interrupt_at_twelve,
        strict=False,
    )
    check(
        "interruption-persists-twelve",
        "Stopped by user" in interrupted[-1]
        and progress[-1] == 12
        and len(interrupted[0].flicks) == 12,
        interrupted[-1],
    )
    resumed_progress: list[int] = []
    resumed = run_engine(
        target=30,
        completed=12,
        frames=[feed_nodes("Fifteen"), feed_nodes("Twenty"), feed_nodes("Twentyfive"), feed_nodes("Thirty")],
        progress_fn=resumed_progress.append,
    )
    check(
        "resume-starts-at-thirteen-and-finishes-thirty",
        not resumed[-1]
        and len(resumed[0].flicks) == 18
        and resumed_progress[0] == 13
        and resumed_progress[-1] == 30
        and resumed[3] is not None
        and resumed[3].completed == 30,
        resumed[-1],
    )

    print(f"FEED SWIPE REGRESS {len(PASS)} pass {len(FAIL)} fail", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
