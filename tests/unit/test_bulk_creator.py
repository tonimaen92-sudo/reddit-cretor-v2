"""Offline Creator batch ordering, retry and restart tests."""

from __future__ import annotations

import threading
import tempfile
import unittest
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from friend_poster.apps import creator
from friend_poster.core import bulk_creation
from friend_poster.services import ip_change
from test_ip_change import Clock
from friend_poster.core.bulk_creation import empty_state


def verified_result(old, new):
    op = "1" * 32
    baseline = {"operation_id": op, "check_sequence": 1, "phase": "baseline",
                "ip": old, "observed_at_utc": "2026-10-16T12:00:00Z"}
    pair = [{"operation_id": op, "check_sequence": seq, "phase": "observation",
             "ip": new, "observed_at_utc": f"2026-10-16T12:00:0{seq}Z"} for seq in (2, 3)]
    return {"outcome": "verified", "old_ip": old, "new_ip": new, "ip": new,
            "verification_evidence": {"baseline": baseline, "confirmations": pair, "confirmation_floor": 1}}


def trigger_checkpoint(old):
    result = verified_result(old, "198.51.100.2")
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=180)).isoformat().replace("+00:00", "Z")
    return {"baseline_ip": old, "deadline_utc": deadline,
            "attempt": {"operation_id": "1" * 32, "proxy_scope": "a" * 64,
                        "verification_target": creator.DEFAULT_PUBLIC_IP_URL,
                        "baseline": result["verification_evidence"]["baseline"], "check_sequence": 1,
                        "candidate": None, "confirmation_floor": 1, "verification_evidence": None,
                        "observation_deadline_utc": deadline}}


class Stop:
    def __init__(self):
        self.flag = False
        self.waits = []
    def is_set(self):
        return self.flag
    def set(self):
        self.flag = True
    def clear(self):
        self.flag = False
    def wait(self, seconds):
        self.waits.append(seconds)
        return self.flag


class Root:
    def after(self, _delay, callback):
        callback()


