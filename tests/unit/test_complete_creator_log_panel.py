"""Offline regressions for the Creator panel's complete-session log view."""

from __future__ import annotations

import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

from friend_poster.apps import creator_controller as creator
from friend_poster.core import diaglog


class _Var:
    def __init__(self, value=True):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Label:
    def __init__(self):
        self.text = ""

    def configure(self, *, text="", **_kwargs):
        self.text = text


class _Root:
    def __init__(self):
        self.clipboard_data = ""

    def clipboard_clear(self):
        self.clipboard_data = ""

    def clipboard_append(self, text):
        self.clipboard_data += text

    def update_idletasks(self):
        return None


class _Box:
    def __init__(self):
        self.text = ""
        self.state = "disabled"
        self.delete_calls = []
        self.yview_calls = []
        self.see_calls = []

    def configure(self, *, state=None, **_kwargs):
        if state is not None:
            self.state = state

    def insert(self, index, text):
        if index == "1.0":
            self.text = text + self.text
        else:
            self.text += text

    def delete(self, start, end):
        self.delete_calls.append((start, end))

    def get(self, _start, _end):
        return self.text

    def see(self, index):
        self.see_calls.append(index)

    def yview_moveto(self, fraction):
        self.yview_calls.append(float(fraction))


class CompleteCreatorLogPanelTests(unittest.TestCase):
    def make_app(self):
        app = creator.App.__new__(creator.App)
        app.root = _Root()
        app._diag = diaglog.DiagnosticStream()
        app.log_box = _Box()
        app._log_window = []
        app._log_window_lines = []
        app._log_window_seq = []
        app._log_window_inserted = 0
        app._log_older_hidden = 0
        app._log_autoscroll = _Var(True)
        app._log_detailed = _Var(True)
        app.log_older_label = _Label()
        app.log_persist_label = _Label()
        app.log_status_label = _Label()
        return app

    @staticmethod
    def append_all(app):
        for event in app._diag.events():
            app._append_log_block(event)
        app._flush_log_render()

    def test_visible_panel_never_trims_even_past_old_small_limit(self):
        app = self.make_app()
        for index in range(8):
            event = app._diag.emit(f"event {index}\ndetail {index}")
            app._append_log_block(event)

        with mock.patch.object(
            creator, "MAX_RENDERED_LOG_LINES", 3, create=True
        ), mock.patch.object(creator, "LOG_HISTORY_SLACK", 0, create=True):
            app._flush_log_render()

        self.assertEqual(app.log_box.delete_calls, [])
        self.assertIn("event 0\ndetail 0", app.log_box.text)
        self.assertIn("event 7\ndetail 7", app.log_box.text)
        self.assertEqual(
            app.log_box.text.rstrip("\n"),
            app._diag.session_text(),
        )

    def test_default_diagnostic_stream_has_unbounded_session_storage(self):
        stream = diaglog.DiagnosticStream()
        self.assertIsNone(
            stream._events.maxlen,
            "the default Creator session must not silently discard early events",
        )
        for index in range(1200):
            stream.emit(f"chronological event {index}")
        text = stream.session_text()
        self.assertEqual(stream.count(), 1200)
        self.assertIn("chronological event 0", text)
        self.assertIn("chronological event 1199", text)

    def test_panel_copy_and_save_share_complete_sanitized_session(self):
        app = self.make_app()
        app._diag.emit("first token=super-secret", severity="DEBUG")
        app._diag.emit("second status", severity="STATUS")
        app._diag.emit("third warning", severity="WARNING")
        try:
            try:
                raise KeyError("inner failure")
            except KeyError as exc:
                raise RuntimeError("outer failure") from exc
        except RuntimeError as exc:
            app._diag.failure(
                kind="unexpected",
                exception=exc,
                traceback_text="".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ),
            )

        self.append_all(app)
        expected = app._diag.session_text()
        app._log_copy_all()

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "complete-session.txt"
            with mock.patch.object(
                creator.filedialog, "asksaveasfilename", return_value=str(target)
            ):
                app._log_save()
            saved = target.read_text(encoding="utf-8")

        self.assertEqual(app.log_box.text.rstrip("\n"), expected)
        self.assertEqual(app.root.clipboard_data, expected)
        self.assertEqual(saved, expected)
        self.assertNotIn("super-secret", expected)
        self.assertIn("token=<redacted>", expected)
        self.assertIn("KeyError: 'inner failure'", expected)
        self.assertIn("The above exception was the direct cause", expected)
        self.assertIn("RuntimeError: outer failure", expected)

    def test_show_complete_moves_to_start_without_hiding_or_deleting(self):
        app = self.make_app()
        for index in range(5):
            app._append_log_block(app._diag.emit(f"event {index}"))
        app._flush_log_render()
        before_text = app.log_box.text
        before_window = list(app._log_window)

        show_complete = getattr(
            app,
            "_log_show_complete",
            app._log_load_earlier,
        )
        show_complete()

        self.assertEqual(app.log_box.text, before_text)
        self.assertEqual(app._log_window, before_window)
        self.assertEqual(app.log_box.delete_calls, [])
        self.assertIn(0.0, app.log_box.yview_calls)


if __name__ == "__main__":
    unittest.main()
