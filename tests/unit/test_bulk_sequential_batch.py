# UTF-8
"""Offline regression tests for reliable sequential bulk-batch execution.

These prove, with a fully mocked phone/IP/slot layer, that ``_run_bulk_batch``
runs 2-, 5-, 10-, and 50-item queues to completion while:

* preserving the legacy one-IP-per-batch behavior when deletion is disabled;
* resetting and verifying a fresh proxy IP before every item when automatic
  container deletion is enabled;
* consuming each queue entry **exactly once** (distinct slots, drained queue);
* honouring the between-item delay (one bounded ``stop.wait`` per completed
  item that still has successors);
* finishing at ``remaining == 0`` / ``stage == "idle"`` with an empty queue;
* recovering a deletion-pending account **without repeating signup**.

Everything is offline: persistence seams are mocked, workers/IP/slots are
stubbed, and no production state, draft, or slots file is ever read or written.
"""

from __future__ import annotations

import threading
import unittest
from datetime import date
from unittest import mock

from friend_poster.apps import creator
from friend_poster.core import bulk_creation
from test_bulk_creator import Root, Stop, verified_result


class Value:
    """Minimal stand-in for a Tk variable."""

    def __init__(self, value=""):
        self.value = value

    def get(self, *_args):
        return self.value

    def set(self, value):
        self.value = value


def _birth(i: int) -> str:
    """A valid DD.MM.YYYY birth date inside the allowed 2002..2006 window."""
    return f"{(i % 28) + 1:02d}.{(i % 12) + 1:02d}.{2002 + (i % 5)}"


def _queue(count: int) -> list[dict]:
    return [
        {"birth_date": _birth(i), "swipes": 10 + i, "delay_seconds": 2.0}
        for i in range(count)
    ]


class SequentialBatchHarness:
    """Drives ``_run_bulk_batch`` with every external effect mocked.

    exactly-once consumption without any device, network, or state file.
    Records reset/recheck/bind/create events so the test can prove one reset per
    batch and exactly-once consumption without any device, network, or state.
    exactly-once consumption without any device, network, or state file.
    """

    def __init__(self, case: unittest.TestCase, count: int, *, delete: bool = False):
        self.case = case
        self.queue = _queue(count)
        self.events: list[tuple] = []
        self.completed: set[str] = set()
        self.deleted: set[str] = set()
        self.pauses: list[tuple] = []
        self._next_ip = 0
        self._next_index = 1000
        self.deletions: list[str] = []

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
        app.logs = []
        app.log = app.logs.append
        app._after_job = lambda: None
        app._refresh_macrodroid_setup = lambda: True
        app._bulk_proxy_scope = lambda: "a" * 64
        app._record_bulk_ip = (
            lambda result, _obs: result["verification_evidence"]["confirmations"][
                -1
            ]["observed_at_utc"]
        )
        app._pause_bulk = lambda phase, reason, **kw: self.pauses.append(
            (phase, reason, kw.get("code"))
        )
        app.bulk_state = bulk_creation.empty_state()
        app.bulk_state.update(
            queue=list(self.queue),
            requested=count,
            remaining=count,
            stage="ip_pending",
            delete_containers=delete,
        )
        self.app = app

    # --- mocked external effects ------------------------------------------
    def _fake_reset(self, *_args, **_kwargs):
        self._next_ip += 1
        old_ip = f"198.51.100.{self._next_ip}"
        new_ip = f"203.0.113.{self._next_ip}"
        self.events.append(("ip_verified", new_ip))
        return verified_result(old_ip, new_ip)

    def _fake_bind(self, sid, ip, _at):
        self.events.append(("ip_bound", sid, ip))

    def _fake_recheck(self, _url, expected, **_kwargs):
        self.events.append(("ip_rechecked", expected))
        return {"outcome": "verified", "new_ip": expected}

    def _fake_pipeline(self, _password, sid, birth, swipes, **_kwargs):
        self.events.append(("create", sid, birth, int(swipes)))
        self.completed.add(sid)
        if bool(self.app.bulk_state.get("delete_containers")):
            self.events.append(("delete", sid))
            self.deletions.append(sid)
            self.deleted.add(sid)
        return True

    def _fake_get_slot(self, sid):
        return {
            "id": sid,
            "done": sid in self.completed,
            "deleted": sid in self.deleted,
        }

    def _resolve_target(self, _rows, _retired):
        sid = f"a{self._next_index}"
        self._next_index += 1
        return sid, None, True

    # --- runner ------------------------------------------------------------
    def run(self):
        app = self.app
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator.App,
                "_connect",
                side_effect=AssertionError("offline batch test must not connect"),
            ),
            mock.patch.object(creator, "load_slots", lambda: []),
            mock.patch.object(creator, "retired_indices", lambda: set()),
            mock.patch.object(
                creator, "_resolve_creation_pipeline_target", self._resolve_target
            ),
            mock.patch.object(creator, "get_slot", self._fake_get_slot),
            mock.patch.object(creator, "slot_bulk_skipped", lambda _row: False),
            mock.patch.object(creator, "ensure_ghost_container_needed", lambda *_a: None),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", self._fake_bind),
            mock.patch.object(creator, "_pipeline_slot_complete",
                              lambda row: bool(row and row.get("done"))),
            mock.patch.object(creator, "_pipeline_deletion_pending",
                              lambda row, required: bool(
                                  required and not (row or {}).get("deleted")
                              )),
            mock.patch.object(creator, "run_macrodroid_change", self._fake_reset),
            mock.patch.object(creator, "observe_proxy_without_trigger",
                              side_effect=self._fake_recheck),
        ):
            # The batch supervisor now owns one shared recovery budget and
            # invokes the bridge wrapper.  Replace that current seam directly
            # so these tests cannot reach a phone even if its internals change.
            app._run_pipeline_with_bridge_recovery = self._fake_pipeline
            if getattr(app.bulk_state, "get", None) and app.bulk_state.get(
                "delete_containers"
            ):
                app._run_deletion_only = self._fake_deletion_only
            creator.App._run_bulk_batch(app)
        return app

    def _fake_deletion_only(self, _password, sid):
        self.deletions.append(sid)
        self.completed.add(sid)
        self.deleted.add(sid)
        return True


