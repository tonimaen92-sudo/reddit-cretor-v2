# UTF-8
"""Offline regressions for Creator's private birthday prompt gate.

The suite constructs ``App`` without calling its Tk-building constructor and
replaces the prompt, secrets store, slot lookup, and worker launcher with
fakes.  It never opens a window, reads/writes secrets.json, or contacts a
phone.

Run: python misc/creator_prompt_regress.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import inspect
from contextlib import contextmanager
from datetime import date
from unittest import mock
from friend_poster.apps import creator  # noqa: E402
from friend_poster.reddit import close_reddit as close_cli  # noqa: E402
from friend_poster.reddit import reddit_about  # noqa: E402
from friend_poster.reddit import reddit_reply_direct  # noqa: E402
from friend_poster.core import slots  # noqa: E402
from friend_poster.core.paths import PROJECT_ROOT


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


@contextmanager
def patched(**replacements):
    originals = {name: getattr(creator, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(creator, name, value)
        yield
    finally:
        for name, value in originals.items():
            setattr(creator, name, value)


class FakeValue:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class FakeButton:
    def __init__(self) -> None:
        self.options: dict[str, str] = {}

    def configure(self, **kwargs) -> None:
        self.options.update(kwargs)


class FakeNotebook:
    def __init__(self) -> None:
        self.selected: list[object] = []

    def select(self, value) -> None:
        self.selected.append(value)


class FakeCombobox:
    def __init__(self, value: str = "") -> None:
        self.value = value
        self.options: dict[str, object] = {}

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value

    def configure(self, **kwargs) -> None:
        self.options.update(kwargs)


class FakeMessageBox:
    def __init__(self) -> None:
        self.errors: list[tuple[tuple, dict]] = []
        self.warnings: list[tuple[tuple, dict]] = []
        self.infos: list[tuple[tuple, dict]] = []

    def showerror(self, *args, **kwargs) -> None:
        self.errors.append((args, kwargs))

    def showwarning(self, *args, **kwargs) -> None:
        self.warnings.append((args, kwargs))

    def showinfo(self, *args, **kwargs) -> None:
        self.infos.append((args, kwargs))


class FakeRoot:
    def __init__(self):
        self.destroyed = False

    def after(self, _delay, callback):
        callback()

    def destroy(self):
        self.destroyed = True


def bare_app() -> tuple[creator.App, list[str], list[tuple]]:
    """Return an App-shaped object without creating a Tk interpreter."""
    app = creator.App.__new__(creator.App)
    logs: list[str] = []
    workers: list[tuple] = []
    app.root = object()
    app.worker = None
    app.pw = FakeValue("ssh-password-canary")
    app.log = logs.append
    app._selected_id = lambda: "a0018"
    app._run_create_reddit = lambda *_args: None
    app._start_worker = lambda *args: workers.append(args)
    app._prompt_creation_options = lambda _slot: (date(2003, 7, 21), 30)
    return app, logs, workers


def per_account_creation_regressions() -> None:
    check(
        "slot-birthday-valid",
        creator.parse_slot_birth_date(
            "21.07.2003", today=date(2026, 10, 9)
        ) == date(2003, 7, 21),
    )
    for raw, expected in (("1", 1), ("30", 30), ("250", 250), ("1000", 1000)):
        check(
            f"creation-count-{raw}",
            creator.parse_creation_swipe_count(raw) == expected,
        )
    for raw in ("", "0", "-1", "1.5", "words"):
        rejected = False
        try:
            creator.parse_creation_swipe_count(raw)
        except ValueError:
            rejected = True
        check(f"creation-count-rejects-{raw or 'empty'}", rejected)

    for raw, expected in (("0", 0), ("3", 3), ("10", 10)):
        check(
            f"creation-pause-count-{raw}",
            creator.parse_creation_pause_count(raw, 10) == expected,
        )
    for raw in ("", "-1", "1.5", "words", "11"):
        rejected = False
        try:
            creator.parse_creation_pause_count(raw, 10)
        except ValueError:
            rejected = True
        check(f"creation-pause-count-rejects-{raw or 'empty'}", rejected)

    slot = {
        "id": "a0018",
        "ghost": True,
        "email": "present",
        "reddit": False,
        "reddit_birth_date": "21.07.2003",
    }
    app, logs, workers = bare_app()
    app._prompt_creation_options = lambda _slot: None
    with patched(get_slot=lambda _sid: dict(slot)):
        creator.App.on_create_reddit(app)
    check("creation-options-cancel-starts-nothing", workers == [])

    app, logs, workers = bare_app()
    captured_date = date(2003, 7, 21)
    app._prompt_creation_options = lambda _slot: (captured_date, 1000)
    with patched(
        get_slot=lambda _sid: dict(slot),
        load_qa_swipes=lambda: (_ for _ in ()).throw(
            AssertionError("creation read the global QA count")
        ),
    ):
        creator.App.on_create_reddit(app)
    check(
        "creation-options-forward-account-values",
        len(workers) == 1
        and workers[0][0] is app._run_create_reddit
        and workers[0][2:] == ("a0018", captured_date, 1000),
    )
    check(
        "creation-options-log-redacts-birthday",
        "21.07.2003" not in "\n".join(logs)
        and any("1000 feed swipes" in line for line in logs),
    )

    prompt_source = inspect.getsource(creator.App._prompt_creation_options)
    init_source = inspect.getsource(creator.App.__init__)
    check(
        "saved-birthday-skips-editable-field",
        "if parsed_saved is None:" in prompt_source
        and "Birthday: {saved_birth_date} (saved)" in prompt_source,
    )
    check(
        "birthday-column-follows-username",
        'cols = ("user", "birthday", "slot"' in init_source,
    )
    check(
        "old-global-birthday-control-removed",
        "Set birth date for next creation" not in init_source,
    )


def one_shot_regressions() -> None:
    # The temporary dialog accepts only strict DD.MM.YYYY and returns a date.
    check(
        "one-shot-dialog-valid",
        creator.parse_one_shot_birth_date(
            "21.07.2003", today=date(2026, 10, 9)
        ) == date(2003, 7, 21),
    )
    check(
        "one-shot-dialog-valid-year-min",
        creator.parse_one_shot_birth_date(
            "01.01.2002", today=date(2026, 10, 9)
        ) == date(2002, 1, 1),
    )
    check(
        "one-shot-dialog-trims-outer-whitespace",
        creator.parse_one_shot_birth_date(
            "  21.07.2003  ", today=date(2026, 10, 9)
        ) == date(2003, 7, 21),
    )
    check(
        "one-shot-dialog-leap-day",
        creator.parse_one_shot_birth_date(
            "29.02.2004", today=date(2026, 10, 9)
        ) == date(2004, 2, 29),
    )
    check(
        "one-shot-birthday-today-is-adult",
        creator.parse_one_shot_birth_date(
            "05.06.2006", today=date(2024, 6, 5)
        ) == date(2006, 6, 5),
    )
    for label, raw in (
        ("empty", ""),
        ("iso-format", "2003-07-21"),
        ("slash-format", "21/07/2003"),
        ("unpadded-format", "1.07.2003"),
        ("impossible", "31.02.2003"),
        ("non-leap-year", "29.02.2003"),
        ("year-range", "01.01.2007"),
        ("birthday-tomorrow-underage", "06.06.2006"),
    ):
        try:
            creator.parse_one_shot_birth_date(
                raw, today=date(2024, 6, 5)
            )
            rejected = False
        except creator.PhoneError:
            rejected = True
        check("one-shot-invalid-" + label, rejected)

    invalid_messages: list[tuple[str, str]] = []
    for raw in ("2003-07-21", "21/07/2003", "31.02.2003", "01.01.2007"):
        try:
            creator.parse_one_shot_birth_date(raw, today=date(2024, 6, 5))
        except creator.PhoneError as exc:
            invalid_messages.append((raw, str(exc)))
    check(
        "one-shot-validation-errors-redact-input",
        len(invalid_messages) == 4
        and all(raw not in message for raw, message in invalid_messages),
    )

    value = date(2004, 2, 29)
    state = creator.OneShotBirthDate()
    state.set(value)
    check("one-shot-set", state.is_set())
    check("one-shot-consumed-once", state.consume() == value)
    check("one-shot-second-consume-empty", state.consume() is None)

    # Atomic consumption permits exactly one caller to receive the value.
    state.set(value)
    barrier = creator.threading.Barrier(8)
    results: list[date | None] = []
    result_lock = creator.threading.Lock()

    def consume_concurrently() -> None:
        barrier.wait()
        result = state.consume()
        with result_lock:
            results.append(result)

    threads = [
        creator.threading.Thread(target=consume_concurrently) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check(
        "one-shot-atomic-consume",
        sum(result is not None for result in results) == 1,
    )

    # Invalid replacement and Cancel preserve the prior one-shot value.
    app, _logs, _workers = bare_app()
    creator.App._set_next_birth_date(app, "21.07.2003")
    try:
        creator.App._set_next_birth_date(app, "31.02.2003")
    except creator.PhoneError:
        pass
    check(
        "one-shot-invalid-does-not-replace-existing",
        app.next_birth_date.consume() == date(2003, 7, 21),
    )

    state = creator.OneShotBirthDate()
    check("one-shot-cancel-without-save", not state.is_set())
    state.set(value)
    dialog_source = inspect.getsource(creator.App.on_set_next_birth_date)
    cancel_source = dialog_source.split("def cancel()", 1)[1].split("def save()", 1)[0]
    check(
        "one-shot-cancel-preserves-existing",
        state.is_set()
        and "next_birth_date" not in cancel_source
        and "dialog.destroy()" in cancel_source,
    )

    exit_app, _logs, _workers = bare_app()
    exit_app.next_birth_date.set(value)
    exit_app._save_post_community = lambda: None
    exit_app.stop = creator.threading.Event()
    exit_app.qa_process = None
    exit_app.phone = None
    destroyed: list[bool] = []
    exit_app.root = type(
        "FakeRoot", (), {"destroy": lambda _self: destroyed.append(True)}
    )()
    with patched(allow_sleep=lambda: None):
        creator.App.on_close(exit_app)
    check(
        "one-shot-clear-on-exit",
        not exit_app.next_birth_date.is_set() and destroyed == [True],
    )

    app, _logs, _workers = bare_app()
    creator.App._set_next_birth_date(app, "29.02.2004")
    check(
        "one-shot-panel-set-status-redacted",
        app.next_birth_date.is_set()
        and app.birth_date_status.value == "Birth date for next creation: Set"
        and "2004" not in app.birth_date_status.value,
    )
    app._clear_next_birth_date()
    check("one-shot-panel-clear", not app.next_birth_date.is_set())

    restarted_state = creator.OneShotBirthDate()
    check("one-shot-restart-begins-empty", not restarted_state.is_set())

    # A supplied value bypasses the persistent-secrets gate, is removed before
    # the worker starts, and is passed only as an in-process date object.
    slot = {"id": "a0018", "ghost": True, "email": "present", "reddit": False}
    app, logs, workers = bare_app()
    app.next_birth_date.set(value)
    app._ensure_reddit_birth_date = lambda: (_ for _ in ()).throw(
        AssertionError("persistent birthday gate was called")
    )
    with patched(
        get_slot=lambda _sid: dict(slot),
        update_secrets=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("one-shot birthday was persisted")
        ),
    ):
        creator.App.on_create_reddit(app)
    check(
        "one-shot-worker-receives-date-object",
        len(workers) == 1 and workers[0][3] == value,
    )
    check("one-shot-cleared-before-worker", not app.next_birth_date.is_set())
    check(
        "one-shot-status-cleared",
        app.birth_date_status.value == "Birth date for next creation: Not set",
    )
    check("one-shot-date-not-logged", value.isoformat() not in "\n".join(logs))

    # Invalid selections return before consumption, leaving the value available.
    app, _logs, workers = bare_app()
    app.next_birth_date.set(value)
    with patched(get_slot=lambda _sid: None):
        creator.App.on_create_reddit(app)
    check(
        "one-shot-invalid-slot-does-not-consume",
        workers == [] and app.next_birth_date.consume() == value,
    )

    # Once dispatched, Stop cannot restore or reuse the already consumed value.
    app, logs, workers = bare_app()
    app.stop = creator.threading.Event()
    app.qa_process = None
    app.next_birth_date.set(value)
    with patched(get_slot=lambda _sid: dict(slot)):
        creator.App.on_create_reddit(app)
    creator.App.on_stop(app)
    check(
        "one-shot-cleared-after-stop",
        len(workers) == 1
        and workers[0][3] == value
        and app.stop.is_set()
        and not app.next_birth_date.is_set(),
    )

    # Exercise the real worker wrapper offline for success and each failure class.
    class WorkerRoot:
        def __init__(self) -> None:
            self.after_calls: list[tuple] = []

        def after(self, *args) -> None:
            self.after_calls.append(args)

    class WorkerPhone:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    for outcome in ("success", "failure", "cancellation", "timeout", "exception"):
        app, _logs, _workers = bare_app()
        app.root = WorkerRoot()
        app.status = FakeValue("")
        app.stop = creator.threading.Event()
        app.phone = None
        worker_phone = WorkerPhone()
        app._connect = lambda _password, phone=worker_phone: (phone, object())
        app.next_birth_date.set(value)
        consumed = app._consume_next_birth_date()
        worker_calls: list[date | None] = []
        worker_channels: list[tuple[object, object]] = []

        def run_worker(
            _sid,
            _phone,
            _stack,
            *,
            birth_date_override=None,
            log_fn=None,
            stop=None,
            outcome=outcome,
        ):
            worker_calls.append(birth_date_override)
            worker_channels.append((log_fn, stop))
            if outcome == "failure":
                raise creator.PhoneError("synthetic worker failure")
            if outcome == "timeout":
                raise TimeoutError("synthetic timeout")
            if outcome == "exception":
                raise RuntimeError("synthetic exception")

        if outcome == "cancellation":
            app.stop.set()
        with patched(
            get_slot=lambda _sid: dict(slot),
            ghost_container_state=lambda _slot: "ready",
            run_create_reddit=run_worker,
        ):
            creator.App._run_create_reddit(app, "password", "a0018", consumed)
        check(
            "one-shot-cleared-after-" + outcome,
            not app.next_birth_date.is_set()
            and worker_phone.closed
            and bool(app.root.after_calls)
            and (
                worker_calls == []
                if outcome == "cancellation"
                else worker_calls == [value]
            ),
        )
        check(
            "creator-forwards-log-and-stop-after-" + outcome,
            worker_channels == []
            if outcome == "cancellation"
            else len(worker_channels) == 1
            and worker_channels[0][0] == app.log
            and worker_channels[0][1] is app.stop,
        )

    # Without a temporary value, the existing configured-date path is intact.
    app, _logs, workers = bare_app()
    ensure_calls: list[bool] = []
    app._ensure_reddit_birth_date = lambda: ensure_calls.append(True) or True
    with patched(get_slot=lambda _sid: dict(slot)):
        creator.App.on_create_reddit(app)
    check(
        "one-shot-absent-preserves-existing-path",
        ensure_calls == [True]
        and len(workers) == 1
        and workers[0][3] is None,
    )

    # Setting the temporary value never invokes a persistent or process channel.
    app, logs, _workers = bare_app()
    persistence_calls: list[tuple] = []
    environment_before = dict(os.environ)

    def record_persistence(*args, **kwargs):
        persistence_calls.append((args, kwargs))

    with patched(
        update_secrets=record_persistence,
        save_slots=record_persistence,
        save_qa_swipes=record_persistence,
    ):
        creator.App._set_next_birth_date(app, "21.07.2003")
    check(
        "one-shot-no-persistent-write",
        persistence_calls == [] and dict(os.environ) == environment_before,
    )
    check(
        "one-shot-raw-input-not-logged",
        "21.07.2003" not in "\n".join(logs),
    )

    # UI wording is exact and the setting stays separate from QA Options.
    init_source = inspect.getsource(creator.App.__init__)
    qa_source = inspect.getsource(creator.App.on_qa_options)
    worker_source = inspect.getsource(creator.App._run_create_reddit)
    check(
        "one-shot-ui-contract",
        'text="Set birth date for next creation…"' in init_source
        and 'value="Birth date for next creation: Not set"' in init_source
        and 'text="Birth date (DD.MM.YYYY):"' in dialog_source
        and 'text="Used only for the next creation attempt, then cleared."'
        in dialog_source
        and 'text="OK"' in dialog_source
        and 'text="Cancel"' in dialog_source,
    )
    check(
        "one-shot-separated-from-qa-options",
        "birth" not in qa_source.casefold() and "qa" not in dialog_source.casefold(),
    )
    check(
        "one-shot-worker-releases-local-reference",
        "finally:" in worker_source and "birth_date_override = None" in worker_source,
    )


    # Source-level guard: the in-memory value is never serialized, logged,
    # exported, named in a file, or placed on a command line.
    guarded_block = "\n".join(
        inspect.getsource(item)
        for item in (
            creator.OneShotBirthDate,
            creator.parse_one_shot_birth_date,
            creator.App._set_next_birth_date,
            creator.App._consume_next_birth_date,
            creator.App.on_create_reddit,
        )
    )
    check(
        "one-shot-no-persistence-or-command-channel",
        "update_secrets(reddit_birth_date" not in guarded_block
        and "subprocess" not in guarded_block
        and "os.environ" not in guarded_block,
    )


def recover_then_create_regression() -> None:
    """Create Reddit recovers Crane state and never provisions through Ghost."""

    class Root:
        def after(self, *_args) -> None:
            pass

    class Phone:
        def close(self) -> None:
            pass

    app = creator.App.__new__(creator.App)
    app.root = Root()
    app.status = FakeValue("")
    app.stop = creator.threading.Event()
    app.phone = None
    app.log = lambda _message: None
    app._after_job = lambda: None
    app._connect = lambda _password: (Phone(), object())
    events: list[str] = []
    slot = {
        "id": "a0018",
        "ghost": False,
        "email": "alias@icloud.com",
        "reddit": False,
    }

    def recover(*_args, **_kwargs):
        events.append("recover-existing")

    def commit(*_args, **_kwargs):
        events.append("commit-ready")

    def create_reddit(*_args, **_kwargs):
        events.append("create-reddit")

    with patched(
        get_slot=lambda _sid: dict(slot),
        ghost_container_state=lambda _slot: "needed",
        recover_existing_container=recover,
        mark_ghost_container_ready_from_crane=commit,
        run_create_reddit=create_reddit,
    ):
        creator.App._run_create_reddit(app, "password", "a0018")
    check(
        "existing-crane-recovery-commits-before-reddit-creation",
        events == ["recover-existing", "commit-ready", "create-reddit"],
        repr(events),
    )

    events.clear()

    def fail_recovery(*_args, **_kwargs):
        events.append("recover-failed")
        raise creator.PhoneError("exact existing row was not proven")

    with patched(
        get_slot=lambda _sid: dict(slot),
        ghost_container_state=lambda _slot: "provisioning",
        recover_existing_container=fail_recovery,
        mark_ghost_container_ready_from_crane=commit,
        run_create_reddit=create_reddit,
    ):
        creator.App._run_create_reddit(app, "password", "a0018")
    check(
        "failed-crane-recovery-does-not-commit-or-start-signup",
        events == ["recover-failed"],
        repr(events),
    )

    worker_source = inspect.getsource(creator.App._run_create_reddit)
    check(
        "create-reddit-never-provisions-a-ghost-container",
        "provision_slot_container" not in worker_source
        and "recover_existing_container" in worker_source,
    )


def ensure_regressions() -> None:
    secret_date = "2004-02-29"

    # An existing valid value neither prompts nor writes.
    app, logs, _workers = bare_app()
    prompts: list[str] = []
    updates: list[dict] = []
    app._prompt_reddit_birth_date = lambda value="": prompts.append(value)
    with patched(
        load_secrets=lambda: {
            "ssh_password": "unrelated-secret",
            "reddit_birth_date": secret_date,
        },
        update_secrets=lambda **kwargs: updates.append(kwargs),
    ):
        ok = creator.App._ensure_reddit_birth_date(app)
    check("existing-valid-accepted", ok)
    check("existing-valid-no-prompt", prompts == [])
    check("existing-valid-no-write", updates == [])
    check("existing-valid-not-logged", secret_date not in "\n".join(logs))

    # A missing value is requested once and saved once.  The fake store models
    # update_secrets' merge semantics so preservation is checked without disk I/O.
    app, logs, _workers = bare_app()
    store = {"ssh_password": "keep-me", "icloud_user": "keep-me-too"}
    prompts = []
    updates = []

    def prompt_missing(initial=""):
        prompts.append(initial)
        return secret_date

    def update_store(**kwargs):
        updates.append(dict(kwargs))
        store.update(kwargs)
        return dict(store)

    app._prompt_reddit_birth_date = prompt_missing
    with patched(load_secrets=lambda: dict(store), update_secrets=update_store):
        ok = creator.App._ensure_reddit_birth_date(app)
    check("missing-prompts", ok and prompts == [""])
    check("missing-saves-once", updates == [{"reddit_birth_date": secret_date}])
    check(
        "save-preserves-other-secrets",
        store.get("ssh_password") == "keep-me"
        and store.get("icloud_user") == "keep-me-too",
    )
    check("missing-date-not-logged", secret_date not in "\n".join(logs))

    # Defensive validation rejects a bad alternate prompt provider and loops;
    # only the later valid value is persisted.
    app, logs, _workers = bare_app()
    answers = iter(("2004-02-30", secret_date))
    prompt_initials: list[str] = []
    updates = []

    def prompt_sequence(initial=""):
        prompt_initials.append(initial)
        return next(answers)

    app._prompt_reddit_birth_date = prompt_sequence
    with patched(
        load_secrets=lambda: {"reddit_birth_date": "not-a-date"},
        update_secrets=lambda **kwargs: updates.append(dict(kwargs)),
    ):
        ok = creator.App._ensure_reddit_birth_date(app)
    check(
        "invalid-reprompts",
        ok and prompt_initials == ["not-a-date", "2004-02-30"],
    )
    check("invalid-never-saved", updates == [{"reddit_birth_date": secret_date}])
    check(
        "invalid-values-not-logged",
        all(value not in "\n".join(logs) for value in (*prompt_initials, secret_date)),
    )

    # Cancel and storage failures fail closed without a write.
    app, _logs, _workers = bare_app()
    updates = []
    app._prompt_reddit_birth_date = lambda _initial="": None
    with patched(
        load_secrets=lambda: {},
        update_secrets=lambda **kwargs: updates.append(dict(kwargs)),
    ):
        ok = creator.App._ensure_reddit_birth_date(app)
    check("cancel-fails-closed", not ok and updates == [])

    app, _logs, _workers = bare_app()
    box = FakeMessageBox()
    prompted: list[str] = []
    app._prompt_reddit_birth_date = lambda initial="": prompted.append(initial)
    with patched(
        load_secrets=lambda: (_ for _ in ()).throw(ValueError("synthetic")),
        messagebox=box,
    ):
        ok = creator.App._ensure_reddit_birth_date(app)
    check("read-error-fails-closed", not ok and prompted == [] and len(box.errors) == 1)

    app, _logs, _workers = bare_app()
    box = FakeMessageBox()
    app._prompt_reddit_birth_date = lambda _initial="": secret_date
    with patched(
        load_secrets=lambda: {},
        update_secrets=lambda **_kwargs: (_ for _ in ()).throw(OSError("synthetic")),
        messagebox=box,
    ):
        ok = creator.App._ensure_reddit_birth_date(app)
    check("write-error-fails-closed", not ok and len(box.errors) == 1)


def worker_gate_regressions() -> None:
    slot = {"id": "a0018", "ghost": True, "email": "present", "reddit": False}

    app, logs, workers = bare_app()
    app._ensure_reddit_birth_date = lambda: False
    with patched(get_slot=lambda _sid: dict(slot)):
        creator.App.on_create_reddit(app)
    check("cancel-starts-no-worker", workers == [])
    check("cancel-touches-no-phone", not hasattr(app, "phone"))

    app, logs, workers = bare_app()
    app._ensure_reddit_birth_date = lambda: True
    with patched(get_slot=lambda _sid: dict(slot), load_qa_swipes=lambda: 30):
        creator.App.on_create_reddit(app)
    check(
        "valid-starts-worker-once-with-captured-feed-count",
        len(workers) == 1
        and workers[0][0] is app._run_create_reddit
        and workers[0][2] == "a0018"
        and workers[0][4] == 30,
    )

    # Each invalid class returns to the fake prompt, then Cancel stops the
    # Creator before _start_worker.  A fixed 'today' covers underage behavior.
    invalid_cases = (
        "02-29-2004",  # format
        "2004-02-30",  # impossible
        "2007-01-01",  # allowed-year range
        "2006-12-31",  # underage relative to the fixed date below
    )
    original_parse = creator.parse_reddit_birth_date

    def fixed_parse(raw):
        return reddit_about.parse_reddit_birth_date(raw, today=date(2023, 1, 1))

    for index, bad_value in enumerate(invalid_cases):
        app, logs, workers = bare_app()
        answers = iter((bad_value, None))
        app._prompt_reddit_birth_date = lambda _initial="", answers=answers: next(answers)
        with patched(
            get_slot=lambda _sid: dict(slot),
            load_secrets=lambda: {},
            update_secrets=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("invalid value was written")
            ),
            parse_reddit_birth_date=fixed_parse,
        ):
            creator.App.on_create_reddit(app)
        check(f"invalid-{index}-starts-no-worker", workers == [])
        check(f"invalid-{index}-not-logged", bad_value not in "\n".join(logs))
    creator.parse_reddit_birth_date = original_parse


def cleanup_state_regressions() -> None:
    legacy = {"id": "a0001", "reddit": True}
    pending = {"id": "a0002", "reddit": True, "reddit_cleanup": "pending"}
    stopped = {"id": "a0003", "reddit": True, "reddit_cleanup": "app_stopped"}
    complete = {"id": "a0004", "reddit": True, "reddit_cleanup": "complete"}
    invalid = {"id": "a0005", "reddit": True, "reddit_cleanup": "unexpected"}
    uncreated = {"id": "a0006", "reddit": False}

    check("legacy-cleanup-is-complete", slots.reddit_cleanup_state(legacy) == "complete")
    check("legacy-slot-is-post-ready", slots.slot_reddit_ready(legacy))
    check("pending-needs-cleanup", slots.slot_reddit_cleanup_pending(pending))
    check("app-stopped-needs-final-proof", slots.slot_reddit_cleanup_pending(stopped))
    check("complete-is-ready", slots.slot_reddit_ready(complete))
    check(
        "invalid-state-fails-closed",
        slots.reddit_cleanup_state(invalid) == "pending"
        and slots.slot_reddit_cleanup_pending(invalid)
        and not slots.slot_reddit_ready(invalid),
    )
    check(
        "uncreated-is-not-ready-or-cleanup-resumable",
        not slots.slot_reddit_ready(uncreated)
        and not slots.slot_reddit_cleanup_pending(uncreated),
    )
    check(
        "standalone-close-detects-only-pending-checkpoints",
        close_cli.pending_cleanup_ids(
            [legacy, pending, stopped, complete, invalid, uncreated]
        )
        == ["a0002", "a0003", "a0005"],
    )
    check(
        "standalone-close-empty-when-no-checkpoint-needs-creator",
        close_cli.pending_cleanup_ids([legacy, complete, uncreated]) == [],
    )

    original_path = slots.SLOTS_PATH
    original_log_dir = slots.LOG_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            slots.SLOTS_PATH = os.path.join(td, "slots.json")
            slots.LOG_DIR = td
            slots.save_slots(
                [
                    {
                        "id": "a0018",
                        "ghost": True,
                        "email": "present",
                        "username": "present",
                        "reddit": False,
                        "day1": False,
                    }
                ]
            )
            with open(slots.SLOTS_PATH, "rb") as f:
                before_day1 = f.read()
            try:
                slots.mark_create_day1("a0018")
                uncreated_day1_rejected = False
            except ValueError:
                uncreated_day1_rejected = True
            with open(slots.SLOTS_PATH, "rb") as f:
                after_uncreated_day1 = f.read()
            check(
                "day1-cannot-create-reddit-state",
                uncreated_day1_rejected and after_uncreated_day1 == before_day1,
            )
            created = slots.mark_reddit_created("a0018")
            check(
                "mark-created-starts-pending",
                created.get("reddit") is True
                and created.get("day1") is False
                and created.get("reddit_cleanup") == "pending",
            )
            original_phone = close_cli.Phone
            phone_construction: list[bool] = []

            def forbidden_phone(*_args, **_kwargs):
                phone_construction.append(True)
                raise AssertionError("Phone must not be constructed for pending cleanup")

            close_cli.Phone = forbidden_phone
            try:
                standalone_result = close_cli.main()
            finally:
                close_cli.Phone = original_phone
            check(
                "standalone-close-refuses-before-phone-construction",
                standalone_result == 1 and phone_construction == [],
            )
            with open(slots.SLOTS_PATH, "rb") as f:
                before_pending_day1 = f.read()
            try:
                slots.mark_create_day1("a0018")
                pending_day1_rejected = False
            except ValueError:
                pending_day1_rejected = True
            with open(slots.SLOTS_PATH, "rb") as f:
                after_pending_day1 = f.read()
            check(
                "day1-requires-cleanup-complete",
                pending_day1_rejected and after_pending_day1 == before_pending_day1,
            )
            stopped_row = slots.mark_reddit_cleanup("a0018", "app_stopped")
            complete_row = slots.mark_reddit_cleanup("a0018", "complete")
            check(
                "cleanup-checkpoints-persist",
                stopped_row.get("reddit_cleanup") == "app_stopped"
                and complete_row.get("reddit_cleanup") == "complete"
                and slots.get_slot("a0018").get("reddit_cleanup") == "complete",
            )
            with open(slots.SLOTS_PATH, "rb") as f:
                before = f.read()
            try:
                slots.mark_reddit_cleanup("a0018", "bad-state")
                rejected = False
            except ValueError:
                rejected = True
            with open(slots.SLOTS_PATH, "rb") as f:
                after = f.read()
            check("invalid-checkpoint-does-not-write", rejected and before == after)
            try:
                slots.mark_reddit_cleanup("a0018", "pending")
                regressed = False
            except ValueError:
                regressed = True
            with open(slots.SLOTS_PATH, "rb") as f:
                after_regression = f.read()
            check(
                "cleanup-checkpoint-cannot-regress",
                regressed and after_regression == after,
            )
            day1_row = slots.mark_create_day1("a0018")
            check(
                "day1-allowed-after-cleanup-complete",
                day1_row.get("day1") is True
                and day1_row.get("reddit_cleanup") == "complete",
            )
            check(
                "atomic-save-leaves-no-temp-file",
                not any(name.startswith(".slots-") for name in os.listdir(td)),
            )
    finally:
        slots.SLOTS_PATH = original_path
        slots.LOG_DIR = original_log_dir


def creator_cleanup_ui_regressions() -> None:
    base = {
        "id": "a0018",
        "ghost": True,
        "email": "present",
        "username": "present",
        "reddit": True,
        "day1": False,
    }

    # Pending cleanup reuses the existing worker but bypasses the birthday and
    # every other creation gate. run_slot owns the cleanup-only resume branch.
    app, logs, workers = bare_app()
    birth_checks: list[bool] = []
    app._ensure_reddit_birth_date = lambda: birth_checks.append(True) or True
    pending = dict(base, reddit_cleanup="pending")
    with patched(get_slot=lambda _sid: dict(pending)):
        creator.App.on_create_reddit(app)
    check(
        "pending-starts-cleanup-worker",
        len(workers) == 1
        and workers[0][0] is app._run_create_reddit
        and workers[0][2] == "a0018",
    )
    check("pending-skips-birthday-gate", birth_checks == [])
    check("pending-logs-cleanup-only", any("cleanup only" in line for line in logs))

    # A completed or legacy-created slot cannot restart creation.
    for label, row in (
        ("complete", dict(base, reddit_cleanup="complete")),
        ("legacy", dict(base)),
    ):
        app, _logs, workers = bare_app()
        app._ensure_reddit_birth_date = lambda: (_ for _ in ()).throw(
            AssertionError("completed slot reached birthday gate")
        )
        with patched(get_slot=lambda _sid, row=row: dict(row)):
            creator.App.on_create_reddit(app)
        check(f"{label}-does-not-restart-creation", workers == [])

    def prepare_buttons(app: creator.App) -> None:
        app.worker = None
        app.email_btn = FakeButton()
        app.copy_btn = FakeButton()
        app.profile_btn = FakeButton()
        app.reddit_btn = FakeButton()
        app.qa_btn = FakeButton()
        app.qa_options_btn = FakeButton()
        app.ghost_btn = FakeButton()

    app, _logs, _workers = bare_app()
    prepare_buttons(app)
    with patched(get_slot=lambda _sid: dict(pending)):
        creator.App._sync_row_buttons(app)
    check(
        "pending-button-says-finish-cleanup",
        app.reddit_btn.options.get("text") == "Finish cleanup (iPhone X)"
        and app.reddit_btn.options.get("state") == "normal",
    )
    check(
        "pending-slot-disables-combined-qa",
        app.qa_btn.options.get("state") == "disabled",
    )
    check(
        "selected-pending-slot-enables-qa-options",
        app.qa_options_btn.options.get("state") == "normal",
    )

    app, _logs, _workers = bare_app()
    prepare_buttons(app)
    ready = dict(base, reddit_cleanup="complete")
    with patched(get_slot=lambda _sid: dict(ready)):
        creator.App._sync_row_buttons(app)
    check(
        "ready-button-disabled",
        app.reddit_btn.options.get("text") == "Create Reddit (iPhone X)"
        and app.reddit_btn.options.get("state") == "disabled",
    )
    check(
        "ready-slot-enables-combined-qa",
        app.qa_btn.options.get("state") == "normal",
    )

    app, logs, workers = bare_app()
    app.worker = None
    app.nb = FakeNotebook()
    app.log_tab = object()
    app._selected_id = lambda: "a0001"
    app._run_combined_qa = lambda *_args: None
    with patched(
        get_slot=lambda _sid: dict(ready, id="a0001"),
        load_qa_swipes=lambda: 19,
    ):
        creator.App.on_run_combined_qa(app)
    check(
        "combined-qa-forwards-selected-ready-slot",
        len(workers) == 1
        and workers[0][0] is app._run_combined_qa
        and workers[0][1:] == ("a0001", 19)
        and any("Run Combined QA for a0001: 19 swipes" in line for line in logs),
    )

    app, logs, workers = bare_app()
    app.worker = None
    app.nb = FakeNotebook()
    app.log_tab = object()
    app._selected_id = lambda: "a0004"
    with patched(get_slot=lambda _sid: dict(base, id="a0004", reddit=False)):
        creator.App.on_run_combined_qa(app)
    check(
        "combined-qa-refuses-incomplete-slot",
        workers == [] and any("cleanup-complete slot" in line for line in logs),
    )

    with tempfile.TemporaryDirectory() as directory:
        settings_path = os.path.join(directory, "qa_settings.json")
        with patched(QA_SETTINGS_PATH=settings_path):
            check("qa-options-default-fourteen", creator.load_qa_swipes() == 14)
            creator.save_qa_swipes(27)
            check("qa-options-round-trip", creator.load_qa_swipes() == 27)
            creator.save_qa_swipes(999)
            check("qa-options-clamp", creator.load_qa_swipes() == 250)

    # Only cleanup-complete slots (including legacy rows) appear as posting
    # choices. Pending/app_stopped rows cannot enable the Post button.
    app, _logs, _workers = bare_app()
    app.post_container = FakeCombobox()
    app._post_container_ids = {}
    rows = [
        dict(base, id="a0001"),
        dict(base, id="a0002", reddit_cleanup="complete"),
        dict(base, id="a0003", reddit_cleanup="pending"),
        dict(base, id="a0004", reddit_cleanup="app_stopped"),
    ]
    with patched(load_slots=lambda: [dict(row) for row in rows]):
        creator.App._refresh_container_choices(app)
    check(
        "posting-dropdown-excludes-incomplete-cleanup",
        set(app._post_container_ids.values()) == {"a0001", "a0002"},
    )

    app, _logs, _workers = bare_app()
    prepare_buttons(app)
    app.post_btn = FakeButton()
    app._selected_container_id = lambda: "a0003"
    by_id = {str(row["id"]): row for row in rows}
    with patched(get_slot=lambda sid: dict(by_id.get(sid, pending))):
        creator.App._sync_row_buttons(app)
    check("pending-disables-post-button", app.post_btn.options.get("state") == "disabled")

    app._selected_container_id = lambda: "a0002"
    with patched(get_slot=lambda sid: dict(by_id.get(sid, ready))):
        creator.App._sync_row_buttons(app)
    check("complete-enables-post-button", app.post_btn.options.get("state") == "normal")

    operational_sources = []
    for relative_path in (
        "apps/commenter_controller.py",
        "apps/poster_controller.py",
        "reddit/reddit_post.py",
        "reddit/reddit_profile_post.py",
        "reddit/reddit_reply_direct.py",
    ):
        with open(PROJECT_ROOT / "src" / "friend_poster" / relative_path, encoding="utf-8") as source:
            operational_sources.append(source.read())
    check(
        "posting-entrypoints-require-cleanup-ready",
        all(
            "slot_reddit_ready" in source and "slot_reddit_done" not in source
            for source in operational_sources
        ),
    )

    original_get_slot = reddit_reply_direct.get_slot
    original_phone = reddit_reply_direct.Phone
    reply_phone_construction: list[bool] = []
    reddit_reply_direct.get_slot = lambda _sid: dict(pending)
    reddit_reply_direct.Phone = lambda *_args, **_kwargs: reply_phone_construction.append(True)
    try:
        reply_result = reddit_reply_direct.run_direct("a0003", {})
    finally:
        reddit_reply_direct.get_slot = original_get_slot
        reddit_reply_direct.Phone = original_phone
    check(
        "direct-reply-pending-stops-before-phone",
        reply_result == 1 and reply_phone_construction == [],
    )

    tutorial_path = PROJECT_ROOT / "docs" / "CREATE_DAY1_TUTORIAL.txt"
    with open(tutorial_path, encoding="utf-8") as tutorial_file:
        tutorial = tutorial_file.read()
    step10 = tutorial.split("## STEP 10", 1)[1].split("## Debug table", 1)[0]
    check(
        "tutorial-step10-uses-creator-finish-cleanup",
        "Finish cleanup" in step10
        and "misc\\close_reddit.py" not in step10
        and "misc/close_reddit.py" not in step10,
    )


def device_separation_regressions() -> None:
    """Current one-button IP flow, history handling, and exact iPhone X selection."""
    init_source = inspect.getsource(creator.App.__init__)
    check(
        "manual-ip-panel-has-exactly-one-button",
        'text="Change IP"' in init_source
        and init_source.count('text="Change IP"') == 1
        and "ip_backend" not in init_source
        and "ip_bind_btn" not in init_source
        and "ip_retry_btn" not in init_source
        and "ip_cancel_btn" not in init_source,
    )
    removed_name = "iPhone " + str(13)
    removed_symbol = "iphone" + str(13)
    removed_backend = "Core" + "Device"
    check(
        "creator-source-has-no-legacy-reset-backend",
        removed_backend not in inspect.getsource(creator)
        and removed_symbol not in inspect.getsource(creator).lower()
        and removed_name not in inspect.getsource(creator),
    )

    def app():
        target, logs, workers = bare_app()
        target.root = FakeRoot()
        target.ip_reset_thread = None
        target.ip_reset_busy = False
        target.pending_ip_action = None
        target.ip_operation_generation = 0
        target.ip_operation_started = 0.0
        target.ip_cancel_event = None
        target.closing = False
        target.macrodroid_setup_ready = True
        target.macrodroid_config_error = ""
        target.macrodroid_webhook = "https://trigger.invalid/private"
        target.falcon_proxy_url = "socks5h://user:pass@proxy.invalid:1080"
        target.ip_previous_var = FakeValue("—")
        target.ip_verified_new_var = FakeValue("—")
        target.ip_verified_at_var = FakeValue("—")
        target.jailbroken_iphone_udid = "iphone-x-udid"
        target.stop = creator.threading.Event()
        target._sync_row_buttons = lambda: None
        return target, logs, workers

    target, logs, _ = app()
    dialogs = FakeMessageBox()
    with patched(load_secrets=lambda: {}, messagebox=dialogs):
        creator.App.on_request_new_ip(target)
    check(
        "missing-configuration-shows-error-without-trigger",
        len(dialogs.errors) == 1
        and target.pending_ip_action is None
        and target.ip_reset_busy is False
        and any("blocked" in line.lower() for line in logs),
    )

    target, logs, _ = app()
    starts = []
    target._start_macrodroid_ip_change = lambda: starts.append(True)
    creator.App.on_request_new_ip(target)
    target.pending_ip_action = {"backend": "macrodroid", "operation_id": 1, "guard_until": 0.0}
    creator.App.on_request_new_ip(target)
    check("one-button-rejects-double-click", starts == [True] and any("already active" in line for line in logs))

    target, _, _ = app()
    backend_starts = []
    target._start_macrodroid_ip_change = (
        lambda **kwargs: backend_starts.append(kwargs) or True
    )
    started = creator.App.request_ip_reset(
        target, trigger_source="backend", show_dialog=False
    )
    check(
        "backend-can-trigger-same-reset-without-panel-click",
        started is True
        and backend_starts == [{"trigger_source": "backend", "show_dialog": False}],
    )

    target, logs, _ = app()
    target._start_macrodroid_ip_change = lambda: starts.append("conflict")
    class ActiveWorker:
        def is_alive(self):
            return True
    target.worker = ActiveWorker()
    creator.App.on_request_new_ip(target)
    check(
        "creator-task-conflict-blocks-ip-trigger",
        "conflict" not in starts and any("current Creator task" in line for line in logs),
    )

    target, logs, _ = app()
    target.pending_ip_action = {
        "backend": "macrodroid", "operation_id": 2,
        "guard_until": creator.time.monotonic() + 100,
    }
    target._start_macrodroid_ip_change = lambda: starts.append("cooldown")
    creator.App.on_request_new_ip(target)
    check("cancel-cooldown-blocks-duplicate-trigger", "cooldown" not in starts)

    target, _, _ = app()
    target.ip_reset_btn = FakeButton()
    creator.App._sync_ip_setup_controls(target, False)
    idle = target.ip_reset_btn.options.get("state") == "normal"
    target.ip_reset_busy = True
    creator.App._sync_ip_setup_controls(target, True)
    check(
        "change-ip-button-locks-and-relabels",
        idle and target.ip_reset_btn.options.get("state") == "disabled"
        and target.ip_reset_btn.options.get("text") == "Changing IP…",
    )

    target, logs, _ = app()
    target.ip_operation_generation = 4
    target.pending_ip_action = {"backend": "macrodroid", "operation_id": 4, "guard_until": 0.0}
    target.ip_reset_busy = True
    dialogs = FakeMessageBox()
    with patched(messagebox=dialogs):
        creator.App._finish_macrodroid_ip_change(target, 3, {"outcome": "verified"})
    stale = target.ip_reset_busy and not dialogs.infos
    with patched(messagebox=dialogs):
        creator.App._finish_macrodroid_ip_change(
            target, 4, {"outcome": "verified", "old_ip": "198.51.100.1",
                        "new_ip": "198.51.100.2", "elapsed_seconds": 10,
                        "confirmed_at_utc": "2026-10-16T12:34:56Z",
                        "history_saved": True},
        )
    check(
        "stale-callback-ignored-and-success-recovers",
        stale and not target.ip_reset_busy and target.pending_ip_action is None
        and len(dialogs.infos) == 1
        and "198.51.100.1" in str(dialogs.infos[0])
        and "198.51.100.2" in str(dialogs.infos[0])
        and target.ip_previous_var.get() == "198.51.100.1"
        and target.ip_verified_new_var.get() == "198.51.100.2"
        and target.ip_verified_at_var.get() == "2026-10-16 12:34:56 UTC"
        and "Creator application route not verified" in str(dialogs.infos[0]),
    )

    target, logs, _ = app()
    restored = {
        "old_ip": "2001:db8::1", "new_ip": "2001:db8::2",
        "confirmed_at_utc": "2026-10-16T13:14:15Z",
    }
    with patched(latest_configured_transition=lambda *_a, **_k: restored):
        creator.App._restore_manual_ip_history(target)
    check(
        "manual-ip-history-restores-latest-proxy-transition",
        target.ip_previous_var.get() == "2001:db8::1"
        and target.ip_verified_new_var.get() == "2001:db8::2"
        and target.ip_verified_at_var.get() == "2026-10-16 13:14:15 UTC"
        and not logs,
    )

    target, logs, _ = app()
    with patched(latest_configured_transition=mock.Mock(side_effect=creator.HistoryError("secret"))):
        creator.App._restore_manual_ip_history(target)
    check(
        "malformed-history-does-not-disable-manual-ip-change",
        target.ip_previous_var.get() == "—"
        and target.ip_verified_new_var.get() == "—"
        and any("could not be restored" in line for line in logs),
    )

    target, logs, _ = app()
    target.ip_operation_generation = 8
    target.pending_ip_action = {"backend": "macrodroid", "operation_id": 8, "guard_until": 0.0}
    target.ip_reset_busy = True
    recorded = []
    operation_uuid = "11111111-1111-4111-8111-111111111111"
    baseline = {"operation_id": operation_uuid, "check_sequence": 1, "phase": "baseline",
                "ip": "198.51.100.10", "observed_at_utc": "2026-10-16T14:00:00Z"}
    pair = [
        {"operation_id": operation_uuid, "check_sequence": seq, "phase": "observation",
         "ip": "198.51.100.11", "observed_at_utc": f"2026-10-16T14:00:0{seq}Z"}
        for seq in (2, 3)
    ]
    def run_ip(_webhook, _proxy, **kwargs):
        kwargs["progress"]({
            "phase": "baseline_warning",
            "status": "Baseline unavailable (timeout); submitting one trigger anyway.",
        })
        for observation in [baseline, *pair]:
            kwargs["on_observation"](observation)
        return {
            "outcome": "verified", "old_ip": baseline["ip"], "new_ip": pair[0]["ip"],
            "elapsed_seconds": 12, "trigger_attempted": True,
            "verification_evidence": {"baseline": baseline, "confirmations": pair},
        }
    dialogs = FakeMessageBox()
    with patched(
        run_macrodroid_change=run_ip,
        record_configured_operation=lambda *a, **k: recorded.append((a, k)) or 2,
        messagebox=dialogs,
    ):
        creator.App._run_macrodroid_ip_change(
            target, 8, operation_uuid, target.macrodroid_webhook,
            target.falcon_proxy_url, creator.threading.Event(),
        )
    check(
        "verified-manual-reset-records-history-and-updates-panel",
        len(recorded) == 1
        and recorded[0][1]["operation_id"] == operation_uuid
        and len(recorded[0][1]["observations"]) == 3
        and target.ip_previous_var.get() == "198.51.100.10"
        and target.ip_verified_new_var.get() == "198.51.100.11"
        and any("WARNING Baseline unavailable (timeout)" in line for line in logs)
        and len(dialogs.infos) == 1,
    )

    target, logs, _ = app()
    target.ip_operation_generation = 9
    target.pending_ip_action = {"backend": "macrodroid", "operation_id": 9, "guard_until": 0.0}
    target.ip_reset_busy = True
    dialogs = FakeMessageBox()
    with patched(
        run_macrodroid_change=run_ip,
        record_configured_operation=mock.Mock(side_effect=creator.HistoryError("secret")),
        messagebox=dialogs,
    ):
        creator.App._run_macrodroid_ip_change(
            target, 9, operation_uuid, target.macrodroid_webhook,
            target.falcon_proxy_url, creator.threading.Event(),
        )
    check(
        "history-write-failure-keeps-verified-result-visible",
        target.ip_previous_var.get() == "198.51.100.10"
        and target.ip_verified_new_var.get() == "198.51.100.11"
        and any("history was not saved" in line.lower() for line in logs)
        and "history was not saved" in str(dialogs.infos[0]).lower(),
    )

    target, _, _ = app()
    target.ip_previous_var.set("198.51.100.20")
    target.ip_verified_new_var.set("198.51.100.21")
    target.ip_operation_generation = 10
    target.pending_ip_action = {"backend": "macrodroid", "operation_id": 10, "guard_until": 0.0}
    target.ip_reset_busy = True
    history_writer = mock.Mock()
    dialogs = FakeMessageBox()
    with patched(
        run_macrodroid_change=lambda *_a, **_k: {
            "outcome": "observed_unverified", "old_ip": "", "new_ip": "",
            "current_ip": "198.51.100.22", "trigger_attempted": True,
            "error": "Current FalconProxy egress was observed, but no pre-reset baseline exists.",
        },
        record_configured_operation=history_writer,
        messagebox=dialogs,
    ):
        creator.App._run_macrodroid_ip_change(
            target, 10, operation_uuid, target.macrodroid_webhook,
            target.falcon_proxy_url, creator.threading.Event(),
        )
    check(
        "missing-baseline-does-not-create-transition-or-replace-verified-display",
        not history_writer.called
        and target.ip_previous_var.get() == "198.51.100.20"
        and target.ip_verified_new_var.get() == "198.51.100.21"
        and len(dialogs.errors) == 1,
    )

    target, _, _ = app()
    target.ip_operation_generation = 6
    target.ip_cancel_event = creator.threading.Event()
    target._save_post_community = lambda: None
    target.qa_process = None
    target.phone = None
    with patched(allow_sleep=lambda: None):
        creator.App.on_close(target)
    check(
        "shutdown-cancels-observation-and-invalidates-callbacks",
        target.closing and target.ip_operation_generation == 7
        and target.ip_cancel_event.is_set() and target.root.destroyed,
    )

    target, _, _ = app()
    target.status = FakeValue("")
    phone_calls = []
    class ExactPhone:
        def __init__(self, *, log, requested_serial):
            phone_calls.append(requested_serial)
            self.serial = requested_serial
            self.ssh_remote_port = 44
        def connect(self, password, also_wda):
            pass
    class ExactStack:
        def __init__(self, phone, log, cancel_event):
            pass
        def attach(self):
            return "attached"
    with patched(Phone=ExactPhone, TapStack=ExactStack):
        creator.App._connect(target, "secret")
    check("creator-selects-exact-iphone-x", phone_calls == ["iphone-x-udid"])
    target.jailbroken_iphone_udid = ""
    with patched(load_secrets=lambda: {}):
        try:
            creator.App._required_jailbroken_iphone_udid(target)
        except creator.PhoneError as exc:
            missing = str(exc)
        else:
            missing = ""
    check("creator-never-falls-back-to-another-device", "will not fall back" in missing)


def bulk_ip_column_regressions() -> None:
    """The account table uses only its own persisted proxy measurement."""
    target, _, _ = bare_app()
    target._selected_id = lambda: ""
    target._sync_row_buttons = lambda: None
    class Tree:
        def __init__(self):
            self.rows = {}
        def get_children(self):
            return list(self.rows)
        def delete(self, item):
            del self.rows[item]
        def insert(self, parent, where, iid, values):
            self.rows[iid] = values
        def selection_set(self, item):
            self.selected = item
    target.tree = Tree()
    rows = [
        {"id": "a0001", "email": "one@icloud.com"},
        {"id": "a0002", "email": "two@icloud.com", "falcon_proxy_verified_ip": "198.51.100.8"},
    ]
    with patched(load_slots=lambda: rows):
        creator.App._refresh_slots(target)
    check(
        "account-table-proxy-ip-is-per-slot-and-old-rows-unknown",
        target.tree.rows["a0001"][5] == "Unknown"
        and "198.51.100.8" in target.tree.rows["a0002"][5]
        and "Proxy IP verified" in target.tree.rows["a0002"][5]
        and target.tree.rows["a0001"][4] == "one@icloud.com",
    )


def creation_pipeline_regressions() -> None:
    completed = {
        "id": "a0041",
        "ghost": True,
        "ghost_container_state": "ready",
        "ghost_container_instance": "OLD0041",
        "email": "done@icloud.com",
        "username": "done-user",
        "password": "done-password",
        "reddit": True,
        "reddit_cleanup": "complete",
    }
    sid, row, is_new = creator._resolve_creation_pipeline_target(
        [dict(completed)], set()
    )
    check(
        "pipeline-completed-tail-selects-a0042",
        (sid, row, is_new) == ("a0042", None, True),
        repr((sid, row, is_new)),
    )

    unfinished = {
        "id": "a0042",
        "ghost": True,
        "ghost_container_state": "ready",
        "ghost_container_instance": "NEW0042",
        "email": "resume@icloud.com",
        "reddit": False,
    }
    sid, row, is_new = creator._resolve_creation_pipeline_target(
        [dict(completed), dict(unfinished)], set()
    )
    check(
        "pipeline-newest-unfinished-resumes",
        sid == "a0042" and row == unfinished and not is_new,
    )
    missing_credentials = dict(completed, id="a0042", password="")
    sid, row, is_new = creator._resolve_creation_pipeline_target(
        [dict(completed), missing_credentials], set()
    )
    check(
        "pipeline-tail-missing-credentials-is-not-complete",
        sid == "a0042" and row == missing_credentials and not is_new,
    )
    sid, row, is_new = creator._resolve_creation_pipeline_target(
        [dict(completed)], {42, 44}
    )
    check(
        "pipeline-retired-ids-are-never-reused",
        (sid, row, is_new) == ("a0045", None, True),
        repr((sid, row, is_new)),
    )
    older_unfinished = dict(unfinished, id="a0035")
    sid, _row, is_new = creator._resolve_creation_pipeline_target(
        [older_unfinished, dict(completed)], set()
    )
    check(
        "pipeline-ignores-older-unfinished-slots",
        sid == "a0042" and is_new,
    )
    duplicate_stopped = False
    try:
        creator._resolve_creation_pipeline_target(
            [dict(unfinished), dict(unfinished)], set()
        )
    except creator.PhoneError:
        duplicate_stopped = True
    check("pipeline-duplicate-tail-stops-safely", duplicate_stopped)

    records = [{"label": "a0042", "email": "recovered.alias@icloud.com"}]
    check(
        "pipeline-unique-label-email-reconciles",
        creator._reconcile_pipeline_email("a0042", [dict(completed)], records)
        == "recovered.alias@icloud.com",
    )
    check(
        "pipeline-missing-label-permits-one-creation",
        creator._reconcile_pipeline_email("a0042", [dict(completed)], []) is None,
    )
    for label, bad_records, rows in (
        (
            "multiple",
            [
                {"label": "a0042", "email": "one@icloud.com"},
                {"label": "a0042", "email": "two@icloud.com"},
            ],
            [dict(completed)],
        ),
        (
            "malformed",
            [{"label": "a0042", "email": "not-an-icloud-address"}],
            [dict(completed)],
        ),
        (
            "conflicting",
            [{"label": "a0042", "email": "owned@icloud.com"}],
            [dict(completed), {"id": "a0039", "email": "owned@icloud.com"}],
        ),
    ):
        stopped = False
        try:
            creator._reconcile_pipeline_email("a0042", rows, bad_records)
        except creator.PhoneError:
            stopped = True
        check(f"pipeline-{label}-email-history-stops", stopped)

    app, logs, workers = bare_app()
    prompt_calls: list[tuple] = []
    chosen_birth = date(2003, 7, 21)

    def prompt_once(slot, **kwargs):
        prompt_calls.append((dict(slot), dict(kwargs)))
        return chosen_birth, 30

    app._prompt_creation_options = prompt_once
    app._run_creation_pipeline = lambda *_args: None
    app.status = FakeValue("")
    with patched(
        load_slots=lambda: [dict(completed)],
        retired_indices=lambda: set(),
    ):
        creator.App.on_start_creation_reddit(app)
    check(
        "pipeline-one-click-prompts-once-and-launches-one-worker",
        len(prompt_calls) == 1
        and len(workers) == 1
        and workers[0][0] is app._run_creation_pipeline
        and workers[0][1:] == (
            "ssh-password-canary",
            "a0042",
            chosen_birth,
            30,
        ),
        repr(workers),
    )
    check(
        "pipeline-new-slot-prompt-does-not-persist-before-confirmation",
        prompt_calls[0][1].get("persist_birth_date") is False,
    )
    check(
        "pipeline-confirmation-log-redacts-birthday-and-password",
        "21.07.2003" not in "\n".join(logs)
        and "ssh-password-canary" not in "\n".join(logs),
    )

    app, _logs, workers = bare_app()
    app.status = FakeValue("")
    app._run_creation_pipeline = lambda *_args: None
    app._prompt_creation_options = lambda *_args, **_kwargs: None
    with patched(
        load_slots=lambda: [dict(completed)],
        retired_indices=lambda: set(),
    ):
        creator.App.on_start_creation_reddit(app)
    check("pipeline-dialog-cancel-starts-nothing", workers == [])

    cleanup_click_slot = dict(
        unfinished,
        username="cleanup-user",
        password="cleanup-password",
        reddit=True,
        reddit_cleanup="pending",
        reddit_birth_date="21.07.2003",
        creation_feed_swipes_target=30,
        creation_feed_swipes_completed=30,
    )
    app, logs, workers = bare_app()
    app.status = FakeValue("")
    app._run_creation_pipeline = lambda *_args: None
    cleanup_prompted: list[bool] = []
    app._prompt_creation_options = (
        lambda *_args, **_kwargs: cleanup_prompted.append(True)
    )
    with patched(
        load_slots=lambda: [dict(completed), dict(cleanup_click_slot)],
        retired_indices=lambda: set(),
    ):
        creator.App.on_start_creation_reddit(app)
    check(
        "pipeline-cleanup-click-uses-persisted-settings-without-prompt",
        cleanup_prompted == []
        and len(workers) == 1
        and workers[0][2] == "a0042"
        and workers[0][4] == 30,
        repr(workers),
    )

    class AliveWorker:
        def is_alive(self):
            return True

    app, _logs, workers = bare_app()
    app.worker = AliveWorker()
    prompted: list[bool] = []
    app._prompt_creation_options = lambda *_args, **_kwargs: prompted.append(True)
    creator.App.on_start_creation_reddit(app)
    check(
        "pipeline-double-click-is-ignored",
        prompted == [] and workers == [],
    )

    class PipelinePhone:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def run_worker_case(
        *,
        initial: dict | None = None,
        records: list[dict] | None = None,
        container_failure: bool = False,
        email_failure: bool = False,
        reddit_failure: bool = False,
        incomplete_final: bool = False,
        stop_after_container: bool = False,
    ):
        app, logs, _workers = bare_app()
        app.root = FakeRoot()
        app.status = FakeValue("")
        app.stop = creator.threading.Event()
        app._after_job = lambda: None
        app.phone = None
        phone = PipelinePhone()
        stack = object()
        connect_calls: list[tuple] = []
        events: list[str] = []
        state = dict(initial) if initial is not None else None

        def current_slot(_sid):
            return dict(state) if state is not None else None

        def checkpoint(sid, _log=None):
            nonlocal state
            events.append("checkpoint")
            state = {
                "id": sid,
                "ghost": False,
                "ghost_container_state": "needed",
                "email": None,
                "reddit": False,
            }
            return dict(state)

        def save_birth(_sid, value):
            events.append("birthday")
            state["reddit_birth_date"] = value
            return dict(state)

        def connect(password):
            connect_calls.append((password, phone, stack))
            return phone, stack

        def container(_phone, _stack, _log, **kwargs):
            events.append("container")
            if container_failure:
                raise creator.PhoneError("synthetic container failure")
            state.update(
                ghost=True,
                ghost_container_state="ready",
                ghost_container_instance="NEW0042",
            )
            if stop_after_container:
                app.stop.set()
            return [kwargs["names"][0]]

        def save_email(_sid, address, _log=None, label=None):
            events.append("save-email")
            state["email"] = address
            state["email_label"] = label
            return dict(state)

        def create_email(_phone, _stack, _log, **_kwargs):
            events.append("create-email")
            if email_failure:
                raise creator.PhoneError("synthetic email failure")
            return "fresh.alias@icloud.com"

        def reddit(_sid, _phone, _stack, **_kwargs):
            events.append("reddit")
            if reddit_failure:
                state["reddit_resume_checkpoint"] = "verify_email"
                raise creator.PhoneError("synthetic Reddit failure")
            state.update(
                username="new-user",
                password="new-password",
                reddit=True,
                reddit_cleanup="pending" if incomplete_final else "complete",
                creation_feed_swipes_target=30,
                creation_feed_swipes_completed=30,
            )

        app._connect = connect
        with patched(
            get_slot=current_slot,
            ensure_ghost_container_needed=checkpoint,
            set_slot_birth_date=save_birth,
            apply_reddit_containers=container,
            load_slots=lambda: [dict(state)] if state is not None else [],
            _load_pipeline_email_records=lambda: list(records or []),
            set_slot_email=save_email,
            create_one_hide_my_email=create_email,
            run_create_reddit=reddit,
        ):
            creator.App._run_creation_pipeline(
                app, "ssh-password-canary", "a0042", chosen_birth, 30
            )
        return app, logs, events, connect_calls, phone, state

    app, logs, events, connects, phone, state = run_worker_case()
    check(
        "pipeline-stage-order-and-one-shared-session",
        events
        == [
            "checkpoint",
            "birthday",
            "container",
            "create-email",
            "save-email",
            "reddit",
        ]
        and len(connects) == 1
        and phone.closed,
        repr((events, connects)),
    )
    check(
        "pipeline-success-requires-all-final-checkpoints",
        any(line == "COMPLETED PIPELINE a0042" for line in logs)
        and state["creation_feed_swipes_completed"] == 30
        and state["reddit_cleanup"] == "complete",
    )

    ready_without_email = {
        "id": "a0042",
        "ghost": True,
        "ghost_container_state": "ready",
        "ghost_container_instance": "NEW0042",
        "reddit_birth_date": "21.07.2003",
        "email": None,
        "reddit": False,
    }
    _app, _logs, events, _connects, _phone, state = run_worker_case(
        initial=ready_without_email,
        records=[{"label": "a0042", "email": "recovered.alias@icloud.com"}],
    )
    check(
        "pipeline-unique-email-recovery-skips-new-alias",
        events == ["save-email", "reddit"]
        and state["email"] == "recovered.alias@icloud.com",
        repr(events),
    )

    fully_staged = dict(
        ready_without_email,
        email="saved.alias@icloud.com",
        email_label="a0042",
    )
    _app, _logs, events, _connects, _phone, _state = run_worker_case(
        initial=fully_staged
    )
    check("pipeline-completed-stages-are-skipped", events == ["reddit"], repr(events))

    for label, kwargs, forbidden in (
        ("container", {"container_failure": True}, {"create-email", "reddit"}),
        (
            "email",
            {"initial": ready_without_email, "email_failure": True},
            {"save-email", "reddit"},
        ),
    ):
        _app, logs, events, _connects, _phone, _state = run_worker_case(**kwargs)
        check(
            f"pipeline-{label}-failure-blocks-later-stages",
            not (set(events) & forbidden)
            and any(line.startswith("FAILED PIPELINE") for line in logs),
            repr((events, logs)),
        )

    _app, logs, events, _connects, _phone, state = run_worker_case(
        initial=fully_staged, reddit_failure=True
    )
    check(
        "pipeline-reddit-failure-preserves-resume-checkpoint",
        events == ["reddit"]
        and state.get("reddit_resume_checkpoint") == "verify_email"
        and any(line.startswith("FAILED PIPELINE") for line in logs),
    )

    _app, logs, events, _connects, _phone, _state = run_worker_case(
        stop_after_container=True
    )
    check(
        "pipeline-stop-blocks-downstream-stages",
        events == ["checkpoint", "birthday", "container"]
        and any(line.startswith("CANCELLED PIPELINE") for line in logs),
        repr((events, logs)),
    )

    _app, logs, events, _connects, _phone, _state = run_worker_case(
        initial=fully_staged, incomplete_final=True
    )
    check(
        "pipeline-refuses-incomplete-final-cleanup",
        events == ["reddit"]
        and any(line.startswith("FAILED PIPELINE") for line in logs)
        and not any(line.startswith("COMPLETED PIPELINE") for line in logs),
    )

    cleanup_slot = dict(
        fully_staged,
        username="cleanup-user",
        password="cleanup-password",
        reddit=True,
        reddit_cleanup="pending",
        creation_feed_swipes_target=30,
        creation_feed_swipes_completed=30,
    )
    _app, logs, events, _connects, _phone, state = run_worker_case(
        initial=cleanup_slot
    )
    check(
        "pipeline-cleanup-only-skips-container-email-and-swipes-setup",
        events == ["reddit"]
        and state["reddit_cleanup"] == "complete"
        and any(line.startswith("COMPLETED PIPELINE") for line in logs),
        repr(events),
    )

    init_source = inspect.getsource(creator.App.__init__)
    combined_source = inspect.getsource(creator.App._run_creation_pipeline)
    check(
        "pipeline-button-added-with-individual-buttons-preserved",
        'text="Start Creation Reddit"' in init_source
        and 'text="New Ghost container"' in init_source
        and 'text="Create Reddit (iPhone X)"' in init_source,
    )
    check(
        "pipeline-worker-reuses-existing-stage-entrypoints",
        "apply_reddit_containers(" in combined_source
        and "create_one_hide_my_email(" in combined_source
        and "run_create_reddit(" in combined_source,
    )


def main() -> int:
    per_account_creation_regressions()
    recover_then_create_regression()
    cleanup_state_regressions()
    creator_cleanup_ui_regressions()
    device_separation_regressions()
    bulk_ip_column_regressions()
    creation_pipeline_regressions()
    print(f"REGRESS {len(PASS)} pass {len(FAIL)} fail", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
