"""Offline regression tests for the Start Batch / Resume separation.

"Start Batch" always begins a genuinely new run from the validated editor
rows: it archives the superseded batch and raw draft, permanently reserves a
vanished slot id without fabricating an account row, allocates a slot above
every known reservation, clears old per-attempt state, and refuses (with
machine-readable log codes) while a worker, foreign writer, IP guard, or
unstartable stage is in the way.  "Retry / Resume" continues the saved batch
and, when a slot the batch already worked against no longer exists, points the
user at Start Batch instead of rebuilding it.

Everything is offline: persistence seams are mocked, workers are stubbed, and
no production state, draft, or slots file is ever read or written.
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime
from unittest import mock

from friend_poster.apps import creator
from friend_poster.core import bulk_creation
from test_bulk_creator import Root, Stop


def parse_birth(value):
    return datetime.strptime(value, "%d.%m.%Y").date()


class Value:
    """Minimal stand-in for a Tk variable."""

    def __init__(self, value=""):
        self.value = value

    def get(self, *_args):
        return self.value

    def set(self, value):
        self.value = value


class FakeWorker:
    def __init__(self, alive: bool):
        self._alive = alive

    def is_alive(self):
        return self._alive


def make_app(**state):
    """A minimal offline Creator App with captured logs and stubbed worker."""
    app = creator.App.__new__(creator.App)
    app.root = Root()
    app.stop = Stop()
    app.closing = False
    app.bulk_mutex = threading.RLock()
    app.bulk_password = ""
    app.pending_ip_action = None
    app.ip_reset_busy = False
    app.worker = None
    app.logs = []
    app.log = app.logs.append
    app._after_job = lambda: None
    app.bulk_state = bulk_creation.empty_state()
    app.bulk_state.update(
        queue=[{"birth_date": "01.01.2000", "swipes": 10, "delay_seconds": 2.0}],
        requested=1,
        remaining=1,
        stage="idle",
    )
    app.bulk_state.update(state)
    app.starts = []
    app._start_bulk_worker = lambda: app.starts.append(dict(app.bulk_state))
    # Offline seam for the durable-state reload a real Start performs; it reads
    # this app's in-memory state so no production file is ever touched.
    app._latest_bulk_state = lambda: dict(app.bulk_state)
    return app


def new_rows(count=2):
    return [
        {"birth_date": f"{i + 1:02d}.01.2001", "swipes": 10 + i, "delay_seconds": 2.0 + i}
        for i in range(count)
    ]


class FreshStartTests(unittest.TestCase):
    def test_first_start_from_idle_archives_nothing_and_allocates_next_slot(self):
        app = make_app(queue=[], requested=0, remaining=0, stage="idle")
        with (
            mock.patch.object(creator, "indices_used", lambda: {1, 2}),
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "archive_bulk_state") as archive_state,
            mock.patch.object(creator, "archive_bulk_draft") as archive_draft,
        ):
            self.assertTrue(app.start_bulk_batch(new_rows(1), 1, "pw"))

        archive_state.assert_not_called()
        archive_draft.assert_not_called()
        self.assertEqual(len(app.starts), 1)
        saved = app.starts[0]
        self.assertEqual(saved["slot_id"], "a0003")
        self.assertTrue(saved["delete_containers"])
        self.assertEqual(saved["stage"], "ip_pending")
        self.assertTrue(any("start_slot_allocated" in line for line in app.logs))

    def test_start_reserves_vanished_slot_then_allocates_above_it(self):
        # Reported scenario: the stopped batch points at a0072 whose row is
        # gone.  Start must reserve a0072 first and run on a0073.
        app = make_app(
            queue=new_rows(1), requested=1, remaining=1,
            stage="stopped", slot_id="a0072",
            failures=[{"slot_id": "a0070", "code": "email_invalid"}],
            verified_ip="198.51.100.7", baseline_ip="198.51.100.1",
            ip_attempt={"operation_id": "x"},
        )
        events = []
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: None),
            mock.patch.object(
                creator, "retire_slot_id",
                lambda sid: events.append(("reserve", sid)) or True,
            ),
            mock.patch.object(creator, "indices_used", lambda: set(range(1, 72))),
            mock.patch.object(
                creator, "archive_bulk_state",
                side_effect=lambda _state, **_kw: events.append(("archive", True))
                or Path_("bulk_creation.superseded-test.json"),
            ),
            mock.patch.object(
                creator, "archive_bulk_draft",
                side_effect=lambda **_kw: events.append(("archive-draft", True))
                or Path_("bulk_draft.superseded-test.json"),
            ),
            mock.patch.object(
                creator, "save_bulk_state",
                side_effect=lambda state: events.append(("save", dict(state))),
            ),
        ):
            self.assertTrue(app.start_bulk_batch(new_rows(2), 2, "pw"))

        # The missing id's reservation is persisted before the new run exists.
        self.assertEqual(events[0], ("reserve", "a0072"))
        self.assertEqual([e[0] for e in events], ["reserve", "archive", "archive-draft", "save"])
        saved = app.bulk_state
        self.assertEqual(saved["slot_id"], "a0073")
        self.assertEqual(len(saved["queue"]), 2)
        # Old per-attempt failure/IP/slot state was cleared.
        self.assertEqual(saved["failures"], [])
        self.assertIsNone(saved["ip_attempt"])
        self.assertIsNone(saved["verified_ip"])
        self.assertIsNone(saved["baseline_ip"])
        self.assertTrue(any("a0072" in line and "start_reservation_reserved" in line
                            for line in app.logs))

    def test_start_uses_a_new_run_id_when_the_writer_claims_state(self):
        mine = "11111111-1111-1111-1111-111111111111"
        app = make_app(
            stage="stopped", slot_id="a0040", run_id="old-run", writer_id=mine,
            revision=4,
        )
        app.bulk_writer_id = mine
        claimed = []
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: {"id": "a0040"}),
            mock.patch.object(creator, "indices_used", lambda: {40}),
            mock.patch.object(creator, "archive_bulk_state",
                              side_effect=lambda _state, **_kw: Path_("st.json")),
            mock.patch.object(creator, "archive_bulk_draft", return_value=None),
            mock.patch.object(
                creator, "save_bulk_state_cas",
                side_effect=lambda state, revision, **_kw: claimed.append(
                    (dict(state), revision, dict(_kw))
                ) or state,
            ),
        ):
            self.assertTrue(app.start_bulk_batch(new_rows(1), 1, "pw"))

        saved, revision, kwargs = claimed[0]
        self.assertNotEqual(saved["run_id"], "old-run")
        self.assertEqual(saved["writer_id"], mine)
        self.assertEqual(revision, 4)
        self.assertTrue(kwargs["allow_writer_takeover"])

    def test_start_never_compares_new_rows_against_the_old_slot(self):
        # The old slot's saved birth date and username deliberately conflict
        # with the new editor rows; a fresh start must not consult them.
        app = make_app(
            stage="paused", slot_id="a0069",
            failure_phase="creation", failure_reason="Email field still invalid.",
        )
        rows = [
            {"birth_date": "05.05.1990", "swipes": 10, "delay_seconds": 2.0,
             "username": "FreshName"},
        ]
        with (
            mock.patch.object(
                creator, "get_slot",
                lambda _sid: {"id": "a0069", "reddit_birth_date": "01.01.2000",
                              "username": "OldName"},
            ),
            mock.patch.object(creator, "indices_used", lambda: {69}),
            mock.patch.object(creator, "retire_slot_id", lambda _sid: True),
            mock.patch.object(creator, "archive_bulk_state",
                              side_effect=lambda _state, **_kw: Path_("st.json")),
            mock.patch.object(creator, "archive_bulk_draft", return_value=None),
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
        ):
            app._check_bulk_resume_compatibility = (
                lambda _state: self.fail("start consulted resume compatibility")
            )
            self.assertTrue(app.start_bulk_batch(rows, 1, "pw"))

        self.assertEqual(app.bulk_state["queue"], rows)
        self.assertEqual(app.bulk_state["stage"], "ip_pending")

    def test_stale_draft_baseline_warning_does_not_block_start(self):
        app = make_app(stage="idle", remaining=0, queue=[], requested=0)
        app.bulk_editor_baseline = [
            {"birth_date": "09.09.1999", "swipes": 50, "delay_seconds": 5.0}
        ]
        app.bulk_draft_reconcile_error = "Existing queued rows changed since the draft."
        with (
            mock.patch.object(creator, "indices_used", lambda: {1}),
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
        ):
            self.assertTrue(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(app.bulk_state["queue"], new_rows(1))

    def test_start_refused_while_a_worker_is_alive(self):
        app = make_app(stage="stopped")
        app.worker = FakeWorker(True)
        with mock.patch.object(creator, "save_bulk_state",
                               side_effect=AssertionError("persisted")):
            self.assertFalse(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(app.starts, [])
        self.assertTrue(any("start_blocked_worker" in line for line in app.logs))

    def test_start_refused_during_in_flight_ip_action_logs_bounded_wait(self):
        app = make_app(stage="idle", remaining=0, queue=[], requested=0)
        app.pending_ip_action = {"guard_until": time.monotonic() + 240}
        with mock.patch.object(creator, "save_bulk_state",
                               side_effect=AssertionError("persisted")):
            self.assertFalse(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(app.starts, [])
        blocked = [line for line in app.logs if "start_blocked_ip_guard" in line]
        self.assertEqual(len(blocked), 1)
        self.assertIn("Bounded wait ~2", blocked[0])

    def test_start_refused_for_a_foreign_writer(self):
        app = make_app(
            stage="stopped",
            writer_id="22222222-2222-2222-2222-222222222222",
        )
        app.bulk_writer_id = "11111111-1111-1111-1111-111111111111"
        with mock.patch.object(creator, "save_bulk_state",
                               side_effect=AssertionError("persisted")):
            self.assertFalse(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(app.starts, [])
        self.assertTrue(any("start_blocked_foreign_writer" in line for line in app.logs))

    def test_start_refused_for_an_unstartable_stage(self):
        app = make_app(stage="creating", slot_id="a0047")
        with mock.patch.object(creator, "save_bulk_state",
                               side_effect=AssertionError("persisted")):
            self.assertFalse(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(app.starts, [])
        self.assertTrue(any("start_blocked_stage" in line for line in app.logs))

    def test_archive_failure_aborts_and_keeps_prior_state(self):
        app = make_app(stage="stopped", slot_id="a0047", queue=new_rows(1))
        original = dict(app.bulk_state)
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: {"id": "a0047"}),
            mock.patch.object(creator, "indices_used", lambda: {47}),
            mock.patch.object(creator, "archive_bulk_state",
                              side_effect=creator.BulkError("disk full")),
            mock.patch.object(creator, "save_bulk_state",
                              side_effect=AssertionError("persisted")),
        ):
            self.assertFalse(app.start_bulk_batch(new_rows(2), 2, "pw"))
        self.assertEqual(app.bulk_state, original)
        self.assertEqual(app.starts, [])
        self.assertTrue(any("start_archive_failed" in line for line in app.logs))

    def test_draft_archive_failure_aborts_and_keeps_prior_state(self):
        app = make_app(stage="stopped", slot_id="a0047")
        original = dict(app.bulk_state)
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: {"id": "a0047"}),
            mock.patch.object(creator, "indices_used", lambda: {47}),
            mock.patch.object(creator, "archive_bulk_state",
                              side_effect=lambda _state, **_kw: Path_("st.json")),
            mock.patch.object(creator, "archive_bulk_draft",
                              side_effect=creator.BulkError("draft locked")),
            mock.patch.object(creator, "save_bulk_state",
                              side_effect=AssertionError("persisted")),
        ):
            self.assertFalse(app.start_bulk_batch(new_rows(2), 2, "pw"))
        self.assertEqual(app.bulk_state, original)
        self.assertEqual(app.starts, [])
        self.assertTrue(any("start_draft_archive_failed" in line for line in app.logs))

    def test_reservation_failure_aborts_and_keeps_prior_state(self):
        app = make_app(stage="stopped", slot_id="a0072")
        original = dict(app.bulk_state)
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: None),
            mock.patch.object(creator, "indices_used", lambda: set(range(1, 72))),
            mock.patch.object(creator, "retire_slot_id",
                              side_effect=OSError("file locked")),
            mock.patch.object(creator, "save_bulk_state",
                              side_effect=AssertionError("persisted")),
        ):
            self.assertFalse(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(app.bulk_state, original)
        self.assertEqual(app.starts, [])
        self.assertTrue(any("start_reservation_failed" in line for line in app.logs))

    def test_persistence_failure_aborts_without_launching_a_worker(self):
        app = make_app(stage="stopped", slot_id="a0047")
        original = dict(app.bulk_state)
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: {"id": "a0047"}),
            mock.patch.object(creator, "indices_used", lambda: {47}),
            mock.patch.object(creator, "archive_bulk_state",
                              side_effect=lambda _state, **_kw: Path_("st.json")),
            mock.patch.object(creator, "archive_bulk_draft", return_value=None),
            mock.patch.object(creator, "save_bulk_state",
                              side_effect=creator.BulkError("cas mismatch")),
        ):
            self.assertFalse(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(app.bulk_state, original)
        self.assertEqual(app.starts, [])
        self.assertTrue(any("start_persist_failed" in line for line in app.logs))

    def test_invalid_rows_are_rejected_before_any_state_change(self):
        app = make_app(stage="stopped", slot_id="a0047")
        original = dict(app.bulk_state)
        with (
            mock.patch.object(creator, "get_slot",
                              side_effect=AssertionError("slot read")),
            mock.patch.object(creator, "archive_bulk_state",
                              side_effect=AssertionError("archived")),
            mock.patch.object(creator, "save_bulk_state",
                              side_effect=AssertionError("persisted")),
        ):
            self.assertRaises(
                creator.BulkError,
                app.start_bulk_batch, new_rows(1), 0, "pw",
            )
        self.assertEqual(app.bulk_state, original)
        self.assertEqual(app.starts, [])


class Path_:
    """Path-like stand-in that never touches the filesystem."""

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


class ResumeMissingSlotTests(unittest.TestCase):
    def test_resume_with_missing_worked_slot_points_to_start_batch(self):
        app = make_app(
            stage="creating", slot_id="a0072",
            verified_ip="198.51.100.7", verified_at_utc="2026-10-16T12:00:00Z",
        )
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: None),
            mock.patch.object(creator, "save_bulk_state",
                              side_effect=AssertionError("persisted")),
            mock.patch.object(creator, "mark_bulk_skipped",
                              side_effect=AssertionError("rebuilt")),
        ):
            self.assertFalse(app.retry_bulk_batch(password="pw"))

        self.assertEqual(app.starts, [])
        blocked = [line for line in app.logs if "resume_slot_missing" in line]
        self.assertEqual(len(blocked), 1)
        self.assertIn("Start Batch", blocked[0])

    def test_resume_of_paused_batch_with_missing_slot_also_points_to_start(self):
        app = make_app(
            stage="paused", slot_id="a0047",
            failure_phase="creation", failure_code="email_invalid",
            failure_reason="Email field still invalid.",
        )
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: None),
            mock.patch.object(creator, "save_bulk_state",
                              side_effect=AssertionError("persisted")),
        ):
            self.assertFalse(app.retry_bulk_batch(password="pw"))
        self.assertEqual(app.starts, [])
        self.assertTrue(any("resume_slot_missing" in line for line in app.logs))

    def test_resume_before_first_creation_needs_no_slot_row(self):
        # A fresh run reserves its slot id before the first account row exists;
        # Resume must still work in that window.
        app = make_app(stage="stopped", slot_id="a0073")
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: None),
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
        ):
            self.assertTrue(app.retry_bulk_batch(password="pw"))
        self.assertEqual(len(app.starts), 1)
        self.assertEqual(app.bulk_state["stage"], "ip_pending")

    def test_resume_with_existing_slot_row_still_works(self):
        app = make_app(
            stage="paused", slot_id="a0047",
            failure_phase="creation", failure_code="email_invalid",
            failure_reason="Email field still invalid.",
            verified_ip="198.51.100.47", verified_at_utc="2026-10-16T12:00:00Z",
        )
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: {"id": "a0047"}),
            mock.patch.object(creator, "prepare_bulk_retry",
                              side_effect=lambda state: dict(state, stage="retry_creation")),
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
        ):
            self.assertTrue(app.retry_bulk_batch(password="pw"))
        self.assertEqual(len(app.starts), 1)
        self.assertEqual(app.bulk_state["stage"], "retry_creation")


class StartButtonTests(unittest.TestCase):
    @staticmethod
    def editor(app, count_text="1"):
        app.bulk_count_var = Value(count_text)
        app.bulk_births_text = Value("01.01.2003")
        app.bulk_swipes_text = Value("10")
        app.bulk_delays_text = Value("2")
        app.bulk_usernames_text = Value("")
        app.pw = Value("offline-password")
        app.status = Value("")

    def test_first_start_shows_no_confirmation_dialog(self):
        app = make_app(queue=[], requested=0, remaining=0, stage="idle")
        self.editor(app)
        calls = []

        def start(items, requested, password, **_kwargs):
            calls.append((items, requested, password))
            return True

        app.start_bulk_batch = start
        with mock.patch.object(
            creator.messagebox, "askokcancel",
            side_effect=AssertionError("unexpected confirmation dialog"),
        ):
            app._on_start_bulk_batch()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 1)

    def test_superseded_start_asks_then_runs_fresh_rows(self):
        app = make_app(stage="paused", slot_id="a0069",
                       failure_phase="creation", failure_reason="bad email")
        self.editor(app, count_text="2")
        app.bulk_births_text = Value("01.01.2003\n02.02.2004")
        app.bulk_swipes_text = Value("10\n11")
        app.bulk_delays_text = Value("2\n3")
        calls = []
        app.start_bulk_batch = (
            lambda items, requested, password, **_kw: calls.append((items, requested))
            or True
        )
        with mock.patch.object(creator.messagebox, "askokcancel", return_value=True):
            app._on_start_bulk_batch()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 2)
        self.assertEqual(
            calls[0][0][0],
            {"birth_date": "01.01.2003", "swipes": 10, "delay_seconds": 2.0},
        )

    def test_failed_start_preserves_the_raw_editor_draft(self):
        app = make_app(stage="idle", remaining=0, queue=[], requested=0)
        self.editor(app, count_text="not-a-number")
        saved = []
        app._save_bulk_draft_now = lambda: saved.append(True) or True
        app._on_start_bulk_batch()
        self.assertEqual(saved, [True])
        self.assertEqual(app.status.value, "Batch not started — see Log")
        self.assertTrue(any(line.startswith("BATCH not started:")
                            for line in app.logs))

    def test_blocked_start_reports_the_logged_reason(self):
        app = make_app(stage="idle", remaining=0, queue=[], requested=0)
        app.worker = FakeWorker(True)
        self.editor(app)
        app._save_bulk_draft_now = lambda: True
        app._on_start_bulk_batch()
        self.assertEqual(app.status.value, "Batch not started — see Log")
        self.assertTrue(any("start_blocked_worker" in line for line in app.logs))


class ForeignWriterTakeoverTests(unittest.TestCase):
    """Stale-writer recovery for Start Batch, with ownership kept otherwise.

    A previous Creator leaves its ``writer_id`` on an otherwise empty idle
    state.  Start reloads the durable state, takes over only that provably
    clean idle batch, and still blocks real active owners or any change that
    slips in between the reload and the CAS write.
    """

    MINE = "11111111-1111-1111-1111-111111111111"
    STALE = "22222222-2222-2222-2222-222222222222"

    def stale_state(self, **overrides):
        """The exact clean-idle-but-stale-owner shape seen in production."""
        state = bulk_creation.empty_state()
        state.update(
            revision=37,
            run_id="d245460a-269b-448e-91ef-ea9c7e6290c5",
            writer_id=self.STALE,
            requested=3,
            remaining=0,
            stage="idle",
            queue=[],
            delete_containers=True,
            failures=[{
                "slot_id": "a0073",
                "item": {"birth_date": "07.01.2005", "swipes": 22, "delay_seconds": 3.6},
                "code": "creation_preserved_skipped",
                "reason": "Reddit not found.",
                "failed_at_utc": "2026-10-17T23:14:49.563099Z",
            }],
        )
        state.update(overrides)
        return state

    @staticmethod
    def cas_recorder(store):
        def _cas(state, revision, **kwargs):
            store.append((dict(state), revision, dict(kwargs)))
            return {**state, "revision": revision + 1}
        return _cas

    def test_stale_writer_clean_idle_is_safely_taken_over(self):
        app = make_app(queue=[], requested=0, remaining=0, stage="idle")
        app.bulk_writer_id = self.MINE
        app._latest_bulk_state = lambda: self.stale_state()
        cas = []
        with (
            mock.patch.object(creator, "indices_used", lambda: {1}),
            mock.patch.object(creator, "get_slot", lambda _sid: {"id": "a0001"}),
            mock.patch.object(creator, "archive_bulk_state",
                              side_effect=lambda _state, **_kw: Path_("st.json")),
            mock.patch.object(creator, "archive_bulk_draft", return_value=None),
            mock.patch.object(creator, "save_bulk_state_cas",
                              side_effect=self.cas_recorder(cas)),
        ):
            self.assertTrue(app.start_bulk_batch(new_rows(1), 1, "pw"))

        self.assertEqual(len(app.starts), 1)
        self.assertEqual(app.starts[0]["stage"], "ip_pending")
        self.assertEqual(app.starts[0]["writer_id"], self.MINE)
        state, revision, kwargs = cas[0]
        self.assertEqual(revision, 37)
        self.assertTrue(kwargs.get("allow_writer_takeover"))
        self.assertFalse(any("start_blocked_foreign_writer" in l for l in app.logs))

    def test_active_foreign_writer_is_still_blocked(self):
        app = make_app(queue=new_rows(1), requested=1, remaining=1,
                       stage="creating", slot_id="a0047")
        app.bulk_writer_id = self.MINE
        app._latest_bulk_state = lambda: dict(
            app.bulk_state, writer_id=self.STALE, revision=12)
        before = dict(app.bulk_state)
        with mock.patch.object(creator, "save_bulk_state_cas",
                               side_effect=AssertionError("persisted")):
            self.assertFalse(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(app.starts, [])
        self.assertEqual(app.bulk_state, before)
        self.assertTrue(any("start_blocked_foreign_writer" in l for l in app.logs))

    def test_idle_foreign_state_with_pending_queue_is_blocked(self):
        # idle stage is not enough: a non-empty queue is outstanding work.
        app = make_app(queue=[], requested=0, remaining=0, stage="idle")
        app.bulk_writer_id = self.MINE
        app._latest_bulk_state = lambda: dict(
            self.stale_state(), queue=new_rows(1), requested=1, remaining=1)
        with mock.patch.object(creator, "save_bulk_state_cas",
                               side_effect=AssertionError("persisted")):
            self.assertFalse(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(app.starts, [])
        self.assertTrue(any("start_blocked_foreign_writer" in l for l in app.logs))

    def test_idle_foreign_state_with_ip_evidence_is_blocked(self):
        app = make_app(queue=[], requested=0, remaining=0, stage="idle")
        app.bulk_writer_id = self.MINE
        app._latest_bulk_state = lambda: dict(
            self.stale_state(), baseline_ip="198.51.100.1", ip_attempt={"operation_id": "x"})
        with mock.patch.object(creator, "save_bulk_state_cas",
                               side_effect=AssertionError("persisted")):
            self.assertFalse(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(app.starts, [])
        self.assertTrue(any("start_blocked_foreign_writer" in l for l in app.logs))

    def test_revision_race_during_takeover_is_rejected_unchanged(self):
        app = make_app(queue=[], requested=0, remaining=0, stage="idle")
        app.bulk_writer_id = self.MINE
        app._latest_bulk_state = lambda: self.stale_state()
        with (
            mock.patch.object(creator, "indices_used", lambda: {1}),
            mock.patch.object(creator, "archive_bulk_state",
                              side_effect=lambda _state, **_kw: Path_("st.json")),
            mock.patch.object(creator, "archive_bulk_draft", return_value=None),
            mock.patch.object(creator, "save_bulk_state_cas",
                              side_effect=creator.BulkError("stale write rejected")),
        ):
            self.assertFalse(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(app.starts, [])
        self.assertEqual(app.bulk_state["queue"], [])
        self.assertEqual(app.bulk_state["writer_id"], self.STALE)
        self.assertTrue(any("start_persist_failed" in l for l in app.logs))

    def test_malformed_saved_state_is_rejected_unchanged(self):
        app = make_app(stage="stopped", slot_id="a0047")
        before = dict(app.bulk_state)

        def boom():
            raise creator.BulkError("Saved bulk configuration is malformed.")

        app._latest_bulk_state = boom
        with mock.patch.object(creator, "save_bulk_state",
                               side_effect=AssertionError("persisted")):
            self.assertFalse(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(app.starts, [])
        self.assertEqual(app.bulk_state, before)
        self.assertTrue(any("start_load_failed" in l for l in app.logs))

    def test_same_writer_idle_start_is_not_foreign(self):
        app = make_app(queue=[], requested=0, remaining=0, stage="idle")
        app.bulk_writer_id = self.MINE
        app._latest_bulk_state = lambda: self.stale_state(writer_id=self.MINE)
        cas = []
        with (
            mock.patch.object(creator, "indices_used", lambda: {1}),
            mock.patch.object(creator, "archive_bulk_state",
                              side_effect=lambda _state, **_kw: Path_("st.json")),
            mock.patch.object(creator, "archive_bulk_draft", return_value=None),
            mock.patch.object(creator, "save_bulk_state_cas",
                              side_effect=self.cas_recorder(cas)),
        ):
            self.assertTrue(app.start_bulk_batch(new_rows(1), 1, "pw"))
        self.assertEqual(len(app.starts), 1)
        self.assertEqual(app.starts[0]["writer_id"], self.MINE)
        self.assertFalse(any("start_blocked_foreign_writer" in l for l in app.logs))


if __name__ == "__main__":
    unittest.main()