class SequentialBatchTests(unittest.TestCase):
    """A 2-, 5-, 10-, and 50-item batch completes in order, exactly once."""

    def test_completed_a0103_allocates_a0104_for_the_next_creation(self):
        with mock.patch.object(creator, "_pipeline_slot_complete", return_value=True):
            sid, row, is_new = creator._resolve_creation_pipeline_target(
                [{"id": "a0103"}], set()
            )

        self.assertEqual(sid, "a0104")
        self.assertIsNone(row)
        self.assertTrue(is_new)

    def _expected_events(self, count):
        expected = []
        for i in range(count):
            ip = "203.0.113.1"
            sid = f"a{1000 + i}"
            row = _queue(count)[i]
            birth = date(
                2002 + (i % 5),
                (i % 12) + 1,
                (i % 28) + 1,
            )
            if i == 0:
                expected.append(("ip_verified", ip))
            else:
                expected.append(("ip_rechecked", ip))
            expected.append(("ip_bound", sid, ip))
            expected.append(("create", sid, birth, 10 + i))
        return expected

    def test_batch_of_2_5_10_and_50_runs_in_order_exactly_once_to_zero(self):
        for count in (2, 5, 10, 50):
            with self.subTest(count=count):
                harness = SequentialBatchHarness(self, count)
                app = harness.run()
                state = app.bulk_state

                # The batch reset once, then every item used the same rechecked
                # IP and recorded it on its own slot.
                self.assertEqual(harness.events, self._expected_events(count))
                self.assertEqual(harness._next_ip, 1)
                self.assertEqual(len(harness.pauses), 0)

                # Exactly-once consumption: distinct slots, fully drained.
                sids = [e[1] for e in harness.events if e[0] == "create"]
                self.assertEqual(len(sids), count)
                self.assertEqual(len(set(sids)), count)
                self.assertEqual(state["remaining"], 0)
                self.assertEqual(state["stage"], "idle")
                self.assertEqual(state["queue"], [])
                self.assertEqual(state["failures"], [])

                # Automatic continuation: a single run drained the whole queue.
                self.assertTrue(
                    any("BATCH complete: every requested item finished." in line
                        for line in app.logs)
                )

    def test_between_item_delay_is_honoured_once_per_completed_item(self):
        for count in (2, 5, 10, 50):
            with self.subTest(count=count):
                harness = SequentialBatchHarness(self, count)
                app = harness.run()
                waits = app.stop.waits
                # One bounded wait after each item that still has a successor;
                # the final item goes straight to idle with no trailing delay.
                self.assertEqual(len(waits), count - 1)
                self.assertTrue(all(w > 0 for w in waits))

    def test_stage_ordering_has_one_reset_then_rechecks_without_more_triggers(self):
        count = 8
        harness = SequentialBatchHarness(self, count)
        harness.run()
        kinds = [e[0] for e in harness.events]
        self.assertEqual(kinds[:3], ["ip_verified", "ip_bound", "create"])
        self.assertEqual(
            kinds[3:], ["ip_rechecked", "ip_bound", "create"] * (count - 1)
        )
        self.assertEqual(harness._next_ip, 1)

    def test_delete_enabled_2_5_10_and_50_reset_before_every_account(self):
        for count in (2, 5, 10, 50):
            with self.subTest(count=count):
                harness = SequentialBatchHarness(self, count, delete=True)
                app = harness.run()

                creates = [event for event in harness.events if event[0] == "create"]
                resets = [event for event in harness.events if event[0] == "ip_verified"]
                rechecks = [event for event in harness.events if event[0] == "ip_rechecked"]
                expected_slots = [f"a{1000 + i}" for i in range(count)]

                self.assertEqual([event[1] for event in creates], expected_slots)
                self.assertEqual(harness.deletions, expected_slots)
                self.assertEqual(len(resets), count)
                self.assertEqual(harness._next_ip, count)
                self.assertEqual(rechecks, [])
                self.assertEqual(app.bulk_state["remaining"], 0)
                self.assertEqual(app.bulk_state["queue"], [])
                self.assertEqual(app.bulk_state["stage"], "idle")
                self.assertEqual(harness.pauses, [])

    def test_completed_deletion_resumes_with_next_item_ip_reset_not_signup_replay(self):
        harness = SequentialBatchHarness(self, 2, delete=True)
        harness.completed.add("a1000")
        harness.deleted.add("a1000")
        harness._next_index = 1001
        harness.app.bulk_state.update(
            stage="creating",
            slot_id="a1000",
            baseline_ip="198.51.100.9",
            verified_ip="203.0.113.9",
            verified_at_utc="2026-10-18T22:00:00Z",
        )

        app = harness.run()

        creates = [event for event in harness.events if event[0] == "create"]
        self.assertEqual([event[1] for event in creates], ["a1001"])
        self.assertEqual(harness.deletions, ["a1001"])
        self.assertEqual(harness._next_ip, 1)
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(app.bulk_state["stage"], "idle")

    def test_deletion_pending_account_resumes_deletion_without_repeating_signup(self):
        harness = SequentialBatchHarness(self, 1, delete=True)
        app = harness.app
        # The account is already fully created; only its required container
        # deletion is pending, so Resume must run deletion only.
        harness._fake_pipeline = lambda *_a, **_k: self.fail("signup must not repeat")

        def deletion_only(_password, sid):
            harness.deletions.append(sid)
            harness.completed.add(sid)
            return True

        app._run_deletion_only = deletion_only
        # The account is already fully created: mark its slot complete so the
        # loop routes into the deletion-pending branch, not signup.
        harness.completed.add("a1000")
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator.App,
                "_connect",
                side_effect=AssertionError("offline batch test must not connect"),
            ),
            mock.patch.object(creator, "load_slots", lambda: []),
            mock.patch.object(creator, "retired_indices", lambda: set()),
            mock.patch.object(
                creator, "_resolve_creation_pipeline_target", harness._resolve_target
            ),
            mock.patch.object(creator, "get_slot", harness._fake_get_slot),
            mock.patch.object(creator, "slot_bulk_skipped", lambda _row: False),
            mock.patch.object(creator, "ensure_ghost_container_needed", lambda *_a: None),
            mock.patch.object(creator, "_pipeline_slot_complete",
                              lambda row: bool(row and row.get("done"))),
            mock.patch.object(creator, "_pipeline_deletion_pending",
                              lambda _row, required: bool(required)),
            mock.patch.object(creator, "run_macrodroid_change",
                              side_effect=AssertionError("no IP reset for deletion-only")),
        ):
            # Stage "creating" + a complete slot routes straight to deletion.
            # A deletion-pending account was fully created and IP-verified, so
            # the "creating" stage carries its verified-IP evidence.
            app.bulk_state.update(
                stage="creating",
                slot_id="a1000",
                baseline_ip="198.51.100.1",
                verified_ip="203.0.113.1",
                verified_at_utc="2026-10-16T12:00:00Z",
            )
            app._run_pipeline_with_bridge_recovery = (
                lambda *_a, **_k: self.fail("signup must not repeat")
            )
            creator.App._run_bulk_batch(app)

        self.assertEqual(harness.deletions, ["a1000"])
        self.assertEqual(app.bulk_state["remaining"], 0)
        self.assertEqual(app.bulk_state["stage"], "idle")


