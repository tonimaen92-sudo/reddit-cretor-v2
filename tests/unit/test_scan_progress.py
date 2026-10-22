# UTF-8
"""Offline tests: a long Ghost container scan and preparation must keep the
300-second inactivity watchdog alive only through *verified* progress.

These tests drive the real ``scan_all_container_instances``,
``scan_named_container_instances``, ``create_named`` and ``slots`` checkpoint
writers through a fake monotonic clock, synthetic Containers-list frames and a
mocked device.  No phone, no live state file, no account creation.
"""

from __future__ import annotations

import socket
import threading
import time
import unittest
from datetime import date
from unittest import mock

from friend_poster.apps import creator
from friend_poster.core import slots
from friend_poster.core.slots import CreationProgressClock
from friend_poster.device import ghost_live
from friend_poster.device.ssh_device import Phone
from friend_poster.device.ui_tree import Node
from friend_poster.reddit.reddit_to_verify import EmailFieldInvalid

REAL_SLEEP = time.sleep


class FakeMonotonic:
    """Injectable monotonic source for the scan/progress clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class CountingClock(CreationProgressClock):
    """Progress clock that records how many times it was noted."""

    def __init__(self, time_fn) -> None:
        super().__init__(time_fn=time_fn)
        self.notes = 0

    def note(self) -> None:
        self.notes += 1
        super().note()


def uuid_for(index: int) -> str:
    return f"{0xA0000000 + index:08X}-1111-2222-3333"


def rows_for(count: int) -> list[tuple[str, str]]:
    return [(f"a{index:04d}", uuid_for(index)) for index in range(1, count + 1)]


def make_frame(visible) -> list[Node]:
    """A proven Containers-list frame: header, fixed footer, paired rows."""
    nodes = [
        Node(ntype="ocr", text="Containers", x=175, y=87, w=287, h=38),
        Node(ntype="ocr", text="New Container", x=375, y=1253, w=180, h=18),
    ]
    y = 300
    for name, uid in visible:
        nodes.append(Node(ntype="ocr", text=name, x=207, y=y, w=115, h=25))
        nodes.append(Node(ntype="ocr", text=uid, x=390, y=y + 35, w=460, h=20))
        y += 200
    return nodes


class ScanDevice:
    """A mocked device that walks a long container list frame by frame.

    ``dump`` advances the fake clock by ``frame_seconds`` per fresh frame so a
    whole scan can simulate far more than the 300-second inactivity budget while
    the test itself runs quickly.
    """

    def __init__(
        self,
        rows,
        clock,
        *,
        span: int = 5,
        step: int = 3,
        frame_seconds: float = 40.0,
        real_frame_sleep: float = 0.0,
        target_name: str | None = None,
    ) -> None:
        self.rows = list(rows)
        self.clock = clock
        self.span = span
        self.step = step
        self.frame_seconds = frame_seconds
        self.real_frame_sleep = real_frame_sleep
        self.target_name = target_name
        self.maxstart = max(0, len(self.rows) - span)
        self.initial_start = self.maxstart // 2
        self._starts: dict[str, int] = {}
        self.dumps: list[str] = []

    def window(self, start: int):
        start = max(0, min(self.maxstart, start))
        return self.rows[start : start + self.span]

    def initial_frame(self) -> list[Node]:
        return make_frame(self.window(self.initial_start))

    def _start_for(self, prefix: str) -> int:
        return self._starts.setdefault(prefix, self.initial_start)

    def _tick(self) -> None:
        self.clock.advance(self.frame_seconds)
        if self.real_frame_sleep:
            REAL_SLEEP(self.real_frame_sleep)

    def reconcile_frame(self) -> list[Node]:
        # Only the freshly created target instance is observable.
        return make_frame([(self.target_name, uuid_for(900))])

    def named_dialog_frame(self) -> list[Node]:
        return [
            Node(ntype="ocr", text="Create", x=580, y=812, w=120, h=40),
            Node(ntype="ocr", text="Enter the name", x=120, y=520, w=300, h=30),
        ]

    def typed_field_frame(self) -> list[Node]:
        return [
            Node(ntype="ocr", text="Create", x=580, y=812, w=120, h=40),
            Node(ntype="ocr", text=str(self.target_name), x=120, y=520, w=200, h=30),
        ]

    def dump(self, _stack, _phone, stem, _lg, **_kwargs):
        self.dumps.append(stem)
        self._tick()
        if stem == "625_name":
            return ("", self.named_dialog_frame())
        if stem.startswith("626_named"):
            return ("", self.typed_field_frame())
        if stem == "627_after_create":
            return ("", self.reconcile_frame())
        if stem.startswith("627_reconcile"):
            return ("", self.reconcile_frame())
        if "_top_" in stem:
            prefix = stem.split("_top_", 1)[0]
            self._starts[prefix] = max(0, self._start_for(prefix) - self.step)
            return ("", make_frame(self.window(self._starts[prefix])))
        if "_bottom_" in stem:
            prefix = stem.split("_bottom_", 1)[0]
            self._starts[prefix] = min(
                self.maxstart, self._start_for(prefix) + self.step
            )
            return ("", make_frame(self.window(self._starts[prefix])))
        return ("", self.initial_frame())


class FakeStack:
    def check_cancelled(self) -> None:
        pass

    def interp_flick(self, *_args, **_kwargs) -> None:
        pass

    def tap_xy(self, *_args, **_kwargs) -> None:
        pass

    def tap_row(self, *_args, **_kwargs) -> None:
        pass

    def type_text_block(self, *_args, **_kwargs) -> None:
        pass

    def press_backspace(self, *_args, **_kwargs) -> None:
        pass

    def wait(self, *_args, **_kwargs) -> None:
        pass


class ScanProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = slots.CREATION_PROGRESS
        self._original_time_fn = self.clock._time_fn

    def tearDown(self) -> None:
        self.clock._time_fn = self._original_time_fn
        self.clock.reset()

    def _watch(self, fake, seconds: float = creator.BULK_INACTIVITY_SECONDS):
        self.clock._time_fn = fake
        self.clock.reset()
        event = threading.Event()
        watch = creator._InactivityWatch(
            event, seconds, self.clock, lambda _m: None, "a0031",
            poll_seconds=0.002,
        )
        watch.start()
        return event, watch

    def test_long_allocation_scan_keeps_the_watchdog_alive_and_finishes(self):
        device = ScanDevice(rows_for(30), FakeMonotonic(), real_frame_sleep=0.002)
        event, watch = self._watch(device.clock)
        try:
            with (
                mock.patch.object(ghost_live, "dump_named", device.dump),
                mock.patch.object(ghost_live, "list_flick", lambda *_a, **_k: None),
                mock.patch.object(ghost_live.time, "sleep", lambda *_a, **_k: None),
            ):
                inventory, _nodes = ghost_live.scan_all_container_instances(
                    FakeStack(), object(), lambda _m: None,
                    device.initial_frame(), stem="624_allocation_scan",
                )
            REAL_SLEEP(0.05)
        finally:
            watch.stop()

        self.assertEqual(len(inventory), 30)
        self.assertTrue(all(len(ids) == 1 for ids in inventory.values()))
        # The scan legitimately ran far longer than the inactivity budget...
        self.assertGreater(device.clock.now - 1000.0, 300.0)
        # ...yet verified traversal progress kept the timer well under it.
        self.assertLess(self.clock.idle_seconds(), creator.BULK_INACTIVITY_SECONDS)
        self.assertFalse(event.is_set())
        self.assertFalse(watch.fired)

    def test_target_absent_scan_reports_progress_through_other_containers(self):
        device = ScanDevice(
            rows_for(30), FakeMonotonic(), real_frame_sleep=0.002,
            target_name="a0031",
        )
        event, watch = self._watch(device.clock)
        try:
            with (
                mock.patch.object(ghost_live, "dump_named", device.dump),
                mock.patch.object(ghost_live, "list_flick", lambda *_a, **_k: None),
                mock.patch.object(ghost_live.time, "sleep", lambda *_a, **_k: None),
            ):
                seen, _nodes = ghost_live.scan_named_container_instances(
                    FakeStack(), object(), lambda _m: None,
                    device.initial_frame(), name="a0031",
                    stem="624_provision_scan_a0031",
                )
            REAL_SLEEP(0.05)
        finally:
            watch.stop()

        self.assertEqual(seen, set())
        self.assertGreater(device.clock.now - 1000.0, 300.0)
        self.assertLess(self.clock.idle_seconds(), creator.BULK_INACTIVITY_SECONDS)
        self.assertFalse(event.is_set())
        self.assertFalse(watch.fired)

    def test_stalled_scan_without_identities_times_out_at_the_budget(self):
        device = ScanDevice([], FakeMonotonic(), frame_seconds=120.0)
        event, watch = self._watch(device.clock)
        try:
            with (
                mock.patch.object(ghost_live, "dump_named", device.dump),
                mock.patch.object(ghost_live, "list_flick", lambda *_a, **_k: None),
                mock.patch.object(ghost_live.time, "sleep", lambda *_a, **_k: None),
            ):
                ghost_live.scan_all_container_instances(
                    FakeStack(), object(), lambda _m: None,
                    make_frame([]), stem="624_allocation_scan",
                )
            deadline = time.monotonic() + 3.0
            while not event.is_set() and time.monotonic() < deadline:
                REAL_SLEEP(0.005)
        finally:
            watch.stop()

        self.assertTrue(event.is_set())
        self.assertTrue(watch.fired)
        self.assertGreaterEqual(
            self.clock.idle_seconds(), creator.BULK_INACTIVITY_SECONDS
        )

    def test_watchdog_boundary_is_179_quiet_and_180_cancelled(self):
        for elapsed, should_fire in ((179.0, False), (180.0, True)):
            with self.subTest(elapsed=elapsed):
                fake = FakeMonotonic()
                clock = CreationProgressClock(time_fn=fake)
                event = threading.Event()
                watch = creator._InactivityWatch(
                    event, 180.0, clock, lambda _m: None, "a0031",
                    poll_seconds=0.002,
                )
                watch.start()
                try:
                    fake.advance(elapsed)
                    REAL_SLEEP(0.08)
                    self.assertEqual(event.is_set(), should_fire)
                finally:
                    watch.stop()


class ProgressIdentityTests(unittest.TestCase):
    """Focused checks that only freshly traversed identities count."""

    def setUp(self) -> None:
        self._original = ghost_live.CREATION_PROGRESS

    def tearDown(self) -> None:
        ghost_live.CREATION_PROGRESS = self._original

    def test_repeated_and_alternating_seen_frames_do_not_note_again(self):
        fake = FakeMonotonic()
        clock = CountingClock(fake)
        ghost_live.CREATION_PROGRESS = clock
        frame_a = make_frame(rows_for(3)[0:2])
        frame_b = make_frame(rows_for(3)[1:3])
        reported: set[str] = set()

        ghost_live._note_scan_progress(frame_a, reported)
        self.assertEqual(clock.notes, 1)
        self.assertEqual(clock.idle_seconds(), 0.0)

        fake.advance(150.0)
        ghost_live._note_scan_progress(frame_a, reported)
        ghost_live._note_scan_progress(frame_a, reported)
        self.assertEqual(clock.notes, 1)  # repeated already-seen frame
        self.assertAlmostEqual(clock.idle_seconds(), 150.0)

        ghost_live._note_scan_progress(frame_b, reported)
        self.assertEqual(clock.notes, 2)  # one genuinely new identity

        fake.advance(90.0)
        ghost_live._note_scan_progress(frame_a, reported)
        ghost_live._note_scan_progress(frame_b, reported)
        self.assertEqual(clock.notes, 2)
        self.assertAlmostEqual(clock.idle_seconds(), 90.0)

    def test_direction_sets_let_the_return_pass_count_again(self):
        fake = FakeMonotonic()
        clock = CountingClock(fake)
        ghost_live.CREATION_PROGRESS = clock
        frame = make_frame(rows_for(2))
        upward: set[str] = set()
        downward: set[str] = set()
        ghost_live._note_scan_progress(frame, upward)
        ghost_live._note_scan_progress(frame, upward)
        self.assertEqual(clock.notes, 1)
        # The downward direction owns a separate set, so re-seeing the same
        # rows on the way back counts as real traversal progress.
        ghost_live._note_scan_progress(frame, downward)
        self.assertEqual(clock.notes, 2)

    def test_cosmetic_status_bar_and_coordinate_changes_do_not_count(self):
        fake = FakeMonotonic()
        clock = CountingClock(fake)
        ghost_live.CREATION_PROGRESS = clock
        frame = make_frame(rows_for(2))
        reported: set[str] = set()
        ghost_live._note_scan_progress(frame, reported)
        self.assertEqual(clock.notes, 1)

        fake.advance(120.0)
        cosmetic = make_frame(rows_for(2))
        cosmetic.append(Node(ntype="ocr", text="18:50", x=600, y=20, w=80, h=20))
        cosmetic.append(Node(ntype="ocr", text="Wi-Fi", x=60, y=20, w=90, h=20))
        for node in cosmetic:
            node.x += 1
            node.y += 1
        ghost_live._note_scan_progress(cosmetic, reported)
        self.assertEqual(clock.notes, 1)
        self.assertAlmostEqual(clock.idle_seconds(), 120.0)

        # An unpaired UUID-shaped text node is not a container identity.
        fake.advance(60.0)
        unpaired = make_frame(rows_for(2))
        unpaired.append(
            Node(ntype="ocr", text=uuid_for(77), x=390, y=1230, w=460, h=20)
        )
        ghost_live._note_scan_progress(unpaired, reported)
        self.assertEqual(clock.notes, 1)
        self.assertAlmostEqual(clock.idle_seconds(), 180.0)


class ProvisioningTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original = slots.CREATION_PROGRESS

    def tearDown(self) -> None:
        slots.CREATION_PROGRESS = self._original

    def test_real_provisioning_transition_notes_once_and_repeat_does_not(self):
        fake = FakeMonotonic()
        clock = CreationProgressClock(time_fn=fake)
        slots.CREATION_PROGRESS = clock
        row = {"id": "a0031", "ghost_container_state": "needed"}
        rows = [row]
        with (
            mock.patch.object(slots, "load_slots", lambda: rows),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
        ):
            fake.advance(200.0)
            slots.mark_ghost_container_provisioning("a0031", {"OLD0001"}, None)
            self.assertEqual(clock.idle_seconds(), 0.0)
            self.assertEqual(row["ghost_container_state"], "provisioning")

            fake.advance(150.0)
            # Re-saving the same baseline returns before the state change and
            # must not touch the clock.
            slots.mark_ghost_container_provisioning("a0031", {"OLD0001"}, None)
            self.assertAlmostEqual(clock.idle_seconds(), 150.0)


class ContainerPipelineTests(unittest.TestCase):
    """The real container stage reaches the mocked email and Reddit stages."""

    class Stop:
        def __init__(self) -> None:
            self.flag = False

        def is_set(self) -> bool:
            return self.flag

        def set(self) -> None:
            self.flag = True

    class Phone:
        def close(self) -> None:
            pass

    class CloseFailPhone:
        def close(self) -> None:
            raise OSError("USB lost during teardown.")

    class RecordingPhone:
        """Fake attempt phone that records every liveness-hook assignment."""

        def __init__(self) -> None:
            self.hook = None
            self.set_calls: list = []

        def close(self) -> None:
            pass

        @property
        def progress_note(self):
            return self.hook

        @progress_note.setter
        def progress_note(self, value) -> None:
            self.hook = value
            self.set_calls.append(value)

    def _app(self):
        app = creator.App.__new__(creator.App)
        app.stop = self.Stop()
        app.log = lambda _m: None
        app.status = mock.Mock()
        app.phone = None
        app._connect = lambda _password: (self.Phone(), FakeStack())
        return app

    def _run_pipeline(self, app, row, create_reddit):
        """Drive the real pipeline with every non-container stage mocked."""
        with (
            mock.patch.object(creator, "create_one_hide_my_email",
                              lambda *_a, **_k: "offline-abcd@icloud.com"),
            mock.patch.object(creator, "run_create_reddit", create_reddit),
            mock.patch.object(
                creator,
                "resolve_crane_container_identity",
                lambda *_a, **_k: ("resolved", uuid_for(900)),
            ),
            mock.patch.object(
                creator,
                "reconcile_crane_container_identity",
                lambda _sid, instance_id, *_a, **_k: row.update(
                    crane_container_instance=instance_id
                ) or row,
            ),
            mock.patch.object(creator, "get_slot", lambda _sid: row),
            mock.patch.object(creator, "load_slots", lambda: [row]),
            mock.patch.object(creator, "slot_reddit_cleanup_pending", lambda _row: False),
            mock.patch.object(creator, "set_slot_birth_date", lambda *_a: None),
            mock.patch.object(creator, "_reconcile_pipeline_email", lambda *_a: None),
            mock.patch.object(creator, "_load_pipeline_email_records", lambda: []),
            mock.patch.object(
                creator, "set_slot_email",
                lambda _sid, address, _log, label=None: row.update(email=address),
            ),
            mock.patch.object(creator, "slot_reddit_ready", lambda _row: True),
            mock.patch.object(
                creator, "creation_feed_swipe_progress", lambda _row: (10, 10)
            ),
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
            mock.patch.object(ghost_live.time, "sleep", lambda *_a, **_k: None),
            mock.patch.object(slots.CREATION_PROGRESS, "_time_fn", FakeMonotonic()),
        ):
            return creator.App._run_creation_pipeline(
                app, "offline-password", "a0031", date(2005, 4, 20), 10,
                manage_worker=False,
            )

    def _container_stage(self, row):
        def stage(_phone, stack, lg, count=1, stop=None, close=True, names=None):
            row["ghost"] = True
            row["ghost_container_state"] = "ready"
            row["ghost_container_instance"] = uuid_for(900)
            return ["a0031"]

        return stage

    def test_late_close_failure_keeps_a_verified_success(self):
        # The account was verified complete before the USB teardown failed, so
        # the pipeline must still report success (and never a failure code).
        row = {"id": "a0031", "ghost": False, "ghost_container_state": "needed"}
        app = self._app()
        app._connect = lambda _password: (self.CloseFailPhone(), FakeStack())

        def create_reddit(_sid, _phone, _stack, **_kwargs):
            row["username"] = "QuietMoon49"
            row["password"] = "offline-secret"
            row["reddit"] = True
            return {"ok": True}

        with mock.patch.object(
            creator, "apply_reddit_containers", self._container_stage(row)
        ):
            ok = self._run_pipeline(app, row, create_reddit)

        self.assertTrue(ok)
        self.assertFalse(app._last_pipeline_attempt_timed_out)
        self.assertFalse(app._last_pipeline_failure_shared)
        self.assertIsNone(app._last_pipeline_failure_code)

    def test_late_close_failure_keeps_an_account_specific_code(self):
        # A teardown error must not erase the specific failure the attempt
        # already classified, even though the pipeline still returns failure.
        row = {"id": "a0031", "ghost": True, "ghost_container_state": "ready",
               "ghost_container_instance": uuid_for(900)}
        app = self._app()
        app._connect = lambda _password: (self.CloseFailPhone(), FakeStack())

        def create_reddit(_sid, _phone, _stack, **_kwargs):
            raise EmailFieldInvalid("email field never accepted the address")

        with mock.patch.object(
            creator, "apply_reddit_containers", self._container_stage(row)
        ):
            ok = self._run_pipeline(app, row, create_reddit)

        self.assertFalse(ok)
        self.assertEqual(app._last_pipeline_failure_code, "reddit_email_field_invalid")
        self.assertIn("email field", app._last_pipeline_failure_reason)

    def test_long_container_stage_reaches_email_and_reddit_without_skipping(self):
        fake = FakeMonotonic()
        device = ScanDevice(
            rows_for(30), fake, real_frame_sleep=0.001, target_name="a0031"
        )
        row = {"id": "a0031", "ghost": False, "ghost_container_state": "needed"}
        app = self._app()
        stages: list[str] = []

        def container_stage(_phone, stack, lg, count=1, stop=None, close=True, names=None):
            nodes = device.initial_frame()
            inventory, _ = ghost_live.scan_all_container_instances(
                stack, _phone, lg, nodes, stem="624_allocation_scan"
            )
            baseline = set().union(*inventory.values())
            # The target does not exist yet: the named scan still traverses.
            ghost_live.scan_named_container_instances(
                stack, _phone, lg, nodes, "a0031", stem="624_provision_scan_a0031"
            )
            slots.mark_ghost_container_provisioning("a0031", baseline, lg)
            instance_id = ghost_live.create_named(
                stack, _phone, lg, nodes, "a0031", baseline_ids=baseline
            )
            slots.mark_ghost_container_ready("a0031", instance_id, lg)
            stages.append("container")
            return ["a0031"]

        def create_email(_phone, _stack, _lg, stop=None, label=None):
            stages.append("email")
            return "offline-abcd@icloud.com"

        def create_reddit(_sid, _phone, _stack, **_kwargs):
            stages.append("reddit")
            row["username"] = "QuietMoon49"
            row["password"] = "offline-secret"
            row["reddit"] = True
            return {"ok": True}

        app._bind_crane_container_identity = lambda _phone, _sid, **_kwargs: (
            row.update(crane_container_instance=uuid_for(900)) or uuid_for(900)
        )

        with (
            mock.patch.object(creator, "apply_reddit_containers", container_stage),
            mock.patch.object(creator, "create_one_hide_my_email", create_email),
            mock.patch.object(creator, "run_create_reddit", create_reddit),
            mock.patch.object(creator, "get_slot", lambda _sid: row),
            mock.patch.object(creator, "load_slots", lambda: [row]),
            mock.patch.object(creator, "slot_reddit_cleanup_pending", lambda _row: False),
            mock.patch.object(creator, "set_slot_birth_date", lambda *_a: None),
            mock.patch.object(creator, "_reconcile_pipeline_email", lambda *_a: None),
            mock.patch.object(creator, "_load_pipeline_email_records", lambda: []),
            mock.patch.object(
                creator, "set_slot_email",
                lambda _sid, address, _log, label=None: row.update(email=address),
            ),
            mock.patch.object(creator, "slot_reddit_ready", lambda _row: True),
            mock.patch.object(
                creator, "creation_feed_swipe_progress", lambda _row: (10, 10)
            ),
            mock.patch.object(slots, "load_slots", lambda: [row]),
            mock.patch.object(slots, "save_slots", lambda _rows: None),
            mock.patch.object(ghost_live.time, "sleep", lambda *_a, **_k: None),
            mock.patch.object(slots.CREATION_PROGRESS, "_time_fn", fake),
            mock.patch.object(ghost_live, "dump_named", device.dump),
            mock.patch.object(ghost_live, "list_flick", lambda *_a, **_k: None),
            mock.patch.object(
                ghost_live, "_select_named_node", lambda *_a, **_k: None
            ),
        ):
            ok = creator.App._run_creation_pipeline(
                app, "offline-password", "a0031", date(2005, 4, 20), 10,
                manage_worker=False,
            )

        self.assertTrue(ok)
        self.assertFalse(app._last_pipeline_attempt_timed_out)
        self.assertEqual(stages, ["container", "email", "reddit"])
        self.assertTrue(row["ghost"])
        self.assertEqual(row["ghost_container_state"], "ready")

    def test_attempt_arms_then_clears_the_device_liveness_hook(self):
        # Every completed device round-trip of the attempt must feed the
        # inactivity watchdog, and the hook must be removed again on teardown so
        # no later op can be masked by a stale pipeline phone.
        row = {"id": "a0031", "ghost": False, "ghost_container_state": "needed"}
        app = self._app()
        phone = self.RecordingPhone()
        app._connect = lambda _password: (phone, FakeStack())

        def create_reddit(_sid, _phone, _stack, **_kwargs):
            row["username"] = "QuietMoon49"
            row["password"] = "offline-secret"
            row["reddit"] = True
            return {"ok": True}

        with mock.patch.object(
            creator, "apply_reddit_containers", self._container_stage(row)
        ):
            ok = self._run_pipeline(app, row, create_reddit)

        self.assertTrue(ok)
        self.assertEqual(phone.set_calls[0], slots.CREATION_PROGRESS.note)
        self.assertIsNone(phone.set_calls[-1])


class DeviceLivenessTests(unittest.TestCase):
    """A completed device round-trip proves the attempt is alive."""

    def _phone(self):
        phone = Phone()
        notes: list[int] = []
        phone.progress_note = lambda: notes.append(1)
        return phone, notes

    def _response(self, payload: bytes = b"ok\n", status: int = 0):
        stdout = mock.Mock()
        stdout.read.return_value = payload
        stdout.channel.recv_exit_status.return_value = status
        stderr = mock.Mock()
        stderr.read.return_value = b""
        return (mock.Mock(), stdout, stderr)

    def test_exec_ok_notes_progress_on_a_completed_round_trip(self):
        phone, notes = self._phone()
        phone.ssh = mock.Mock()
        phone.ssh.exec_command.return_value = self._response()

        code, _out = phone.exec_ok("true")

        self.assertEqual(code, 0)
        self.assertEqual(len(notes), 1)

    def test_silent_timeout_does_not_note_progress(self):
        phone, notes = self._phone()
        phone.ssh = mock.Mock()
        phone.ssh.exec_command.side_effect = socket.timeout("device did not answer")

        code, _out = phone.exec_ok("true", timeout=1)

        self.assertEqual(code, 124)
        self.assertEqual(notes, [])

    def test_sftp_read_fallback_still_notes_progress(self):
        # The dump path falls back to an SSH stream when SFTP is unavailable;
        # that completed read is still proof the attempt is alive.
        phone, notes = self._phone()
        phone.ssh = mock.Mock()
        phone.ssh.open_sftp.side_effect = RuntimeError("sftp unavailable")
        phone.ssh.exec_command.return_value = self._response(b"<hierarchy/>")

        raw = phone.sftp_read_bytes("/tmp/xx_dump.xml")

        self.assertEqual(raw, b"<hierarchy/>")
        self.assertEqual(len(notes), 1)

    def test_no_hook_set_is_a_silent_no_op(self):
        phone = Phone()
        phone.ssh = mock.Mock()
        phone.ssh.exec_command.return_value = self._response()

        code, _out = phone.exec_ok("true")

        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
