"""Offline tests for exact recorded Ghost-container selection."""

from __future__ import annotations

import unittest
from unittest import mock

from friend_poster.device import ghost_live


class FakeStack:
    def __init__(self) -> None:
        self.cancel_checks = 0
        self.waits: list[float] = []

    def check_cancelled(self) -> None:
        self.cancel_checks += 1

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)


class FakeNode:
    x = 180
    y = 420
    w = 220
    h = 44

    def center(self):
        return self.x + self.w // 2, self.y + self.h // 2


class RecordedContainerSelectionTests(unittest.TestCase):
    def _successful_selection(self, recorded: str, inventory_ids: set[str]):
        events: list[object] = []
        stack = FakeStack()
        dump_frames = iter(
            [
                ("", ["containers-list"]),
                ("", ["verified-reddit-settings"]),
            ]
        )

        def close(*_args, **_kwargs):
            events.append("close")
            return True

        def scan(*_args, **_kwargs):
            events.append("scan-all")
            return {"a0047": set(inventory_ids)}, ["stable-bottom"]

        def reacquire(_stack, _phone, _lg, nodes, name, instance_id):
            events.append(("reacquire", name, instance_id, nodes))
            return ["exact-row-frame"]

        def fresh(_stack, _phone, _lg, nodes, name, instance_id):
            events.append(("fresh-row", name, instance_id, nodes))
            return nodes, FakeNode()

        def tap(_stack, _node):
            events.append("tap-exact-row")

        def proven(nodes, name):
            events.append(("prove-active", name, nodes))
            return True

        with (
            mock.patch.object(ghost_live, "close_ghost_organic", side_effect=close),
            mock.patch.object(
                ghost_live,
                "open_reddit_containers",
                side_effect=lambda *_a, **_k: events.append("open-containers"),
            ),
            mock.patch.object(ghost_live, "dump_named", side_effect=dump_frames),
            mock.patch.object(ghost_live, "stop_if_reset", return_value=None),
            mock.patch.object(
                ghost_live, "scan_all_container_instances", side_effect=scan
            ),
            mock.patch.object(
                ghost_live,
                "_reacquire_recorded_instance_frame",
                side_effect=reacquire,
            ),
            mock.patch.object(
                ghost_live, "_fresh_safe_instance_node", side_effect=fresh
            ),
            mock.patch.object(ghost_live, "tap_container_name", side_effect=tap),
            mock.patch.object(
                ghost_live, "_proven_reddit_settings", side_effect=proven
            ),
        ):
            matched = ghost_live.select_recorded_container(
                stack, object(), lambda _message: None, "a0047", recorded
            )
        return matched, events, stack

    def test_exact_recorded_identity_is_selected_and_verified(self):
        matched, events, stack = self._successful_selection(
            "abc12345", {"ABC12345", "OTHER999"}
        )

        self.assertEqual(matched, "ABC12345")
        self.assertIn(
            ("reacquire", "a0047", "ABC12345", ["stable-bottom"]), events
        )
        self.assertIn(
            ("fresh-row", "a0047", "ABC12345", ["exact-row-frame"]), events
        )
        self.assertEqual(events.count("tap-exact-row"), 1)
        self.assertEqual(events.count("close"), 2)
        proof_index = events.index(
            ("prove-active", "a0047", ["verified-reddit-settings"])
        )
        self.assertLess(proof_index, events.index("close", 1))
        self.assertEqual(stack.waits, [0.9])

    def test_unique_recorded_prefix_resolves_to_full_instance(self):
        matched, events, _stack = self._successful_selection(
            "abc1234", {"ABC123456", "OTHER999"}
        )

        self.assertEqual(matched, "ABC123456")
        self.assertIn(
            ("reacquire", "a0047", "ABC123456", ["stable-bottom"]), events
        )
        self.assertIn(
            ("fresh-row", "a0047", "ABC123456", ["exact-row-frame"]), events
        )

    def _assert_refused_without_selection(
        self, recorded: str, inventory_ids: set[str], message: str
    ) -> None:
        closes: list[bool] = []
        select = mock.Mock()
        reacquire = mock.Mock()
        with (
            mock.patch.object(
                ghost_live,
                "close_ghost_organic",
                side_effect=lambda *_a, **_k: closes.append(True) or True,
            ),
            mock.patch.object(ghost_live, "open_reddit_containers", return_value=None),
            mock.patch.object(
                ghost_live,
                "dump_named",
                return_value=("", ["containers-list"]),
            ),
            mock.patch.object(ghost_live, "stop_if_reset", return_value=None),
            mock.patch.object(
                ghost_live,
                "scan_all_container_instances",
                return_value=({"a0047": set(inventory_ids)}, ["stable-bottom"]),
            ),
            mock.patch.object(
                ghost_live, "_reacquire_recorded_instance_frame", reacquire
            ),
            mock.patch.object(ghost_live, "_select_named_node", select),
        ):
            with self.assertRaisesRegex(ghost_live.PhoneError, message):
                ghost_live.select_recorded_container(
                    FakeStack(),
                    object(),
                    lambda _message: None,
                    "a0047",
                    recorded,
                )

        reacquire.assert_not_called()
        select.assert_not_called()
        self.assertEqual(closes, [True, True])

    def test_absent_recorded_identity_is_refused_and_ghost_is_closed(self):
        self._assert_refused_without_selection(
            "ABC1234", {"OTHER999"}, "absent from the complete validated"
        )

    def test_ambiguous_recorded_prefix_is_refused_and_ghost_is_closed(self):
        self._assert_refused_without_selection(
            "ABC1234",
            {"ABC12345", "ABC12346"},
            "matches multiple containers",
        )

    def test_failed_active_container_proof_is_not_success_and_closes_ghost(self):
        closes: list[bool] = []
        stack = FakeStack()
        dump_frames = iter(
            [
                ("", ["containers-list"]),
                ("", ["wrong-settings-page"]),
            ]
        )
        with (
            mock.patch.object(
                ghost_live,
                "close_ghost_organic",
                side_effect=lambda *_a, **_k: closes.append(True) or True,
            ),
            mock.patch.object(ghost_live, "open_reddit_containers", return_value=None),
            mock.patch.object(ghost_live, "dump_named", side_effect=dump_frames),
            mock.patch.object(ghost_live, "stop_if_reset", return_value=None),
            mock.patch.object(
                ghost_live,
                "scan_all_container_instances",
                return_value=({"a0047": {"ABC12345"}}, ["stable-bottom"]),
            ),
            mock.patch.object(
                ghost_live,
                "_reacquire_recorded_instance_frame",
                return_value=["exact-row-frame"],
            ),
            mock.patch.object(
                ghost_live,
                "_fresh_safe_instance_node",
                return_value=(["exact-row-frame"], FakeNode()),
            ),
            mock.patch.object(ghost_live, "tap_container_name", return_value=None),
            mock.patch.object(
                ghost_live, "_proven_reddit_settings", return_value=False
            ),
        ):
            with self.assertRaisesRegex(
                ghost_live.PhoneError, "does not prove exact active container"
            ):
                ghost_live.select_recorded_container(
                    stack,
                    object(),
                    lambda _message: None,
                    "a0047",
                    "ABC12345",
                )

        self.assertEqual(closes, [True, True])
        self.assertEqual(stack.waits, [0.9])

    def test_reacquisition_walks_from_bottom_to_exact_uuid_frame(self):
        stack = FakeStack()
        flicks: list[tuple[bool, bool]] = []

        def instance_node(nodes, name, instance_id):
            self.assertEqual(name, "a0047")
            self.assertEqual(instance_id, "ABC12345")
            return FakeNode() if nodes == ["target-frame"] else None

        with (
            mock.patch.object(ghost_live, "_instance_node", side_effect=instance_node),
            mock.patch.object(
                ghost_live,
                "_container_scan_signature",
                side_effect=lambda nodes: tuple(nodes),
            ),
            mock.patch.object(
                ghost_live,
                "list_flick",
                side_effect=lambda _stack, toward_bottom, big=False: flicks.append(
                    (toward_bottom, big)
                ),
            ),
            mock.patch.object(
                ghost_live, "dump_named", return_value=("", ["target-frame"])
            ),
            mock.patch.object(ghost_live, "on_containers_list", return_value=True),
        ):
            nodes = ghost_live._reacquire_recorded_instance_frame(
                stack,
                object(),
                lambda _message: None,
                ["stable-bottom"],
                "a0047",
                "ABC12345",
            )

        self.assertEqual(nodes, ["target-frame"])
        self.assertEqual(flicks, [(False, True)])
        self.assertEqual(stack.cancel_checks, 1)


if __name__ == "__main__":
    unittest.main()
