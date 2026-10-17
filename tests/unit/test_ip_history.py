"""Offline persistence and evidence tests for append-only IP history."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from friend_poster.services import ip_history


class HistoryTests(unittest.TestCase):
    proxy = "socks5h://secret-user:secret-password@proxy.example:1080"
    key = "7a" * 32

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ip_history.txt"

    @staticmethod
    def evidence(operation, seq, phase, ip, second):
        return {
            "operation_id": operation,
            "check_sequence": seq,
            "phase": phase,
            "ip": ip,
            "observed_at_utc": f"2026-10-16T12:00:{second:02d}Z",
        }

    def save(self, observations, result, operation=None, proxy=None):
        operation = operation or observations[0]["operation_id"]
        return ip_history.record_operation(
            self.path, proxy_url=proxy or self.proxy, key_hex=self.key,
            operation_id=operation, observations=observations, result=result,
        )

    def verified(self, old, new, operation=None):
        operation = operation or str(uuid.uuid4())
        observations = [
            self.evidence(operation, 1, "baseline", old, 1),
            self.evidence(operation, 2, "observation", new, 2),
            self.evidence(operation, 3, "observation", new, 3),
        ]
        result = {
            "outcome": "verified", "trigger_attempted": True,
            "old_ip": old, "new_ip": new,
            "verification_evidence": {
                "baseline": observations[0], "confirmations": observations[1:],
            },
        }
        return operation, observations, result

    def test_restart_persistence_and_returning_address(self):
        for old, new in [("198.51.100.1", "198.51.100.2"),
                         ("198.51.100.2", "198.51.100.1")]:
            operation, observations, result = self.verified(old, new)
            self.save(observations, result, operation)
        # Read with a fresh call, as a subsequent application run does.
        records = ip_history.read_history(self.path)
        transitions = [r for r in records if r["result"] == "verified_transition"]
        self.assertEqual([(r["old_ip"], r["new_ip"]) for r in transitions], [
            ("198.51.100.1", "198.51.100.2"),
            ("198.51.100.2", "198.51.100.1"),
        ])
        self.assertEqual(records[0]["result"], "initial_observation")
        self.assertEqual(records[0]["old_ip"], None)

    def test_unchanged_retry_and_failed_checks_do_not_create_changes(self):
        operation, observations, result = self.verified("198.51.100.1", "198.51.100.2")
        observations.insert(1, self.evidence(operation, 2, "observation", "198.51.100.1", 2))
        observations[2]["check_sequence"] = 4
        observations[3]["check_sequence"] = 5
        # A failed check is absent from successful observation evidence.
        result["verification_evidence"]["confirmations"] = observations[-2:]
        self.assertEqual(self.save(observations, result, operation), 2)
        self.assertEqual(self.save(observations, result, operation), 0)
        self.assertEqual(len(ip_history.read_history(self.path)), 2)

    def test_verified_status_without_supporting_addresses_is_rejected(self):
        operation, observations, result = self.verified("198.51.100.1", "198.51.100.2")
        result["verification_evidence"] = None
        with self.assertRaises(ip_history.HistoryError):
            self.save(observations, result, operation)
        self.assertFalse(self.path.exists())

    def test_confirmation_allows_failed_request_gaps_but_not_contradictory_observations(self):
        operation, observations, result = self.verified("198.51.100.1", "198.51.100.2")
        observations[-1]["check_sequence"] = 6
        observations[-1]["observed_at_utc"] = "2026-10-16T12:00:53Z"
        self.assertEqual(self.save(observations, result, operation), 2)
        observations.insert(2, self.evidence(operation, 4, "observation", "198.51.100.3", 20))
        with self.assertRaises(ip_history.HistoryError):
            self.save(observations, result, operation)

    def test_confirmation_rejects_expired_pair_and_hard_failure_barrier(self):
        operation, observations, result = self.verified("198.51.100.1", "198.51.100.2")
        observations[-1]["observed_at_utc"] = "2026-10-16T12:01:33Z"
        with self.assertRaises(ip_history.HistoryError):
            self.save(observations, result, operation)
        observations[-1]["observed_at_utc"] = "2026-10-16T12:00:03Z"
        result["verification_evidence"]["confirmation_floor"] = 2
        with self.assertRaises(ip_history.HistoryError):
            self.save(observations, result, operation)

    def test_legacy_baseline_time_stays_unknown_on_read_only_confirmation(self):
        operation, observations, result = self.verified("198.51.100.1", "198.51.100.2")
        observations[0].update(check_sequence=0, observed_at_utc=None, provenance="legacy_saved_baseline")
        result.update(trigger_attempted=False, observation_only=True)
        self.save(observations, result, operation)
        transition = ip_history.read_history(self.path)[-1]
        self.assertEqual(transition["result"], "verified_transition")
        self.assertIsNone(transition["old_observed_at_utc"])

    def test_scope_key_creation_preserves_other_settings_and_reuses_identity(self):
        path = Path(self.temp.name) / "secrets.json"
        path.write_text(json.dumps({"untouched": {"value": 42}}), encoding="utf-8")
        first = ip_history.configured_scope(self.proxy, path)
        second = ip_history.configured_scope(self.proxy, path)
        self.assertEqual(first, second)
        self.assertEqual(json.loads(path.read_text())["untouched"], {"value": 42})
        self.assertNotIn("secret-password", first)

    def test_configured_record_and_latest_transition_hide_private_key(self):
        secrets_path = Path(self.temp.name) / "secrets.json"
        secrets_path.write_text(json.dumps({"untouched": True}), encoding="utf-8")
        operation, observations, result = self.verified("198.51.100.11", "198.51.100.12")
        self.assertEqual(ip_history.record_configured_operation(
            self.path,
            proxy_url=self.proxy,
            secrets_path=secrets_path,
            operation_id=operation,
            observations=observations,
            result=result,
        ), 2)
        # Replaying the same callback is idempotent.
        self.assertEqual(ip_history.record_configured_operation(
            self.path,
            proxy_url=self.proxy,
            secrets_path=secrets_path,
            operation_id=operation,
            observations=observations,
            result=result,
        ), 0)
        latest = ip_history.latest_configured_transition(
            self.path, proxy_url=self.proxy, secrets_path=secrets_path
        )
        self.assertEqual((latest["old_ip"], latest["new_ip"]),
                         ("198.51.100.11", "198.51.100.12"))
        payload = self.path.read_text(encoding="utf-8")
        self.assertNotIn("secret-user", payload)
        self.assertNotIn("secret-password", payload)
        self.assertTrue(json.loads(secrets_path.read_text(encoding="utf-8"))["untouched"])

    def test_latest_transition_is_separate_per_configured_scope(self):
        secrets_path = Path(self.temp.name) / "secrets.json"
        secrets_path.write_text("{}", encoding="utf-8")
        operation, observations, result = self.verified("198.51.100.1", "198.51.100.2")
        ip_history.record_configured_operation(
            self.path, proxy_url=self.proxy, secrets_path=secrets_path,
            operation_id=operation, observations=observations, result=result,
        )
        other = "socks5h://secret-user:other-password@proxy.example:1080"
        self.assertIsNone(ip_history.latest_configured_transition(
            self.path, proxy_url=other, secrets_path=secrets_path
        ))

    def test_no_baseline_is_initial_not_transition(self):
        operation = str(uuid.uuid4())
        observations = [self.evidence(operation, 3, "observation", "2001:db8::2", 3)]
        self.save(observations, {"outcome": "observed_unverified"}, operation)
        record = ip_history.read_history(self.path)[0]
        self.assertEqual((record["result"], record["old_ip"], record["new_ip"]),
                         ("initial_observation", None, "2001:db8::2"))

    def test_single_changed_candidate_stays_unverified(self):
        operation = str(uuid.uuid4())
        observations = [
            self.evidence(operation, 1, "baseline", "198.51.100.4", 1),
            self.evidence(operation, 3, "observation", "198.51.100.5", 3),
        ]
        self.save(observations, {"outcome": "verification_failed"}, operation)
        rows = ip_history.read_history(self.path)
        self.assertEqual([row["result"] for row in rows],
                         ["initial_observation", "observed_unverified"])
        self.assertTrue(all(row["old_ip"] is None for row in rows))

    def test_ipv6_change_and_scope_separation(self):
        operation, observations, result = self.verified("2001:db8::1", "2001:db8::2")
        self.save(observations, result, operation)
        other = "socks5h://secret-user:other-password@proxy.example:1080"
        operation2, observations2, result2 = self.verified("2001:db8::1", "2001:db8::3")
        self.save(observations2, result2, operation2, other)
        rows = ip_history.read_history(self.path)
        self.assertEqual(len({r["scope_id"] for r in rows}), 2)
        self.assertTrue(all(r["proxy_endpoint"] == "proxy.example:1080" for r in rows))

    def test_reimport_is_idempotent_and_unknown_timestamp_stays_unknown(self):
        operation = str(uuid.uuid4())
        self.save([self.evidence(operation, 1, "baseline", "198.51.100.4", 1)],
                  {"outcome": "verification_failed"}, operation)
        source = Path(self.temp.name) / "backup.txt"
        row = ip_history.read_history(self.path)[0]
        row["new_observed_at_utc"] = None
        source.write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.path.unlink()
        self.assertEqual(ip_history.import_history(self.path, source), 1)
        self.assertEqual(ip_history.import_history(self.path, source), 0)
        imported = ip_history.read_history(self.path)[0]
        self.assertIsNone(imported["new_observed_at_utc"])
        self.assertIsNotNone(imported["imported_at_utc"])

    def test_malformed_or_unwritable_history_is_visible_and_not_overwritten(self):
        self.path.write_text("not valid json\n", encoding="utf-8")
        operation = str(uuid.uuid4())
        with self.assertRaises(ip_history.HistoryError):
            self.save([self.evidence(operation, 1, "baseline", "198.51.100.1", 1)],
                      {"outcome": "verification_failed"}, operation)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "not valid json\n")
        with mock.patch.object(ip_history, "_locked", side_effect=OSError("secret")):
            with self.assertRaisesRegex(ip_history.HistoryError, "could not be saved"):
                ip_history.append_events(self.path, [self._sample_record()])

    def _sample_record(self):
        operation = str(uuid.uuid4())
        obs = self.evidence(operation, 1, "baseline", "198.51.100.1", 1)
        scope, endpoint = ip_history.history_scope(self.proxy, self.key)
        return dict(
            version=1, event_id=str(uuid.uuid4()), operation_id=operation,
            scope_id=scope, device_label=ip_history.DEVICE_LABEL,
            proxy_endpoint=endpoint, source=ip_history.SOURCE,
            result="initial_observation", old_ip=None, new_ip=obs["ip"],
            old_observed_at_utc=None, new_observed_at_utc=obs["observed_at_utc"],
            confirmed_at_utc=None, recorded_at_utc=ip_history.utc_now(),
            imported_at_utc=None,
        )

    def test_csv_export_and_secret_redaction(self):
        self.assertEqual(ip_history.append_events(self.path, [self._sample_record()]), 1)
        csv_path = Path(self.temp.name) / "export.csv"
        self.assertEqual(ip_history.export_csv(self.path, csv_path), 1)
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["new_ip"], "198.51.100.1")
        for payload in (self.path.read_text(encoding="utf-8"), csv_path.read_text(encoding="utf-8-sig")):
            self.assertNotIn("secret-user", payload)
            self.assertNotIn("secret-password", payload)
            self.assertNotIn("socks5h://", payload)
        with self.assertRaises(ip_history.HistoryError):
            ip_history.export_csv(self.path, self.path)

    def test_import_rejects_secret_bearing_or_unknown_fields(self):
        record = self._sample_record()
        record["proxy_url"] = self.proxy
        source = Path(self.temp.name) / "unsafe.txt"
        source.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaises(ip_history.HistoryError):
            ip_history.import_history(self.path, source)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