class BatchCreatorTests(unittest.TestCase):
    @staticmethod
    def parse_birth(value):
        return datetime.strptime(value, "%d.%m.%Y").date()

    def test_parse_queue_uses_requested_prefix_and_ignores_surplus(self):
        requested, queue = bulk_creation.parse_queue(
            "2",
            "01.01.2000\n02.02.2001\nnot-a-date",
            "10\n11\nnot-a-number\n99",
            "2.5\n3.5\nnot-a-delay",
            self.parse_birth,
            usernames_text="FirstName\nSecondName\nFirstName",
        )
        self.assertEqual(requested, 2)
        self.assertEqual(len(queue), 2)
        self.assertEqual([row["username"] for row in queue], ["FirstName", "SecondName"])

    def test_parse_queue_allows_different_surplus_column_lengths(self):
        requested, queue = bulk_creation.parse_queue(
            "2",
            "01.01.2000\n02.02.2001\n03.03.2002",
            "10\n11\n12\n13",
            "2.5\n3.5\n4.5",
            self.parse_birth,
            usernames_text="",
        )
        self.assertEqual(requested, 2)
        self.assertEqual(len(queue), 2)
        self.assertNotIn("username", queue[0])

    def test_parse_queue_rejects_required_column_shorter_than_requested(self):
        with self.assertRaisesRegex(bulk_creation.BulkError, "at least 3 row"):
            bulk_creation.parse_queue(
                "3", "01.01.2000\n02.02.2001\n03.03.2002",
                "10\n11", "2.5\n3.5\n4.5", self.parse_birth,
            )

    def test_parse_queue_short_username_column_uses_suggestion_for_missing_rows(self):
        _, queue = bulk_creation.parse_queue(
            "3", "01.01.2000\n02.02.2001\n03.03.2002",
            "10\n11\n12", "2.5\n3.5\n4.5", self.parse_birth,
            usernames_text="FirstName\nSecondName",
        )
        self.assertEqual(queue[0]["username"], "FirstName")
        self.assertEqual(queue[1]["username"], "SecondName")
        self.assertNotIn("username", queue[2])

    def test_parse_queue_invalid_or_repeated_username_uses_suggestion(self):
        _, queue = bulk_creation.parse_queue(
            "3", "01.01.2000\n02.02.2001\n03.03.2002",
            "10\n11\n12", "2.5\n3.5\n4.5", self.parse_birth,
            usernames_text="FirstName\nbad name\nFirstName",
        )
        self.assertEqual(queue[0]["username"], "FirstName")
        self.assertNotIn("username", queue[1])
        self.assertNotIn("username", queue[2])

    def app(self, count=2):
        app = creator.App.__new__(creator.App)
        app.root = Root()
        app.stop = Stop()
        app.closing = False
        app.bulk_mutex = threading.RLock()
        app.bulk_password = "offline-password"
        app.macrodroid_webhook = "https://trigger.invalid/private"
        app.falcon_proxy_url = "socks5h://user:pass@proxy.invalid:1080"
        app.pending_ip_action = None
        app.ip_reset_busy = False
        app.worker = None
        app.log = lambda _message: None
        app._after_job = lambda: None
        app._refresh_macrodroid_setup = lambda: True
        app._bulk_proxy_scope = lambda: "a" * 64
        app._record_bulk_ip = lambda result, _observations: result["verification_evidence"]["confirmations"][-1]["observed_at_utc"]
        app._pause_bulk = lambda phase, reason: self.fail(f"unexpected pause: {phase}: {reason}")
        app.bulk_state = empty_state()
        app.bulk_state.update(
            queue=[{"birth_date": "01.01.2000", "swipes": 10 + i, "delay_seconds": 2.0 + i}
                   for i in range(count)],
            requested=count, remaining=count, stage="ip_pending",
        )
        return app

    def test_startup_resume_flag_calls_existing_retry_method(self):
        calls = []
        class FakeApp:
            def __init__(self):
                self.root = Root()
            def retry_bulk_batch(self):
                calls.append("retry")
            def run(self):
                calls.append("run")
        with (
            mock.patch.object(creator, "_acquire_creator_single_instance", return_value=True),
            mock.patch.object(creator, "App", FakeApp),
        ):
            self.assertEqual(creator.main(["--resume-batch"]), 0)
        self.assertEqual(calls, ["retry", "run"])

    def test_loaded_draft_drops_completed_head_and_keeps_uncommitted_tail(self):
        app = self.app(1)
        first = {"birth_date": "01.01.2003", "swipes": 10, "delay_seconds": 2.0, "username": "FirstName"}
        current = {"birth_date": "02.02.2004", "swipes": 11, "delay_seconds": 3.0, "username": "SecondName"}
        addition = {"birth_date": "03.03.2005", "swipes": 12, "delay_seconds": 4.0, "username": "ThirdName"}
        app.bulk_state["queue"] = [current]
        app.bulk_state["requested"] = 2
        app.bulk_state["remaining"] = 1
        app.bulk_draft_reconcile_error = ""
        app.bulk_editor_baseline = []
        draft = bulk_creation.empty_draft()
        draft.update(
            count_text="3",
            births_text="01.01.2003\n02.02.2004\n03.03.2005",
            swipes_text="10\n11\n12",
            delays_text="2\n3\n4",
            usernames_text="FirstName\nSecondName\nThirdName",
            baseline_queue=[first, current],
        )
        restored = creator.App._prepare_loaded_bulk_draft(app, draft)
        self.assertEqual(restored["births_text"], "02.02.2004\n03.03.2005")
        self.assertEqual(restored["usernames_text"], "SecondName\nThirdName")
        self.assertEqual(restored["count_text"], "2")
        self.assertEqual(restored["baseline_queue"], [current])
        self.assertEqual(app.bulk_editor_baseline, [current])
        self.assertEqual(app.bulk_draft_reconcile_error, "")

    def test_incomplete_draft_is_restored_exactly_and_blocks_extension(self):
        app = self.app(1)
        app.bulk_draft_reconcile_error = ""
        app.bulk_editor_baseline = []
        draft = bulk_creation.empty_draft()
        draft.update(
            count_text="2",
            births_text="01.01.2003\n02.02.",
            swipes_text="10\n",
            delays_text="2\n3",
            usernames_text="FirstName\nSecondName",
            baseline_queue=[dict(app.bulk_state["queue"][0])],
        )
        restored = creator.App._prepare_loaded_bulk_draft(app, draft)
        self.assertEqual(restored, draft)
        self.assertIn("incomplete or invalid", app.bulk_draft_reconcile_error)

    def test_completed_batch_preserves_raw_next_batch_draft(self):
        app = self.app(1)
        app.bulk_state.update(stage="idle", remaining=0)
        app.bulk_draft_reconcile_error = ""
        app.bulk_editor_baseline = []
        draft = bulk_creation.empty_draft()
        draft.update(
            count_text="2",
            births_text="01.01.2003\n02.02.2004\n03.03.2005",
            swipes_text="10\n11\n12\n13",
            delays_text="2\n3\n4",
            usernames_text="FirstName\nSecondName\nThirdName\nFourthName\nFifthName",
            baseline_queue=[dict(app.bulk_state["queue"][0])],
        )
        restored = creator.App._prepare_loaded_bulk_draft(app, draft)
        self.assertEqual(restored["count_text"], "2")
        self.assertEqual(restored["births_text"], draft["births_text"])
        self.assertEqual(restored["swipes_text"], draft["swipes_text"])
        self.assertEqual(restored["usernames_text"], draft["usernames_text"])
        self.assertEqual(restored["baseline_queue"], [])
        self.assertEqual(app.bulk_editor_baseline, [])

    def test_active_queue_prefix_preserves_unequal_raw_surplus(self):
        app = self.app(2)
        current = [
            {"birth_date": "01.01.2003", "swipes": 10, "delay_seconds": 2.0, "username": "FirstName"},
            {"birth_date": "02.02.2004", "swipes": 11, "delay_seconds": 3.0, "username": "SecondName"},
        ]
        app.bulk_state.update(queue=current, requested=2, remaining=2, stage="ip_pending")
        app.bulk_draft_reconcile_error = ""
        app.bulk_editor_baseline = []
        draft = bulk_creation.empty_draft()
        draft.update(
            count_text="2",
            births_text="01.01.2003\n02.02.2004\n03.03.2005",
            swipes_text="10\n11\n12\n13",
            delays_text="2\n3\n4",
            usernames_text="FirstName\nSecondName\nThirdName\nFourthName",
            baseline_queue=[current[0]],
        )
        restored = creator.App._prepare_loaded_bulk_draft(app, draft)
        self.assertEqual(restored["count_text"], "2")
        self.assertEqual(restored["swipes_text"], draft["swipes_text"])
        self.assertEqual(restored["usernames_text"], draft["usernames_text"])
        self.assertEqual(restored["baseline_queue"], current)
        self.assertEqual(app.bulk_editor_baseline, current)

    def test_loaded_legacy_editor_merges_hidden_unused_queue_tail_once(self):
        app = self.app(1)
        shown = [
            {"birth_date": f"0{i}.01.2003", "swipes": 10 + i, "delay_seconds": 2.0, "username": f"Shown{i}"}
            for i in range(1, 4)
        ]
        hidden = {"birth_date": "04.01.2003", "swipes": 14, "delay_seconds": 2.0, "username": "Hidden4"}
        addition = {"birth_date": "05.01.2003", "swipes": 15, "delay_seconds": 2.0, "username": "Added5"}
        app.bulk_state.update(queue=[shown[2], hidden], requested=3, remaining=1)
        app.bulk_draft_reconcile_error = ""
        app.bulk_editor_baseline = []
        draft = creator.App._bulk_rows_payload([*shown, addition], count_text="4")
        draft["baseline_queue"] = shown
        restored = creator.App._prepare_loaded_bulk_draft(app, draft)
        rows = creator.App._parse_bulk_text_rows(
            restored["births_text"], restored["swipes_text"],
            restored["delays_text"], restored["usernames_text"],
        )
        self.assertEqual(rows, [shown[2], hidden, addition])
        self.assertEqual(restored["count_text"], "3")
        self.assertEqual(app.bulk_editor_baseline, [shown[2], hidden])

    def test_two_accounts_run_strictly_in_sequence_with_post_completion_gap(self):
        app = self.app()
        app.bulk_state["queue"][0]["username"] = "BlueHarbor_01"
        app.bulk_state["queue"][1]["username"] = "QuietMoon_02"
        events = []
        complete = {}
        serial = [0]
        def slot_and_entry(state):
            if not state["slot_id"]:
                serial[0] += 1
                changed = dict(state, slot_id=f"a{serial[0]:04d}")
                app._persist_bulk(changed)
            return app.bulk_state["slot_id"], app.bulk_state["queue"][0], date(2000, 1, 1)
        app._bulk_slot_and_entry = slot_and_entry
        def ip_change(_webhook, _proxy, **kwargs):
            events.append("reset")
            self.assertTrue(kwargs["require_baseline"])
            self.assertGreaterEqual(kwargs["baseline_cycles"], 2)
            old_ip = "198.51.100.1"
            new_ip = f"198.51.100.{serial[0] + 1}"
            kwargs["before_trigger"](trigger_checkpoint(old_ip))
            return verified_result(old_ip, new_ip)
        def pipeline(_password, sid, _birth, swipes, **kwargs):
            self.assertFalse("fixed_delay_seconds" in kwargs)
            self.assertEqual(
                kwargs["requested_username"],
                "BlueHarbor_01" if sid == "a0001" else "QuietMoon_02",
            )
            events.append(("create", sid, swipes))
            complete[sid] = True
            events.append("creation_complete")
            return True
        app._run_creation_pipeline = pipeline
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "run_macrodroid_change", ip_change),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", lambda sid, ip, stamp: events.append(("slot_ip", sid, ip))),
            mock.patch.object(creator, "get_slot", lambda sid: {
                "complete": complete.get(sid, False),
                "username": ("BlueHarbor_01" if sid == "a0001" else "QuietMoon_02") if complete.get(sid, False) else "",
            }),
            mock.patch.object(creator, "_pipeline_slot_complete", lambda row: bool(row and row.get("complete"))),
        ):
            creator.App._run_bulk_batch(app)
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(len(app.bulk_state["queue"]), 0)
        self.assertEqual(events.count("reset"), 2)
        self.assertEqual(events[0], "reset")
        self.assertEqual(events[1], ("slot_ip", "a0001", "198.51.100.2"))
        self.assertEqual(events[2], ("create", "a0001", 10))
        self.assertEqual(events[4], "reset")
        self.assertEqual(events[5], ("slot_ip", "a0002", "198.51.100.3"))
        self.assertEqual(events[6], ("create", "a0002", 11))
        self.assertEqual(len(app.stop.waits), 1)
        self.assertGreater(app.stop.waits[0], 1.5)
        self.assertLessEqual(app.stop.waits[0], 2.0)

    def test_saved_username_mismatch_prevents_reuse(self):
        app = self.app(1)
        app.bulk_state["queue"][0]["username"] = "RequestedName"
        app.bulk_state["slot_id"] = "a0001"
        with mock.patch.object(creator, "get_slot", return_value={"id": "a0001", "username": "DifferentName", "reddit_birth_date": "01.01.2000"}):
            with self.assertRaisesRegex(creator.BulkError, "different|requests"):
                app._check_bulk_resume_compatibility(app.bulk_state)

    def test_pipeline_passes_requested_name_only_for_named_batch(self):
        app = self.app(1)
        app.status = mock.Mock()
        slot = {
            "id": "a0001", "reddit_birth_date": "01.01.2000",
            "ghost": True, "ghost_container_state": "ready",
            "ghost_container_instance": "a0001", "email": "test@icloud.com",
            "username": "", "password": "", "reddit": False,
        }
        calls = []
        phone = mock.Mock()
        app._connect = lambda _password: (phone, object())

        def create(*_args, **kwargs):
            calls.append(kwargs)
            slot.update(username=kwargs.get("requested_username") or "SuggestedName", password="saved-password", reddit=True)

        with (
            mock.patch.object(creator, "get_slot", return_value=slot),
            mock.patch.object(creator, "ghost_container_state", return_value="ready"),
            mock.patch.object(creator, "run_create_reddit", side_effect=create),
            mock.patch.object(creator, "creation_feed_swipe_progress", return_value=(10, 10)),
            mock.patch.object(creator, "slot_reddit_ready", return_value=True),
        ):
            self.assertTrue(app._run_creation_pipeline(
                "offline-password", "a0001", date(2000, 1, 1), 10,
                requested_username="ChosenName", manage_worker=False,
            ))
        self.assertEqual(calls[0]["requested_username"], "ChosenName")
        phone.close.assert_called_once()

    def test_ten_items_are_ip_first_and_skip_the_final_delay(self):
        app = self.app(10)
        app.bulk_state["queue"] = [
            {"birth_date": "01.01.2000", "swipes": 10, "delay_seconds": 2.0}
            for _ in range(10)
        ]
        events = []
        complete = set()
        serial = [0]

        def slot_and_entry(state):
            if not state["slot_id"]:
                serial[0] += 1
                app._persist_bulk(dict(state, slot_id=f"a{serial[0]:04d}"))
            return app.bulk_state["slot_id"], app.bulk_state["queue"][0], date(2000, 1, 1)

        def rotate(_webhook, _proxy, **kwargs):
            sid = app.bulk_state["slot_id"]
            events.append(("ip", sid))
            kwargs["before_trigger"](trigger_checkpoint("198.51.100.1"))
            return verified_result("198.51.100.1", f"198.51.100.{serial[0] + 1}")

        def create(_password, sid, _birth, _swipes, **_kwargs):
            events.append(("create", sid))
            complete.add(sid)
            return True

        app._bulk_slot_and_entry = slot_and_entry
        app._run_creation_pipeline = create
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "run_macrodroid_change", rotate),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", lambda *_args: None),
            mock.patch.object(creator, "get_slot", lambda sid: {"complete": sid in complete}),
            mock.patch.object(creator, "_pipeline_slot_complete", lambda row: bool(row and row.get("complete"))),
        ):
            app._run_bulk_batch()
        self.assertEqual(app.bulk_state["stage"], "idle")
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(len(app.stop.waits), 9)
        for index in range(10):
            sid = f"a{index + 1:04d}"
            self.assertEqual(events[index * 2:index * 2 + 2], [("ip", sid), ("create", sid)])

    def test_reopen_can_resume_an_active_saved_stage(self):
        app = self.app(1)
        app.bulk_state.update(stage="may_have_triggered", slot_id="a0001")
        starts = []
        app._start_bulk_worker = lambda: starts.append(True)
        with mock.patch.object(creator, "save_bulk_state", lambda _state: None):
            self.assertTrue(app.retry_bulk_batch(password="offline-password"))
        self.assertEqual(starts, [True])
        self.assertEqual(app.bulk_state["stage"], "may_have_triggered")

    def test_backend_start_api_persists_before_starting_worker(self):
        app = self.app(1)
        app.bulk_state = empty_state()
        starts = []
        app._check_bulk_resume_compatibility = lambda _state: None
        app._start_bulk_worker = lambda: starts.append(dict(app.bulk_state))
        item = {"birth_date": "01.01.2000", "swipes": 12, "delay_seconds": 2.5}
        with mock.patch.object(creator, "save_bulk_state", lambda _state: None):
            started = app.start_bulk_batch([item], 1, "offline-password")
        self.assertTrue(started)
        self.assertEqual(starts[0]["stage"], "ip_pending")
        self.assertEqual(starts[0]["queue"], [item])
        self.assertEqual(starts[0]["remaining"], 1)

    def test_start_reconciles_stopped_head_before_three_new_items(self):
        app = self.app(1)
        old_head = {"birth_date": "20.04.2005", "swipes": 27, "delay_seconds": 3.0}
        old_tail = {"birth_date": "11.02.2004", "swipes": 22, "delay_seconds": 4.0}
        app.bulk_state.update(
            queue=[old_head, old_tail],
            requested=2,
            remaining=2,
            stage="stopped",
            slot_id="a0047",
            baseline_ip="31.217.14.3",
            verified_ip="31.217.18.190",
            verified_at_utc="2026-10-16T18:45:53Z",
        )
        new_items = [
            {"birth_date": "01.01.2001", "swipes": 10, "delay_seconds": 2.0},
            {"birth_date": "02.02.2002", "swipes": 11, "delay_seconds": 3.0},
            {"birth_date": "03.03.2003", "swipes": 12, "delay_seconds": 4.0},
        ]
        events = []
        app._start_bulk_worker = lambda: events.append(("start", dict(app.bulk_state)))
        app._show_bulk_queue = lambda state: events.append(("show", list(state["queue"])))
        with (
            mock.patch.object(
                creator, "get_slot",
                return_value={"id": "a0047", "reddit_birth_date": "20.04.2005"},
            ),
            mock.patch.object(
                creator,
                "archive_bulk_state",
                side_effect=lambda state: events.append(("archive", list(state["queue"])))
                or Path("bulk_creation.pre-reconcile-test.json"),
            ),
            mock.patch.object(
                creator,
                "save_bulk_state",
                side_effect=lambda state: events.append(("save", list(state["queue"]))),
            ),
        ):
            self.assertTrue(app.start_bulk_batch(new_items, 3, "offline-password"))

        self.assertEqual(app.bulk_state["queue"], [old_head, *new_items])
        self.assertEqual(app.bulk_state["stage"], "retry_creation")
        self.assertEqual(app.bulk_state["remaining"], 4)
        self.assertEqual(app.bulk_state["verified_ip"], "31.217.18.190")
        self.assertEqual(events[0], ("archive", [old_head, old_tail]))
        self.assertEqual(events[1], ("save", [old_head, *new_items]))

    def test_reconciliation_preview_cancel_changes_nothing(self):
        app = self.app(1)
        original = dict(app.bulk_state)

        class Value:
            def __init__(self, value):
                self.value = value
            def get(self, *_args):
                return self.value
            def set(self, value):
                self.value = value

        app.bulk_count_var = Value("1")
        app.bulk_births_text = Value("01.01.2003")
        app.bulk_swipes_text = Value("10")
        app.bulk_delays_text = Value("2")
        app.bulk_usernames_text = Value("")
        app.pw = Value("offline-password")
        app.status = Value("")
        app.root = object()
        app._plan_bulk_start = lambda *_args: (
            {},
            {
                "slot_id": "a0047",
                "birth_date": "20.04.2005",
                "swipes": 27,
                "delay_seconds": 3.0,
                "old_tail_count": 1,
            },
        )
        app.start_bulk_batch = lambda *_args: self.fail("cancelled preview started")
        with mock.patch.object(creator.messagebox, "askokcancel", return_value=False):
            app._on_start_bulk_batch()
        self.assertEqual(app.bulk_state, original)
        self.assertEqual(app.status.value, "Batch start cancelled")

    def test_mid_reset_restart_observes_without_new_webhook(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="may_have_triggered", slot_id="a0001", baseline_ip="198.51.100.1",
            trigger_deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=180)).isoformat().replace("+00:00", "Z"),
        )
        app._bulk_slot_and_entry = lambda state: ("a0001", state["queue"][0], date(2000, 1, 1))
        created = []
        app._run_creation_pipeline = lambda *_args, **_kwargs: created.append(True) or True
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "run_macrodroid_change", side_effect=AssertionError("duplicate webhook")),
            mock.patch.object(creator, "observe_proxy_without_trigger", return_value=verified_result("198.51.100.1", "198.51.100.2")),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", lambda *_args: None),
            mock.patch.object(creator, "get_slot", lambda _sid: {"complete": bool(created)}),
            mock.patch.object(creator, "_pipeline_slot_complete", lambda row: bool(row and row.get("complete"))),
        ):
            creator.App._run_bulk_batch(app)
        self.assertEqual(created, [True])
        self.assertEqual(app.bulk_state["remaining"], 0)

    def test_creation_retry_rechecks_same_ip_without_reset(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="retry_creation", slot_id="a0001", verified_ip="198.51.100.9",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._bulk_slot_and_entry = lambda state: ("a0001", state["queue"][0], date(2000, 1, 1))
        calls = []
        app._run_creation_pipeline = lambda *_args, **_kwargs: calls.append("creation") or True
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "run_macrodroid_change", side_effect=AssertionError("reset on retry")),
            mock.patch.object(creator, "observe_proxy_without_trigger", side_effect=lambda *a, **k: calls.append(("recheck", k["require_change"])) or verified_result("198.51.100.9", "198.51.100.9")),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", lambda *_args: None),
            mock.patch.object(creator, "get_slot", lambda _sid: {"complete": bool(calls and calls[-1] == "creation")}),
            mock.patch.object(creator, "_pipeline_slot_complete", lambda row: bool(row and row.get("complete"))),
        ):
            creator.App._run_bulk_batch(app)
        self.assertEqual(calls, [("recheck", False), "creation"])

    def test_creation_pause_uses_exact_pipeline_failure_reason(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="retry_creation", slot_id="a0047", verified_ip="198.51.100.9",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._bulk_slot_and_entry = lambda state: (
            "a0047", state["queue"][0], date(2005, 4, 20)
        )
        pauses = []
        app._pause_bulk = lambda phase, reason: pauses.append((phase, reason))

        def rejected(*_args, **_kwargs):
            app._last_pipeline_failure_reason = (
                "Reddit returned 'Sorry, please try again later.' "
                "Saved password preserved; no automatic resubmit."
            )
            return False

        app._run_creation_pipeline = rejected
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator, "run_macrodroid_change",
                side_effect=AssertionError("reset on creation retry"),
            ),
            mock.patch.object(
                creator, "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.9", "198.51.100.9"),
            ),
            mock.patch.object(creator, "get_slot", return_value={"reddit": False}),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(len(pauses), 1)
        self.assertEqual(pauses[0][0], "creation")
        self.assertIn("Sorry, please try again later", pauses[0][1])
        self.assertEqual(app.bulk_state["remaining"], 1)

    def test_exhausted_a0049_is_recorded_and_a0050_gets_next_row_and_fresh_ip(self):
        app = self.app(2)
        app.bulk_state["queue"][0]["username"] = "QuietMoon49"
        app.bulk_state["queue"][1]["username"] = "QuietMoon50"
        app.bulk_state.update(
            stage="retry_creation",
            slot_id="a0049",
            verified_ip="198.51.100.49",
            verified_at_utc="2026-10-16T21:28:00Z",
        )
        completed = set()
        creation_calls = []
        attached_ips = []

        def slot_and_entry(state):
            if not state.get("slot_id"):
                changed = dict(state, slot_id="a0050")
                app._persist_bulk(changed)
                state = dict(app.bulk_state)
            return state["slot_id"], state["queue"][0], date(2000, 1, 1)

        def pipeline(_password, sid, _birth, _swipes, **kwargs):
            creation_calls.append((sid, kwargs.get("requested_username")))
            if sid == "a0049":
                app._last_pipeline_failure_code = (
                    creator.EmailReplacementExhausted.failure_code
                )
                app._last_pipeline_failure_reason = "Four aliases timed out."
                return False
            completed.add(sid)
            return True

        app._bulk_slot_and_entry = slot_and_entry
        app._run_creation_pipeline = pipeline
        app._run_bulk_ip_check = lambda _state: (
            verified_result("198.51.100.49", "198.51.100.50"),
            [],
        )

        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.49", "198.51.100.49"),
            ),
            mock.patch.object(
                creator,
                "set_slot_falcon_proxy_ip",
                side_effect=lambda sid, ip, _at: attached_ips.append((sid, ip)),
            ),
            mock.patch.object(
                creator, "mark_creation_terminal_failure", return_value={}
            ) as terminal,
            mock.patch.object(
                creator,
                "get_slot",
                side_effect=lambda sid: {
                    "complete": sid in completed,
                    "username": "QuietMoon50" if sid in completed else "",
                },
            ),
            mock.patch.object(
                creator,
                "_pipeline_slot_complete",
                side_effect=lambda row: bool(row and row.get("complete")),
            ),
        ):
            creator.App._run_bulk_batch(app)

        terminal.assert_called_once()
        self.assertEqual(
            creation_calls,
            [("a0049", "QuietMoon49"), ("a0050", "QuietMoon50")],
        )
        self.assertEqual(attached_ips, [("a0050", "198.51.100.50")])
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(app.bulk_state["stage"], "idle")
        self.assertEqual(app.bulk_state["failures"][0]["slot_id"], "a0049")

    def test_terminal_newest_slot_is_not_reused(self):
        sid, row, is_new = creator._resolve_creation_pipeline_target(
            [{
                "id": "a0049",
                "reddit": False,
                "creation_terminal_failure": {"code": "reddit_email_replacements_exhausted"},
            }],
            set(),
        )
        self.assertEqual((sid, row, is_new), ("a0050", None, True))

    def test_success_label_without_address_evidence_does_not_start_creation(self):
        app = self.app(1)
        app._bulk_slot_and_entry = lambda state: ("a0001", state["queue"][0], date(2000, 1, 1))
        pauses = []
        app._pause_bulk = lambda phase, reason: pauses.append((phase, reason))
        def ip_change(_webhook, _proxy, **kwargs):
            kwargs["before_trigger"](trigger_checkpoint("198.51.100.1"))
            return {"outcome": "verified", "old_ip": "198.51.100.1", "new_ip": "198.51.100.2"}
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "run_macrodroid_change", ip_change),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", side_effect=AssertionError("IP attached without evidence")),
        ):
            creator.App._run_bulk_batch(app)
        self.assertEqual(pauses[0][0], "ip")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_missing_baseline_retry_pauses_without_reset_or_creation(self):
        app = self.app(1)
        app.bulk_state.update(stage="retry_ip", slot_id="a0001", baseline_ip=None)
        app._bulk_slot_and_entry = lambda state: ("a0001", state["queue"][0], date(2000, 1, 1))
        pauses = []
        app._pause_bulk = lambda phase, reason: pauses.append((phase, reason))
        with (
            mock.patch.object(creator, "run_macrodroid_change", side_effect=AssertionError("duplicate reset")),
            mock.patch.object(creator, "observe_proxy_without_trigger", side_effect=AssertionError("no comparison IP")),
        ):
            creator.App._run_bulk_batch(app)
        self.assertEqual(pauses[0][0], "ip")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_completed_slot_reconciles_queue_without_reset(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="creating", slot_id="a0001", verified_ip="198.51.100.2",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._bulk_slot_and_entry = lambda state: ("a0001", state["queue"][0], date(2000, 1, 1))
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", lambda _sid: {"complete": True}),
            mock.patch.object(creator, "_pipeline_slot_complete", lambda row: row["complete"]),
            mock.patch.object(creator, "run_macrodroid_change", side_effect=AssertionError("duplicate reset")),
            mock.patch.object(creator, "observe_proxy_without_trigger", side_effect=AssertionError("completed slot rechecked")),
        ):
            creator.App._run_bulk_batch(app)
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(app.bulk_state["queue"], [])

    def test_start_preflight_rejects_incompatible_unfinished_slot(self):
        app = self.app(1)
        with (
            mock.patch.object(creator, "load_slots", return_value=[{"name": "a0001", "reddit_birth_date": "02.02.2000"}]),
            mock.patch.object(creator, "retired_indices", return_value=set()),
            mock.patch.object(creator, "_resolve_creation_pipeline_target", return_value=("a0001", {"reddit_birth_date": "02.02.2000"}, False)),
        ):
            with self.assertRaisesRegex(creator.BulkError, "birth date"):
                app._check_bulk_resume_compatibility(app.bulk_state)

    def test_failed_same_ip_retry_pauses_without_creation_or_reset(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="retry_creation", slot_id="a0001", verified_ip="198.51.100.9",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._bulk_slot_and_entry = lambda state: ("a0001", state["queue"][0], date(2000, 1, 1))
        pauses = []
        app._pause_bulk = lambda phase, reason: pauses.append((phase, reason))
        with (
            mock.patch.object(creator, "run_macrodroid_change", side_effect=AssertionError("duplicate reset")),
            mock.patch.object(creator, "observe_proxy_without_trigger", return_value={"outcome": "verification_failed", "ip": ""}),
            mock.patch.object(creator, "get_slot", lambda _sid: {"complete": False}),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", side_effect=AssertionError("unverified IP assigned")),
        ):
            creator.App._run_bulk_batch(app)
        self.assertEqual(pauses[0][0], "creation")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_stop_persists_stopped_stage_and_retains_queue(self):
        app = self.app(1)
        app._sync_row_buttons = lambda: None
        with mock.patch.object(creator, "save_bulk_state", lambda _state: None):
            app._stop_bulk_batch()
        self.assertEqual(app.bulk_state["stage"], "stopped")
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(len(app.bulk_state["queue"]), 1)
        self.assertTrue(app.stop.is_set())

    def test_stopped_uncertain_reset_resumes_with_read_only_window(self):
        app = self.app(1)
        checkpoint = trigger_checkpoint("31.217.20.252")
        attempt = dict(checkpoint["attempt"], observation_deadline_utc="2000-01-01T00:00:00Z")
        app.bulk_state.update(
            stage="stopped", slot_id="a0001", baseline_ip=checkpoint["baseline_ip"],
            trigger_deadline_utc=checkpoint["deadline_utc"], ip_attempt=attempt,
        )
        starts = []
        app._start_bulk_worker = lambda: starts.append(dict(app.bulk_state))
        with mock.patch.object(creator, "save_bulk_state", lambda _state: None):
            self.assertTrue(app.retry_bulk_batch(password="offline-password"))
        self.assertEqual(len(starts), 1)
        self.assertEqual(app.bulk_state["stage"], "retry_ip")
        self.assertGreater(
            app.bulk_state["ip_attempt"]["observation_deadline_utc"],
            "2000-01-01T00:00:00Z",
        )
        self.assertEqual(app.bulk_state["ip_attempt"]["confirmation_floor"], 1)

    def test_real_verifier_outage_pattern_hands_off_once_and_saves_actual_time(self):
        app = self.app(1)
        app.bulk_state["slot_id"] = "a0046"
        app._bulk_slot_and_entry = lambda state: ("a0046", state["queue"][0], date(2000, 1, 1))
        clock = Clock()
        offline = ip_change.IpChangeError("temporary", category="timeout")
        values = iter(["31.217.20.252", "31.217.14.168", offline, offline, "31.217.14.168"])
        events = []
        def fetch(*_args):
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value
        def run(webhook, proxy, **kwargs):
            return ip_change.run_macrodroid_change(
                webhook, proxy, **kwargs, fetcher=fetch, monotonic=clock.monotonic,
                now=clock.now, waiter=lambda seconds: clock.advance(seconds) or False,
                triggerer=lambda *_args: events.append("trigger") or {"acknowledged": True},
            )
        app._run_creation_pipeline = lambda *_a, **_k: events.append("create-and-close") or True
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(creator, "save_bulk_state", lambda state: bulk_creation.save_state(state, Path(folder) / "queue.json")),
            mock.patch.object(creator, "run_macrodroid_change", run),
            mock.patch.object(creator, "get_slot", lambda _sid: {"complete": "create-and-close" in events}),
            mock.patch.object(creator, "_pipeline_slot_complete", lambda slot: slot["complete"]),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", lambda sid, ip, at: events.append((sid, ip, at))),
        ):
            app._run_bulk_batch()
            self.assertEqual(bulk_creation.load_state(Path(folder) / "queue.json")["remaining"], 0)
        self.assertEqual(events, ["trigger", ("a0046", "31.217.14.168", "2026-10-16T12:00:13Z"), "create-and-close"])

    def test_actual_legacy_retry_keeps_unknown_baseline_time_and_sends_no_reset(self):
        app = self.app(1)
        app.bulk_state.update(stage="retry_ip", slot_id="a0046", baseline_ip="31.217.20.252")
        app._bulk_slot_and_entry = lambda state: ("a0046", state["queue"][0], date(2000, 1, 1))
        clock = Clock()
        created = []
        checkpoints = []
        app._run_creation_pipeline = lambda *_a, **_k: created.append(True) or True
        def recheck(proxy, baseline, **kwargs):
            return ip_change.observe_proxy_without_trigger(
                proxy, baseline, **kwargs, fetcher=lambda *_a: "31.217.14.168",
                monotonic=clock.monotonic, now=clock.now,
                waiter=lambda seconds: clock.advance(seconds) or False,
            )
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(creator, "save_bulk_state", lambda state: checkpoints.append(state) or bulk_creation.save_state(state, Path(folder) / "queue.json")),
            mock.patch.object(creator, "run_macrodroid_change", side_effect=AssertionError("duplicate webhook")),
            mock.patch.object(creator, "observe_proxy_without_trigger", recheck),
            mock.patch.object(creator, "get_slot", lambda _sid: {"complete": bool(created)}),
            mock.patch.object(creator, "_pipeline_slot_complete", lambda slot: slot["complete"]),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", lambda *_a: None),
        ):
            app._run_bulk_batch()
        self.assertEqual(created, [True])
        proof = next(state["ip_attempt"]["verification_evidence"] for state in checkpoints
                     if state.get("ip_attempt") and state["ip_attempt"]["verification_evidence"])
        self.assertIsNone(proof["baseline"]["observed_at_utc"])
        self.assertEqual(proof["baseline"]["ip"], "31.217.20.252")

    def test_paused_ui_names_confirmation_without_enabling_manual_reset(self):
        app = self.app(1)
        app.bulk_state.update(stage="paused", failure_phase="ip", failure_reason="FalconProxy authentication did not recover.")
        class Button:
            def configure(self, **kwargs):
                self.values = kwargs
        class Value:
            def set(self, value):
                self.value = value
        app.ip_reset_btn = Button()
        app.bulk_quick_resume_btn = Button()
        app.ip_cycle_status_var = Value()
        app._sync_ip_setup_controls(True)
        app._refresh_bulk_panel()
        self.assertEqual(app.ip_reset_btn.values, {"text": "Batch paused — IP confirmation", "state": "disabled"})
        self.assertEqual(app.bulk_quick_resume_btn.values, {"state": "normal"})
        self.assertIn("Batch paused", app.ip_cycle_status_var.value)
        self.assertIn("authentication did not recover", app.ip_cycle_status_var.value)
        self.assertIn("Retry / Resume", app.ip_cycle_status_var.value)

    def test_paused_batch_remains_idle_on_reopen(self):
        app = self.app(3)
        app.bulk_state.update(stage="paused", slot_id="a0046", baseline_ip="31.217.20.252", failure_phase="ip")
        before = dict(app.bulk_state)
        with mock.patch.object(creator.threading, "Thread", side_effect=AssertionError("paused batch started")):
            app._start_bulk_worker()
        self.assertEqual(app.bulk_state, before)
        self.assertIsNone(app.worker)

    def test_expired_restart_and_changed_scope_pause_without_requests(self):
        for reason in ("expired", "scope"):
            with self.subTest(reason=reason):
                app = self.app(3)
                proof = trigger_checkpoint("31.217.20.252")
                app.bulk_state.update(stage="may_have_triggered", slot_id="a0046", baseline_ip=proof["baseline_ip"],
                                      trigger_deadline_utc=proof["deadline_utc"], ip_attempt=proof["attempt"])
                if reason == "expired":
                    app.bulk_state["trigger_deadline_utc"] = "2000-01-01T00:00:00Z"
                else:
                    app._bulk_proxy_scope = lambda: "b" * 64
                app._bulk_slot_and_entry = lambda state: ("a0046", state["queue"][0], date(2000, 1, 1))
                pauses = []
                app._pause_bulk = lambda phase, message: pauses.append((phase, message))
                with (
                    mock.patch.object(creator, "run_macrodroid_change", side_effect=AssertionError("reset")),
                    mock.patch.object(creator, "observe_proxy_without_trigger", side_effect=AssertionError("read")),
                ):
                    app._run_bulk_batch()
                self.assertEqual(pauses[0][0], "ip")
                self.assertEqual((app.bulk_state["remaining"], len(app.bulk_state["queue"])), (3, 3))

    def test_late_checkpoint_cannot_restart_stopped_batch(self):
        app = self.app(1)
        proof = trigger_checkpoint("31.217.20.252")
        app.bulk_state.update(stage="stopped", slot_id="a0046", baseline_ip=proof["baseline_ip"])
        stale = dict(app.bulk_state, stage="may_have_triggered", ip_attempt=proof["attempt"])
        with mock.patch.object(creator, "save_bulk_state", side_effect=AssertionError("stopped state overwritten")):
            with self.assertRaises(creator.BulkError):
                app._persist_bulk(stale)


if __name__ == "__main__":
    unittest.main()
