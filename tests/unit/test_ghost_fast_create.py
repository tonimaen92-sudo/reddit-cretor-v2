# UTF-8
"""Offline tests for boundary-proven Ghost creation and reconciliation.

A new slot may reach Create only after a fresh, complete Ghost inventory proves
that its exact name is absent. Post-create UUID capture remains bounded when
possible and falls back to complete exact-name reconciliation when needed.

Everything here is synthetic: no phone, no live slots file, no account.
"""

from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from friend_poster.device import ghost_live
from friend_poster.device.ui_tree import Node

NEW_SHORT = "A000270F"
OLD_SHORT = "A0000049"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fixture_nodes(name: str) -> list[Node]:
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return [Node(ntype="ocr", **item) for item in raw]


def uuid_for(index: int) -> str:
    return f"{0xA0000000 + index:08X}-1111-2222-3333"


class FakeGhostDevice:
    """A scrollable 70+ row Containers list with a fixed New Container footer."""

    def __init__(
        self, count: int = 70, span: int = 6, step: int = 5,
        *, reveal_created: bool = False, after_create_nodes=None,
    ) -> None:
        self.span = span
        self.step = step
        self.rows = [("Default", None)] + [
            (f"a{i:04d}", uuid_for(i)) for i in range(1, count + 1)
        ]
        self.created_uuid = uuid_for(9999)
        self.start = 0
        self.dialog_open = False
        self.pending_name = None
        self.hide_new_uuid = False
        self.unstable = False
        self.unstable_counter = 0
        self.reveal_created = reveal_created
        self.after_create_nodes = after_create_nodes
        self.down_flicks = 0
        self.up_flicks = 0
        self.dumps: list[str] = []
        self.creates: list[str] = []
        self.events: list[str] = []
        self.maxstart = max(0, len(self.rows) - self.span)

    # --- frames -------------------------------------------------------------
    def window(self):
        return self.rows[self.start : self.start + self.span]

    def frame(self):
        nodes = [
            Node(ntype="ocr", text="Containers", x=175, y=87, w=287, h=38),
            Node(ntype="ocr", text="New Container", x=375, y=1253, w=180, h=18),
        ]
        y = 231
        for name, uid in self.window():
            nodes.append(Node(ntype="ocr", text=name, x=207, y=y, w=115, h=25))
            if uid and not (self.hide_new_uuid and name == self.pending_name):
                nodes.append(
                    Node(ntype="ocr", text=uid, x=390, y=y + 35, w=460, h=20)
                )
            y += 131
        if self.unstable:
            self.unstable_counter += 1
            nodes.append(
                Node(
                    ntype="ocr",
                    text=f"{0xB0000000 + self.unstable_counter:08X}-1111-2222-3333",
                    x=390,
                    y=1150,
                    w=460,
                    h=20,
                )
            )
        return nodes

    def dialog_frame(self, typed=None):
        nodes = [
            Node(ntype="ocr", text="Create", x=580, y=812, w=120, h=40),
            Node(ntype="ocr", text="Enter the name", x=120, y=520, w=300, h=30),
        ]
        if typed:
            nodes.append(Node(ntype="ocr", text=typed, x=120, y=620, w=200, h=30))
        return nodes

    def dump(self, _stack, _phone, stem, _lg, **_kwargs):
        self.dumps.append(stem)
        if stem == "625_name":
            return ("", self.dialog_frame())
        if stem.startswith("626_named"):
            return ("", self.dialog_frame(self.pending_name))
        if stem == "627_after_create" and self.after_create_nodes is not None:
            return ("", list(self.after_create_nodes))
        return ("", self.frame())

    def flick(self, _stack, toward_bottom, big=False):
        self.events.append("flick")
        if toward_bottom:
            self.down_flicks += 1
            self.start = min(self.maxstart, self.start + self.step)
        else:
            self.up_flicks += 1
            self.start = max(0, self.start - self.step)

    def tap_xy(self, _x, y):
        if y >= 1100:
            self.events.append("add")
            self.dialog_open = True
        elif self.dialog_open and self.pending_name:
            self.events.append("create")
            self.rows.append((self.pending_name, self.created_uuid))
            self.creates.append(self.pending_name)
            self.dialog_open = False
            self.maxstart = max(0, len(self.rows) - self.span)
            if self.reveal_created:
                self.start = self.maxstart

    def add_row(self, name: str, uid: str) -> None:
        self.rows.append((name, uid))
        self.maxstart = max(0, len(self.rows) - self.span)


