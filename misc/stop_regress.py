# UTF-8
"""Offline regressions for Creator Stop cancellation.

No phone, USB, SSH, network, mailbox, or account creation is used.
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import creator  # noqa: E402
import reddit_create_day1 as create  # noqa: E402
import reddit_to_verify as to_verify  # noqa: E402
import ui_tree  # noqa: E402
from config import DUMP_DIR  # noqa: E402
from ssh_device import PhoneError  # noqa: E402


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(("PASS " if condition else "FAIL ") + name + (" :: " + detail if detail else ""))


def expect_cancel(name: str, call) -> None:
    try:
        call()
    except ui_tree.OperationCancelled:
        check(name, True)
    except Exception as exc:
        check(name, False, f"wrong exception {type(exc).__name__}: {exc}")
    else:
        check(name, False, "cancellation was not raised")


class NoPhoneCalls:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name):
        def forbidden(*_args, **_kwargs):
            self.calls.append(name)
            raise AssertionError(f"phone call after Stop: {name}")

        return forbidden


def already_stopped_regressions() -> None:
    stop = threading.Event()
    stop.set()
    phone = NoPhoneCalls()
    stack = ui_tree.TapStack(phone, lambda _message: None, cancel_event=stop)
    stack.backend = "xxtouch"
    calls = (
        ("stop-blocks-dump", stack.dump_xml),
        ("stop-blocks-screenshot", stack.screenshot_png),
        ("stop-blocks-tap", lambda: stack.tap_xy(100, 200)),
        ("stop-blocks-type", lambda: stack.type_text("abc")),
        ("stop-blocks-swipe", lambda: stack.swipe(1, 2, 3, 4)),
        ("stop-blocks-home", stack.press_home),
        ("stop-blocks-app-open", lambda: stack.open_bundle("com.example.app")),
    )
    for name, call in calls:
        expect_cancel(name, call)
    check("stop-blocks-all-phone-calls", phone.calls == [], repr(phone.calls))


def interruptible_wait_regression() -> None:
    stop = threading.Event()
    stack = ui_tree.TapStack(NoPhoneCalls(), lambda _message: None, cancel_event=stop)
    timer = threading.Timer(0.04, stop.set)
    started = time.monotonic()
    timer.start()
    expect_cancel("stop-interrupts-wait", lambda: stack.wait(5.0))
    elapsed = time.monotonic() - started
    timer.cancel()
    check("stop-wait-wakes-promptly", elapsed < 0.5, f"elapsed={elapsed:.3f}")


class PollPhone:
    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.reads = 0
        self.byte_reads = 0

    def sftp_write(self, *_args, **_kwargs):
        return None

    def sftp_read(self, *_args, **_kwargs):
        self.reads += 1
        self.stop.set()
        raise OSError("synthetic missing result")

    def sftp_read_bytes(self, *_args, **_kwargs):
        self.byte_reads += 1
        self.stop.set()
        raise OSError("synthetic missing screenshot")

    def exec_ok(self, *_args, **_kwargs):
        return 0, ""


class PollStack(ui_tree.TapStack):
    def _xxt_post(self, *_args, **_kwargs) -> str:
        self.check_cancelled()
        return '{"code":0}'


class ScreenshotStack(ui_tree.TapStack):
    def _run_xxt_lua(self, *_args, **_kwargs) -> str:
        self.check_cancelled()
        return "openapi-launched"


def polling_regressions() -> None:
    stop = threading.Event()
    phone = PollPhone(stop)
    stack = PollStack(phone, lambda _message: None, cancel_event=stop)
    stack.backend = "xxtouch"
    expect_cancel(
        "stop-interrupts-xxt-result-poll",
        lambda: stack._run_xxt_lua("test.lua", "", timeout=20, wait_path="/tmp/out"),
    )
    check("stop-prevents-second-xxt-read", phone.reads == 1, repr(phone.reads))

    stop2 = threading.Event()
    phone2 = PollPhone(stop2)
    stack2 = ScreenshotStack(phone2, lambda _message: None, cancel_event=stop2)
    stack2.backend = "xxtouch"
    expect_cancel("stop-interrupts-screenshot-poll", stack2.screenshot_png)
    check(
        "stop-prevents-second-screenshot-read",
        phone2.byte_reads == 1,
        repr(phone2.byte_reads),
    )


def saved_home_nodes():
    path = os.path.join(DUMP_DIR, "877w_6_2.json")
    with open(path, encoding="utf-8") as handle:
        return ui_tree.parse_nodes(handle.read())


def saved_verify_nodes():
    path = os.path.join(DUMP_DIR, "879_verify.json")
    with open(path, encoding="utf-8") as handle:
        return ui_tree.parse_nodes(handle.read())


def signup_wait_regressions() -> None:
    stop = threading.Event()
    stop.set()
    dump_calls: list[str] = []
    original_dump = to_verify.dump_named
    original_open = to_verify.open_container
    try:
        to_verify.dump_named = lambda *_args, **_kwargs: dump_calls.append("dump")
        expect_cancel(
            "stopped-wait-ui-does-not-dump",
            lambda: to_verify.wait_ui(object(), object(), "stop", [], stop=stop),
        )
        check("stopped-wait-ui-has-no-followup", dump_calls == [], repr(dump_calls))

        nodes = saved_home_nodes()
        home_error = ""
        try:
            to_verify.wait_ui(object(), object(), "home", nodes)
        except PhoneError as exc:
            home_error = str(exc)
        check(
            "saved-springboard-stops-immediately",
            "iphone home is visible" in home_error.casefold(),
            home_error,
        )
        check("springboard-wait-does-not-redump", dump_calls == [], repr(dump_calls))

        verify_nodes = saved_verify_nodes()
        run_dumps: list[str] = []
        open_calls: list[tuple] = []

        def fake_dump(_stack, _phone, stem, *_args, **_kwargs):
            run_dumps.append(stem)
            return "fixture", nodes if stem == "870_now" else verify_nodes

        def fake_open(*args, **_kwargs):
            open_calls.append(args)

        to_verify.dump_named = fake_dump
        to_verify.open_container = fake_open
        initial_error = None
        try:
            to_verify.run_to_verify(object(), object(), "a0001", "alias@icloud.com")
        except Exception as exc:
            initial_error = f"{type(exc).__name__}: {exc}"
        stop_before_open = threading.Event()
        stop_before_open.set()
        stopped_before_open = False
        try:
            to_verify.run_to_verify(
                object(),
                object(),
                "a0001",
                "alias@icloud.com",
                stop=stop_before_open,
            )
        except ui_tree.OperationCancelled:
            stopped_before_open = True
        check(
            "initial-springboard-opens-exact-container-and-continues",
            initial_error is None
            and len(open_calls) == 1
            and open_calls[0][2:] == (to_verify.log, "a0001", "alias@icloud.com")
            and run_dumps == ["870_now", "871_after_open", "879_verify"]
            and stopped_before_open,
            repr(
                {
                    "error": initial_error,
                    "open": open_calls,
                    "dumps": run_dumps,
                    "stopped": stopped_before_open,
                }
            ),
        )
    finally:
        to_verify.dump_named = original_dump
        to_verify.open_container = original_open


def forwarding_regression() -> None:
    original_get_slot = create.get_slot
    original_dump = create.dump_named
    original_run = create.run_to_verify
    calls: list[dict] = []
    stop = threading.Event()
    logger = lambda _message: None
    try:
        create.get_slot = lambda _sid: {
            "id": "a0001",
            "ghost": True,
            "email": "alias@icloud.com",
            "reddit": False,
        }
        create.dump_named = lambda *_args, **_kwargs: ("fixture", [])

        def fake_run(*_args, **kwargs):
            calls.append(kwargs)
            raise PhoneError("synthetic end after forwarding proof")

        create.run_to_verify = fake_run
        try:
            create.run_slot(
                "a0001",
                object(),
                object(),
                stop=stop,
                log_fn=logger,
            )
        except PhoneError:
            pass
    finally:
        create.get_slot = original_get_slot
        create.dump_named = original_dump
        create.run_to_verify = original_run
    check(
        "run-slot-forwards-stop-and-logger",
        len(calls) == 1
        and calls[0].get("stop") is stop
        and calls[0].get("log_fn") is logger,
        repr(calls),
    )


def stopped_run_has_no_persistence_regression() -> None:
    original_get_slot = create.get_slot
    original_mark = create.mark_reddit_created
    original_close = create._close_and_finalize_cleanup
    stop = threading.Event()
    stop.set()
    persistence: list[str] = []
    stack = ui_tree.TapStack(NoPhoneCalls(), lambda _message: None, cancel_event=stop)
    stack.backend = "xxtouch"
    try:
        create.get_slot = lambda _sid: {
            "id": "a0001",
            "ghost": True,
            "email": "alias@icloud.com",
            "reddit": False,
        }
        create.mark_reddit_created = lambda *_args, **_kwargs: persistence.append("mark")
        create._close_and_finalize_cleanup = (
            lambda *_args, **_kwargs: persistence.append("cleanup")
        )
        expect_cancel(
            "stopped-run-slot-cancels-before-first-dump",
            lambda: create.run_slot("a0001", object(), stack, stop=stop),
        )
    finally:
        create.get_slot = original_get_slot
        create.mark_reddit_created = original_mark
        create._close_and_finalize_cleanup = original_close
    check("stopped-run-slot-writes-no-state", persistence == [], repr(persistence))


class FakeValue:
    def __init__(self) -> None:
        self.values: list[str] = []

    def set(self, value: str) -> None:
        self.values.append(value)


class FakeButton:
    def __init__(self) -> None:
        self.states: list[str] = []

    def configure(self, *, state: str) -> None:
        self.states.append(state)


class FakeApp:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.status = FakeValue()
        self.stop_btn = FakeButton()
        self.post_stop_btn = FakeButton()
        self.qa_process = None
        self.logs: list[str] = []

    def log(self, message: str) -> None:
        self.logs.append(message)


def stop_button_regression() -> None:
    app = FakeApp()
    creator.App.on_stop(app)
    creator.App.on_stop(app)
    check("stop-button-sets-event", app.stop.is_set())
    check("stop-button-shows-stopping", app.status.values == ["Stopping…"], repr(app.status.values))
    check("stop-button-disables-once", app.stop_btn.states == ["disabled"])
    check("post-stop-button-disables-once", app.post_stop_btn.states == ["disabled"])
    check(
        "stop-button-logs-once",
        app.logs == ["Stop requested; waiting for the current bounded device call."],
        repr(app.logs),
    )


def creator_status_regression() -> None:
    class Root:
        def after(self, *_args) -> None:
            pass

    class Phone:
        def close(self) -> None:
            pass

    original_get_slot = creator.get_slot
    original_state = creator.ghost_container_state
    original_run = creator.run_create_reddit
    results: dict[str, tuple[list[str], list[str]]] = {}
    try:
        creator.get_slot = lambda _sid: {
            "id": "a0001",
            "ghost": True,
            "email": "alias@icloud.com",
            "reddit": False,
        }
        creator.ghost_container_state = lambda _slot: "ready"
        for mode in ("cancel", "error"):
            app = FakeApp()
            app.root = Root()
            app.phone = None
            app._after_job = lambda: None
            app._connect = lambda _password: (Phone(), object())

            def fake_run(*_args, mode=mode, **_kwargs):
                if mode == "cancel":
                    app.stop.set()
                    raise ui_tree.OperationCancelled("Stopped by user.")
                raise PhoneError("synthetic genuine failure")

            creator.run_create_reddit = fake_run
            creator.App._run_create_reddit(app, "password", "a0001")
            results[mode] = (app.status.values, app.logs)
    finally:
        creator.get_slot = original_get_slot
        creator.ghost_container_state = original_state
        creator.run_create_reddit = original_run
    check(
        "creator-cancellation-reports-stopped",
        results["cancel"][0][-1:] == ["Stopped"]
        and not any(line.startswith("ERROR") for line in results["cancel"][1]),
        repr(results["cancel"]),
    )
    check(
        "creator-genuine-failure-remains-error",
        results["error"][0][-1:] == ["Failed — see Log"]
        and any(line.startswith("ERROR") for line in results["error"][1]),
        repr(results["error"]),
    )


def main() -> int:
    already_stopped_regressions()
    interruptible_wait_regression()
    polling_regressions()
    signup_wait_regressions()
    forwarding_regression()
    stopped_run_has_no_persistence_regression()
    stop_button_regression()
    creator_status_regression()
    print(f"REGRESS {len(PASS)} pass {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
