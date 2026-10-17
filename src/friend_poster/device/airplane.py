# UTF-8
"""Airplane-mode IP rotation over USB SSH. Dump-first. Never touches Reddit.

Proves the radio is ON first (baseline public IP), then Settings ->
Airplane Mode switch ON ~15s -> OFF -> polls for data back and compares
IPs. Any failure leaves the radio ON: data state is never stranded.
SSH/XXTouch are USB and unaffected by the radio toggle.

  python misc/airplane.py   # one cycle, prints before/after IP
"""

from __future__ import annotations

import os
import sys
import time
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

AIRPLANE_ROW = ("Airplane Mode", "Airplane mode", "Aeroplane Mode")
ON_HOLD_S = 5.0
RECONNECT_BUDGET_S = 120.0
POLL_SLEEP_S = 3.0


def log(msg: str) -> None:
    print(msg, flush=True)


def _public_ip(phone: Phone, timeout: int = 20) -> str:
    """Carrier public IP via on-device HTTPS echo. '' on failure."""
    code, out = phone.exec_ok(
        "curl -s -m %d https://api.ipify.org 2>/dev/null" % timeout,
        timeout=timeout + 10)
    ip = (out or "").strip().split()[0] if (out or "").strip() else ""
    if code != 0 or not ip or len(ip) > 45 or "html" in ip.lower():
        return ""
    return ip


def _sleep_stop(stop, seconds: float) -> bool:
    end = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end:
        if stop is not None and stop.is_set():
            return True
        time.sleep(2.0)
    return bool(stop is not None and stop.is_set())


def cycle_on(phone: Phone, stack: TapStack, lg=log, stop=None,
             ) -> tuple[bool, str]:
    """Radio cycle on a live connection (no connect/close). For callers
    that already hold the phone, e.g. poster batch. Radio ends ON."""
    try:
        if not stack.can_tap():
            return False, "no tap backend"
        ip0 = _public_ip(phone)
        if not ip0:
            return False, "no baseline IP (radio state unknown, refusing)"
        lg("IP before %s" % ip0)
        stack.press_home()
        time.sleep(0.7)
        phone.exec_ok("uiopen --bundleid com.apple.Preferences", timeout=10)
        time.sleep(1.5)
        blob, nodes = dump_named(stack, phone, "air_settings", lg, shot=False)
        row = find_label_exact(nodes, AIRPLANE_ROW)
        for _i in range(3):
            if row is not None:
                break
            # Settings reopens on its last pane (e.g. Hotspot): one
            # bounded back-tap toward the main list, never blind.
            back = None
            for n in nodes or []:
                if n.ntype == "ocr-line" or not n.on_screen():
                    continue
                if collapsed_text(n.labels()) == "<" and n.y < 140 \
                        and n.x < 100:
                    back = n
                    break
            if back is None:
                break
            lg("Settings not on main list, one back-tap")
            stack.tap_xy(*back.center())
            time.sleep(1.2)
            blob, nodes = dump_named(stack, phone, "air_settings_back", lg,
                                     shot=False)
            row = find_label_exact(nodes, AIRPLANE_ROW)
        if row is None:
            # Still stuck (e.g. Crane pane with no back node): kill Settings
            # so it relaunches on the main list, then re-check once.
            lg("Settings stuck off main list, force-restarting it")
            phone.exec_ok("killall Preferences 2>/dev/null; "
                          "killall MobilePreferences 2>/dev/null; true",
                          timeout=10)
            time.sleep(1.5)
            phone.exec_ok("uiopen --bundleid com.apple.Preferences",
                          timeout=10)
            time.sleep(1.5)
            blob, nodes = dump_named(stack, phone, "air_settings_fresh", lg,
                                     shot=False)
            row = find_label_exact(nodes, AIRPLANE_ROW)
        if row is None:
            stack.press_home()
            return False, "Airplane Mode row missing"
        lg("Airplane switch ON")
        stack.tap_switch(row)
        time.sleep(1.0)
        dump_named(stack, phone, "air_on", lg, shot=True)
        if _sleep_stop(stop, ON_HOLD_S):
            lg("Stop during hold, still restoring radio")
        blob, nodes = dump_named(stack, phone, "air_held", lg, shot=False)
        row = find_label_exact(nodes, AIRPLANE_ROW)
        if row is None:
            # Row scrolled off; nudge up once, never blind-tap.
            stack.interp_flick(375, 400, 375, 900, steps=14, ms=22)
            time.sleep(1.0)
            blob, nodes = dump_named(stack, phone, "air_on_top", lg,
                                     shot=False)
            row = find_label_exact(nodes, AIRPLANE_ROW)
        if row is None:
            return False, "Airplane row lost while ON (radio may be ON)"
        lg("Airplane switch OFF")
        stack.tap_switch(row)
        time.sleep(1.5)
        dump_named(stack, phone, "air_off", lg, shot=True)
        lg("OFF tapped, polling data back")
        deadline = time.monotonic() + RECONNECT_BUDGET_S
        ip1 = ""
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                break
            ip1 = _public_ip(phone, timeout=8)
            if ip1:
                break
            time.sleep(POLL_SLEEP_S)
        stack.press_home()
        if not ip1:
            return False, "no data after OFF (radio ON, IP unknown)"
        if ip1 != ip0:
            return True, "%s -> %s" % (ip0, ip1)
        return True, "same IP reissued (%s)" % ip1
    except PhoneError as e:
        return False, "%s" % e
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:100])


def cycle_airplane(password: str, lg=log, stop=None) -> tuple[bool, str]:
    """One OFF->ON->OFF radio cycle (own connection). Radio ends ON."""
    phone = Phone(log=lg)
    try:
        phone.connect(password or "", also_wda=False)
        stack = TapStack(phone, lg)
        lg("BACKEND " + stack.attach())
        return cycle_on(phone, stack, lg, stop)
    except PhoneError as e:
        return False, "%s" % e
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:100])
    finally:
        try:
            phone.close()
        except Exception:
            pass


def main() -> int:
    ok, detail = cycle_airplane("")
    print(("IP_ROTATE_OK " if ok else "IP_ROTATE_FAIL ") + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
