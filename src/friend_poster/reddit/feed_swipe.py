# UTF-8
"""Shared bounded feed swipes for creation and Combined QA.

The caller must prove the Reddit feed before entering this module.  Checkpoints
reject SpringBoard and protected flows, but do not require prior post text to
remain visible after a long flick.  Creation callers can additionally require a
fresh strict cleanup proof before closing Reddit.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from friend_poster.reddit.reddit_open import (
    FeedCleanupProof,
    crane_haptic_parent,
    crane_submenu_marker,
    make_feed_cleanup_proof,
    make_terminal_feed_cleanup_proof,
    normalized_slot_window,
    protected_reddit_marker,
    proven_reddit_feed_structure,
    proven_springboard,
)
from friend_poster.device.ssh_device import Phone, PhoneError
from friend_poster.device.ui_tree import OperationCancelled, TapStack, dump_named, find_label_exact


FEED_SWIPE_MIN = 1
FEED_SWIPE_CHECKPOINT = 5
FEED_SWIPE_X = 375
FEED_SWIPE_START_Y = 970
FEED_SWIPE_END_Y = 520
FEED_SWIPE_STEPS = 16
FEED_SWIPE_STEP_MS = 18
FEED_SWIPE_SETTLE_SECONDS = 0.35
FEED_SWIPE_FRAME_RETRIES = 3
FEED_SWIPE_RETRY_SECONDS = 0.35
FEED_SWIPE_DELAY_MIN_SECONDS = 1.5
FEED_SWIPE_DELAY_MAX_SECONDS = 3.5
FEED_SWIPE_PAUSE_MIN_SECONDS = 3.0
FEED_SWIPE_PAUSE_MAX_SECONDS = 7.0


@dataclass(frozen=True, slots=True)
class FeedSwipeResult:
    target: int
    completed: int
    final_proof: FeedCleanupProof | None


def normalize_feed_swipe_count(value: int, *, allow_zero: bool = False) -> int:
    count = int(value)
    if allow_zero and count == 0:
        return 0
    if count < FEED_SWIPE_MIN:
        raise ValueError("Feed swipe count must be a positive whole number")
    return count


def normalize_feed_pause_count(value: int, *, available_swipes: int) -> int:
    """Validate the number of unique random pauses for this swipe session."""
    count = int(value)
    available = int(available_swipes)
    if count < 0:
        raise ValueError("Feed pause count must be zero or a positive whole number")
    if count > available:
        raise ValueError(
            "Feed pause count cannot be greater than the swipes remaining in this session"
        )
    return count


def _check_stop(stop) -> None:
    is_set = getattr(stop, "is_set", None)
    if callable(is_set) and is_set():
        raise OperationCancelled("Stopped by user.")


def _wait_interruptibly(stack: TapStack, seconds: float, stop=None) -> None:
    """Wait without making the panel's Stop button unresponsive."""
    _check_stop(stop)
    waiter = getattr(stop, "wait", None)
    if callable(waiter):
        if waiter(max(0.0, float(seconds))):
            raise OperationCancelled("Stopped by user.")
    else:
        stack.wait(max(0.0, float(seconds)))
    _check_stop(stop)


def _checkpoint_error(nodes) -> str:
    if proven_springboard(nodes):
        return "iPhone Home appeared"
    if _proven_crane_checkpoint(nodes):
        return "Crane container UI appeared"
    marker = protected_reddit_marker(nodes)
    if marker:
        return f"protected Reddit screen '{marker}' appeared"
    return ""


def _proven_crane_checkpoint(nodes) -> bool:
    """Recognize Crane without treating incidental feed text as slot rows.

    The general Crane list classifier is intentionally permissive after the
    automation has opened Crane. Feed checkpoints have no such context, so
    they require an explicit UI marker or a dense canonical a0001... prefix.
    """
    if find_label_exact(nodes, ("Select Container",)) is not None:
        return True

    visible = normalized_slot_window(nodes)
    if crane_haptic_parent(nodes) and (
        crane_submenu_marker(nodes) is not None or bool(visible)
    ):
        return True

    indices = {
        int(value[1:])
        for value in visible
        if len(value) == 5 and value.startswith("a") and value[1:].isdigit()
    }
    if len(indices) < 4:
        return False
    maximum = max(indices)
    return indices == set(range(1, maximum + 1))