class FakeStack:
    def __init__(self, device: FakeGhostDevice, stopped: bool = False) -> None:
        self.device = device
        self.stopped = stopped
        self.cleared_bundles: list[str] = []

    def check_cancelled(self):
        if self.stopped:
            raise ghost_live.PhoneError("Stopped by user.")

    def tap_xy(self, x, y):
        self.device.tap_xy(x, y)

    def tap_row(self, _node):
        pass

    def type_text_block(self, text):
        self.device.pending_name = text

    def press_backspace(self, *_a, **_k):
        pass

    def swipe(self, *_a, **_k):
        pass

    def long_tap(self, *_a, **_k):
        pass

    def wait(self, *_a, **_k):
        pass

    def interp_flick(self, *_a, **_k):
        pass

    def clear_app_memory(self, bundle_id: str) -> str:
        self.cleared_bundles.append(bundle_id)
        return "force-close"


class SlotHarness:
    """Durable checkpoint double that tracks state transitions."""

    def __init__(self, name, state="needed", baseline=None, instance=None) -> None:
        self.name = name
        self.state = state
        self.baseline = list(baseline or [])
        self.instance = instance
        self.provisioning: list[list] = []
        self.ready: list[str] = []

    def row(self):
        return {
            "id": self.name,
            "ghost": True,
            "ghost_container_state": self.state,
            "ghost_container_baseline": self.baseline,
            "ghost_container_instance": self.instance,
        }

    def ensure(self, _name, _log=None):
        return self.row()

    def state_of(self, row):
        return (row or {}).get("ghost_container_state")

    def mark_provisioning(self, _name, baseline, _log=None):
        self.state = "provisioning"
        self.baseline = list(baseline)
        self.provisioning.append(list(baseline))

    def mark_ready(self, _name, instance_id, _log=None):
        self.state = "ready"
        self.instance = instance_id
        self.ready.append(instance_id)


@contextmanager
def fast_env(device, harness, calls, *, stack, all_result=None,
             named_result=(set(), []), provision_result="AABBCCDD",
             configure_result="AABBCCDD"):
    if all_result is None:
        all_result = ({}, [])
    def record_all(_stack, _phone, _lg, nodes, stem="624_allocation_scan"):
        calls["full_all"].append(stem)
        calls["flow"].append("complete_inventory")
        if isinstance(all_result, Exception):
            raise all_result
        return all_result

    def record_named(_stack, _phone, _lg, nodes, name, stem="624_instance_scan"):
        calls["full_named"].append(stem)
        if isinstance(named_result, Exception):
            raise named_result
        return named_result

    def record_open(*_args, **_kwargs):
        calls["open"].append(1)

    def record_select(_stack, _phone, _lg, _nodes, _name, instance_id):
        calls["select"].append(instance_id)

    def record_provision(*_args, **_kwargs):
        calls["flow"].append("provision")
        calls["provision"].append(1)
        return provision_result

    with (
        mock.patch.object(ghost_live, "dump_named", device.dump),
        mock.patch.object(
            ghost_live, "list_flick",
            lambda s, toward_bottom, big=False: device.flick(s, toward_bottom, big),
        ),
        mock.patch.object(ghost_live.time, "sleep", lambda *_a, **_k: None),
        mock.patch.object(ghost_live, "close_ghost_organic", lambda *_a, **_k: None),
        mock.patch.object(ghost_live, "open_reddit_containers", record_open),
        mock.patch.object(
            ghost_live, "formulate_named",
            lambda *_a, **_k: calls["formulate"].append(1),
        ),
        mock.patch.object(
            ghost_live, "advanced_named",
            lambda *_a, **_k: calls["advanced"].append(1),
        ),
        mock.patch.object(ghost_live, "_select_named_node", record_select),
        mock.patch.object(ghost_live, "scan_all_container_instances", record_all),
        mock.patch.object(ghost_live, "scan_named_container_instances", record_named),
        mock.patch.object(
            ghost_live, "provision_slot_container",
            record_provision,
        ),
        mock.patch.object(
            ghost_live, "configure_existing_slot_container",
            lambda *_a, **_k: calls["configure"].append(1) or configure_result,
        ),
        mock.patch.object(ghost_live, "ensure_ghost_container_needed", harness.ensure),
        mock.patch.object(ghost_live, "ghost_container_state", harness.state_of),
        mock.patch.object(
            ghost_live, "mark_ghost_container_provisioning", harness.mark_provisioning
        ),
        mock.patch.object(ghost_live, "mark_ghost_container_ready", harness.mark_ready),
        mock.patch.object(ghost_live, "get_slot", lambda _n: None),
        mock.patch.object(ghost_live, "unretire_slot_id", lambda _n: False),
        mock.patch.object(ghost_live, "stop_if_reset", lambda _nodes: None),
    ):
        yield


