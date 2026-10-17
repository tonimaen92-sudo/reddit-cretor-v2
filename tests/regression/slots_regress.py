# UTF-8
"""Offline regressions for slot-ID allocation; no phone or project data writes."""

from __future__ import annotations

import os
import json
import sys
import tempfile
from friend_poster.core import slots  # noqa: E402


def check(name: str, condition: bool) -> None:
    print(("PASS " if condition else "FAIL ") + name, flush=True)
    if not condition:
        raise AssertionError(name)


def main() -> int:
    original_load_slots = slots.load_slots
    original_retired_indices = slots.retired_indices
    try:
        rows = [
            {"id": slots.index_to_name(n), "ghost": True}
            for n in range(1, 18)
        ]
        slots.load_slots = lambda: rows
        slots.retired_indices = lambda: set()
        check("ghost-only-a0017-reserves-id", slots.next_slot_id() == "a0018")

        rows.append(
            {
                "id": "a0018",
                "ghost": False,
                "email": None,
                "reddit": False,
                "day1": False,
            }
        )
        check("empty-a0018-placeholder-is-reusable", slots.next_slot_id() == "a0018")

        rows[-1]["email"] = "configured"
        check("configured-a0018-reserves-id", slots.next_slot_id() == "a0019")

        rows = [
            {
                "id": "a0001",
                "ghost": False,
                "ghost_container_state": "needed",
                "email": None,
                "reddit": False,
                "day1": False,
            }
        ]
        check("fresh-needed-a0001-is-allocatable", slots.next_slot_id() == "a0001")
        rows[0]["email"] = "configured"
        check("configured-needed-a0001-is-reserved", slots.next_slot_id() == "a0002")
        rows[0]["email"] = None
        rows[0].pop("ghost_container_state")
        check("legacy-empty-a0001-remains-reserved", slots.next_slot_id() == "a0002")
    finally:
        slots.load_slots = original_load_slots
        slots.retired_indices = original_retired_indices

    check(
        "legacy-ghost-container-state-remains-ready",
        slots.ghost_container_state({"id": "a0002", "ghost": True}) == "ready",
    )
    check(
        "explicit-a0001-needed-is-not-treated-ready",
        slots.ghost_container_state(
            {"id": "a0001", "ghost": True, "ghost_container_state": "needed"}
        )
        == "needed",
    )

    original_path = slots.SLOTS_PATH
    original_retired_path = slots.RETIRED_PATH
    original_log_dir = slots.LOG_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            slots.LOG_DIR = td
            slots.SLOTS_PATH = os.path.join(td, "slots.json")
            slots.RETIRED_PATH = os.path.join(td, "retired.json")
            with open(slots.RETIRED_PATH, "w", encoding="utf-8") as f:
                json.dump(["a0003", "A0003"], f)
            check("pc-delete-adds-retired-id", slots.retire_slot_id("r7"))
            check("retired-add-is-idempotent", not slots.retire_slot_id("a0007"))
            check(
                "existing-retired-ids-preserved-without-duplicates",
                slots.retired_ids() == {"a0003", "a0007"},
            )
            check(
                "retired-write-keeps-recoverable-backup",
                any(name.startswith("retired.json.bak-") for name in os.listdir(td)),
            )
            check("explicit-rebuild-releases-only-a0007", slots.unretire_slot_id("a0007"))
            check(
                "unretire-preserves-other-retired-ids",
                slots.retired_ids() == {"a0003"},
            )
            check(
                "unretire-is-idempotent",
                not slots.unretire_slot_id("a0007"),
            )
            # Restore the reservation for the allocator regression below.
            slots.retire_slot_id("a0007", backup=False)
            slots.load_slots = lambda: [
                {"id": slots.index_to_name(n), "ghost": True}
                for n in range(1, 7)
                if n != 3
            ]
            check("next-slot-never-returns-retired-id", slots.next_slot_id() == "a0008")
            slots.load_slots = original_load_slots
            slots.save_slots(
                [
                    {
                        "id": "a0001",
                        "ghost": True,
                        "email": "preserved@example.invalid",
                        "reddit": False,
                        "ghost_container_state": "needed",
                    }
                ]
            )
            slots.mark_ghost_container_provisioning("a0001", {"OLDUUID1"})
            try:
                slots.mark_ghost_container_provisioning("a0001", {"NEWUUID2"})
                replaced = True
            except ValueError:
                replaced = False
            saved = slots.load_slots()[0]
            check(
                "provisioning-baseline-is-immutable",
                not replaced
                and saved.get("ghost_container_baseline") == ["OLDUUID1"]
                and saved.get("email") == "preserved@example.invalid",
            )
            slots.mark_ghost_container_ready("a0001", "NEWUUID2")
            ready = slots.load_slots()[0]
            check(
                "ready-commits-only-reconciled-instance",
                ready.get("ghost_container_state") == "ready"
                and ready.get("ghost_container_instance") == "NEWUUID2"
                and "ghost_container_baseline" not in ready
                and "crane_selection_policy" not in ready,
            )

            slots.save_slots(
                [
                    {
                        "id": "a0008",
                        "ghost": False,
                        "email": "recovery@example.invalid",
                        "reddit": False,
                        "ghost_container_state": "provisioning",
                        "ghost_container_baseline": ["STALEUUID"],
                    }
                ]
            )
            recovered = slots.mark_ghost_container_ready_from_crane("a0008")
            check(
                "crane-proven-recovery-commits-without-inventing-instance",
                recovered.get("ghost") is True
                and recovered.get("ghost_container_state") == "ready"
                and recovered.get("crane_selection_policy") == "newest_exact"
                and "ghost_container_instance" not in recovered
                and "ghost_container_baseline" not in recovered
                and recovered.get("email") == "recovery@example.invalid",
            )

            ready.update(
                {
                    "email": "feed@example.invalid",
                    "username": "feed_user",
                    "password": "password123",
                }
            )
            slots.save_slots([ready])
            saved_birthday = slots.set_slot_birth_date("a0001", "29.02.2004")
            check(
                "slot-birthday-persists-canonically",
                saved_birthday.get("reddit_birth_date") == "29.02.2004"
                and slots.load_slots()[0].get("reddit_birth_date") == "29.02.2004",
            )
            rejected_birthday = False
            try:
                slots.set_slot_birth_date("a0001", "2004-02-29")
            except ValueError:
                rejected_birthday = True
            check(
                "invalid-birthday-does-not-replace-slot-value",
                rejected_birthday
                and slots.load_slots()[0].get("reddit_birth_date") == "29.02.2004",
            )
            slots.mark_reddit_created("a0001", feed_swipes=30)
            started = slots.load_slots()[0]
            check(
                "creation-swipe-progress-initialized",
                slots.creation_feed_swipe_progress(started) == (30, 0)
                and started.get("day1") is False
                and started.get("reddit_cleanup") == "pending",
            )
            slots.mark_creation_feed_swipe_completed("a0001", 12)
            advanced = slots.load_slots()[0]
            check(
                "creation-swipe-progress-advances",
                slots.creation_feed_swipe_progress(advanced) == (30, 12),
            )
            slots.mark_reddit_created("a0001", feed_swipes=40)
            unchanged = slots.load_slots()[0]
            check(
                "changed-panel-count-does-not-rewrite-started-target",
                slots.creation_feed_swipe_progress(unchanged) == (30, 12),
            )
            regressed = False
            try:
                slots.mark_creation_feed_swipe_completed("a0001", 11)
            except ValueError:
                regressed = True
            check("completed-count-is-monotonic", regressed)
            check(
                "legacy-created-slot-needs-zero-creation-swipes",
                slots.creation_feed_swipe_progress(
                    {"id": "a0099", "reddit": True, "reddit_cleanup": "pending"}
                )
                == (0, 0),
            )
            check(
                "creation-swipe-target-allows-over-250",
                slots.creation_feed_swipe_progress(
                    {
                        "creation_feed_swipes_target": 1000,
                        "creation_feed_swipes_completed": 999,
                    }
                )
                == (1000, 999),
            )
    finally:
        slots.SLOTS_PATH = original_path
        slots.RETIRED_PATH = original_retired_path
        slots.LOG_DIR = original_log_dir
        slots.load_slots = original_load_slots
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
