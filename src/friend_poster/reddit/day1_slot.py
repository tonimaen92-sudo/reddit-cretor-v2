# UTF-8
"""Run one isolated Day 1 feed-scroll session for an already-created slot.

This command is deliberately separate from Creator.  It never creates an
account and it does not change the normal creation defaults.

    python misc/day1_slot.py a0002
    python misc/day1_slot.py a0002 --minutes 12
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from typing import Callable
from friend_poster.core.config import LOG_DIR  # noqa: E402
from friend_poster.reddit.reddit_open import (  # noqa: E402
    FeedCleanupProof,
    advance_feed_cleanup_proof,
    close_reddit,
    day1_sparse_launch_blocker,
    make_feed_cleanup_proof,
    make_terminal_feed_cleanup_proof,
    open_container,
    protected_reddit_marker,
    proven_springboard,
)
from friend_poster.core.slots import canonicalize_name, get_slot, mark_create_day1  # noqa: E402
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import TapStack, dump_named  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

MIN_MINUTES = 10
MAX_MINUTES = 17
FEED_WAIT_ATTEMPTS = 12
FEED_WAIT_SECONDS = 1.0
TERMINAL_RECOVERY_ATTEMPTS = 3
LOG_PATH = os.path.join(LOG_DIR, "day1_slot.log")


def _print_unicode_safe(line: str) -> None:
    """Print one log line even when the Windows console has a narrow codec."""
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        safe = line.encode(encoding, errors="backslashreplace").decode(encoding)
        print(safe, flush=True)


@dataclass(frozen=True, slots=True)
class ReadySlot:
    sid: str
    email: str


@dataclass(frozen=True, slots=True)
class Day1Result:
    sid: str
    minutes: int
    flicks: int


class SecretSafeLog:
    """Print/file logger that redacts the selected slot's stored secrets."""

    def __init__(
        self,
        secrets: tuple[str, ...] = (),
        *,
        path: str | None = LOG_PATH,
        sink: Callable[[str], None] | None = None,
    ) -> None:
        clean = sorted(
            {str(value) for value in secrets if str(value)},
            key=len,
            reverse=True,
        )
        self._secret_pattern = (
            re.compile("|".join(re.escape(value) for value in clean), re.IGNORECASE)
            if clean
            else None
        )
        self.path = path
        self.sink = sink or _print_unicode_safe

    def redact(self, message: object) -> str:
        text = str(message)
        if self._secret_pattern is not None:
            text = self._secret_pattern.sub("<redacted>", text)
        return text

    def __call__(self, message: object) -> None:
        line = time.strftime("%H:%M:%S") + "  " + self.redact(message)
        try:
            self.sink(line)
        except UnicodeEncodeError as exc:
            # Windows consoles can still be backed by a legacy code page even
            # though OCR and the file log are UTF-8.  A non-ASCII character in
            # ordinary feed text must never abort the safety-critical cleanup
            # path.  Keep secrets redacted, escape only the console copy, and
            # retain the original Unicode text in the UTF-8 log below.
            encoding = exc.encoding or sys.stdout.encoding or "ascii"
            safe = line.encode(encoding, errors="backslashreplace").decode(encoding)
            self.sink(safe)
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # A logging failure must not authorize or alter a phone gesture.
            pass


def slot_secrets(row: dict | None) -> tuple[str, ...]:
    """Return only values that must be scrubbed from every emitted log line."""
    if not row:
        return ()
    return tuple(
        value
        for value in (
            str(row.get("email") or "").strip(),
            str(row.get("password") or ""),
        )
        if value
    )


def validate_ready_slot(requested_sid: str, row: dict | None) -> ReadySlot:
    """Validate the persisted slot state required by this isolated command."""
    sid = canonicalize_name(requested_sid)
    if not sid or row is None:
        raise PhoneError(f"No authoritative slot {sid or requested_sid!r}.")
    stored_sid = canonicalize_name(str(row.get("id") or ""))
    if stored_sid != sid:
        raise PhoneError(f"Requested slot {sid} did not match the stored slot identity.")
    if row.get("ghost") is not True:
        raise PhoneError(f"{sid}: Ghost container is not ready.")
    email = str(row.get("email") or "").strip()
    if not email:
        raise PhoneError(f"{sid}: Hide My Email is missing.")
    if row.get("reddit") is not True:
        raise PhoneError(f"{sid}: Reddit has not been created.")
    cleanup = str(row.get("reddit_cleanup") or "").strip().lower()
    if cleanup != "complete":
        raise PhoneError(
            f"{sid}: Reddit creation cleanup must be explicitly complete; found "
            f"{cleanup or 'missing'}."
        )
    if row.get("day1") is not False:
        raise PhoneError(f"{sid}: Day 1 is already complete or has invalid state.")
    return ReadySlot(sid=sid, email=email)


