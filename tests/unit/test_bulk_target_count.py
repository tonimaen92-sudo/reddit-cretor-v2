# UTF-8
"""Offline tests for changing an active batch's total item target."""

from __future__ import annotations

import threading
import unittest

from friend_poster.apps import creator
from friend_poster.core.bulk_creation import empty_state


def row(index: int) -> dict:
    return {
        "birth_date": f"{(index % 27) + 1:02d}.01.2004",
        "swipes": 10 + index,
        "delay_seconds": 2.0 + (index % 5),
        "username": f"TargetUser{index}",
    }


class Value:
    def __init__(self, value=""):
        self.value = str(value)

    def get(self):
        return self.value

    def set(self, value):
        self.value = str(value)


class Text:
    def __init__(self, value=""):
        self.value = str(value)

    def get(self, *_args):
        return self.value

    def delete(self, *_args):
        self.value = ""

    def insert(self, _index, value):
        self.value = str(value)

    def edit_modified(self, *_args):
        return None


class TargetCountTests(unittest.TestCase):
    def app(
        self, *, target="150", total_rows=196, consumed=1, original_count=50,
    ):
        app = creator.App.__new__(creator.App)
        original = [row(i) for i in range(original_count)]
        current = original[consumed:]
        state = empty_state()
        state.update(
            queue=[dict(item) for item in current],
            requested=original_count,
            remaining=len(current),
            stage="creating",
            slot_id="a0104",
            baseline_ip="198.51.100.1",
            verified_ip="198.51.100.2",
            verified_at_utc="2026-10-19T00:00:00Z",
            delete_containers=True,
        )
        all_rows = [*original, *(row(i) for i in range(original_count, total_rows))]
        app.bulk_state = state
        app.bulk_editor_baseline = [dict(item) for item in original]
        app.bulk_mutex = threading.RLock()
        app.bulk_draft_suspended = False
        app.bulk_count_var = Value(target)
        app.bulk_births_text = Text("\n".join(item["birth_date"] for item in all_rows))
        app.bulk_swipes_text = Text("\n".join(str(item["swipes"]) for item in all_rows))
        app.bulk_delays_text = Text("\n".join(str(item["delay_seconds"]) for item in all_rows))
        app.bulk_usernames_text = Text("\n".join(item["username"] for item in all_rows))
        app.bulk_draft_status_var = Value()
        app.status = Value()
        app.logs = []
        app.log = app.logs.append
        app._bulk_owner_gated = lambda: False
        app._save_bulk_draft_now = lambda: True
        app._refresh_bulk_panel = lambda: None
        app._persist_bulk = lambda updated: setattr(app, "bulk_state", dict(updated))
        return app

    def test_50_to_150_adds_exactly_100_without_changing_active_head(self):
        app = self.app()
        active_head = dict(app.bulk_state["queue"][0])

        self.assertTrue(app.save_bulk_item_count())

        self.assertEqual(app.bulk_state["requested"], 150)
        self.assertEqual(app.bulk_state["remaining"], 149)
        self.assertEqual(len(app.bulk_state["queue"]), 149)
        self.assertEqual(app.bulk_state["queue"][0], active_head)
        self.assertEqual(app.bulk_state["stage"], "creating")
        self.assertEqual(app.bulk_state["slot_id"], "a0104")
        self.assertEqual(app.bulk_count_var.get(), "150")
        self.assertIn("100 item(s) added", app.bulk_draft_status_var.get())

    def test_repeated_save_does_not_duplicate_items(self):
        app = self.app()
        self.assertTrue(app.save_bulk_item_count())
        first_queue = [dict(item) for item in app.bulk_state["queue"]]

        self.assertTrue(app.save_bulk_item_count())

        self.assertEqual(app.bulk_state["queue"], first_queue)
        self.assertEqual(app.bulk_state["requested"], 150)
        self.assertIn("no items were duplicated", app.bulk_draft_status_var.get())

    def test_current_two_item_batch_can_be_raised_to_150(self):
        app = self.app(original_count=2, consumed=0)

        self.assertTrue(app.save_bulk_item_count())

        self.assertEqual(app.bulk_state["requested"], 150)
        self.assertEqual(app.bulk_state["remaining"], 150)
        self.assertEqual(len(app.bulk_state["queue"]), 150)
        self.assertIn("148 item(s) added", app.bulk_draft_status_var.get())

    def test_reducing_active_total_is_rejected_without_state_change(self):
        app = self.app(target="49")
        before = dict(app.bulk_state)
        before["queue"] = [dict(item) for item in app.bulk_state["queue"]]

        self.assertFalse(app.save_bulk_item_count())

        self.assertEqual(app.bulk_state, before)
        self.assertIn("cannot be reduced below 50", app.bulk_draft_status_var.get())

    def test_insufficient_rows_is_rejected_without_state_change(self):
        app = self.app(target="150", total_rows=120)
        before_queue = [dict(item) for item in app.bulk_state["queue"]]

        self.assertFalse(app.save_bulk_item_count())

        self.assertEqual(app.bulk_state["requested"], 50)
        self.assertEqual(app.bulk_state["queue"], before_queue)
        self.assertIn("only 70 are available", app.bulk_draft_status_var.get())

    def test_save_target_skips_claimed_addition_and_uses_next_unused_name(self):
        app = self.app(target="4", total_rows=6, consumed=0, original_count=2)
        app._username_claim_owners = lambda: {
            "targetuser2": {"slot_id": "a0009", "status": "success"},
        }

        self.assertTrue(app.save_bulk_item_count())

        self.assertEqual(app.bulk_state["requested"], 4)
        self.assertEqual(
            [item.get("username") for item in app.bulk_state["queue"]],
            ["TargetUser0", "TargetUser1", "TargetUser3", "TargetUser4"],
        )
        self.assertIn("Ignored 1 username row", app.bulk_draft_status_var.get())

    def test_active_queue_shifts_past_username_owned_by_successful_slot(self):
        app = self.app(target="3", total_rows=3, consumed=0, original_count=3)
        app._username_claim_owners = lambda: {
            "targetuser0": {"slot_id": "a0103", "status": "success"},
        }

        updated = app._normalize_bulk_queue_usernames(app.bulk_state, "a0105")

        self.assertEqual(
            [item.get("username") for item in updated["queue"]],
            ["TargetUser1", "TargetUser2", None],
        )
        self.assertEqual(updated["requested"], 3)
        self.assertEqual(updated["remaining"], 3)

    def test_fresh_start_skips_whole_claimed_row_and_pulls_surplus_forward(self):
        app = self.app(target="2", total_rows=5, consumed=0, original_count=2)
        app.bulk_writer_id = "offline"
        app._username_claim_owners = lambda: {
            "targetuser0": {"slot_id": "a0103", "status": "success"},
        }

        requested, items, skipped = app._parse_fresh_bulk_editor()

        self.assertEqual((requested, skipped), (2, 1))
        self.assertEqual(
            [item.get("username") for item in items],
            ["TargetUser1", "TargetUser2"],
        )
        self.assertEqual(items[0]["birth_date"], row(1)["birth_date"])


if __name__ == "__main__":
    unittest.main()
