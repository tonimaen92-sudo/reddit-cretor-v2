# UTF-8
"""Focused offline regressions for the isolated slot Day 1 runner.

Synthetic frames and fakes only: no phone, USB, SSH, network, or project-data
writes.  Run with::

    python misc/day1_slot_regress.py
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from friend_poster.reddit import day1_slot as day1  # noqa: E402
from friend_poster.reddit import reddit_open  # noqa: E402
from friend_poster.reddit.reddit_open import FeedCleanupProof  # noqa: E402
from friend_poster.device.ssh_device import PhoneError  # noqa: E402
from friend_poster.device.ui_tree import Node, parse_nodes  # noqa: E402
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


def ready_row(**changes) -> dict:
    row = {
        "id": "a0002",
        "ghost": True,
        "email": "day1-secret@example.invalid",
        "password": "Password-Canary-91",
        "reddit": True,
        "reddit_cleanup": "complete",
        "day1": False,
    }
    row.update(changes)
    return row


class FakeRng:
    def __init__(self, minute: int = 13) -> None:
        self.minute = minute
        self.randint_calls: list[tuple[int, int]] = []

    def randint(self, low: int, high: int) -> int:
        self.randint_calls.append((low, high))
        if (low, high) == (day1.MIN_MINUTES, day1.MAX_MINUTES):
            return self.minute
        return (low + high) // 2

    def uniform(self, low: float, high: float) -> float:
        return low

    def random(self) -> float:
        return 1.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep_to_deadline(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        # Make a ten-minute production duration deterministic and one-gesture
        # long in the offline suite.
        self.now += 600.0


class FakeStack:
    def __init__(self) -> None:
        self.flicks: list[tuple[int, int, int, int, int, int]] = []
        self.taps: list[tuple[int, int]] = []

    def interp_flick(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        steps: int,
        ms: int,
    ) -> None:
        self.flicks.append((x1, y1, x2, y2, steps, ms))

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class FakeDumper:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.frame = 0

    def __call__(self, _stack, _phone, stem, _log, **kwargs):
        self.calls.append((stem, dict(kwargs)))
        self.frame += 1
        return "", [f"frame-{self.frame}"]


def proof_for(nodes) -> FeedCleanupProof:
    return FeedCleanupProof(tuple(nodes))


def captured_fixture(stem: str) -> tuple[str, list[Node]]:
    path = FIXTURES_DIR / (stem + ".json")
    with open(path, encoding="utf-8") as handle:
        blob = handle.read()
    return blob, parse_nodes(blob)


def validation_regressions() -> None:
    valid = day1.validate_ready_slot("a0002", ready_row())
    check("slot-valid", valid.sid == "a0002" and "secret" in valid.email)

    invalids = (
        ("missing", None),
        ("ghost", ready_row(ghost=False)),
        ("email", ready_row(email="")),
        ("reddit", ready_row(reddit=False)),
        ("cleanup-missing", ready_row(reddit_cleanup=None)),
        ("cleanup-pending", ready_row(reddit_cleanup="pending")),
        ("day1-true", ready_row(day1=True)),
        ("day1-missing", {k: v for k, v in ready_row().items() if k != "day1"}),
    )
    for name, row in invalids:
        try:
            day1.validate_ready_slot("a0002", row)
        except PhoneError:
            rejected = True
        else:
            rejected = False
        check("slot-reject-" + name, rejected)


def duration_and_log_regressions() -> None:
    rng = FakeRng(17)
    check("minutes-default-system-rng-shape", day1.resolve_minutes(None, rng) == 17)
    check(
        "minutes-default-bounded-call",
        rng.randint_calls == [(day1.MIN_MINUTES, day1.MAX_MINUTES)],
    )
    check("minutes-explicit-lower", day1.resolve_minutes(10, FakeRng()) == 10)
    check("minutes-explicit-upper", day1.resolve_minutes(17, FakeRng()) == 17)
    for value in (9, 18):
        try:
            day1.resolve_minutes(value, FakeRng())
        except ValueError:
            rejected = True
        else:
            rejected = False
        check(f"minutes-reject-{value}", rejected)

    lines: list[str] = []
    email = "day1-secret@example.invalid"
    password = "Password-Canary-91"
    log = day1.SecretSafeLog((email, password), path=None, sink=lines.append)
    log(f"OPENED a0002 email={email} password={password}")
    joined = "\n".join(lines)
    check(
        "logger-redacts-email-password",
        email not in joined and password not in joined and joined.count("<redacted>") == 2,
    )

    # Windows may give redirected stdout a legacy codec such as cp1252.  OCR
    # previews can contain Cyrillic, and logging that text must not abort the
    # verified close path.  The console gets escaped text while the UTF-8 log
    # keeps the original diagnostic.
    raw_console = io.BytesIO()
    narrow_console = io.TextIOWrapper(raw_console, encoding="cp1252", errors="strict")
    original_stdout = sys.stdout
    with tempfile.TemporaryDirectory() as temp_dir:
        unicode_log_path = os.path.join(temp_dir, "day1.log")
        try:
            sys.stdout = narrow_console
            unicode_log = day1.SecretSafeLog(path=unicode_log_path)
            unicode_log("close preview ocr 'D о'")
            narrow_console.flush()
        except UnicodeEncodeError:
            unicode_safe = False
        else:
            unicode_safe = True
        finally:
            sys.stdout = original_stdout
        console_text = raw_console.getvalue().decode("cp1252")
        if os.path.exists(unicode_log_path):
            with open(unicode_log_path, "r", encoding="utf-8") as handle:
                file_text = handle.read()
        else:
            file_text = ""
    check(
        "logger-cyrillic-survives-narrow-console",
        unicode_safe
        and "D о" in file_text
        and "D о" not in console_text
        and "D \\u043e" in console_text,
        repr(console_text),
    )


def successful_session_regression() -> None:
    row = ready_row()
    ready = day1.validate_ready_slot("a0002", row)
    logs: list[str] = []
    safe_log = day1.SecretSafeLog(
        day1.slot_secrets(row), path=None, sink=logs.append
    )
    stack = FakeStack()
    dumper = FakeDumper()
    clock = FakeClock()
    opened: list[tuple[str, str]] = []
    closed: list[FeedCleanupProof] = []
    marked: list[str] = []
    order: list[str] = []

    def opener(_stack, _phone, log, sid, email):
        opened.append((sid, email))
        # Exercise the production redactor around existing open_container's
        # current email-bearing completion line.
        log(f"OPENED {sid} email={email} password={row['password']}")

    def closer(_stack, _phone, _log, *, feed_proof):
        closed.append(feed_proof)
        order.append("close")
        return True

    def marker(sid, _log):
        marked.append(sid)
        order.append("mark")

    result = day1.run_day1_slot(
        ready,
        object(),
        stack,
        10,
        safe_log,
        rng=FakeRng(),
        clock=clock,
        sleeper=clock.sleep_to_deadline,
        slot_lookup=lambda _sid: dict(row),
        opener=opener,
        dumper=dumper,
        proof_maker=proof_for,
        proof_advancer=lambda _proof, nodes: proof_for(nodes),
        terminal_proof_maker=lambda _nodes: None,
        springboard_check=lambda _nodes: False,
        protected_marker=lambda _nodes: "",
        closer=closer,
        marker=marker,
    )

    check("success-open-exact-slot", opened == [("a0002", row["email"])])
    check("success-one-flick", result.flicks == 1 and len(stack.flicks) == 1)
    flick = stack.flicks[0]
    check(
        "success-flick-is-upward-vertical",
        flick[0] == flick[2] and flick[1] > flick[3],
        repr(flick),
    )
    check("success-no-content-taps", stack.taps == [])
    check(
        "success-all-dumps-transient-preview-off",
        bool(dumper.calls)
        and all(
            kwargs.get("persist") is False
            and kwargs.get("preview") is False
            and kwargs.get("shot") is False
            for _stem, kwargs in dumper.calls
        ),
    )
    check(
        "success-post-flick-dump-present",
        any("_post_1" in stem for stem, _kwargs in dumper.calls),
    )
    check(
        "success-close-before-mark",
        len(closed) == 1 and marked == ["a0002"] and order == ["close", "mark"],
    )
    check(
        "success-final-proof-passed-to-close",
        isinstance(closed[0], FeedCleanupProof),
    )
    rendered = "\n".join(logs)
    check(
        "success-logs-have-no-secrets",
        row["email"] not in rendered and row["password"] not in rendered,
    )


def sparse_launch_regressions() -> None:
    """The captured sparse ad feed is Day1-only and never a direct flick proof."""
    blob, nodes = captured_fixture("day1_sparse_launch_live")
    terminal = reddit_open.make_terminal_feed_cleanup_proof(nodes)
    check(
        "sparse-launch-real-fixture-is-terminal-only",
        len(blob.rstrip("\r\n")) == 709
        and reddit_open.post_selection_state(nodes, "a0002") == "unknown"
        and reddit_open.make_feed_cleanup_proof(nodes) is None
        and isinstance(terminal, FeedCleanupProof),
    )

    original_dump = reddit_open._transient_dump
    original_sleep = reddit_open.time.sleep
    original_save = reddit_open._save_confirmed_reddit_dump
    reddit_open._transient_dump = lambda *_args, **_kwargs: (blob, nodes)
    reddit_open.time.sleep = lambda _seconds: None
    reddit_open._save_confirmed_reddit_dump = lambda *_args, **_kwargs: None
    strict_stack = FakeStack()
    try:
        try:
            reddit_open._confirm_container_launch(
                strict_stack,
                object(),
                lambda _message: None,
                "a0002",
            )
        except PhoneError as exc:
            default_rejected = "unrecognized screen" in str(exc).casefold()
        else:
            default_rejected = False

        opted_stack = FakeStack()
        opted_proof = reddit_open._confirm_container_launch(
            opted_stack,
            object(),
            lambda _message: None,
            "a0002",
            allow_existing_sparse_feed=True,
        )
    finally:
        reddit_open._transient_dump = original_dump
        reddit_open.time.sleep = original_sleep
        reddit_open._save_confirmed_reddit_dump = original_save
    check(
        "sparse-launch-default-open-route-still-rejects",
        default_rejected and strict_stack.taps == [] and strict_stack.flicks == [],
    )
    check(
        "sparse-launch-explicit-opt-in-returns-terminal-proof",
        isinstance(opted_proof, FeedCleanupProof)
        and opted_proof.sparse_continuity
        and opted_stack.taps == []
        and opted_stack.flicks == [],
    )

    # Mirror the live failure's final two probes: proven Home first, followed
    # by the captured sparse Reddit frame.  Launch confirmation may tap the
    # freshly located Reddit icon exactly once, never a content coordinate.
    springboard = [
        Node(ntype="ocr", text="Reddit", x=240, y=1010, w=90, h=24),
    ]
    live_sequence = iter((("springboard", springboard), (blob, nodes)))
    reddit_open._transient_dump = lambda *_args, **_kwargs: next(live_sequence)
    reddit_open.time.sleep = lambda _seconds: None
    launch_stack = FakeStack()
    try:
        launched_proof = reddit_open._confirm_container_launch(
            launch_stack,
            object(),
            lambda _message: None,
            "a0002",
            allow_existing_sparse_feed=True,
        )
    finally:
        reddit_open._transient_dump = original_dump
        reddit_open.time.sleep = original_sleep
    check(
        "sparse-launch-live-sequence-taps-icon-once-only",
        isinstance(launched_proof, FeedCleanupProof)
        and launch_stack.taps == [(285, 962)]
        and launch_stack.flicks == [],
        repr(launch_stack.taps),
    )

    # Some already-scrolled feeds retain Reddit's search header and therefore
    # hit the generic ``reddit`` state even though they still lack the strict
    # Home+bottom-navigation proof.  The same Day1-only terminal seed must be
    # returned for that variant; the default route remains unchanged.
    sparse_with_search = [
        Node(ntype="ocr", text="Search Reddit", x=220, y=95, w=300, h=30),
        Node(ntype="ocr", text="r/example 2h", x=40, y=260, w=180, h=24),
        Node(ntype="ocr", text="A sparse feed headline", x=40, y=330, w=360, h=28),
        Node(ntype="ocr", text="Another card anchor", x=40, y=720, w=310, h=28),
    ]
    synthetic_blob = "synthetic sparse search feed"
    reddit_open._transient_dump = lambda *_args, **_kwargs: (
        synthetic_blob,
        sparse_with_search,
    )
    reddit_open.time.sleep = lambda _seconds: None
    reddit_open._save_confirmed_reddit_dump = lambda *_args, **_kwargs: None
    try:
        generic_search_result = reddit_open._confirm_container_launch(
            FakeStack(), object(), lambda _message: None, "a0002"
        )
        opted_search_result = reddit_open._confirm_container_launch(
            FakeStack(),
            object(),
            lambda _message: None,
            "a0002",
            allow_existing_sparse_feed=True,
        )
    finally:
        reddit_open._transient_dump = original_dump
        reddit_open.time.sleep = original_sleep
        reddit_open._save_confirmed_reddit_dump = original_save
    check(
        "sparse-search-reddit-state-seeds-only-opted-day1",
        reddit_open.post_selection_state(sparse_with_search, "a0002") == "reddit"
        and generic_search_result is None
        and isinstance(opted_search_result, FeedCleanupProof),
    )

    decorated_landing_markers = (
        "Continue with Apple >",
        "Log in or sign up to continue",
        "Use email or username >",
    )
    reddit_open.time.sleep = lambda _seconds: None
    reddit_open._save_confirmed_reddit_dump = lambda *_args, **_kwargs: None
    landing_results: list[object] = []
    landing_stacks: list[FakeStack] = []
    raw_terminal_results: list[object] = []
    try:
        for marker in decorated_landing_markers:
            landing_over_feed = sparse_with_search + [
                Node(
                    ntype="ocr",
                    text=marker,
                    x=110,
                    y=880,
                    w=500,
                    h=32,
                )
            ]
            reddit_open._transient_dump = (
                lambda *_args, frame=landing_over_feed, **_kwargs: (
                    "synthetic landing over feed",
                    frame,
                )
            )
            landing_stack = FakeStack()
            landing_stacks.append(landing_stack)
            raw_terminal_results.append(
                reddit_open.make_terminal_feed_cleanup_proof(landing_over_feed)
            )
            landing_results.append(
                reddit_open._confirm_container_launch(
                    landing_stack,
                    object(),
                    lambda _message: None,
                    "a0002",
                    allow_existing_sparse_feed=True,
                )
            )
    finally:
        reddit_open._transient_dump = original_dump
        reddit_open.time.sleep = original_sleep
        reddit_open._save_confirmed_reddit_dump = original_save
    check(
        "sparse-launch-decorated-login-siwa-never-seeds-proof",
        all(isinstance(item, FeedCleanupProof) for item in raw_terminal_results)
        and landing_results == [None] * len(decorated_landing_markers)
        and all(stack.taps == [] and stack.flicks == [] for stack in landing_stacks),
    )

    strict_with_landing = [
        Node(
            ntype="ocr",
            text=f"Strict feed content anchor {index}",
            x=40,
            y=260 + index * 150,
            w=440,
            h=28,
        )
        for index in range(4)
    ] + [
        Node(ntype="ocr", text="Home", x=40, y=1190, w=90, h=22),
        Node(ntype="ocr", text="Inbox", x=610, y=1190, w=100, h=22),
        Node(
            ntype="ocr",
            text="Continue with Apple >",
            x=110,
            y=920,
            w=440,
            h=32,
        ),
    ]
    strict_stack = FakeStack()
    try:
        day1.wait_for_strict_feed(
            strict_stack,
            object(),
            "a0002",
            lambda _message: None,
            sleeper=lambda _seconds: None,
            dumper=lambda *_args, **_kwargs: ("synthetic", strict_with_landing),
        )
    except PhoneError as exc:
        strict_blocked = "login/signup marker" in str(exc)
    else:
        strict_blocked = False
    check(
        "sparse-launch-strict-fallback-gates-landing-before-proof",
        strict_blocked
        and reddit_open.make_feed_cleanup_proof(strict_with_landing) is not None
        and strict_stack.flicks == []
        and strict_stack.taps == [],
    )

    wrapper_calls: list[dict] = []
    original_open = day1.open_container

    def wrapped_open(*_args, **kwargs):
        wrapper_calls.append(dict(kwargs))
        return terminal

    day1.open_container = wrapped_open
    try:
        wrapper_proof = day1.open_day1_container(
            FakeStack(), object(), lambda _message: None, "a0002", "fixture@example"
        )
    finally:
        day1.open_container = original_open
    check(
        "sparse-launch-opt-in-is-local-to-day1-wrapper",
        wrapper_proof is terminal
        and wrapper_calls == [{"allow_existing_sparse_feed": True}],
        repr(wrapper_calls),
    )

    class FixtureDumper:
        def __init__(self, order: list[str]) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.order = order

        def __call__(self, _stack, _phone, stem, _log, **kwargs):
            self.calls.append((stem, dict(kwargs)))
            self.order.append("dump:" + stem)
            return blob, nodes

    class OrderedStack(FakeStack):
        def __init__(self, order: list[str]) -> None:
            super().__init__()
            self.order = order

        def interp_flick(self, *args, **kwargs) -> None:
            self.order.append("flick")
            super().interp_flick(*args, **kwargs)

    row = ready_row()
    ready = day1.validate_ready_slot("a0002", row)
    order: list[str] = []
    stack = OrderedStack(order)
    dumper = FixtureDumper(order)
    clock = FakeClock()
    closes: list[FeedCleanupProof] = []
    marks: list[str] = []
    result = day1.run_day1_slot(
        ready,
        object(),
        stack,
        10,
        lambda _message: None,
        rng=FakeRng(),
        clock=clock,
        sleeper=clock.sleep_to_deadline,
        slot_lookup=lambda _sid: dict(row),
        opener=lambda *_args: terminal,
        dumper=dumper,
        proof_maker=lambda _nodes: None,
        proof_advancer=reddit_open.advance_feed_cleanup_proof,
        terminal_proof_maker=reddit_open.make_terminal_feed_cleanup_proof,
        springboard_check=reddit_open.proven_springboard,
        protected_marker=reddit_open.protected_reddit_marker,
        closer=lambda _stack, _phone, _log, *, feed_proof: (
            closes.append(feed_proof) or True
        ),
        marker=lambda sid, _log: marks.append(sid),
    )
    launch_dump = "dump:930r_a0002_launch_1"
    check(
        "sparse-launch-fresh-no-gesture-proof-precedes-first-flick",
        launch_dump in order
        and "flick" in order
        and order.index(launch_dump) < order.index("flick")
        and result.flicks == 1,
        repr(order),
    )
    check(
        "sparse-launch-stabilized-session-closes-and-marks",
        len(closes) == 1 and marks == ["a0002"],
    )

    # The cleanup-only launch seed must never authorize a gesture by itself.
    # If no fresh frame can be related to it, Day 1 closes from the newest
    # terminal frame and fails without a flick or persisted completion mark.
    refused_stack = FakeStack()
    refused_dumper = FixtureDumper([])
    refused_closes: list[FeedCleanupProof] = []
    refused_marks: list[str] = []
    try:
        day1.run_day1_slot(
            ready,
            object(),
            refused_stack,
            10,
            lambda _message: None,
            rng=FakeRng(),
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
            slot_lookup=lambda _sid: dict(row),
            opener=lambda *_args: terminal,
            dumper=refused_dumper,
            proof_maker=lambda _nodes: None,
            proof_advancer=lambda _proof, _nodes: None,
            terminal_proof_maker=reddit_open.make_terminal_feed_cleanup_proof,
            springboard_check=reddit_open.proven_springboard,
            protected_marker=reddit_open.protected_reddit_marker,
            closer=lambda _stack, _phone, _log, *, feed_proof: (
                refused_closes.append(feed_proof) or True
            ),
            marker=lambda sid, _log: refused_marks.append(sid),
        )
    except PhoneError as exc:
        refused = "did not stabilize after sparse container launch" in str(exc)
    else:
        refused = False
    check(
        "sparse-launch-seed-alone-never-authorizes-flick",
        refused
        and refused_stack.flicks == []
        and len(refused_dumper.calls) == day1.TERMINAL_RECOVERY_ATTEMPTS,
    )
    check(
        "sparse-launch-unstable-path-cleans-up-without-mark",
        len(refused_closes) == 1 and refused_marks == [],
    )

    recovery_landing_nodes = nodes + [
        Node(
            ntype="ocr",
            text="Continue with Apple >",
            x=110,
            y=880,
            w=440,
            h=32,
        )
    ]
    recovery_blocked_stack = FakeStack()
    recovery_blocked_closes: list[FeedCleanupProof] = []
    recovery_blocked_marks: list[str] = []
    try:
        day1.run_day1_slot(
            ready,
            object(),
            recovery_blocked_stack,
            10,
            lambda _message: None,
            rng=FakeRng(),
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
            slot_lookup=lambda _sid: dict(row),
            opener=lambda *_args: terminal,
            dumper=lambda *_args, **_kwargs: (
                "synthetic landing recovery",
                recovery_landing_nodes,
            ),
            proof_maker=reddit_open.make_feed_cleanup_proof,
            proof_advancer=reddit_open.advance_feed_cleanup_proof,
            terminal_proof_maker=reddit_open.make_terminal_feed_cleanup_proof,
            springboard_check=reddit_open.proven_springboard,
            interaction_blocker=reddit_open.day1_sparse_launch_blocker,
            protected_marker=reddit_open.protected_reddit_marker,
            closer=lambda _stack, _phone, _log, *, feed_proof: (
                recovery_blocked_closes.append(feed_proof) or True
            ),
            marker=lambda sid, _log: recovery_blocked_marks.append(sid),
        )
    except PhoneError as exc:
        recovery_blocked = "login/signup marker" in str(exc)
    else:
        recovery_blocked = False
    check(
        "sparse-launch-recovery-frame-blocker-precedes-proof-advance",
        recovery_blocked
        and recovery_blocked_stack.flicks == []
        and recovery_blocked_stack.taps == []
        and recovery_blocked_closes == []
        and recovery_blocked_marks == [],
    )


def decorated_attribution_regressions() -> None:
    """Accept bounded Reddit OCR decoration without admitting generic prose."""
    _launch_blob, launch_nodes = captured_fixture("day1_sparse_launch_live")
    _blob, post_nodes = captured_fixture("day1_post_sparse_live")
    _tail_blob, tail_nodes = captured_fixture("day1_ad_tail_live")
    post_terminal = reddit_open.make_terminal_feed_cleanup_proof(post_nodes)
    check(
        "decorated-attribution-real-post-flick-fixture-proven",
        reddit_open._reddit_feed_structural_evidence(post_nodes)
        and isinstance(post_terminal, FeedCleanupProof),
    )

    launch_terminal = reddit_open.make_terminal_feed_cleanup_proof(launch_nodes)
    launch_actionable = (
        reddit_open.advance_feed_cleanup_proof(launch_terminal, launch_nodes)
        if launch_terminal is not None
        else None
    )
    direct_post_advance = (
        reddit_open.advance_feed_cleanup_proof(launch_actionable, post_nodes)
        if launch_actionable is not None
        else None
    )
    recovered_post = (
        reddit_open.advance_feed_cleanup_proof(post_terminal, post_nodes)
        if post_terminal is not None
        else None
    )
    check(
        "decorated-attribution-real-934-to-935-terminal-recovery",
        isinstance(launch_actionable, FeedCleanupProof)
        and direct_post_advance is None
        and isinstance(post_terminal, FeedCleanupProof)
        and isinstance(recovered_post, FeedCleanupProof),
    )

    post_actionable = (
        reddit_open.advance_feed_cleanup_proof(post_terminal, post_nodes)
        if post_terminal is not None
        else None
    )
    direct_tail_advance = (
        reddit_open.advance_feed_cleanup_proof(post_actionable, tail_nodes)
        if post_actionable is not None
        else None
    )
    tail_terminal = reddit_open.make_terminal_feed_cleanup_proof(tail_nodes)
    recovered_tail = (
        reddit_open.advance_feed_cleanup_proof(tail_terminal, tail_nodes)
        if tail_terminal is not None
        else None
    )
    check(
        "decorated-attribution-real-935-to-936-terminal-recovery",
        isinstance(post_actionable, FeedCleanupProof)
        and direct_tail_advance is None
        and isinstance(tail_terminal, FeedCleanupProof)
        and isinstance(recovered_tail, FeedCleanupProof),
    )

    def attribution_frame(text: str) -> list[Node]:
        return [
            Node(ntype="ocr", text=text, x=40, y=300, w=310, h=28),
            Node(
                ntype="ocr",
                text="First unique content anchor",
                x=40,
                y=470,
                w=420,
                h=28,
            ),
            Node(
                ntype="ocr",
                text="Second unique content anchor",
                x=40,
                y=650,
                w=430,
                h=28,
            ),
        ]

    accepted = (
        "r/Haircare 1d",
        "(• r/Haircare 1d",
        "(h r/Haircare 1d",
        "@u/example-user 4h",
    )
    rejected = (
        "promoted • r/Haircare 1d",
        "read this r/Haircare 1d",
        "(· r/Haircare 1d",
        "(∙ r/Haircare 1d",
        "(• reddit/Haircare 1d",
        "(• r/",
        "(x r/Haircare 1d",
        "(h u/Haircare 1d",
        "(h r/Haircare",
        "(hh r/Haircare 1d",
        "prefix (h r/Haircare 1d",
        "(h r/Haircare 0d",
        "https://example.test/r/Haircare",
        "https://example.test/path promoted ad",
        "1/2 off promoted ad",
    )
    check(
        "decorated-attribution-bounded-prefix-variants",
        all(
            reddit_open._reddit_feed_structural_evidence(attribution_frame(text))
            for text in accepted
        ),
    )
    check(
        "decorated-attribution-generic-prose-and-urls-rejected",
        all(
            not reddit_open._reddit_feed_structural_evidence(
                attribution_frame(text)
            )
            for text in rejected
        ),
    )

    # Preserve the separately observed OCR-confused promoted attribution, but
    # only in its exact anchored ``4/handle Ad`` shape.
    _ad_blob, ad_nodes = captured_fixture("feed_939_after")
    check(
        "decorated-attribution-observed-ocr-ad-shape-remains-proven",
        reddit_open._reddit_feed_structural_evidence(ad_nodes)
        and isinstance(
            reddit_open.make_terminal_feed_cleanup_proof(ad_nodes),
            FeedCleanupProof,
        ),
    )

    protected_nodes = post_nodes + [
        Node(
            ntype="ocr",
            text="Verify your email",
            x=100,
            y=220,
            w=360,
            h=30,
        )
    ]
    springboard_nodes = post_nodes + [
        Node(ntype="ocr", text="Weather", x=40, y=220, w=100, h=20),
        Node(ntype="ocr", text="Books", x=220, y=380, w=90, h=20),
        Node(ntype="ocr", text="Photos", x=400, y=530, w=90, h=20),
        Node(ntype="ocr", text="Settings", x=580, y=680, w=110, h=20),
        Node(ntype="ocr", text="Q Search", x=310, y=1100, w=120, h=24),
    ]
    check(
        "decorated-attribution-protected-guard-remains-fatal",
        reddit_open.protected_reddit_marker(protected_nodes) == "verify your email"
        and reddit_open.make_terminal_feed_cleanup_proof(protected_nodes) is None,
    )
    check(
        "decorated-attribution-springboard-guard-remains-fatal",
        reddit_open.proven_springboard(springboard_nodes)
        and reddit_open.make_terminal_feed_cleanup_proof(springboard_nodes) is None,
    )


def terminal_recovery_regressions() -> None:
    row = ready_row()
    ready = day1.validate_ready_slot("a0002", row)
    stack = FakeStack()
    dumper = FakeDumper()
    clock = FakeClock()
    terminal = FeedCleanupProof(("terminal-post-flick",), sparse_continuity=True)
    advance_calls = 0
    closes: list[FeedCleanupProof] = []
    marks: list[str] = []

    def recover_after_one_check(_proof, nodes):
        nonlocal advance_calls
        advance_calls += 1
        if advance_calls == 1:
            # The mandatory post-flick frame has valid terminal evidence but
            # cannot be related to the previous feed frame.
            return None
        return proof_for(nodes)

    result = day1.run_day1_slot(
        ready,
        object(),
        stack,
        10,
        lambda _message: None,
        rng=FakeRng(),
        clock=clock,
        sleeper=clock.sleep_to_deadline,
        slot_lookup=lambda _sid: dict(row),
        opener=lambda *_args: None,
        dumper=dumper,
        proof_maker=proof_for,
        proof_advancer=recover_after_one_check,
        terminal_proof_maker=lambda _nodes: terminal,
        springboard_check=lambda _nodes: False,
        protected_marker=lambda _nodes: "",
        closer=lambda _stack, _phone, _log, *, feed_proof: closes.append(
            feed_proof
        )
        or True,
        marker=lambda sid, _log: marks.append(sid),
    )
    recovery_stems = [
        stem for stem, _kwargs in dumper.calls if stem.startswith("932r_")
    ]
    check(
        "terminal-recovery-resumes-after-stable-check",
        result.flicks == 1 and len(recovery_stems) == 1,
        repr(recovery_stems),
    )
    check(
        "terminal-recovery-makes-no-extra-gesture",
        len(stack.flicks) == 1 and stack.taps == [],
    )
    check(
        "terminal-recovery-can-finish-close-and-mark",
        len(closes) == 1 and marks == ["a0002"],
    )

    # If every no-gesture frame remains terminal-only, attempts are bounded
    # and the freshest terminal proof remains available strictly for cleanup.
    bounded_stack = FakeStack()
    bounded_dumper = FakeDumper()
    bounded_sleeps: list[float] = []
    made_terminal: list[FeedCleanupProof] = []

    def refresh_terminal(nodes):
        refreshed = FeedCleanupProof(tuple(nodes), sparse_continuity=True)
        made_terminal.append(refreshed)
        return refreshed

    bounded_proof, bounded_reason = day1.recover_terminal_feed_proof(
        bounded_stack,
        object(),
        "a0002",
        2,
        terminal,
        lambda _message: None,
        sleeper=bounded_sleeps.append,
        dumper=bounded_dumper,
        proof_advancer=lambda _proof, _nodes: None,
        terminal_proof_maker=refresh_terminal,
        springboard_check=lambda _nodes: False,
        protected_marker=lambda _nodes: "",
    )
    check(
        "terminal-recovery-is-bounded",
        len(bounded_dumper.calls) == day1.TERMINAL_RECOVERY_ATTEMPTS
        and bounded_sleeps
        == [day1.FEED_WAIT_SECONDS] * day1.TERMINAL_RECOVERY_ATTEMPTS,
    )
    check(
        "terminal-recovery-retains-fresh-cleanup-only-proof",
        bool(made_terminal)
        and bounded_proof is made_terminal[-1]
        and "did not stabilize" in bounded_reason,
    )
    check(
        "terminal-recovery-bounded-path-has-no-gesture",
        bounded_stack.flicks == [] and bounded_stack.taps == [],
    )

    for name, springboard, protected, expected in (
        ("springboard", True, "", "springboard"),
        ("protected", False, "verify your email", "protected"),
    ):
        fatal_stack = FakeStack()
        fatal_dumper = FakeDumper()
        fatal_proof, fatal_reason = day1.recover_terminal_feed_proof(
            fatal_stack,
            object(),
            "a0002",
            2,
            terminal,
            lambda _message: None,
            sleeper=lambda _seconds: None,
            dumper=fatal_dumper,
            proof_advancer=lambda _proof, _nodes: None,
            terminal_proof_maker=lambda _nodes: None,
            springboard_check=lambda _nodes, value=springboard: value,
            protected_marker=lambda _nodes, value=protected: value,
        )
        check(
            f"terminal-recovery-{name}-is-fatal",
            fatal_proof is None
            and expected in fatal_reason.casefold()
            and len(fatal_dumper.calls) == 1
            and fatal_stack.flicks == []
            and fatal_stack.taps == [],
        )

    class FailingRecoveryDumper(FakeDumper):
        def __call__(self, stack, phone, stem, log, **kwargs):
            if stem.startswith("932r_"):
                raise PhoneError("synthetic recovery dump failure")
            return super().__call__(stack, phone, stem, log, **kwargs)

    failed_recovery_stack = FakeStack()
    failed_recovery_closes: list[FeedCleanupProof] = []
    try:
        day1.run_day1_slot(
            ready,
            object(),
            failed_recovery_stack,
            10,
            lambda _message: None,
            rng=FakeRng(),
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
            slot_lookup=lambda _sid: dict(row),
            opener=lambda *_args: None,
            dumper=FailingRecoveryDumper(),
            proof_maker=proof_for,
            proof_advancer=lambda _proof, _nodes: None,
            terminal_proof_maker=lambda _nodes: terminal,
            springboard_check=lambda _nodes: False,
            protected_marker=lambda _nodes: "",
            closer=lambda _stack, _phone, _log, *, feed_proof: (
                failed_recovery_closes.append(feed_proof) or True
            ),
            marker=lambda *_args: None,
        )
    except PhoneError as exc:
        recovery_dump_failed = "recovery dump failure" in str(exc)
    else:
        recovery_dump_failed = False
    check(
        "terminal-recovery-dump-failure-never-reuses-stale-proof",
        recovery_dump_failed
        and len(failed_recovery_stack.flicks) == 1
        and failed_recovery_closes == [],
    )


def proof_loss_regressions() -> None:
    row = ready_row()
    ready = day1.validate_ready_slot("a0002", row)
    stack = FakeStack()
    dumper = FakeDumper()
    terminal = FeedCleanupProof(("terminal",), sparse_continuity=True)
    closes: list[FeedCleanupProof] = []
    marks: list[str] = []

    try:
        day1.run_day1_slot(
            ready,
            object(),
            stack,
            10,
            lambda _message: None,
            rng=FakeRng(),
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
            slot_lookup=lambda _sid: dict(row),
            opener=lambda *_args: None,
            dumper=dumper,
            proof_maker=proof_for,
            proof_advancer=lambda _proof, _nodes: None,
            terminal_proof_maker=lambda _nodes: terminal,
            springboard_check=lambda _nodes: False,
            protected_marker=lambda _nodes: "",
            closer=lambda _stack, _phone, _log, *, feed_proof: closes.append(
                feed_proof
            )
            or True,
            marker=lambda sid, _log: marks.append(sid),
        )
    except PhoneError as exc:
        stopped = "continuity did not stabilize" in str(exc)
    else:
        stopped = False
    check("loss-stops-after-one-flick", stopped and len(stack.flicks) == 1)
    check("loss-uses-terminal-proof-to-close", closes == [terminal])
    check("loss-never-marks-day1", marks == [])

    # A protected frame cannot become cleanup evidence and therefore permits
    # neither another feed gesture nor a close gesture.
    protected_closes: list[object] = []
    protected_stack = FakeStack()
    try:
        day1.run_day1_slot(
            ready,
            object(),
            protected_stack,
            10,
            lambda _message: None,
            rng=FakeRng(),
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
            slot_lookup=lambda _sid: dict(row),
            opener=lambda *_args: None,
            dumper=FakeDumper(),
            proof_maker=proof_for,
            proof_advancer=lambda _proof, _nodes: None,
            terminal_proof_maker=lambda _nodes: None,
            springboard_check=lambda _nodes: False,
            protected_marker=lambda _nodes: "verify your email",
            closer=lambda *_args, **_kwargs: protected_closes.append(object()) or True,
            marker=lambda *_args: marks.append("bad"),
        )
    except PhoneError:
        protected_stopped = True
    else:
        protected_stopped = False
    check(
        "protected-stops-without-close",
        protected_stopped
        and len(protected_stack.flicks) == 1
        and protected_closes == [],
    )
    check("protected-never-marks-day1", "bad" not in marks)


def close_failure_regression() -> None:
    row = ready_row()
    ready = day1.validate_ready_slot("a0002", row)
    stack = FakeStack()
    dumper = FakeDumper()
    clock = FakeClock()
    close_calls: list[FeedCleanupProof] = []
    marks: list[str] = []

    def failed_close(_stack, _phone, _log, *, feed_proof):
        close_calls.append(feed_proof)
        raise PhoneError("synthetic final Home verification failure")

    try:
        day1.run_day1_slot(
            ready,
            object(),
            stack,
            10,
            lambda _message: None,
            rng=FakeRng(),
            clock=clock,
            sleeper=clock.sleep_to_deadline,
            slot_lookup=lambda _sid: dict(row),
            opener=lambda *_args: None,
            dumper=dumper,
            proof_maker=proof_for,
            proof_advancer=lambda _proof, nodes: proof_for(nodes),
            terminal_proof_maker=lambda _nodes: None,
            springboard_check=lambda _nodes: False,
            protected_marker=lambda _nodes: "",
            closer=failed_close,
            marker=lambda sid, _log: marks.append(sid),
        )
    except PhoneError:
        failed = True
    else:
        failed = False
    check("close-failure-propagates", failed)
    check("close-failure-is-not-retried-with-stale-proof", len(close_calls) == 1)
    check("close-failure-never-marks-day1", marks == [])


def main_connect_once_regression() -> None:
    originals = {
        "get_slot": day1.get_slot,
        "Phone": day1.Phone,
        "TapStack": day1.TapStack,
        "SecretSafeLog": day1.SecretSafeLog,
        "run_day1_slot": day1.run_day1_slot,
    }
    events: list[str] = []

    class FakePhone:
        def __init__(self, log):
            self.log = log

        def connect(self, password, *, also_wda):
            events.append(f"connect:{password}:{also_wda}")

        def close(self):
            events.append("phone-close")

    class MainStack:
        def __init__(self, _phone, _log):
            pass

        def attach(self):
            return "fake"

        def can_tap(self):
            return True

    try:
        day1.get_slot = lambda _sid: ready_row()
        day1.Phone = FakePhone
        day1.TapStack = MainStack
        day1.SecretSafeLog = lambda *_args, **_kwargs: (lambda _message: None)
        day1.run_day1_slot = (
            lambda ready, _phone, _stack, minutes, _log: events.append(
                f"run:{ready.sid}:{minutes}"
            )
        )
        code = day1.main(["a0002", "--minutes", "10"])
    finally:
        for name, value in originals.items():
            setattr(day1, name, value)
    check("main-success", code == 0)
    check(
        "main-connects-exactly-once",
        [event for event in events if event.startswith("connect:")]
        == ["connect::False"],
        repr(events),
    )
    check("main-closes-phone", events[-1:] == ["phone-close"])


def main() -> int:
    validation_regressions()
    duration_and_log_regressions()
    successful_session_regression()
    sparse_launch_regressions()
    decorated_attribution_regressions()
    terminal_recovery_regressions()
    proof_loss_regressions()
    close_failure_regression()
    main_connect_once_regression()
    print(f"SUMMARY pass={len(PASS)} fail={len(FAIL)}", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