def resolve_minutes(
    requested: int | None,
    rng: random.Random | random.SystemRandom | None = None,
) -> int:
    """Resolve a strict 10-17 minute duration; omission uses SystemRandom."""
    if requested is not None:
        if requested < MIN_MINUTES or requested > MAX_MINUTES:
            raise ValueError(
                f"--minutes must be between {MIN_MINUTES} and {MAX_MINUTES}"
            )
        return requested
    chooser = rng if rng is not None else random.SystemRandom()
    return chooser.randint(MIN_MINUTES, MAX_MINUTES)


def _transient_dump(
    stack: TapStack,
    phone: Phone,
    stem: str,
    log: Callable[[str], None],
    *,
    dumper=dump_named,
):
    """Capture labels in memory only, with label previews disabled."""
    return dumper(
        stack,
        phone,
        stem,
        log,
        shot=False,
        persist=False,
        preview=False,
    )


def wait_for_strict_feed(
    stack: TapStack,
    phone: Phone,
    sid: str,
    log: Callable[[str], None],
    *,
    sleeper: Callable[[float], None] = time.sleep,
    dumper=dump_named,
    proof_maker=make_feed_cleanup_proof,
    springboard_check=proven_springboard,
    interaction_blocker=day1_sparse_launch_blocker,
    protected_marker=protected_reddit_marker,
) -> FeedCleanupProof:
    """Wait without gestures until a strict Home feed yields cleanup proof."""
    for attempt in range(FEED_WAIT_ATTEMPTS):
        _blob, nodes = _transient_dump(
            stack,
            phone,
            f"930_{sid}_day1_feed_{attempt}",
            log,
            dumper=dumper,
        )
        if springboard_check(nodes):
            raise PhoneError(
                f"{sid}: Reddit returned to SpringBoard before its Home feed was proven."
            )
        blocked = interaction_blocker(nodes)
        if blocked:
            raise PhoneError(
                f"{sid}: Reddit login/signup marker {blocked!r} is visible; "
                "no feed gesture was made."
            )
        proof = proof_maker(nodes)
        if isinstance(proof, FeedCleanupProof):
            log(f"{sid}: strict Reddit Home feed proven; Day 1 may begin.")
            return proof
        protected = protected_marker(nodes)
        if protected:
            raise PhoneError(
                f"{sid}: protected Reddit screen {protected!r} is visible; "
                "no feed gesture was made."
            )
        if attempt + 1 < FEED_WAIT_ATTEMPTS:
            sleeper(FEED_WAIT_SECONDS)
    raise PhoneError(
        f"{sid}: strict Reddit Home feed did not become provable; no feed gesture was made."
    )


def open_day1_container(
    stack: TapStack,
    phone: Phone,
    log: Callable[[str], None],
    sid: str,
    email: str,
) -> FeedCleanupProof | None:
    """Open an existing slot with Day 1's narrow sparse-feed allowance."""
    return open_container(
        stack,
        phone,
        log,
        sid,
        email,
        allow_existing_sparse_feed=True,
    )


def vertical_feed_flick(
    stack: TapStack,
    rng: random.Random | random.SystemRandom,
) -> None:
    """One small upward, exactly vertical gesture inside the feed body.

    The bounded distance is intentionally compatible with the existing feed
    transition proof, which accepts coherent content motion of 40-220 points.
    """
    x = rng.randint(300, 450)
    y1 = rng.randint(800, 930)
    distance = rng.randint(110, 180)
    y2 = y1 - distance
    stack.interp_flick(
        x,
        y1,
        x,
        y2,
        steps=rng.randint(12, 18),
        ms=rng.randint(18, 24),
    )


def _pause_seconds(rng: random.Random | random.SystemRandom) -> float:
    pause = rng.uniform(0.7, 1.7)
    if rng.random() < 0.12:
        pause += rng.uniform(2.0, 5.0)
    return pause


