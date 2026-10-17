"""Offline queue validation and persistence for Creator's sequential batch."""

from __future__ import annotations

import tempfile
import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import bulk_creation
import slots


def adult(raw: str) -> date:
    day, month, year = map(int, raw.split("."))
    result = date(year, month, day)
    if result > date(2008, 1, 1):
        raise ValueError("underage")
    return result


class BulkQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bulk_creation.json"

    def test_aligned_usernames_round_trip_and_legacy_rows_remain_valid(self):
        requested, queue = bulk_creation.parse_queue(
            "2", "01.01.2000\n02.02.2001", "10\n11", "2\n3", adult,
            usernames_text="BlueHarbor_01\nQuiet.Moon-2",
        )
        self.assertEqual(requested, 2)
        self.assertEqual([row["username"] for row in queue], ["BlueHarbor_01", "Quiet.Moon-2"])
        state = bulk_creation.empty_state()
        state.update(queue=queue, requested=2, remaining=2, stage="ip_pending")
        bulk_creation.save_state(state, self.path)
        self.assertEqual(bulk_creation.load_state(self.path)["queue"], queue)
        paused = dict(
            state, stage="paused", slot_id="a0049", failure_phase="creation",
            verified_ip="198.51.100.2", verified_at_utc="2026-10-16T12:00:00Z",
        )
        self.assertEqual(bulk_creation.prepare_retry(paused)["queue"], queue)
        _, legacy = bulk_creation.parse_queue("1", "01.01.2000", "10", "2", adult)
        self.assertNotIn("username", legacy[0])

    def test_username_alignment_invalid_chars_and_duplicate_are_rejected(self):
        fields = ("2", "01.01.2000\n02.02.2001", "10\n11", "2\n3", adult)
        for names in ("OneName", "OneName\n", "OneName\nAnother Name", "OneName\nonename"):
            with self.subTest(names=names), self.assertRaises(bulk_creation.BulkError):
                bulk_creation.parse_queue(*fields, usernames_text=names)

    def test_five_thousand_entries_and_first_n_consumption(self):
        births = "\n".join("01.01.2000" for _ in range(5000))
        swipes = "\n".join(str(10 + i % 3) for i in range(5000))
        delays = "\n".join(str(2 + i % 6) for i in range(5000))
        requested, queue = bulk_creation.parse_queue("15", births, swipes, delays, adult)
        self.assertEqual((requested, len(queue)), (15, 5000))
        state = bulk_creation.empty_state()
        state.update(queue=queue, requested=requested)
        bulk_creation.save_state(state, self.path)
        self.assertEqual(len(bulk_creation.load_state(self.path)["queue"]), 5000)
        state.update(remaining=15, stage="creating", slot_id="a0001",
                     verified_ip="198.51.100.1", verified_at_utc="2026-10-16T12:00:00Z")
        bulk_creation.save_state(state, self.path)
        first = bulk_creation.consume_completed(state, "a0001", ready_at_utc="2026-10-16T12:00:05Z")
        bulk_creation.save_state(first, self.path)
        persisted = bulk_creation.load_state(self.path)
        self.assertEqual((persisted["remaining"], len(persisted["queue"])), (14, 4999))
        self.assertEqual(persisted["queue"][0], queue[1])
        self.assertEqual(persisted["stage"], "between")

    def test_invalid_alignment_and_values_rejected_before_save(self):
        cases = [
            ("2", "01.01.2000", "10\n20", "2\n3"),
            ("1", "01.01.2000", "0", "2"),
            ("1", "01.01.2000", "10", "7.1"),
            ("1", "01.01.2000", "10", "nan"),
            ("1", "01.01.2020", "10", "2"),
            ("1", "01.01.2000\n\n", "10\n11", "2\n3"),
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(bulk_creation.BulkError):
                bulk_creation.parse_queue(*args, adult)
        self.assertFalse(self.path.exists())

    def test_saved_queue_is_idle_until_started(self):
        _, queue = bulk_creation.parse_queue("1", "01.01.2000", "15", "2.5", adult)
        state = bulk_creation.empty_state()
        state.update(queue=queue, requested=1)
        bulk_creation.save_state(state, self.path)
        loaded = bulk_creation.load_state(self.path)
        self.assertEqual((loaded["stage"], loaded["remaining"]), ("idle", 0))

    def test_draft_round_trip_preserves_incomplete_raw_columns(self):
        path = Path(self.temp.name) / "bulk_draft.json"
        draft = bulk_creation.empty_draft()
        draft.update(
            count_text="12",
            births_text="01.01.2000\n02.02.",
            swipes_text="10\n",
            delays_text="2\n3",
            usernames_text="FirstName\nSecond",
            baseline_queue=[
                {"birth_date": "01.01.2000", "swipes": 10, "delay_seconds": 2.0}
            ],
        )
        bulk_creation.save_draft(draft, path)
        self.assertEqual(bulk_creation.load_draft(path), draft)
        self.assertFalse(list(path.parent.glob(".bulk-draft-*.tmp")))

    def test_extend_schedules_unused_tail_and_new_rows_without_changing_checkpoint(self):
        rows = [
            {"birth_date": f"0{i}.01.2000", "swipes": 10 + i, "delay_seconds": 2.0, "username": f"Name{i}"}
            for i in range(1, 5)
        ]
        state = bulk_creation.empty_state()
        state.update(
            queue=rows[:3], requested=2, remaining=2, stage="creating",
            slot_id="a0052", baseline_ip="198.51.100.1",
            verified_ip="198.51.100.2", verified_at_utc="2026-10-16T12:00:00Z",
        )
        extended = bulk_creation.extend_queue(state, [rows[3]])
        self.assertEqual(extended["queue"], rows)
        self.assertEqual((extended["requested"], extended["remaining"]), (4, 4))
        for field in ("stage", "slot_id", "baseline_ip", "verified_ip", "verified_at_utc"):
            self.assertEqual(extended[field], state[field])
        self.assertEqual((state["requested"], state["remaining"], len(state["queue"])), (2, 2, 3))

    def test_extend_rejects_duplicate_and_noop_without_mutating_state(self):
        state = bulk_creation.empty_state()
        state.update(
            queue=[{"birth_date": "01.01.2000", "swipes": 10, "delay_seconds": 2.0, "username": "SameName"}],
            requested=1, remaining=1, stage="stopped",
        )
        original = json.loads(json.dumps(state))
        duplicate = {"birth_date": "02.02.2001", "swipes": 11, "delay_seconds": 3.0, "username": "samename"}
        with self.assertRaisesRegex(bulk_creation.BulkError, "repeats"):
            bulk_creation.extend_queue(state, [duplicate])
        with self.assertRaisesRegex(bulk_creation.BulkError, "no new rows"):
            bulk_creation.extend_queue(state, [])
        self.assertEqual(state, original)

    def test_draft_atomic_replace_failure_keeps_previous_document(self):
        path = Path(self.temp.name) / "bulk_draft.json"
        first = bulk_creation.empty_draft()
        first["count_text"] = "3"
        bulk_creation.save_draft(first, path)
        second = dict(first, count_text="9")
        with mock.patch.object(bulk_creation.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(bulk_creation.BulkError):
                bulk_creation.save_draft(second, path)
        self.assertEqual(bulk_creation.load_draft(path), first)

    def test_legacy_paused_queue_migrates_atomically_without_consuming_a0046(self):
        state = bulk_creation.empty_state()
        state.update(queue=[{"birth_date": "22.05.2003", "swipes": 25, "delay_seconds": 3.0}],
                     requested=1, remaining=1, slot_id="a0046", stage="paused", failure_phase="ip",
                     baseline_ip="31.217.20.252", trigger_deadline_utc="2026-10-16T14:39:52Z")
        del state["ip_attempt"]
        del state["failure_reason"]
        del state["failure_code"]
        del state["failures"]
        state["version"] = 1
        self.path.write_text(json.dumps(state), encoding="utf-8")
        migrated = bulk_creation.load_state(self.path)
        self.assertEqual(migrated["version"], 4)
        self.assertIsNone(migrated["ip_attempt"])
        self.assertIsNone(migrated["failure_reason"])
        self.assertIsNone(migrated["failure_code"])
        self.assertEqual(migrated["failures"], [])
        self.assertEqual({key: migrated[key] for key in state if key != "version"},
                         {key: value for key, value in state.items() if key != "version"})
        self.assertEqual(bulk_creation.load_state(self.path), migrated)
        self.assertEqual(json.loads(self.path.read_text()), migrated)

    def test_failed_legacy_migration_keeps_original_file(self):
        state = bulk_creation.empty_state()
        del state["ip_attempt"]
        del state["failure_reason"]
        del state["failure_code"]
        del state["failures"]
        state["version"] = 1
        original = json.dumps(state)
        self.path.write_text(original, encoding="utf-8")
        with mock.patch.object(bulk_creation.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(bulk_creation.BulkError):
                bulk_creation.load_state(self.path)
        self.assertEqual(self.path.read_text(), original)

    def test_v2_paused_checkpoint_migrates_without_losing_attempt(self):
        state = bulk_creation.empty_state()
        operation = "1" * 32
        baseline = {"operation_id": operation, "check_sequence": 1, "phase": "baseline",
                    "ip": "31.217.14.168", "observed_at_utc": "2026-10-16T18:29:43Z"}
        attempt = {"operation_id": operation, "proxy_scope": "a" * 64,
                   "verification_target": "https://api64.ipify.org?format=json",
                   "baseline": baseline, "candidate": None, "check_sequence": 6,
                   "confirmation_floor": 6, "verification_evidence": None,
                   "observation_deadline_utc": "2026-10-16T18:32:43Z"}
        state.update(queue=[{"birth_date": "22.05.2003", "swipes": 25, "delay_seconds": 3.0}],
                     requested=1, remaining=1, slot_id="a0047", stage="paused", failure_phase="ip",
                     baseline_ip="31.217.14.168", ip_attempt=attempt)
        del state["failure_reason"]
        del state["failure_code"]
        del state["failures"]
        state["version"] = 2
        self.path.write_text(json.dumps(state), encoding="utf-8")
        migrated = bulk_creation.load_state(self.path)
        self.assertEqual(migrated["version"], 4)
        self.assertEqual(migrated["slot_id"], "a0047")
        self.assertEqual(migrated["queue"], state["queue"])
        self.assertEqual(migrated["ip_attempt"], attempt)
        self.assertIsNone(migrated["failure_reason"])

    def test_v3_no_code_pause_migrates_to_typed_recovery_without_consuming_head(self):
        state = bulk_creation.empty_state()
        state.update(
            queue=[{
                "birth_date": "22.05.2003",
                "swipes": 25,
                "delay_seconds": 3.0,
                "username": "QuietMoon49",
            }],
            requested=1,
            remaining=1,
            slot_id="a0049",
            stage="paused",
            failure_phase="creation",
            failure_reason="No 6-digit Reddit code in iCloud IMAP. Last error: TimeoutError.",
            verified_ip="198.51.100.49",
            verified_at_utc="2026-10-16T21:28:00Z",
        )
        del state["failure_code"]
        del state["failures"]
        state["version"] = 3
        self.path.write_text(json.dumps(state), encoding="utf-8")

        migrated = bulk_creation.load_state(self.path)

        self.assertEqual(migrated["version"], 4)
        self.assertEqual(migrated["failure_code"], "reddit_code_timeout")
        self.assertEqual(migrated["queue"], state["queue"])
        self.assertEqual(migrated["remaining"], 1)
        self.assertEqual(migrated["slot_id"], "a0049")

    def test_terminal_failure_records_full_head_and_advances(self):
        state = bulk_creation.empty_state()
        head = {
            "birth_date": "22.05.2003",
            "swipes": 25,
            "delay_seconds": 3.0,
            "username": "QuietMoon49",
        }
        tail = {
            "birth_date": "23.05.2003",
            "swipes": 26,
            "delay_seconds": 4.0,
            "username": "QuietMoon50",
        }
        state.update(
            queue=[head, tail],
            requested=2,
            remaining=2,
            slot_id="a0049",
            stage="creating",
            verified_ip="198.51.100.49",
            verified_at_utc="2026-10-16T21:28:00Z",
        )

        advanced = bulk_creation.consume_failed(
            state,
            "a0049",
            code="reddit_email_replacements_exhausted",
            reason="No verification email after four addresses.",
            ready_at_utc="2026-10-16T21:28:03Z",
        )

        self.assertEqual(advanced["queue"], [tail])
        self.assertEqual(advanced["remaining"], 1)
        self.assertEqual(advanced["stage"], "between")
        self.assertIsNone(advanced["slot_id"])
        self.assertEqual(advanced["failures"][0]["slot_id"], "a0049")
        self.assertEqual(advanced["failures"][0]["item"], head)

    def test_retry_opens_fresh_window_preserving_identity_baseline_and_unused_entries(self):
        state = bulk_creation.empty_state()
        op = "1" * 32
        baseline = {"operation_id": op, "check_sequence": 1, "phase": "baseline",
                    "ip": "198.51.100.1", "observed_at_utc": "2026-10-16T12:00:00Z"}
        candidate = {"operation_id": op, "check_sequence": 4, "phase": "observation",
                     "ip": "198.51.100.2", "observed_at_utc": "2026-10-16T12:02:00Z"}
        state.update(queue=[{"birth_date": "01.01.2000", "swipes": n, "delay_seconds": 3.0} for n in range(1, 5001)],
                     requested=15, remaining=15, stage="paused", slot_id="a0046", failure_phase="ip",
                     baseline_ip=baseline["ip"], trigger_deadline_utc="2026-10-16T12:03:00Z",
                     ip_attempt={"operation_id": op, "proxy_scope": "a" * 64,
                                 "verification_target": "https://api64.ipify.org?format=json",
                                 "baseline": baseline, "candidate": candidate, "check_sequence": 4,
                                 "confirmation_floor": 1, "verification_evidence": None,
                                 "observation_deadline_utc": "2026-10-16T12:03:00Z"})
        retry = bulk_creation.prepare_retry(state, now=datetime(2026, 10, 16, 13, tzinfo=timezone.utc))
        self.assertEqual(state["stage"], "paused")
        self.assertEqual(retry["stage"], "retry_ip")
        self.assertEqual(retry["queue"], state["queue"])
        self.assertEqual(retry["remaining"], 15)
        self.assertEqual(retry["trigger_deadline_utc"], state["trigger_deadline_utc"])
        self.assertEqual(retry["ip_attempt"]["operation_id"], op)
        self.assertEqual(retry["ip_attempt"]["baseline"], baseline)
        self.assertEqual(retry["ip_attempt"]["observation_deadline_utc"], "2026-10-16T13:03:00Z")
        self.assertEqual(retry["ip_attempt"]["confirmation_floor"], 4)
        self.assertIsNone(retry["ip_attempt"]["candidate"])
        bulk_creation.save_state(retry, self.path)
        self.assertEqual(bulk_creation.load_state(self.path), retry)

    def test_failed_or_retried_head_is_not_consumed(self):
        _, queue = bulk_creation.parse_queue("1", "01.01.2000", "15", "2.5", adult)
        state = bulk_creation.empty_state()
        state.update(queue=queue, requested=1, remaining=1, stage="paused", slot_id="a0001")
        bulk_creation.save_state(state, self.path)
        self.assertEqual(bulk_creation.load_state(self.path)["queue"], queue)
        with self.assertRaises(bulk_creation.BulkError):
            bulk_creation.consume_completed(state, "a0002", ready_at_utc="2026-10-16T12:00:05Z")

    def test_stopped_head_is_prepended_to_new_entries_with_verified_ip(self):
        old_head = {"birth_date": "20.04.2005", "swipes": 27, "delay_seconds": 3.0}
        old_tail = {"birth_date": "11.02.2004", "swipes": 22, "delay_seconds": 4.0}
        current = bulk_creation.empty_state()
        current.update(
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
        result = bulk_creation.reconcile_stopped_start(
            current,
            new_items,
            3,
            slot_id="a0047",
            saved_birth="20.04.2005",
        )
        self.assertEqual(result["queue"], [old_head, *new_items])
        self.assertEqual((result["requested"], result["remaining"]), (4, 4))
        self.assertEqual(result["stage"], "retry_creation")
        self.assertEqual(result["verified_ip"], "31.217.18.190")
        self.assertEqual(current["queue"], [old_head, old_tail])

    def test_reconcile_conflict_leaves_original_state_unchanged(self):
        current = bulk_creation.empty_state()
        current.update(
            queue=[{"birth_date": "20.04.2005", "swipes": 27, "delay_seconds": 3.0}],
            requested=1,
            remaining=1,
            stage="stopped",
            slot_id="a0047",
        )
        before = json.loads(json.dumps(current))
        with self.assertRaisesRegex(bulk_creation.BulkError, "a0047.*saved birth date"):
            bulk_creation.reconcile_stopped_start(
                current,
                [{"birth_date": "01.01.2001", "swipes": 10, "delay_seconds": 2.0}],
                1,
                slot_id="a0047",
                saved_birth="21.04.2005",
            )
        self.assertEqual(current, before)

    def test_reconcile_archive_is_timestamped_and_round_trips(self):
        state = bulk_creation.empty_state()
        state.update(
            queue=[{"birth_date": "20.04.2005", "swipes": 27, "delay_seconds": 3.0}],
            requested=1,
            remaining=1,
            stage="stopped",
            slot_id="a0047",
        )
        archived = bulk_creation.archive_state(
            state,
            self.path,
            stamp=datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            archived.name, "bulk_creation.pre-reconcile-20261016-200000.json"
        )
        self.assertEqual(bulk_creation.load_state(archived), state)
        self.assertFalse(self.path.exists())

    def test_proxy_ip_belongs_to_one_slot_and_completed_value_is_immutable(self):
        slot_file = Path(self.temp.name) / "slots.json"
        with mock.patch.object(slots, "LOG_DIR", self.temp.name), mock.patch.object(slots, "SLOTS_PATH", str(slot_file)):
            slots.ensure_ghost_container_needed("a0001")
            slots.ensure_ghost_container_needed("a0002")
            slots.set_slot_falcon_proxy_ip("a0001", "2001:db8::1", "2026-10-16T12:00:00Z")
            rows = slots.load_slots()
            self.assertEqual(rows[0]["falcon_proxy_verified_ip"], "2001:db8::1")
            self.assertNotIn("falcon_proxy_verified_ip", rows[1])
            with mock.patch.object(slots, "slot_reddit_ready", return_value=True):
                with self.assertRaises(ValueError):
                    slots.set_slot_falcon_proxy_ip("a0001", "2001:db8::2", "2026-10-16T12:01:00Z")
            self.assertEqual(slots.load_slots()[0]["falcon_proxy_verified_ip"], "2001:db8::1")

    def test_slot_save_retries_transient_windows_replace_lock(self):
        slot_file = Path(self.temp.name) / "slots.json"
        actual_replace = slots.os.replace
        attempts = []

        def flaky_replace(source, target):
            attempts.append((source, target))
            if len(attempts) < 3:
                raise PermissionError(13, "OneDrive file temporarily locked")
            return actual_replace(source, target)

        with (
            mock.patch.object(slots, "LOG_DIR", self.temp.name),
            mock.patch.object(slots, "SLOTS_PATH", str(slot_file)),
            mock.patch.object(slots.os, "replace", side_effect=flaky_replace),
            mock.patch.object(slots.time, "sleep", return_value=None),
        ):
            slots.save_slots([{"id": "a0048", "reddit": False}])
        self.assertEqual(len(attempts), 3)
        self.assertEqual(json.loads(slot_file.read_text(encoding="utf-8"))[0]["id"], "a0048")


if __name__ == "__main__":
    unittest.main()
