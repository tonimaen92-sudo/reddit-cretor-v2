"""Offline regressions for Creator ownership and bridge recovery primitives."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paramiko

from friend_poster.core import bulk_creation
from friend_poster.device.ssh_device import (
    BridgeTransportError,
    PhoneError,
    SSHAuthError,
    is_recoverable_bridge_failure,
)


class RecoveryV5Tests(unittest.TestCase):
    def test_compare_and_swap_rejects_stale_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bulk.json"
            state = bulk_creation.empty_state()
            bulk_creation.save_state(state, path)
            writer = str(uuid.uuid4())
            first = bulk_creation.claim_state(state, writer, new_run=True)
            saved = bulk_creation.save_state_cas(first, 0, path)
            self.assertEqual(saved["revision"], 1)
            with self.assertRaisesRegex(bulk_creation.BulkError, "stale write"):
                bulk_creation.save_state_cas(first, 0, path)

    def test_foreign_writer_requires_explicit_takeover(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bulk.json"
            state = bulk_creation.empty_state()
            bulk_creation.save_state(state, path)
            one = bulk_creation.save_state_cas(
                bulk_creation.claim_state(state, str(uuid.uuid4()), new_run=True),
                0,
                path,
            )
            foreign = bulk_creation.claim_state(one, str(uuid.uuid4()))
            with self.assertRaisesRegex(bulk_creation.BulkError, "owns this batch"):
                bulk_creation.save_state_cas(foreign, 1, path)
            claimed = bulk_creation.save_state_cas(
                foreign, 1, path, allow_writer_takeover=True
            )
            self.assertEqual(claimed["revision"], 2)

    def test_only_infrastructure_errors_are_recoverable(self):
        self.assertTrue(is_recoverable_bridge_failure(
            BridgeTransportError("XXTouch connection reset")
        ))
        self.assertTrue(is_recoverable_bridge_failure(
            PhoneError("SSH stream read failed (1): broken pipe")
        ))
        self.assertFalse(is_recoverable_bridge_failure(
            SSHAuthError("SSH auth failed")
        ))
        self.assertFalse(is_recoverable_bridge_failure(
            PhoneError("Customize screen is ambiguous; did not guess")
        ))

    def test_raw_paramiko_channel_close_is_recoverable_but_auth_is_not(self):
        self.assertTrue(is_recoverable_bridge_failure(
            paramiko.SSHException("Channel closed.")
        ))
        self.assertTrue(is_recoverable_bridge_failure(
            RuntimeError("SSH channel is not open")
        ))
        self.assertFalse(is_recoverable_bridge_failure(
            paramiko.AuthenticationException("Authentication failed")
        ))

    def test_recovery_delays_are_exact(self):
        self.assertEqual(bulk_creation.RECOVERY_DELAYS_SECONDS, (5, 15, 30, 60, 120))

    def test_paused_ip_without_baseline_starts_new_baseline_first_operation(self):
        state = bulk_creation.empty_state()
        state.update(
            queue=[{
                "birth_date": "01.01.2004", "swipes": 20,
                "delay_seconds": 3.0,
            }],
            requested=1,
            remaining=1,
            stage="paused",
            slot_id="a0065",
            failure_phase="ip",
            failure_reason="No pre-reset baseline exists.",
            trigger_deadline_utc="2026-10-17T08:07:23Z",
            ip_attempt={
                "operation_id": "1" * 32,
                "proxy_scope": "a" * 64,
                "verification_target": "https://api64.ipify.org?format=json",
                "baseline": None,
                "check_sequence": 4,
                "candidate": None,
                "confirmation_floor": 4,
                "verification_evidence": None,
                "observation_deadline_utc": "2026-10-17T08:07:23Z",
            },
        )
        resumed = bulk_creation.prepare_retry(
            state, now=datetime(2026, 10, 17, 8, 9, tzinfo=timezone.utc)
        )
        self.assertEqual(resumed["stage"], "ip_pending")
        self.assertEqual(resumed["slot_id"], "a0065")
        self.assertEqual(resumed["remaining"], 1)
        self.assertIsNone(resumed["baseline_ip"])
        self.assertIsNone(resumed["ip_attempt"])
        self.assertIsNone(resumed["trigger_deadline_utc"])


if __name__ == "__main__":
    unittest.main()
