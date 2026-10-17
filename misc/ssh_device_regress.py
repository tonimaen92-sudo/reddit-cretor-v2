# UTF-8
"""Offline regressions for automatic usbmux connection waiting."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ssh_device as device  # noqa: E402


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool) -> None:
    (PASS if condition else FAIL).append(name)
    print(("PASS " if condition else "FAIL ") + name, flush=True)


def forward_lifecycle_regressions() -> None:
    original_which = device._which
    original_port_open = device._port_open
    old_module = sys.modules.get("pymobiledevice3.tcp_forwarder")
    active_ports: set[int] = set()
    constructed: list[object] = []

    class FakeForwarder:
        fail_next = False

        def __init__(self, _serial, _remote_port, local_port, listening_event=None):
            self.local_port = local_port
            self.listening_event = listening_event
            self.stopped = asyncio.Event()
            constructed.append(self)

        async def start(self, address="127.0.0.1"):
            del address
            if FakeForwarder.fail_next:
                FakeForwarder.fail_next = False
                raise OSError("synthetic bind failure")
            active_ports.add(self.local_port)
            if self.listening_event is not None:
                self.listening_event.set()
            try:
                await self.stopped.wait()
            finally:
                active_ports.discard(self.local_port)

        def stop(self):
            self.stopped.set()

    module = types.ModuleType("pymobiledevice3.tcp_forwarder")
    module.UsbmuxTcpForwarder = FakeForwarder
    sys.modules["pymobiledevice3.tcp_forwarder"] = module
    device._which = lambda _name: None
    device._port_open = lambda _host, port, timeout=0.6: port in active_ports
    device._FORWARD_REGISTRY.clear()
    try:
        first = device.PortForward("serial", 2222, 22, None)
        first.start()
        check("forward-starts-listening", 2222 in active_ports and first._thread.is_alive())
        first.stop()
        first.stop()
        check(
            "forward-stop-is-idempotent-and-releases-port",
            2222 not in active_ports
            and not first._thread
            and (device.SSH_LOCAL_HOST, 2222) not in device._FORWARD_REGISTRY,
        )

        second = device.PortForward("serial", 2222, 44, None)
        second.start()
        check("forward-reconnect-reuses-released-port", 2222 in active_ports)

        errors: list[BaseException] = []
        threads = [
            threading.Thread(
                target=lambda: _stop_and_capture(second, errors), daemon=True
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        check(
            "concurrent-forward-stop-is-safe",
            not errors and all(not thread.is_alive() for thread in threads),
        )

        FakeForwarder.fail_next = True
        failed = device.PortForward("serial", 2222, 22, None)
        started = time.monotonic()
        try:
            failed.start()
            failure_text = ""
        except device.PhoneError as exc:
            failure_text = str(exc)
        check(
            "forward-task-failure-is-immediate",
            "synthetic bind failure" in failure_text and time.monotonic() - started < 1.0,
        )
        check(
            "failed-forward-start-releases-reservation",
            (device.SSH_LOCAL_HOST, 2222) not in device._FORWARD_REGISTRY
            and 2222 not in active_ports,
        )

        stale = device.PortForward("serial", 2222, 22, None)
        stale.start()
        replacement = device.PortForward("serial", 2222, 44, None)
        replacement.start()
        check(
            "creator-owned-stale-forward-is-cleaned",
            not stale._thread and replacement._thread is not None and replacement._thread.is_alive(),
        )
        replacement.stop()

        device._port_open = lambda _host, port, timeout=0.6: port == 2999
        external = device.PortForward("serial", 2999, 22, None)
        before_constructed = len(constructed)
        try:
            external.start()
            external_error = ""
        except device.PhoneError as exc:
            external_error = str(exc)
        check(
            "external-listener-fails-without-starting-or-stopping-it",
            "another process" in external_error
            and len(constructed) == before_constructed
            and device._port_open("127.0.0.1", 2999),
        )
    finally:
        for forward in list(device._FORWARD_REGISTRY.values()):
            try:
                forward.stop()
            except Exception:
                pass
        device._FORWARD_REGISTRY.clear()
        device._which = original_which
        device._port_open = original_port_open
        if old_module is None:
            sys.modules.pop("pymobiledevice3.tcp_forwarder", None)
        else:
            sys.modules["pymobiledevice3.tcp_forwarder"] = old_module


def _stop_and_capture(forward, errors: list[BaseException]) -> None:
    try:
        forward.stop()
    except BaseException as exc:
        errors.append(exc)


def ssh_fallback_regression() -> None:
    original_diagnose = device.diagnose_usb
    original_credentials = device.resolve_password_candidates
    attempts: list[int] = []
    stopped: list[bool] = []

    class DummyForward:
        def stop(self):
            stopped.append(True)

    class DummySSH:
        def close(self):
            return None

    phone = device.Phone()

    def fake_connect(_password: str, remote_port: int) -> None:
        attempts.append(remote_port)
        if remote_port == device.SSH_REMOTE_PORT:
            phone.forwards.append(DummyForward())
            raise device.SSHAuthError("synthetic auth rejection")
        check("port-22-forward-stops-before-port-44", stopped == [True])
        phone.ssh = DummySSH()

    try:
        device.diagnose_usb = lambda _log=None: device.UsbReport(
            pnp_iphone=True,
            usbmux_port_open=True,
            usbmux_devices=["connected-phone"],
        )
        device.resolve_password_candidates = lambda _typed: [("saved", "secret")]
        phone._connect_ssh = fake_connect
        phone.connect("", also_wda=False)
        check(
            "ssh-auth-fallback-tries-22-then-44",
            attempts == [device.SSH_REMOTE_PORT, device.SSH_REMOTE_PORT_ALT],
        )
    finally:
        phone.close()
        device.diagnose_usb = original_diagnose
        device.resolve_password_candidates = original_credentials


def main() -> int:
    original_list = device._list_usbmux_device_serials
    original_diagnose = device.diagnose_usb
    original_wait = device.wait_for_usbmux_device
    original_credentials = device.resolve_password_candidates
    try:
        forward_lifecycle_regressions()
        ssh_fallback_regression()

        ready = device.UsbReport(usbmux_devices=["already-ready"])
        calls: list[bool] = []
        device._list_usbmux_device_serials = lambda: calls.append(True) or ([], "")
        result = device.wait_for_usbmux_device(ready, timeout=0.01, poll_interval=0.001)
        check("usbmux-ready-does-not-poll", result is ready and calls == [])

        sequence = iter((([], ""), (["new-phone"], "")))
        device._list_usbmux_device_serials = lambda: next(sequence)
        logs: list[str] = []
        waiting = device.UsbReport(pnp_iphone=True, usbmux_port_open=True)
        result = device.wait_for_usbmux_device(
            waiting,
            logs.append,
            timeout=0.1,
            poll_interval=0.001,
        )
        check("usbmux-appears-during-wait", result.usbmux_devices == ["new-phone"])
        check(
            "usbmux-wait-logs-safe-guidance",
            any("tap Trust This Computer" in line for line in logs)
            and any("continuing automatically" in line for line in logs),
        )

        device._list_usbmux_device_serials = lambda: ([], "")
        missing = device.UsbReport(pnp_iphone=True, usbmux_port_open=True)
        result = device.wait_for_usbmux_device(
            missing,
            timeout=0.01,
            poll_interval=0.001,
        )
        check("usbmux-timeout-remains-missing", result.usbmux_devices == [])

        wait_calls: list[device.UsbReport] = []
        device.diagnose_usb = lambda _log=None: device.UsbReport(
            pnp_iphone=True,
            usbmux_port_open=True,
        )

        def become_ready(report, _log=None):
            wait_calls.append(report)
            report.usbmux_devices = ["connected-phone"]
            return report

        device.wait_for_usbmux_device = become_ready
        device.resolve_password_candidates = lambda _typed: []
        phone = device.Phone()
        try:
            phone.connect("", also_wda=False)
            connect_error = ""
        except device.PhoneError as exc:
            connect_error = str(exc)
        check(
            "phone-connect-waits-before-failing",
            len(wait_calls) == 1
            and phone.serial == "connected-phone"
            and "SSH password is empty" in connect_error,
        )

        device.diagnose_usb = lambda _log=None: device.UsbReport(
            pnp_iphone=True,
            usbmux_port_open=True,
            usbmux_devices=["stock-phone", "jailbroken-phone"],
        )
        device.resolve_password_candidates = lambda _typed: []
        phone = device.Phone(requested_serial="jailbroken-phone")
        try:
            phone.connect("", also_wda=False)
            exact_error = ""
        except device.PhoneError as exc:
            exact_error = str(exc)
        check(
            "phone-exact-serial-beats-first-usbmux-device",
            phone.serial == "jailbroken-phone"
            and "SSH password is empty" in exact_error,
        )

        credential_calls: list[str] = []
        device.resolve_password_candidates = lambda typed: credential_calls.append(typed) or []
        phone = device.Phone(requested_serial="missing-jailbroken-phone")
        try:
            phone.connect("secret", also_wda=False)
            missing_exact_error = ""
        except device.PhoneError as exc:
            missing_exact_error = str(exc)
        check(
            "phone-missing-exact-serial-never-falls-back-or-authenticates",
            phone.serial is None
            and credential_calls == []
            and "jailbroken iPhone X" in missing_exact_error
            and "refusing to use a different iPhone" in missing_exact_error,
        )

        device.wait_for_usbmux_device = lambda report, _log=None: report
        device.diagnose_usb = lambda _log=None: device.UsbReport(
            pnp_iphone=True,
            usbmux_port_open=True,
        )
        phone = device.Phone()
        try:
            phone.connect("", also_wda=False)
            missing_error = ""
        except device.PhoneError as exc:
            missing_error = str(exc)
        check(
            "phone-connect-still-explains-trust-timeout",
            "usbmux sees no lockdown device" in missing_error
            and "automatic wait" in missing_error
            and "Trust This Computer" in missing_error,
        )
    finally:
        device._list_usbmux_device_serials = original_list
        device.diagnose_usb = original_diagnose
        device.wait_for_usbmux_device = original_wait
        device.resolve_password_candidates = original_credentials

    print(f"REGRESS {len(PASS)} pass {len(FAIL)} fail", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