@contextmanager
def device_env(device):
    with (
        mock.patch.object(ghost_live, "dump_named", device.dump),
        mock.patch.object(
            ghost_live, "list_flick",
            lambda s, toward_bottom, big=False: device.flick(s, toward_bottom, big),
        ),
        mock.patch.object(ghost_live.time, "sleep", lambda *_a, **_k: None),
    ):
        yield


def new_calls():
    return {
        "open": [],
        "full_all": [],
        "full_named": [],
        "formulate": [],
        "advanced": [],
        "select": [],
        "provision": [],
        "configure": [],
        "flow": [],
    }


class FastCreateTests(unittest.TestCase):
    def _apply(self, device, harness, calls, logs, *, stack=None, names=None,
               **env_kwargs):
        stack = stack or FakeStack(device)
        with fast_env(device, harness, calls, stack=stack, **env_kwargs):
            return ghost_live.apply_reddit_containers(
                object(), stack, logs.append, count=1, stop=None, close=False,
                names=names if names is not None else [harness.name],
            )

    def test_new_slot_requires_complete_inventory_before_provision(self):
        """A clean slot never bypasses the fresh complete inventory."""
        name = "a0073"
        device = FakeGhostDevice(count=70, reveal_created=True)
        harness = SlotHarness(name)
        calls, logs = new_calls(), []

        stack = FakeStack(device)
        created = self._apply(device, harness, calls, logs, stack=stack)

        self.assertEqual(created, [name])
        self.assertEqual(calls["full_all"], ["624_allocation_scan"])
        self.assertEqual(calls["full_named"], [])
        self.assertEqual(calls["provision"], [1])
        self.assertEqual(calls["flow"], ["complete_inventory", "provision"])
        # This routing double does not create; it proves apply delegated only
        # after the complete inventory result was available.
        self.assertEqual(device.up_flicks, 0)
        self.assertEqual(device.down_flicks, 0)
        self.assertEqual(device.creates, [])
        self.assertEqual(stack.cleared_bundles, [ghost_live.GHOST_BUNDLE])
        self.assertTrue(any("GHOST_CACHE_INVALIDATED=force-close" in m for m in logs))
        self.assertTrue(any("before any Create" in m for m in logs))

    def test_real_stale_fast_open_fixture_is_not_a0227_proof(self):
        """The captured cached a0226 row cannot satisfy a0227 reconciliation."""
        stale = fixture_nodes("ghost_624_fast_open_stale_a0226.json")

        self.assertTrue(ghost_live.on_containers_list(stale))
        self.assertEqual(
            [item[0] for item in ghost_live.container_instances(stale, "a0226")],
            ["4F1A9C93"],
        )
        self.assertEqual(ghost_live.container_instances(stale, "a0227"), [])

    def test_real_home_after_create_reopens_and_fully_reconciles_once(self):
        """The captured a0227 SpringBoard return is locally recovered, not guessed."""
        name = "a0227"
        home = fixture_nodes("ghost_627_after_create_home.json")
        device = FakeGhostDevice(
            count=70,
            reveal_created=True,
            after_create_nodes=home,
        )
        harness = SlotHarness(name)
        calls, logs = new_calls(), []
        verified_final = [
            Node(ntype="ocr", text="Containers", x=175, y=87, w=287, h=38),
            Node(ntype="ocr", text="New Container", x=375, y=1253, w=180, h=18),
            Node(ntype="ocr", text=name, x=207, y=900, w=115, h=25),
            Node(ntype="ocr", text=uuid_for(9999), x=390, y=935, w=300, h=20),
        ]

        stack = FakeStack(device)
        with fast_env(
            device,
            harness,
            calls,
            stack=stack,
            named_result=({NEW_SHORT}, verified_final),
        ):
            instance_id = ghost_live.create_named(
                stack, object(), logs.append, device.frame(), name, baseline_ids=set()
            )

        self.assertFalse(ghost_live.on_containers_list(home))
        self.assertEqual(instance_id, NEW_SHORT)
        self.assertEqual(calls["open"], [1])
        self.assertEqual(calls["full_named"], ["627_reconcile_reopened_scan"])
        self.assertEqual(device.creates, [name])
        self.assertEqual(calls["select"], [NEW_SHORT])
        self.assertTrue(
            any("deterministically reopening Ghost" in message for message in logs)
        )

    def test_home_reconcile_subtracts_the_persisted_duplicate_baseline(self):
        """Only the new UUID is selected when the slot name existed before Create."""
        name = "a0227"
        home = fixture_nodes("ghost_627_after_create_home.json")
        device = FakeGhostDevice(
            count=70,
            reveal_created=True,
            after_create_nodes=home,
        )
        device.add_row(name, uuid_for(73))
        harness = SlotHarness(name, state="provisioning", baseline=[OLD_SHORT])
        calls, logs = new_calls(), []
        verified_final = [
            Node(ntype="ocr", text="Containers", x=175, y=87, w=287, h=38),
            Node(ntype="ocr", text="New Container", x=375, y=1253, w=180, h=18),
            Node(ntype="ocr", text=name, x=207, y=750, w=115, h=25),
            Node(ntype="ocr", text=uuid_for(73), x=390, y=785, w=300, h=20),
            Node(ntype="ocr", text=name, x=207, y=900, w=115, h=25),
            Node(ntype="ocr", text=uuid_for(9999), x=390, y=935, w=300, h=20),
        ]
        stack = FakeStack(device)

        with fast_env(
            device,
            harness,
            calls,
            stack=stack,
            named_result=({OLD_SHORT, NEW_SHORT}, verified_final),
        ):
            instance_id = ghost_live.create_named(
                stack,
                object(),
                logs.append,
                device.frame(),
                name,
                baseline_ids={OLD_SHORT},
            )

        self.assertEqual(instance_id, NEW_SHORT)
        self.assertEqual(device.creates, [name])
        self.assertEqual(calls["open"], [1])
        self.assertEqual(calls["full_named"], ["627_reconcile_reopened_scan"])
        self.assertEqual(calls["select"], [NEW_SHORT])

    def test_home_reconcile_failure_is_typed_and_preserves_provisioning(self):
        """A failed local rescan pauses; it never creates twice or selects a row."""
        name = "a0227"
        device = FakeGhostDevice(
            count=70,
            reveal_created=True,
            after_create_nodes=fixture_nodes("ghost_627_after_create_home.json"),
        )
        harness = SlotHarness(name, state="provisioning", baseline=[])
        calls, logs = new_calls(), []

        stack = FakeStack(device)
        with fast_env(
            device,
            harness,
            calls,
            stack=stack,
            named_result=ghost_live.PhoneError("complete scan failed"),
        ):
            with self.assertRaises(
                ghost_live.ContainerProvisioningReconcileError
            ) as caught:
                ghost_live.create_named(
                    stack,
                    object(),
                    logs.append,
                    device.frame(),
                    name,
                    baseline_ids=set(),
                )

        self.assertEqual(
            caught.exception.failure_code,
            "container_provisioning_reconcile_failed",
        )
        self.assertEqual(device.creates, [name])
        self.assertEqual(harness.state, "provisioning")
        self.assertEqual(calls["select"], [])
        self.assertEqual(harness.ready, [])

    def test_resumed_slot_keeps_the_complete_scan(self):
        """A provisioning resume is never routed through the fast path."""
        name = "a0073"
        device = FakeGhostDevice(count=70)
        harness = SlotHarness(name, state="provisioning", baseline=[])
        calls, logs = new_calls(), []

        stack = FakeStack(device)
        created = self._apply(device, harness, calls, logs, stack=stack)

        self.assertEqual(created, [name])
        self.assertEqual(calls["full_all"], ["624_allocation_scan"])
        self.assertEqual(calls["full_named"], [])
        self.assertEqual(calls["provision"], [1])
        self.assertEqual(device.creates, [])
        self.assertEqual(stack.cleared_bundles, [ghost_live.GHOST_BUNDLE])
        self.assertFalse(any("fast Ghost create" in m for m in logs))

    def test_unverified_cache_clear_stops_before_open_or_create(self):
        """A stale Ghost process is never handed to inventory or Anthropic."""
        name = "a0227"
        device = FakeGhostDevice(count=70)
        harness = SlotHarness(name, state="provisioning", baseline=[])
        calls, logs = new_calls(), []
        stack = FakeStack(device)
        stack.clear_app_memory = lambda _bundle: "unknown"

        with self.assertRaisesRegex(
            ghost_live.ContainerProvisioningReconcileError, "unverified outcome"
        ) as caught:
            self._apply(device, harness, calls, logs, stack=stack)

        self.assertEqual(
            caught.exception.failure_code,
            "container_provisioning_reconcile_failed",
        )
        self.assertEqual(calls["open"], [])
        self.assertEqual(calls["full_all"], [])
        self.assertEqual(device.creates, [])

    def test_unproven_complete_inventory_is_typed_before_provision(self):
        name = "a0227"
        device = FakeGhostDevice(count=70)
        harness = SlotHarness(name)
        calls, logs = new_calls(), []

        with self.assertRaisesRegex(
            ghost_live.ContainerProvisioningReconcileError,
            "boundary-proven complete Ghost inventory",
        ):
            self._apply(
                device,
                harness,
                calls,
                logs,
                all_result=ghost_live.PhoneError("bottom boundary not proven"),
            )

        self.assertEqual(calls["flow"], ["complete_inventory"])
        self.assertEqual(calls["provision"], [])
        self.assertEqual(device.creates, [])

    def test_existing_name_on_phone_falls_back_to_complete_scan(self):
        """An unexplained phone copy fails closed into the complete scan."""
        name = "a0003"
        device = FakeGhostDevice(count=70)
        device.add_row(name, uuid_for(73))
        harness = SlotHarness(name)
        calls, logs = new_calls(), []

        created = self._apply(
            device, harness, calls, logs,
            all_result=({name: {OLD_SHORT}}, []),
            configure_result=OLD_SHORT,
        )

        self.assertEqual(created, [name])
        self.assertEqual(calls["full_all"], ["624_allocation_scan"])
        self.assertEqual(device.creates, [])
        self.assertEqual(calls["configure"], [1])
        self.assertTrue(any("exists as one incomplete" in m for m in logs))

    def test_inconclusive_capture_falls_back_to_complete_named_scan(self):
        """An unreadable post-create row retries with the complete named scan."""
        name = "a0073"
        device = FakeGhostDevice(count=70)
        device.hide_new_uuid = True
        harness = SlotHarness(name)
        calls, logs = new_calls(), []
        verified_final = [
            Node(ntype="ocr", text="Containers", x=175, y=87, w=287, h=38),
            Node(ntype="ocr", text="New Container", x=375, y=1253, w=180, h=18),
            Node(ntype="ocr", text=name, x=207, y=900, w=115, h=25),
            Node(ntype="ocr", text=uuid_for(9999), x=390, y=935, w=300, h=20),
        ]
        named_result = ({NEW_SHORT}, verified_final)

        stack = FakeStack(device)
        with fast_env(
            device, harness, calls, stack=stack, named_result=named_result
        ):
            instance_id = ghost_live.create_named(
                stack, object(), logs.append, device.frame(), name, baseline_ids=set()
            )

        self.assertEqual(instance_id, NEW_SHORT)
        self.assertEqual(calls["full_named"], ["627_reconcile_full"])
        self.assertEqual(device.creates, [name])
        self.assertTrue(any("inconclusive" in m for m in logs))

    def test_stop_aborts_before_creating(self):
        name = "a0073"
        device = FakeGhostDevice(count=70)
        harness = SlotHarness(name)
        calls, logs = new_calls(), []
        stack = FakeStack(device, stopped=True)

        with self.assertRaises(ghost_live.PhoneError):
            self._apply(device, harness, calls, logs, stack=stack)

        self.assertEqual(device.creates, [])
        self.assertEqual(calls["full_all"], [])
        self.assertEqual(calls["full_named"], [])

    def test_bounded_reconcile_is_cheaper_than_the_complete_named_scan(self):
        """The post-create capture reads a fraction of the complete named scan.

        With the list already near the bottom (the real post-create position,
        newest row appended), the full ``scan_named_container_instances`` must
        reverse to the top and walk all the way back down, while the bounded
        downward capture only has to prove the bottom once.  Both must return
        the identical instance set.
        """
        name = "a0073"

        def at_bottom_with_new_row():
            device = FakeGhostDevice(count=70)
            device.add_row(name, uuid_for(9999))
            device.start = device.maxstart - device.step  # one notch above bottom
            return device

        fast_device = at_bottom_with_new_row()
        with device_env(fast_device):
            fast_seen, _nodes = ghost_live.scan_named_container_instances_down(
                FakeStack(fast_device), object(), lambda _m: None,
                fast_device.frame(), name, stem="fast_reconcile",
            )

        full_device = at_bottom_with_new_row()
        with device_env(full_device):
            full_seen, _nodes = ghost_live.scan_named_container_instances(
                FakeStack(full_device), object(), lambda _m: None,
                full_device.frame(), name, stem="full_reconcile",
            )

        self.assertEqual(fast_seen, {NEW_SHORT})
        self.assertEqual(fast_seen, full_seen)
        self.assertEqual(fast_device.up_flicks, 0)
        self.assertGreater(full_device.up_flicks, 10)
        self.assertLessEqual(len(fast_device.dumps), 4)
        self.assertGreater(len(full_device.dumps), 20)

    def test_unstable_post_create_list_fails_without_selecting_a_uuid(self):
        """Direct create stays fail-closed if its new UUID cannot be proven."""
        name = "a0073"
        device = FakeGhostDevice(count=70)
        device.unstable = True
        harness = SlotHarness(name)
        calls, logs = new_calls(), []

        stack = FakeStack(device)
        with fast_env(device, harness, calls, stack=stack):
            with self.assertRaises(ghost_live.ContainerProvisioningReconcileError):
                ghost_live.create_named(
                    stack,
                    object(),
                    logs.append,
                    device.frame(),
                    name,
                    baseline_ids=set(),
                )

        self.assertEqual(device.creates, [name])
        self.assertEqual(calls["select"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