def _dump_checkpoint(
    stack: TapStack,
    phone: Phone,
    logger: Callable[[str], None],
    completed: int,
    *,
    stop=None,
    continue_on_unclear: bool = False,
    dump_fn=dump_named,
):
    last_nodes = []
    for capture in range(1, FEED_SWIPE_FRAME_RETRIES + 1):
        _check_stop(stop)
        suffix = "" if capture == 1 else f"_retry_{capture}"
        _blob, nodes = dump_fn(
            stack,
            phone,
            f"creation_feed_swipe_{completed:03d}{suffix}",
            logger,
            shot=False,
            persist=False,
            preview=False,
        )
        last_nodes = nodes
        error = _checkpoint_error(nodes)
        if error:
            raise PhoneError(
                f"Feed swipe checkpoint after gesture {completed} stopped because {error}."
            )
        if proven_reddit_feed_structure(nodes):
            logger(f"Feed checkpoint {completed}: Reddit feed structure proven.")
            return nodes
        if capture < FEED_SWIPE_FRAME_RETRIES:
            logger(
                f"Feed checkpoint {completed}: OCR structure unclear; retrying "
                f"without another gesture ({capture + 1}/{FEED_SWIPE_FRAME_RETRIES})."
            )
            stack.wait(FEED_SWIPE_RETRY_SECONDS)
    if continue_on_unclear:
        logger(
            f"Feed checkpoint {completed}: OCR structure remained unclear after "
            f"{FEED_SWIPE_FRAME_RETRIES} captures; no unsafe screen was detected, "
            "so the configured swipe sequence will continue."
        )
        return last_nodes
    raise PhoneError(
        f"Reddit feed structure was not proven after gesture {completed}; "
        "no later gesture was sent."
    )


def _strict_final_proof(
    stack: TapStack,
    phone: Phone,
    logger: Callable[[str], None],
    nodes,
    *,
    stop=None,
    allow_unclear_completion: bool = False,
    dump_fn=dump_named,
) -> FeedCleanupProof | None:
    candidate_nodes = nodes
    for capture in range(1, FEED_SWIPE_FRAME_RETRIES + 1):
        _check_stop(stop)
        proof = make_feed_cleanup_proof(candidate_nodes)
        if proof is None:
            proof = make_terminal_feed_cleanup_proof(candidate_nodes)
        if proof is not None:
            logger("Final post-swipe feed proof established for exact cleanup.")
            return proof
        if capture == FEED_SWIPE_FRAME_RETRIES:
            break
        stack.wait(FEED_SWIPE_RETRY_SECONDS)
        _check_stop(stop)
        _blob, candidate_nodes = dump_fn(
            stack,
            phone,
            f"creation_feed_final_retry_{capture + 1}",
            logger,
            shot=False,
            persist=False,
            preview=False,
        )
        error = _checkpoint_error(candidate_nodes)
        if error:
            raise PhoneError(f"Final feed proof stopped because {error}.")
    if allow_unclear_completion:
        logger(
            "Final Reddit feed cleanup proof remained unclear after all configured "
            "swipes completed; exact-bundle Reddit cleanup is required."
        )
        return None
    raise PhoneError(
        "Final Reddit feed cleanup proof remained unclear. Account creation and "
        "swipe progress were saved; Reddit was not closed from stale evidence."
    )


