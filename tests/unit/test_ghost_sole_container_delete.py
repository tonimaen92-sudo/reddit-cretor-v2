"""Offline tests for fail-closed sole-container deletion through Ghost."""

from __future__ import annotations

import unittest
from unittest import mock

from friend_poster.device import ghost_live
from friend_poster.device.ui_tree import Node


class FakeStack:
    def __init__(self) -> None:
        self.cancel_checks = 0

    def check_cancelled(self) -> None:
        self.cancel_checks += 1


class ResolveSoleUserContainerTests(unittest.TestCase):
    def _resolve(self, inventory, *, expected_name=None):
        events: list[str] = []
        self.events = events
        self.delete = mock.Mock()

        def close(*_args, **_kwargs):
            events.append("close")
            return True

        def open_containers(*_args, **_kwargs):
            events.append("open")

        def scan(*_args, **_kwargs):
            events.append("scan")
            return inventory, ["stable-bottom"]

        with (
            mock.patch.object(ghost_live, "close_ghost_organic", side_effect=close),
            mock.patch.object(
                ghost_live, "open_reddit_containers", side_effect=open_containers
            ),
            mock.patch.object(
                ghost_live,
                "dump_named",
                return_value=("", ["fresh-containers-list"]),
            ),
            mock.patch.object(ghost_live, "stop_if_reset", return_value=None),
            mock.patch.object(
                ghost_live, "scan_all_user_container_instances", side_effect=scan
            ),
            mock.patch.object(
                ghost_live, "_delete_row_in_edit", self.delete
            ),
        ):
            return ghost_live.resolve_sole_user_container(
                FakeStack(),
                object(),
                lambda _message: None,
                expected_name=expected_name,
            )

    def _assert_closed_without_delete(self) -> None:
        self.assertEqual(self.events, ["close", "open", "scan", "close"])
        self.delete.assert_not_called()

    def test_unique_expected_container_returns_exact_pair_without_delete(self):
        identity = self._resolve(
            {"a0225": {"ABC12345"}}, expected_name="A0225"
        )

        self.assertEqual(identity, ("a0225", "ABC12345"))
        self._assert_closed_without_delete()

    def test_zero_user_containers_fails_and_closes_ghost(self):
        with self.assertRaisesRegex(ghost_live.PhoneError, "no user container"):
            self._resolve({}, expected_name="a0225")

        self._assert_closed_without_delete()

    def test_ambiguous_inventory_fails_and_closes_ghost(self):
        with self.assertRaisesRegex(ghost_live.PhoneError, "2 user-container"):
            self._resolve(
                {
                    "a0224": {"AAA11111"},
                    "a0225": {"BBB22222"},
                },
                expected_name="a0225",
            )

        self._assert_closed_without_delete()

    def test_expected_name_mismatch_fails_and_closes_ghost(self):
        with self.assertRaisesRegex(
            ghost_live.PhoneError, "a0224, not expected a0225"
        ):
            self._resolve(
                {"a0224": {"AAA11111"}}, expected_name="a0225"
            )

        self._assert_closed_without_delete()

    def test_additional_arbitrary_container_counts_and_fails_closed(self):
        with self.assertRaisesRegex(ghost_live.PhoneError, "2 user-container"):
            self._resolve(
                {
                    "a0225": {"ABC12345"},
                    "Personal": {"DEF67890"},
                },
                expected_name="a0225",
            )

        self._assert_closed_without_delete()

    def test_sole_arbitrary_container_is_never_bound_to_expected_slot(self):
        with self.assertRaisesRegex(ghost_live.PhoneError, "unrecognized name"):
            self._resolve(
                {"Personal": {"DEF67890"}}, expected_name="a0225"
            )

        self._assert_closed_without_delete()

    def test_bare_numeric_alias_is_not_exact_expected_name(self):
        with self.assertRaisesRegex(ghost_live.PhoneError, "unrecognized name"):
            self._resolve({"0225": {"DEF67890"}}, expected_name="a0225")

        self._assert_closed_without_delete()