def classify_advanced_proof(
    sid: str,
    current: FeedCleanupProof,
    nodes,
    *,
    phase: str,
    proof_advancer=advance_feed_cleanup_proof,
    terminal_proof_maker=make_terminal_feed_cleanup_proof,
    springboard_check=proven_springboard,
    interaction_blocker=day1_sparse_launch_blocker,
    protected_marker=protected_reddit_marker,
) -> tuple[FeedCleanupProof | None, str]:
    """Advance one trusted frame, returning terminal-only proof on continuity loss."""
    if springboard_check(nodes):
        return None, f"{sid}: SpringBoard appeared {phase}; stopped all feed gestures."
    blocked = interaction_blocker(nodes)
    if blocked:
        return (
            None,
            f"{sid}: Reddit login/signup marker {blocked!r} appeared {phase}; "
            "stopped all feed gestures.",
        )
    advanced = proof_advancer(current, nodes)
    if isinstance(advanced, FeedCleanupProof):
        return advanced, ""
    terminal = terminal_proof_maker(nodes)
    if isinstance(terminal, FeedCleanupProof):
        return (
            terminal,
            f"{sid}: trusted feed continuity was lost {phase}; stopped all feed "
            "gestures and retained terminal cleanup proof.",
        )
    protected = protected_marker(nodes)
    if protected:
        return (
            None,
            f"{sid}: protected Reddit screen {protected!r} appeared {phase}; "
            "stopped all feed gestures.",
        )
    return None, f"{sid}: Reddit feed proof was lost {phase}; stopped all feed gestures."


def recover_terminal_feed_proof(
    stack: TapStack,
    phone: Phone,
    sid: str,
    flicks: int,
    terminal_proof: FeedCleanupProof,
    log: Callable[[str], None],
    *,
    sleeper: Callable[[float], None] = time.sleep,
    dumper=dump_named,
    proof_advancer=advance_feed_cleanup_proof,
    terminal_proof_maker=make_terminal_feed_cleanup_proof,
    springboard_check=proven_springboard,
    interaction_blocker=day1_sparse_launch_blocker,
    protected_marker=protected_reddit_marker,
) -> tuple[FeedCleanupProof | None, str]:
    """Re-prove a terminal feed frame without making another gesture.

    A terminal proof is sufficient for cleanup but not for another feed flick.
    Give a just-scrolled frame a few bounded, gesture-free checks to settle.  A
    later gesture is authorized only when the ordinary proof advancer relates a
    fresh frame to the immediately preceding terminal frame.  Every failed
    check either refreshes terminal-only cleanup evidence or clears it when the
    new frame is SpringBoard, protected, or otherwise unprovable.
    """
    current: FeedCleanupProof | None = terminal_proof
    recovery_context = (
        f"after flick {flicks}" if flicks > 0 else "after sparse container launch"
    )
    for attempt in range(1, TERMINAL_RECOVERY_ATTEMPTS + 1):
        log(
            f"{sid}: feed continuity unprovable {recovery_context}; "
            f"waiting for no-gesture recovery check {attempt}/"
            f"{TERMINAL_RECOVERY_ATTEMPTS}."
        )
        sleeper(FEED_WAIT_SECONDS)
        stem = (
            f"932r_{sid}_post_{flicks}_{attempt}"
            if flicks > 0
            else f"930r_{sid}_launch_{attempt}"
        )
        _blob, nodes = _transient_dump(
            stack,
            phone,
            stem,
            log,
            dumper=dumper,
        )
        previous = current
        # A fresh frame now exists.  The preceding proof is eligible only as
        # the reference passed into the classifier, never directly for cleanup.
        current = None
        candidate, reason = classify_advanced_proof(
            sid,
            previous,
            nodes,
            phase=(
                f"during no-gesture recovery check {attempt}/"
                f"{TERMINAL_RECOVERY_ATTEMPTS} {recovery_context}"
            ),
            proof_advancer=proof_advancer,
            terminal_proof_maker=terminal_proof_maker,
            springboard_check=springboard_check,
            interaction_blocker=interaction_blocker,
            protected_marker=protected_marker,
        )
        current = candidate
        if not reason:
            log(
                f"{sid}: feed continuity recovered {recovery_context} "
                f"with {attempt} no-gesture check(s)."
            )
            return current, ""
        if current is None:
            return None, reason

    return (
        current,
        f"{sid}: feed continuity did not stabilize {recovery_context} "
        f"during {TERMINAL_RECOVERY_ATTEMPTS} no-gesture checks; stopped all "
        "feed gestures and retained terminal cleanup proof.",
    )