class InterruptionAndPersistenceTests(unittest.TestCase):
    """Stop and a failed state save must preserve the queue, never lose items."""

    def test_stop_after_one_creation_preserves_the_unconsumed_queue(self):
        harness = SequentialBatchHarness(self, 3)
        app = harness.app

        def stopping_pipeline(_password, sid, birth, swipes, **_kwargs):
            harness.events.append(("create", sid, birth, int(swipes)))
            app.stop.set()  # user presses Stop during this item's creation
            return True

        app._run_pipeline_with_bridge_recovery = stopping_pipeline
        with (
            mock.patch.object(creator, "save_bulk_state", lambda _state: None),
            mock.patch.object(
                creator.App,
                "_connect",
                side_effect=AssertionError("offline batch test must not connect"),
            ),
            mock.patch.object(creator, "load_slots", lambda: []),
            mock.patch.object(creator, "retired_indices", lambda: set()),
            mock.patch.object(
                creator, "_resolve_creation_pipeline_target", harness._resolve_target
            ),
            mock.patch.object(creator, "get_slot", harness._fake_get_slot),
            mock.patch.object(creator, "slot_bulk_skipped", lambda _row: False),
            mock.patch.object(creator, "ensure_ghost_container_needed", lambda *_a: None),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", harness._fake_bind),
            mock.patch.object(creator, "_pipeline_slot_complete",
                              lambda row: bool(row and row.get("done"))),
            mock.patch.object(creator, "_pipeline_deletion_pending",
                              lambda _row, required: bool(required)),
            mock.patch.object(creator, "run_macrodroid_change", harness._fake_reset),
        ):
            creator.App._run_bulk_batch(app)

        # Stop halted the loop before the in-flight item was consumed; the
        # remaining queue and its count are preserved, never advanced.
        creates = [e for e in harness.events if e[0] == "create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(app.bulk_state["remaining"], 3)
        self.assertEqual(len(app.bulk_state["queue"]), 3)

    def test_mid_run_persistence_failure_pauses_without_consuming_the_queue(self):
        harness = SequentialBatchHarness(self, 4)
        app = harness.app

        def flaky_save(state):
            if state.get("stage") == "ip_verified":
                raise creator.BulkSaveError("state file could not be written")
            return None

        app._run_pipeline_with_bridge_recovery = harness._fake_pipeline
        with (
            mock.patch.object(creator, "save_bulk_state", flaky_save),
            mock.patch.object(
                creator.App,
                "_connect",
                side_effect=AssertionError("offline batch test must not connect"),
            ),
            mock.patch.object(creator, "load_slots", lambda: []),
            mock.patch.object(creator, "retired_indices", lambda: set()),
            mock.patch.object(
                creator, "_resolve_creation_pipeline_target", harness._resolve_target
            ),
            mock.patch.object(creator, "get_slot", harness._fake_get_slot),
            mock.patch.object(creator, "slot_bulk_skipped", lambda _row: False),
            mock.patch.object(creator, "ensure_ghost_container_needed", lambda *_a: None),
            mock.patch.object(creator, "set_slot_falcon_proxy_ip", harness._fake_bind),
            mock.patch.object(creator, "_pipeline_slot_complete",
                              lambda row: bool(row and row.get("done"))),
            mock.patch.object(creator, "_pipeline_deletion_pending",
                              lambda _row, required: bool(required)),
            mock.patch.object(creator, "run_macrodroid_change", harness._fake_reset),
        ):
            creator.App._run_bulk_batch(app)

        # The save failure paused the batch with the shared code and the queue
        # is untouched: no creation ran and nothing was consumed.
        creates = [e for e in harness.events if e[0] == "create"]
        self.assertEqual(creates, [])
        self.assertEqual(app.bulk_state["remaining"], 4)
        self.assertEqual(len(app.bulk_state["queue"]), 4)
        self.assertTrue(any(p[2] == "bulk_save_failed" for p in harness.pauses))


if __name__ == "__main__":
    unittest.main()
