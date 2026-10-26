"""Bounded, device-bound Android cellular radio reset helpers.

This module never operates on an arbitrary device.  Callers may provide the
configured Xiaomi serial; otherwise preflight requires exactly one authorized
Xiaomi/Redmi/POCO and binds every mutating command to it with ``adb -s``.  The
only untargeted ADB command is the read-only ``adb devices`` inventory.

Errors and progress events never contain command output, paths, or the device
serial.  This is important because ADB output can contain identifiers which
must not be copied into Creator logs or recovery prompts.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence


DEFAULT_COMMAND_TIMEOUT_SECONDS = 10.0
DEFAULT_STATE_TIMEOUT_SECONDS = 12.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_AIRPLANE_HOLD_SECONDS = 15.0
DEFAULT_SETTLE_SECONDS = 2.0

_SAFE_SERIAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_XIAOMI_IDENTITIES = frozenset({"xiaomi", "redmi", "poco"})


class CancelEvent(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


@dataclass(frozen=True)
class CommandResult:
    """Sanitized command-runner result used by the injectable boundary."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], float], CommandResult]
ProgressCallback = Callable[[dict[str, str]], None]
TransitionCallback = Callable[[str], None]


@dataclass(frozen=True)
class AndroidRadioResetResult:
    """Proof that one bounded radio cycle and its final restore completed."""

    cycle_completed: bool
    airplane_enabled_verified: bool
    airplane_disabled_verified: bool
    mobile_data_enabled_verified: bool
    restore_verified: bool
    transitions: tuple[str, ...]


@dataclass(frozen=True)
class AndroidRadioRestoreResult:
    """Proof that a nonfinal journal was restored without a second ON cycle."""

    airplane_disabled_verified: bool
    mobile_data_enabled_verified: bool
    restore_verified: bool
    transitions: tuple[str, ...]


class AndroidRadioResetError(RuntimeError):
    """Base class for secret-free, non-AI Android reset failures."""

    ai_recoverable = False

    def __init__(self, message: str, *, category: str):
        super().__init__(message)
        self.category = category
        self.restore_verified: bool | None = None


class AndroidRadioConfigurationError(AndroidRadioResetError):
    def __init__(self, message: str = "Android radio reset configuration is invalid."):
        super().__init__(message, category="configuration")


class AndroidDeviceUnavailableError(AndroidRadioResetError):
    def __init__(self, message: str = "The configured Xiaomi is not ready over ADB."):
        super().__init__(message, category="device_unavailable")


class AndroidCommandUnsupportedError(AndroidRadioResetError):
    def __init__(self, message: str = "The configured Xiaomi lacks required radio commands."):
        super().__init__(message, category="command_unsupported")


class AndroidAdbCommandError(AndroidRadioResetError):
    def __init__(self, message: str = "A bounded Xiaomi ADB command failed safely."):
        super().__init__(message, category="adb_command_failed")


class AndroidRadioStateError(AndroidRadioResetError):
    def __init__(self, message: str = "The Xiaomi radio transition could not be verified."):
        super().__init__(message, category="state_unverified")


class AndroidRadioJournalError(AndroidRadioResetError):
    def __init__(self, message: str = "The radio recovery journal could not be updated safely."):
        super().__init__(message, category="journal_failed")


class AndroidRadioResetCancelled(AndroidRadioResetError):
    def __init__(self, message: str = "Android radio reset was cancelled safely."):
        super().__init__(message, category="cancelled")


class AndroidRadioRestoreError(AndroidRadioResetError):
    def __init__(self, message: str = "The Xiaomi safe radio state could not be verified."):
        super().__init__(message, category="restore_unverified")


