# UTF-8
"""Offline tests for crash-safe username processing/success ownership."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from friend_poster.core import username_lifecycle as lifecycle


class UsernameLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "username_ledger.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_claim_is_atomic_idempotent_and_cannot_move_slots(self):
        first = lifecycle.claim_username("UniqueName7", "a0103", self.path)
        again = lifecycle.claim_username("UniqueName7", "a0103", self.path)

        self.assertEqual(first, again)
        self.assertEqual(first["status"], "processing")
        with self.assertRaisesRegex(lifecycle.UsernameLedgerError, "already bound"):
            lifecycle.claim_username("uniquename7", "a0104", self.path)

    def test_success_keeps_permanent_owner(self):
        lifecycle.claim_username("UniqueName7", "a0103", self.path)
        done = lifecycle.mark_username_success("UniqueName7", "a0103", self.path)

        self.assertEqual(done["status"], "success")
        self.assertTrue(done["completed_at_utc"])
        with self.assertRaises(lifecycle.UsernameLedgerError):
            lifecycle.claim_username("UniqueName7", "a0104", self.path)

    def test_historical_slots_migrate_processing_and_success(self):
        owners = lifecycle.reconcile_from_slots(
            [
                {"id": "a0103", "username": "FinishedName", "reddit": True,
                 "reddit_cleanup": "complete", "created_at": "old"},
                {"id": "a0104", "username": "TypedName", "reddit": False,
                 "reddit_cleanup": None, "created_at": "old"},
            ],
            self.path,
        )

        self.assertEqual(owners["finishedname"]["status"], "success")
        self.assertEqual(owners["typedname"]["status"], "processing")

    def test_malformed_ledger_fails_closed(self):
        self.path.write_text('{"version":1,"claims":[]}', encoding="utf-8")
        with self.assertRaisesRegex(lifecycle.UsernameLedgerError, "malformed"):
            lifecycle.load_ledger(self.path)


if __name__ == "__main__":
    unittest.main()
