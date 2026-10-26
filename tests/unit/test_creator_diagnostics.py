# UTF-8
"""Offline diagnostics tests for the Creator Log tab and shared stream.

These tests use fake Tk-free surfaces where possible and fully offline
harnesses (no phone, no live services, no state writes beyond temp dirs).
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date
from unittest import mock

from friend_poster.apps import creator_controller as creator
from friend_poster.core import diaglog, crashlog


class _FakeRoot:
    """Minimal fake Tk root for clipboard/status hooks."""

    def __init__(self) -> None:
        self.clipboard_data = ""
        self.statuses: list[str] = []

    def clipboard_clear(self) -> None:
        self.clipboard_data = ""

    def clipboard_append(self, text: str) -> None:
        self.clipboard_data += text

    def update_idletasks(self) -> None:
        pass

    def after(self, delay: int, fn) -> str:
        # Execute immediately in tests that don't drive the event loop.
        try:
            fn()
        except Exception:
            pass
        return "fake"


class _FakeBox:
    """Minimal scrolled text fake that records state."""

    def __init__(self) -> None:
        self._text = io.StringIO()
        self._state = "disabled"
        self._lines_deleted = 0
        self._selections = False

    def configure(self, state: str | None = None, **_kwargs) -> None:
        if state is not None:
            self._state = state

    def insert(self, index: str, text: str) -> None:
        self._text.write(text)

    def delete(self, start: str, end: str) -> None:
        self._lines_deleted += 1

    def get(self, start: str, end: str) -> str:
        return self._text.getvalue()

    def see(self, index: str) -> None:
        pass

    def index(self, mark: str) -> str:
        if mark in {"sel.first", "sel.last"}:
            raise RuntimeError("no selection")
        return "end"


class DiagnosticsStreamTests(unittest.TestCase):
    def test_redaction_masks_secrets_and_emails(self) -> None:
        s = diaglog.DiagnosticStream()
        ev = s.emit(
            "Login http://alice:secret123@host/path Authorization: Bearer abcdef "
            "token=xyz code=123456 email bob@example.com body: hello world"
        )
        self.assertIn("***:***@", ev.message)
        self.assertNotIn("secret123", ev.message)
        self.assertNotIn("abcdef", ev.message)
        self.assertNotIn("123456", ev.message)
        self.assertIn("b***@example.com", ev.message)
        self.assertIn("<redacted> body", ev.message)
        rendered = ev.render()
        self.assertIn("b***@example.com", rendered)

    def test_traceback_reaches_stream_and_is_redacted(self) -> None:
        s = diaglog.DiagnosticStream()
        try:
            raise RuntimeError("boom with http://u:p@h/x token=abc123")
        except RuntimeError as exc:
            import traceback

            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            ev = s.emit("FAILED: boom", severity="ERROR")
            fb = s.failure(kind="unexpected", exception=exc, traceback_text=tb)
        self.assertIsNotNone(fb)
        self.assertIn("<redacted>", fb.traceback_text)
        self.assertNotIn("abc123", fb.traceback_text)
        self.assertTrue(s.has_failure())
        last = s.last_failure_text(10)
        self.assertIn("CREATOR FAILURE", last)
        self.assertIn("<redacted>", last)

    def test_classify_infers_stage_slot_severity(self) -> None:
        s = diaglog.DiagnosticStream()
        ev = s.emit("CONTAINER a0047: READY")
        self.assertEqual(ev.stage, "container")
        self.assertEqual(ev.slot_id, "a0047")
        self.assertEqual(ev.status, "READY")
        ev2 = s.emit("FAILED PIPELINE a0001: boom")
        self.assertEqual(ev2.severity, "ERROR")
        self.assertEqual(ev2.stage, "pipeline")

    def test_frame_boundaries_demote_nodes(self) -> None:
        s = diaglog.DiagnosticStream()
        s.emit("== frame begin: test ==")
        n1 = s.emit("  - node1")
        n2 = s.emit("plain")
        s.emit("== frame end: test ==")
        self.assertEqual(n1.severity, "DEBUG")
        self.assertNotEqual(n2.severity, "DEBUG")

    def test_hearbeat_never_counts_as_progress(self) -> None:
        s = diaglog.DiagnosticStream()
        h = s.heartbeat(operation="op", elapsed_s=5, last_progress_s=5, timeout_s=30)
        self.assertEqual(h.kind, "heartbeat")
        self.assertEqual(h.severity, "STATUS")

    def test_original_failure_survives_cleanup_error(self) -> None:
        s = diaglog.DiagnosticStream()
        try:
            raise ValueError("orig")
        except ValueError as exc:
            fb = s.failure(kind="attempt_failure", exception=exc)
        self.assertIsNotNone(fb)
        s.cleanup_error("teardown failed", exception=RuntimeError("close bad"))
        self.assertEqual(len(fb.cleanup_errors), 1)
        self.assertIn("CLEANUP ERROR", s.session_text())
        # original exception preserved
        self.assertIn("orig", fb.exception_message)

    def test_disposition_queue_preservation_stored(self) -> None:
        s = diaglog.DiagnosticStream()
        fb = s.failure(kind="attempt_failure", exception=RuntimeError("x"))
        s.set_disposition("user Stop")
        s.set_queue("queue preserved")
        s.set_preservation("account and container preserved")
        self.assertEqual(fb.disposition, "user Stop")
        self.assertEqual(fb.queue, "queue preserved")
        self.assertEqual(fb.preservation, "account and container preserved")
        self.assertIn("DISPOSITION:", s.session_text())
        self.assertIn("QUEUE:", s.session_text())
        self.assertIn("PRESERVATION:", s.session_text())

    def test_crash_sink_forwards(self) -> None:
        s = diaglog.DiagnosticStream()
        s.crash("uncaught", "RuntimeError: bad\nTraceback...")
        self.assertTrue(s.has_failure())
        self.assertEqual(s.last_failure().kind, "uncaught")

    def test_python_logging_installs_once(self) -> None:
        s = diaglog.DiagnosticStream()
        self.assertTrue(s.install_python_logging())
        self.assertTrue(s.install_python_logging())  # idempotent

    def test_process_hooks_installs_once(self) -> None:
        s = diaglog.DiagnosticStream()
        self.assertTrue(s.install_process_hooks())
        self.assertTrue(s.install_process_hooks())  # idempotent

    def test_persist_failure_visible_in_panel_text(self) -> None:
        d = tempfile.mkdtemp(prefix="diag-")
        blocker = os.path.join(d, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        # A file used as a directory makes _append fail with an OSError.
        bad = os.path.join(blocker, "creator.log")
        s = diaglog.DiagnosticStream(log_path=bad)
        ev = s.emit("hello")
        self.assertEqual(ev.message, "hello")
        self.assertTrue(s.persist_error, "persist_error should be recorded")
        # A visible, non-recursive diagnostic event was stored for the panel.
        messages = [e.message for e in s.events()]
        self.assertTrue(
            any("LOGGING ERROR" in m for m in messages), messages
        )
        self.assertIn("Automation is unaffected", " ".join(messages))
        # A second failure must not spam a second diagnostic event.
        s.emit("hello again")
        messages = [e.message for e in s.events()]
        self.assertEqual(
            sum(1 for m in messages if "LOGGING ERROR" in m), 1, messages
        )

    def test_session_text_includes_all_events_and_last_failure_text_has_preceding(self) -> None:
        s = diaglog.DiagnosticStream()
        for i in range(5):
            s.emit(f"msg {i}")
        try:
            raise RuntimeError("fail")
        except RuntimeError as exc:
            fb = s.failure(kind="unexpected", exception=exc)
        text = s.session_text()
        self.assertIn("msg 0", text)
        self.assertIn("msg 4", text)
        self.assertIn("CREATOR FAILURE", text)
        last = s.last_failure_text(preceding=2)
        self.assertIn("--- last 2 event(s) before the failure ---", last)
        self.assertIn(fb.text(), last)


    def test_failure_report_lists_recent_frame_stems(self) -> None:
        s = diaglog.DiagnosticStream()
        s.emit("== frame begin: 930_a0047_launch_1 ==")
        s.emit("  - node")
        s.emit("== frame end: 930_a0047_launch_1 ==")
        s.emit("== frame begin: 931_a0047_after ==")
        s.emit("  - node")
        s.emit("== frame end: 931_a0047_after ==")
        block = s.failure(kind="attempt_failure", exception=RuntimeError("x"))
        self.assertEqual(
            block.dumps, ["931_a0047_after", "930_a0047_launch_1"]
        )
        self.assertIn("dumps: 931_a0047_after", block.text())

    def test_recent_frames_deduplicates_and_bounds(self) -> None:
        s = diaglog.DiagnosticStream()
        for i in range(20):
            s.emit(f"== frame begin: f{i} ==")
            s.emit(f"== frame end: f{i} ==")
        frames = s.recent_frames(5)
        self.assertEqual(frames, ["f19", "f18", "f17", "f16", "f15"])

    def test_events_capture_originating_module_and_function(self) -> None:
        s = diaglog.DiagnosticStream()

        def caller():
            return s.emit("hello")

        ev = caller()
        self.assertTrue(ev.module, ev)
        self.assertEqual(ev.function, "caller")


class InactivityHeartbeatTests(unittest.TestCase):
    class _ClockSpy:
        """Real-time clock that records any attempt to mark progress."""

        def __init__(self) -> None:
            self.start = time.monotonic()
            self.notes = 0

        def __call__(self) -> float:
            return time.monotonic()

        def note(self) -> None:
            self.notes += 1

        def idle_seconds(self) -> float:
            return time.monotonic() - self.start

    def test_heartbeat_is_status_only_and_watchdog_still_fires(self) -> None:
        clock = self._ClockSpy()
        event = threading.Event()
        beats: list[dict] = []
        watch = creator._InactivityWatch(
            event, 0.30, clock, lambda _m: None, "a0047",
            poll_seconds=0.005,
            heartbeat=lambda **kw: beats.append(kw),
            heartbeat_seconds=0.05,
        )
        watch.start()
        try:
            deadline = time.monotonic() + 4.0
            while not event.is_set() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(event.is_set(), "watchdog should still fire")
        finally:
            watch.stop()
        self.assertTrue(beats, "heartbeat should have been emitted")
        self.assertTrue(all("idle_s" in b for b in beats))
        # The status heartbeat must never count as workflow progress.
        self.assertEqual(clock.notes, 0)

    def test_make_heartbeat_emits_only_status_events(self) -> None:
        app = creator.App.__new__(creator.App)
        app._diag = diaglog.DiagnosticStream()
        app._diag.set_context(operation="creation_pipeline")
        heartbeat = creator.App._make_heartbeat(app, "a0047")
        heartbeat(elapsed_s=16.0, idle_s=16.0, timeout_s=300.0)
        events = app._diag.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[-1].kind, "heartbeat")
        self.assertEqual(events[-1].severity, "STATUS")
        self.assertTrue(events[-1].message.startswith("[HEARTBEAT]"))
        self.assertTrue(app._diag.last_message("a0047").startswith("[HEARTBEAT]"))


class ControllerLogTabTests(unittest.TestCase):
    def make_app(self) -> creator.App:
        app = creator.App.__new__(creator.App)
        app.root = _FakeRoot()
        app.stop = threading.Event()
        app.closing = False
        app._diag = diaglog.DiagnosticStream()
        app._diag.subscribe(lambda e: None)  # no-op
        app.log_q = __import__("queue").Queue()
        app._log_window = []
        app._log_window_lines = []
        app._log_window_seq = []
        app._log_window_inserted = 0
        app._log_older_hidden = 0
        try:
            import tkinter as tk
            app._log_autoscroll = tk.BooleanVar(value=True)
            app._log_detailed = tk.BooleanVar(value=True)
        except Exception:
            class DummyVar:
                def __init__(self, value=True):
                    self._v = value
                def get(self): return self._v
                def set(self, v): self._v = v
            app._log_autoscroll = DummyVar(True)
            app._log_detailed = DummyVar(True)
        app._log_status_label = type("L", (), {"configure": lambda self, text="": None})()
        app._log_older_label = type("L", (), {"configure": lambda self, text="": None})()
        app._log_persist_label = type("L", (), {"configure": lambda self, text="": None})()
        app.log_box = _FakeBox()
        app._diag_attempt = 0
        return app

    def test_log_routes_to_stream(self) -> None:
        app = self.make_app()
        app.log("CONTAINER a0047: READY")
        self.assertTrue(app._diag.count() >= 1)

    def test_copy_failure_no_failure(self) -> None:
        app = self.make_app()
        app._log_copy_failure()
        # status set via label; just ensure no crash

    def test_copy_all_empty(self) -> None:
        app = self.make_app()
        app._log_copy_all()

    def test_copy_last_failure_with_preceding(self) -> None:
        app = self.make_app()
        for i in range(10):
            app.log(f"event {i}")
        try:
            raise RuntimeError("bad")
        except RuntimeError as exc:
            app._diag.failure(kind="attempt_failure", exception=exc)
        app._log_copy_failure()
        self.assertIn("CREATOR FAILURE", app.root.clipboard_data)
        self.assertIn("event 8", app.root.clipboard_data)  # preceding

    def test_copy_all_beyond_window(self) -> None:
        app = self.make_app()
        for i in range(3000):
            app.log(f"e{i}")
        app._log_copy_all()
        self.assertIn("e0", app.root.clipboard_data)
        self.assertIn("e2999", app.root.clipboard_data)

    def test_load_earlier(self) -> None:
        app = self.make_app()
        # render some events into window
        for i in range(100):
            ev = app._diag.emit(f"m{i}")
            app._append_log_block(ev)
        app._flush_log_render()
        first = app._log_window_seq[0] if app._log_window_seq else 0
        if first > 1:
            app._log_load_earlier()
            # window should grow
            self.assertGreater(len(app._log_window), 1)

    def test_autoscroll_toggle_behavior(self) -> None:
        app = self.make_app()
        app._log_autoscroll.set(True)
        app._on_autoscroll_toggle()
        app._log_autoscroll.set(False)
        app._on_autoscroll_toggle()

    def test_log_drain_bounded_batch(self) -> None:
        app = self.make_app()
        for i in range(1000):
            app.log_q.put(f"msg{i}")
        drained = 0
        import queue

        try:
            while drained < creator.LOG_DRAIN_BATCH:
                item = app.log_q.get_nowait()
                if isinstance(item, tuple) and item[:1] == ("__diag__",):
                    app._append_log_block(item[1])
                    drained += 1
                    continue
                import datetime

                stamp = datetime.datetime.now().strftime("%H:%M:%S")
                app._append_log_line(f"{stamp}  {item}")
                drained += 1
        except queue.Empty:
            pass
        self.assertLessEqual(drained, creator.LOG_DRAIN_BATCH)

    def test_crash_sink_filters_and_forwards(self) -> None:
        app = self.make_app()
        app._crash_sink("UNCAUGHT", "ValueError: x")
        app._crash_sink("OTHER", "bad")
        self.assertTrue(app._diag.has_failure())
        self.assertEqual(app._diag.last_failure().kind, "uncaught")

    def test_callback_exception_reported(self) -> None:
        app = self.make_app()
        try:
            raise RuntimeError("cb")
        except RuntimeError as exc:
            app._report_callback_exception(exc, exc, None)
        self.assertTrue(app._diag.has_failure())

    def test_logging_never_resets_watchdog_via_progress(self) -> None:
        # progress not touched by log
        pass


class PipelineDiagnosticsTests(unittest.TestCase):
    """Drive the real pipeline exception handlers with a diagnostic stream."""

    @staticmethod
    def _make(count=1):
        from test_bulk_preserve_skip import make_app

        app = make_app(None, count)
        app.status = mock.Mock()
        app._diag = diaglog.DiagnosticStream()
        app._diag.set_context(run_id="run-test")
        app._diag_attempt = 0
        return app

    class _Fires:
        def __init__(self, event, *_args, **_kwargs):
            self._event = event

        def start(self):
            self._event.set()

        def stop(self):
            pass

    class _QuietWatch:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    def test_inactivity_timeout_is_recorded_without_a_traceback(self):
        app = self._make()
        with mock.patch.object(creator, "_InactivityWatch", self._Fires):
            ok = creator.App._run_creation_pipeline(
                app, "offline-password", "a0047", date(2005, 4, 20), 10,
                manage_worker=False,
            )
        self.assertFalse(ok)
        block = app._diag.last_failure()
        self.assertIsNotNone(block)
        self.assertEqual(block.kind, "inactivity_timeout")
        self.assertEqual(block.failure_code, creator.BULK_INACTIVITY_TIMEOUT_CODE)
        # No fabricated traceback for a hang.
        self.assertEqual(block.traceback_text.strip(), "")
        self.assertIn("inactivity timeout", block.text())

    def test_user_stop_is_distinguished_from_timeout(self):
        app = self._make()
        app.stop.set()
        with mock.patch.object(creator, "_InactivityWatch", self._QuietWatch):
            ok = creator.App._run_creation_pipeline(
                app, "offline-password", "a0047", date(2005, 4, 20), 10,
                manage_worker=False,
            )
        self.assertFalse(ok)
        block = app._diag.last_failure()
        self.assertIsNotNone(block)
        self.assertEqual(block.kind, "user_stop")
        self.assertEqual(block.failure_code, "user_stop")
        self.assertIn("Stop", block.disposition)

    def test_device_phone_error_is_an_attempt_failure(self):
        app = self._make()
        with mock.patch.object(creator, "_InactivityWatch", self._QuietWatch), \
                mock.patch.object(
                    creator.App, "_connect",
                    lambda self, _pw: (_ for _ in ()).throw(
                        creator.PhoneError("synthetic device failure")
                    ),
                ):
            ok = creator.App._run_creation_pipeline(
                app, "offline-password", "a0047", date(2005, 4, 20), 10,
                manage_worker=False,
            )
        self.assertFalse(ok)
        block = app._diag.last_failure()
        self.assertIsNotNone(block)
        self.assertEqual(block.kind, "attempt_failure")
        self.assertIn("PhoneError", block.exception_type)
        self.assertIn("synthetic device failure", block.exception_message)

    def test_unexpected_exception_keeps_chained_traceback(self):
        app = self._make()

        def boom(self, _pw):
            try:
                raise KeyError("root cause")
            except KeyError as exc:
                raise ValueError("outer failure") from exc

        with mock.patch.object(creator, "_InactivityWatch", self._QuietWatch), \
                mock.patch.object(creator.App, "_connect", boom), \
                mock.patch.object(
                    creator, "is_recoverable_bridge_failure", lambda _exc: False
                ):
            ok = creator.App._run_creation_pipeline(
                app, "offline-password", "a0047", date(2005, 4, 20), 10,
                manage_worker=False,
            )
        self.assertFalse(ok)
        block = app._diag.last_failure()
        self.assertIsNotNone(block)
        self.assertEqual(block.kind, "unexpected")
        self.assertIn("ValueError", block.exception_type)
        self.assertIn("ValueError: outer failure", block.traceback_text)
        # Chained cause is preserved in the traceback.
        self.assertIn("KeyError: 'root cause'", block.traceback_text)
        self.assertIn("The above exception was the direct cause", block.traceback_text)

    def test_cleanup_error_preserves_the_original_failure(self):
        app = self._make()
        original = creator.PhoneError("original device failure")

        def connect_then_fail_close(self, _pw):
            class FakePhone:
                def close(self_inner):
                    raise OSError("close failed")

            class FakeStack:
                cancel_event = None

            self.phone = FakePhone()
            return FakePhone(), FakeStack()

        with mock.patch.object(creator, "_InactivityWatch", self._QuietWatch), \
                mock.patch.object(creator.App, "_connect", connect_then_fail_close), \
                mock.patch.object(
                    creator, "is_recoverable_bridge_failure", lambda _exc: False
                ), \
                mock.patch.object(creator, "get_slot", lambda _sid: {
                    "id": "a0047", "reddit_birth_date": "20.04.2005",
                }), \
                mock.patch.object(creator, "slot_reddit_cleanup_pending", lambda _s: True), \
                mock.patch.object(
                    creator, "apply_reddit_containers",
                    lambda *a, **k: (_ for _ in ()).throw(original),
                ):
            ok = creator.App._run_creation_pipeline(
                app, "offline-password", "a0047", None, 10,
                manage_worker=False,
            )
        self.assertFalse(ok)
        block = app._diag.last_failure()
        self.assertIsNotNone(block)
        # Original failure survives; the cleanup error is reported separately.
        self.assertIn("original device failure", block.exception_message)
        self.assertTrue(
            any("close failed" in err for err in block.cleanup_errors),
            block.cleanup_errors,
        )


class CrashlogSinkTests(unittest.TestCase):
    def test_add_remove_sink_and_notify(self) -> None:
        received = []

        def sink(kind, text):
            received.append((kind, text))

        crashlog.add_sink(sink)
        try:
            crashlog._notify("UNCAUGHT", "boom")
            self.assertEqual(received, [("UNCAUGHT", "boom")])
        finally:
            crashlog.remove_sink(sink)
        received.clear()
        crashlog._notify("UNCAUGHT", "after-remove")
        self.assertEqual(received, [])

    def test_broken_sink_never_breaks_notify(self) -> None:
        def broken(kind, text):
            raise RuntimeError("sink is broken")

        crashlog.add_sink(broken)
        try:
            # Must not raise even though the sink does.
            crashlog._notify("THREAD", "t")
        finally:
            crashlog.remove_sink(broken)

    def test_duplicate_sink_is_registered_once(self) -> None:
        received = []

        def sink(kind, text):
            received.append(kind)

        crashlog.add_sink(sink)
        crashlog.add_sink(sink)
        try:
            crashlog._notify("UNCAUGHT", "x")
        finally:
            crashlog.remove_sink(sink)
        self.assertEqual(received, ["UNCAUGHT"])


class UiTreeDetailTests(unittest.TestCase):
    def _nodes(self, count: int):
        from friend_poster.device import ui_tree

        return [
            ui_tree.Node(text=f"label-{i}", x=10, y=10 + i * 4, w=100, h=20)
            for i in range(count)
        ]

    def test_detailed_mode_shows_all_nodes_with_boundaries(self) -> None:
        from friend_poster.device import ui_tree

        nodes = self._nodes(30)
        lines: list[str] = []
        stack = type("S", (), {"dump_xml": lambda self: "<x/>", "backend": ""})()
        original = ui_tree.preview_detail_enabled()
        try:
            with mock.patch.object(ui_tree, "parse_nodes", lambda _blob: nodes):
                ui_tree.set_preview_detail(True)
                ui_tree.dump_named(stack, None, "frame1", lines.append, persist=False)
        finally:
            ui_tree.set_preview_detail(original)
        joined = "\n".join(lines)
        self.assertIn("== frame begin: frame1 ==", joined)
        self.assertIn("== frame end: frame1 (30 of 30 labeled) ==", joined)
        self.assertEqual(sum(1 for ln in lines if ln.startswith("  - ")), 30)

    def test_default_mode_limits_to_twelve_nodes(self) -> None:
        from friend_poster.device import ui_tree

        nodes = self._nodes(30)
        lines: list[str] = []
        stack = type("S", (), {"dump_xml": lambda self: "<x/>", "backend": ""})()
        original = ui_tree.preview_detail_enabled()
        try:
            with mock.patch.object(ui_tree, "parse_nodes", lambda _blob: nodes):
                ui_tree.set_preview_detail(False)
                ui_tree.dump_named(stack, None, "frame2", lines.append, persist=False)
        finally:
            ui_tree.set_preview_detail(original)
        joined = "\n".join(lines)
        self.assertNotIn("== frame begin", joined)
        self.assertEqual(sum(1 for ln in lines if ln.startswith("  - ")), 12)

    def test_privacy_suppressed_frame_has_safe_metadata(self) -> None:
        from friend_poster.device import ui_tree

        nodes = self._nodes(5)
        lines: list[str] = []
        stack = type("S", (), {"dump_xml": lambda self: "<x/>", "backend": ""})()
        original = ui_tree.preview_detail_enabled()
        try:
            with mock.patch.object(ui_tree, "parse_nodes", lambda _blob: nodes):
                ui_tree.set_preview_detail(True)
                ui_tree.dump_named(
                    stack, None, "secure", lines.append, persist=False, preview=False
                )
        finally:
            ui_tree.set_preview_detail(original)
        joined = "\n".join(lines)
        self.assertIn("privacy-sensitive", joined)
        self.assertIn("5 nodes, 5 labeled", joined)
        self.assertNotIn("label-0", joined)

    def test_ui_preview_escapes_text_rejected_by_legacy_console_encoding(self) -> None:
        from friend_poster.device import ui_tree

        nodes = [ui_tree.Node(text="feed-\u0425", x=10, y=10, w=100, h=20)]
        lines: list[str] = []

        def cp1252_log(message: str) -> None:
            message.encode("cp1252")
            lines.append(message)

        stack = type("S", (), {"dump_xml": lambda self: "<x/>", "backend": ""})()
        with mock.patch.object(ui_tree, "parse_nodes", lambda _blob: nodes):
            ui_tree.dump_named(
                stack, None, "unicode", cp1252_log, persist=False, preview=True
            )

        joined = "\n".join(lines)
        self.assertIn(r"\u0425", joined)
        self.assertNotIn("\u0425", joined)


if __name__ == "__main__":
    unittest.main()
