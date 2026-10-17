# UTF-8
"""Standalone deterministic feed-scroll stability test.

The runner opens the requested app/container, uses the shared bounded feed
gesture engine, checks safe Reddit structure every five gestures and after the
final partial group, then stops only the exact app bundle and verifies Home.
It remains disconnected from creation slot progress.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import REDDIT_BUNDLE as QA_APP_BUNDLE  # noqa: E402
from feed_swipe import (  # noqa: E402
    FEED_SWIPE_END_Y,
    FEED_SWIPE_START_Y,
    FEED_SWIPE_STEP_MS,
    FEED_SWIPE_STEPS,
    FEED_SWIPE_X,
    run_feed_swipes,
)
from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack, collapsed_text, dump_named  # noqa: E402


QA_CYCLES = 12
QA_X = FEED_SWIPE_X
QA_START_Y = FEED_SWIPE_START_Y
QA_END_Y = FEED_SWIPE_END_Y
QA_STEPS = FEED_SWIPE_STEPS
QA_STEP_MS = FEED_SWIPE_STEP_MS
QA_SETTLE_SECONDS = 0.35
QA_LAUNCH_POLLS = 4
QA_LAUNCH_POLL_SECONDS = 0.8
QA_FRAME_RETRIES = 3
QA_MIN_ANCHORS = 2
QA_MAX_X_DRIFT = 15
QA_MIN_UPWARD_SHIFT = 25
QA_MAX_UPWARD_SHIFT = 520
QA_SHIFT_TOLERANCE = 30
QA_BOTTOM_NAV_MARKERS = {
    "home", "popular", "communities", "create", "chat", "inbox",
}
QA_REJECTED_SCREEN_MARKERS = (
    "verify your email",
    "verification code",
    "set a password",
    "about you",
    "choose your interests",
    "customize your feed",
    "building your home feed",
    "continue with apple",
    "continue with google",
    "continue with email",
    "use email or username",
    "create a new account",
    "log in or sign up",
    "please turn on notifications",
    "allow notifications",
)


@dataclass(frozen=True, slots=True)
class QaScrollResult:
    cycles_completed: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class QaFeedProof:
    reference_nodes: tuple[object, ...]


def log(message: str) -> None:
    print(message, flush=True)


def _visible_labels(nodes) -> list[tuple[str, object]]:
    labels = []
    for item in nodes or ():
        if item.ntype == "ocr-line" or not item.on_screen() or not item.visible:
            continue
        text = collapsed_text(item.labels())
        if text:
            labels.append((text, item))
    return labels


def _rejected_marker(nodes) -> str:
    blob = " ".join(text for text, _item in _visible_labels(nodes))
    return next((marker for marker in QA_REJECTED_SCREEN_MARKERS if marker in blob), "")


def _content_positions(nodes) -> dict[str, list[tuple[int, int]]]:
    positions: dict[str, list[tuple[int, int]]] = {}
    for text, item in _visible_labels(nodes):
        center_x, center_y = item.center()
        if center_y < 150 or center_y >= 900:
            continue
        if text in QA_BOTTOM_NAV_MARKERS or len(text) < 4:
            continue
        positions.setdefault(text, []).append((center_x, center_y))
    return positions


def proven_qa_main_screen(nodes) -> bool:
    """Strict initial feed proof using exact bottom navigation."""
    labels = _visible_labels(nodes)
    if _rejected_marker(nodes):
        return False
    bottom = {
        text
        for text, item in labels
        if item.center()[1] >= 900 and text in QA_BOTTOM_NAV_MARKERS
    }
    return (
        "home" in bottom
        and len(bottom) >= 2
        and len(_content_positions(nodes)) >= QA_MIN_ANCHORS
    )


def make_qa_feed_proof(nodes) -> QaFeedProof | None:
    if not proven_qa_main_screen(nodes):
        return None
    return QaFeedProof(tuple(nodes or ()))


def _stable_or_upward_continuity(before_nodes, after_nodes) -> bool:
    before = _content_positions(before_nodes)
    after = _content_positions(after_nodes)
    stable = 0
    shifts: list[int] = []
    for text in before.keys() & after.keys():
        matched_stable = False
        accepted_shift = None
        for before_x, before_y in before[text]:
            for after_x, after_y in after[text]:
                if abs(after_x - before_x) > QA_MAX_X_DRIFT:
                    continue
                shift = after_y - before_y
                if abs(shift) <= 12:
                    matched_stable = True
                if -QA_MAX_UPWARD_SHIFT <= shift <= -QA_MIN_UPWARD_SHIFT:
                    accepted_shift = shift
            if matched_stable or accepted_shift is not None:
                break
        if matched_stable:
            stable += 1
        if accepted_shift is not None:
            shifts.append(accepted_shift)
    if stable >= QA_MIN_ANCHORS:
        return True
    return any(
        sum(
            abs(observed - expected) <= QA_SHIFT_TOLERANCE
            for observed in shifts
        ) >= QA_MIN_ANCHORS
        for expected in shifts
    )


def advance_qa_feed_proof(
    proof: QaFeedProof,
    after_nodes,
) -> QaFeedProof | None:
    """Advance only from a trusted feed through stable/upward content motion."""
    if not isinstance(proof, QaFeedProof):
        return None
    if _rejected_marker(after_nodes) or proven_qa_springboard(after_nodes):
        return None
    if proven_qa_main_screen(after_nodes):
        return QaFeedProof(tuple(after_nodes or ()))
    if len(_content_positions(after_nodes)) < QA_MIN_ANCHORS:
        return None
    if not _stable_or_upward_continuity(proof.reference_nodes, after_nodes):
        return None
    return QaFeedProof(tuple(after_nodes or ()))


def proven_replacement_qa_feed(nodes, *, replacement_prover=None) -> bool:
    """Allow a safe feed rebaseline when a long swipe replaces all anchors."""
    if _rejected_marker(nodes) or proven_qa_springboard(nodes):
        return False
    if replacement_prover is None:
        from reddit_open import make_terminal_feed_cleanup_proof

        replacement_prover = make_terminal_feed_cleanup_proof
    try:
        return replacement_prover(nodes) is not None
    except (TypeError, ValueError):
        return False


def proven_qa_springboard(nodes) -> bool:
    """Require iPhone Home using Search plus a small icon-label grid."""
    labels = _visible_labels(nodes)
    search = next(
        (
            item
            for text, item in labels
            if text == "q search" and item.center()[1] >= 1050
        ),
        None,
    )
    if search is None:
        return False
    icon_like = 0
    for text, item in labels:
        center_x, center_y = item.center()
        if not (180 <= center_y <= 1060 and 20 <= center_x <= 730):
            continue
        if not (20 <= item.w <= 190 and 10 <= item.h <= 65):
            continue
        if text not in {"q search", "search"}:
            icon_like += 1
    return icon_like >= 4


def qa_app_icon(nodes):
    """Return one exact Reddit SpringBoard label, never an in-app title."""
    if not proven_qa_springboard(nodes):
        return None
    matches = []
    for text, item in _visible_labels(nodes):
        if text != "reddit" or item.y < 220 or item.y > 1280:
            continue
        matches.append(item)
    return matches[0] if len(matches) == 1 else None


def open_qa_app(
    stack: TapStack,
    phone: Phone,
    logger: Callable[[str], None] = log,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    dump_fn=dump_named,
) -> QaFeedProof:
    """Open the exact SpringBoard app icon once and require its main feed."""
    logger("Opening test app")
    stack.press_home()
    _blob, home_nodes = dump_fn(
        stack, phone, "qa_launch_home", logger,
        shot=False, persist=False, preview=False,
    )
    icon = qa_app_icon(home_nodes)
    if icon is None:
        raise PhoneError(
            "QA launch requires one exact Reddit icon on the proven Home screen. "
            "No app icon was tapped."
        )
    x = icon.center()[0]
    y = max(80, icon.y - 48)
    logger("Opening test app from its proven Home-screen icon")
    stack.tap_xy(x, y)
    for attempt in range(1, QA_LAUNCH_POLLS + 1):
        sleeper(QA_LAUNCH_POLL_SECONDS)
        _blob, launch_nodes = dump_fn(
            stack, phone, f"qa_launch_feed_{attempt}", logger,
            shot=False, persist=False, preview=False,
        )
        proof = make_qa_feed_proof(launch_nodes)
        if proof is not None:
            logger("Test app main feed proven")
            return proof
        if _rejected_marker(launch_nodes):
            raise PhoneError(
                "QA launch reached a protected signup/onboarding screen. "
                "No swipe was sent."
            )
        if proven_qa_springboard(launch_nodes):
            raise PhoneError(
                "QA launch returned to Home instead of opening the main feed. "
                "No second icon tap was sent."
            )
    raise PhoneError(
        "QA launch did not reach a proven main feed after four checks. "
        "No swipe was sent."
    )


def open_selected_qa_container(
    stack: TapStack,
    phone: Phone,
    slot_name: str,
    logger: Callable[[str], None] = log,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    dump_fn=dump_named,
    container_opener=None,
) -> QaFeedProof:
    """Select one existing Crane container, then prove its main feed."""
    if container_opener is None:
        from reddit_open import open_container as container_opener

    logger(f"Selecting QA container {slot_name}")
    container_opener(
        stack,
        phone,
        logger,
        slot_name,
        "",
        allow_existing_sparse_feed=True,
    )
    for attempt in range(1, QA_LAUNCH_POLLS + 1):
        sleeper(QA_LAUNCH_POLL_SECONDS)
        _blob, nodes = dump_fn(
            stack, phone, f"qa_container_feed_{attempt}", logger,
            shot=False, persist=False, preview=False,
        )
        proof = make_qa_feed_proof(nodes)
        if proof is not None:
            logger(f"QA container {slot_name} main feed proven")
            return proof
        if _rejected_marker(nodes):
            raise PhoneError(
                f"QA container {slot_name} opened a protected signup/onboarding "
                "screen. No swipe was sent."
            )
        if proven_qa_springboard(nodes):
            raise PhoneError(
                f"QA container {slot_name} returned to Home instead of its main "
                "feed. No extra icon tap was sent."
            )
    raise PhoneError(
        f"QA container {slot_name} did not reach a proven main feed after four "
        "checks. No swipe was sent."
    )


def run_scroll_session(
    stack: TapStack,
    validate_after_gesture: Callable[[int], None],
    logger: Callable[[str], None] = log,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    cycles: int = QA_CYCLES,
) -> QaScrollResult:
    started = clock()
    logger(f"QA scroll started: {cycles} deterministic feed swipes.")
    for cycle_number in range(1, cycles + 1):
        logger(f"Starting swipe {cycle_number}/{cycles}")
        try:
            stack.interp_flick(
                QA_X,
                QA_START_Y,
                QA_X,
                QA_END_Y,
                steps=QA_STEPS,
                ms=QA_STEP_MS,
            )
        except PhoneError as exc:
            raise PhoneError(
                f"QA scroll gesture {cycle_number} failed; no later gesture was sent."
            ) from exc
        sleeper(QA_SETTLE_SECONDS)
        validate_after_gesture(cycle_number)
        logger(f"Swipe {cycle_number}/{cycles} completed")
    return QaScrollResult(cycles, max(0.0, clock() - started))


def cleanup_qa_app(
    stack: TapStack,
    phone: Phone,
    logger: Callable[[str], None] = log,
    *,
    dump_fn=dump_named,
) -> str:
    """Stop only the configured QA bundle, then prove iPhone Home."""
    logger("Closing app")
    close_mode = stack.clear_app_memory(QA_APP_BUNDLE)
    if stack.app_is_running(QA_APP_BUNDLE):
        raise PhoneError(
            "QA cleanup failed because the exact application process is still running."
        )
    stack.press_home()
    _blob, cleanup_nodes = dump_fn(
        stack, phone, "qa_scroll_cleanup", logger,
        shot=False, persist=False, preview=False,
    )
    if not proven_qa_springboard(cleanup_nodes):
        raise PhoneError(
            "QA cleanup stopped the exact process, but the device Home screen was "
            "not proven."
        )
    logger(f"TEST COMPLETE — cleanup executed ({close_mode})")
    return close_mode


def run_qa_scroll_test(
    stack: TapStack,
    phone: Phone,
    logger: Callable[[str], None] = log,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    dump_fn=dump_named,
    cleanup_on_error: bool = False,
    slot_name: str = "",
    container_opener=None,
    swipe_count: int = QA_CYCLES,
    replacement_prover=None,
) -> QaScrollResult:
    try:
        if slot_name:
            proof = open_selected_qa_container(
                stack,
                phone,
                slot_name,
                logger,
                sleeper=sleeper,
                dump_fn=dump_fn,
                container_opener=container_opener,
            )
        else:
            proof = open_qa_app(
                stack, phone, logger, sleeper=sleeper, dump_fn=dump_fn,
            )
        started = clock()
        shared_result = run_feed_swipes(
            stack,
            phone,
            logger,
            target=swipe_count,
            completed=0,
            require_final_proof=False,
            dump_fn=dump_fn,
        )
        result = QaScrollResult(
            shared_result.completed,
            max(0.0, clock() - started),
        )
        logger("Swipe sequence completed")
    except PhoneError as swipe_error:
        logger(f"QA swipe error: {swipe_error}")
        if cleanup_on_error:
            try:
                cleanup_qa_app(stack, phone, logger, dump_fn=dump_fn)
            except PhoneError as cleanup_error:
                raise PhoneError(
                    f"{swipe_error} Cleanup also failed: {cleanup_error}"
                ) from swipe_error
        raise

    cleanup_qa_app(stack, phone, logger, dump_fn=dump_fn)
    return result


def parse_qa_options(argv: list[str]) -> tuple[bool, bool, str, int]:
    cleanup_on_error = "--cleanup-on-error" in argv
    simulated_success = "--simulated-success" in argv
    slot_name = ""
    swipe_count = QA_CYCLES
    consumed: set[int] = set()
    for index, item in enumerate(argv):
        if item != "--slot":
            continue
        if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
            raise ValueError("--slot requires a slot id")
        if slot_name:
            raise ValueError("--slot may be specified only once")
        slot_name = argv[index + 1].strip().lower()
        consumed.update({index, index + 1})
    for index, item in enumerate(argv):
        if item != "--swipes":
            continue
        if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
            raise ValueError("--swipes requires a count")
        if index in consumed:
            continue
        try:
            swipe_count = int(argv[index + 1])
        except ValueError as exc:
            raise ValueError("QA swipe count must be a whole number") from exc
        if not 14 <= swipe_count <= 250:
            raise ValueError("QA swipe count must be from 14 through 250")
        consumed.update({index, index + 1})
    known_flags = {"--cleanup-on-error", "--simulated-success", "--noninteractive"}
    unknown = [
        item for index, item in enumerate(argv)
        if index not in consumed and item not in known_flags
    ]
    if unknown:
        raise ValueError("Unknown QA option: " + " ".join(unknown))
    if slot_name and re.fullmatch(r"[a-z][0-9]{4,}", slot_name) is None:
        raise ValueError("Invalid QA slot id")
    return cleanup_on_error, simulated_success, slot_name, swipe_count


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cleanup_on_error, simulated_success, slot_name, swipe_count = parse_qa_options(argv)
    except ValueError as exc:
        log("ERROR " + str(exc))
        return 2
    if slot_name:
        from slots import get_slot, slot_reddit_ready

        slot = get_slot(slot_name)
        if not slot_reddit_ready(slot):
            log(
                f"ERROR QA slot {slot_name} is missing or is not creation/cleanup complete."
            )
            return 2
    if simulated_success:
        log("QA MODE: simulated successful completion")
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No gesture backend. Open XXTouch Elite with Remote Service on.")
        run_qa_scroll_test(
            stack,
            phone,
            log,
            cleanup_on_error=cleanup_on_error,
            slot_name=slot_name,
            swipe_count=swipe_count,
        )
        return 0
    except PhoneError as exc:
        log("ERROR " + str(exc))
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
