# UTF-8
"""USB usbmux + port forward + SSH to palera1n. Password never committed."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import paramiko

from friend_poster.core.config import (
    JB_PATH,
    SECRETS_PATH,
    SSH_LOCAL_HOST,
    SSH_LOCAL_PORT,
    SSH_REMOTE_PORT,
    SSH_REMOTE_PORT_ALT,
    SSH_USER,
    SSH_USERS,
    USBMUX_PORT,
    WDA_LOCAL_PORT,
    WDA_REMOTE_PORT,
)

LogFn = Callable[[str], None]
USBMUX_CONNECT_WAIT_SECONDS = 30.0
USBMUX_CONNECT_POLL_SECONDS = 1.0
FORWARD_START_TIMEOUT_SECONDS = 8.0
FORWARD_STOP_TIMEOUT_SECONDS = 5.0


_FORWARD_REGISTRY_LOCK = threading.RLock()
_FORWARD_REGISTRY: dict[tuple[str, int], "PortForward"] = {}


class PhoneError(Exception):
    """Exact missing-piece failure. Message is safe to show in the GUI log."""


class SSHAuthError(PhoneError):
    """An SSH server answered, but rejected the offered credential."""


class BridgeTransportError(PhoneError):
    """USB/usbmux/forward/SSH/XXTouch transport can be reconnected safely."""


_BRIDGE_MARKERS = (
    "usb", "usbmux", "ssh is not connected", "ssh stream", "ssh cmd failed",
    "xxtouch", "openapi", "forward", "connection reset", "broken pipe",
    "channel closed", "channel is not open", "timed out", "timeout", "socket",
)


def is_recoverable_bridge_failure(exc: BaseException) -> bool:
    """Conservatively identify infrastructure failures, never UI ambiguity/auth."""
    if isinstance(exc, BridgeTransportError):
        return True
    if isinstance(exc, SSHAuthError):
        return False
    # Paramiko can surface a channel teardown directly instead of through the
    # Phone wrapper.  AuthenticationException is also an SSHException, so it
    # must be excluded before accepting the remaining transport exceptions.
    if isinstance(exc, paramiko.AuthenticationException):
        return False
    if isinstance(exc, paramiko.SSHException):
        return True
    message = str(exc).casefold()
    unsafe = (
        "password", "credential", "auth failed", "malformed", "unproven",
        "ambiguous", "did not guess", "invalid",
    )
    return not any(word in message for word in unsafe) and any(
        word in message for word in _BRIDGE_MARKERS
    )


@dataclass
class UsbReport:
    pnp_iphone: bool = False
    pnp_names: list[str] = field(default_factory=list)
    usbmux_port_open: bool = False
    usbmux_devices: list[str] = field(default_factory=list)
    usbmux_error: str = ""
    apple_service: str = ""
    store_itunes: bool = False
    desktop_itunes: bool = False
    amds_exe: bool = False


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        log(msg)


def load_secrets() -> dict:
    if not os.path.isfile(SECRETS_PATH):
        return {}
    with open(SECRETS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    return data


def update_secrets(**kwargs) -> dict:
    payload = load_secrets()
    for key, value in kwargs.items():
        if value is None:
            continue
        payload[key] = value
    with open(SECRETS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return payload


def save_secrets(password: str, user: str = SSH_USER) -> None:
    update_secrets(ssh_user=user, ssh_password=password)


def resolve_password(typed: str) -> str:
    candidates = resolve_password_candidates(typed)
    return candidates[0][1] if candidates else ""


def resolve_password_candidates(typed: str) -> list[tuple[str, str]]:
    """Return unique SSH credentials in priority order without logging their values."""
    configured = (
        ("entered", (typed or "").strip()),
        ("environment", (os.environ.get("IPHONE_SSH_PASSWORD") or "").strip()),
        ("saved", str(load_secrets().get("ssh_password") or "").strip()),
    )
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, value in configured:
        if value and value not in seen:
            result.append((source, value))
            seen.add(value)
    return result


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _list_usbmux_device_serials() -> tuple[list[str], str]:
    """Return currently visible usbmux serials and a redacted backend error."""
    try:
        from pymobiledevice3 import usbmux

        devices = _run_maybe_async(usbmux.list_devices)
        serials = []
        for dev in devices or []:
            serial = getattr(dev, "serial", None) or str(dev)
            serials.append(str(serial))
        return serials, ""
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def diagnose_usb(log: LogFn | None = None) -> UsbReport:
    report = UsbReport()
    report.usbmux_port_open = _port_open("127.0.0.1", USBMUX_PORT)
    report.store_itunes = os.path.isfile(
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\iTunes.exe")
    )
    report.desktop_itunes = os.path.isdir(r"C:\Program Files\iTunes") or os.path.isdir(
        r"C:\Program Files (x86)\iTunes"
    )
    report.amds_exe = os.path.isfile(
        r"C:\Program Files\Common Files\Apple\Mobile Device Support\AppleMobileDeviceService.exe"
    ) or os.path.isfile(
        r"C:\Program Files (x86)\Common Files\Apple\Mobile Device Support\AppleMobileDeviceService.exe"
    )
    try:
        raw = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-PnpDevice -PresentOnly | "
                "Where-Object { $_.FriendlyName -match 'iPhone|Apple Mobile|Apple Device' } | "
                "ForEach-Object { $_.Status + '|' + $_.Class + '|' + $_.FriendlyName }",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            report.pnp_names.append(line)
            low = line.lower()
            if "iphone" in low or "apple mobile device" in low:
                report.pnp_iphone = True
    except (subprocess.SubprocessError, OSError) as e:
        _log(log, f"PnP query failed: {e}")

    try:
        svc = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Service | Where-Object { $_.Name -match 'Apple|MobileDevice' "
                "-or $_.DisplayName -match 'Apple Mobile|iTunes' } | "
                "ForEach-Object { $_.Status.ToString() + ' ' + $_.Name + ' ' + $_.DisplayName }",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        ).strip()
        report.apple_service = svc or "(none)"
    except (subprocess.SubprocessError, OSError):
        report.apple_service = "(query failed)"

    report.usbmux_devices, report.usbmux_error = _list_usbmux_device_serials()
    return report


def _run_maybe_async(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            raise PhoneError("internal: async usbmux called from a running loop")
        return asyncio.run(result)
    return result


def wait_for_usbmux_device(
    report: UsbReport,
    log: LogFn | None = None,
    *,
    timeout: float = USBMUX_CONNECT_WAIT_SECONDS,
    poll_interval: float = USBMUX_CONNECT_POLL_SECONDS,
) -> UsbReport:
    """Wait briefly for Apple pairing/usbmux to expose a newly connected phone."""
    if report.usbmux_devices or timeout <= 0:
        return report
    timeout = max(0.0, float(timeout))
    poll_interval = max(0.05, float(poll_interval))
    _log(
        log,
        f"Waiting up to {timeout:g}s for Apple usbmux to expose the iPhone. "
        "Keep it unlocked and tap Trust This Computer if prompted.",
    )
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return report
        time.sleep(min(poll_interval, remaining))
        devices, error = _list_usbmux_device_serials()
        report.usbmux_devices = devices
        report.usbmux_error = error
        if devices:
            _log(log, "Apple usbmux detected the iPhone; continuing automatically.")
            return report


def usb_status_line(report: UsbReport) -> str:
    pnp = "yes" if report.pnp_iphone else "no"
    mux = "yes" if report.usbmux_devices else "no"
    svc = "up" if report.usbmux_port_open else "down"
    return f"USB iPhone: {pnp} | usbmux devices: {mux} | usbmux :{USBMUX_PORT}: {svc}"


def explain_usb_failure(report: UsbReport) -> str:
    if report.usbmux_devices:
        return ""
    bits = [usb_status_line(report)]
    if report.pnp_names:
        bits.append("PnP: " + "; ".join(report.pnp_names))
    else:
        bits.append("PnP did not list an iPhone. Plug the USB cable into this PC, unlock, tap Trust.")
    bits.append(f"Apple pairing service: {report.apple_service}")
    bits.append(
        f"iTunes: store={'yes' if report.store_itunes else 'no'} "
        f"desktop={'yes' if report.desktop_itunes else 'no'} "
        f"AppleMobileDeviceService.exe={'yes' if report.amds_exe else 'no'}"
    )
    if report.usbmux_error:
        bits.append(f"usbmux error: {report.usbmux_error}")
    if report.pnp_iphone and not report.usbmux_port_open:
        if report.store_itunes and not report.amds_exe:
            bits.append(
                "Missing piece: Microsoft Store iTunes is installed, but Apple Mobile Device "
                "Support is not. Store iTunes is a music app; it does not start usbmux on "
                f"127.0.0.1:{USBMUX_PORT}. Install the Apple Devices app from the Microsoft Store "
                "(or iTunes 64-bit from apple.com/itunes/download/win64), reboot or reconnect USB, "
                "unlock the iPhone, tap Trust. Keep Store iTunes if you want; we need the pairing service."
            )
        else:
            bits.append(
                "Missing piece: Apple usbmux / Mobile Device driver. The phone shows as a USB "
                "media device, but lockdown (needed for iproxy/SSH forward) is not listening on "
                f"127.0.0.1:{USBMUX_PORT}. Start Apple Mobile Device Service, or install Apple Devices "
                "/ desktop iTunes, then reconnect and tap Trust."
            )
    elif not report.pnp_iphone:
        bits.append(
            "Missing piece: phone not visible over USB. Check the cable (data, not charge-only), "
            "the Apple driver, and that palera1n is not stuck in DFU."
        )
    else:
        bits.append(
            "Missing piece: usbmux sees no lockdown device after the automatic wait. Unlock the iPhone "
            "and tap Trust This Computer if prompted. If it is already trusted, unplug and reconnect the "
            "USB cable; Apple Mobile Device Service can be running before it publishes the phone."
        )
    return "\n".join(bits)


def _which(name: str) -> str:
    from shutil import which

    return which(name) or ""


class PortForward:
    """Keep 127.0.0.1:local -> device:remote up for the session."""

    def __init__(self, serial: str | None, local_port: int, remote_port: int, log: LogFn | None):
        self.serial = serial
        self.local_port = local_port
        self.remote_port = remote_port
        self.log = log
        self.proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._forwarder = None
        self._task: asyncio.Task | None = None
        self._thread_done = threading.Event()
        self._stop_lock = threading.Lock()
        self._claimed = False
        self.backend = ""

    def start(self) -> None:
        self._claim_local_port()
        try:
            iproxy = _which("iproxy")
            if iproxy:
                self._start_iproxy(iproxy)
                return
            self._start_pymobiledevice3()
        except BaseException:
            try:
                self.stop()
            except Exception as cleanup_exc:
                _log(self.log, f"USB forward startup cleanup warning: {cleanup_exc}")
            raise

    def _claim_local_port(self) -> None:
        """Own one local listener without killing unrelated processes."""
        key = (SSH_LOCAL_HOST, self.local_port)
        with _FORWARD_REGISTRY_LOCK:
            previous = _FORWARD_REGISTRY.get(key)
        if previous is not None and previous is not self:
            _log(self.log, f"Cleaning stale Creator USB forward on {key[0]}:{key[1]}.")
            previous.stop()
        if _port_open(*key, timeout=0.2):
            raise PhoneError(
                f"Local USB forward port {key[0]}:{key[1]} is already in use by another "
                "process. Close the other Creator/iproxy window, then retry. No process was stopped."
            )
        with _FORWARD_REGISTRY_LOCK:
            current = _FORWARD_REGISTRY.get(key)
            if current is not None and current is not self:
                raise PhoneError(
                    f"Local USB forward port {key[0]}:{key[1]} is already reserved by Creator."
                )
            _FORWARD_REGISTRY[key] = self
            self._claimed = True

    def _release_local_port(self) -> None:
        key = (SSH_LOCAL_HOST, self.local_port)
        with _FORWARD_REGISTRY_LOCK:
            if _FORWARD_REGISTRY.get(key) is self:
                _FORWARD_REGISTRY.pop(key, None)
            self._claimed = False

    def _start_iproxy(self, iproxy: str) -> None:
        cmd = [iproxy, str(self.local_port), str(self.remote_port)]
        _log(self.log, f"Starting iproxy: {cmd[0]} {self.local_port}->{self.remote_port}")
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.backend = "iproxy"
        self._wait_listen("iproxy")

    def _start_pymobiledevice3(self) -> None:
        from pymobiledevice3.tcp_forwarder import UsbmuxTcpForwarder

        class CreatorUsbmuxTcpForwarder(UsbmuxTcpForwarder):
            async def _handle_server_connection(self, local_reader, local_writer):
                try:
                    await super()._handle_server_connection(local_reader, local_writer)
                except (ConnectionResetError, BrokenPipeError) as exc:
                    # Short-lived banner/preflight clients intentionally close
                    # after reading enough data. Windows may report that normal
                    # disconnect while wait_closed() is completing.
                    if getattr(exc, "winerror", None) not in (None, 64, 10054):
                        raise

        _log(
            self.log,
            f"Starting pymobiledevice3 usbmux forward {self.local_port}->{self.remote_port} "
            "(iproxy.exe was not on PATH).",
        )
        ready = threading.Event()
        error: list[BaseException] = []
        self._thread_done.clear()

        def runner() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)

            async def main() -> None:
                listening = asyncio.Event()
                fwd = CreatorUsbmuxTcpForwarder(
                    self.serial,
                    self.remote_port,
                    self.local_port,
                    listening_event=listening,
                )
                self._forwarder = fwd
                task = asyncio.create_task(fwd.start(address=SSH_LOCAL_HOST))
                self._task = task
                listening_task = asyncio.create_task(listening.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {listening_task, task},
                        timeout=FORWARD_START_TIMEOUT_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if listening_task in done and listening_task.result():
                        ready.set()
                        await task
                        return
                    if task in done:
                        await task
                        raise RuntimeError("usbmux forward exited before listening")
                    raise TimeoutError("usbmux forward did not start listening in time")
                except asyncio.CancelledError:
                    if not ready.is_set():
                        error.append(RuntimeError("usbmux forward was cancelled before listening"))
                    raise
                except BaseException as exc:
                    if not ready.is_set():
                        error.append(exc)
                    raise
                finally:
                    ready.set()
                    if not listening_task.done():
                        listening_task.cancel()
                    await asyncio.gather(listening_task, return_exceptions=True)
                    if not task.done():
                        fwd.stop()
                        try:
                            await asyncio.wait_for(task, timeout=FORWARD_STOP_TIMEOUT_SECONDS)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)

            try:
                loop.run_until_complete(main())
            except BaseException as exc:
                if not error:
                    error.append(exc)
                ready.set()
            finally:
                self._task = None
                self._forwarder = None
                loop.close()
                self._loop = None
                self._thread_done.set()

        self._thread = threading.Thread(target=runner, name=f"fwd-{self.local_port}", daemon=True)
        self._thread.start()
        if not ready.wait(12):
            raise PhoneError(
                f"Missing piece: usbmux forward {self.local_port}->{self.remote_port} did not start listening."
            )
        if error:
            exc = error[0]
            if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10048:
                detail = "local port is already in use"
            else:
                detail = f"{type(exc).__name__}: {exc}"
            raise PhoneError(
                f"Missing piece: pymobiledevice3 forward {self.local_port}->{self.remote_port} failed: "
                f"{detail}"
            )
        self.backend = "pymobiledevice3"
        # UsbmuxTcpForwarder raised its listening event only after
        # asyncio.start_server succeeded. Do not probe the port with a throwaway
        # TCP connection: that creates a real forwarded device connection and
        # can produce a harmless but noisy Windows connection-reset traceback.
        _log(
            self.log,
            f"Forward ready: {SSH_LOCAL_HOST}:{self.local_port} -> "
            f"device:{self.remote_port} (pymobiledevice3)",
        )

    def _wait_listen(self, who: str) -> None:
        deadline = time.time() + 8
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                out = ""
                try:
                    out = (self.proc.stdout.read() or "")[:500]
                except Exception:
                    pass
                raise PhoneError(f"Missing piece: {who} exited before listening. {out}".strip())
            if _port_open(SSH_LOCAL_HOST, self.local_port):
                _log(self.log, f"Forward ready: {SSH_LOCAL_HOST}:{self.local_port} -> device:{self.remote_port} ({who})")
                return
            time.sleep(0.2)
        raise PhoneError(
            f"Missing piece: {who} did not open {SSH_LOCAL_HOST}:{self.local_port}. "
            "Wrong port, driver, or the jailbreak SSH daemon is not bound on the device."
        )

    def stop(self) -> None:
        with self._stop_lock:
            forwarder = self._forwarder
            loop = self._loop
            thread = self._thread
            owns_resources = bool(self._claimed or forwarder or thread or self.proc)
            if not owns_resources:
                return
            if forwarder is not None:
                try:
                    if loop is not None and loop.is_running():
                        loop.call_soon_threadsafe(forwarder.stop)
                    else:
                        forwarder.stop()
                except (RuntimeError, OSError):
                    pass
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=FORWARD_STOP_TIMEOUT_SECONDS)
                if thread.is_alive() and loop is not None and loop.is_running():
                    task = self._task
                    if task is not None:
                        loop.call_soon_threadsafe(task.cancel)
                    thread.join(timeout=1.0)
            self.proc = None
            deadline = time.monotonic() + FORWARD_STOP_TIMEOUT_SECONDS
            while time.monotonic() < deadline and _port_open(
                SSH_LOCAL_HOST, self.local_port, timeout=0.2
            ):
                time.sleep(0.1)
            still_listening = _port_open(SSH_LOCAL_HOST, self.local_port, timeout=0.2)
            thread_alive = bool(thread is not None and thread.is_alive())
            if not thread_alive and not still_listening:
                self._thread = None
                self.backend = ""
                self._release_local_port()
                return
            problem = []
            if thread_alive:
                problem.append("forward thread did not exit")
            if still_listening:
                problem.append(f"{SSH_LOCAL_HOST}:{self.local_port} is still listening")
            raise PhoneError("USB forward cleanup failed: " + " and ".join(problem) + ".")


class Phone:
    def __init__(self, log: LogFn | None = None, requested_serial: str | None = None):
        self.log = log or (lambda _m: None)
        self.requested_serial = str(requested_serial or "").strip()
        self.usb = UsbReport()
        self.serial: str | None = None
        self.ssh: paramiko.SSHClient | None = None
        self.forwards: list[PortForward] = []
        self.ssh_remote_port = SSH_REMOTE_PORT
        self.ssh_user = ""
        self.password_used = False

    def connect(self, password: str, also_wda: bool = True) -> None:
        self.usb = diagnose_usb(self.log)
        self.log(usb_status_line(self.usb))
        if self.usb.pnp_names:
            self.log("PnP: " + "; ".join(self.usb.pnp_names))
        if not self.usb.usbmux_devices:
            self.usb = wait_for_usbmux_device(self.usb, self.log)
            if self.usb.usbmux_devices:
                self.log(usb_status_line(self.usb))
        if not self.usb.usbmux_devices:
            raise PhoneError(explain_usb_failure(self.usb))
        if self.requested_serial:
            if self.requested_serial not in self.usb.usbmux_devices:
                suffix = self.requested_serial[-4:] if len(self.requested_serial) > 4 else self.requested_serial
                raise PhoneError(
                    f"The required jailbroken iPhone X ({suffix}) is disconnected from USB; "
                    "refusing to use a different iPhone."
                )
            self.serial = self.requested_serial
        else:
            self.serial = self.usb.usbmux_devices[0]
        self.log(f"usbmux serial: {self.serial}")

        credentials = resolve_password_candidates(password)
        if not credentials:
            raise PhoneError(
                "Missing piece: SSH password is empty. Type the palera1n root password in the GUI "
                "(not stored in git). Optional: gitignored secrets.json or env IPHONE_SSH_PASSWORD."
            )

        last_err = None
        auth_failed_ports: set[int] = set()
        for remote in (SSH_REMOTE_PORT, SSH_REMOTE_PORT_ALT):
            for index, (source, pwd) in enumerate(credentials):
                try:
                    self._connect_ssh(pwd, remote)
                    self.ssh_remote_port = remote
                    last_err = None
                    break
                except SSHAuthError as e:
                    last_err = e
                    auth_failed_ports.add(remote)
                    self.log(str(e))
                    self._stop_forwards()
                    if index + 1 < len(credentials):
                        next_source = credentials[index + 1][0]
                        self.log(
                            f"SSH credential from {source} was rejected on device port {remote}; "
                            f"trying the {next_source} credential."
                        )
                except PhoneError as e:
                    last_err = e
                    self.log(str(e))
                    self._stop_forwards()
                    break
            if self.ssh is not None:
                break
        if last_err and self.ssh is None:
            if auth_failed_ports == {SSH_REMOTE_PORT, SSH_REMOTE_PORT_ALT}:
                raise PhoneError(
                    "Missing piece: OpenSSH (device port 22) and Dropbear (device port 44) are "
                    "installed and reachable, but rejected every available SSH credential. "
                    "Do not reinstall OpenSSH. Clear the GUI SSH password field to use the saved "
                    "credential, or enter the current palera1n/iPhone SSH password."
                )
            raise last_err

        if also_wda:
            try:
                self._forward(WDA_LOCAL_PORT, WDA_REMOTE_PORT)
            except PhoneError as e:
                self.log(f"WDA forward skipped: {e}")

        if password.strip() and not load_secrets().get("ssh_password"):
            save_secrets(pwd)
            self.log("Wrote gitignored secrets.json after a successful SSH login.")

    def _forward(self, local_port: int, remote_port: int) -> PortForward:
        fwd = PortForward(self.serial, local_port, remote_port, self.log)
        self.forwards.append(fwd)
        try:
            fwd.start()
        except BaseException:
            try:
                fwd.stop()
            except Exception as cleanup_exc:
                self.log(f"USB forward cleanup warning: {cleanup_exc}")
            if fwd in self.forwards:
                self.forwards.remove(fwd)
            raise
        return fwd

    def _connect_ssh(self, password: str, remote_port: int) -> None:
        self._forward(SSH_LOCAL_PORT, remote_port)
        banner = _peek_ssh_banner(SSH_LOCAL_HOST, SSH_LOCAL_PORT)
        self.log(f"SSH banner device:{remote_port}: {banner or '(empty / not SSH)'}")
        if remote_port == SSH_REMOTE_PORT_ALT and not banner.startswith("SSH-"):
            raise PhoneError(
                f"Missing piece: device port {remote_port} is open through usbmux but did not send an "
                f"SSH banner ({banner!r}). OpenSSH is not running there."
            )

        last_auth = None
        for user in SSH_USERS:
            try:
                client = _paramiko_login(user, password)
            except paramiko.AuthenticationException as e:
                last_auth = e
                allowed = getattr(e, "allowed_types", None)
                extra = f" allowed={allowed}" if allowed else ""
                self.log(f"auth failed for {user} on device port {remote_port} ({type(e).__name__}{extra})")
                continue
            except (paramiko.SSHException, OSError, socket.timeout) as e:
                raise PhoneError(
                    f"Missing piece: SSH {user}@127.0.0.1:{SSH_LOCAL_PORT} -> device:{remote_port} "
                    f"failed ({type(e).__name__}: {e}). "
                    + (
                        "Jailbreak userspace SSH is not accepting connections on device port 44. "
                        "If the phone rebooted, re-run palera1n. Unlock the phone."
                        if remote_port == SSH_REMOTE_PORT
                        else f"Device port {remote_port} also failed."
                    )
                ) from e
            self.ssh = client
            self.ssh_user = user
            self.password_used = True
            who = self.exec("echo OK; uname -a; id; echo PATH=$PATH", timeout=15)
            self.log(f"SSH up as {user} via device port {remote_port}.")
            self.log(who.strip())
            save_secrets(password, user=user)
            return
        raise SSHAuthError(
            "Missing piece: SSH auth failed for "
            f"{' and '.join(SSH_USERS)}@127.0.0.1:{SSH_LOCAL_PORT} (device port {remote_port}). "
            f"{_auth_hint(last_auth, remote_port)}"
        )

    def exec(self, command: str, timeout: int = 30) -> str:
        if self.ssh is None:
            raise PhoneError("SSH is not connected.")
        wrapped = f"export PATH={JB_PATH}; export LANG=C.UTF-8; {command}"
        stdin, stdout, stderr = self.ssh.exec_command(wrapped, timeout=timeout)
        stdin.close()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        if code != 0 and not out:
            raise PhoneError(f"SSH cmd failed ({code}): {command}\n{err.strip()}")
        if err.strip() and code != 0:
            return (out + "\n" + err).strip() + "\n"
        return out

    def exec_input(
        self, command: str, data: bytes | str, timeout: int = 30
    ) -> tuple[int, str]:
        """Run a command with private data on stdin, never in its command line."""
        if self.ssh is None:
            raise PhoneError("SSH is not connected.")
        raw = data.encode("utf-8") if isinstance(data, str) else data
        wrapped = f"export PATH={JB_PATH}; export LANG=C.UTF-8; {command}"
        try:
            stdin, stdout, stderr = self.ssh.exec_command(wrapped, timeout=timeout)
            stdin.write(raw)
            stdin.flush()
            stdin.channel.shutdown_write()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            stdin.close()
        except (socket.timeout, TimeoutError) as exc:
            return 124, f"(timeout {timeout}s: {exc})\n"
        text = out
        if err.strip():
            text = (out + "\n" + err).strip() + "\n"
        return code, text

    def sftp_write_bytes(self, path: str, data: bytes) -> None:
        if self.ssh is None:
            raise PhoneError("SSH is not connected.")
        if isinstance(data, str):
            data = data.encode("utf-8")
        try:
            sftp = self.ssh.open_sftp()
            try:
                folder = path.rsplit("/", 1)[0]
                try:
                    sftp.stat(folder)
                except OSError:
                    self.exec_ok(f"mkdir -p {shlex.quote(folder)}", timeout=10)
                with sftp.file(path, "wb") as fh:
                    fh.write(data)
                return
            finally:
                sftp.close()
        except Exception as e:
            self.log(f"SFTP write unavailable ({type(e).__name__}); using SSH stream.")

        folder = path.rsplit("/", 1)[0]
        code, out = self.exec_ok(f"mkdir -p {shlex.quote(folder)}", timeout=10)
        if code != 0:
            raise PhoneError(f"SSH stream mkdir failed: {out.strip()}")
        command = (
            f"export PATH={JB_PATH}; export LANG=C.UTF-8; "
            f"cat > {shlex.quote(path)}"
        )
        stdin, stdout, stderr = self.ssh.exec_command(command, timeout=60)
        stdin.write(data)
        stdin.flush()
        stdin.channel.shutdown_write()
        out_b = stdout.read()
        err_b = stderr.read()
        status = stdout.channel.recv_exit_status()
        stdin.close()
        if status != 0:
            detail = (out_b + b"\n" + err_b).decode("utf-8", errors="replace").strip()
            raise PhoneError(f"SSH stream write failed ({status}): {detail}")

    def sftp_write(self, path: str, data: str) -> None:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        self.sftp_write_bytes(path, raw)

    def sftp_read(self, path: str) -> str:
        raw = self.sftp_read_bytes(path)
        return raw.decode("utf-8", errors="replace")

    def sftp_read_bytes(self, path: str) -> bytes:
        if self.ssh is None:
            raise PhoneError("SSH is not connected.")
        try:
            sftp = self.ssh.open_sftp()
            try:
                with sftp.file(path, "rb") as fh:
                    raw = fh.read()
                if isinstance(raw, str):
                    return raw.encode("utf-8")
                return raw
            finally:
                sftp.close()
        except Exception as e:
            self.log(f"SFTP read unavailable ({type(e).__name__}); using SSH stream.")

        command = (
            f"export PATH={JB_PATH}; export LANG=C.UTF-8; "
            f"cat {shlex.quote(path)}"
        )
        stdin, stdout, stderr = self.ssh.exec_command(command, timeout=60)
        stdin.close()
        raw = stdout.read()
        err = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
        if status != 0:
            raise PhoneError(f"SSH stream read failed ({status}): {err.strip()}")
        return raw

    def exec_ok(self, command: str, timeout: int = 30) -> tuple[int, str]:
        if self.ssh is None:
            raise PhoneError("SSH is not connected.")
        wrapped = f"export PATH={JB_PATH}; export LANG=C.UTF-8; {command}"
        try:
            stdin, stdout, stderr = self.ssh.exec_command(wrapped, timeout=timeout)
            stdin.close()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
        except (socket.timeout, TimeoutError) as e:
            return 124, f"(timeout {timeout}s: {e})\n"
        text = out
        if err.strip():
            text = (out + "\n" + err).strip() + "\n"
        return code, text

    def _stop_forwards(self) -> None:
        cleanup_errors: list[str] = []
        for fwd in reversed(self.forwards):
            try:
                fwd.stop()
            except Exception as exc:
                cleanup_errors.append(str(exc))
        self.forwards.clear()
        if cleanup_errors:
            message = "USB forward cleanup failed: " + "; ".join(cleanup_errors)
            self.log(message)
            raise PhoneError(message)

    def close(self) -> None:
        if self.ssh is not None:
            try:
                self.ssh.close()
            except Exception:
                pass
            self.ssh = None
        self._stop_forwards()


def _peek_ssh_banner(host: str, port: int, timeout: float = 3.0) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            data = sock.recv(256)
        return data.decode("utf-8", errors="replace").strip().splitlines()[0][:120] if data else ""
    except OSError:
        return ""


def _auth_hint(exc: BaseException | None, remote_port: int | None = None) -> str:
    allowed = list(getattr(exc, "allowed_types", None) or [])
    name = type(exc).__name__ if exc else "AuthenticationException"
    if name == "BadAuthenticationType" or (allowed and "password" not in allowed):
        kinds = ",".join(allowed) if allowed else "unknown"
        return (
            f"{name} allowed_types={kinds}. palera1n Dropbear on USB port 44 is not offering "
            "password login (usual after Sileo bootstrap). On the phone, open Sileo, install "
            "OpenSSH, keep the phone unlocked, then Start again — we will use device port 22. "
            "Do not paste the password here."
        )
    server = "OpenSSH" if remote_port == SSH_REMOTE_PORT else "Dropbear"
    return (
        f"{name}. {server} is installed and reachable, but rejected this credential. "
        "Use the current palera1n/iPhone SSH password; reinstalling OpenSSH will not fix a "
        "rejected password."
    )


def _paramiko_login(user: str, password: str) -> paramiko.SSHClient:
    # Try password first. Do not send auth_none — Dropbear MaxAuthTries is tiny and
    # none-then-password fails even with the right password.
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            SSH_LOCAL_HOST,
            port=SSH_LOCAL_PORT,
            username=user,
            password=password,
            timeout=12,
            allow_agent=False,
            look_for_keys=False,
            banner_timeout=12,
            auth_timeout=12,
        )
        return client
    except paramiko.BadAuthenticationType as e:
        client.close()
        allowed = list(e.allowed_types or [])
        if "keyboard-interactive" in allowed:
            sock = socket.create_connection((SSH_LOCAL_HOST, SSH_LOCAL_PORT), 12)
            transport = paramiko.Transport(sock)
            try:
                transport.start_client(timeout=12)
                transport.auth_interactive(
                    user, lambda _t, _i, prompts: [password] * len(prompts)
                )
                return _client_from_transport(transport)
            except Exception:
                transport.close()
                raise
        if "publickey" in allowed:
            key_client = paramiko.SSHClient()
            key_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            key_client.connect(
                SSH_LOCAL_HOST,
                port=SSH_LOCAL_PORT,
                username=user,
                timeout=12,
                allow_agent=True,
                look_for_keys=True,
                banner_timeout=12,
                auth_timeout=12,
            )
            return key_client
        raise
    except paramiko.AuthenticationException:
        client.close()
        raise


def _client_from_transport(transport: paramiko.Transport) -> paramiko.SSHClient:
    sess = paramiko.SSHClient()
    sess.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sess._transport = transport
    return sess


def which_python() -> str:
    return sys.executable
