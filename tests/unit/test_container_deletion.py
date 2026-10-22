"""Offline tests for durable local-container deletion and container-prep reuse.

Everything here is offline: mocked device primitives, mocked slot persistence,
a fake monotonic clock, stubbed IP flows, and temporary files.  No phone, no
live state file, and no real container deletion.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from friend_poster.apps import creator
from friend_poster.core import bulk_creation, slots
from friend_poster.core.slots import CreationProgressClock
from friend_poster.device import ghost_live
from test_bulk_creator import Root, Stop, verified_result


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeStack:
    """Minimal TapStack stand-in for the deletion SOP."""

    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.taps: list[tuple[float, float]] = []
        self.cleared_bundles: list[str] = []

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise ghost_live.PhoneError("Stopped.")

    def tap_xy(self, x, y) -> None:
        self.taps.append((x, y))

    def clear_app_memory(self, bundle_id: str) -> str:
        self.cleared_bundles.append(bundle_id)
        return "already-stopped"


def container_row(**extra):
    row = {
        "id": "a0047",
        "email": "keep-4f@icloud.com",
        "email_label": "a0047",
        "username": "QuietMoon49",
        "password": "offline-secret",
        "ghost": True,
        "ghost_container_state": "ready",
        "ghost_container_instance": "ABC1234",
        "reddit": True,
    }
    row.update(extra)
    return row


def complete_record(instance_id="ABC1234"):
    return {
        "slot_id": "a0047",
        "instance_id": instance_id,
        "state": "complete",
        "requested_at_utc": "2026-10-17T00:00:00Z",
        "completed_at_utc": "2026-10-17T00:01:00Z",
    }


# --------------------------------------------------------------------------- #
# slots.py: durable deletion checkpoints and permanent reservations
# --------------------------------------------------------------------------- #


class ContainerDeletionStateTests(unittest.TestCase):
    def test_legacy_pending_ghost_prefix_migrates_atomically_to_full_crane_uuid(self):
        row = container_row(
            ghost_container_instance="SHORT47",
            container_deletion={
                "slot_id": "a0047",
                "instance_id": "SHORT47",
                "state": "pending",
                "requested_at_utc": "2026-10-17T00:00:00Z",
            },
        )
        writes = []
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda rows: writes.append(rows)),
        ):
            slots.reconcile_crane_container_identity(
                "a0047",
                "12345678-1234-1234-1234-123456789ABC",
                allow_pending_rebind=True,
            )
        self.assertEqual(
            row["ghost_container_instance"], "SHORT47"
        )
        self.assertEqual(
            row["crane_container_instance"],
            "12345678-1234-1234-1234-123456789ABC",
        )
        self.assertEqual(
            row["container_deletion"]["instance_id"],
            "12345678-1234-1234-1234-123456789ABC",
        )
        self.assertEqual(len(writes), 1)

    def test_legacy_pending_identity_cannot_rebind_without_explicit_proof(self):
        row = container_row(
            container_deletion={
                "slot_id": "a0047",
                "instance_id": "SHORT47",
                "state": "pending",
            }
        )
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots") as save,
        ):
            with self.assertRaises(ValueError):
                slots.reconcile_crane_container_identity(
                    "a0047", "FULL-UUID-47", allow_pending_rebind=False
                )
        save.assert_not_called()

    def test_pending_is_persisted_with_exact_identity_and_reserves_the_id(self):
        row = container_row()
        saved = {}

        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda rows: saved.update(rows=rows)),
            mock.patch.object(slots, "retired_indices", lambda: set()),
        ):
            slots.mark_container_deletion_pending("a0047", "abc1234", None)
            record = row["container_deletion"]
            self.assertEqual(record["slot_id"], "a0047")
            self.assertEqual(record["instance_id"], "ABC1234")
            self.assertEqual(record["state"], "pending")
            self.assertTrue(str(record["requested_at_utc"]).endswith("Z"))
            self.assertEqual(slots.container_deletion_state(row), "pending")
            self.assertEqual(slots.container_deletion_instance(row), "ABC1234")
            self.assertFalse(slots.slot_container_deleted(row))
            self.assertTrue(slots.slot_container_available(row))
            self.assertEqual(saved["rows"], [row])
            # A pending row is a durable reservation: the id must never be
            # handed out again even before the deletion is verified.
            self.assertIn(47, slots.indices_used())

    def test_complete_keeps_the_account_credentials_alias_and_reservation(self):
        row = container_row()
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            slots.mark_container_deletion_pending("a0047", "ABC1234", None)
            slots.mark_container_deletion_complete("a0047", None)

        self.assertEqual(row["username"], "QuietMoon49")
        self.assertEqual(row["password"], "offline-secret")
        self.assertEqual(row["email"], "keep-4f@icloud.com")
        self.assertEqual(row["ghost_container_instance"], "ABC1234")
        self.assertTrue(row["reddit"])
        self.assertTrue(slots.slot_container_deleted(row))
        self.assertFalse(slots.slot_container_available(row))
        self.assertEqual(slots.container_deletion_state(row), "complete")
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "retired_indices", lambda: set()),
        ):
            self.assertIn(47, slots.indices_used())

    def test_pending_refuses_a_different_full_crane_container(self):
        row = container_row(crane_container_instance="ABC1234")
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            with self.assertRaises(ValueError):
                slots.mark_container_deletion_pending("a0047", "OTHER99", None)

    def test_pending_refuses_an_account_without_a_created_reddit(self):
        row = container_row(reddit=False, username=None)
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            with self.assertRaises(ValueError):
                slots.mark_container_deletion_pending("a0047", "ABC1234", None)

    def test_pending_refuses_a_deleted_container(self):
        row = container_row(container_deletion=complete_record())
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            with self.assertRaises(ValueError):
                slots.mark_container_deletion_pending("a0047", "ABC1234", None)

    def test_complete_requires_a_pending_record(self):
        row = container_row()
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            with self.assertRaises(ValueError):
                slots.mark_container_deletion_complete("a0047", None)

    def test_malformed_record_fails_closed_as_absent(self):
        self.assertIsNone(slots.container_deletion({"container_deletion": []}))
        self.assertEqual(
            slots.container_deletion_state(
                {"container_deletion": {"state": "banana", "instance_id": "X"}}
            ),
            "",
        )
        self.assertEqual(
            slots.container_deletion_state(
                {"container_deletion": {"state": "complete", "instance_id": "  "}}
            ),
            "",
        )
        # Fails closed: an unverifiable record must not look deleted.
        self.assertFalse(
            slots.slot_container_deleted(
                {"container_deletion": {"state": "pending", "instance_id": "X"}}
            )
        )

    def test_repeated_pending_write_does_not_reset_the_progress_clock(self):
        fake = FakeMonotonic()
        clock = CreationProgressClock(time_fn=fake)
        row = container_row()
        with (
            mock.patch.object(slots, "CREATION_PROGRESS", clock),
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            slots.mark_container_deletion_pending("a0047", "ABC1234", None)
            fake.advance(200.0)
            # Re-issuing the identical durable intent is a retry, not progress.
            slots.mark_container_deletion_pending("a0047", "ABC1234", None)
            self.assertAlmostEqual(clock.idle_seconds(), 200.0)
            slots.mark_container_deletion_complete("a0047", None)
            self.assertEqual(clock.idle_seconds(), 0.0)

    def test_deletion_record_alone_reserves_the_id_after_restart(self):
        # Simulates a restart: only the persisted row is available.
        row = {"id": "a0047", "container_deletion": complete_record()}
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "retired_indices", lambda: set()),
        ):
            self.assertIn(47, slots.indices_used())
            self.assertNotEqual(slots.next_slot_id(), "a0047")


class BulkResetCleanupStateTests(unittest.TestCase):
    def incomplete_row(self, **extra):
        row = container_row(
            email=None,
            username=None,
            password=None,
            reddit=False,
        )
        row.update(extra)
        return row

    def test_pending_precedes_delete_and_complete_preserves_exact_uuid(self):
        row = self.incomplete_row()
        writes = []
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda rows: writes.append(rows)),
        ):
            slots.mark_bulk_reset_cleanup_pending("a0047", "abc1234")
            self.assertEqual(slots.bulk_reset_cleanup_state(row), "pending")
            self.assertEqual(slots.bulk_reset_cleanup_instance(row), "ABC1234")
            slots.mark_bulk_reset_cleanup_complete("a0047")

        self.assertEqual(slots.bulk_reset_cleanup_state(row), "complete")
        self.assertEqual(slots.bulk_reset_cleanup_instance(row), "ABC1234")
        self.assertEqual(len(writes), 2)

    def test_absent_checkpoint_does_not_change_provisioning_resume(self):
        row = self.incomplete_row(
            ghost=False,
            ghost_container_state="provisioning",
            ghost_container_instance=None,
        )
        self.assertEqual(slots.bulk_reset_cleanup_state(row), "")
        self.assertEqual(slots.bulk_reset_cleanup_instance(row), "")
        self.assertEqual(slots.ghost_container_state(row), "provisioning")

    def test_pending_uuid_is_immutable_and_idempotent(self):
        fake = FakeMonotonic()
        clock = CreationProgressClock(time_fn=fake)
        row = self.incomplete_row()
        with (
            mock.patch.object(slots, "CREATION_PROGRESS", clock),
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            slots.mark_bulk_reset_cleanup_pending("a0047", "ABC1234")
            fake.advance(200.0)
            slots.mark_bulk_reset_cleanup_pending("a0047", "abc1234")
            self.assertAlmostEqual(clock.idle_seconds(), 200.0)
            with self.assertRaises(ValueError):
                slots.mark_bulk_reset_cleanup_pending("a0047", "OTHER99")

    def test_pending_ignores_ghost_prefix_but_matches_full_crane_uuid(self):
        row = self.incomplete_row(
            ghost_container_instance="RECORDED47",
            crane_container_instance="FULL-CRANE-47",
        )
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            with self.assertRaises(ValueError):
                slots.mark_bulk_reset_cleanup_pending("a0047", "OTHER47")

    def test_reset_checkpoint_refuses_created_account_and_normal_cleanup(self):
        for row in (
            container_row(),
            self.incomplete_row(container_deletion=complete_record()),
        ):
            with (
                mock.patch.object(slots, "load_slots", lambda row=row: [row]),
                mock.patch.object(slots, "save_slots", lambda _rows: None),
            ):
                with self.assertRaises(ValueError):
                    slots.mark_bulk_reset_cleanup_pending(
                        "a0047", "ABC1234"
                    )

    def test_malformed_checkpoint_cannot_be_replaced_or_completed(self):
        row = self.incomplete_row(
            bulk_reset_cleanup={"state": "pending", "instance_id": ""}
        )
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            with self.assertRaises(ValueError):
                slots.mark_bulk_reset_cleanup_pending("a0047", "ABC1234")
            with self.assertRaises(ValueError):
                slots.mark_bulk_reset_cleanup_complete("a0047")

    def test_reset_checkpoint_alone_reserves_slot_id(self):
        row = {
            "id": "a0047",
            "bulk_reset_cleanup": {
                "slot_id": "a0047",
                "instance_id": "ABC1234",
                "state": "complete",
            },
        }
        with (
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "retired_indices", lambda: set()),
        ):
            self.assertIn(47, slots.indices_used())


# --------------------------------------------------------------------------- #
# Identity resolution: a UUID prefix is never trusted as an exact UUID
# --------------------------------------------------------------------------- #


class RecordedInstanceResolutionTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(
            ghost_live.resolve_recorded_instance({"ABC1234"}, "abc1234"),
            ("exact", "ABC1234"),
        )

    def test_unique_strict_prefix_is_accepted(self):
        self.assertEqual(
            ghost_live.resolve_recorded_instance({"ABC123456"}, "ABC1234"),
            ("prefix", "ABC123456"),
        )

    def test_ambiguous_prefix_is_never_an_exact_uuid(self):
        self.assertEqual(
            ghost_live.resolve_recorded_instance(
                {"ABC12345", "ABC12346"}, "ABC1234"
            ),
            ("ambiguous", ""),
        )

    def test_absent_when_nothing_matches(self):
        self.assertEqual(
            ghost_live.resolve_recorded_instance({"ZZZ9999"}, "ABC1234"),
            ("absent", ""),
        )
        self.assertEqual(
            ghost_live.resolve_recorded_instance(set(), "ABC1234"), ("absent", "")
        )

    def test_blank_recorded_is_absent(self):
        self.assertEqual(
            ghost_live.resolve_recorded_instance({"ABC1234"}, "   "), ("absent", "")
        )
        self.assertEqual(
            ghost_live.resolve_recorded_instance({"ABC1234"}, None), ("absent", "")
        )


# --------------------------------------------------------------------------- #
# Device-level deletion SOP
# --------------------------------------------------------------------------- #


@contextmanager
def deletion_ctx(frames, delete_calls):
    """Patch the whole device SOP except its decision logic."""

    def fake_delete(_stack, _phone, _lg, nodes, name, instance_id):
        delete_calls.append((name, instance_id))
        return nodes

    with (
        mock.patch.object(ghost_live, "close_ghost_organic", lambda *_a, **_k: None),
        mock.patch.object(ghost_live, "open_reddit_containers", lambda *_a, **_k: None),
        mock.patch.object(ghost_live, "dump_named", lambda *_a, **_k: ("", ["frame"])),
        mock.patch.object(ghost_live, "stop_if_reset", lambda *_a, **_k: None),
        mock.patch.object(ghost_live, "on_containers_list", lambda *_a, **_k: True),
        mock.patch.object(
            ghost_live, "scan_all_container_instances", side_effect=frames
        ),
        mock.patch.object(ghost_live, "_delete_row_in_edit", fake_delete),
    ):
        yield


class DeviceDeletionTests(unittest.TestCase):
    def test_deletes_only_the_exact_recorded_container(self):
        frames = [
            ({"a0047": {"ABC1234", "ZZZ9999"}}, ["bottom"]),
            ({"a0047": {"ZZZ9999"}}, ["bottom"]),
        ]
        calls: list = []
        with deletion_ctx(frames, calls):
            outcome = ghost_live.delete_named_instance(
                FakeStack(), object(), lambda _m: None, "a0047", "ABC1234"
            )
        self.assertEqual(outcome, "deleted")
        self.assertEqual(calls, [("a0047", "ABC1234")])

    def test_ambiguous_prefix_is_refused_without_deleting(self):
        calls: list = []
        frames = [({"a0047": {"ABC12345", "ABC12346"}}, ["bottom"])]
        with deletion_ctx(frames, calls):
            with self.assertRaises(ghost_live.PhoneError):
                ghost_live.delete_named_instance(
                    FakeStack(), object(), lambda _m: None, "a0047", "ABC1234"
                )
        self.assertEqual(calls, [])

    def test_unique_prefix_deletes_the_single_matching_row(self):
        frames = [
            ({"a0047": {"ABC123456"}}, ["bottom"]),
            ({"a0047": set()}, ["bottom"]),
        ]
        calls: list = []
        with deletion_ctx(frames, calls):
            outcome = ghost_live.delete_named_instance(
                FakeStack(), object(), lambda _m: None, "a0047", "ABC1234"
            )
        self.assertEqual(outcome, "deleted")
        self.assertEqual(calls, [("a0047", "ABC123456")])

    def test_already_absent_uuid_finishes_without_deleting_anything(self):
        # Same name, different (unrelated) container: the recorded identity is
        # already gone.  Finish the pending deletion without touching the
        # unrelated container.
        calls: list = []
        frames = [({"a0047": {"OTHER99"}}, ["bottom"])]
        with deletion_ctx(frames, calls):
            outcome = ghost_live.delete_named_instance(
                FakeStack(), object(), lambda _m: None, "a0047", "ABC1234"
            )
        self.assertEqual(outcome, "absent")
        self.assertEqual(calls, [])

    def test_unconfirmed_deletion_raises_even_when_no_row_is_visible(self):
        # A missing viewport row is not proof: the complete inventory still
        # reports the container, so the deletion must not be confirmed.
        calls: list = []
        frames = [
            ({"a0047": {"ABC1234"}}, ["bottom"]),
            ({"a0047": {"ABC1234"}}, ["bottom"]),
        ]
        with deletion_ctx(frames, calls):
            with self.assertRaises(ghost_live.PhoneError):
                ghost_live.delete_named_instance(
                    FakeStack(), object(), lambda _m: None, "a0047", "ABC1234"
                )
        self.assertEqual(calls, [("a0047", "ABC1234")])

    def test_missing_recorded_uuid_is_refused(self):
        calls: list = []
        with deletion_ctx([], calls):
            with self.assertRaises(ghost_live.PhoneError):
                ghost_live.delete_named_instance(
                    FakeStack(), object(), lambda _m: None, "a0047", ""
                )
        self.assertEqual(calls, [])


# --------------------------------------------------------------------------- #
# Controller: _ensure_container_deleted (intent-first, idempotent)
# --------------------------------------------------------------------------- #


def bare_app():
    app = creator.App.__new__(creator.App)
    app.stop = Stop()
    app.log = lambda _message: None
    app.status = mock.Mock()
    app._bind_crane_container_identity = (
        lambda _phone, _sid, **_kwargs: "ABC1234"
    )
    return app


class EnsureContainerDeletedTests(unittest.TestCase):
    def test_completed_legacy_prefix_binds_unique_exact_name_full_uuid(self):
        app = bare_app()
        row = container_row(
            ghost_container_instance="LEGACY7",
            container_deletion={
                "slot_id": "a0047",
                "instance_id": "LEGACY7",
                "state": "pending",
            },
        )
        full_uuid = "12345678-1234-1234-1234-123456789ABC"
        with (
            mock.patch.object(creator, "get_slot", return_value=row),
            mock.patch.object(
                creator,
                "resolve_crane_container_identity",
                return_value=("resolved", full_uuid),
            ) as resolve,
            mock.patch.object(
                creator, "reconcile_crane_container_identity", return_value=row
            ) as reconcile,
        ):
            resolved = creator.App._bind_crane_container_identity(
                app, object(), "a0047", allow_completed_rebind=True
            )
        self.assertEqual(resolved, full_uuid)
        resolve.assert_called_once()
        self.assertTrue(reconcile.call_args.kwargs["allow_pending_rebind"])

    def test_intent_is_persisted_before_the_delete_gesture(self):
        app = bare_app()
        row = container_row()
        order: list = []
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: row),
            mock.patch.object(creator, "container_deletion_state", lambda _row: ""),
            mock.patch.object(creator, "container_deletion_instance", lambda _row: ""),
            mock.patch.object(
                creator,
                "mark_container_deletion_pending",
                lambda sid, iid, log: order.append(("pending", sid, iid)),
            ),
            mock.patch.object(
                creator,
                "delete_named_instance",
                lambda _stack, _phone, _log, name, iid, **_kwargs: (
                    order.append(("delete", name, iid)) or "deleted"
                ),
            ),
            mock.patch.object(
                creator,
                "mark_container_deletion_complete",
                lambda sid, log: order.append(("complete", sid)),
            ),
        ):
            outcome = app._ensure_container_deleted(
                object(), FakeStack(), "a0047", None
            )
        self.assertEqual(outcome, "complete")
        self.assertEqual(
            order,
            [
                ("pending", "a0047", "ABC1234"),
                ("delete", "a0047", "ABC1234"),
                ("complete", "a0047"),
            ],
        )

    def test_already_absent_uuid_finishes_the_pending_checkpoint(self):
        # A crash after the physical delete but before the checkpoint: the
        # recorded UUID is gone, so resume must finish persistence and must not
        # repeat signup or delete anything else.
        app = bare_app()
        row = container_row()
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: row),
            mock.patch.object(creator, "container_deletion_state", lambda _row: ""),
            mock.patch.object(creator, "container_deletion_instance", lambda _row: ""),
            mock.patch.object(
                creator, "mark_container_deletion_pending", lambda *_a, **_k: None
            ),
            mock.patch.object(
                creator, "delete_named_instance", lambda *_a, **_k: "absent"
            ),
            mock.patch.object(creator, "mark_container_deletion_complete") as done,
        ):
            outcome = app._ensure_container_deleted(
                object(), FakeStack(), "a0047", None
            )
        self.assertEqual(outcome, "complete")
        done.assert_called_once()

    def test_failed_exact_uuid_delete_is_not_retried(self):
        app = bare_app()
        row = container_row()
        calls = []

        def delete(_stack, _phone, _log, _sid, _instance_id, **_kwargs):
            calls.append("delete")
            raise ghost_live.PhoneError("Edit is not on screen")

        stack = FakeStack()
        stack.press_home = mock.Mock()
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: row),
            mock.patch.object(creator, "container_deletion_state", lambda _row: ""),
            mock.patch.object(creator, "container_deletion_instance", lambda _row: ""),
            mock.patch.object(
                creator, "mark_container_deletion_pending", lambda *_a, **_k: None
            ),
            mock.patch.object(creator, "delete_named_instance", side_effect=delete),
            mock.patch.object(creator, "mark_container_deletion_complete") as done,
        ):
            with self.assertRaises(ghost_live.PhoneError):
                app._ensure_container_deleted(object(), stack, "a0047", None)
        self.assertEqual(calls, ["delete"])
        stack.press_home.assert_not_called()
        done.assert_not_called()

    def test_already_complete_checkpoint_short_circuits(self):
        app = bare_app()
        row = container_row()
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: row),
            mock.patch.object(
                creator, "container_deletion_state", lambda _row: "complete"
            ),
            mock.patch.object(creator, "delete_named_instance") as delete,
        ):
            outcome = app._ensure_container_deleted(
                object(), FakeStack(), "a0047", None
            )
        self.assertEqual(outcome, "complete")
        delete.assert_not_called()

    def test_without_uuid_resolves_and_persists_exact_identity_before_delete(self):
        app = bare_app()
        row = container_row(ghost_container_instance=None)
        order = []
        app._bind_crane_container_identity = (
            lambda _phone, sid, **_kwargs: (
                order.append(("resolve", sid)) or "RESOLVED47"
            )
        )
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: row),
            mock.patch.object(creator, "container_deletion_state", lambda _row: ""),
            mock.patch.object(creator, "container_deletion_instance", lambda _row: ""),
            mock.patch.object(
                creator,
                "mark_container_deletion_pending",
                lambda sid, iid, log: order.append(("pending", sid, iid)),
            ),
            mock.patch.object(
                creator,
                "delete_named_instance",
                side_effect=lambda _stack, _phone, _log, sid, iid, **_kwargs: (
                    order.append(("delete", sid, iid)) or "deleted"
                ),
            ) as delete,
            mock.patch.object(
                creator,
                "mark_container_deletion_complete",
                lambda sid, log: order.append(("complete", sid)),
            ),
        ):
            outcome = app._ensure_container_deleted(
                object(), FakeStack(), "a0047", None
            )
        self.assertEqual(outcome, "complete")
        self.assertEqual(
            order,
            [
                ("resolve", "a0047"),
                ("pending", "a0047", "RESOLVED47"),
                ("delete", "a0047", "RESOLVED47"),
                ("complete", "a0047"),
            ],
        )
        delete.assert_called_once()

    def test_ambiguous_no_uuid_inventory_never_persists_or_deletes(self):
        app = bare_app()
        app._bind_crane_container_identity = mock.Mock(
            side_effect=ghost_live.PhoneError(
                "Crane preferences contain 2 containers named a0047"
            )
        )
        row = container_row(ghost_container_instance=None)
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: row),
            mock.patch.object(creator, "container_deletion_state", lambda _row: ""),
            mock.patch.object(creator, "container_deletion_instance", lambda _row: ""),
            mock.patch.object(creator, "mark_container_deletion_pending") as pending,
            mock.patch.object(creator, "delete_named_instance") as delete,
            mock.patch.object(creator, "mark_container_deletion_complete") as done,
        ):
            with self.assertRaises(ghost_live.PhoneError):
                app._ensure_container_deleted(object(), FakeStack(), "a0047", None)

        pending.assert_not_called()
        delete.assert_not_called()
        done.assert_not_called()

    def test_refuses_an_account_without_a_created_reddit(self):
        app = bare_app()
        row = container_row(reddit=False, username=None)
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: row),
            mock.patch.object(creator, "container_deletion_state", lambda _row: ""),
            mock.patch.object(creator, "container_deletion_instance", lambda _row: ""),
            mock.patch.object(creator, "delete_named_instance") as delete,
        ):
            with self.assertRaises(ghost_live.PhoneError):
                app._ensure_container_deleted(object(), FakeStack(), "a0047", None)
        delete.assert_not_called()

    def test_run_deletion_only_pauses_on_failure_and_preserves_the_queue(self):
        app = bare_app()
        pauses: list = []
        app._pause_bulk = lambda phase, reason, **kw: pauses.append(
            (phase, kw.get("code"))
        )
        app._connect = lambda _pw: (mock.Mock(), mock.Mock())
        with mock.patch.object(
            app,
            "_ensure_container_deleted",
            side_effect=ghost_live.PhoneError("container still present"),
        ):
            self.assertFalse(app._run_deletion_only("offline-password", "a0047"))
        self.assertEqual(pauses, [("creation", "container_deletion_incomplete")])

    def test_run_deletion_only_does_not_overwrite_a_user_stop(self):
        # A user Stop must leave the batch in its "stopped" state instead of
        # being relabelled as a deletion failure.
        app = bare_app()
        app.stop.set()
        pauses: list = []
        app._pause_bulk = lambda *_a, **_k: pauses.append(True)
        app._connect = lambda _pw: (mock.Mock(), mock.Mock())
        with mock.patch.object(
            app,
            "_ensure_container_deleted",
            side_effect=ghost_live.PhoneError("Stopped."),
        ):
            self.assertFalse(app._run_deletion_only("offline-password", "a0047"))
        self.assertEqual(pauses, [])

    def test_run_deletion_only_never_starts_ai_cleanup(self):
        app = bare_app()
        app._bulk_live_status = lambda _message: None
        app._connect = lambda _pw: (mock.Mock(), mock.Mock())
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, kwargs.get("code"))
        )
        attempts = []

        def cleanup(*_args, **_kwargs):
            attempts.append("cleanup")
            raise ghost_live.PhoneError("container cleanup screen is stuck")

        class NeverAgent:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("deterministic deletion must never construct AI")

        with (
            mock.patch.object(app, "_ensure_container_deleted", side_effect=cleanup),
            mock.patch.object(creator, "UIRecoveryAgent", NeverAgent),
        ):
            self.assertFalse(app._run_deletion_only("offline-password", "a0047"))

        self.assertEqual(attempts, ["cleanup"])
        self.assertEqual(pauses, [("creation", "container_deletion_incomplete")])


# --------------------------------------------------------------------------- #
# Controller batch loop gate: deletion may never be bypassed
# --------------------------------------------------------------------------- #


def make_app(case, count=1):
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
    app._record_bulk_ip = lambda result, _o: result["verification_evidence"][
        "confirmations"
    ][-1]["observed_at_utc"]
    app._pause_bulk = lambda phase, reason, **kwargs: case.fail(
        f"unexpected pause: {phase}: {reason}"
    )
    app.status = mock.Mock()
    app.bulk_state = bulk_creation.empty_state()
    app.bulk_state.update(
        queue=[
            {"birth_date": f"01.01.{2002 + i}", "swipes": 10 + i, "delay_seconds": 2.0}
            for i in range(count)
        ],
        requested=count,
        remaining=count,
        stage="creating",
        slot_id="a0047",
        delete_containers=True,
        verified_ip="198.51.100.47",
        verified_at_utc="2026-10-16T12:00:00Z",
    )
    return app


@contextmanager
def loop_ctx(row, complete_when):
    """Common patches that drive one offline _run_bulk_batch iteration."""
    with (
        mock.patch.object(creator, "save_bulk_state", lambda _state: None),
        mock.patch.object(creator, "get_slot", lambda _sid: row),
        mock.patch.object(
            creator, "_pipeline_slot_complete", lambda row_arg: complete_when(row_arg)
        ),
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
    ):
        yield


class ControllerDeletionGateTests(unittest.TestCase):
    def test_verified_account_with_pending_deletion_pauses_and_preserves_queue(self):
        # The account is verified and complete, but the required local-container
        # deletion never happened.  A successful-looking return must not advance
        # the queue; the batch must pause with the item preserved.
        app = make_app(self, 1)
        row = container_row()
        state = {"complete": False}
        pauses: list = []
        app._pause_bulk = lambda phase, reason, **kw: pauses.append(
            (phase, kw.get("code"))
        )
        app._retry_completed_cleanup_after_failure = (
            lambda _sid, reason: app._pause_bulk(
                "creation", reason, code="container_deletion_incomplete"
            )
            or False
        )

        def pipeline(*_args, **_kwargs):
            state["complete"] = True
            app._last_pipeline_failure_reason = (
                "Ghost container a0047 ABC1234 is still present after deletion."
            )
            return False

        app._run_creation_pipeline = pipeline
        with loop_ctx(row, lambda r: state["complete"] and bool(r)):
            creator.App._run_bulk_batch(app)

        self.assertEqual(pauses, [("creation", "container_deletion_incomplete")])
        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(len(app.bulk_state["queue"]), 1)

    def test_verified_account_with_completed_deletion_advances(self):
        app = make_app(self, 1)
        row = container_row(container_deletion=complete_record())
        app._run_creation_pipeline = lambda *_a, **_k: self.fail(
            "a fully finished slot must not replay the pipeline"
        )
        with loop_ctx(row, lambda r: bool(r)):
            creator.App._run_bulk_batch(app)

        self.assertEqual(app.bulk_state["stage"], "idle")
        self.assertEqual(app.bulk_state["remaining"], 0)

    def test_deletion_only_resume_skips_pipeline_and_advances(self):
        app = make_app(self, 1)
        row = container_row()  # complete account, deletion still pending
        resumed: list = []
        app._run_deletion_only = lambda password, sid: resumed.append(
            (password, sid)
        ) or True
        app._run_creation_pipeline = lambda *_a, **_k: self.fail(
            "deletion-only resume must not call the pipeline"
        )
        with loop_ctx(row, lambda r: bool(r)):
            creator.App._run_bulk_batch(app)

        self.assertEqual(resumed, [("offline-password", "a0047")])
        self.assertEqual(app.bulk_state["stage"], "idle")
        self.assertEqual(app.bulk_state["remaining"], 0)

    def test_deletion_only_resume_failure_preserves_the_queue(self):
        app = make_app(self, 1)
        row = container_row()
        app._run_deletion_only = lambda password, sid: False
        app._run_creation_pipeline = lambda *_a, **_k: self.fail(
            "must not fall through to the pipeline"
        )
        cleanup_retries = []
        app._retry_completed_cleanup_after_failure = (
            lambda sid, reason: cleanup_retries.append((sid, reason)) or False
        )
        with loop_ctx(row, lambda r: bool(r)):
            creator.App._run_bulk_batch(app)

        self.assertEqual(app.bulk_state["remaining"], 1)
        self.assertEqual(len(app.bulk_state["queue"]), 1)
        # `_run_deletion_only` alone is responsible for the pause; the loop must
        # simply not advance or start the next item.  The bounded cleanup retry
        # policy is invoked without sleeping in this offline test.
        self.assertEqual(app.bulk_state["stage"], "creating")
        self.assertEqual(cleanup_retries[0][0], "a0047")

    def test_policy_false_keeps_the_previous_advance_behavior(self):
        # A pre-policy batch (delete_containers False) must advance on account
        # completion exactly as before, even with no deletion record.
        app = make_app(self, 1)
        app.bulk_state["delete_containers"] = False
        row = container_row()
        app._run_creation_pipeline = lambda *_a, **_k: True
        with loop_ctx(row, lambda r: bool(r)):
            creator.App._run_bulk_batch(app)

        self.assertEqual(app.bulk_state["stage"], "idle")
        self.assertEqual(app.bulk_state["remaining"], 0)

    def test_pipeline_receives_the_delete_requirement(self):
        app = make_app(self, 1)
        row = container_row()
        seen: list = []
        state = {"complete": False}

        def pipeline(*_args, **kwargs):
            seen.append(kwargs.get("delete_container_required"))
            state["complete"] = True
            return False

        app._run_creation_pipeline = pipeline
        app._pause_bulk = lambda *_a, **_k: None
        app._retry_completed_cleanup_after_failure = lambda *_a, **_k: False
        with loop_ctx(row, lambda r: state["complete"] and bool(r)):
            creator.App._run_bulk_batch(app)
        self.assertEqual(seen, [True])


# --------------------------------------------------------------------------- #
# Bulk state v6: a durable per-batch deletion policy
# --------------------------------------------------------------------------- #


class BulkStateV6Tests(unittest.TestCase):
    def test_empty_state_is_v6_with_policy_off(self):
        state = bulk_creation.empty_state()
        self.assertEqual(state["version"], 6)
        self.assertIs(state["delete_containers"], False)

    def test_v5_state_migrates_without_retroactive_deletion(self):
        v5 = bulk_creation.empty_state()
        v5["version"] = 5
        v5.pop("delete_containers")
        v5.update(
            queue=[{"birth_date": "01.01.2000", "swipes": 10, "delay_seconds": 2.0}],
            requested=1,
            remaining=1,
            stage="paused",
            slot_id="a0069",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bulk.json"
            path.write_text(json.dumps(v5), encoding="utf-8")
            loaded = bulk_creation.load_state(path)
            self.assertEqual(loaded["version"], 6)
            self.assertIs(loaded["delete_containers"], False)
            self.assertEqual(loaded["stage"], "paused")
            self.assertEqual(loaded["remaining"], 1)
            self.assertEqual(loaded["slot_id"], "a0069")
            self.assertEqual(len(loaded["queue"]), 1)
            # The migration is persisted so a restart sees the same policy.
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["version"], 6
            )

    def test_v6_round_trips(self):
        state = bulk_creation.empty_state()
        state["delete_containers"] = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bulk.json"
            bulk_creation.save_state(state, path)
            loaded = bulk_creation.load_state(path)
        self.assertEqual(loaded, state)

    def test_non_bool_policy_is_rejected(self):
        state = bulk_creation.empty_state()
        state["delete_containers"] = "yes"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bulk.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(bulk_creation.BulkError):
                bulk_creation.load_state(path)

    def test_consume_and_extend_preserve_the_policy(self):
        state = bulk_creation.empty_state()
        state.update(
            delete_containers=True,
            queue=[{"birth_date": "01.01.2000", "swipes": 10, "delay_seconds": 2.0}],
            requested=1,
            remaining=1,
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        consumed = bulk_creation.consume_completed(
            state, "a0047", ready_at_utc="2026-10-17T00:00:00Z"
        )
        self.assertIs(consumed["delete_containers"], True)
        extended = bulk_creation.extend_queue(
            state, [{"birth_date": "01.01.2001", "swipes": 11, "delay_seconds": 2.0}]
        )
        self.assertIs(extended["delete_containers"], True)

    def test_delete_enabled_completion_requires_fresh_ip_for_next_item(self):
        state = bulk_creation.empty_state()
        item = {"birth_date": "01.01.2000", "swipes": 10, "delay_seconds": 2.0}
        state.update(
            delete_containers=True,
            queue=[item, dict(item)],
            requested=2,
            remaining=2,
            stage="creating",
            slot_id="a0047",
            baseline_ip="198.51.100.46",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        consumed = bulk_creation.consume_completed(
            state, "a0047", ready_at_utc="2026-10-17T00:00:00Z"
        )
        self.assertEqual(consumed["stage"], "between")
        self.assertIsNone(consumed["verified_ip"])
        self.assertIsNone(consumed["verified_at_utc"])


# --------------------------------------------------------------------------- #
# Container-prep optimization: reuse a fresh complete inventory
# --------------------------------------------------------------------------- #


class ContainerPrepReuseTests(unittest.TestCase):
    @staticmethod
    def _needed_row(name="a0047"):
        return {
            "id": name,
            "ghost": True,
            "ghost_container_state": "needed",
            "email": None,
        }

    def test_reuse_skips_reopen_and_second_scan_and_uses_inventory_ids(self):
        row = self._needed_row()
        baselines: list = []
        closes: list = []
        with (
            mock.patch.object(ghost_live, "get_slot", lambda _n: row),
            mock.patch.object(
                ghost_live,
                "close_ghost_organic",
                lambda *_a, **_k: closes.append(True),
            ),
            mock.patch.object(
                ghost_live,
                "open_reddit_containers",
                side_effect=AssertionError("must not reopen the list"),
            ),
            mock.patch.object(
                ghost_live, "dump_named", side_effect=AssertionError("must not dump")
            ),
            mock.patch.object(
                ghost_live,
                "scan_named_container_instances",
                side_effect=AssertionError("must not rescan"),
            ),
            mock.patch.object(
                ghost_live,
                "mark_ghost_container_provisioning",
                lambda name, baseline, _log: baselines.append(set(baseline)),
            ),
            mock.patch.object(
                ghost_live, "ensure_new_container", lambda s, p, lg, n: n
            ),
            mock.patch.object(
                ghost_live, "create_named", lambda *_a, **_k: "NEW1234"
            ),
            mock.patch.object(ghost_live, "formulate_named", lambda *_a: None),
            mock.patch.object(ghost_live, "advanced_named", lambda *_a: None),
            mock.patch.object(
                ghost_live, "mark_ghost_container_ready", lambda *_a: None
            ),
        ):
            instance_id = ghost_live.provision_slot_container(
                object(),
                FakeStack(),
                lambda _m: None,
                "a0047",
                inventory={},
                nodes=["bottom-frame"],
            )
        self.assertEqual(instance_id, "NEW1234")
        self.assertEqual(baselines, [set()])
        # Exactly one close: the final, unchanged teardown.  The redundant
        # close/reopen before a fresh scan is gone.
        self.assertEqual(len(closes), 1)

    def test_reuse_requires_a_proven_frame(self):
        row = self._needed_row()
        with (
            mock.patch.object(ghost_live, "get_slot", lambda _n: row),
            mock.patch.object(
                ghost_live, "mark_ghost_container_provisioning", lambda *_a: None
            ),
        ):
            with self.assertRaises(ghost_live.ContainerProvisioningReconcileError):
                ghost_live.provision_slot_container(
                    object(),
                    FakeStack(),
                    lambda _m: None,
                    "a0047",
                    inventory={"a0047": {"OLD11111"}},
                    nodes=None,
                )

    def test_needed_slot_with_existing_exact_name_never_creates(self):
        row = self._needed_row()
        with (
            mock.patch.object(ghost_live, "get_slot", lambda _n: row),
            mock.patch.object(
                ghost_live,
                "mark_ghost_container_provisioning",
                side_effect=AssertionError("must not persist a Create intent"),
            ),
            mock.patch.object(
                ghost_live,
                "create_named",
                side_effect=AssertionError("must not Create over an existing name"),
            ),
        ):
            with self.assertRaisesRegex(
                ghost_live.ContainerProvisioningReconcileError,
                "already contains 1 exact a0047",
            ):
                ghost_live.provision_slot_container(
                    object(),
                    FakeStack(),
                    lambda _m: None,
                    "a0047",
                    inventory={"a0047": {"OLD11111"}},
                    nodes=["bottom-frame"],
                )

    def test_provisioning_baseline_rows_do_not_authorize_another_create(self):
        row = {
            **self._needed_row(),
            "ghost_container_state": "provisioning",
            "ghost_container_baseline": ["OLD11111"],
        }
        with (
            mock.patch.object(ghost_live, "get_slot", lambda _n: row),
            mock.patch.object(
                ghost_live,
                "create_named",
                side_effect=AssertionError("must not issue a second Create"),
            ),
        ):
            with self.assertRaisesRegex(
                ghost_live.ContainerProvisioningReconcileError,
                "none is a uniquely reconciled new instance",
            ):
                ghost_live.provision_slot_container(
                    object(),
                    FakeStack(),
                    lambda _m: None,
                    "a0047",
                    inventory={"a0047": {"OLD11111"}},
                    nodes=["bottom-frame"],
                )

    def test_checkpoint_write_failure_is_typed_before_create(self):
        row = self._needed_row()
        with (
            mock.patch.object(ghost_live, "get_slot", lambda _n: row),
            mock.patch.object(
                ghost_live,
                "mark_ghost_container_provisioning",
                side_effect=OSError("disk full"),
            ),
            mock.patch.object(
                ghost_live,
                "create_named",
                side_effect=AssertionError("must not Create without checkpoint"),
            ),
        ):
            with self.assertRaisesRegex(
                ghost_live.ContainerProvisioningReconcileError,
                "persist the pre-Create Ghost inventory proof",
            ) as caught:
                ghost_live.provision_slot_container(
                    object(),
                    FakeStack(),
                    lambda _m: None,
                    "a0047",
                    inventory={},
                    nodes=["bottom-frame"],
                )
        self.assertEqual(
            caught.exception.failure_code,
            "container_provisioning_reconcile_failed",
        )

    def test_ready_checkpoint_write_failure_is_typed_after_single_create(self):
        row = self._needed_row()
        creates: list[str] = []
        with (
            mock.patch.object(ghost_live, "get_slot", lambda _n: row),
            mock.patch.object(
                ghost_live, "mark_ghost_container_provisioning", lambda *_a: None
            ),
            mock.patch.object(
                ghost_live, "ensure_new_container", lambda _s, _p, _lg, nodes: nodes
            ),
            mock.patch.object(
                ghost_live,
                "create_named",
                lambda *_a, **_k: creates.append("NEW1234") or "NEW1234",
            ),
            mock.patch.object(ghost_live, "formulate_named", lambda *_a: None),
            mock.patch.object(ghost_live, "advanced_named", lambda *_a: None),
            mock.patch.object(
                ghost_live,
                "mark_ghost_container_ready",
                side_effect=OSError("write failed"),
            ),
        ):
            with self.assertRaisesRegex(
                ghost_live.ContainerProvisioningReconcileError,
                "persist verified Ghost container",
            ):
                ghost_live.provision_slot_container(
                    object(),
                    FakeStack(),
                    lambda _m: None,
                    "a0047",
                    inventory={},
                    nodes=["bottom-frame"],
                )
        self.assertEqual(creates, ["NEW1234"])

    def test_without_inventory_takes_the_original_scanned_path(self):
        row = self._needed_row()
        scans: list = []
        with (
            mock.patch.object(ghost_live, "get_slot", lambda _n: row),
            mock.patch.object(ghost_live, "close_ghost_organic", lambda *_a: None),
            mock.patch.object(
                ghost_live, "open_reddit_containers", lambda *_a, **_k: None
            ),
            mock.patch.object(ghost_live, "dump_named", lambda *_a, **_k: ("", ["f"])),
            mock.patch.object(ghost_live, "stop_if_reset", lambda *_a: None),
            mock.patch.object(
                ghost_live,
                "scan_all_container_instances",
                lambda *_a, **_k: scans.append(True) or ({}, ["f"]),
            ),
            mock.patch.object(
                ghost_live, "mark_ghost_container_provisioning", lambda *_a: None
            ),
            mock.patch.object(
                ghost_live, "ensure_new_container", lambda s, p, lg, n: n
            ),
            mock.patch.object(ghost_live, "create_named", lambda *_a, **_k: "NEW1234"),
            mock.patch.object(ghost_live, "formulate_named", lambda *_a: None),
            mock.patch.object(ghost_live, "advanced_named", lambda *_a: None),
            mock.patch.object(
                ghost_live, "mark_ghost_container_ready", lambda *_a: None
            ),
        ):
            instance_id = ghost_live.provision_slot_container(
                object(), FakeStack(), lambda _m: None, "a0047"
            )
        self.assertEqual(instance_id, "NEW1234")
        self.assertEqual(scans, [True])

    def test_ready_state_still_short_circuits_without_any_device_work(self):
        row = container_row()
        with (
            mock.patch.object(ghost_live, "get_slot", lambda _n: row),
            mock.patch.object(
                ghost_live,
                "close_ghost_organic",
                side_effect=AssertionError("must not touch the device"),
            ),
            mock.patch.object(
                ghost_live,
                "open_reddit_containers",
                side_effect=AssertionError("must not touch the device"),
            ),
        ):
            instance_id = ghost_live.provision_slot_container(
                object(), FakeStack(), lambda _m: None, "a0047"
            )
        self.assertEqual(instance_id, "ABC1234")

    def test_apply_reuses_only_the_first_unmutated_target(self):
        seen: list = []

        def fake_provision(
            phone, stack, lg, name, stop=None, *, inventory=None, nodes=None
        ):
            seen.append((name, inventory is not None, nodes is not None))
            return f"NEW{name}"

        with (
            mock.patch.object(ghost_live, "close_ghost_organic", lambda *_a: None),
            mock.patch.object(
                ghost_live, "open_reddit_containers", lambda *_a, **_k: None
            ),
            mock.patch.object(ghost_live, "dump_named", lambda *_a, **_k: ("", ["f"])),
            mock.patch.object(ghost_live, "stop_if_reset", lambda *_a: None),
            mock.patch.object(
                ghost_live,
                "scan_all_container_instances",
                lambda *_a, **_k: ({}, ["bottom"]),
            ),
            mock.patch.object(
                ghost_live,
                "ensure_ghost_container_needed",
                lambda name, _log: {"id": name, "ghost_container_state": "needed"},
            ),
            mock.patch.object(
                ghost_live, "ghost_container_state", lambda _row: "needed"
            ),
            mock.patch.object(ghost_live, "unretire_slot_id", lambda _n: False),
            mock.patch.object(ghost_live, "slot_ids_used", lambda: set()),
            mock.patch.object(ghost_live, "provision_slot_container", fake_provision),
        ):
            created = ghost_live.apply_reddit_containers(
                object(),
                FakeStack(),
                lambda _m: None,
                count=2,
                close=False,
                names=["a0047", "a0048"],
            )
        self.assertEqual(created, ["a0047", "a0048"])
        # First target reuses the fresh inventory; the second must not, because
        # the first create invalidated it.
        self.assertEqual(seen, [("a0047", True, True), ("a0048", False, False)])


# --------------------------------------------------------------------------- #
# Manual paths must not rebuild or post to a deleted container
# --------------------------------------------------------------------------- #


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value


class ManualPathGuardTests(unittest.TestCase):
    def test_manual_create_reddit_refuses_a_deleted_container(self):
        app = creator.App.__new__(creator.App)
        app.worker = None
        app.log = lambda _message: None
        app._selected_id = lambda: "a0047"
        row = container_row(container_deletion=complete_record())
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: row),
            mock.patch.object(
                app,
                "_continue_create_reddit",
                side_effect=AssertionError("a deleted container must not restart"),
            ),
        ):
            creator.App.on_create_reddit(app)

    def test_manual_post_refuses_a_deleted_container(self):
        app = creator.App.__new__(creator.App)
        app.log = lambda _message: None
        app._selected_container_id = lambda: "a0047"
        app.post_title = FakeVar("Title")
        app.post_images = ["C:/images/post.jpg"]
        app.post_image = FakeVar("")
        app.post_dest = FakeVar("profile")
        row = container_row(container_deletion=complete_record())
        with (
            mock.patch.object(creator, "get_slot", lambda _sid: row),
            mock.patch.object(creator.os.path, "isfile", lambda _p: True),
            mock.patch.object(
                app,
                "_start_worker",
                side_effect=AssertionError("a deleted container must not post"),
            ),
        ):
            creator.App.on_post(app)


if __name__ == "__main__":
    unittest.main()