def _existing_file(value: str | Path | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        path = Path(text).expanduser()
        if path.is_file():
            return str(path.resolve())
    except (OSError, RuntimeError):
        return None
    return None


def discover_adb_executable(
    configured_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """Resolve ADB using a fixed, bounded and testable search order.

    An explicitly configured path (or ``ANDROID_ADB``) is authoritative: if
    present but invalid, discovery fails rather than silently executing a
    different binary.  The temporary diagnostic fallback is limited to the
    known Codex Android diagnostic directory shape and chooses the newest
    executable only.
    """

    env = os.environ if environ is None else environ
    if configured_path is not None and str(configured_path).strip():
        configured = str(configured_path).strip()
        direct = _existing_file(configured)
        if direct:
            return direct
        if not Path(configured).parent.name:
            located = _existing_file(which(configured))
            if located:
                return located
        raise AndroidRadioConfigurationError("The configured ADB executable is unavailable.")

    env_adb = str(env.get("ANDROID_ADB", "") or "").strip()
    if env_adb:
        direct = _existing_file(env_adb)
        if direct:
            return direct
        if not Path(env_adb).parent.name:
            located = _existing_file(which(env_adb))
            if located:
                return located
        raise AndroidRadioConfigurationError("The configured ADB executable is unavailable.")

    located = _existing_file(which("adb"))
    if located:
        return located

    local_app_data = str(env.get("LOCALAPPDATA", "") or "").strip()
    if local_app_data:
        sdk_adb = _existing_file(
            Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
        )
        if sdk_adb:
            return sdk_adb

    temp_root = str(env.get("TEMP", "") or env.get("TMP", "") or "").strip()
    if not temp_root:
        temp_root = tempfile.gettempdir()
    try:
        candidates = [
            path
            for path in Path(temp_root).glob(
                "codex-android-diagnostics-*/platform-tools/adb.exe"
            )
            if path.is_file()
        ]
        # Only the newest 128 known-shape candidates are considered.  This
        # keeps discovery bounded even if an unhealthy temp cleanup left many
        # old diagnostic folders behind.
        candidates = sorted(
            candidates,
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )[:128]
    except (OSError, RuntimeError):
        candidates = []
    if candidates:
        return str(candidates[0].resolve())
    raise AndroidRadioConfigurationError("ADB executable could not be located safely.")


def _default_command_runner(argv: Sequence[str], timeout: float) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, float(timeout)),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        # Never include exception text: it can contain the path, command line,
        # serial, or output from the attached device.
        raise AndroidAdbCommandError() from None
    return CommandResult(
        returncode=int(completed.returncode),
        stdout=str(completed.stdout or ""),
        stderr=str(completed.stderr or ""),
    )


class _RadioResetSession:
    def __init__(
        self,
        *,
        adb_path: str,
        device_serial: str | None,
        cancel_event: CancelEvent,
        command_runner: CommandRunner,
        progress: ProgressCallback | None,
        on_transition: TransitionCallback | None,
        monotonic: Callable[[], float],
        waiter: Callable[[float], bool],
        command_timeout_seconds: float,
        state_timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> None:
        self.adb_path = adb_path
        self.device_serial = device_serial
        self.cancel_event = cancel_event
        self.command_runner = command_runner
        self.progress = progress
        self.on_transition = on_transition
        self.transitions: list[str] = []
        self.monotonic = monotonic
        self.waiter = waiter
        self.command_timeout_seconds = command_timeout_seconds
        self.state_timeout_seconds = state_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def emit(self, phase: str) -> None:
        try:
            if self.progress is not None:
                self.progress({"phase": phase})
        except Exception:
            raise AndroidAdbCommandError(
                "Android radio reset progress reporting failed safely."
            ) from None

    def transition(self, phase: str, *, best_effort: bool = False) -> bool:
        self.transitions.append(phase)
        try:
            if self.on_transition is not None:
                self.on_transition(phase)
            self.emit(phase)
        except Exception:
            if best_effort:
                return False
            raise AndroidRadioJournalError() from None
        return True

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise AndroidRadioResetCancelled()

    def wait(self, seconds: float) -> None:
        self.check_cancelled()
        if seconds > 0 and self.waiter(float(seconds)):
            raise AndroidRadioResetCancelled()
        self.check_cancelled()

    def run_raw(self, argv: Sequence[str]) -> CommandResult:
        try:
            result = self.command_runner(tuple(argv), self.command_timeout_seconds)
        except AndroidRadioResetError:
            raise
        except Exception:
            raise AndroidAdbCommandError() from None
        if not isinstance(result, CommandResult):
            raise AndroidAdbCommandError()
        return result

    def run_inventory(self) -> CommandResult:
        # This is the sole command without -s and is strictly read-only.
        return self.run_raw((self.adb_path, "devices"))

    def run_target(self, *args: str, require_success: bool = True) -> CommandResult:
        if not self.device_serial:
            raise AndroidDeviceUnavailableError()
        result = self.run_raw((self.adb_path, "-s", self.device_serial, *args))
        if require_success and result.returncode != 0:
            raise AndroidAdbCommandError()
        return result

    def preflight(self) -> None:
        self.check_cancelled()
        inventory = self.run_inventory()
        if inventory.returncode != 0:
            raise AndroidDeviceUnavailableError()

        authorized: list[str] = []
        for raw_line in inventory.stdout.splitlines():
            fields = raw_line.strip().split()
            if len(fields) >= 2 and fields[1].lower() == "device":
                authorized.append(fields[0])

        if self.device_serial:
            if authorized.count(self.device_serial) != 1:
                raise AndroidDeviceUnavailableError()
        else:
            matches: list[str] = []
            for serial in authorized:
                self.device_serial = serial
                manufacturer = self.run_target(
                    "shell", "getprop", "ro.product.manufacturer"
                )
                brand = self.run_target("shell", "getprop", "ro.product.brand")
                # Resolve the model as part of identity preflight but never
                # retain, log or return it.
                self.run_target("shell", "getprop", "ro.product.model")
                identities = {
                    manufacturer.stdout.strip().casefold(),
                    brand.stdout.strip().casefold(),
                }
                if identities.intersection(_XIAOMI_IDENTITIES):
                    matches.append(serial)
            self.device_serial = None
            if len(matches) != 1:
                raise AndroidDeviceUnavailableError(
                    "Exactly one authorized Xiaomi is required for radio reset."
                )
            self.device_serial = matches[0]

        state = self.run_target("get-state")
        if state.stdout.strip().lower() != "device":
            raise AndroidDeviceUnavailableError()

        manufacturer = self.run_target("shell", "getprop", "ro.product.manufacturer")
        brand = self.run_target("shell", "getprop", "ro.product.brand")
        self.run_target("shell", "getprop", "ro.product.model")
        identities = {
            manufacturer.stdout.strip().casefold(),
            brand.stdout.strip().casefold(),
        }
        if not identities.intersection(_XIAOMI_IDENTITIES):
            raise AndroidDeviceUnavailableError(
                "The configured ADB device is not the intended Xiaomi."
            )
        self.emit("device_ready")

    def verify_command_support(self) -> None:
        self.check_cancelled()
        connectivity = self.run_target(
            "shell", "cmd", "connectivity", "help", require_success=False
        )
        phone_data = self.run_target(
            "shell", "cmd", "phone", "data", "help", require_success=False
        )
        connectivity_lines = {
            line.strip().casefold()
            for line in (connectivity.stdout + "\n" + connectivity.stderr).splitlines()
            if line.strip()
        }
        phone_data_lines = {
            line.strip().casefold()
            for line in (phone_data.stdout + "\n" + phone_data.stderr).splitlines()
            if line.strip()
        }
        # Android's ``cmd ... help`` is not consistent about exit status.  In
        # particular, the live Xiaomi build prints the complete, valid help
        # document and exits 255.  Prove support from the service-specific
        # help grammar instead of treating that non-zero status as failure.
        # A generic shell error cannot satisfy all of these anchors.
        connectivity_supported = (
            connectivity.returncode in {0, 255}
            and "connectivity service commands:" in connectivity_lines
            and "airplane-mode [enable|disable]" in connectivity_lines
        )
        phone_supported = (
            phone_data.returncode in {0, 255}
            and "mobile data test mode commands:" in phone_data_lines
            and any(line.startswith("data enable:") for line in phone_data_lines)
            and any(line.startswith("data disable:") for line in phone_data_lines)
        )
        if not connectivity_supported:
            raise AndroidCommandUnsupportedError()
        if not phone_supported:
            raise AndroidCommandUnsupportedError()
        self.emit("commands_verified")

    def read_airplane_enabled(self) -> bool | None:
        result = self.run_target("shell", "settings", "get", "global", "airplane_mode_on")
        value = result.stdout.strip()
        return True if value == "1" else False if value == "0" else None

    def read_mobile_data_enabled(self) -> bool | None:
        result = self.run_target("shell", "settings", "get", "global", "mobile_data")
        value = result.stdout.strip()
        return True if value == "1" else False if value == "0" else None

    def wait_for_state(
        self,
        reader: Callable[[], bool | None],
        expected: bool,
        *,
        cancelled: bool = True,
    ) -> bool:
        deadline = self.monotonic() + self.state_timeout_seconds
        while True:
            if cancelled:
                self.check_cancelled()
            try:
                if reader() is expected:
                    return True
            except AndroidRadioResetError:
                # A transient read failure may recover before the bounded
                # deadline.  Mutating command failures are never retried here.
                pass
            if self.monotonic() >= deadline:
                return False
            delay = min(self.poll_interval_seconds, max(0.0, deadline - self.monotonic()))
            if cancelled:
                self.wait(delay)
            elif delay > 0:
                self.waiter(delay)

    def ensure_safe_initial_state(self) -> None:
        self.check_cancelled()
        airplane = self.read_airplane_enabled()
        data = self.read_mobile_data_enabled()
        if airplane is not False:
            self.run_target("shell", "cmd", "connectivity", "airplane-mode", "disable")
        if data is not True:
            self.run_target("shell", "cmd", "phone", "data", "enable")
        if not self.wait_for_state(self.read_airplane_enabled, False):
            raise AndroidRadioStateError("Initial airplane OFF state could not be verified.")
        if not self.wait_for_state(self.read_mobile_data_enabled, True):
            raise AndroidRadioStateError("Initial mobile-data ON state could not be verified.")
        self.emit("initial_state_verified")

    def restore(self, *, emit_transitions: bool = True) -> bool:
        """Best-effort restore that deliberately ignores cancellation."""
        if emit_transitions:
            # A journal callback failure cannot be allowed to prevent the
            # physical safety restore.  The state readback still determines
            # whether the phone is safe.
            self.transition("restore_pending", best_effort=True)
        for args in (
            ("shell", "cmd", "connectivity", "airplane-mode", "disable"),
            ("shell", "cmd", "phone", "data", "enable"),
        ):
            try:
                self.run_target(*args)
            except AndroidRadioResetError:
                pass
        airplane_ok = self.wait_for_state(
            self.read_airplane_enabled, False, cancelled=False
        )
        data_ok = self.wait_for_state(
            self.read_mobile_data_enabled, True, cancelled=False
        )
        verified = airplane_ok and data_ok
        if verified and emit_transitions:
            self.transition("off_confirmed", best_effort=True)
        try:
            self.emit("restore_verified" if verified else "restore_unverified")
        except AndroidRadioResetError:
            # The physical safe-state readback takes priority while unwinding
            # another failure.  Durable transitions above remain best-effort.
            pass
        return verified


def reset_android_cellular_radio(
    *,
    adb_path: str | Path | None = None,
    device_serial: str | None = None,
    cancel_event: CancelEvent,
    command_runner: CommandRunner | None = None,
    progress: ProgressCallback | None = None,
    on_transition: TransitionCallback | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    waiter: Callable[[float], bool] | None = None,
    command_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    state_timeout_seconds: float = DEFAULT_STATE_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    airplane_hold_seconds: float = DEFAULT_AIRPLANE_HOLD_SECONDS,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
) -> AndroidRadioResetResult:
    """Perform exactly one verified Xiaomi airplane/data reset cycle.

    The function never selects an arbitrary ADB device.  It verifies that the
    configured serial is online and identifies as Xiaomi/Redmi/POCO, verifies
    command availability, normalizes the initial safe state, and performs one
    bounded cycle.  A final OFF/ON restore is attempted regardless of success,
    cancellation, or transition failure.
    """

    serial_text = str(device_serial or "").strip()
    if serial_text and not _SAFE_SERIAL.fullmatch(serial_text):
        raise AndroidRadioConfigurationError()
    if cancel_event is None or not callable(getattr(cancel_event, "is_set", None)):
        raise AndroidRadioConfigurationError()
    if any(
        value <= 0
        for value in (
            command_timeout_seconds,
            state_timeout_seconds,
            poll_interval_seconds,
        )
    ) or airplane_hold_seconds < 0 or settle_seconds < 0:
        raise AndroidRadioConfigurationError()

    effective_waiter = waiter or getattr(cancel_event, "wait", None)
    if not callable(effective_waiter):
        raise AndroidRadioConfigurationError()

    if command_runner is None:
        adb_text = discover_adb_executable(adb_path)
    else:
        # Tests and embedding callers may inject a runner with a logical
        # executable name.  No process is launched by this branch.
        adb_text = str(adb_path or "adb").strip()
        if not adb_text:
            raise AndroidRadioConfigurationError()

    session = _RadioResetSession(
        adb_path=adb_text,
        device_serial=serial_text or None,
        cancel_event=cancel_event,
        command_runner=command_runner or _default_command_runner,
        progress=progress,
        on_transition=on_transition,
        monotonic=monotonic,
        waiter=effective_waiter,
        command_timeout_seconds=float(command_timeout_seconds),
        state_timeout_seconds=float(state_timeout_seconds),
        poll_interval_seconds=float(poll_interval_seconds),
    )

    # Preflight and capability checks are read-only.  Enter the restoration
    # region only after the exact intended device and commands are proven.
    session.preflight()
    session.verify_command_support()

    primary_error: AndroidRadioResetError | None = None
    cycle_verified = False
    restore_verified = False
    try:
        session.ensure_safe_initial_state()
        session.check_cancelled()
        # The durable callback runs before the first mutating ON action.
        session.transition("pending")
        session.run_target("shell", "cmd", "connectivity", "airplane-mode", "enable")
        if not session.wait_for_state(session.read_airplane_enabled, True):
            raise AndroidRadioStateError("Airplane ON transition could not be verified.")
        session.transition("on_confirmed")

        session.wait(float(airplane_hold_seconds))
        session.transition("restore_pending")
        session.run_target("shell", "cmd", "connectivity", "airplane-mode", "disable")
        if not session.wait_for_state(session.read_airplane_enabled, False):
            raise AndroidRadioStateError("Airplane OFF transition could not be verified.")
        session.run_target("shell", "cmd", "phone", "data", "enable")
        if not session.wait_for_state(session.read_mobile_data_enabled, True):
            raise AndroidRadioStateError("Mobile-data ON state could not be verified.")
        session.transition("off_confirmed")
        session.wait(float(settle_seconds))
        # Public-IP verification belongs to the caller.  This transition says
        # it is now safe to begin that verification, not that IP changed.
        session.transition("verify_pending")
        cycle_verified = True
    except AndroidRadioResetError as exc:
        primary_error = exc
    except Exception:
        primary_error = AndroidAdbCommandError()
    finally:
        restore_verified = session.restore(emit_transitions=not cycle_verified)

    if primary_error is not None:
        primary_error.restore_verified = restore_verified
        raise primary_error
    if not restore_verified:
        restore_error = AndroidRadioRestoreError()
        restore_error.restore_verified = False
        raise restore_error

    session.transition("completed")

    return AndroidRadioResetResult(
        cycle_completed=cycle_verified,
        airplane_enabled_verified=cycle_verified,
        airplane_disabled_verified=cycle_verified,
        mobile_data_enabled_verified=cycle_verified,
        restore_verified=True,
        transitions=tuple(session.transitions),
    )


def restore_android_cellular_radio(
    *,
    adb_path: str | Path | None = None,
    device_serial: str | None = None,
    cancel_event: CancelEvent,
    command_runner: CommandRunner | None = None,
    progress: ProgressCallback | None = None,
    on_transition: TransitionCallback | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    waiter: Callable[[float], bool] | None = None,
    command_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    state_timeout_seconds: float = DEFAULT_STATE_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> AndroidRadioRestoreResult:
    """Restore airplane OFF/data ON after a nonfinal durable journal.

    This entry point never enables airplane mode and therefore cannot perform
    a blind second reset cycle during Resume.  It verifies the exact device
    and command support, then performs the same mandatory safe-state restore
    and readbacks used by the full cycle.
    """

    serial_text = str(device_serial or "").strip()
    if serial_text and not _SAFE_SERIAL.fullmatch(serial_text):
        raise AndroidRadioConfigurationError()
    if cancel_event is None or not callable(getattr(cancel_event, "is_set", None)):
        raise AndroidRadioConfigurationError()
    if any(
        value <= 0
        for value in (
            command_timeout_seconds,
            state_timeout_seconds,
            poll_interval_seconds,
        )
    ):
        raise AndroidRadioConfigurationError()
    effective_waiter = waiter or getattr(cancel_event, "wait", None)
    if not callable(effective_waiter):
        raise AndroidRadioConfigurationError()
    if command_runner is None:
        adb_text = discover_adb_executable(adb_path)
    else:
        adb_text = str(adb_path or "adb").strip()
        if not adb_text:
            raise AndroidRadioConfigurationError()

    session = _RadioResetSession(
        adb_path=adb_text,
        device_serial=serial_text or None,
        cancel_event=cancel_event,
        command_runner=command_runner or _default_command_runner,
        progress=progress,
        on_transition=on_transition,
        monotonic=monotonic,
        waiter=effective_waiter,
        command_timeout_seconds=float(command_timeout_seconds),
        state_timeout_seconds=float(state_timeout_seconds),
        poll_interval_seconds=float(poll_interval_seconds),
    )
    session.preflight()
    session.verify_command_support()
    session.check_cancelled()
    if not session.restore():
        restore_error = AndroidRadioRestoreError()
        restore_error.restore_verified = False
        raise restore_error
    session.transition("completed")
    return AndroidRadioRestoreResult(
        airplane_disabled_verified=True,
        mobile_data_enabled_verified=True,
        restore_verified=True,
        transitions=tuple(session.transitions),
    )


__all__ = [
    "AndroidAdbCommandError",
    "AndroidCommandUnsupportedError",
    "AndroidDeviceUnavailableError",
    "AndroidRadioConfigurationError",
    "AndroidRadioResetCancelled",
    "AndroidRadioResetError",
    "AndroidRadioResetResult",
    "AndroidRadioJournalError",
    "AndroidRadioRestoreResult",
    "AndroidRadioRestoreError",
    "AndroidRadioStateError",
    "CommandResult",
    "discover_adb_executable",
    "restore_android_cellular_radio",
    "reset_android_cellular_radio",
]