def run_day1_slot(
    ready: ReadySlot,
    phone: Phone,
    stack: TapStack,
    minutes: int,
    log: Callable[[str], None],
    *,
    rng: random.Random | random.SystemRandom | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    slot_lookup=get_slot,
    opener=open_day1_container,
    dumper=dump_named,
    proof_maker=make_feed_cleanup_proof,
    proof_advancer=advance_feed_cleanup_proof,
    terminal_proof_maker=make_terminal_feed_cleanup_proof,
    springboard_check=proven_springboard,
    interaction_blocker=day1_sparse_launch_blocker,
    protected_marker=protected_reddit_marker,
    closer=close_reddit,
    marker=mark_create_day1,
) -> Day1Result:
    """Open, prove, scroll, fully close, then and only then mark Day 1."""
    minutes = resolve_minutes(minutes, rng)
    random_source = rng if rng is not None else random.SystemRandom()
    proof: FeedCleanupProof | None = None
    close_started = False
    full_duration = False
    flicks = 0

    try:
        authoritative = validate_ready_slot(ready.sid, slot_lookup(ready.sid))
        if authoritative != ready:
            raise PhoneError(
                f"{ready.sid}: authoritative slot identity changed before Day 1; "
                "the container was not opened."
            )
        launch_proof = opener(stack, phone, log, ready.sid, ready.email)
        if launch_proof is None:
            proof = wait_for_strict_feed(
                stack,
                phone,
                ready.sid,
                log,
                sleeper=sleeper,
                dumper=dumper,
                proof_maker=proof_maker,
                springboard_check=springboard_check,
                interaction_blocker=interaction_blocker,
                protected_marker=protected_marker,
            )
        else:
            if not isinstance(launch_proof, FeedCleanupProof):
                raise PhoneError(
                    f"{ready.sid}: container opener returned invalid feed proof."
                )
            log(
                f"{ready.sid}: sparse launch is cleanup-only; requiring a fresh "
                "no-gesture continuity proof before Day 1."
            )
            # Hand the launch proof to recovery exactly once.  If its wait,
            # dump, or classifier raises, the outer cleanup path must not reuse
            # the now potentially stale launch frame.
            proof = None
            proof, reason = recover_terminal_feed_proof(
                stack,
                phone,
                ready.sid,
                0,
                launch_proof,
                log,
                sleeper=sleeper,
                dumper=dumper,
                proof_advancer=proof_advancer,
                terminal_proof_maker=terminal_proof_maker,
                springboard_check=springboard_check,
                interaction_blocker=interaction_blocker,
                protected_marker=protected_marker,
            )
            if reason:
                raise PhoneError(reason)

        started = clock()
        deadline = started + minutes * 60.0
        log(
            f"{ready.sid}: Day 1 scrolling for {minutes} minutes; "
            "vertical feed flicks only, with no taps, joins, votes, comments, or posts."
        )

        while clock() < deadline:
            # The previous post-flick proof is fresh for the first gesture.  On
            # later gestures, re-prove the live frame after the dwell so an app
            # change during the pause cannot receive a feed flick.
            if flicks:
                _blob, live_nodes = _transient_dump(
                    stack,
                    phone,
                    f"931_{ready.sid}_pre_{flicks + 1}",
                    log,
                    dumper=dumper,
                )
                previous_proof = proof
                # A fresh frame now exists.  Do not let an exception inside a
                # classifier leave the older, pre-dwell proof eligible for
                # emergency cleanup.
                proof = None
                candidate, reason = classify_advanced_proof(
                    ready.sid,
                    previous_proof,
                    live_nodes,
                    phase="before the next flick",
                    proof_advancer=proof_advancer,
                    terminal_proof_maker=terminal_proof_maker,
                    springboard_check=springboard_check,
                    interaction_blocker=interaction_blocker,
                    protected_marker=protected_marker,
                )
                proof = candidate
                if reason:
                    raise PhoneError(reason)
                if clock() >= deadline:
                    break

            before_flick = proof
            # Once the gesture begins, the old frame is not valid cleanup
            # evidence for the resulting screen.  Restore proof only from the
            # mandatory post-flick transient dump.
            proof = None
            vertical_feed_flick(stack, random_source)
            flicks += 1
            _blob, after_nodes = _transient_dump(
                stack,
                phone,
                f"932_{ready.sid}_post_{flicks}",
                log,
                dumper=dumper,
            )
            candidate, reason = classify_advanced_proof(
                ready.sid,
                before_flick,
                after_nodes,
                phase=f"after flick {flicks}",
                proof_advancer=proof_advancer,
                terminal_proof_maker=terminal_proof_maker,
                springboard_check=springboard_check,
                interaction_blocker=interaction_blocker,
                protected_marker=protected_marker,
            )
            proof = candidate
            if reason and isinstance(proof, FeedCleanupProof):
                terminal_recovery_proof = proof
                # Hand recovery sole ownership of this exact-frame proof.  If
                # its wait, dump, or classifier raises, the outer error path
                # must not reuse the now potentially stale terminal frame.
                proof = None
                proof, reason = recover_terminal_feed_proof(
                    stack,
                    phone,
                    ready.sid,
                    flicks,
                    terminal_recovery_proof,
                    log,
                    sleeper=sleeper,
                    dumper=dumper,
                    proof_advancer=proof_advancer,
                    terminal_proof_maker=terminal_proof_maker,
                    springboard_check=springboard_check,
                    interaction_blocker=interaction_blocker,
                    protected_marker=protected_marker,
                )
            if reason:
                raise PhoneError(reason)

            remaining = deadline - clock()
            if remaining > 0:
                sleeper(min(_pause_seconds(random_source), remaining))

        full_duration = clock() >= deadline and flicks > 0
        if not full_duration:
            raise PhoneError(f"{ready.sid}: Day 1 duration ended early.")

        # Bind the final cleanup proof to a fresh frame immediately before the
        # existing exact-card close routine takes over.
        _blob, final_nodes = _transient_dump(
            stack,
            phone,
            f"933_{ready.sid}_final",
            log,
            dumper=dumper,
        )
        previous_proof = proof
        proof = None
        candidate, reason = classify_advanced_proof(
            ready.sid,
            previous_proof,
            final_nodes,
            phase="at the end of the session",
            proof_advancer=proof_advancer,
            terminal_proof_maker=terminal_proof_maker,
            springboard_check=springboard_check,
            interaction_blocker=interaction_blocker,
            protected_marker=protected_marker,
        )
        proof = candidate
        if reason:
            raise PhoneError(reason)

        final_proof = proof
        proof = None
        close_started = True
        closed = closer(stack, phone, log, feed_proof=final_proof)
        if closed is not True:
            raise PhoneError(f"{ready.sid}: Reddit close did not return verified success.")

        # Re-read the authoritative slot after the long-running session.  A
        # concurrent state or identity change must never be overwritten.
        latest = validate_ready_slot(ready.sid, slot_lookup(ready.sid))
        if latest != ready:
            raise PhoneError(
                f"{ready.sid}: authoritative slot identity changed during Day 1; "
                "Reddit was closed but Day 1 was not marked."
            )
        if not full_duration:
            raise PhoneError(f"{ready.sid}: refusing to mark an incomplete Day 1.")
        marker(ready.sid, log)
        log(f"{ready.sid}: Day 1 complete after {flicks} verified vertical flicks.")
        return Day1Result(ready.sid, minutes, flicks)
    except Exception as exc:
        # Only a proof for the *current* frame permits cleanup.  proof is
        # cleared before every feed gesture and before handing control to the
        # close routine, so stale evidence can never authorize recovery.
        if proof is not None and not close_started:
            cleanup_proof = proof
            proof = None
            log(f"{ready.sid}: Day 1 failed; attempting one verified Reddit close.")
            try:
                cleanup_ok = closer(stack, phone, log, feed_proof=cleanup_proof)
                if cleanup_ok is not True:
                    raise PhoneError("Reddit close returned no verified success result.")
                log(f"{ready.sid}: Reddit was fully closed after the Day 1 error.")
            except Exception as cleanup_exc:
                log(f"{ready.sid}: verified error cleanup failed: {cleanup_exc}")
                raise PhoneError(
                    f"{ready.sid}: Day 1 failed and verified Reddit cleanup also failed."
                ) from exc
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run isolated Day 1 scrolling for one already-created slot."
    )
    parser.add_argument("slot", help="Exact slot id, for example a0002")
    parser.add_argument(
        "--minutes",
        type=int,
        choices=range(MIN_MINUTES, MAX_MINUTES + 1),
        default=None,
        metavar="10..17",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sid = canonicalize_name(args.slot)
    row = get_slot(sid)
    log = SecretSafeLog(slot_secrets(row))
    phone: Phone | None = None
    try:
        ready = validate_ready_slot(sid, row)
        minutes = resolve_minutes(args.minutes)
        log(f"{ready.sid}: isolated Day 1 selected ({minutes} minutes).")

        phone = Phone(log=log)
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend. Open XXTouch Elite with Remote Service on.")

        run_day1_slot(ready, phone, stack, minutes, log)
        return 0
    except (PhoneError, ValueError) as exc:
        log("ERROR " + str(exc))
        return 1
    finally:
        if phone is not None:
            phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
