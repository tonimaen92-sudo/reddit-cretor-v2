"""Focused offline tests for the device-bound Xiaomi radio reset."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from friend_poster.services import android_radio_reset as radio


SERIAL = "configured-xiaomi"
OTHER_SERIAL = "other-android"


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.event: FakeCancelEvent | None = None

    def monotonic(self) -> float:
        return self.value

    def wait(self, seconds: float) -> bool:
        self.value += float(seconds)
        return bool(self.event and self.event.is_set())


class FakeCancelEvent:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.flag = False
        clock.event = self

    def is_set(self) -> bool:
        return self.flag

    def wait(self, seconds: float) -> bool:
        return self.clock.wait(seconds)


class FakeAdb:
    def __init__(
        self,
        *,
        devices: str | None = None,
        manufacturer: str = "Xiaomi",
        brand: str = "Redmi",
        command_support: bool = True,
        help_returncode: int = 0,
        airplane: bool = False,
        mobile_data: bool = True,
    ) -> None:
        self.devices = devices or (
            "List of devices attached\n"
            f"{OTHER_SERIAL}\tdevice\n"
            f"{SERIAL}\tdevice\n"
        )
        self.manufacturer = manufacturer
        self.brand = brand
        self.command_support = command_support
        self.help_returncode = help_returncode
        self.airplane = airplane
        self.mobile_data = mobile_data
        self.calls: list[tuple[str, ...]] = []
        self.ignore_airplane_enable = False
        self.fail_restore_disable = False
        self.raise_with_secret = False
        self.on_airplane_enable = None
        self.identities = {
            SERIAL: (manufacturer, brand, "Configured model"),
            OTHER_SERIAL: ("Other", "Other", "Other model"),
        }

    def __call__(self, argv, timeout):
        del timeout
        command = tuple(argv)
        self.calls.append(command)
        if self.raise_with_secret:
            raise RuntimeError("secret-device-identifier")
        if command == ("adb", "devices"):
            return radio.CommandResult(0, self.devices)

        self._assert_targeted(command)
        selected_serial = command[2]
        args = command[3:]
        if args == ("get-state",):
            return radio.CommandResult(0, "device\n")
        if args == ("shell", "getprop", "ro.product.manufacturer"):
            return radio.CommandResult(0, self.identities[selected_serial][0] + "\n")
        if args == ("shell", "getprop", "ro.product.brand"):
            return radio.CommandResult(0, self.identities[selected_serial][1] + "\n")
        if args == ("shell", "getprop", "ro.product.model"):
            return radio.CommandResult(0, self.identities[selected_serial][2] + "\n")
        if args == ("shell", "cmd", "connectivity", "help"):
            output = (
                "Connectivity service commands:\n"
                "  airplane-mode [enable|disable]\n"
                "    Turn airplane mode on or off."
                if self.command_support
                else "help"
            )
            return radio.CommandResult(self.help_returncode, output)
        if args == ("shell", "cmd", "phone", "data", "help"):
            output = (
                "Mobile Data Test Mode Commands:\n"
                "  data enable: enable mobile data connectivity\n"
                "  data disable: disable mobile data connectivity"
                if self.command_support
                else "help"
            )
            return radio.CommandResult(self.help_returncode, output)
        if args == ("shell", "settings", "get", "global", "airplane_mode_on"):
            return radio.CommandResult(0, "1\n" if self.airplane else "0\n")
        if args == ("shell", "settings", "get", "global", "mobile_data"):
            return radio.CommandResult(0, "1\n" if self.mobile_data else "0\n")
        if args == ("shell", "cmd", "connectivity", "airplane-mode", "enable"):
            if not self.ignore_airplane_enable:
                self.airplane = True
            if self.on_airplane_enable is not None:
                self.on_airplane_enable()
            return radio.CommandResult(0)
        if args == ("shell", "cmd", "connectivity", "airplane-mode", "disable"):
            if self.fail_restore_disable:
                return radio.CommandResult(1, stderr="private adb detail")
            self.airplane = False
            return radio.CommandResult(0)
        if args == ("shell", "cmd", "phone", "data", "enable"):
            self.mobile_data = True
            return radio.CommandResult(0)
        return radio.CommandResult(1, stderr="unsupported private output")

    @staticmethod
    def _assert_targeted(command: tuple[str, ...]) -> None:
        if command[:2] != ("adb", "-s") or command[2] not in {SERIAL, OTHER_SERIAL}:
            raise AssertionError(f"device command was not exact-targeted: {command!r}")


class AndroidRadioResetTests(unittest.TestCase):
    def run_reset(self, fake: FakeAdb, **kwargs):
        clock = FakeClock()
        cancel = FakeCancelEvent(clock)
        result = radio.reset_android_cellular_radio(
            adb_path="adb",
            device_serial=SERIAL,
            cancel_event=cancel,
            command_runner=fake,
            monotonic=clock.monotonic,
            waiter=cancel.wait,
            command_timeout_seconds=2,
            state_timeout_seconds=2,
            poll_interval_seconds=0.25,
            airplane_hold_seconds=0.5,
            settle_seconds=0.25,
            **kwargs,
        )
        return result, clock, cancel

    def test_multiple_devices_never_selects_generic_or_wrong_device(self):
        fake = FakeAdb()
        result, _clock, _cancel = self.run_reset(fake)

        self.assertTrue(result.cycle_completed)
        self.assertTrue(result.restore_verified)
        self.assertEqual(fake.calls[0], ("adb", "devices"))
        self.assertTrue(all(call[:3] == ("adb", "-s", SERIAL) for call in fake.calls[1:]))
        self.assertFalse(any(OTHER_SERIAL in call for call in fake.calls[1:]))

    def test_without_configured_serial_exactly_one_xiaomi_is_locked(self):
        fake = FakeAdb()
        clock = FakeClock()
        cancel = FakeCancelEvent(clock)
        result = radio.reset_android_cellular_radio(
            adb_path="adb",
            device_serial=None,
            cancel_event=cancel,
            command_runner=fake,
            monotonic=clock.monotonic,
            waiter=cancel.wait,
            command_timeout_seconds=2,
            state_timeout_seconds=2,
            poll_interval_seconds=0.25,
            airplane_hold_seconds=0,
            settle_seconds=0,
        )

        self.assertTrue(result.cycle_completed)
        mutating = [
            call for call in fake.calls
            if call[-1:] in (("enable",), ("disable",))
        ]
        self.assertTrue(mutating)
        self.assertTrue(all(call[2] == SERIAL for call in mutating))

    def test_without_serial_two_xiaomi_devices_is_ambiguous_and_never_mutates(self):
        fake = FakeAdb()
        fake.identities[OTHER_SERIAL] = ("Xiaomi", "Poco", "Second Xiaomi")
        clock = FakeClock()
        cancel = FakeCancelEvent(clock)
        with self.assertRaises(radio.AndroidDeviceUnavailableError):
            radio.reset_android_cellular_radio(
                adb_path="adb",
                device_serial=None,
                cancel_event=cancel,
                command_runner=fake,
                monotonic=clock.monotonic,
                waiter=cancel.wait,
            )
        self.assertFalse(any(call[-1:] in (("enable",), ("disable",)) for call in fake.calls))

    def test_missing_configured_device_fails_before_any_target_action(self):
        fake = FakeAdb(devices=f"List of devices attached\n{OTHER_SERIAL}\tdevice\n")
        with self.assertRaises(radio.AndroidDeviceUnavailableError) as raised:
            self.run_reset(fake)

        self.assertEqual(raised.exception.category, "device_unavailable")
        self.assertEqual(fake.calls, [("adb", "devices")])

    def test_non_xiaomi_identity_fails_before_capability_or_mutation(self):
        fake = FakeAdb(manufacturer="Other", brand="Other")
        with self.assertRaises(radio.AndroidDeviceUnavailableError):
            self.run_reset(fake)

        self.assertFalse(any("airplane-mode" in call for call in fake.calls))
        self.assertFalse(any(call[-3:] == ("phone", "data", "enable") for call in fake.calls))

    def test_command_support_is_proven_before_mutation(self):
        fake = FakeAdb(command_support=False)
        with self.assertRaises(radio.AndroidCommandUnsupportedError) as raised:
            self.run_reset(fake)

        self.assertEqual(raised.exception.category, "command_unsupported")
        self.assertFalse(any(call[-1:] in (("enable",), ("disable",)) for call in fake.calls))

    def test_live_help_exit_255_is_accepted_when_full_grammar_is_proven(self):
        fake = FakeAdb(command_support=True, help_returncode=255)

        result, _clock, _cancel = self.run_reset(fake)

        self.assertTrue(result.cycle_completed)
        self.assertTrue(result.restore_verified)

    def test_exit_255_without_full_help_grammar_is_rejected_before_mutation(self):
        fake = FakeAdb(command_support=False, help_returncode=255)

        with self.assertRaises(radio.AndroidCommandUnsupportedError):
            self.run_reset(fake)

        self.assertFalse(any(call[-1:] in (("enable",), ("disable",)) for call in fake.calls))

    def test_unexpected_help_exit_rejects_even_valid_looking_grammar(self):
        fake = FakeAdb(command_support=True, help_returncode=42)

        with self.assertRaises(radio.AndroidCommandUnsupportedError):
            self.run_reset(fake)

        self.assertFalse(any(call[-1:] in (("enable",), ("disable",)) for call in fake.calls))

    def test_normalizes_initial_state_then_runs_one_bounded_cycle(self):
        fake = FakeAdb(airplane=True, mobile_data=False)
        phases = []
        result, _clock, _cancel = self.run_reset(fake, progress=phases.append)

        self.assertTrue(result.airplane_enabled_verified)
        self.assertFalse(fake.airplane)
        self.assertTrue(fake.mobile_data)
        # Exactly one ON mutation is the actual reset cycle.  OFF/data ON can
        # occur once for normalization and once again in mandatory restoration.
        self.assertEqual(
            sum(call[-2:] == ("airplane-mode", "enable") for call in fake.calls),
            1,
        )
        self.assertIn({"phase": "initial_state_verified"}, phases)
        self.assertIn({"phase": "restore_verified"}, phases)

    def test_durable_transition_order_and_restore_only_never_enable_airplane(self):
        fake = FakeAdb()
        transitions = []
        result, _clock, _cancel = self.run_reset(fake, on_transition=transitions.append)
        self.assertEqual(
            transitions,
            [
                "pending",
                "on_confirmed",
                "restore_pending",
                "off_confirmed",
                "verify_pending",
                "completed",
            ],
        )
        self.assertEqual(result.transitions, tuple(transitions))

        restore_fake = FakeAdb(airplane=True, mobile_data=False)
        restore_clock = FakeClock()
        restore_cancel = FakeCancelEvent(restore_clock)
        restore_transitions = []
        restored = radio.restore_android_cellular_radio(
            adb_path="adb",
            device_serial=SERIAL,
            cancel_event=restore_cancel,
            command_runner=restore_fake,
            on_transition=restore_transitions.append,
            monotonic=restore_clock.monotonic,
            waiter=restore_cancel.wait,
            command_timeout_seconds=2,
            state_timeout_seconds=2,
            poll_interval_seconds=0.25,
        )
        self.assertTrue(restored.restore_verified)
        self.assertEqual(
            restore_transitions,
            ["restore_pending", "off_confirmed", "completed"],
        )
        self.assertFalse(
            any(call[-2:] == ("airplane-mode", "enable") for call in restore_fake.calls)
        )

    def test_cancellation_during_cycle_still_restores_off_and_data_on(self):
        fake = FakeAdb()
        clock = FakeClock()
        cancel = FakeCancelEvent(clock)
        fake.on_airplane_enable = lambda: setattr(cancel, "flag", True)

        with self.assertRaises(radio.AndroidRadioResetCancelled) as raised:
            radio.reset_android_cellular_radio(
                adb_path="adb",
                device_serial=SERIAL,
                cancel_event=cancel,
                command_runner=fake,
                monotonic=clock.monotonic,
                waiter=cancel.wait,
                command_timeout_seconds=2,
                state_timeout_seconds=2,
                poll_interval_seconds=0.25,
                airplane_hold_seconds=0.5,
                settle_seconds=0,
            )

        self.assertEqual(raised.exception.category, "cancelled")
        self.assertTrue(raised.exception.restore_verified)
        self.assertFalse(fake.airplane)
        self.assertTrue(fake.mobile_data)
        self.assertGreaterEqual(
            sum(call[-2:] == ("airplane-mode", "disable") for call in fake.calls),
            1,
        )

    def test_journal_failure_precedes_airplane_on_and_still_restores(self):
        fake = FakeAdb()

        def fail_journal(_phase):
            raise OSError("private journal path")

        with self.assertRaises(radio.AndroidRadioJournalError) as raised:
            self.run_reset(fake, on_transition=fail_journal)

        self.assertEqual(raised.exception.category, "journal_failed")
        self.assertTrue(raised.exception.restore_verified)
        self.assertFalse(
            any(call[-2:] == ("airplane-mode", "enable") for call in fake.calls)
        )
        self.assertFalse(fake.airplane)
        self.assertTrue(fake.mobile_data)

    def test_restore_failure_is_explicitly_unverified(self):
        fake = FakeAdb(airplane=True, mobile_data=False)
        fake.fail_restore_disable = True
        clock = FakeClock()
        cancel = FakeCancelEvent(clock)

        with self.assertRaises(radio.AndroidRadioRestoreError) as raised:
            radio.restore_android_cellular_radio(
                adb_path="adb",
                device_serial=SERIAL,
                cancel_event=cancel,
                command_runner=fake,
                monotonic=clock.monotonic,
                waiter=cancel.wait,
                command_timeout_seconds=2,
                state_timeout_seconds=1,
                poll_interval_seconds=0.25,
            )

        self.assertIs(raised.exception.restore_verified, False)
        self.assertTrue(fake.airplane)

    def test_unverified_transition_is_bounded_and_finally_restored(self):
        fake = FakeAdb()
        fake.ignore_airplane_enable = True
        with self.assertRaises(radio.AndroidRadioStateError) as raised:
            self.run_reset(fake)

        self.assertEqual(raised.exception.category, "state_unverified")
        self.assertTrue(raised.exception.restore_verified)
        self.assertFalse(fake.airplane)
        self.assertTrue(fake.mobile_data)

    def test_runner_exception_is_sanitized(self):
        fake = FakeAdb()
        fake.raise_with_secret = True
        with self.assertRaises(radio.AndroidAdbCommandError) as raised:
            self.run_reset(fake)

        self.assertNotIn("secret", str(raised.exception).casefold())
        self.assertNotIn(SERIAL, str(raised.exception))
        self.assertEqual(raised.exception.category, "adb_command_failed")

    def test_malformed_or_option_like_serial_is_rejected_without_adb(self):
        fake = FakeAdb()
        clock = FakeClock()
        cancel = FakeCancelEvent(clock)
        for serial in ("-d", "serial with spaces", "serial\nnext"):
            with self.subTest(serial=serial):
                with self.assertRaises(radio.AndroidRadioConfigurationError):
                    radio.reset_android_cellular_radio(
                        adb_path="adb",
                        device_serial=serial,
                        cancel_event=cancel,
                        command_runner=fake,
                    )
        self.assertEqual(fake.calls, [])

    def test_adb_discovery_prefers_explicit_then_env_and_newest_temp_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit = root / "explicit-adb.exe"
            env_adb = root / "env-adb.exe"
            explicit.touch()
            env_adb.touch()
            self.assertEqual(
                radio.discover_adb_executable(
                    explicit,
                    environ={"ANDROID_ADB": str(env_adb), "TEMP": str(root)},
                    which=lambda _name: None,
                ),
                str(explicit.resolve()),
            )
            self.assertEqual(
                radio.discover_adb_executable(
                    environ={"ANDROID_ADB": str(env_adb), "TEMP": str(root)},
                    which=lambda _name: None,
                ),
                str(env_adb.resolve()),
            )

            older = root / "codex-android-diagnostics-old" / "platform-tools" / "adb.exe"
            newer = root / "codex-android-diagnostics-new" / "platform-tools" / "adb.exe"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            older.touch()
            newer.touch()
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))
            self.assertEqual(
                radio.discover_adb_executable(
                    environ={"TEMP": str(root)},
                    which=lambda _name: None,
                ),
                str(newer.resolve()),
            )


if __name__ == "__main__":
    unittest.main()
