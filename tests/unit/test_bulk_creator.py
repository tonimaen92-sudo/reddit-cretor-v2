"""Offline Creator batch ordering, retry and restart tests."""

from __future__ import annotations

import threading
import tempfile
import unittest
import uuid
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


def radio_journal(state, phase="completed"):
    """Return credential-free journal metadata bound to ``state``'s head."""
    return {
        "version": bulk_creation.RADIO_RECOVERY_VERSION,
        "state": phase,
        "attempt": creator.MAX_IP_RESET_ATTEMPTS_PER_HEAD,
        "head_fingerprint": bulk_creation.radio_recovery_head_fingerprint(state),
        "operation_id": str(uuid.uuid4()),
        "updated_at_utc": "2026-10-23T10:00:00Z",
    }


def complete_radio_cycle(*, on_transition, **_kwargs):
    """Offline Xiaomi reset seam which persists every real controller phase."""
    for phase in (
        "pending", "on_confirmed", "restore_pending", "off_confirmed",
        "verify_pending", "completed",
    ):
        on_transition(phase)


def restore_radio_only(*, on_transition, **_kwargs):
    """Offline crash recovery seam; deliberately never re-enters airplane ON."""
    for phase in ("restore_pending", "off_confirmed", "verify_pending", "completed"):
        on_transition(phase)


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
        app._pause_bulk = lambda phase, reason, **_kwargs: self.fail(f"unexpected pause: {phase}: {reason}")
        app.bulk_state = empty_state()
        app.bulk_state.update(
            queue=[{"birth_date": "01.01.2000", "swipes": 10 + i, "delay_seconds": 2.0 + i}
                   for i in range(count)],
            requested=count, remaining=count, stage="ip_pending",
        )
        # Offline seam for the durable-state reload; test fixtures set bulk_state
        # directly and expect it to be the source of truth for the decision.
        app._latest_bulk_state = lambda: dict(app.bulk_state)
        return app

    def test_bind_crane_identity_does_not_compare_ghost_display_id_to_uuid(self):
        app = self.app(1)
        messages = []
        app.log = messages.append
        row = {
            "id": "a0236",
            "ghost": True,
            "ghost_container_state": "ready",
            "ghost_container_instance": "SHORT-GHOST-ID",
            "reddit": False,
        }
        full_uuid = "E3A21B7C-6B97-4C8F-91D8-8B2B7D552F10"

        with (
            mock.patch.object(creator, "get_slot", return_value=row),
            mock.patch.object(
                creator,
                "resolve_crane_container_identity",
                return_value=("unique", full_uuid),
            ),
            mock.patch.object(
                creator, "reconcile_crane_container_identity"
            ) as reconcile,
        ):
            resolved = creator.App._bind_crane_container_identity(
                app, mock.Mock(), "a0236"
            )

        self.assertEqual(resolved, full_uuid)
        reconcile.assert_called_once_with(
            "a0236", full_uuid, app.log, allow_pending_rebind=False
        )
        self.assertTrue(any("separate" in message for message in messages))

    def test_accepted_username_variant_updates_only_current_queue_name(self):
        app = self.app(2)
        app.bulk_state.update(slot_id="a0229", stage="creating")
        app.bulk_state["queue"][0].update(
            username="Chosen_Name9", password="FixedPassword1!"
        )
        app.bulk_state["queue"][1]["username"] = "NextName"
        app.bulk_editor_baseline = [dict(row) for row in app.bulk_state["queue"]]
        app._username_claim_owners = lambda: {}
        saved = []

        def persist(state):
            app.bulk_state = dict(state)
            saved.append(dict(state))

        app._persist_bulk = persist
        creator.App._commit_bulk_username_variant(
            app, "a0229", "Chosen_Name9", "Chosen_Name0"
        )

        self.assertEqual(len(saved), 1)
        self.assertEqual(app.bulk_state["queue"][0]["username"], "Chosen_Name0")
        self.assertEqual(
            app.bulk_state["queue"][0]["password"], "FixedPassword1!"
        )
        self.assertEqual(app.bulk_state["queue"][1]["username"], "NextName")
        self.assertEqual(
            app.bulk_editor_baseline[0]["username"], "Chosen_Name0"
        )

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
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value={"outcome": "verified"},
            ),
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
        self.assertEqual(events.count("reset"), 1)
        self.assertEqual(events[0], "reset")
        self.assertEqual(events[1], ("slot_ip", "a0001", "198.51.100.2"))
        self.assertEqual(events[2], ("create", "a0001", 10))
        self.assertEqual(events[4], ("slot_ip", "a0002", "198.51.100.2"))
        self.assertEqual(events[5], ("create", "a0002", 11))
        self.assertEqual(len(app.stop.waits), 1)
        self.assertGreater(app.stop.waits[0], 1.5)
        self.assertLessEqual(app.stop.waits[0], 2.0)

    def test_saved_username_mismatch_prevents_reuse(self):
        app = self.app(1)
        app.bulk_state["queue"][0]["username"] = "RequestedName"
        with self.assertRaisesRegex(creator.BulkError, "different|requests"):
            app._assert_bulk_requested_username(
                "a0001", app.bulk_state["queue"][0],
                {"id": "a0001", "username": "DifferentName"},
            )

    def test_pipeline_passes_requested_name_and_password_for_named_batch(self):
        app = self.app(1)
        app.status = mock.Mock()
        slot = {
            "id": "a0001", "reddit_birth_date": "01.01.2000",
            "ghost": True, "ghost_container_state": "ready",
            "ghost_container_instance": "a0001",
            "crane_container_instance": "A0001-FULL",
            "email": "test@icloud.com",
            "username": "", "password": "", "reddit": False,
        }
        calls = []
        phone = mock.Mock()
        app._connect = lambda _password: (phone, object())

        def create(*_args, **kwargs):
            calls.append(kwargs)
            slot.update(
                username=kwargs.get("requested_username") or "SuggestedName",
                password=kwargs.get("requested_password") or "saved-password",
                reddit=True,
            )

        with (
            mock.patch.object(creator, "get_slot", return_value=slot),
            mock.patch.object(creator, "ghost_container_state", return_value="ready"),
            mock.patch.object(creator, "run_create_reddit", side_effect=create),
            mock.patch.object(creator, "creation_feed_swipe_progress", return_value=(10, 10)),
            mock.patch.object(creator, "slot_reddit_ready", return_value=True),
        ):
            self.assertTrue(app._run_creation_pipeline(
                "offline-password", "a0001", date(2000, 1, 1), 10,
                requested_username="ChosenName",
                requested_password="ChosenPass9!",
                manage_worker=False,
            ))
        self.assertEqual(calls[0]["requested_username"], "ChosenName")
        self.assertEqual(calls[0]["requested_password"], "ChosenPass9!")
        phone.close.assert_called_once()

    def test_pipeline_accepts_committed_username_variant_without_false_failure(self):
        app = self.app(1)
        app.status = mock.Mock()
        app.bulk_state.update(slot_id="a0001", stage="creating")
        app.bulk_state["queue"][0].update(
            username="ChosenName9", password="ChosenPass9!"
        )
        app.bulk_editor_baseline = [dict(app.bulk_state["queue"][0])]
        app._username_claim_owners = lambda: {}
        app._persist_bulk = lambda state: setattr(app, "bulk_state", dict(state))
        slot = {
            "id": "a0001", "reddit_birth_date": "01.01.2000",
            "ghost": True, "ghost_container_state": "ready",
            "ghost_container_instance": "a0001",
            "crane_container_instance": "A0001-FULL",
            "email": "test@icloud.com",
            "username": "", "password": "", "reddit": False,
        }
        phone = mock.Mock()
        app._connect = lambda _password: (phone, object())

        def create(*_args, **kwargs):
            kwargs["username_committer"]("ChosenName9", "ChosenName0")
            slot.update(
                username="ChosenName0",
                password=kwargs["requested_password"],
                reddit=True,
            )

        with (
            mock.patch.object(creator, "get_slot", return_value=slot),
            mock.patch.object(creator, "ghost_container_state", return_value="ready"),
            mock.patch.object(creator, "run_create_reddit", side_effect=create),
            mock.patch.object(
                creator, "creation_feed_swipe_progress", return_value=(10, 10)
            ),
            mock.patch.object(creator, "slot_reddit_ready", return_value=True),
        ):
            self.assertTrue(app._run_creation_pipeline(
                "offline-password", "a0001", date(2000, 1, 1), 10,
                requested_username="ChosenName9",
                requested_password="ChosenPass9!",
                manage_worker=False,
            ))
        self.assertEqual(
            app.bulk_state["queue"][0]["username"], "ChosenName0"
        )
        self.assertEqual(
            app.bulk_state["queue"][0]["password"], "ChosenPass9!"
        )
        phone.close.assert_called_once()

    def test_pipeline_reconnect_reloads_committed_username_variant(self):
        app = self.app(1)
        app.bulk_state.update(slot_id="a0001", stage="creating")
        app.bulk_state["queue"][0].update(
            username="ChosenName9", password="ChosenPass9!"
        )
        calls = []

        def attempt(*_args, **kwargs):
            calls.append(
                (kwargs["requested_username"], kwargs["requested_password"])
            )
            if len(calls) == 1:
                app.bulk_state["queue"][0]["username"] = "ChosenName0"
                return creator._PIPELINE_RECOVERY_RETRY
            return True

        app._run_creation_pipeline_attempt = attempt

        self.assertTrue(
            app._run_creation_pipeline(
                "offline-password",
                "a0001",
                date(2000, 1, 1),
                10,
                requested_username="ChosenName9",
                requested_password="ChosenPass9!",
                manage_worker=False,
            )
        )
        self.assertEqual(
            calls,
            [
                ("ChosenName9", "ChosenPass9!"),
                ("ChosenName0", "ChosenPass9!"),
            ],
        )

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
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value={"outcome": "verified"},
            ),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", lambda *_args: None),
            mock.patch.object(creator, "get_slot", lambda sid: {"complete": sid in complete}),
            mock.patch.object(creator, "_pipeline_slot_complete", lambda row: bool(row and row.get("complete"))),
        ):
            app._run_bulk_batch()
        self.assertEqual(app.bulk_state["stage"], "idle")
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(len(app.stop.waits), 9)
        self.assertEqual([event for event in events if event[0] == "ip"], [("ip", "a0001")])
        self.assertEqual(
            [event for event in events if event[0] == "create"],
            [("create", f"a{index + 1:04d}") for index in range(10)],
        )

    def test_reopen_can_resume_an_active_saved_stage(self):
        app = self.app(1)
        app.bulk_state.update(stage="may_have_triggered", slot_id="a0001")
        starts = []
        app._start_bulk_worker = lambda: starts.append(True)
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", lambda _sid: {"id": "a0001"}),
        ):
            self.assertTrue(app.retry_bulk_batch(password="offline-password"))
        self.assertEqual(starts, [True])
        self.assertEqual(app.bulk_state["stage"], "may_have_triggered")

    def test_backend_start_api_persists_before_starting_worker(self):
        app = self.app(1)
        app.bulk_state = empty_state()
        starts = []
        app._start_bulk_worker = lambda: starts.append(dict(app.bulk_state))
        item = {"birth_date": "01.01.2000", "swipes": 12, "delay_seconds": 2.5}
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "indices_used", lambda: {1}),
        ):
            started = app.start_bulk_batch([item], 1, "offline-password")
        self.assertTrue(started)
        self.assertEqual(starts[0]["stage"], "ip_pending")
        self.assertEqual(starts[0]["queue"], [item])
        self.assertEqual(starts[0]["remaining"], 1)
        # Start always targets a fresh slot above every known reservation.
        self.assertEqual(starts[0]["slot_id"], "a0002")

    def test_start_from_stopped_archives_reserves_and_runs_fresh(self):
        # The reported scenario: a stopped batch points at a slot (a0072) whose
        # row never survived.  Start Batch must archive that batch, permanently
        # reserve a0072 without fabricating a row, and run the new rows only.
        app = self.app(1)
        old_head = {"birth_date": "20.04.2005", "swipes": 27, "delay_seconds": 3.0}
        old_tail = {"birth_date": "11.02.2004", "swipes": 22, "delay_seconds": 4.0}
        app.bulk_state.update(
            queue=[old_head, old_tail],
            requested=2,
            remaining=2,
            stage="stopped",
            slot_id="a0072",
            baseline_ip="31.217.14.3",
            verified_ip="31.217.18.190",
            verified_at_utc="2026-10-16T18:45:53Z",
            failures=[{"slot_id": "a0070", "code": "email_invalid"}],
        )
        new_items = [
            {"birth_date": "01.01.2001", "swipes": 10, "delay_seconds": 2.0},
            {"birth_date": "02.02.2002", "swipes": 11, "delay_seconds": 3.0},
            {"birth_date": "03.03.2003", "swipes": 12, "delay_seconds": 4.0},
        ]
        events = []
        app._start_bulk_worker = lambda: events.append(("start", dict(app.bulk_state)))
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: None),
            mock.patch.object(
                creator, "retire_slot_id",
                lambda sid: events.append(("reserve", sid)) or True,
            ),
            mock.patch.object(creator, "indices_used", lambda: set(range(1, 72))),
            mock.patch.object(
                creator,
                "archive_bulk_state",
                side_effect=lambda state, **_kw: events.append(("archive", list(state["queue"])))
                or Path("bulk_creation.superseded-test.json"),
            ),
            mock.patch.object(
                creator,
                "archive_bulk_draft",
                side_effect=lambda **_kw: events.append(("archive-draft", True))
                or Path("bulk_draft.superseded-test.json"),
            ),
            mock.patch.object(
                creator,
                "save_bulk_state",
                side_effect=lambda state: events.append(("save", dict(state))),
            ),
        ):
            self.assertTrue(app.start_bulk_batch(new_items, 3, "offline-password"))

        saved = dict(app.bulk_state)
        # The old queue was never prepended or replayed; only editor rows run.
        self.assertEqual(saved["queue"], new_items)
        self.assertEqual(saved["stage"], "ip_pending")
        self.assertEqual(saved["remaining"], 3)
        # The vanished id is permanently reserved before the new run activates,
        # and the fresh run lands above every known reservation (a0073).
        self.assertEqual(events[0], ("reserve", "a0072"))
        self.assertEqual(saved["slot_id"], "a0073")
        self.assertTrue(saved["delete_containers"])
        # Old per-attempt failure/IP state is cleared with the new run.
        self.assertEqual(saved["failures"], [])
        self.assertIsNone(saved["verified_ip"])
        self.assertIsNone(saved["baseline_ip"])
        self.assertIsNone(saved["ip_attempt"])
        # Superseded state and draft were archived and logged before the save.
        self.assertEqual(events[1], ("archive", [old_head, old_tail]))
        self.assertEqual(events[2], ("archive-draft", True))
        self.assertEqual(events[3][0], "save")
        self.assertEqual(events[4], ("start", events[3][1]))

    def test_fresh_start_preview_cancel_changes_nothing(self):
        # Cancelling the "start a new batch" confirmation leaves the saved
        # batch, its queue and its slot untouched.
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
        app.start_bulk_batch = lambda *_args, **_kwargs: self.fail("cancelled start ran")
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

    def test_creation_reset_uses_exact_pipeline_failure_reason(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="retry_creation", slot_id="a0047", verified_ip="198.51.100.9",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._bulk_slot_and_entry = lambda state: (
            "a0047", state["queue"][0], date(2005, 4, 20)
        )
        resets = []
        app._reset_failed_creation = lambda sid, entry, reason, **kwargs: resets.append(
            (sid, entry, reason, kwargs)
        ) or False
        app._pause_bulk = lambda *_a, **_k: self.fail(
            "ordinary incomplete failure was paused instead of reset"
        )

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

        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0][0], "a0047")
        self.assertIn("Sorry, please try again later", resets[0][2])
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["slot_id"], "a0047")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_failed_pre_account_slot_is_preserved_and_skipped_without_delete(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._pause_bulk = lambda *_args, **_kwargs: self.fail("unexpected pause")
        entry = app.bulk_state["queue"][0]

        with (
            mock.patch.object(creator, "get_slot", return_value={"reddit": False}),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
            mock.patch.object(
                creator, "delete_named_instance", side_effect=AssertionError("deleted")
            ),
            mock.patch.object(
                creator, "retire_slot_id", side_effect=AssertionError("retired")
            ),
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
        ):
            self.assertTrue(
                creator.App._skip_failed_bulk_slot(
                    app,
                    "a0047",
                    entry,
                    "Email field still invalid.",
                )
            )

        self.assertEqual(app.stop.waits, [])
        skipped.assert_called_once_with(
            "a0047",
            creator.BULK_SKIP_FAILURE_CODE,
            "Email field still invalid.",
            app.log,
        )
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(app.bulk_state["stage"], "idle")
        self.assertEqual(
            app.bulk_state["failures"][0]["code"],
            creator.BULK_SKIP_FAILURE_CODE,
        )

    def test_stop_preserves_failed_slot_and_queue_without_advancing(self):
        app = self.app(1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        app.stop.flag = True
        entry = app.bulk_state["queue"][0]

        with (
            mock.patch.object(creator, "get_slot", return_value={"reddit": False}),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
            mock.patch.object(
                creator, "delete_named_instance", side_effect=AssertionError("deleted")
            ),
        ):
            self.assertFalse(
                creator.App._skip_failed_bulk_slot(
                    app, "a0047", entry, "Email field still invalid."
                )
            )

        skipped.assert_not_called()
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["stage"], "creating")

    def test_created_slot_late_failure_is_preserved_with_its_account_data(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        recorded = []

        def record(sid, code, reason, log):
            recorded.append((sid, code, reason))
            return {"id": sid, "reddit": True}

        with (
            mock.patch.object(creator, "get_slot", return_value={"reddit": True}),
            mock.patch.object(creator, "mark_bulk_skipped", side_effect=record),
            mock.patch.object(
                creator, "delete_named_instance", side_effect=AssertionError("deleted")
            ),
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
        ):
            self.assertTrue(
                creator.App._skip_failed_bulk_slot(
                    app,
                    "a0047",
                    app.bulk_state["queue"][0],
                    "Post-account cleanup failed.",
                )
            )

        self.assertEqual(recorded[0][0], "a0047")
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(
            app.bulk_state["failures"][0]["code"], creator.BULK_SKIP_FAILURE_CODE
        )

    def test_unpreservable_failed_slot_pauses_without_consuming_queue(self):
        app = self.app(1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )

        with (
            mock.patch.object(creator, "get_slot", return_value={"reddit": False}),
            mock.patch.object(
                creator,
                "mark_bulk_skipped",
                side_effect=creator.PhoneError("slots.json is unreadable"),
            ),
            mock.patch.object(
                creator, "delete_named_instance", side_effect=AssertionError("deleted")
            ),
            mock.patch.object(creator, "retire_slot_id") as retire,
        ):
            self.assertFalse(
                creator.App._skip_failed_bulk_slot(
                    app,
                    "a0047",
                    app.bulk_state["queue"][0],
                    "Email field still invalid.",
                )
            )

        retire.assert_not_called()
        self.assertEqual(pauses[0][2], "bulk_skip_persist_failed")
        self.assertIn("slots.json is unreadable", pauses[0][1])
        self.assertEqual(app.bulk_state["remaining"], 1)

    def test_bridge_exhaustion_pauses_without_attempting_container_delete(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="retry_creation",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._bulk_slot_and_entry = lambda state: (
            "a0047", state["queue"][0], date(2005, 4, 20)
        )
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )

        def bridge_failure(*_args, **_kwargs):
            app._last_pipeline_failure_code = "bridge_retries_exhausted"
            app._last_pipeline_failure_reason = "USB bridge unavailable."
            return False

        app._run_creation_pipeline = bridge_failure
        app._skip_failed_bulk_slot = lambda *_args, **_kwargs: self.fail(
            "container deletion attempted without a working bridge"
        )
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(creator, "get_slot", return_value={"reddit": False}),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(
            pauses,
            [("creation", "USB bridge unavailable.", "bridge_retries_exhausted")],
        )
        self.assertEqual(app.bulk_state["remaining"], 1)

    def test_ai_budget_exhaustion_pauses_without_reset_delete_or_queue_consume(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="retry_creation",
            slot_id="a0227",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._bulk_slot_and_entry = lambda state: (
            "a0227", state["queue"][0], date(2005, 4, 20)
        )
        original_queue = [dict(row) for row in app.bulk_state["queue"]]
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )

        def budget_failure(*_args, **_kwargs):
            app._last_pipeline_failure_shared = True
            app._last_pipeline_failure_code = (
                creator.AI_RECOVERY_BUDGET_EXHAUSTED_CODE
            )
            app._last_pipeline_failure_reason = (
                "AI recovery API call limit reached before a locally verified "
                "postcondition"
            )
            return False

        app._run_pipeline_with_bridge_recovery = budget_failure
        app._reset_failed_creation = mock.Mock(
            side_effect=AssertionError("budget exhaustion triggered stall reset")
        )
        app._stall_reset = mock.Mock(
            side_effect=AssertionError("budget exhaustion deleted the attempt")
        )

        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(creator, "get_slot", return_value={"reddit": False}),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(
                creator,
                "delete_named_instance",
                side_effect=AssertionError("budget exhaustion deleted a container"),
            ),
            mock.patch.object(
                creator,
                "consume_stalled",
                side_effect=AssertionError("budget exhaustion consumed the queue"),
            ),
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(
            pauses,
            [
                (
                    "creation",
                    app._last_pipeline_failure_reason,
                    creator.AI_RECOVERY_BUDGET_EXHAUSTED_CODE,
                )
            ],
        )
        app._reset_failed_creation.assert_not_called()
        app._stall_reset.assert_not_called()
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["slot_id"], "a0227")
        self.assertEqual(app.bulk_state["queue"], original_queue)

    def test_ai_discard_directive_is_pause_only_without_delete_or_queue_consume(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="retry_creation",
            slot_id="a0227",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._bulk_slot_and_entry = lambda state: (
            "a0227", state["queue"][0], date(2005, 4, 20)
        )
        original_queue = [dict(row) for row in app.bulk_state["queue"]]
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )

        def refused_directive(*_args, **_kwargs):
            app._last_pipeline_failure_shared = True
            app._last_pipeline_failure_code = creator.AI_DISCARD_REFUSED_CODE
            app._last_pipeline_failure_reason = (
                "Anthropic requested a destructive attempt reset; controller refused it."
            )
            return False

        app._run_pipeline_with_bridge_recovery = refused_directive
        app._reset_failed_creation = mock.Mock(
            side_effect=AssertionError("AI directive triggered creation reset")
        )
        app._stall_reset = mock.Mock(
            side_effect=AssertionError("AI directive deleted the attempt")
        )

        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(creator, "get_slot", return_value={"reddit": False}),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(
                creator,
                "delete_named_instance",
                side_effect=AssertionError("AI directive deleted a container"),
            ),
            mock.patch.object(
                creator,
                "consume_stalled",
                side_effect=AssertionError("AI directive consumed the queue"),
            ),
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(
            pauses,
            [
                (
                    "creation",
                    app._last_pipeline_failure_reason,
                    creator.AI_DISCARD_REFUSED_CODE,
                )
            ],
        )
        app._reset_failed_creation.assert_not_called()
        app._stall_reset.assert_not_called()
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["slot_id"], "a0227")
        self.assertEqual(app.bulk_state["queue"], original_queue)

    def test_pipeline_email_field_invalid_sets_dedicated_failure_code(self):
        app = self.app(1)
        app.status = mock.Mock()
        slot = {
            "id": "a0047",
            "email": "fixture.saved_8h@icloud.com",
            "reddit": False,
            "ghost": True,
            "ghost_container_state": "ready",
            "ghost_container_instance": "instance-proof",
            "crane_container_instance": "INSTANCE-PROOF-FULL",
            "reddit_birth_date": "20.04.2005",
        }
        phone = mock.Mock()
        app._connect = lambda _password: (phone, object())

        def fail_reddit(*_args, **_kwargs):
            raise creator.EmailFieldInvalid(
                "Email field still invalid after one type and at most one bounded "
                "single-missing-character retype (need the exact saved @icloud.com "
                "address in Reddit's email form and no 'Please enter a valid email'). "
                "Did not tap Continue. See 873_email_still_invalid."
            )

        with (
            mock.patch.object(creator, "get_slot", return_value=slot),
            mock.patch.object(creator, "run_create_reddit", side_effect=fail_reddit),
        ):
            success = creator.App._run_creation_pipeline(
                app,
                "offline-password",
                "a0047",
                date(2005, 4, 20),
                10,
                manage_worker=False,
            )

        self.assertFalse(success)
        self.assertEqual(
            app._last_pipeline_failure_code,
            creator.EmailFieldInvalid.failure_code,
        )
        self.assertEqual(app._last_pipeline_failure_code, "reddit_email_field_invalid")
        self.assertIn("873_email_still_invalid", app._last_pipeline_failure_reason)
        phone.close.assert_called_once_with()

    def test_pipeline_plain_phone_error_is_local_pause_only(self):
        app = self.app(1)
        app.status = mock.Mock()
        slot = {
            "id": "a0047",
            "email": "fixture.saved_8h@icloud.com",
            "reddit": False,
            "ghost": True,
            "ghost_container_state": "ready",
            "ghost_container_instance": "instance-proof",
            "crane_container_instance": "INSTANCE-PROOF-FULL",
            "reddit_birth_date": "20.04.2005",
        }
        phone = mock.Mock()
        app._connect = lambda _password: (phone, object())

        def fail_reddit(*_args, **_kwargs):
            raise creator.PhoneError("Saved password flow did not finish its code step.")

        with (
            mock.patch.object(creator, "get_slot", return_value=slot),
            mock.patch.object(creator, "run_create_reddit", side_effect=fail_reddit),
        ):
            success = creator.App._run_creation_pipeline(
                app,
                "offline-password",
                "a0047",
                date(2005, 4, 20),
                10,
                manage_worker=False,
            )

        self.assertFalse(success)
        self.assertEqual(
            app._last_pipeline_failure_code, "deterministic_phone_error"
        )
        self.assertTrue(app._last_pipeline_failure_shared)
        self.assertEqual(
            app._last_pipeline_failure_reason,
            "Saved password flow did not finish its code step.",
        )
        phone.close.assert_called_once_with()

    def test_existing_profile_in_incomplete_bulk_container_requests_replacement(self):
        app = self.app(1)
        app.status = mock.Mock()
        app.bulk_state.update(stage="creating", slot_id="a0047")
        slot = {
            "id": "a0047",
            "email": "fixture.saved_8h@icloud.com",
            "reddit": False,
            "ghost": True,
            "ghost_container_state": "ready",
            "ghost_container_instance": "instance-proof",
            "crane_container_instance": "INSTANCE-PROOF-FULL",
            "reddit_birth_date": "20.04.2005",
        }
        phone = mock.Mock()
        app._connect = lambda _password: (phone, object())

        def fail_reddit(*_args, **_kwargs):
            raise creator.ExistingAccountContainerError(
                "Bottom-nav You opened an existing account/profile."
            )

        with (
            mock.patch.object(creator, "get_slot", return_value=slot),
            mock.patch.object(creator, "run_create_reddit", side_effect=fail_reddit),
            mock.patch.object(creator, "UIRecoveryAgent") as agent,
        ):
            success = creator.App._run_creation_pipeline(
                app,
                "offline-password",
                "a0047",
                date(2005, 4, 20),
                10,
                manage_worker=False,
            )

        self.assertFalse(success)
        self.assertEqual(
            app._last_pipeline_failure_code,
            creator.CONTAINER_RECREATE_FAILURE_CODE,
        )
        self.assertFalse(app._last_pipeline_failure_shared)
        agent.assert_not_called()
        phone.close.assert_called_once_with()

    def test_existing_profile_preserves_container_outside_active_bulk_attempt(self):
        app = self.app(1)
        app.status = mock.Mock()
        app.bulk_state.update(stage="idle", slot_id=None)
        slot = {
            "id": "a0047",
            "email": "fixture.saved_8h@icloud.com",
            "reddit": False,
            "ghost": True,
            "ghost_container_state": "ready",
            "ghost_container_instance": "instance-proof",
            "crane_container_instance": "INSTANCE-PROOF-FULL",
            "reddit_birth_date": "20.04.2005",
        }
        phone = mock.Mock()
        app._connect = lambda _password: (phone, object())

        with (
            mock.patch.object(creator, "get_slot", return_value=slot),
            mock.patch.object(
                creator,
                "run_create_reddit",
                side_effect=creator.ExistingAccountContainerError(
                    "Existing account profile is open."
                ),
            ),
        ):
            success = creator.App._run_creation_pipeline(
                app,
                "offline-password",
                "a0047",
                date(2005, 4, 20),
                10,
                manage_worker=False,
            )

        self.assertFalse(success)
        self.assertEqual(
            app._last_pipeline_failure_code,
            creator.ExistingAccountContainerError.failure_code,
        )
        self.assertTrue(app._last_pipeline_failure_shared)
        phone.close.assert_called_once_with()

    def test_reddit_email_retry_later_is_typed_and_local(self):
        app = self.app(1)
        app.status = mock.Mock()
        slot = {
            "id": "a0047",
            "email": "fixture.saved_8h@icloud.com",
            "reddit": False,
            "ghost": True,
            "ghost_container_state": "ready",
            "ghost_container_instance": "instance-proof",
            "crane_container_instance": "INSTANCE-PROOF-FULL",
            "reddit_birth_date": "20.04.2005",
        }
        phone = mock.Mock()
        app._connect = lambda _password: (phone, object())

        with (
            mock.patch.object(creator, "get_slot", return_value=slot),
            mock.patch.object(
                creator,
                "run_create_reddit",
                side_effect=creator.RedditEmailRetryLater("retry later"),
            ),
            mock.patch.object(creator, "UIRecoveryAgent") as agent,
        ):
            success = creator.App._run_creation_pipeline(
                app, "offline-password", "a0047", date(2005, 4, 20), 10,
                manage_worker=False,
            )

        self.assertFalse(success)
        self.assertEqual(
            app._last_pipeline_failure_code,
            creator.RedditEmailRetryLater.failure_code,
        )
        self.assertFalse(app._last_pipeline_failure_shared)
        agent.assert_not_called()
        phone.close.assert_called_once_with()

    def test_reddit_email_retry_later_allows_one_delayed_retry_only(self):
        app = self.app(1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        app._wait_bulk_failure_grace = mock.Mock(return_value=True)
        app._persist_bulk = lambda state: setattr(app, "bulk_state", state)
        app._pause_bulk = mock.Mock()

        self.assertTrue(
            creator.App._retry_reddit_email_later(app, "a0047", "retry later")
        )
        self.assertEqual(app.bulk_state["stage"], "retry_creation")
        self.assertEqual(
            app.bulk_state["failure_code"],
            "reddit_email_retry_later_auto_retry",
        )
        self.assertFalse(
            creator.App._retry_reddit_email_later(app, "a0047", "retry later")
        )
        app._pause_bulk.assert_called_once_with(
            "creation",
            "retry later",
            code=creator.RedditEmailRetryLater.failure_code,
        )

    def test_email_field_invalid_batch_failure_pauses_without_reset(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="retry_creation",
            slot_id="a0047",
            verified_ip="198.51.100.9",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._bulk_slot_and_entry = lambda state: (
            "a0047",
            state["queue"][0],
            date(2005, 4, 20),
        )
        resets = []
        app._reset_failed_creation = lambda sid, entry, reason, **kwargs: resets.append(
            (sid, entry, reason, kwargs)
        ) or False
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )

        def rejected(*_args, **_kwargs):
            app._last_pipeline_failure_code = "reddit_email_field_invalid"
            app._last_pipeline_failure_reason = (
                "Email field still invalid after one type and at most one bounded "
                "single-missing-character retype. Did not tap Continue. "
                "See 873_email_still_invalid."
            )
            return False

        app._run_creation_pipeline = rejected
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator,
                "run_macrodroid_change",
                side_effect=AssertionError("reset on creation retry"),
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.9", "198.51.100.9"),
            ),
            mock.patch.object(creator, "get_slot", return_value={"reddit": False}),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(resets, [])
        self.assertEqual(len(pauses), 1)
        self.assertEqual(pauses[0][0], "creation")
        self.assertIn("873_email_still_invalid", pauses[0][1])
        self.assertEqual(pauses[0][2], "reddit_email_field_invalid")
        self.assertEqual(app._last_pipeline_failure_code, "reddit_email_field_invalid")
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["slot_id"], "a0047")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_plain_phone_error_batch_failure_routes_to_bounded_reset(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="retry_creation",
            slot_id="a0047",
            verified_ip="198.51.100.9",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._bulk_slot_and_entry = lambda state: (
            "a0047",
            state["queue"][0],
            date(2005, 4, 20),
        )
        resets = []
        app._reset_failed_creation = lambda sid, entry, reason, **kwargs: resets.append(
            (sid, entry, reason, kwargs)
        ) or False
        app._pause_bulk = lambda *_a, **_k: self.fail(
            "ordinary incomplete failure was paused instead of reset"
        )

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
                creator,
                "run_macrodroid_change",
                side_effect=AssertionError("reset on creation retry"),
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.9", "198.51.100.9"),
            ),
            mock.patch.object(creator, "get_slot", return_value={"reddit": False}),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0][0], "a0047")
        self.assertIn("Sorry, please try again later", resets[0][2])
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["slot_id"], "a0047")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_email_field_invalid_preserved_and_skipped_with_exact_code(self):
        app = self.app(2)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        entry = app.bulk_state["queue"][0]
        reason = (
            "Email field still invalid after one type and at most one bounded "
            "single-missing-character retype. Did not tap Continue. "
            "See 873_email_still_invalid."
        )

        with (
            mock.patch.object(creator, "get_slot", return_value={"reddit": False}),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
            mock.patch.object(
                creator, "delete_named_instance", side_effect=AssertionError("deleted")
            ),
            mock.patch.object(
                creator, "retire_slot_id", side_effect=AssertionError("retired")
            ),
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
        ):
            self.assertTrue(
                creator.App._skip_failed_bulk_slot(
                    app,
                    "a0047",
                    entry,
                    reason,
                    failure_code="reddit_email_field_invalid",
                )
            )

        self.assertEqual(app.stop.waits, [])
        skipped.assert_called_once_with(
            "a0047", "reddit_email_field_invalid", reason, app.log
        )
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["stage"], "between")
        self.assertEqual(
            app.bulk_state["failures"][0]["code"], "reddit_email_field_invalid"
        )
        self.assertEqual(app.bulk_state["failures"][0]["slot_id"], "a0047")

    def test_failed_email_slot_pauses_without_starting_next_item(self):
        app = self.app(2)
        app.bulk_state["queue"][0]["birth_date"] = "01.01.2003"
        app.bulk_state["queue"][1]["birth_date"] = "02.02.2004"
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        phone = mock.Mock()
        app._connect = lambda _password: (phone, object())
        previous_row = {
            "id": "a0047",
            "reddit": False,
            "email": "keep-4f@icloud.com",
            "ghost": True,
            "ghost_container_state": "ready",
            "ghost_container_instance": "instance-proof",
            "crane_container_instance": "INSTANCE-PROOF-FULL",
        }
        pipelines = []
        skipped = []
        deleted = []
        retired = []
        ghosts = []
        resets = []
        app._reset_failed_creation = lambda sid, entry, reason, **kwargs: resets.append(
            (sid, entry, reason, kwargs)
        ) or False
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )

        def pipeline(_password, sid, _birth, _swipes, **_kwargs):
            pipelines.append(sid)
            if sid == "a0047":
                app._last_pipeline_failure_code = "reddit_email_field_invalid"
                app._last_pipeline_failure_reason = (
                    "Email field still invalid after one type and at most one "
                    "bounded single-missing-character retype. Did not tap Continue. "
                    "See 873_email_still_invalid."
                )
                return False
            return True

        app._run_creation_pipeline = pipeline

        def slot_state(sid):
            if sid == "a0047":
                return {"reddit": False, "email": "keep-4f@icloud.com"}
            return {
                "reddit": True,
                "username": "",
                "email": "keep-4f@icloud.com",
                "complete": True,
            }

        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", side_effect=slot_state),
            mock.patch.object(
                creator,
                "_pipeline_slot_complete",
                side_effect=lambda row: bool(row and row.get("complete")),
            ),
            mock.patch.object(creator, "load_slots", lambda: [previous_row]),
            mock.patch.object(creator, "retired_indices", lambda: set()),
            mock.patch.object(
                creator,
                "ensure_ghost_container_needed",
                side_effect=lambda sid, _log: ghosts.append(sid),
            ),
            mock.patch.object(
                creator,
                "mark_bulk_skipped",
                side_effect=lambda sid, code, _reason, _log: (
                    skipped.append((sid, code)),
                    previous_row.setdefault(
                        "bulk_skip", {"slot_id": sid, "code": code}
                    ),
                )[1],
            ),
            mock.patch.object(
                creator,
                "delete_named_instance",
                side_effect=lambda _stack, _phone, sid: deleted.append(sid),
            ),
            mock.patch.object(
                creator,
                "retire_slot_id",
                side_effect=lambda sid: retired.append(sid),
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(
                creator,
                "run_macrodroid_change",
                return_value=verified_result("198.51.100.47", "198.51.100.48"),
            ),
            mock.patch.object(
                creator, "set_slot_falcon_proxy_ip", lambda *_args: None
            ),
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(pipelines, ["a0047"])
        self.assertEqual(skipped, [])
        self.assertEqual(deleted, [])
        self.assertEqual(retired, [])
        self.assertEqual(ghosts, ["a0047"])
        self.assertEqual(resets, [])
        self.assertEqual(len(pauses), 1)
        self.assertEqual(pauses[0][0], "creation")
        self.assertIn("873_email_still_invalid", pauses[0][1])
        self.assertEqual(pauses[0][2], "reddit_email_field_invalid")
        self.assertEqual(app.bulk_state["remaining"], 2)
        self.assertEqual(app.bulk_state["slot_id"], "a0047")
        self.assertEqual(len(app.bulk_state["queue"]), 2)
        self.assertEqual(app.bulk_state["failures"], [])

    def test_terminal_retired_slot_row_is_skipped_and_next_id_allocated(self):
        row = {
            "id": "a0047",
            "reddit": False,
            "email": "keep-4f@icloud.com",
            "creation_terminal_failure": {"code": "reddit_email_field_invalid"},
        }
        sid, slot, is_new = creator._resolve_creation_pipeline_target([row], {47})
        self.assertEqual(sid, "a0048")
        self.assertIsNone(slot)
        self.assertTrue(is_new)

    def test_retired_row_without_terminal_record_still_raises_the_guard(self):
        row = {"id": "a0047", "reddit": False}
        with self.assertRaises(creator.PhoneError):
            creator._resolve_creation_pipeline_target([row], {47})

    def test_exhausted_a0049_enters_bounded_reset_without_starting_a0050(self):
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
        resets = []
        app._reset_failed_creation = lambda sid, entry, reason, **kwargs: resets.append(
            (sid, entry, reason, kwargs)
        ) or False
        app._pause_bulk = lambda *_a, **_k: self.fail(
            "ordinary incomplete failure was paused instead of reset"
        )

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

        app._skip_failed_bulk_slot = lambda *_a, **_k: self.fail(
            "failed item was consumed instead of paused"
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

        self.assertEqual(
            creation_calls,
            [("a0049", "QuietMoon49")],
        )
        self.assertEqual(attached_ips, [])
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0][0], "a0049")
        self.assertEqual(
            app._last_pipeline_failure_code,
            creator.EmailReplacementExhausted.failure_code,
        )
        self.assertEqual(app.bulk_state["remaining"], 2)
        self.assertEqual(app.bulk_state["slot_id"], "a0049")
        self.assertEqual(len(app.bulk_state["queue"]), 2)
        self.assertEqual(app.bulk_state["failures"], [])

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

    def test_success_label_without_address_evidence_routes_to_bounded_ip_retry(self):
        app = self.app(1)
        app._bulk_slot_and_entry = lambda state: ("a0001", state["queue"][0], date(2000, 1, 1))
        retries = []
        app._retry_ip_after_failure = lambda reason, **kwargs: retries.append(
            (reason, kwargs.get("code"))
        ) or False
        def ip_change(_webhook, _proxy, **kwargs):
            kwargs["before_trigger"](trigger_checkpoint("198.51.100.1"))
            return {"outcome": "verified", "old_ip": "198.51.100.1", "new_ip": "198.51.100.2"}
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "run_macrodroid_change", ip_change),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", side_effect=AssertionError("IP attached without evidence")),
        ):
            creator.App._run_bulk_batch(app)
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0][1], "unexpected_local_failure")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_fast_adb_no_change_runs_old_macrodroid_then_final_adb(self):
        app = self.app(1)
        app.bulk_state["slot_id"] = "a0266"
        app._android_radio_config = lambda: (None, "test-xiaomi")
        original_queue = [dict(item) for item in app.bulk_state["queue"]]

        def no_change(_webhook, _proxy, **kwargs):
            kwargs["before_trigger"](trigger_checkpoint("198.51.100.1"))
            triggerer = kwargs.get("triggerer")
            if triggerer is not None:
                triggerer(_webhook, kwargs.get("trigger_timeout", 3.0))
            return {
                "outcome": "no_change",
                "error": "No IP change observed before the deadline.",
                "error_category": "",
                "trigger_acknowledged": True,
            }

        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator, "run_macrodroid_change", side_effect=no_change
            ) as webhook,
            mock.patch.object(
                creator, "reset_android_cellular_radio",
                side_effect=complete_radio_cycle,
            ) as adb_reset,
            mock.patch.object(
                creator, "restore_android_cellular_radio",
                side_effect=AssertionError("fresh fallback restored instead of cycling"),
            ) as adb_restore,
            mock.patch.object(
                creator, "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.1", "198.51.100.2"),
            ) as observe,
            mock.patch.object(
                creator.UIRecoveryAgent, "recover",
                side_effect=AssertionError("IP rotation reached Anthropic"),
            ) as ai_recover,
        ):
            result, _observations = creator.App._run_bulk_ip_check(
                app, dict(app.bulk_state)
            )

        self.assertEqual(result["outcome"], "verified")
        self.assertTrue(result["local_radio_fallback"])
        self.assertEqual(webhook.call_count, 2)
        self.assertEqual(
            webhook.call_args_list[0].args[0],
            "https://local-xiaomi.invalid/reset",
        )
        self.assertEqual(webhook.call_args_list[1].args[0], app.macrodroid_webhook)
        self.assertEqual(adb_reset.call_count, 2)
        adb_restore.assert_not_called()
        observe.assert_called_once()
        ai_recover.assert_not_called()
        self.assertEqual(
            app.bulk_state["ip_reset_attempt_count"],
            creator.MAX_IP_RESET_ATTEMPTS_PER_HEAD,
        )
        self.assertEqual(app.bulk_state["radio_recovery"]["state"], "completed")
        self.assertEqual(app.bulk_state["queue"], original_queue)
        self.assertEqual(app.bulk_state["remaining"], 1)

    def test_fast_adb_success_never_calls_real_macrodroid_fallback(self):
        app = self.app(1)
        app.bulk_state["slot_id"] = "a0266"
        app._android_radio_config = lambda: (None, "test-xiaomi")

        def fast_verified(target, _proxy, **kwargs):
            self.assertEqual(target, "https://local-xiaomi.invalid/reset")
            kwargs["before_trigger"](trigger_checkpoint("198.51.100.1"))
            kwargs["triggerer"](target, kwargs["trigger_timeout"])
            return verified_result("198.51.100.1", "198.51.100.2")

        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator, "run_macrodroid_change", side_effect=fast_verified
            ) as change,
            mock.patch.object(
                creator, "reset_android_cellular_radio",
                side_effect=complete_radio_cycle,
            ) as adb_reset,
        ):
            result, _observations = creator.App._run_bulk_ip_check(
                app, dict(app.bulk_state)
            )

        self.assertEqual(result["outcome"], "verified")
        self.assertTrue(result["local_radio_primary"])
        change.assert_called_once()
        adb_reset.assert_called_once()
        fast_call = change.call_args.kwargs
        self.assertEqual(fast_call["observation_seconds"], 25.0)
        self.assertEqual(fast_call["poll_interval"], 1.0)
        self.assertEqual(fast_call["baseline_cycles"], 2)
        self.assertEqual(
            adb_reset.call_args.kwargs["airplane_hold_seconds"], 10.0
        )
        self.assertEqual(adb_reset.call_args.kwargs["settle_seconds"], 1.0)
        self.assertEqual(app.bulk_state["ip_reset_attempt_count"], 1)
        self.assertEqual(app.bulk_state["radio_recovery"]["state"], "completed")

    def test_unavailable_fast_adb_immediately_hands_off_to_macrodroid(self):
        app = self.app(1)
        app.bulk_state["slot_id"] = "a0266"
        app._android_radio_config = lambda: (None, "test-xiaomi")
        targets = []

        def change(target, _proxy, **kwargs):
            targets.append(target)
            kwargs["before_trigger"](trigger_checkpoint("198.51.100.1"))
            if kwargs.get("triggerer") is not None:
                trigger = kwargs["triggerer"](target, kwargs["trigger_timeout"])
                return {
                    "outcome": "verification_failed",
                    "error": trigger["error"],
                    "trigger_acknowledged": False,
                }
            return verified_result("198.51.100.1", "198.51.100.2")

        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator, "run_macrodroid_change", side_effect=change
            ),
            mock.patch.object(
                creator,
                "reset_android_cellular_radio",
                side_effect=creator.AndroidRadioResetError(
                    "The configured Xiaomi is not ready over ADB.",
                    category="device_unavailable",
                ),
            ),
        ):
            result, _observations = creator.App._run_bulk_ip_check(
                app, dict(app.bulk_state)
            )

        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(
            targets,
            ["https://local-xiaomi.invalid/reset", app.macrodroid_webhook],
        )
        self.assertEqual(app.bulk_state["ip_reset_attempt_count"], 1)
        self.assertEqual(
            app.bulk_state["radio_recovery"], bulk_creation.empty_radio_recovery()
        )

    def test_fast_adb_restart_restores_without_repeating_or_using_macrodroid(self):
        app = self.app(1)
        checkpoint = trigger_checkpoint("198.51.100.1")
        app.bulk_state.update(
            stage="retry_ip",
            slot_id="a0266",
            baseline_ip=checkpoint["baseline_ip"],
            trigger_deadline_utc=checkpoint["deadline_utc"],
            ip_attempt=checkpoint["attempt"],
            ip_reset_attempt_count=1,
        )
        journal = radio_journal(app.bulk_state, "on_confirmed")
        journal["attempt"] = 1
        app.bulk_state["radio_recovery"] = journal
        app._android_radio_config = lambda: (None, "test-xiaomi")

        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator, "reset_android_cellular_radio",
                side_effect=AssertionError("restart repeated airplane ON"),
            ) as reset,
            mock.patch.object(
                creator, "restore_android_cellular_radio",
                side_effect=restore_radio_only,
            ) as restore,
            mock.patch.object(
                creator, "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.1", "198.51.100.2"),
            ) as observe,
            mock.patch.object(
                creator, "run_macrodroid_change",
                side_effect=AssertionError("restart called MacroDroid"),
            ) as macrodroid,
        ):
            result, _observations = creator.App._run_bulk_ip_check(
                app, dict(app.bulk_state)
            )

        self.assertEqual(result["outcome"], "verified")
        reset.assert_not_called()
        restore.assert_called_once()
        macrodroid.assert_not_called()
        self.assertEqual(observe.call_args.kwargs["observation_seconds"], 25.0)
        self.assertEqual(observe.call_args.kwargs["poll_interval"], 1.0)

    def test_crash_radio_journal_restores_only_without_repeating_cycle(self):
        for crash_phase in ("pending", "on_confirmed", "restore_pending"):
            with self.subTest(phase=crash_phase):
                app = self.app(1)
                checkpoint = trigger_checkpoint("198.51.100.1")
                app.bulk_state.update(
                    stage="retry_ip",
                    slot_id="a0266",
                    baseline_ip=checkpoint["baseline_ip"],
                    trigger_deadline_utc=checkpoint["deadline_utc"],
                    ip_attempt=checkpoint["attempt"],
                    ip_reset_attempt_count=creator.MAX_IP_RESET_ATTEMPTS_PER_HEAD,
                )
                app.bulk_state["radio_recovery"] = radio_journal(
                    app.bulk_state, crash_phase
                )
                app._android_radio_config = lambda: (None, "test-xiaomi")

                with (
                    mock.patch.object(
                        creator, "save_bulk_state", lambda _state: None
                    ),
                    mock.patch.object(
                        creator, "reset_android_cellular_radio",
                        side_effect=AssertionError("crash resume repeated airplane ON"),
                    ) as adb_reset,
                    mock.patch.object(
                        creator, "restore_android_cellular_radio",
                        side_effect=restore_radio_only,
                    ) as adb_restore,
                    mock.patch.object(
                        creator, "observe_proxy_without_trigger",
                        return_value=verified_result(
                            "198.51.100.1", "198.51.100.2"
                        ),
                    ) as observe,
                    mock.patch.object(
                        creator, "run_macrodroid_change",
                        side_effect=AssertionError("crash resume resent webhook"),
                    ) as webhook,
                    mock.patch.object(
                        creator.UIRecoveryAgent, "recover",
                        side_effect=AssertionError("IP recovery reached Anthropic"),
                    ) as ai_recover,
                ):
                    result, _observations = creator.App._run_bulk_ip_check(
                        app, dict(app.bulk_state)
                    )

                self.assertEqual(result["outcome"], "verified")
                self.assertEqual(app.bulk_state["radio_recovery"]["state"], "completed")
                webhook.assert_not_called()
                adb_reset.assert_not_called()
                adb_restore.assert_called_once()
                observe.assert_called_once()
                ai_recover.assert_not_called()

    def test_completed_radio_journal_rechecks_only_without_third_device_action(self):
        app = self.app(1)
        checkpoint = trigger_checkpoint("198.51.100.1")
        app.bulk_state.update(
            stage="retry_ip",
            slot_id="a0266",
            baseline_ip=checkpoint["baseline_ip"],
            trigger_deadline_utc=checkpoint["deadline_utc"],
            ip_attempt=checkpoint["attempt"],
            ip_reset_attempt_count=creator.MAX_IP_RESET_ATTEMPTS_PER_HEAD,
        )
        app.bulk_state["radio_recovery"] = radio_journal(app.bulk_state)
        app._android_radio_config = lambda: (None, "test-xiaomi")

        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator, "run_macrodroid_change",
                side_effect=AssertionError("completed journal resent webhook"),
            ) as webhook,
            mock.patch.object(
                creator, "reset_android_cellular_radio",
                side_effect=AssertionError("completed journal repeated airplane ON"),
            ) as adb_reset,
            mock.patch.object(
                creator, "restore_android_cellular_radio",
                side_effect=AssertionError("completed journal repeated restore"),
            ) as adb_restore,
            mock.patch.object(
                creator, "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.1", "198.51.100.2"),
            ) as observe,
        ):
            result, _observations = creator.App._run_bulk_ip_check(
                app, dict(app.bulk_state)
            )

        self.assertEqual(result["outcome"], "verified")
        webhook.assert_not_called()
        adb_reset.assert_not_called()
        adb_restore.assert_not_called()
        observe.assert_called_once()
        self.assertEqual(
            app.bulk_state["ip_reset_attempt_count"],
            creator.MAX_IP_RESET_ATTEMPTS_PER_HEAD,
        )
        self.assertEqual(app.bulk_state["radio_recovery"]["state"], "completed")

    def test_missing_baseline_routes_to_bounded_retry_without_creation(self):
        app = self.app(1)
        app.bulk_state.update(stage="retry_ip", slot_id="a0001", baseline_ip=None)
        app._bulk_slot_and_entry = lambda state: ("a0001", state["queue"][0], date(2000, 1, 1))
        retries = []
        app._retry_ip_after_failure = lambda reason, **kwargs: retries.append(
            (reason, kwargs.get("code"))
        ) or False
        with (
            mock.patch.object(creator, "run_macrodroid_change", side_effect=AssertionError("duplicate reset")),
            mock.patch.object(creator, "observe_proxy_without_trigger", side_effect=AssertionError("no comparison IP")),
        ):
            creator.App._run_bulk_batch(app)
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0][1], "ip_verification_failed")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_exhausted_remote_and_adb_phases_pause_once_and_never_call_ai(self):
        app = self.app(1)
        original_queue = [dict(item) for item in app.bulk_state["queue"]]
        app.bulk_state["slot_id"] = "a0262"
        app._bulk_slot_and_entry = lambda state: (
            "a0262", state["queue"][0], date(2000, 1, 1)
        )
        calls = []
        logs = []
        app.log = logs.append
        app._set_diag = lambda *_args: None
        app._pause_bulk = creator.App._pause_bulk.__get__(app, creator.App)

        def unchanged(_state):
            # The dedicated checker owns both action phases and returns only
            # after MacroDroid and the one local ADB fallback are spent.
            calls.append("macrodroid+adb")
            app.bulk_state["ip_reset_attempt_count"] = (
                creator.MAX_IP_RESET_ATTEMPTS_PER_HEAD
            )
            app.bulk_state["radio_recovery"] = radio_journal(app.bulk_state)
            return {
                "outcome": "no_change",
                "error": "No IP change observed before the deadline.",
                "error_category": "",
            }, []

        app._run_bulk_ip_check = unchanged
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", return_value={
                "id": "a0262", "reddit": False,
            }),
            mock.patch.object(
                creator.UIRecoveryAgent,
                "recover",
                side_effect=AssertionError("IP failure reached Anthropic"),
            ) as ai_recover,
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(calls, ["macrodroid+adb"])
        # The checker already spent both bounded phases, so the generic
        # failure path pauses instead of manufacturing a third action.
        self.assertEqual(app.stop.waits, [])
        self.assertEqual(app.bulk_state["stage"], "paused")
        self.assertEqual(
            app.bulk_state["failure_code"], creator.IP_RETRY_EXHAUSTED_CODE
        )
        self.assertEqual(app.bulk_state["slot_id"], "a0262")
        self.assertEqual(app.bulk_state["queue"], original_queue)
        self.assertEqual(app.bulk_state["remaining"], 1)
        ai_recover.assert_not_called()
        self.assertEqual(
            app.bulk_state["ip_reset_attempt_count"],
            creator.MAX_IP_RESET_ATTEMPTS_PER_HEAD,
        )
        self.assertTrue(any("without another webhook" in line for line in logs))

    def test_ip_retry_marker_survives_worker_recreation_without_new_allowance(self):
        first = self.app(1)
        first.bulk_state["slot_id"] = "a0262"
        first._set_diag = lambda *_args: None
        with mock.patch.object(creator, "save_bulk_state", lambda _state: None):
            self.assertTrue(creator.App._retry_ip_after_failure(
                first, "Proxy exit did not change."
            ))
        durable = dict(first.bulk_state)
        self.assertEqual(
            durable["failure_code"], creator.IP_AUTO_RETRY_MARKER_CODE
        )

        reopened = self.app(1)
        reopened.bulk_state = durable
        reopened._set_diag = lambda *_args: None
        reopened._pause_bulk = creator.App._pause_bulk.__get__(reopened, creator.App)
        with mock.patch.object(creator, "save_bulk_state", lambda _state: None):
            self.assertFalse(creator.App._retry_ip_after_failure(
                reopened, "Proxy exit still did not change."
            ))
        self.assertEqual(reopened.stop.waits, [])
        self.assertEqual(reopened.bulk_state["stage"], "paused")
        self.assertEqual(
            reopened.bulk_state["failure_code"], creator.IP_AUTO_RETRY_MARKER_CODE
        )
        self.assertEqual(reopened.bulk_state["slot_id"], "a0262")
        self.assertEqual(reopened.bulk_state["remaining"], 1)

    def test_explicit_retry_preserves_spent_remote_phase_for_adb_handoff(self):
        app = self.app(1)
        checkpoint = trigger_checkpoint("198.51.100.1")
        app.bulk_state.update(
            stage="paused",
            slot_id="a0262",
            baseline_ip=checkpoint["baseline_ip"],
            trigger_deadline_utc=checkpoint["deadline_utc"],
            ip_attempt=checkpoint["attempt"],
            ip_reset_attempt_count=1,
            failure_phase="ip",
            failure_reason="IP reset limit reached.",
            failure_code=creator.IP_RETRY_EXHAUSTED_CODE,
        )
        starts = []
        app._start_bulk_worker = lambda: starts.append(dict(app.bulk_state))
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", return_value={"id": "a0262"}),
        ):
            self.assertTrue(app.retry_bulk_batch(password="offline-password"))
        self.assertEqual(len(starts), 1)
        self.assertEqual(app.bulk_state["stage"], "retry_ip")
        self.assertIsNone(app.bulk_state["failure_phase"])
        self.assertIsNone(app.bulk_state["failure_reason"])
        self.assertEqual(
            app.bulk_state["failure_code"], creator.IP_RETRY_EXHAUSTED_CODE
        )
        self.assertEqual(app.bulk_state["ip_reset_attempt_count"], 1)
        self.assertEqual(
            app.bulk_state["radio_recovery"],
            bulk_creation.empty_radio_recovery(),
        )
        self.assertEqual(app.bulk_state["slot_id"], "a0262")
        self.assertEqual(app.bulk_state["remaining"], 1)

    def test_nonretryable_ip_failure_pauses_without_wait_or_ai(self):
        app = self.app(1)
        app.bulk_state["slot_id"] = "a0262"
        app._set_diag = lambda *_args: None
        app._pause_bulk = creator.App._pause_bulk.__get__(app, creator.App)
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator.UIRecoveryAgent,
                "recover",
                side_effect=AssertionError("configuration failure reached Anthropic"),
            ) as ai_recover,
        ):
            self.assertFalse(creator.App._retry_ip_after_failure(
                app,
                "FalconProxy configuration is invalid.",
                code="macrodroid_setup_failed",
                retryable=False,
            ))
        self.assertEqual(app.stop.waits, [])
        self.assertEqual(app.bulk_state["stage"], "paused")
        self.assertEqual(app.bulk_state["failure_code"], "macrodroid_setup_failed")
        self.assertEqual(app.bulk_state["slot_id"], "a0262")
        self.assertEqual(app.bulk_state["remaining"], 1)
        ai_recover.assert_not_called()

    def test_history_scope_failure_escaping_ip_check_pauses_immediately(self):
        app = self.app(1)
        original_queue = [dict(item) for item in app.bulk_state["queue"]]
        app.bulk_state["slot_id"] = "a0262"
        app._bulk_slot_and_entry = lambda state: (
            "a0262", state["queue"][0], date(2000, 1, 1)
        )
        app._bulk_proxy_scope = mock.Mock(
            side_effect=creator.HistoryError(
                "The private proxy scope could not be established."
            )
        )
        app._set_diag = lambda *_args: None
        app._pause_bulk = creator.App._pause_bulk.__get__(app, creator.App)
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", return_value={
                "id": "a0262", "reddit": False,
            }),
            mock.patch.object(
                creator.UIRecoveryAgent,
                "recover",
                side_effect=AssertionError("history failure reached Anthropic"),
            ) as ai_recover,
        ):
            creator.App._run_bulk_batch(app)
        self.assertEqual(app.stop.waits, [])
        self.assertEqual(app.bulk_state["stage"], "paused")
        self.assertEqual(app.bulk_state["failure_code"], "ip_history_failed")
        self.assertEqual(app.bulk_state["slot_id"], "a0262")
        self.assertEqual(app.bulk_state["queue"], original_queue)
        self.assertEqual(app.bulk_state["remaining"], 1)
        ai_recover.assert_not_called()

    def test_verified_ip_clears_retry_marker_before_creation_resumes(self):
        app = self.app(1)
        app.bulk_state.update(
            slot_id="a0262",
            failure_phase="ip",
            failure_reason="Previous reset did not verify.",
            failure_code=creator.IP_AUTO_RETRY_MARKER_CODE,
        )
        app._bulk_slot_and_entry = lambda state: (
            "a0262", state["queue"][0], date(2000, 1, 1)
        )
        app._run_bulk_ip_check = lambda _state: (
            verified_result("198.51.100.1", "198.51.100.2"), []
        )
        observed = []

        def pipeline(*_args, **_kwargs):
            observed.append({
                "stage": app.bulk_state["stage"],
                "failure_phase": app.bulk_state["failure_phase"],
                "failure_reason": app.bulk_state["failure_reason"],
                "failure_code": app.bulk_state["failure_code"],
            })
            app.stop.set()
            return False

        app._run_pipeline_with_bridge_recovery = pipeline
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", return_value={
                "id": "a0262", "reddit": False,
            }),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", return_value=None),
        ):
            creator.App._run_bulk_batch(app)
        self.assertEqual(observed, [{
            "stage": "creating",
            "failure_phase": None,
            "failure_reason": None,
            "failure_code": None,
        }])

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

    def test_a0227_provisioning_resume_ip_failure_retries_ip_without_delete(self):
        app = self.app(1)
        app.bulk_state.update(
            stage="retry_creation", slot_id="a0227", verified_ip="198.51.100.9",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._bulk_slot_and_entry = lambda state: (
            "a0227", state["queue"][0], date(2000, 1, 1)
        )
        slot = {
            "id": "a0227",
            "ghost": True,
            "ghost_container_state": "provisioning",
            "ghost_container_baseline": [],
            "reddit": False,
        }
        original_slot = dict(slot)
        original_queue = [dict(item) for item in app.bulk_state["queue"]]
        logs = []
        app.log = logs.append
        app._set_diag = lambda *_args: None
        app._reset_failed_creation = mock.Mock(
            side_effect=AssertionError("IP recheck failure reset the a0227 attempt")
        )
        app._stall_reset = mock.Mock(
            side_effect=AssertionError("IP recheck failure deleted a0227")
        )
        real_retry = app._retry_ip_after_failure

        def retry_same_slot(reason, **kwargs):
            result = real_retry(reason, **kwargs)
            app.stop.set()
            return result

        app._retry_ip_after_failure = retry_same_slot
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "run_macrodroid_change", side_effect=AssertionError("duplicate reset")),
            mock.patch.object(creator, "observe_proxy_without_trigger", return_value={"outcome": "verification_failed", "ip": ""}),
            mock.patch.object(creator, "get_slot", return_value=slot),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", side_effect=AssertionError("unverified IP assigned")),
            mock.patch.object(
                creator,
                "delete_named_instance",
                side_effect=AssertionError("IP recheck failure tapped Delete"),
            ),
            mock.patch.object(
                creator,
                "consume_stalled",
                side_effect=AssertionError("IP recheck failure consumed the queue"),
            ),
        ):
            creator.App._run_bulk_batch(app)

        app._reset_failed_creation.assert_not_called()
        app._stall_reset.assert_not_called()
        self.assertEqual(app.bulk_state["stage"], "ip_pending")
        self.assertEqual(app.bulk_state["slot_id"], "a0227")
        self.assertIsNone(app.bulk_state["verified_ip"])
        self.assertEqual(app.bulk_state["queue"], original_queue)
        self.assertEqual(slot, original_slot)
        self.assertEqual(app.stop.waits, [])
        self.assertTrue(any("immediately" in line for line in logs))

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
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", lambda _sid: {"id": "a0001"}),
        ):
            self.assertTrue(app.retry_bulk_batch(password="offline-password"))
        self.assertEqual(len(starts), 1)
        self.assertEqual(app.bulk_state["stage"], "retry_ip")
        self.assertGreater(
            app.bulk_state["ip_attempt"]["observation_deadline_utc"],
            "2000-01-01T00:00:00Z",
        )
        self.assertEqual(app.bulk_state["ip_attempt"]["confirmation_floor"], 1)

    def test_explicit_resume_renews_expired_active_ip_window_without_webhook(self):
        for active_stage in ("may_have_triggered", "retry_ip"):
            with self.subTest(stage=active_stage):
                app = self.app(1)
                checkpoint = trigger_checkpoint("198.51.100.1")
                expired = dict(
                    checkpoint["attempt"],
                    observation_deadline_utc="2000-01-01T00:00:00Z",
                )
                original_queue = [dict(item) for item in app.bulk_state["queue"]]
                app.bulk_state.update(
                    stage=active_stage,
                    slot_id="a0262",
                    baseline_ip=checkpoint["baseline_ip"],
                    trigger_deadline_utc="2000-01-01T00:00:00Z",
                    ip_attempt=expired,
                    ip_reset_attempt_count=1,
                    failure_phase="ip",
                    failure_code=creator.IP_AUTO_RETRY_MARKER_CODE,
                )
                starts = []
                logs = []
                app.log = logs.append
                app._start_bulk_worker = lambda: starts.append(dict(app.bulk_state))
                with (
                    mock.patch.object(creator, "save_bulk_state", lambda _state: None),
                    mock.patch.object(creator, "get_slot", return_value={"id": "a0262"}),
                    mock.patch.object(
                        creator,
                        "run_macrodroid_change",
                        side_effect=AssertionError("resume submitted a webhook"),
                    ) as reset,
                ):
                    self.assertTrue(app.retry_bulk_batch(password="offline-password"))

                self.assertEqual(len(starts), 1)
                self.assertEqual(app.bulk_state["stage"], "retry_ip")
                self.assertEqual(app.bulk_state["slot_id"], "a0262")
                self.assertEqual(app.bulk_state["baseline_ip"], checkpoint["baseline_ip"])
                self.assertEqual(app.bulk_state["queue"], original_queue)
                self.assertEqual(app.bulk_state["remaining"], 1)
                self.assertIsNone(app.bulk_state["failure_phase"])
                self.assertEqual(
                    app.bulk_state["failure_code"],
                    creator.IP_AUTO_RETRY_MARKER_CODE,
                )
                self.assertEqual(app.bulk_state["ip_reset_attempt_count"], 1)
                renewed = datetime.fromisoformat(
                    app.bulk_state["ip_attempt"]["observation_deadline_utc"].replace(
                        "Z", "+00:00"
                    )
                )
                self.assertGreater(renewed, datetime.now(timezone.utc))
                self.assertEqual(
                    app.bulk_state["trigger_deadline_utc"],
                    app.bulk_state["ip_attempt"]["observation_deadline_utc"],
                )
                self.assertTrue(any("no webhook" in line for line in logs))
                reset.assert_not_called()

    def test_expired_resume_accepts_offline_ip_change_without_webhook(self):
        app = self.app(1)
        checkpoint = trigger_checkpoint("198.51.100.1")
        app.bulk_state.update(
            stage="may_have_triggered",
            slot_id="a0262",
            baseline_ip=checkpoint["baseline_ip"],
            trigger_deadline_utc="2000-01-01T00:00:00Z",
            ip_attempt=dict(
                checkpoint["attempt"],
                observation_deadline_utc="2000-01-01T00:00:00Z",
            ),
        )
        app._bulk_slot_and_entry = lambda state: (
            "a0262", state["queue"][0], date(2000, 1, 1)
        )
        app._start_bulk_worker = lambda: None
        created = []
        app._run_creation_pipeline = lambda *_a, **_k: created.append(True) or True
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator,
                "run_macrodroid_change",
                side_effect=AssertionError("offline IP change submitted a webhook"),
            ) as reset,
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.1", "198.51.100.2"),
            ),
            mock.patch.object(
                creator, "get_slot", side_effect=lambda _sid: {"complete": bool(created)}
            ),
            mock.patch.object(
                creator, "_pipeline_slot_complete", lambda slot: slot["complete"]
            ),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", return_value=None),
        ):
            self.assertTrue(app.retry_bulk_batch(password="offline-password"))
            creator.App._run_bulk_batch(app)

        self.assertEqual(created, [True])
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(app.bulk_state["queue"], [])
        reset.assert_not_called()

    def test_same_ip_after_renewed_resume_uses_adb_once_then_pauses(self):
        app = self.app(1)
        checkpoint = trigger_checkpoint("198.51.100.1")
        app.bulk_state.update(
            stage="may_have_triggered",
            slot_id="a0262",
            baseline_ip=checkpoint["baseline_ip"],
            trigger_deadline_utc="2000-01-01T00:00:00Z",
            ip_attempt=dict(
                checkpoint["attempt"],
                observation_deadline_utc="2000-01-01T00:00:00Z",
            ),
            ip_reset_attempt_count=1,
            failure_phase="ip",
            failure_code=creator.IP_AUTO_RETRY_MARKER_CODE,
        )
        app._bulk_slot_and_entry = lambda state: (
            "a0262", state["queue"][0], date(2000, 1, 1)
        )
        app._start_bulk_worker = lambda: None
        app._set_diag = lambda *_args: None
        app._pause_bulk = creator.App._pause_bulk.__get__(app, creator.App)
        app._android_radio_config = lambda: (None, "test-xiaomi")
        unchanged = {
            "outcome": "no_change",
            "error": "No IP change observed before the deadline.",
            "error_category": "",
        }
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", return_value={
                "id": "a0262", "reddit": False,
            }),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(
                creator,
                "run_macrodroid_change",
                side_effect=AssertionError("resume submitted another webhook"),
            ) as reset,
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                side_effect=[dict(unchanged), dict(unchanged)],
            ) as observe,
            mock.patch.object(
                creator, "reset_android_cellular_radio",
                side_effect=complete_radio_cycle,
            ) as adb_reset,
            mock.patch.object(
                creator, "restore_android_cellular_radio",
                side_effect=AssertionError("fresh fallback used restore-only path"),
            ) as adb_restore,
            mock.patch.object(
                creator.UIRecoveryAgent, "recover",
                side_effect=AssertionError("IP failure reached Anthropic"),
            ) as ai_recover,
        ):
            self.assertTrue(app.retry_bulk_batch(password="offline-password"))
            renewed_deadline = app.bulk_state["ip_attempt"][
                "observation_deadline_utc"
            ]
            creator.App._run_bulk_batch(app)

        self.assertGreater(renewed_deadline, "2000-01-01T00:00:00Z")
        self.assertEqual(app.stop.waits, [])
        self.assertEqual(app.bulk_state["stage"], "paused")
        self.assertEqual(
            app.bulk_state["failure_code"], creator.IP_RETRY_EXHAUSTED_CODE
        )
        self.assertEqual(
            app.bulk_state["ip_reset_attempt_count"],
            2,
        )
        self.assertEqual(app.bulk_state["radio_recovery"]["state"], "completed")
        self.assertEqual(app.bulk_state["slot_id"], "a0262")
        self.assertEqual(app.bulk_state["remaining"], 1)
        reset.assert_not_called()
        adb_reset.assert_called_once()
        adb_restore.assert_not_called()
        self.assertEqual(observe.call_count, 2)
        ai_recover.assert_not_called()

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
            supplied_triggerer = kwargs.pop("triggerer", None)
            if supplied_triggerer is not None:
                def triggerer(*args):
                    events.append("adb-trigger")
                    return supplied_triggerer(*args)
            else:
                triggerer = lambda *_args: events.append("trigger") or {
                    "acknowledged": True,
                }
            return ip_change.run_macrodroid_change(
                webhook, proxy, **kwargs, fetcher=fetch, monotonic=clock.monotonic,
                now=clock.now, waiter=lambda seconds: clock.advance(seconds) or False,
                triggerer=triggerer,
            )
        app._run_creation_pipeline = lambda *_a, **_k: events.append("create-and-close") or True
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(creator, "save_bulk_state", lambda state: bulk_creation.save_state(state, Path(folder) / "queue.json")),
            mock.patch.object(creator, "run_macrodroid_change", run),
            mock.patch.object(
                creator, "reset_android_cellular_radio",
                side_effect=complete_radio_cycle,
            ),
            mock.patch.object(creator, "get_slot", lambda _sid: {"complete": "create-and-close" in events}),
            mock.patch.object(creator, "_pipeline_slot_complete", lambda slot: slot["complete"]),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", lambda sid, ip, at: events.append((sid, ip, at))),
        ):
            app._run_bulk_batch()
            self.assertEqual(bulk_creation.load_state(Path(folder) / "queue.json")["remaining"], 0)
        self.assertEqual(events, ["adb-trigger", ("a0046", "31.217.14.168", "2026-10-16T12:00:03.500000Z"), "create-and-close"])

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

    def test_expired_restart_and_changed_scope_route_to_retry_without_requests(self):
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
                retries = []
                app._retry_ip_after_failure = lambda message, **kwargs: retries.append(
                    (message, kwargs.get("code"))
                ) or False
                with (
                    mock.patch.object(creator, "run_macrodroid_change", side_effect=AssertionError("reset")),
                    mock.patch.object(creator, "observe_proxy_without_trigger", side_effect=AssertionError("read")),
                ):
                    app._run_bulk_batch()
                self.assertEqual(len(retries), 1)
                self.assertEqual(retries[0][1], "unexpected_local_failure")
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