def run_feed_swipes(
    stack: TapStack,
    phone: Phone,
    logger: Callable[[str], None],
    *,
    target: int,
    completed: int = 0,
    stop=None,
    progress_fn: Callable[[int], None] | None = None,
    initial_proof: FeedCleanupProof | None = None,
    require_final_proof: bool = True,
    continue_on_unclear: bool = False,
    dump_fn=dump_named,
    pause_count: int = 0,
    fixed_delay_seconds: float | None = None,
    rng=None,
) -> FeedSwipeResult:
    target = int(target)
    if target < 0:
        raise ValueError("Feed swipe target must be zero or a positive whole number")
    completed = int(completed)
    if completed < 0 or completed > target:
        raise ValueError("Completed feed swipes must be between zero and the target")
    remaining = target - completed
    pause_count = normalize_feed_pause_count(
        pause_count,
        available_swipes=remaining,
    )
    if fixed_delay_seconds is not None:
        fixed_delay_seconds = float(fixed_delay_seconds)
        if not 2.0 <= fixed_delay_seconds <= 7.0:
            raise ValueError("Fixed feed swipe delay must be between 2 and 7 seconds")
    if target == 0:
        return FeedSwipeResult(target, completed, initial_proof)

    random_source = rng or random.SystemRandom()
    pause_positions = set(
        random_source.sample(range(completed + 1, target + 1), pause_count)
    )
    logger(f"Feed swipes: resume at {completed + 1}/{target}." if completed else f"Feed swipes: starting {target} gestures.")
    logger(
        f"Feed swipes: {pause_count} random pause(s) scheduled for this session."
    )
    latest_nodes = None
    for gesture in range(completed + 1, target + 1):
        _check_stop(stop)
        logger(f"Starting feed swipe {gesture}/{target}")
        try:
            stack.interp_flick(
                FEED_SWIPE_X,
                FEED_SWIPE_START_Y,
                FEED_SWIPE_X,
                FEED_SWIPE_END_Y,
                steps=FEED_SWIPE_STEPS,
                ms=FEED_SWIPE_STEP_MS,
                settle_seconds=FEED_SWIPE_SETTLE_SECONDS,
            )
        except PhoneError as exc:
            raise PhoneError(
                f"Feed swipe gesture {gesture} failed; no later gesture was sent."
            ) from exc
        completed = gesture
        if progress_fn is not None:
            progress_fn(completed)
        logger(f"Feed swipe {gesture}/{target} completed")
        if gesture % FEED_SWIPE_CHECKPOINT == 0 or gesture == target:
            latest_nodes = _dump_checkpoint(
                stack,
                phone,
                logger,
                completed,
                stop=stop,
                continue_on_unclear=continue_on_unclear,
                dump_fn=dump_fn,
            )
        if gesture in pause_positions:
            pause_seconds = random_source.uniform(
                FEED_SWIPE_PAUSE_MIN_SECONDS,
                FEED_SWIPE_PAUSE_MAX_SECONDS,
            )
            logger(
                "🛑 Taking a random pause for "
                f"{pause_seconds:.1f} seconds after swipe {gesture}/{target}..."
            )
            _wait_interruptibly(stack, pause_seconds, stop)
        elif gesture < target:
            _wait_interruptibly(
                stack,
                fixed_delay_seconds if fixed_delay_seconds is not None else random_source.uniform(
                    FEED_SWIPE_DELAY_MIN_SECONDS, FEED_SWIPE_DELAY_MAX_SECONDS
                ),
                stop,
            )

    final_proof = initial_proof
    if require_final_proof:
        if latest_nodes is None:
            _check_stop(stop)
            _blob, latest_nodes = dump_fn(
                stack,
                phone,
                "creation_feed_final",
                logger,
                shot=False,
                persist=False,
                preview=False,
            )
        final_proof = _strict_final_proof(
            stack,
            phone,
            logger,
            latest_nodes,
            stop=stop,
            allow_unclear_completion=(continue_on_unclear and completed == target),
            dump_fn=dump_fn,
        )
    logger(f"Feed swipe sequence completed: {completed}/{target}.")
    return FeedSwipeResult(target, completed, final_proof)
