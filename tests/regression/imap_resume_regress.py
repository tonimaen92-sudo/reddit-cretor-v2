# UTF-8
"""Offline regressions for IMAP candidate selection and Verify resume.

No phone, USB, SSH, network, or secrets access.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from email.message import EmailMessage
from friend_poster.device import icloud_code as imap_code  # noqa: E402
from friend_poster.reddit import reddit_create_day1 as create  # noqa: E402
from friend_poster.reddit import reddit_enter_code as enter_code  # noqa: E402
from friend_poster.device import ui_tree as ui_tree_module  # noqa: E402
from friend_poster.core.config import DUMP_DIR  # noqa: E402
from friend_poster.reddit.reddit_open import FeedCleanupProof  # noqa: E402
from friend_poster.device.ssh_device import PhoneError  # noqa: E402
from friend_poster.device.ui_tree import Node  # noqa: E402


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(("PASS " if condition else "FAIL ") + name + (" :: " + detail if detail else ""))


def node(text: str, x: int = 40, y: int = 220) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=y, w=420, h=30)


def saved_dump_nodes(stem: str) -> list[Node]:
    with open(os.path.join(DUMP_DIR, stem + ".json"), encoding="utf-8") as handle:
        return ui_tree_module.parse_nodes(handle.read())


def expect_phone_error(name: str, call, contains: str = "") -> None:
    try:
        call()
    except PhoneError as exc:
        check(name, not contains or contains.casefold() in str(exc).casefold())
    else:
        check(name, False, "PhoneError was not raised")


def message_bytes(*, code: str, date_header: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "noreply@redditmail.com"
    msg["To"] = "alias@icloud.com"
    msg["Subject"] = f"{code} is your Reddit verification code"
    msg["Date"] = date_header
    msg.set_content("Enter your verification code to finish signing up.")
    return msg.as_bytes()


class FakeMail:
    def __init__(self, searches, messages=None) -> None:
        self.searches = searches
        self.messages = messages or {}
        self.search_calls: list[str] = []
        self.fetch_calls: list[tuple[bytes, str]] = []
        self.logged_out = False

    def search(self, _charset, query):
        self.search_calls.append(query)
        return "OK", [self.searches.get(query, b"")]

    def fetch(self, message_id, query):
        self.fetch_calls.append((message_id, query))
        metadata, raw = self.messages[message_id]
        return "OK", [(metadata, raw), b")"]

    def logout(self):
        self.logged_out = True


def imap_regressions() -> None:
    searches = {
        '(FROM "reddit")': b"2 10",
        '(SUBJECT "verification code")': b"3 9",
        '(SUBJECT "reddit")': b"10 11",
        "(UNSEEN)": b"4",
        "ALL": b"1 2 3 4 5 6 7 8 9 10 11 12",
    }
    mail = FakeMail(searches)
    check(
        "imap-unions-all-searches-newest-first",
        imap_code._candidate_message_ids(mail)
        == [str(value).encode() for value in range(12, 0, -1)],
    )
    check(
        "imap-runs-every-query-and-all",
        mail.search_calls
        == [
            '(FROM "reddit")',
            '(SUBJECT "verification code")',
            '(SUBJECT "reddit")',
            "(UNSEEN)",
            "ALL",
        ],
    )

    historical = FakeMail(
        {
            '(FROM "reddit")': b" ".join(
                str(value).encode() for value in range(1, 470)
            ),
            '(SUBJECT "verification code")': b" ".join(
                str(value).encode() for value in range(400, 557)
            ),
            '(SUBJECT "reddit")': b" ".join(
                str(value).encode() for value in range(398, 557)
            ),
            "(UNSEEN)": b" ".join(
                str(value).encode() for value in range(1, 1060)
            ),
            "ALL": b" ".join(
                str(value).encode() for value in range(1, 1258)
            ),
        }
    )
    bounded = imap_code._candidate_message_ids(historical)
    check(
        "imap-historical-union-is-globally-bounded-newest-first",
        len(bounded) == imap_code.LATEST_CANDIDATE_LIMIT
        and bounded[0] == b"1257"
        and bounded[-1] == b"1218"
        and bounded == sorted(
            bounded, key=imap_code._numeric_message_id, reverse=True
        ),
    )

    old_raw = message_bytes(
        code="123456", date_header="Fri, 09 Oct 2026 16:00:00 +0000"
    )
    current_raw = message_bytes(
        code="654321", date_header="Fri, 09 Oct 2026 12:00:00 +0000"
    )
    current_mail = FakeMail(
        {
            '(FROM "reddit")': b"100",
            '(SUBJECT "verification code")': b"100",
            '(SUBJECT "reddit")': b"100",
            "(UNSEEN)": b"",
            "ALL": b"100 1041",
        },
        {
            b"100": (
                b'100 (INTERNALDATE "09-Oct-2026 12:00:00 +0000")',
                old_raw,
            ),
            b"1041": (
                b'1041 (INTERNALDATE "09-Oct-2026 16:00:00 +0000")',
                current_raw,
            ),
        },
    )
    original_connect = imap_code._connect
    imap_code._connect = lambda **_kwargs: current_mail
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            result = imap_code.fetch_reddit_code(
                after_ts=1791561540.0,
                email_hint="alias@icloud.com",
            )
    finally:
        imap_code._connect = original_connect
    check("imap-latest-all-finds-current-message", result == "654321")
    check(
        "imap-fetches-body-peek-with-internaldate",
        current_mail.fetch_calls
        and current_mail.fetch_calls[0]
        == (b"1041", "(INTERNALDATE BODY.PEEK[])")
        and current_mail.logged_out,
    )
    check("imap-does-not-print-code-or-subject", captured.getvalue() == "")

    old_only = FakeMail(
        {
            '(FROM "reddit")': b"100",
            '(SUBJECT "verification code")': b"",
            '(SUBJECT "reddit")': b"",
            "(UNSEEN)": b"",
            "ALL": b"100",
        },
        {
            b"100": (
                b'100 (INTERNALDATE "09-Oct-2026 12:00:00 +0000")',
                old_raw,
            ),
        },
    )
    imap_code._connect = lambda **_kwargs: old_only
    try:
        rejected = imap_code.fetch_reddit_code(after_ts=1791561540.0)
    finally:
        imap_code._connect = original_connect
    check(
        "imap-internaldate-overrides-newer-date-header",
        rejected is None,
    )


def imap_timeout_regressions() -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.closed = False

        def settimeout(self, value) -> None:
            self.timeouts.append(float(value))

        def close(self) -> None:
            self.closed = True

    class ConnectMail(FakeMail):
        def __init__(self) -> None:
            super().__init__({query: b"" for query in (
                '(FROM "reddit")',
                '(SUBJECT "verification code")',
                '(SUBJECT "reddit")',
                "(UNSEEN)",
                "ALL",
            )})
            self.sock = FakeSocket()
            self.login_calls: list[tuple[str, str]] = []
            self.select_calls: list[tuple[str, bool]] = []
            self.shutdown_called = False

        def login(self, user, password):
            self.login_calls.append((user, password))

        def select(self, mailbox, readonly=False):
            self.select_calls.append((mailbox, readonly))
            return "OK", [b""]

        def shutdown(self):
            self.shutdown_called = True
            self.sock.close()

    constructor: list[tuple] = []
    mail = ConnectMail()
    original_ssl = imap_code.imaplib.IMAP4_SSL
    original_secrets = imap_code.load_secrets
    original_monotonic = imap_code.time.monotonic
    imap_code.load_secrets = lambda: {
        "icloud_user": "private-user",
        "icloud_app_password": "private-password",
    }
    imap_code.time.monotonic = lambda: 100.0

    def fake_ssl(host, port, **kwargs):
        constructor.append((host, port, kwargs))
        return mail

    imap_code.imaplib.IMAP4_SSL = fake_ssl
    try:
        connected = imap_code._connect(deadline=130.0)
    finally:
        imap_code.imaplib.IMAP4_SSL = original_ssl
        imap_code.load_secrets = original_secrets
        imap_code.time.monotonic = original_monotonic
    check(
        "imap-connect-login-select-have-finite-timeouts",
        connected is mail
        and constructor == [("imap.mail.me.com", 993, {"timeout": 10.0})]
        and len(mail.sock.timeouts) == 2
        and all(value == 10.0 for value in mail.sock.timeouts)
        and len(mail.login_calls) == 1
        and mail.select_calls == [("INBOX", True)],
    )

    hung = ConnectMail()
    hung.search = lambda *_args: (_ for _ in ()).throw(TimeoutError("synthetic hang"))
    original_connect = imap_code._connect
    imap_code._connect = lambda **_kwargs: hung
    try:
        try:
            imap_code.fetch_reddit_code()
        except TimeoutError:
            search_timed_out = True
        else:
            search_timed_out = False
    finally:
        imap_code._connect = original_connect
    check(
        "imap-hung-search-aborts-connection",
        search_timed_out and hung.shutdown_called and hung.sock.closed,
    )

    logout_hang = ConnectMail()
    logout_hang.logout = lambda: (_ for _ in ()).throw(TimeoutError("logout hang"))
    imap_code._connect = lambda **_kwargs: logout_hang
    try:
        result = imap_code.fetch_reddit_code()
    finally:
        imap_code._connect = original_connect
    check(
        "imap-hung-logout-forces-shutdown",
        result is None and logout_hang.shutdown_called and logout_hang.sock.closed,
    )

    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = FakeClock()
    calls: list[float] = []
    logs: list[str] = []
    original_fetch = imap_code.fetch_reddit_code
    original_wait = imap_code._wait_for_retry
    original_monotonic = imap_code.time.monotonic

    def timed_out_fetch(*_args, deadline=None, **_kwargs):
        calls.append(deadline)
        clock.now += min(10.0, max(0.0, deadline - clock.now))
        raise TimeoutError("synthetic blocked operation")

    imap_code.fetch_reddit_code = timed_out_fetch
    imap_code._wait_for_retry = lambda seconds, _stop: setattr(
        clock, "now", clock.now + seconds
    )
    imap_code.time.monotonic = clock.monotonic
    timeout_typed = False
    try:
        try:
            imap_code.poll_reddit_code(log=logs.append, timeout=25)
        except RuntimeError as exc:
            timeout_message = str(exc)
            timeout_typed = isinstance(exc, imap_code.VerificationCodeTimeout)
        else:
            timeout_message = ""
    finally:
        imap_code.fetch_reddit_code = original_fetch
        imap_code._wait_for_retry = original_wait
        imap_code.time.monotonic = original_monotonic
    check(
        "imap-total-deadline-is-hard-bounded",
        clock.now == 25.0
        and len(calls) == 2
        and "No 6-digit" in timeout_message
        and timeout_typed,
    )
    check(
        "imap-timeout-progress-is-redacted",
        any("IMAP polling started" in line for line in logs)
        and any("IMAP retry (TimeoutError)" in line for line in logs)
        and any("timed out" in line for line in logs)
        and all(token not in " ".join(logs) for token in ("private-user", "private-password")),
    )

    clock = FakeClock()
    pass_deadlines: list[float] = []

    def pass_timeout_fetch(*_args, deadline=None, **_kwargs):
        pass_deadlines.append(deadline)
        clock.now = deadline
        raise TimeoutError("synthetic pass timeout")

    imap_code.fetch_reddit_code = pass_timeout_fetch
    imap_code._wait_for_retry = lambda seconds, _stop: setattr(
        clock, "now", clock.now + seconds
    )
    imap_code.time.monotonic = clock.monotonic
    try:
        try:
            imap_code.poll_reddit_code(timeout=55)
        except RuntimeError:
            pass
    finally:
        imap_code.fetch_reddit_code = original_fetch
        imap_code._wait_for_retry = original_wait
        imap_code.time.monotonic = original_monotonic
    check(
        "imap-each-scan-pass-has-a-bounded-retry-deadline",
        pass_deadlines == [20.0, 46.0, 55.0]
        and all(
            later > earlier
            for earlier, later in zip(pass_deadlines, pass_deadlines[1:])
        ),
    )

    class CancelOnWait:
        def __init__(self) -> None:
            self.cancelled = False
            self.wait_calls: list[float] = []

        def is_set(self) -> bool:
            return self.cancelled

        def wait(self, seconds: float) -> bool:
            self.wait_calls.append(seconds)
            self.cancelled = True
            return True

    stop = CancelOnWait()
    cancel_logs: list[str] = []
    imap_code.fetch_reddit_code = lambda **_kwargs: None
    try:
        try:
            imap_code.poll_reddit_code(log=cancel_logs.append, timeout=150, stop=stop)
        except imap_code.PollCancelled:
            cancelled = True
        else:
            cancelled = False
    finally:
        imap_code.fetch_reddit_code = original_fetch
    check(
        "imap-stop-interrupts-retry-wait",
        cancelled and stop.wait_calls == [6.0],
    )
    check(
        "imap-stop-log-preserves-verify-screen",
        any("remains on Verify your email" in line for line in cancel_logs),
    )

    already_stopped = CancelOnWait()
    already_stopped.cancelled = True
    immediate_logs: list[str] = []
    try:
        imap_code.poll_reddit_code(
            log=immediate_logs.append,
            timeout=150,
            stop=already_stopped,
        )
    except imap_code.PollCancelled:
        immediate_cancelled = True
    else:
        immediate_cancelled = False
    check(
        "imap-immediate-stop-is-logged-without-network",
        immediate_cancelled
        and already_stopped.wait_calls == []
        and any("remains on Verify your email" in line for line in immediate_logs),
    )


def invalid_code_node_regressions() -> None:
    live_nodes = saved_dump_nodes("889_user_0")
    marker = enter_code.invalid_code_message(live_nodes)
    resend = enter_code.resend_control(live_nodes)
    merged = next(
        (
            item
            for item in live_nodes
            if ui_tree_module.collapsed_text(item.labels())
            == "check vour spam folder or resend"
        ),
        None,
    )
    check(
        "invalid-code-live-exact-marker",
        marker is not None and marker.center() == (275, 505),
    )
    check(
        "invalid-code-live-value-is-bounded-without-printing",
        enter_code.verification_code_value(live_nodes) is not None,
    )
    check(
        "invalid-code-live-derived-resend-word",
        resend is not None
        and resend.ntype == "derived-word"
        and 510 <= resend.center()[0] <= 611
        and 699 <= resend.center()[1] <= 719
        and merged is not None
        and resend.center() != merged.center(),
    )
    unrelated = [
        node("Please enter a valid verification code"),
        node("We may resend this later", x=100, y=700),
    ]
    check(
        "invalid-code-unrelated-resend-text-rejected",
        enter_code.resend_control(unrelated) is None,
    )
    nonterminal = [node("Check your spam folder or Resend now", x=100, y=700)]
    check(
        "invalid-code-nonterminal-resend-rejected",
        enter_code.resend_control(nonterminal) is None,
    )

    class FakeStack:
        def __init__(self) -> None:
            self.events: list[object] = []

        def tap_xy(self, x, y):
            self.events.append(("tap", x, y))

        def press_backspace(self, count):
            self.events.append(("backspace", count))

    verify = node("Verify your email", x=80, y=180)
    field = node("Verification code", x=80, y=400, )
    stale = node("112233", x=95, y=445)
    invalid = node("Please enter a valid verification code", x=80, y=500)
    resend_line = node("Check vour spam folder or Resend", x=128, y=699)
    resend_line.w = 487
    resend_line.h = 20
    initial = [verify, field, stale, invalid, resend_line]
    partial = [verify, field, node("233", x=95, y=445)]
    check(
        "invalid-code-partial-value-is-not-cleared",
        enter_code.verification_code_value(partial) is None
        and enter_code.verification_code_digits(partial) is not None,
    )
    after_resend = [verify, field, stale, invalid, resend_line]
    cleared = [verify, field, invalid, resend_line]
    responses = iter((("", after_resend), ("", cleared)))
    original_dump = enter_code.dump_named
    original_time = enter_code.time.time
    original_sleep = enter_code.time.sleep
    logs: list[str] = []
    stack = FakeStack()
    enter_code.dump_named = lambda *_args, **_kwargs: next(responses)
    enter_code.time.time = lambda: 12_345.5
    enter_code.time.sleep = lambda _seconds: None
    try:
        resend_at, stale_value, result_nodes = enter_code.resend_invalid_code_once(
            stack,
            object(),
            initial,
            logs.append,
        )
    finally:
        enter_code.dump_named = original_dump
        enter_code.time.time = original_time
        enter_code.time.sleep = original_sleep
    taps = [event for event in stack.events if event[0] == "tap"]
    check(
        "invalid-code-resend-once-and-clear",
        resend_at == 12_345.0
        and stale_value == "112233"
        and len(taps) == 2
        and 510 <= taps[0][1] <= 611
        and stack.events[-1] == ("backspace", 8)
        and enter_code.verification_code_value(result_nodes) is None,
    )
    check(
        "invalid-code-ui-logs-private",
        all("112233" not in line and "@" not in line for line in logs),
    )

    no_resend_stack = FakeStack()
    expect_phone_error(
        "invalid-code-missing-resend-does-not-tap",
        lambda: enter_code.resend_invalid_code_once(
            no_resend_stack,
            object(),
            [verify, field, stale, invalid],
            lambda _message: None,
        ),
        "did not guess",
    )
    check(
        "invalid-code-missing-resend-no-action",
        no_resend_stack.events == [],
    )


def invalid_code_recovery_regressions() -> None:
    originals = {
        "resend_invalid_code_once": create.resend_invalid_code_once,
        "poll_reddit_code": create.poll_reddit_code,
        "type_code": create.type_code,
        "dump_named": create.dump_named,
        "log": create.log,
    }
    logs: list[str] = []
    events: list[object] = []
    polls: list[dict] = []
    invalid_nodes = [node("Please enter a valid verification code")]
    username_nodes = [node("Create your username")]

    create.resend_invalid_code_once = lambda *_args, **_kwargs: (
        events.append("resend") or (4_321.25, "112233", [])
    )

    def poll_new(**kwargs):
        polls.append(kwargs)
        return "445566"

    create.poll_reddit_code = poll_new
    create.type_code = lambda *_args: (
        events.append("type-replacement") or username_nodes
    )
    create.dump_named = lambda *_args, **_kwargs: ("", username_nodes)
    create.log = logs.append
    recovery_stop = object()
    recovery_log = logs.append
    error = ""
    try:
        result = create.recover_invalid_verification_code(
            object(),
            object(),
            "alias@icloud.com",
            invalid_nodes,
            log_fn=recovery_log,
            stop=recovery_stop,
        )
    except Exception as exc:  # pragma: no cover - diagnostic below
        result = []
        error = str(exc)
    finally:
        for name, value in originals.items():
            setattr(create, name, value)
    check("invalid-code-recovery-reaches-username", not error and result == username_nodes)
    check(
        "invalid-code-recovery-strict-new-mail",
        len(polls) == 1
        and polls[0].get("after_ts") == 4_321.25
        and polls[0].get("email_hint") == "alias@icloud.com"
        and polls[0].get("log") == recovery_log
        and polls[0].get("stop") is recovery_stop
        and events == ["resend", "type-replacement"],
    )
    check(
        "invalid-code-recovery-logs-private",
        all(
            token not in line
            for line in logs
            for token in ("112233", "445566", "alias@icloud.com")
        ),
    )

    events.clear()
    create.resend_invalid_code_once = lambda *_args, **_kwargs: (
        events.append("resend") or (4_500.0, "112233", [])
    )
    create.poll_reddit_code = lambda **_kwargs: "112233"
    create.type_code = lambda *_args: events.append("typed-stale")
    create.log = lambda _message: None
    try:
        expect_phone_error(
            "invalid-code-stale-result-not-entered",
            lambda: create.recover_invalid_verification_code(
                object(), object(), "alias@icloud.com", invalid_nodes
            ),
            "already-rejected",
        )
    finally:
        for name, value in originals.items():
            setattr(create, name, value)
    check(
        "invalid-code-stale-result-stops-after-one-resend",
        events == ["resend"],
    )

    events.clear()
    create.resend_invalid_code_once = lambda *_args, **_kwargs: (
        events.append("resend") or (4_600.0, "112233", [])
    )
    create.poll_reddit_code = lambda **_kwargs: "445566"
    create.type_code = lambda *_args: invalid_nodes
    create.log = lambda _message: None
    try:
        expect_phone_error(
            "invalid-code-second-rejection-is-terminal",
            lambda: create.recover_invalid_verification_code(
                object(), object(), "alias@icloud.com", invalid_nodes
            ),
            "not tapped again",
        )
    finally:
        for name, value in originals.items():
            setattr(create, name, value)
    check(
        "invalid-code-second-rejection-one-resend-only",
        events == ["resend"],
    )


def verify_resume_regressions() -> None:
    originals = {
        name: getattr(create, name)
        for name in (
            "get_slot",
            "email_verification_state",
            "mark_email_verification",
            "clear_email_verification",
            "slot_reddit_done",
            "dump_named",
            "run_to_verify",
            "poll_reddit_code",
            "type_code",
            "wait_named",
            "keep_suggested_username",
            "type_password",
            "tap_woman",
            "pick_interests",
            "wait_feed",
            "mark_reddit_created",
            "_close_and_finalize_cleanup",
            "log",
        )
    }
    original_time = create.time.time
    events: list[object] = []
    logs: list[str] = []
    proof = FeedCleanupProof(reference_nodes=tuple())
    verification = {"value": None}

    create.get_slot = lambda _sid: {
        "id": "a0001",
        "ghost": True,
        "email": "alias@icloud.com",
        "email_label": "a0001",
        "email_verification": verification["value"],
        "reddit": False,
    }
    create.email_verification_state = lambda row: row.get("email_verification")

    def mark_verification(_sid, index, label, phase, _log=None):
        verification["value"] = {
            "replacement_index": index,
            "label": label,
            "phase": phase,
            "updated_at": "2026-10-16T21:28:00Z",
        }
        return create.get_slot("a0001")

    create.mark_email_verification = mark_verification
    create.clear_email_verification = lambda *_args, **_kwargs: (
        verification.update(value=None) or create.get_slot("a0001")
    )
    create.slot_reddit_done = lambda _slot: False
    create.dump_named = lambda *args, **kwargs: (
        "",
        [node("Verify your email"), node("Verification code", y=520)],
    )
    create.run_to_verify = lambda *args, **kwargs: events.append("run-to-verify")

    def fake_poll(**kwargs):
        events.append(("poll", kwargs))
        return "654321"

    create.poll_reddit_code = fake_poll
    create.type_code = lambda *_args: (
        events.append("type-code") or [node("Create your username")]
    )
    create.wait_named = lambda _stack, _phone, names, *_args, **_kwargs: [node(names[0])]
    create.keep_suggested_username = lambda *_args: events.append("username")
    create.type_password = lambda *_args: events.append("password")
    create.tap_woman = lambda *_args: events.append("about")
    create.pick_interests = lambda *_args: events.append("interests")
    create.wait_feed = lambda *_args, **_kwargs: proof
    create.mark_reddit_created = lambda *_args: events.append("created")
    create._close_and_finalize_cleanup = lambda *_args, **_kwargs: events.append("cleanup")
    create.log = logs.append
    create.time.time = lambda: 10_000.0
    resume_stop = object()
    resume_log = logs.append
    error = ""
    success_events: list[object] = []
    success_logs: list[str] = []
    cancel_events: list[object] = []
    cancel_error = ""
    try:
        create.run_slot(
            "a0001",
            object(),
            object(),
            log_fn=resume_log,
            stop=resume_stop,
        )
        success_events = list(events)
        success_logs = list(logs)
        events.clear()
        logs.clear()

        def cancelled_poll(**kwargs):
            events.append(("cancel-poll", kwargs))
            raise create.PollCancelled("synthetic stop")

        create.poll_reddit_code = cancelled_poll
        try:
            create.run_slot(
                "a0001",
                object(),
                object(),
                log_fn=logs.append,
                stop=resume_stop,
            )
        except PhoneError as exc:
            cancel_error = str(exc)
        cancel_events = list(events)
    except Exception as exc:  # pragma: no cover - diagnostic output below
        error = str(exc)
    finally:
        for name, value in originals.items():
            setattr(create, name, value)
        create.time.time = original_time

    polls = [
        event
        for event in success_events
        if isinstance(event, tuple) and event[0] == "poll"
    ]
    check("verify-resume-completes-offline", not error, error)
    check("verify-resume-skips-email-and-crane", "run-to-verify" not in success_events)
    check(
        "verify-resume-uses-exact-30-minute-lookback",
        len(polls) == 1
        and polls[0][1].get("after_ts") == 8_200.0
        and polls[0][1].get("log") == resume_log
        and polls[0][1].get("stop") is resume_stop,
    )
    check("verify-resume-enters-received-code", "type-code" in success_events)
    check(
        "verify-resume-logs-no-code-digits",
        all("654321" not in line for line in success_logs)
        and all("alias@icloud.com" not in line for line in success_logs)
        and "IMAP verification code received." in success_logs,
    )
    check(
        "verify-stop-does-not-enter-code",
        "synthetic stop" in cancel_error
        and "type-code" not in cancel_events
        and any(
            isinstance(event, tuple) and event[0] == "cancel-poll"
            for event in cancel_events
        ),
    )


def main() -> int:
    imap_regressions()
    imap_timeout_regressions()
    invalid_code_node_regressions()
    invalid_code_recovery_regressions()
    verify_resume_regressions()
    print(f"REGRESS {len(PASS)} pass {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
