"""Offline tests for bulk preserve-and-skip recovery and inactivity cancellation.

Everything here is offline: mocked slots persistence, a fake monotonic clock,
and stubbed device/IP flows.  No phone, no Crane, no live state files.
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import date
from unittest import mock

from friend_poster.apps import creator
from friend_poster.core import bulk_creation, slots
from friend_poster.core.slots import CreationProgressClock
from test_bulk_creator import Root, Stop, trigger_checkpoint, verified_result


class FakeMonotonic:
    """Injectable monotonic source for the progress clock and watchdog."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def make_app(case, count=1):
    """A minimal offline Creator App for the batch loop."""
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
    app._record_bulk_ip = (
        lambda result, _observations: result["verification_evidence"]["confirmations"][
            -1
        ]["observed_at_utc"]
    )
    app._pause_bulk = lambda phase, reason, **_kwargs: case.fail(
        f"unexpected pause: {phase}: {reason}"
    )
    app.bulk_state = bulk_creation.empty_state()
    app.bulk_state.update(
        queue=[
            {"birth_date": f"01.01.{2002 + i}", "swipes": 10 + i, "delay_seconds": 2.0}
            for i in range(count)
        ],
        requested=count,
        remaining=count,
        stage="ip_pending",
    )
    return app


class PreserveSkipSlotTests(unittest.TestCase):
    def test_mark_bulk_skipped_preserves_container_alias_and_credentials(self):
        row = {
            "id": "a0047",
            "email": "keep-4f@icloud.com",
            "email_label": "a0047",
            "ghost": True,
            "ghost_container_state": "ready",
            "ghost_container_instance": "instance-proof",
            "reddit": False,
            "reddit_birth_date": "22.05.2003",
        }
        rows = [row]
        with (
            mock.patch.object(slots, "load_slots", lambda: rows),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            out = slots.mark_bulk_skipped(
                "a0047", "reddit_email_field_invalid", "Email field still invalid.", None
            )

        self.assertEqual(out["email"], "keep-4f@icloud.com")
        self.assertEqual(out["ghost_container_instance"], "instance-proof")
        self.assertTrue(out["ghost"])
        skip = out["bulk_skip"]
        self.assertEqual(skip["slot_id"], "a0047")
        self.assertEqual(skip["code"], "reddit_email_field_invalid")
        self.assertEqual(skip["reason"], "Email field still invalid.")
        self.assertTrue(str(skip["skipped_at_utc"]).endswith("Z"))
        self.assertFalse(skip["created_account"])
        self.assertTrue(slots.slot_bulk_skipped(row))
        self.assertTrue(slots.slot_creation_unavailable(row))

    def test_mark_bulk_skipped_accepts_a_created_account_without_raising(self):
        row = {
            "id": "a0047",
            "reddit": True,
            "email": "keep-4f@icloud.com",
            "username": "QuietMoon49",
        }
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            out = slots.mark_bulk_skipped(
                "a0047", "post_account_cleanup_failed", "Cleanup failed.", None
            )

        self.assertTrue(out["reddit"])
        self.assertEqual(out["username"], "QuietMoon49")
        self.assertTrue(out["bulk_skip"]["created_account"])

        # The older terminal-creation marker still refuses a created account;
        # the preserve-and-skip record deliberately does not.
        with (
            mock.patch.object(slots, "load_slots", lambda: [dict(row)]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
            self.assertRaises(ValueError),
        ):
            slots.mark_creation_terminal_failure("a0047", "x_failure", "reason", None)

    def test_bulk_skip_and_terminal_rows_both_reserve_the_slot_id(self):
        skipped = {
            "id": "a0047",
            "bulk_skip": {
                "slot_id": "a0047",
                "code": "reddit_email_field_invalid",
                "reason": "Email field still invalid.",
                "skipped_at_utc": "2026-10-17T00:00:00Z",
            },
        }
        with (
            mock.patch.object(slots, "load_slots", lambda: [skipped]),
            mock.patch.object(slots, "retired_indices", lambda: set()),
        ):
            self.assertIn(47, slots.indices_used())
            self.assertTrue(slots.slot_creation_unavailable(skipped))

        placeholder = {"id": "a0047", "ghost_container_state": "needed"}
        with (
            mock.patch.object(slots, "load_slots", lambda: [placeholder]),
            mock.patch.object(slots, "retired_indices", lambda: set()),
        ):
            self.assertNotIn(47, slots.indices_used())

    def test_unavailable_predicate_covers_both_records_only(self):
        self.assertFalse(slots.slot_creation_unavailable(None))
        self.assertFalse(slots.slot_creation_unavailable({"id": "a0047"}))
        self.assertTrue(
            slots.slot_creation_unavailable(
                {"creation_terminal_failure": {"code": "x_failure"}}
            )
        )
        self.assertTrue(slots.slot_bulk_skipped({"bulk_skip": {"code": "x_failure"}}))
        self.assertFalse(slots.slot_bulk_skipped({"bulk_skip": {"code": "   "}}))


class ProgressClockTests(unittest.TestCase):
    def test_clock_is_monotonic_and_only_a_note_resets_it(self):
        ticks = [1000.0]
        clock = CreationProgressClock(time_fn=lambda: ticks[0])
        self.assertEqual(clock.idle_seconds(), 0.0)
        ticks[0] += 299.0
        self.assertAlmostEqual(clock.idle_seconds(), 299.0)
        clock.note()
        self.assertEqual(clock.idle_seconds(), 0.0)
        ticks[0] += 300.0
        self.assertAlmostEqual(clock.idle_seconds(), 300.0)

    def test_only_a_verified_swipe_advance_resets_the_clock(self):
        fake = FakeMonotonic()
        clock = CreationProgressClock(time_fn=fake)
        row = {
            "id": "a0047",
            "reddit": True,
            "creation_feed_swipes_target": 5,
            "creation_feed_swipes_completed": 2,
        }
        with (
            mock.patch.object(slots, "CREATION_PROGRESS", clock),
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            fake.advance(250.0)
            # Re-writing the same completed count is a retry, not progress.
            slots.mark_creation_feed_swipe_completed("a0047", 2, None)
            self.assertAlmostEqual(clock.idle_seconds(), 250.0)
            slots.mark_creation_feed_swipe_completed("a0047", 3, None)
            self.assertEqual(clock.idle_seconds(), 0.0)

    def test_a_repeated_verification_phase_does_not_reset_the_clock(self):
        fake = FakeMonotonic()
        clock = CreationProgressClock(time_fn=fake)
        row = {"id": "a0047", "reddit": False}
        with (
            mock.patch.object(slots, "CREATION_PROGRESS", clock),
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            slots.mark_email_verification("a0047", 0, "a0047", "submitted", None)
            fake.advance(120.0)
            slots.mark_email_verification("a0047", 0, "a0047", "submitted", None)
            self.assertAlmostEqual(clock.idle_seconds(), 120.0)
            slots.mark_email_verification("a0047", 0, "a0047", "timed_out", None)
            self.assertEqual(clock.idle_seconds(), 0.0)


class InactivityWatchTests(unittest.TestCase):
    def test_watchdog_fires_only_at_the_three_minute_boundary(self):
        self.assertEqual(creator.BULK_INACTIVITY_SECONDS, 180.0)
        fake = FakeMonotonic()
        clock = CreationProgressClock(time_fn=fake)
        event = threading.Event()
        watch = creator._InactivityWatch(
            event,
            creator.BULK_INACTIVITY_SECONDS,
            clock,
            lambda _m: None,
            "a0047",
            poll_seconds=0.002,
        )
        watch.start()
        try:
            fake.advance(179.0)
            time.sleep(0.06)
            self.assertFalse(event.is_set())
            fake.advance(1.0)
            deadline = time.monotonic() + 2.0
            while not event.is_set() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(event.is_set())
            self.assertTrue(watch.fired)
        finally:
            watch.stop()

    def test_watchdog_never_fires_while_verified_progress_continues(self):
        fake = FakeMonotonic()
        clock = CreationProgressClock(time_fn=fake)
        event = threading.Event()
        watch = creator._InactivityWatch(
            event,
            creator.BULK_INACTIVITY_SECONDS,
            clock,
            lambda _m: None,
            "a0047",
            poll_seconds=0.002,
        )
        watch.start()
        try:
            # 60 * 10s = 600 simulated seconds, well past the 180s window, but
            # a real verified transition keeps arriving so this is not a total
            # account-duration limit.
            for _ in range(60):
                fake.advance(10.0)
                time.sleep(0.008)
                clock.note()
            time.sleep(0.06)
            self.assertFalse(event.is_set())
            self.assertFalse(watch.fired)
        finally:
            watch.stop()

    def test_user_stop_and_timeout_share_one_cancel_view(self):
        user = threading.Event()
        timeout = threading.Event()
        cancel = creator._AttemptCancel(user, timeout)
        self.assertFalse(cancel.is_set())
        user.set()
        self.assertTrue(cancel.is_set())
        self.assertTrue(cancel.wait(1.0))

    def test_a_new_attempt_does_not_inherit_the_previous_timeout(self):
        user = threading.Event()
        old_timeout = threading.Event()
        old = creator._AttemptCancel(user, old_timeout)
        old_timeout.set()
        self.assertTrue(old.is_set())

        new = creator._AttemptCancel(user, threading.Event())
        self.assertFalse(new.is_set())
        self.assertFalse(new.wait(0.0))

    def test_plain_phone_error_timeout_is_classified_and_preserved(self):
        app = make_app(self, 1)
        app.status = mock.Mock()

        class Fires:
            def __init__(self, event, *_args, **_kwargs):
                self._event = event

            def start(self):
                self._event.set()

            def stop(self):
                pass

        with mock.patch.object(creator, "_InactivityWatch", Fires):
            ok = creator.App._run_creation_pipeline(
                app,
                "offline-password",
                "a0047",
                date(2005, 4, 20),
                10,
                manage_worker=False,
            )

        self.assertFalse(ok)
        self.assertTrue(app._last_pipeline_attempt_timed_out)
        self.assertFalse(app._last_pipeline_failure_shared)
        self.assertEqual(
            app._last_pipeline_failure_code, creator.BULK_INACTIVITY_TIMEOUT_CODE
        )
        self.assertIn("No verified creation progress", app._last_pipeline_failure_reason)


class PreserveSkipControllerTests(unittest.TestCase):
    @staticmethod
    def harness(case, count=1):
        return make_app(case, count)

    def test_already_skipped_head_is_consumed_exactly_once(self):
        app = self.harness(self, 1)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        entry = app.bulk_state["queue"][0]
        existing = {
            "id": "a0047",
            "reddit": False,
            "bulk_skip": {
                "slot_id": "a0047",
                "code": "reddit_email_field_invalid",
                "reason": "Email field still invalid.",
                "skipped_at_utc": "2026-10-17T00:00:00Z",
            },
        }
        with (
            mock.patch.object(creator, "get_slot", return_value=existing),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
        ):
            self.assertTrue(
                creator.App._skip_failed_bulk_slot(app, "a0047", entry, "ignored replay")
            )

        skipped.assert_not_called()
        self.assertEqual(len(app.bulk_state["failures"]), 1)
        self.assertEqual(
            app.bulk_state["failures"][0]["code"], "reddit_email_field_invalid"
        )
        self.assertEqual(app.bulk_state["remaining"], 0)

    def test_batch_resume_pauses_on_an_already_skipped_head(self):
        app = self.harness(self, 2)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        skipped_row = {
            "id": "a0047",
            "reddit": False,
            "bulk_skip": {
                "slot_id": "a0047",
                "code": "reddit_email_field_invalid",
                "reason": "Email field still invalid.",
                "skipped_at_utc": "2026-10-17T00:00:00Z",
            },
        }
        next_row = {
            "id": "a0048",
            "reddit": True,
            "username": "",
            "email": "next@icloud.com",
            "complete": True,
        }
        pipelines = []
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )
        app._run_creation_pipeline = lambda _password, sid, *_a, **_k: (
            pipelines.append(sid) or True
        )
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator,
                "get_slot",
                side_effect=lambda sid: skipped_row if sid == "a0047" else next_row,
            ),
            mock.patch.object(creator, "load_slots", lambda: [skipped_row]),
            mock.patch.object(creator, "retired_indices", lambda: set()),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", lambda _sid, _log: None
            ),
            mock.patch.object(
                creator,
                "_pipeline_slot_complete",
                side_effect=lambda row: bool(row and row.get("complete")),
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
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", lambda *_a: None),
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(pipelines, [])
        self.assertEqual(pauses[0][2], "reddit_email_field_invalid")
        self.assertEqual(app.bulk_state["failures"], [])
        self.assertEqual(app.bulk_state["remaining"], 2)
        self.assertEqual(app.bulk_state["slot_id"], "a0047")
        self.assertEqual(len(app.bulk_state["queue"]), 2)

    def test_shared_failure_waits_three_minutes_then_resets_every_attempt(self):
        app = self.harness(self, 2)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        discarded = []
        app._discard_stalled_container = (
            lambda sid: discarded.append(sid) or "deleted"
        )

        def shared(*_args, **_kwargs):
            app._last_pipeline_failure_shared = True
            app._last_pipeline_failure_code = "ssh_auth_failed"
            app._last_pipeline_failure_reason = "iPhone SSH authentication failed."
            return False

        app._run_creation_pipeline = shared
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", return_value={"complete": False}),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(creator, "load_slots", return_value=[]),
            mock.patch.object(creator, "retired_indices", return_value={47}),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", lambda _sid, _log: None
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
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", lambda *_a: None),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(discarded, ["a0047", "a0048"])
        self.assertEqual(skipped.call_count, 2)
        self.assertEqual(
            [
                seconds
                for seconds in app.stop.waits
                if seconds == creator.BULK_FAILURE_GRACE_SECONDS
            ],
            [creator.BULK_FAILURE_GRACE_SECONDS] * 2,
        )
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(app.bulk_state["queue"], [])

    def test_three_minute_timeout_deletes_container_and_resets_the_attempt(self):
        # No verified progress for three minutes is a dead attempt: the stale
        # container is deleted, the slot is retired and the queue head is
        # dropped without keeping its credentials.  The batch continues with
        # the next identity instead of pausing.
        app = self.harness(self, 1)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        discarded = []
        app._discard_stalled_container = (
            lambda sid: discarded.append(sid) or "deleted"
        )

        def timed_out(*_args, **_kwargs):
            app._last_pipeline_attempt_timed_out = True
            app._last_pipeline_failure_reason = (
                "No verified creation progress for 180 seconds."
            )
            return False

        app._run_creation_pipeline = timed_out
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", return_value={"complete": False}),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", lambda _sid, _log: None
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(discarded, ["a0047"])
        skipped.assert_called_once()
        self.assertEqual(skipped.call_args.args[0], "a0047")
        self.assertEqual(skipped.call_args.args[1], creator.STALL_RESET_FAILURE_CODE)
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(app.bulk_state["queue"], [])
        self.assertIsNone(app.bulk_state["slot_id"])
        self.assertEqual(app.bulk_state["stage"], "idle")
        # Nothing from the abandoned account is archived.
        self.assertEqual(app.bulk_state["failures"], [])

    def test_missing_crane_doorway_resets_immediately_without_failure_grace(self):
        app = self.harness(self, 1)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        discarded = []
        app._discard_stalled_container = (
            lambda sid: discarded.append(sid) or "deleted"
        )
        app.status = mock.Mock()
        app.anthropic_recovery_enabled_var = True
        app._pending_ui_recovery = None
        app._pending_ui_recovery_agent = None
        app._pending_ui_recovery_chain = None
        app._report_attempt_failure = lambda *_args, **_kwargs: None

        slot = {
            "id": "a0047",
            "ghost": True,
            "ghost_container_state": "ready",
            "ghost_container_instance": "INSTANCE-A0047",
            "crane_container_instance": "INSTANCE-A0047-FULL",
            "email": "masked@icloud.com",
            "reddit_birth_date": "01.01.2002",
            "reddit": False,
        }
        phone = mock.Mock()
        phone.progress_note = None
        stack = mock.Mock()
        stack.cancel_event = None

        def connect(_password):
            app.phone = phone
            return phone, stack

        app._connect = connect

        class QuietWatch:
            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", return_value=slot),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", lambda _sid, _log: None
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(creator, "_InactivityWatch", QuietWatch),
            mock.patch.object(creator, "ghost_container_state", return_value="ready"),
            mock.patch.object(creator, "slot_container_available", return_value=True),
            mock.patch.object(
                creator,
                "run_create_reddit",
                side_effect=creator.CraneDoorwayUnavailable(
                    "stock Reddit haptic had no Crane doorway"
                ),
            ),
            mock.patch.object(creator, "UIRecoveryAgent") as agent,
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
        ):
            creator.App._run_bulk_batch(app)

        agent.assert_not_called()
        self.assertEqual(discarded, ["a0047"])
        skipped.assert_called_once()
        self.assertNotIn(creator.BULK_FAILURE_GRACE_SECONDS, app.stop.waits)
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(app.bulk_state["queue"], [])
        self.assertEqual(app.bulk_state["stage"], "idle")

    def test_missing_doorway_e2e_resets_once_then_starts_fresh_slot_and_ip(self):
        app = self.harness(self, 2)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app.status = mock.Mock()
        app.anthropic_recovery_enabled_var = True
        app._pending_ui_recovery = None
        app._pending_ui_recovery_agent = None
        app._pending_ui_recovery_chain = None
        app._report_attempt_failure = lambda *_args, **_kwargs: None
        app._bulk_live_status = lambda _message: None
        app._bridge_health_check = lambda _password: None

        rows = {
            "a0047": {
                "id": "a0047",
                "ghost": True,
                "ghost_container_state": "ready",
                "ghost_container_instance": "ABC0047X",
                "crane_container_instance": "ABC0047X-FULL",
                "email": "masked47@icloud.com",
                "reddit_birth_date": "01.01.2002",
                "reddit": False,
            }
        }
        connections = []
        checkpoint_writes = []
        skipped = []
        allocated = []
        container_creates = []
        ip_changes = []
        slot_ips = []
        budget_instances = []
        pipeline_budget_trace = []

        def connect(_password):
            phone = mock.Mock()
            phone.progress_note = None
            stack = mock.Mock()
            stack.cancel_event = None
            app.phone = phone
            connections.append((phone, stack))
            return phone, stack

        app._connect = connect

        def ensure_needed(sid, _log):
            allocated.append(sid)
            if sid not in rows:
                rows[sid] = {
                    "id": sid,
                    "ghost": False,
                    "ghost_container_state": "needed",
                    "reddit": False,
                }
            return rows[sid]

        def mark_pending(sid, instance_id, _log):
            checkpoint_writes.append(("pending", sid, instance_id))
            rows[sid]["bulk_reset_cleanup"] = {
                "slot_id": sid,
                "instance_id": instance_id,
                "state": "pending",
                "requested_at_utc": "2026-10-21T12:00:00Z",
            }

        def mark_complete(sid, _log):
            checkpoint_writes.append(("complete", sid))
            rows[sid]["bulk_reset_cleanup"]["state"] = "complete"

        def mark_skipped(sid, code, reason, _log):
            skipped.append((sid, code))
            rows[sid]["bulk_skip"] = {
                "slot_id": sid,
                "code": code,
                "reason": reason,
                "skipped_at_utc": "2026-10-21T12:00:01Z",
            }

        def set_birth(sid, birth):
            rows[sid]["reddit_birth_date"] = birth
            return rows[sid]

        def create_next_container(_phone, _stack, _log, **kwargs):
            sid = kwargs["names"][0]
            container_creates.append(sid)
            rows[sid].update(
                ghost=True,
                ghost_container_state="ready",
                ghost_container_instance="ABC0048X",
            )
            # End the fixture after proving that the successor reached its new
            # container step. The outer batch must preserve that second head.
            app.stop.set()
            return [sid]

        def change_ip(*_args, **_kwargs):
            ip_changes.append(("198.51.100.47", "198.51.100.48"))
            return verified_result("198.51.100.47", "198.51.100.48")

        def save_slot_ip(sid, ip, _confirmed):
            slot_ips.append((sid, ip))

        def missing_doorway(sid, *_args, **_kwargs):
            self.assertEqual(sid, "a0047")
            raise creator.CraneDoorwayUnavailable(
                "stock Reddit haptic had no Crane doorway"
            )

        original_budget = creator.RecoveryAttemptBudget

        class CountingBudget(original_budget):
            def __post_init__(self):
                super().__post_init__()
                budget_instances.append(self)

        original_pipeline = app._run_creation_pipeline
        first_transport_loss = {"pending": True}

        def pipeline_with_one_reconnect(*args, **kwargs):
            sid = args[1]
            budget = kwargs["recovery_budget"]
            pipeline_budget_trace.append((sid, id(budget)))
            if sid == "a0047" and first_transport_loss["pending"]:
                first_transport_loss["pending"] = False
                raise creator.BridgeTransportError("simulated USB reconnect")
            return original_pipeline(*args, **kwargs)

        app._run_creation_pipeline = pipeline_with_one_reconnect

        class QuietWatch:
            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        real_consume_stalled = creator.consume_stalled
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", side_effect=lambda sid: rows.get(sid)),
            mock.patch.object(creator, "load_slots", side_effect=lambda: list(rows.values())),
            mock.patch.object(creator, "retired_indices", return_value=set()),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", side_effect=ensure_needed
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(creator, "run_macrodroid_change", side_effect=change_ip),
            mock.patch.object(
                creator, "set_slot_falcon_proxy_ip", side_effect=save_slot_ip
            ),
            mock.patch.object(creator, "set_slot_birth_date", side_effect=set_birth),
            mock.patch.object(creator, "_InactivityWatch", QuietWatch),
            mock.patch.object(creator, "run_create_reddit", side_effect=missing_doorway),
            mock.patch.object(
                creator, "apply_reddit_containers", side_effect=create_next_container
            ),
            mock.patch.object(
                creator, "mark_bulk_reset_cleanup_pending", side_effect=mark_pending
            ),
            mock.patch.object(
                creator, "mark_bulk_reset_cleanup_complete", side_effect=mark_complete
            ),
            mock.patch.object(creator, "mark_bulk_skipped", side_effect=mark_skipped),
            mock.patch.object(
                creator, "delete_named_instance", return_value="deleted"
            ) as delete,
            mock.patch.object(
                creator, "consume_stalled", wraps=real_consume_stalled
            ) as consume,
            mock.patch.object(creator, "RecoveryAttemptBudget", CountingBudget),
            mock.patch.object(
                creator,
                "UIRecoveryAgent",
                side_effect=AssertionError(
                    "stock-haptic missing-doorway recovery must use zero AI calls"
                ),
            ) as agent,
            mock.patch.object(creator, "create_one_hide_my_email") as create_email,
        ):
            creator.App._run_bulk_batch(app)

        agent.assert_not_called()
        create_email.assert_not_called()
        delete.assert_called_once()
        consume.assert_called_once()
        self.assertEqual(
            checkpoint_writes,
            [
                ("pending", "a0047", "ABC0047X"),
                ("complete", "a0047"),
            ],
        )
        self.assertEqual(
            skipped, [("a0047", creator.STALL_RESET_FAILURE_CODE)]
        )
        self.assertNotIn(creator.BULK_FAILURE_GRACE_SECONDS, app.stop.waits)
        self.assertEqual(ip_changes, [("198.51.100.47", "198.51.100.48")])
        self.assertEqual(slot_ips, [("a0048", "198.51.100.48")])
        self.assertEqual(container_creates, ["a0048"])
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["slot_id"], "a0048")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

        # The reconnect reuses one attempt budget; the successor receives one
        # new budget because it is a genuinely different queue attempt. No
        # budget was spent because UIRecoveryAgent was never constructed.
        self.assertEqual([sid for sid, _budget in pipeline_budget_trace], [
            "a0047", "a0047", "a0048"
        ])
        self.assertEqual(
            pipeline_budget_trace[0][1], pipeline_budget_trace[1][1]
        )
        self.assertNotEqual(
            pipeline_budget_trace[1][1], pipeline_budget_trace[2][1]
        )
        self.assertEqual(len(budget_instances), 2)
        self.assertTrue(all(budget.api_calls == 0 for budget in budget_instances))

    def test_stalled_container_uses_recorded_uuid_once_without_ai(self):
        app = self.harness(self, 1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        app._bulk_live_status = lambda _message: None
        phone = mock.Mock()
        stack = mock.Mock()
        app._connect = lambda _password: (phone, stack)
        row = {
            "id": "a0047",
            "ghost": True,
            "ghost_container_instance": "GHOST-1",
        }
        order = []

        class NeverAgent:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("stall cleanup must never construct AI")

        with (
            mock.patch.object(creator, "get_slot", return_value=row),
            mock.patch.object(
                creator,
                "mark_bulk_reset_cleanup_pending",
                side_effect=lambda sid, iid, _log: order.append(
                    ("pending", sid, iid)
                ),
            ),
            mock.patch.object(
                creator,
                "delete_named_instance",
                side_effect=lambda _stack, _phone, _log, sid, iid, **_kwargs: (
                    order.append(("delete", sid, iid)) or "deleted"
                ),
            ) as delete,
            mock.patch.object(
                creator, "mark_bulk_reset_cleanup_complete",
                side_effect=lambda sid, _log: order.append(("complete", sid)),
            ),
            mock.patch.object(creator, "resolve_sole_user_container") as resolve,
            mock.patch.object(creator, "UIRecoveryAgent", NeverAgent),
        ):
            outcome = creator.App._discard_stalled_container(app, "a0047")

        self.assertEqual(outcome, "deleted")
        self.assertEqual(
            order,
            [
                ("pending", "a0047", "GHOST-1"),
                ("delete", "a0047", "GHOST-1"),
                ("complete", "a0047"),
            ],
        )
        delete.assert_called_once()
        resolve.assert_not_called()
        stack.press_home.assert_not_called()
        phone.close.assert_called_once_with()
        self.assertIsNone(app.phone)

    def test_stalled_container_exact_delete_failure_is_not_retried(self):
        app = self.harness(self, 1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        app._bulk_live_status = lambda _message: None
        phone = mock.Mock()
        stack = mock.Mock()
        app._connect = lambda _password: (phone, stack)

        with (
            mock.patch.object(
                creator,
                "get_slot",
                return_value={
                    "id": "a0047",
                    "ghost": True,
                    "ghost_container_instance": "GHOST-1",
                },
            ),
            mock.patch.object(
                creator, "mark_bulk_reset_cleanup_pending"
            ) as pending,
            mock.patch.object(
                creator,
                "delete_named_instance",
                side_effect=creator.PhoneError("recorded UUID not found"),
            ) as delete,
            mock.patch.object(
                creator, "mark_bulk_reset_cleanup_complete"
            ) as complete,
        ):
            outcome = creator.App._discard_stalled_container(app, "a0047")

        self.assertTrue(outcome.startswith("failed (PhoneError"))
        pending.assert_called_once_with("a0047", "GHOST-1", app.log)
        delete.assert_called_once()
        complete.assert_not_called()
        stack.press_home.assert_not_called()
        phone.close.assert_called_once_with()
        self.assertIsNone(app.phone)

    def test_stalled_container_without_uuid_resolves_then_checkpoints_exact_uuid(self):
        app = self.harness(self, 1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        app._bulk_live_status = lambda _message: None
        phone = mock.Mock()
        stack = mock.Mock()
        app._connect = lambda _password: (phone, stack)
        order = []

        class NeverAgent:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("stall cleanup must never construct AI")

        with (
            mock.patch.object(
                creator,
                "get_slot",
                return_value={"id": "a0047", "ghost": True},
            ),
            mock.patch.object(
                creator,
                "resolve_sole_user_container",
                side_effect=lambda *_a, **kwargs: (
                    order.append(("resolve", kwargs.get("expected_name")))
                    or ("a0047", "RESOLVED-47")
                ),
            ) as resolve,
            mock.patch.object(
                creator,
                "invalidate_ghost_container_cache",
                side_effect=lambda *_a, **_k: order.append(("invalidate",)),
            ) as invalidate,
            mock.patch.object(
                creator,
                "mark_bulk_reset_cleanup_pending",
                side_effect=lambda sid, iid, _log: order.append(
                    ("pending", sid, iid)
                ),
            ),
            mock.patch.object(
                creator,
                "delete_named_instance",
                side_effect=lambda _stack, _phone, _log, sid, iid, **_kwargs: (
                    order.append(("delete", sid, iid)) or "deleted"
                ),
            ) as delete,
            mock.patch.object(
                creator, "mark_bulk_reset_cleanup_complete",
                side_effect=lambda sid, _log: order.append(("complete", sid)),
            ),
            mock.patch.object(creator, "UIRecoveryAgent", NeverAgent),
        ):
            outcome = creator.App._discard_stalled_container(app, "a0047")

        self.assertEqual(outcome, "deleted")
        self.assertEqual(
            order,
            [
                ("invalidate",),
                ("resolve", "a0047"),
                ("pending", "a0047", "RESOLVED-47"),
                ("delete", "a0047", "RESOLVED-47"),
                ("complete", "a0047"),
            ],
        )
        invalidate.assert_called_once_with(stack, app.log)
        resolve.assert_called_once()
        delete.assert_called_once()
        stack.press_home.assert_not_called()
        phone.close.assert_called_once_with()

    def test_stalled_container_pending_resume_reuses_checkpoint_uuid(self):
        app = self.harness(self, 1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        app._bulk_live_status = lambda _message: None
        phone = mock.Mock()
        stack = mock.Mock()
        app._connect = lambda _password: (phone, stack)
        row = {
            "id": "a0047",
            "ghost": True,
            "bulk_reset_cleanup": {
                "slot_id": "a0047",
                "instance_id": "CHECKPOINT-47",
                "state": "pending",
                "requested_at_utc": "2026-10-21T12:00:00Z",
            },
        }

        with (
            mock.patch.object(creator, "get_slot", return_value=row),
            mock.patch.object(creator, "resolve_sole_user_container") as resolve,
            mock.patch.object(
                creator, "mark_bulk_reset_cleanup_pending"
            ) as pending,
            mock.patch.object(
                creator, "delete_named_instance", return_value="absent"
            ) as delete,
            mock.patch.object(
                creator, "mark_bulk_reset_cleanup_complete"
            ) as complete,
        ):
            outcome = creator.App._discard_stalled_container(app, "a0047")

        self.assertEqual(outcome, "absent")
        resolve.assert_not_called()
        pending.assert_not_called()
        delete.assert_called_once_with(
            stack,
            phone,
            app.log,
            "a0047",
            "CHECKPOINT-47",
            stop=app.stop,
        )
        complete.assert_called_once_with("a0047", app.log)

    def test_stalled_container_complete_checkpoint_skips_connection_and_delete(self):
        app = self.harness(self, 1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        app._bulk_live_status = lambda _message: None
        app._connect = mock.Mock(
            side_effect=AssertionError("complete cleanup must not reconnect")
        )
        row = {
            "id": "a0047",
            "ghost": True,
            "bulk_reset_cleanup": {
                "slot_id": "a0047",
                "instance_id": "CHECKPOINT-47",
                "state": "complete",
                "requested_at_utc": "2026-10-21T12:00:00Z",
                "completed_at_utc": "2026-10-21T12:01:00Z",
            },
        }

        with (
            mock.patch.object(creator, "get_slot", return_value=row),
            mock.patch.object(creator, "delete_named_instance") as delete,
        ):
            outcome = creator.App._discard_stalled_container(app, "a0047")

        self.assertEqual(outcome, "complete")
        app._connect.assert_not_called()
        delete.assert_not_called()

    def test_stop_after_stall_checkpoint_preserves_pending_without_delete(self):
        app = self.harness(self, 1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        app._bulk_live_status = lambda _message: None
        phone = mock.Mock()
        stack = mock.Mock()
        app._connect = lambda _password: (phone, stack)
        row = {
            "id": "a0047",
            "ghost": True,
            "ghost_container_instance": "GHOST-1",
        }

        def checkpoint_then_stop(sid, iid, _log):
            row["bulk_reset_cleanup"] = {
                "slot_id": sid,
                "instance_id": iid,
                "state": "pending",
                "requested_at_utc": "2026-10-21T12:00:00Z",
            }
            app.stop.set()

        with (
            mock.patch.object(creator, "get_slot", return_value=row),
            mock.patch.object(
                creator,
                "mark_bulk_reset_cleanup_pending",
                side_effect=checkpoint_then_stop,
            ),
            mock.patch.object(creator, "delete_named_instance") as delete,
            mock.patch.object(
                creator, "mark_bulk_reset_cleanup_complete"
            ) as complete,
        ):
            outcome = creator.App._discard_stalled_container(app, "a0047")

        self.assertEqual(outcome, "stopped")
        self.assertEqual(
            row["bulk_reset_cleanup"]["instance_id"], "GHOST-1"
        )
        self.assertEqual(row["bulk_reset_cleanup"]["state"], "pending")
        delete.assert_not_called()
        complete.assert_not_called()
        phone.close.assert_called_once_with()

    def test_malformed_stall_checkpoint_fails_closed_without_connection(self):
        app = self.harness(self, 1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        app._bulk_live_status = lambda _message: None
        app._connect = mock.Mock(
            side_effect=AssertionError("malformed checkpoint must fail first")
        )
        row = {
            "id": "a0047",
            "ghost": True,
            "ghost_container_instance": "GHOST-1",
            "bulk_reset_cleanup": {"state": "pending", "instance_id": ""},
        }

        with (
            mock.patch.object(creator, "get_slot", return_value=row),
            mock.patch.object(creator, "delete_named_instance") as delete,
        ):
            outcome = creator.App._discard_stalled_container(app, "a0047")

        self.assertTrue(outcome.startswith("failed (PhoneError"))
        app._connect.assert_not_called()
        delete.assert_not_called()

    def test_ambiguous_stall_inventory_never_checkpoints_or_deletes(self):
        app = self.harness(self, 1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        app._bulk_live_status = lambda _message: None
        phone = mock.Mock()
        stack = mock.Mock()
        app._connect = lambda _password: (phone, stack)
        row = {"id": "a0047", "ghost": True}

        with (
            mock.patch.object(creator, "get_slot", return_value=row),
            mock.patch.object(
                creator, "invalidate_ghost_container_cache", return_value="force-close"
            ) as invalidate,
            mock.patch.object(
                creator,
                "resolve_sole_user_container",
                side_effect=creator.PhoneError(
                    "Crane preferences contain 2 non-Default containers"
                ),
            ),
            mock.patch.object(
                creator, "mark_bulk_reset_cleanup_pending"
            ) as pending,
            mock.patch.object(creator, "delete_named_instance") as delete,
        ):
            outcome = creator.App._discard_stalled_container(app, "a0047")

        self.assertTrue(outcome.startswith("failed (PhoneError"))
        invalidate.assert_called_once_with(stack, app.log)
        pending.assert_not_called()
        delete.assert_not_called()
        phone.close.assert_called_once_with()

    def test_ambiguous_stall_cleanup_pauses_and_preserves_the_queue(self):
        app = self.harness(self, 1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        app._bulk_live_status = lambda _message: None
        phone = mock.Mock()
        stack = mock.Mock()
        app._connect = lambda _password: (phone, stack)
        entry = dict(app.bulk_state["queue"][0])
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )
        row = {"id": "a0047", "ghost": True}

        with (
            mock.patch.object(creator, "get_slot", return_value=row),
            mock.patch.object(
                creator, "invalidate_ghost_container_cache", return_value="force-close"
            ) as invalidate,
            mock.patch.object(
                creator,
                "resolve_sole_user_container",
                side_effect=creator.PhoneError(
                    "Crane preferences contain 2 non-Default containers"
                ),
            ),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
            mock.patch.object(creator, "delete_named_instance") as delete,
        ):
            outcome = creator.App._stall_reset(
                app, "a0047", entry, "ambiguous inventory", immediate=True
            )

        self.assertFalse(outcome)
        invalidate.assert_called_once_with(stack, app.log)
        self.assertEqual(pauses[0][2], "stall_reset_container_delete_failed")
        skipped.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["slot_id"], "a0047")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_stop_after_stall_mark_preserves_queue_head(self):
        app = self.harness(self, 1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        entry = dict(app.bulk_state["queue"][0])
        app._discard_stalled_container = lambda _sid: "deleted"

        def mark_then_stop(*_args, **_kwargs):
            app.stop.set()

        with (
            mock.patch.object(creator, "get_slot", return_value={"id": "a0047"}),
            mock.patch.object(
                creator, "mark_bulk_skipped", side_effect=mark_then_stop
            ) as mark,
            mock.patch.object(creator, "consume_stalled") as consume,
        ):
            outcome = creator.App._stall_reset(
                app, "a0047", entry, "typed recovery failure", immediate=True
            )

        self.assertFalse(outcome)
        mark.assert_called_once()
        consume.assert_not_called()
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["slot_id"], "a0047")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_stop_after_already_skipped_resume_preserves_queue_head(self):
        app = self.harness(self, 1)
        app.bulk_state.update(stage="creating", slot_id="a0047")
        entry = dict(app.bulk_state["queue"][0])
        skipped_row = {
            "id": "a0047",
            "bulk_skip": {
                "slot_id": "a0047",
                "code": creator.STALL_RESET_FAILURE_CODE,
                "reason": "typed recovery failure",
                "skipped_at_utc": "2026-10-21T12:00:00Z",
            },
        }

        def load_marked_then_stop(_sid):
            app.stop.set()
            return skipped_row

        app._discard_stalled_container = mock.Mock(
            side_effect=AssertionError("already-skipped resume must not delete again")
        )
        with (
            mock.patch.object(creator, "get_slot", side_effect=load_marked_then_stop),
            mock.patch.object(creator, "mark_bulk_skipped") as mark,
            mock.patch.object(creator, "consume_stalled") as consume,
        ):
            outcome = creator.App._stall_reset(
                app, "a0047", entry, "", immediate=True
            )

        self.assertFalse(outcome)
        app._discard_stalled_container.assert_not_called()
        mark.assert_not_called()
        consume.assert_not_called()
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["slot_id"], "a0047")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_stall_reset_pauses_when_the_slot_cannot_be_retired(self):
        app = self.harness(self, 1)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        app._discard_stalled_container = lambda _sid: "deleted"
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )

        def timed_out(*_args, **_kwargs):
            app._last_pipeline_attempt_timed_out = True
            app._last_pipeline_failure_reason = "stalled"
            return False

        app._run_creation_pipeline = timed_out
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", return_value={"complete": False}),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", lambda _sid, _log: None
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(
                creator,
                "mark_bulk_skipped",
                side_effect=OSError("slots.json is read-only"),
            ),
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(pauses[0][2], "stall_reset_persist_failed")
        # The head is preserved when the durable retirement failed.
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(app.bulk_state["slot_id"], "a0047")
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_failure_reset_starts_each_next_item_with_a_new_slot_and_date(self):
        app = self.harness(self, 2)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        discarded = []
        app._discard_stalled_container = (
            lambda sid: discarded.append(sid) or "deleted"
        )
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )
        calls = []
        skipped = []
        stalled_row = {"id": "a0047"}
        next_row = {"id": "a0048", "reddit_birth_date": "02.01.2003"}

        def mark(sid, code, reason, _log=None):
            skipped.append((sid, code))
            stalled_row["bulk_skip"] = {
                "slot_id": sid,
                "code": code,
                "reason": reason,
                "skipped_at_utc": "2026-10-16T12:00:00Z",
            }

        def pipeline(_password, sid, birth, *_args, **_kwargs):
            calls.append((sid, str(birth)))
            if sid == "a0047":
                app._last_pipeline_attempt_timed_out = True
                app._last_pipeline_failure_reason = "No verified progress."
            else:
                app._last_pipeline_attempt_timed_out = False
                app._last_pipeline_failure_shared = False
                app._last_pipeline_failure_code = ""
                app._last_pipeline_failure_reason = "second attempt stopped for the test"
            return False

        app._run_creation_pipeline = pipeline
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator,
                "get_slot",
                side_effect=lambda sid: stalled_row if sid == "a0047" else next_row,
            ),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(creator, "load_slots", lambda: [stalled_row]),
            mock.patch.object(creator, "retired_indices", lambda: set()),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", lambda _sid, _log: None
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.48", "198.51.100.48"),
            ),
            mock.patch.object(
                creator,
                "run_macrodroid_change",
                return_value=verified_result("198.51.100.48", "198.51.100.49"),
            ),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", lambda *_a: None),
            mock.patch.object(creator, "mark_bulk_skipped", side_effect=mark),
        ):
            creator.App._run_bulk_batch(app)

        # Both incomplete identities are retired. The first timed out in its
        # step; the second returned an explicit failure and therefore waited
        # the fixed grace period before receiving the same full reset.
        self.assertEqual(discarded, ["a0047", "a0048"])
        self.assertEqual(
            skipped,
            [
                ("a0047", creator.STALL_RESET_FAILURE_CODE),
                ("a0048", creator.STALL_RESET_FAILURE_CODE),
            ],
        )
        self.assertEqual([sid for sid, _birth in calls], ["a0047", "a0048"])
        self.assertEqual(calls[1][1], "2003-01-01")
        self.assertIsNone(app.bulk_state["slot_id"])
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(pauses, [])
        # The dead account's item never lands in the failure record.
        self.assertEqual(app.bulk_state["failures"], [])

    def test_consume_stalled_drops_the_head_without_credentials(self):
        state = bulk_creation.empty_state()
        state.update(
            queue=[
                {
                    "birth_date": "01.01.2002",
                    "swipes": 10,
                    "delay_seconds": 2.0,
                    "username": "StalledName",
                    "password": "stalled-secret",
                },
                {"birth_date": "02.01.2003", "swipes": 11, "delay_seconds": 3.0},
            ],
            requested=2,
            remaining=2,
            stage="creating",
            slot_id="a0047",
            baseline_ip="198.51.100.47",
            verified_ip="198.51.100.48",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        out = bulk_creation.consume_stalled(
            state, "a0047", ready_at_utc="2026-10-16T12:00:02Z"
        )

        self.assertEqual(out["remaining"], 1)
        self.assertEqual(out["queue"], [state["queue"][1]])
        self.assertEqual(out["failures"], [])
        self.assertIsNone(out["slot_id"])
        self.assertEqual(out["stage"], "between")
        # A stall reset always forces the next attempt onto a fresh IP.
        self.assertIsNone(out["verified_ip"])
        self.assertIsNone(out["baseline_ip"])
        # The stalled identity is gone from every persisted field.
        self.assertNotIn("StalledName", str(out))

    def test_verified_complete_account_with_late_close_error_is_completed(self):
        # A fully verified account whose USB teardown then failed must advance
        # as *completed*; it must never be recorded as a skipped-by-bulk
        # failure.  `success` is False here only because phone.close() raised
        # after every creation checkpoint was already persisted.
        app = self.harness(self, 1)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        complete_row = {
            "id": "a0047",
            "reddit": True,
            "username": "QuietMoon49",
            "password": "offline-secret",
            "email": "keep-4f@icloud.com",
            "complete": True,
        }
        created = {"done": False}

        def not_yet_complete(row):
            return created["done"] and bool(row and row.get("complete"))

        def close_failed(*_args, **_kwargs):
            # The pipeline persisted every checkpoint, then teardown lost USB.
            created["done"] = True
            app._last_pipeline_failure_code = None
            app._last_pipeline_failure_reason = "PC connection close: USB lost."
            app._last_pipeline_failure_shared = False
            app._last_pipeline_attempt_timed_out = False
            return False

        app._run_creation_pipeline = close_failed
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", return_value=complete_row),
            mock.patch.object(
                creator, "_pipeline_slot_complete", side_effect=not_yet_complete
            ),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", lambda _sid, _log: None
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
        ):
            creator.App._run_bulk_batch(app)

        skipped.assert_not_called()
        self.assertEqual(app.bulk_state["stage"], "idle")
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(app.bulk_state["failures"], [])

    def test_legacy_paused_null_code_creation_failure_advances_once(self):
        app = self.harness(self, 1)
        app.bulk_state.update(
            stage="paused",
            slot_id="a0069",
            failure_phase="creation",
            failure_reason="Email field still invalid after one type.",
        )
        starts = []
        app._start_bulk_worker = lambda: starts.append(dict(app.bulk_state))
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", lambda _sid: {"id": "a0069"}),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
        ):
            self.assertTrue(app.retry_bulk_batch(password="offline-password"))

        skipped.assert_called_once_with(
            "a0069",
            creator.LEGACY_CREATION_FAILURE_CODE,
            "Email field still invalid after one type.",
            app.log,
        )
        self.assertEqual(app.bulk_state["stage"], "idle")
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(
            app.bulk_state["failures"][0]["code"], creator.LEGACY_CREATION_FAILURE_CODE
        )
        # Nothing was replayed and no worker was started for a finished queue.
        self.assertEqual(starts, [])

    def test_legacy_paused_null_code_without_slot_id_stays_paused(self):
        app = self.harness(self, 1)
        app.bulk_state.update(
            stage="paused",
            slot_id=None,
            failure_phase="creation",
            failure_reason="Email field still invalid after one type.",
        )
        starts = []
        app._start_bulk_worker = lambda: starts.append(True)
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
        ):
            self.assertFalse(app.retry_bulk_batch(password="offline-password"))

        skipped.assert_not_called()
        self.assertEqual(app.bulk_state["stage"], "paused")
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(starts, [])

    def test_proxy_recheck_pause_is_not_treated_as_a_legacy_creation_failure(self):
        app = self.harness(self, 1)
        app.bulk_state.update(
            stage="paused",
            slot_id="a0047",
            failure_phase="creation",
            failure_code="proxy_recheck_failed",
            failure_reason="Verified proxy IP could not be reconfirmed.",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        starts = []
        app._start_bulk_worker = lambda: starts.append(dict(app.bulk_state))
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", lambda _sid: {"id": "a0047"}),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
        ):
            self.assertTrue(app.retry_bulk_batch(password="offline-password"))

        skipped.assert_not_called()
        self.assertEqual(len(starts), 1)
        self.assertEqual(app.bulk_state["stage"], "retry_creation")

    def test_stop_never_starts_the_next_item_after_a_failed_attempt(self):
        app = self.harness(self, 2)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )

        def failed(*_args, **_kwargs):
            app._last_pipeline_failure_reason = "Email field still invalid."
            # The user presses Stop while the failed attempt is unwinding.
            app.stop.flag = True
            return False

        app._run_creation_pipeline = failed
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(creator, "get_slot", return_value={"complete": False}),
            mock.patch.object(creator, "_pipeline_slot_complete", return_value=False),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", lambda _sid, _log: None
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
            mock.patch.object(creator, "mark_bulk_skipped") as skipped,
        ):
            creator.App._run_bulk_batch(app)

        skipped.assert_not_called()
        self.assertEqual(app.bulk_state["remaining"], 2)
        self.assertEqual(len(app.bulk_state["queue"]), 2)


if __name__ == "__main__":
    unittest.main()