class NoUuidFullInventoryTests(unittest.TestCase):
    @staticmethod
    def _node(text, x, y, w=160, h=24):
        return Node(ntype="ocr", text=text, x=x, y=y, w=w, h=h)

    def _frame(self, rows):
        nodes = [
            self._node("Containers", 175, 87, 287, 38),
            self._node("Default", 214, 220, 128, 25),
            self._node("DEFAULT", 204, 252, 102, 18),
        ]
        y = 360
        for name, instance_id in rows:
            nodes.append(self._node(name, 207, y, 180, 25))
            if instance_id is not None:
                nodes.append(
                    self._node(
                        f"{instance_id}-1111-4222-8333-444455556666",
                        390,
                        y + 34,
                        470,
                        20,
                    )
                )
            y += 132
        nodes.append(self._node("New Container", 375, 1254, 180, 18))
        return nodes

    def _scan(self, rows):
        frame = self._frame(rows)
        with (
            mock.patch.object(ghost_live, "list_flick", return_value=None),
            mock.patch.object(ghost_live, "dump_named", return_value=("", frame)),
        ):
            return ghost_live.scan_all_user_container_instances(
                FakeStack(), object(), lambda _message: None, frame
            )[0]

    def test_unique_expected_is_the_only_non_default_inventory_entry(self):
        self.assertEqual(
            self._scan([("a0227", "ABC12345")]),
            {"a0227": {"ABC12345"}},
        )

    def test_absent_inventory_excludes_default(self):
        self.assertEqual(self._scan([]), {})

    def test_two_known_containers_remain_ambiguous(self):
        self.assertEqual(
            self._scan(
                [("a0226", "AAA11111"), ("a0227", "BBB22222")]
            ),
            {"a0226": {"AAA11111"}, "a0227": {"BBB22222"}},
        )

    def test_arbitrary_container_is_included_not_dropped(self):
        self.assertEqual(
            self._scan(
                [("a0227", "ABC12345"), ("Personal", "DEF67890")]
            ),
            {"a0227": {"ABC12345"}, "personal": {"DEF67890"}},
        )

    def test_unpaired_arbitrary_row_fails_closed(self):
        with self.assertRaisesRegex(
            ghost_live.PhoneError, "unrecognized or malformed user container row"
        ):
            self._scan([("Personal", None)])


class SoleContainerDeletionTests(unittest.TestCase):
    def _run(self, inventories):
        events: list[object] = []
        self.events = events
        frames = iter(
            (inventory, [f"bottom-{index}"])
            for index, inventory in enumerate(inventories)
        )

        def close(*_args, **_kwargs):
            events.append("close")
            return True

        def open_containers(*_args, **_kwargs):
            events.append("open")

        def scan(*_args, **_kwargs):
            events.append("scan")
            return next(frames)

        def delete(_stack, _phone, _lg, nodes, name, instance_id):
            events.append(("delete", nodes, name, instance_id))
            return ["after-delete"]

        with (
            mock.patch.object(ghost_live, "close_ghost_organic", side_effect=close),
            mock.patch.object(
                ghost_live, "open_reddit_containers", side_effect=open_containers
            ),
            mock.patch.object(
                ghost_live,
                "dump_named",
                return_value=("", ["fresh-containers-list"]),
            ),
            mock.patch.object(ghost_live, "stop_if_reset", return_value=None),
            mock.patch.object(
                ghost_live, "scan_all_user_container_instances", side_effect=scan
            ),
            mock.patch.object(
                ghost_live, "_delete_row_in_edit", side_effect=delete
            ),
        ):
            outcome = ghost_live.delete_sole_user_container(
                FakeStack(), object(), lambda _message: None
            )
        return outcome, events

    def test_zero_user_instances_is_the_only_absent_result(self):
        outcome, events = self._run([{}])

        self.assertEqual(outcome, "absent")
        self.assertEqual(events, ["close", "open", "scan", "close"])

    def test_exactly_one_full_identity_is_deleted_and_rescanned(self):
        outcome, events = self._run([{"a0225": {"ABC12345"}}, {}])

        self.assertEqual(outcome, "deleted")
        self.assertEqual(
            events,
            [
                "close",
                "open",
                "scan",
                ("delete", ["bottom-0"], "a0225", "ABC12345"),
                "close",
                "open",
                "scan",
                "close",
            ],
        )

    def test_two_different_names_are_refused_without_delete(self):
        with self.assertRaisesRegex(ghost_live.PhoneError, "2 user-container"):
            self._run(
                [
                    {
                        "a0224": {"AAA11111"},
                        "a0225": {"BBB22222"},
                    }
                ]
            )
        self.assertFalse(
            any(
                event[0] == "delete"
                for event in self.events
                if isinstance(event, tuple)
            )
        )

    def test_two_uuids_for_one_name_are_refused_without_delete(self):
        with self.assertRaisesRegex(ghost_live.PhoneError, "2 user-container"):
            self._run([{"a0225": {"ABC12345", "ABC12346"}}])
        self.assertFalse(
            any(
                event[0] == "delete"
                for event in self.events
                if isinstance(event, tuple)
            )
        )

    def test_partial_or_malformed_identity_is_refused_without_delete(self):
        with self.assertRaisesRegex(ghost_live.PhoneError, "no full UUID identity"):
            self._run([{"a0225": {"ABC12"}}])
        self.assertFalse(
            any(
                event[0] == "delete"
                for event in self.events
                if isinstance(event, tuple)
            )
        )

    def test_same_exact_identity_remaining_is_not_verified(self):
        with self.assertRaisesRegex(ghost_live.PhoneError, "still present"):
            self._run(
                [
                    {"a0225": {"ABC12345"}},
                    {"a0225": {"ABC12345"}},
                ]
            )

    def test_unexpected_replacement_container_is_not_verified(self):
        with self.assertRaisesRegex(ghost_live.PhoneError, "unexpected user container"):
            self._run(
                [
                    {"a0225": {"ABC12345"}},
                    {"a0226": {"DEF67890"}},
                ]
            )


if __name__ == "__main__":
    unittest.main()
