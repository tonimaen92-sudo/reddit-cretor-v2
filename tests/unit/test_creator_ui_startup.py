# UTF-8
"""Creator's batch panel must build and wire every batch button to a real handler.

Regression: ``_build_batch_tab`` bound the Stop button to a non-existent
attribute (``self.stop_bulk_batch``), so constructing the batch tab raised
``AttributeError`` and the Creator could not start.

These tests build the real ttk batch tab against an offscreen Tk root and prove
the panel starts, that the count Save / Start / Retry-Resume / Save & Extend /
Stop controls each resolve to an implemented App method, and that the Stop
button reaches ``_stop_bulk_batch``.
Everything is offline: the draft-save and slot-panel seams are isolated so no
live state file is read or written, and no batch is started.
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import tkinter as tk
from tkinter import ttk

from friend_poster.apps import creator
from friend_poster.core import bulk_creation


class CreatorBatchTabStartupTests(unittest.TestCase):
    """The batch tab is the Creator's startup path for local batch controls."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.root = tk.Tk()
        except Exception as exc:  # pragma: no cover - headless CI without Tk
            raise unittest.SkipTest(f"Tk is unavailable: {exc}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        root = getattr(cls, "root", None)
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass

    def make_app(self) -> creator.App:
        """A minimal offline App carrying only what the batch tab reads."""
        app = creator.App.__new__(creator.App)  # never runs __init__ (no live state)
        app.root = self.root
        app.stop = threading.Event()
        app.closing = False
        app.bulk_state = bulk_creation.empty_state()
        app.bulk_draft = bulk_creation.empty_draft()
        app.bulk_load_error = ""
        app.bulk_draft_load_error = ""
        app.bulk_draft_reconcile_error = ""
        app.bulk_editor_baseline = []
        app.bulk_draft_suspended = False
        app.bulk_draft_after_id = None
        app.logs = []
        app.log = app.logs.append
        # Keep the panel off every live file: the draft save and the slot-panel
        # refresh touch real paths, and Stop would persist live batch state.
        app._save_bulk_draft_now = lambda: False
        app._refresh_bulk_panel = lambda: None
        app._persist_bulk = lambda _state: None
        app._sync_row_buttons = lambda: None
        app.batch_tab = ttk.Frame(self.root)
        return app

    def build_panel(self) -> creator.App:
        app = self.make_app()
        # This call raised AttributeError before the fix.
        app._build_batch_tab()
        return app

    def test_batch_panel_builds_and_every_batch_callback_resolves(self):
        app = self.build_panel()
        for attr in (
            "bulk_count_save_btn",
            "bulk_start_btn",
            "bulk_retry_btn",
            "bulk_extend_btn",
            "bulk_stop_btn",
        ):
            button = getattr(app, attr)
            self.assertIsInstance(button, ttk.Button)
            # A registered Tcl command proves the callback resolved at build.
            self.assertTrue(str(button.cget("command")), attr)
        for method in (
            "save_bulk_item_count",
            "_on_start_bulk_batch",
            "retry_bulk_batch",
            "extend_bulk_batch",
            "_stop_bulk_batch",
        ):
            self.assertTrue(hasattr(creator.App, method), method)

    def test_stop_button_calls_the_implemented_stop_handler(self):
        with mock.patch.object(creator.App, "_stop_bulk_batch") as stop_spy:
            app = self.build_panel()
            app.bulk_stop_btn.invoke()
        stop_spy.assert_called_once_with()

    def test_stop_button_reaches_the_real_handler_and_raises_stop(self):
        app = self.build_panel()
        self.assertFalse(app.stop.is_set())
        app.bulk_stop_btn.invoke()
        self.assertTrue(app.stop.is_set())


if __name__ == "__main__":
    unittest.main()
