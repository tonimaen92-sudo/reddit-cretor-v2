"""Crash-safe bulk-state coverage for the Xiaomi radio fallback journal."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from friend_poster.core import bulk_creation


HEAD = {
    "birth_date": "01.01.2000",
    "swipes": 12,
    "delay_seconds": 2.5,
    "username": "PrivateUser01",
    "password": "private-password",
}
TAIL = {
    "birth_date": "02.02.2000",
    "swipes": 13,
    "delay_seconds": 3.0,
}


def active_state(*, stage: str = "paused") -> dict:
    state = bulk_creation.empty_state()
    state.update(
        queue=[copy.deepcopy(HEAD), copy.deepcopy(TAIL)],
        requested=2,
        remaining=2,
        stage=stage,
        slot_id="a0266",
        failure_phase="ip" if stage == "paused" else None,
    )
    if stage == "creating":
        state.update(
            verified_ip="198.51.100.2",
            verified_at_utc="2026-10-23T10:00:00Z",
        )
    return state


def with_radio_journal(state: dict, journal_state: str = "verify_pending") -> dict:
    updated = copy.deepcopy(state)
    updated["ip_reset_attempt_count"] = 2
    updated["radio_recovery"] = {
        "version": bulk_creation.RADIO_RECOVERY_VERSION,
        "state": journal_state,
        "attempt": 2,
        "head_fingerprint": bulk_creation.radio_recovery_head_fingerprint(updated),
        "operation_id": str(uuid.uuid4()),
        "updated_at_utc": "2026-10-23T10:00:00Z",
    }
    bulk_creation._validate(updated)
    return updated


class BulkRadioRecoveryStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bulk_creation.json"

    def test_empty_state_is_v7_with_an_empty_credential_free_journal(self):
        state = bulk_creation.empty_state()

        self.assertEqual(state["version"], 7)
        self.assertEqual(state["ip_reset_attempt_count"], 0)
        self.assertEqual(state["radio_recovery"], bulk_creation.empty_radio_recovery())
        self.assertFalse(
            {"username", "password", "birth_date", "ip"}
            & set(state["radio_recovery"])
        )
        bulk_creation._validate(state)

    def test_v6_migration_preserves_head_and_marks_spent_remote_phase_once(self):
        for code, expected_count in (
            (None, 0),
            ("ip_auto_retry_pending", 1),
            ("ip_auto_retry_scheduled", 1),
            ("ip_retry_exhausted", 1),
        ):
            with self.subTest(code=code):
                v6 = active_state()
                v6["version"] = 6
                v6["failure_code"] = code
                del v6["ip_reset_attempt_count"]
                del v6["radio_recovery"]
                self.path.write_text(json.dumps(v6), encoding="utf-8")

                migrated = bulk_creation.load_state(self.path)

                self.assertEqual(migrated["version"], 7)
                self.assertEqual(migrated["queue"], v6["queue"])
                self.assertEqual(migrated["remaining"], 2)
                self.assertEqual(migrated["slot_id"], "a0266")
                self.assertEqual(migrated["ip_reset_attempt_count"], expected_count)
                self.assertEqual(
                    migrated["radio_recovery"], bulk_creation.empty_radio_recovery()
                )
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), migrated)

        for active_stage in ("may_have_triggered", "retry_ip"):
            with self.subTest(stage=active_stage):
                v6 = active_state(stage=active_stage)
                deadline = "2026-10-23T10:03:00Z"
                baseline = {
                    "operation_id": "1" * 32,
                    "check_sequence": 1,
                    "phase": "baseline",
                    "ip": "198.51.100.1",
                    "observed_at_utc": "2026-10-23T10:00:00Z",
                }
                v6.update(
                    baseline_ip=baseline["ip"],
                    trigger_deadline_utc=deadline,
                    ip_attempt={
                        "operation_id": baseline["operation_id"],
                        "proxy_scope": "a" * 64,
                        "verification_target": "https://api64.ipify.org?format=json",
                        "baseline": baseline,
                        "check_sequence": 1,
                        "candidate": None,
                        "confirmation_floor": 1,
                        "verification_evidence": None,
                        "observation_deadline_utc": deadline,
                    },
                )
                v6["version"] = 6
                del v6["ip_reset_attempt_count"]
                del v6["radio_recovery"]
                self.path.write_text(json.dumps(v6), encoding="utf-8")
                migrated = bulk_creation.load_state(self.path)
                self.assertEqual(migrated["ip_reset_attempt_count"], 1)

    def test_every_radio_phase_round_trips_and_is_bound_to_exact_head(self):
        for phase in bulk_creation.RADIO_RECOVERY_STATES - {"none"}:
            with self.subTest(phase=phase):
                state = with_radio_journal(active_state(), phase)
                bulk_creation.save_state(state, self.path)
                self.assertEqual(bulk_creation.load_state(self.path), state)

        wrong_head = with_radio_journal(active_state())
        wrong_head["slot_id"] = "a0267"
        with self.assertRaisesRegex(bulk_creation.BulkError, "radio recovery"):
            bulk_creation._validate(wrong_head)

        final_fallback = with_radio_journal(active_state(), "completed")
        final_fallback["ip_reset_attempt_count"] = 3
        final_fallback["radio_recovery"]["attempt"] = 3
        bulk_creation._validate(final_fallback)

    def test_malformed_or_credential_bearing_journals_fail_closed(self):
        valid = with_radio_journal(active_state())
        corruptions = []
        for field, value in (
            ("state", "airplane_maybe"),
            ("attempt", 1),
            ("operation_id", "not-an-operation"),
            ("updated_at_utc", "tomorrow"),
        ):
            bad = copy.deepcopy(valid)
            bad["radio_recovery"][field] = value
            corruptions.append(bad)
        bad = copy.deepcopy(valid)
        bad["radio_recovery"]["password"] = "must-not-be-saved"
        corruptions.append(bad)
        bad = copy.deepcopy(valid)
        bad["ip_reset_attempt_count"] = 4
        bad["radio_recovery"]["attempt"] = 4
        corruptions.append(bad)

        for bad in corruptions:
            with self.subTest(radio=bad["radio_recovery"]), self.assertRaises(
                bulk_creation.BulkError
            ):
                bulk_creation._validate(bad)

    def test_retry_and_same_head_reconcile_preserve_counter_and_journal(self):
        state = with_radio_journal(active_state())

        retried = bulk_creation.prepare_retry(
            state, now=datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
        )
        self.assertEqual(retried["ip_reset_attempt_count"], 2)
        self.assertEqual(retried["radio_recovery"], state["radio_recovery"])

        stopped = copy.deepcopy(state)
        stopped.update(stage="stopped", failure_phase=None)
        reconciled = bulk_creation.reconcile_stopped_start(
            stopped,
            [copy.deepcopy(TAIL)],
            1,
            slot_id="a0266",
            saved_birth=HEAD["birth_date"],
        )
        self.assertEqual(reconciled["ip_reset_attempt_count"], 2)
        self.assertEqual(reconciled["radio_recovery"], state["radio_recovery"])
        self.assertEqual(reconciled["queue"][0], HEAD)

    def test_all_head_consumption_paths_clear_counter_and_journal(self):
        cases = []

        completed = with_radio_journal(active_state(stage="creating"), "completed")
        completed.update(
            verified_ip="198.51.100.2",
            verified_at_utc="2026-10-23T10:00:00Z",
        )
        cases.append(lambda: bulk_creation.consume_completed(
            completed, "a0266", ready_at_utc="2026-10-23T10:01:00Z"
        ))

        failed = with_radio_journal(active_state(stage="creating"), "completed")
        failed.update(
            verified_ip="198.51.100.2",
            verified_at_utc="2026-10-23T10:00:00Z",
        )
        cases.append(lambda: bulk_creation.consume_failed(
            failed,
            "a0266",
            code="creation_failed",
            reason="sanitized",
            ready_at_utc="2026-10-23T10:01:00Z",
        ))

        stalled = with_radio_journal(active_state(stage="creating"), "completed")
        stalled.update(
            verified_ip="198.51.100.2",
            verified_at_utc="2026-10-23T10:00:00Z",
        )
        cases.append(lambda: bulk_creation.consume_stalled(
            stalled, "a0266", ready_at_utc="2026-10-23T10:01:00Z"
        ))

        for consume in cases:
            advanced = consume()
            self.assertEqual(advanced["queue"], [TAIL])
            self.assertEqual(advanced["ip_reset_attempt_count"], 0)
            self.assertEqual(
                advanced["radio_recovery"], bulk_creation.empty_radio_recovery()
            )
            bulk_creation._validate(advanced)


if __name__ == "__main__":
    unittest.main()
