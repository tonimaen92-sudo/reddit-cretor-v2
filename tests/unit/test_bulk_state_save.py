"""Durable bulk-state save failures: sanitized detail, bounded retries, pause.

Every test runs against an isolated temp directory; the live
``logs/bulk_creation.json`` is never read or written.
"""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from friend_poster.core import bulk_creation


def valid_state(**overrides):
    state = bulk_creation.empty_state()
    state.update(**overrides)
    return state


class SaveStateFailureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "bulk_creation.json"

    def test_oserror_detail_is_sanitized_and_keeps_machine_codes(self):
        err = OSError(errno.EIO, "Input/output error")
        err.filename = str(self.path)  # a path must never leak into the message
        with (
            mock.patch.object(bulk_creation.os, "replace", side_effect=err),
            mock.patch.object(bulk_creation.time, "sleep", return_value=None),
        ):
            with self.assertRaises(bulk_creation.BulkSaveError) as caught:
                bulk_creation.save_state(valid_state(), self.path)
        message = str(caught.exception)
        self.assertIn("Bulk configuration could not be saved", message)
        self.assertIn("errno=5", message)
        self.assertIn("EIO", message)
        self.assertNotIn(str(self.path), message)
        # BulkSaveError stays a BulkError so existing routing keeps working.
        self.assertIsInstance(caught.exception, bulk_creation.BulkError)

    def test_transient_sharing_error_is_retried_then_succeeds(self):
        calls = {"n": 0}
        real_replace = os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise OSError(errno.EACCES, "Access is denied")
            return real_replace(src, dst)

        with (
            mock.patch.object(bulk_creation.os, "replace", side_effect=flaky),
            mock.patch.object(bulk_creation.time, "sleep", return_value=None),
        ):
            bulk_creation.save_state(valid_state(), self.path)
        self.assertEqual(calls["n"], 3)  # initial attempt + two retries
        self.assertTrue(self.path.exists())

    def test_winerror_sharing_violation_is_recognized_as_transient(self):
        err = OSError(errno.EACCES, "Sharing violation")
        err.winerror = 32
        calls = {"n": 0}
        real_replace = os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise err
            return real_replace(src, dst)

        with (
            mock.patch.object(bulk_creation.os, "replace", side_effect=flaky),
            mock.patch.object(bulk_creation.time, "sleep", return_value=None),
        ):
            bulk_creation.save_state(valid_state(), self.path)
        self.assertEqual(calls["n"], 2)
        self.assertTrue(self.path.exists())

    def test_transient_retries_are_bounded_and_the_file_is_unchanged(self):
        # stage="creating" additionally requires slot/verification fields.
        item = {"birth_date": "20.04.2005", "swipes": 10, "delay_seconds": 5}
        first = valid_state(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
            requested=1,
            remaining=1,
            queue=[dict(item)],
        )
        bulk_creation.save_state(first, self.path)
        before = self.path.read_text(encoding="utf-8")
        calls = {"n": 0}
        boom = OSError(errno.EACCES, "Access is denied")

        def always(src, dst):
            calls["n"] += 1
            raise boom

        changed = valid_state(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
            requested=1,
            remaining=1,
            queue=[{**item, "swipes": 20}],
        )
        with (
            mock.patch.object(bulk_creation.os, "replace", side_effect=always),
            mock.patch.object(bulk_creation.time, "sleep", return_value=None),
        ):
            with self.assertRaises(bulk_creation.BulkSaveError):
                bulk_creation.save_state(changed, self.path)
        self.assertEqual(calls["n"], 3)  # initial + at most two retries
        # The durable file still holds exactly the first version: the failed
        # save never half-wrote or replaced it.
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertIn('"swipes":10', before)

    def test_non_transient_error_is_not_retried(self):
        calls = {"n": 0}

        def fail(src, dst):
            calls["n"] += 1
            raise OSError(errno.EIO, "Input/output error")

        with (
            mock.patch.object(bulk_creation.os, "replace", side_effect=fail),
            mock.patch.object(bulk_creation.time, "sleep", return_value=None),
        ):
            with self.assertRaises(bulk_creation.BulkSaveError):
                bulk_creation.save_state(valid_state(), self.path)
        self.assertEqual(calls["n"], 1)

    def test_successful_save_leaves_no_temp_files_behind(self):
        bulk_creation.save_state(valid_state(), self.path)
        leftovers = [p for p in Path(self._tmp.name).iterdir() if p != self.path]
        self.assertEqual(leftovers, [])


class BulkBatchSaveFailureRoutingTests(unittest.TestCase):
    """An unresolved save failure pauses the batch before any workflow action."""

    def _app(self):
        from test_bulk_preserve_skip import make_app

        app = make_app(self, 1)
        app.bulk_state.update(
            stage="creating",
            slot_id="a0047",
            verified_ip="198.51.100.47",
            verified_at_utc="2026-10-16T12:00:00Z",
        )
        return app

    def test_bulk_save_failure_pauses_with_a_specific_code_and_no_actions(self):
        from friend_poster.apps import creator
        from test_bulk_creator import verified_result

        app = self._app()
        pauses = []
        app._pause_bulk = lambda phase, reason, **kwargs: pauses.append(
            (phase, reason, kwargs.get("code"))
        )

        def no_pipeline(*_args, **_kwargs):
            raise AssertionError("no workflow action may run after a save failure")

        ghost_slot = {
            "id": "a0047",
            "ghost": True,
            "ghost_container_state": "ready",
            "ghost_container_instance": "ghost-instance-proof",
        }

        with (
            mock.patch.object(creator, "_InactivityWatch", self._no_watch()),
            mock.patch.object(creator, "save_bulk_state", side_effect=bulk_creation.BulkSaveError(
                "Bulk configuration could not be saved (OSError | winerror=32 | Access is denied)."
            )),
            mock.patch.object(
                creator.App,
                "_connect",
                side_effect=AssertionError(
                    "a durable-save failure must not touch the live device"
                ),
            ),
            mock.patch.object(
                creator.App,
                "_discard_stalled_container",
                side_effect=AssertionError(
                    "a durable-save failure must not delete a container"
                ),
            ),
            mock.patch.object(creator, "create_one_hide_my_email", side_effect=no_pipeline),
            mock.patch.object(creator, "get_slot", lambda _sid: ghost_slot),
            mock.patch.object(creator, "load_slots", lambda: [ghost_slot]),
            mock.patch.object(
                creator, "ensure_ghost_container_needed", lambda _sid, _log: None
            ),
            mock.patch.object(
                creator,
                "observe_proxy_without_trigger",
                return_value=verified_result("198.51.100.47", "198.51.100.47"),
            ),
        ):
            creator.App._run_bulk_batch(app)

        self.assertEqual(len(pauses), 1)
        phase, reason, code = pauses[0]
        self.assertEqual(phase, "creation")
        self.assertEqual(code, "bulk_save_failed")
        self.assertIn("Bulk configuration could not be saved", reason)
        # The in-memory state was not advanced either (CAS/atomic guarantee).
        self.assertEqual(app.bulk_state["slot_id"], "a0047")

    @staticmethod
    def _no_watch():
        class NoWatch:
            def __init__(self, *_a, **_k):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        return NoWatch


if __name__ == "__main__":
    unittest.main()
