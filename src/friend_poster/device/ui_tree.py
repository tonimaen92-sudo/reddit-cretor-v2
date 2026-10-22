# UTF-8
"""Live UI dump + tap. Dump first; never reuse last run's coordinates."""

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable

from friend_poster.core.config import (
    DUMP_DIR,
    HME_SETTINGS_URL,
    REDDIT_BUNDLE,
    SETTINGS_BUNDLE,
    WDA_LOCAL_PORT,
    XXT_BUNDLES,
    XXT_LOCAL_PORT,
    XXT_LOG,
    XXT_REMOTE_PORT,
    XXT_SCRIPTS,
)
from friend_poster.device.ssh_device import Phone, PhoneError

LogFn = Callable[[str], None]

# Creator enables detailed screen-dump previews so every permitted labeled node
# from an *already captured* frame is shown, not just the first 12.  It defaults
# to off so CLI tools and other apps keep their existing output.
_PREVIEW_DETAIL = False


def set_preview_detail(enabled: bool) -> None:
    """Enable or disable the all-nodes frame preview (Creator detailed mode)."""
    global _PREVIEW_DETAIL
    _PREVIEW_DETAIL = bool(enabled)


def preview_detail_enabled() -> bool:
    return _PREVIEW_DETAIL


class OperationCancelled(PhoneError):
    """Raised when Creator Stop cancels a pending phone operation."""


@dataclass
class Node:
    ntype: str = ""
    name: str = ""
    label: str = ""
    value: str = ""
    text: str = ""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    visible: bool = True
    enabled: bool = True
    raw: str = ""

    def labels(self) -> str:
        return " ".join(p for p in (self.name, self.label, self.value, self.text) if p)

    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    def on_screen(self, sw: int = 750, sh: int = 1334) -> bool:
        if self.w <= 2 or self.h <= 2:
            return False
        cx, cy = self.center()
        return 0 <= cx <= sw and 0 <= cy <= sh


def _wda_client():
    import wda

    return wda.Client(f"http://127.0.0.1:{WDA_LOCAL_PORT}")


class TapStack:
    def __init__(self, phone: Phone, log: LogFn, cancel_event=None):
        self.phone = phone
        self.log = log
        self.cancel_event = cancel_event
        self.backend = ""
        self.wda = None
        self.session = None
        # XXTouch remains the mutation backend, while WDA (when installed) can
        # simultaneously provide a real XCUITest hierarchy for verification.
        self.wda_observer = None
        self.wda_observer_session = None
        self.screen_w = 750
        self.screen_h = 1334
        self._xxt_size_ok = False
        self.xxt_bin = ""
        self.xxt_scripts = XXT_SCRIPTS

    def check_cancelled(self) -> None:
        is_set = getattr(self.cancel_event, "is_set", None)
        if callable(is_set) and is_set():
            raise OperationCancelled("Stopped by user.")

    def wait(self, seconds: float) -> None:
        """Sleep unless Stop is requested; wake immediately when it is."""
        self.check_cancelled()
        waiter = getattr(self.cancel_event, "wait", None)
        if callable(waiter):
            if waiter(max(0.0, float(seconds))):
                raise OperationCancelled("Stopped by user.")
        else:
            time.sleep(max(0.0, float(seconds)))
        self.check_cancelled()

    def attach(self) -> str:
        self.check_cancelled()
        if self._try_xxtouch():
            self._try_wda_observer()
            return self.backend
        if self._try_wda():
            return self.backend
        if self._try_zxtouch_port():
            return self.backend
        self.backend = "ssh-open-only"
        self.log(
            "Tap stack: no XXTouch / WDA / ZXTouch. Can uiopen Settings. "
            "Open XXTouch Elite once on the phone, or install WDA. Did not guess taps."
        )
        return self.backend

    def _try_wda_observer(self) -> bool:
        """Attach a read-only WDA/XPath observer without replacing XXTouch taps."""
        self.check_cancelled()
        try:
            import urllib.request

            with urllib.request.urlopen(
                f"http://127.0.0.1:{WDA_LOCAL_PORT}/status", timeout=3
            ) as response:
                body = response.read().decode("utf-8", errors="replace")
            if not any(token in body.lower() for token in ("sessionid", "value", "ready")):
                return False
            client = _wda_client()
            try:
                session = client.session()
            except Exception:
                session = client
            self.wda_observer = client
            self.wda_observer_session = session
            self.log(
                f"UI observer: WebDriverAgent hierarchy on 127.0.0.1:{WDA_LOCAL_PORT}; "
                "XXTouch remains the tap backend."
            )
            return True
        except OperationCancelled:
            raise
        except Exception as exc:
            self.log(
                "UI observer: WDA hierarchy unavailable "
                f"({type(exc).__name__}); using bounded XXTouch OCR."
            )
            return False

    def structured_dump_xml(self) -> str | None:
        """Return a real WDA/XCUITest hierarchy when the observer is available."""
        target = self.wda_observer_session
        if target is None and self.backend == "wda":
            target = self.session if self.session is not None else self.wda
        if target is None:
            return None
        for attr in ("source", "dump"):
            fn = getattr(target, attr, None)
            if callable(fn):
                try:
                    value = fn()
                    if value:
                        return value if isinstance(value, str) else str(value)
                except Exception:
                    continue
        return None

    def _try_xxtouch(self) -> bool:
        self.check_cancelled()
        code, listing = self.phone.exec_ok(
            "ls -ld /var/mobile/Media/1ferver /var/mobile/Media/1ferver/lua/scripts "
            "/var/jb/usr/local/xxtouch/bin /usr/local/xxtouch/bin 2>&1; "
            "command -v croissant; dpkg -l 2>/dev/null | grep -i xxtouch | head -5; "
            "ls -1 /var/jb/Applications 2>/dev/null | grep -i xx; "
            "netstat -an 2>/dev/null | grep 46952 | head -3",
            timeout=20,
        )
        self.check_cancelled()
        self.log("XXTouch probe:\n" + (listing.strip() or "(empty)"))
        has_pkg = "xxtouch" in listing.lower()
        has_home = any(
            "1ferver" in ln and "no such file" not in ln.lower() for ln in listing.splitlines()
        )
        has_app = "XXTExplorer" in listing
        # croissant is a Lua REPL. Scripts run through OpenAPI :46952.
        self.xxt_bin = ""
        if not has_pkg and not has_home and not has_app:
            return False
        self.phone.exec_ok(f"mkdir -p {XXT_SCRIPTS}", timeout=10)
        self.check_cancelled()
        try:
            self.phone._forward(XXT_LOCAL_PORT, XXT_REMOTE_PORT)
        except PhoneError as e:
            self.log(f"XXTouch OpenAPI forward skipped: {e}")
        self.backend = "xxtouch"
        self.log(
            "Tap stack: XXTouch Elite OpenAPI :46952. "
            "Keep XXTouch Elite open with Remote Service on."
        )
        return True

    def _try_wda(self) -> bool:
        self.check_cancelled()
        try:
            import urllib.request

            with urllib.request.urlopen(f"http://127.0.0.1:{WDA_LOCAL_PORT}/status", timeout=3) as r:
                body = r.read().decode("utf-8", errors="replace")
            self.check_cancelled()
            if "sessionId" not in body and "value" not in body and "ready" not in body.lower():
                self.log(f"WDA :{WDA_LOCAL_PORT} answered but did not look like WDA: {body[:180]}")
                return False
            client = _wda_client()
            self.wda = client
            try:
                st = client.status()
                self.log(f"WDA status: {st}")
            except Exception:
                pass
            try:
                self.session = client.session()
            except Exception as e:
                self.log(f"WDA session() failed ({e}); trying without a new session.")
                self.session = client
            self.backend = "wda"
            try:
                size = self.session.window_size()
                self.screen_w, self.screen_h = int(size[0]), int(size[1])
            except Exception:
                pass
            self.log(f"Tap stack: WebDriverAgent on 127.0.0.1:{WDA_LOCAL_PORT}")
            return True
        except OperationCancelled:
            raise
        except Exception as e:
            self.log(f"WDA not reachable on :{WDA_LOCAL_PORT} ({type(e).__name__}: {e})")
            return False

    def _try_zxtouch_port(self) -> bool:
        self.check_cancelled()
        code, out = self.phone.exec_ok(
            "netstat -an 2>/dev/null | grep -E '\\.6000' ; ps aux | grep -i zxtouch | grep -v grep",
            timeout=10,
        )
        if "6000" in out or "zxtouch" in out.lower():
            self.backend = "zxtouch"
            self.log("Tap stack: ZXTouch/SimulateTouch appears to be on the phone.")
            return True
        return False

    def can_tap(self) -> bool:
        return self.backend in ("wda", "zxtouch", "xxtouch")

    def open_url(self, url: str) -> None:
        self.check_cancelled()
        escaped = url.replace("'", "'\\''")
        code, out = self.phone.exec_ok(f"uiopen '{escaped}'", timeout=15)
        if code == 0:
            self.log(f"uiopen {url}")
            self.wait(2.0)
            return
        code, out = self.phone.exec_ok(f"open '{escaped}'", timeout=15)
        if code == 0:
            self.log(f"open {url}")
            self.wait(2.0)
            return
        raise PhoneError(
            f"Missing piece: cannot open URL {url!r}. uiopen/open failed ({out.strip()[:300]}). "
            "Install uiopen (Procursus) or use WDA."
        )

    def open_bundle(self, bundle: str) -> None:
        self.check_cancelled()
        if self.backend == "wda":
            try:
                if hasattr(self.session, "app_launch"):
                    self.session.app_launch(bundle)
                    self.check_cancelled()
                    self.wait(2.0)
                    self.log(f"WDA launch {bundle}")
                    return
            except OperationCancelled:
                raise
            except Exception as e:
                self.log(f"WDA launch {bundle} failed: {e}")
        for cmd in (
            f"uiopen --bundleid {bundle}",
            f"open {bundle}://",
            f"uiopen {bundle}://",
        ):
            self.check_cancelled()
            code, out = self.phone.exec_ok(cmd, timeout=15)
            if code == 0:
                self.log(cmd)
                self.wait(2.5)
                return
        raise PhoneError(f"Could not launch {bundle}.")

    def dump_xml(self) -> str:
        self.check_cancelled()
        if self.backend == "wda":
            result = self._dump_wda()
        elif self.backend == "xxtouch":
            result = self._dump_xxtouch()
        else:
            result = self._dump_cycript()
        self.check_cancelled()
        return result

    def _xxt_post(self, path: str, payload: bytes = b"", timeout: int = 8) -> str:
        import urllib.request

        self.check_cancelled()
        req = urllib.request.Request(
            f"http://127.0.0.1:{XXT_LOCAL_PORT}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = r.read().decode("utf-8", errors="replace")
        self.check_cancelled()
        return result

    def _run_xxt_lua(self, name: str, source: str, timeout: int = 25, wait_path: str = "") -> str:
        self.check_cancelled()
        script = f"{XXT_SCRIPTS}/{name}"
        self.phone.sftp_write(script, source)
        self.check_cancelled()
        # One Lua at a time: if a previous script is still running the
        # launch answers code 3 ("running another script") and the tap/type
        # is silently lost. Recycle + retry instead of firing blind.
        last = ""
        for attempt in range(4):
            self.check_cancelled()
            try:
                self._xxt_post("/recycle", b"", timeout=5)
            except OperationCancelled:
                raise
            except Exception as e:
                self.log(f"XXTouch recycle skipped: {e}")
            self.wait(0.15 + 0.9 * attempt)
            try:
                body = json.dumps({"filename": name}).encode("utf-8")
                sel = self._xxt_post("/select_script_file", body)
                launch = self._xxt_post("/launch_script_file", b"")
                last = f"select={sel[:120]} launch={launch[:120]}"
                self.log(f"XXTouch OpenAPI {last}")
                if '"code":3' in sel or '"code":3' in launch or "another script" in (sel + launch):
                    self.log(f"XXTouch busy (attempt {attempt + 1}/4) — recycle + retry")
                    self.wait(1.5)
                    continue
                break
            except OperationCancelled:
                raise
            except Exception as e:
                raise PhoneError(
                    f"XXTouch Elite is installed but the script did not start ({e}). "
                    "Keep XXTouch Elite open with Remote Service on."
                ) from e
        else:
            raise PhoneError(
                "XXTouch still running another script after 4 recycle+retry rounds. "
                f"Last: {last}. Open XXTouch Elite, stop scripts, Remote Service on."
            )
        if wait_path:
            last_err = "missing"
            for rnd in range(2):
                self.check_cancelled()
                if rnd:
                    self.log("XXTouch dump timeout, one recycle+relaunch")
                    try:
                        self._xxt_post("/recycle", b"", timeout=5)
                    except OperationCancelled:
                        raise
                    except Exception as e:
                        self.log(f"XXTouch recycle skipped: {e}")
                    self.wait(1.0)
                    try:
                        body = json.dumps({"filename": name}).encode("utf-8")
                        self._xxt_post("/select_script_file", body)
                        self._xxt_post("/launch_script_file", b"")
                    except OperationCancelled:
                        raise
                    except Exception as e:
                        last_err = str(e)
                        continue
                deadline = time.time() + timeout
                while time.time() < deadline:
                    self.check_cancelled()
                    try:
                        blob = self.phone.sftp_read(wait_path)
                        if blob.strip():
                            self.check_cancelled()
                            return blob
                    except Exception as e:
                        last_err = str(e)
                    self.wait(0.15)
            raise PhoneError(
                f"XXTouch dump file missing after {timeout}s x2 ({last_err}). "
                "Keep XXTouch Elite open with Remote Service on, auto-lock off."
            )
        self.wait(0.5)
        return "openapi-launched"

    def _dump_xxtouch(self) -> str:
        self.check_cancelled()
        out_path = f"{XXT_SCRIPTS}/ra_dump.json"
        lua = f"""screen.init(0)
local w, h = screen.size()
local canonical_w, canonical_h = 750, 1334
local scale_x = canonical_w / math.max(1, tonumber(w) or canonical_w)
local scale_y = canonical_h / math.max(1, tonumber(h) or canonical_h)
local txts, details
local ok, a, b = pcall(function()
  return screen.ocr_text({{}})
end)
if ok then txts, details = a, b end
if type(txts) ~= "table" then
  ok, a, b = pcall(function()
    return screen.ocr_text(0, 0, w, h)
  end)
  if ok then txts, details = a, b end
end
local parts = {{}}
local function add(t, x, y, bw, bh)
  t = tostring(t or ""):gsub("\\\\", "\\\\\\\\"):gsub('"', '\\\\"'):gsub("\\n", " ")
  -- The rest of the project uses the original iPhone 7 coordinate frame.
  -- Normalize OCR results here; _to_touch() maps them back to this device.
  x = math.floor((tonumber(x) or 0) * scale_x)
  y = math.floor((tonumber(y) or 0) * scale_y)
  bw = math.floor((tonumber(bw) or 0) * scale_x)
  bh = math.floor((tonumber(bh) or 0) * scale_y)
  table.insert(parts, string.format('{{"text":"%s","x":%d,"y":%d,"w":%d,"h":%d}}', t, x, y, bw, bh))
end
local okd, errd = pcall(function()
if type(txts) == "string" then
  add(txts, w/2, h/2, w, h)
elseif type(txts) == "table" then
  for i, t in ipairs(txts) do
    local d = (details and details[i]) or {{}}
    local x, y, bw, bh = 0, 0, 24, 24
    if type(d) == "table" then
      if type(d.center) == "table" then
        x = tonumber(d.center[1] or d.center.x) or 0
        y = tonumber(d.center[2] or d.center.y) or 0
      end
      if type(d.bounding_box) == "table" then
        local bb = d.bounding_box
        local l = tonumber(bb[1]) or 0
        local t0 = tonumber(bb[2]) or 0
        local r = tonumber(bb[3]) or 0
        local b0 = tonumber(bb[4]) or 0
        x, y, bw, bh = (l+r)/2, (t0+b0)/2, math.max(2, r-l), math.max(2, b0-t0)
      elseif d.left then
        local l, t0, r, b0 = tonumber(d.left) or 0, tonumber(d.top) or 0, tonumber(d.right) or 0, tonumber(d.bottom) or 0
        x, y, bw, bh = (l+r)/2, (t0+b0)/2, math.max(2, r-l), math.max(2, b0-t0)
      elseif d.x then
        x = tonumber(d.x) or x
        y = tonumber(d.y) or y
        bw = tonumber(d.w or d.width) or bw
        bh = tonumber(d.h or d.height) or bh
      end
    end
    add(t, x, y, bw, bh)
  end
end
if #parts == 0 then
  add("OCR_EMPTY:" .. type(txts), w/2, h/2, w, h)
end
local fh = io.open("{out_path}", "w")
if fh == nil then
  error("cannot write dump")
end
fh:write("[" .. table.concat(parts, ",") .. "]")
fh:close()
end)
if not okd then
  local fh = io.open("{out_path}", "w")
  if fh then
    local msg = tostring(errd or "err"):gsub('"', "'")
    fh:write('[{{"text":"LUA_ERR:' .. msg .. '","x":375,"y":667,"w":48,"h":24}}]')
    fh:close()
  end
end
"""
        self.phone.exec_ok(f"rm -f '{out_path}'", timeout=8)
        blob = self._run_xxt_lua("ra_dump.lua", lua, timeout=45, wait_path=out_path)
        if not blob.strip():
            raise PhoneError("XXTouch dump was empty. Live text dump failed — did not guess taps.")
        self.log(f"XXTouch live text dump {len(blob)} chars")
        return blob

    def _xxt_screen_size(self) -> tuple[int, int]:
        """Return native XXTouch touch space; OCR dumps use canonical 750x1334."""
        self.check_cancelled()
        if self._xxt_size_ok:
            return self.screen_w, self.screen_h
        path = f"{XXT_SCRIPTS}/ra_size.txt"
        lua = (
            "screen.init(0)\n"
            "local w, h = screen.size()\n"
            f'local fh = io.open("{path}", "w")\n'
            'fh:write(string.format("%s %s", tostring(w), tostring(h)))\n'
            "fh:close()\n"
        )
        self._run_xxt_lua("ra_size.lua", lua, timeout=15, wait_path=path)
        self.check_cancelled()
        blob = (self.phone.sftp_read(path) or "").strip().replace("x", " ")
        parts = blob.split()
        if len(parts) < 2:
            raise PhoneError(f"XXTouch screen.size unreadable: {blob!r}")
        self.screen_w, self.screen_h = int(float(parts[0])), int(float(parts[1]))
        self._xxt_size_ok = True
        self.log(f"XXTouch screen.size {self.screen_w}x{self.screen_h}")
        return self.screen_w, self.screen_h

    def refresh_screen_geometry(self) -> tuple[int, int]:
        """Refresh native touch geometry before a resolution-sensitive tap."""

        self.check_cancelled()
        if self.backend == "xxtouch":
            self._xxt_size_ok = False
            width, height = self._xxt_screen_size()
        else:
            width, height = int(self.screen_w), int(self.screen_h)
        if width < 100 or height < 100:
            raise PhoneError(
                f"Device returned invalid screen geometry {width}x{height}; did not tap."
            )
        return width, height

    def _to_touch(self, x: int, y: int) -> tuple[int, int]:
        self.check_cancelled()
        w, h = self._xxt_screen_size()
        return int(round(x * w / 750)), int(round(y * h / 1334))

    def _tap_xxtouch(self, x: int, y: int) -> None:
        self.check_cancelled()
        tx, ty = self._to_touch(x, y)
        self.log(f"touch ({tx},{ty}) from dump ({int(x)},{int(y)})")
        lua = f"screen.init(0)\ntouch.tap({tx}, {ty}, 40, 200)\n"
        self._run_xxt_lua("ra_tap.lua", lua, timeout=15)
        self.wait(0.15)

    def screenshot_png(self) -> bytes | None:
        """XXTouch screen.image PNG. Do not use Apple lockdown ScreenshotService."""
        self.check_cancelled()
        if self.backend != "xxtouch":
            return maybe_screenshot(self.phone, self.log)
        path = f"{XXT_SCRIPTS}/ra_shot.png"
        err_path = path + ".err"
        lua = (
            "screen.init(0)\n"
            f'os.remove("{err_path}")\n'
            "local ok, err = pcall(function()\n"
            f'  screen.image():save_to_png_file("{path}")\n'
            "end)\n"
            "if not ok then\n"
            f'  local fh = io.open("{err_path}", "w")\n'
            "  fh:write(tostring(err))\n"
            "  fh:close()\n"
            "end\n"
        )
        self.phone.exec_ok(f"rm -f '{path}' '{err_path}'", timeout=8)
        self._run_xxt_lua("ra_shot.lua", lua, timeout=20)
        deadline = time.time() + 12
        while time.time() < deadline:
            self.check_cancelled()
            try:
                data = self.phone.sftp_read_bytes(path)
                if data.startswith(b"\x89PNG"):
                    self.log(f"XXTouch screenshot {len(data)} bytes")
                    return data
            except Exception:
                pass
            self.wait(0.2)
        try:
            err = self.phone.sftp_read(err_path)
            self.log(f"XXTouch screenshot failed: {err.strip()[:240]}")
        except Exception as e:
            self.log(f"XXTouch screenshot missing: {e}")
        return None

    def type_text(self, text: str) -> None:
        """Type one key at a time. Never clipboard / paste (Ghost Pasteboard Prevent)."""
        self.check_cancelled()
        for ch in text or "":
            self.check_cancelled()
            if ch == "\b":
                lua = 'screen.init(0)\nkey.press("BACKSPACE")\n'
            elif ch == "\n":
                lua = 'screen.init(0)\nkey.press("RETURN")\n'
            else:
                esc = ch.replace("\\", "\\\\").replace('"', '\\"')
                lua = f'screen.init(0)\nkey.send_text("{esc}")\n'
            self._run_xxt_lua("ra_type.lua", lua, timeout=15)
            self.wait(0.3)
        self.check_cancelled()
        self.phone.exec_ok(f"rm -f '{XXT_SCRIPTS}/ra_type.lua'", timeout=8)
        self.wait(0.2)

    def type_human(self, text: str, ms: int = 95) -> None:
        """One Lua: char-by-char send_text with delay. Literal UTF-8 (not
        string.char, which breaks on ord>255 like curly quotes)."""
        self.check_cancelled()
        lines = ["screen.init(0)"]
        for ch in text or "":
            if ch == "\n":
                lines.append('key.press("RETURN")')
            else:
                esc = ch.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'key.send_text("{esc}")')
            lines.append(f"sys.msleep({max(40, int(ms))})")
            if ch == " ":
                lines.append("sys.msleep(45)")
        done = f"{XXT_SCRIPTS}/ra_human_done.txt"
        lines.append(f'local f = io.open("{done}", "w")')
        lines.append('f:write("OK")')
        lines.append("f:close()")
        lua = "\n".join(lines) + "\n"
        self.phone.exec_ok(f"rm -f '{done}'", timeout=8)
        self.log(f"type_human {len(text or '')} chars")
        wait_s = max(4.0, (len(text or "") * (ms + 20)) / 1000.0 + 2.0)
        self._run_xxt_lua("ra_human.lua", lua, timeout=int(wait_s) + 15, wait_path=done)
        self.wait(0.3)

    def press_backspace(self, n: int = 40) -> None:
        """One Lua, many BACKSPACE. Email-pill clear. Not paste."""
        self.check_cancelled()
        n = max(1, min(int(n), 80))
        lua = "screen.init(0)\n" + ('key.press("BACKSPACE")\n' * n)
        self._run_xxt_lua("ra_type.lua", lua, timeout=20)
        self.wait(0.25)

    def type_text_block(self, text: str) -> None:
        """One key.send_text of the whole string. Email pill only. Not clipboard."""
        self.check_cancelled()
        esc = (text or "").replace("\\", "\\\\").replace('"', '\\"')
        lua = f'screen.init(0)\nkey.send_text("{esc}")\n'
        self.log(f"type_text_block {len(text or '')} chars (not paste)")
        self._run_xxt_lua("ra_type.lua", lua, timeout=20)
        self.phone.exec_ok(f"rm -f '{XXT_SCRIPTS}/ra_type.lua'", timeout=8)
        self.wait(0.4)

    def _dump_wda(self) -> str:
        self.check_cancelled()
        last = None
        target = self.session if self.session is not None else self.wda
        for attr in ("source", "dump"):
            fn = getattr(target, attr, None)
            if callable(fn):
                try:
                    xml = fn()
                    if xml:
                        return xml if isinstance(xml, str) else str(xml)
                except Exception as e:
                    last = e
        try:
            import urllib.request

            sid = ""
            if self.session is not None and hasattr(self.session, "session_id"):
                sid = self.session.session_id
            url = f"http://127.0.0.1:{WDA_LOCAL_PORT}/source"
            if sid:
                url = f"http://127.0.0.1:{WDA_LOCAL_PORT}/session/{sid}/source"
            with urllib.request.urlopen(url, timeout=20) as r:
                payload = json.loads(r.read().decode("utf-8", errors="replace"))
            val = payload.get("value") if isinstance(payload, dict) else payload
            if isinstance(val, str) and val.strip():
                return val
        except Exception as e:
            last = e
        raise PhoneError(f"WDA dump failed: {last}")

    def _dump_cycript(self) -> str:
        self.check_cancelled()
        code, _ = self.phone.exec_ok("command -v cycript", timeout=8)
        if code != 0:
            raise PhoneError(
                "Missing piece: no UI dump tool. XXTouch Elite did not dump, WebDriverAgent is not on :8100, "
                "and cycript is not installed. Open XXTouch Elite on the phone. Did not guess taps."
            )
        script = (
            "try{var a=[UIApplication sharedApplication];"
            "var w=[a keyWindow]||[[a windows] firstObject];"
            "[w recursiveDescription].toString()}catch(e){e.toString()}"
        )
        for proc in ("Preferences", "SpringBoard", "Reddit"):
            self.check_cancelled()
            code, out = self.phone.exec_ok(
                f"cycript -p {proc} -e '{script}' 2>/dev/null",
                timeout=20,
            )
            if code == 0 and out.strip() and "Error" not in out[:40]:
                self.log(f"cycript dump via {proc} ({len(out)} chars)")
                return out
        raise PhoneError("cycript is installed but recursiveDescription returned nothing useful.")

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.check_cancelled()
        if self.backend != "xxtouch":
            raise PhoneError("Swipe needs XXTouch Elite.")
        ax, ay = self._to_touch(x1, y1)
        bx, by = self._to_touch(x2, y2)
        # XXTouch 5.4.6 has touch.on/move/off. touch.swipe is nil and raises Runtime Error.
        lua = (
            "screen.init(0)\n"
            f"touch.on(1, {ax}, {ay})\n"
            "sys.msleep(50)\n"
            f"touch.move(1, {bx}, {by})\n"
            "sys.msleep(280)\n"
            "touch.off(1)\n"
        )
        self.log(f"swipe dump ({x1},{y1})->({x2},{y2}) touch ({ax},{ay})->({bx},{by})")
        self._run_xxt_lua("ra_swipe.lua", lua, timeout=15)
        self.wait(0.9)

    def swipe_up(self) -> None:
        self.swipe(375, 1100, 375, 480)

    def interp_flick(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        steps: int = 12,
        ms: int = 18,
        settle_seconds: float = 0.9,
    ) -> None:
        """Smooth flick. Needed to dismiss an iPhone 7 app-switcher card. stack.swipe is too coarse."""
        self.check_cancelled()
        if self.backend != "xxtouch":
            raise PhoneError("Flick needs XXTouch Elite.")
        ax, ay = self._to_touch(x1, y1)
        bx, by = self._to_touch(x2, y2)
        lines = ["screen.init(0)", f"touch.on(1, {ax}, {ay})", "sys.msleep(40)"]
        for i in range(1, steps + 1):
            t = i / steps
            x = int(round(ax + (bx - ax) * t))
            y = int(round(ay + (by - ay) * t))
            lines.append(f"touch.move(1, {x}, {y})")
            lines.append(f"sys.msleep({ms})")
        lines.append("touch.off(1)")
        self.log(f"interp_flick dump ({x1},{y1})->({x2},{y2})")
        self._run_xxt_lua("ra_swipe.lua", "\n".join(lines) + "\n", timeout=15)
        self.wait(max(0.0, float(settle_seconds)))

    def tap_row(self, node: Node) -> None:
        """Tap inside a table row. OCR boxes are fake 24x24 at the left of the label."""
        self.check_cancelled()
        x = min(max(node.x + 180, 240), 560)
        y = node.center()[1]
        self.tap_xy(x, y)

    def tap_switch(self, node: Node) -> None:
        """Settings UISwitch (Shadow / Crane). Trailing-edge knob. Do not use on Ghost Advanced."""
        self.check_cancelled()
        x = self.screen_w - 62
        y = node.center()[1]
        self.tap_xy(x, y)

    def tap_ghost_knob(self, node: Node) -> None:
        """Ghost glass-card UISwitch. Knob is at x=626, ~38pt below the title OCR center."""
        self.check_cancelled()
        x = 626
        y = node.center()[1] + 38
        tx, ty = self._to_touch(x, y)
        self.log(f"ghost_knob dump ({x},{y})")
        lua = (
            "screen.init(0)\n"
            f"touch.on(1, {tx}, {ty})\n"
            "sys.msleep(140)\n"
            "touch.off(1)\n"
        )
        self._run_xxt_lua("ra_tap.lua", lua, timeout=15)
        self.wait(0.35)

    def long_tap(self, x: int, y: int, hold_ms: int = 550) -> None:
        self.check_cancelled()
        tx, ty = self._to_touch(int(x), int(y))
        self.log(f"long_tap dump ({int(x)},{int(y)}) hold={hold_ms}")
        lua = (
            "screen.init(0)\n"
            f"touch.on(1, {tx}, {ty})\n"
            f"sys.msleep({int(hold_ms)})\n"
            "touch.off(1)\n"
        )
        self._run_xxt_lua("ra_tap.lua", lua, timeout=15)
        self.wait(0.5)

    def press_home(self) -> None:
        """Physical Home via XXTouch. Do not killall / force-stop."""
        self.check_cancelled()
        if self.backend != "xxtouch":
            self.home()
            return
        lua = 'screen.init(0)\nkey.press("HOMEBUTTON")\n'
        self._run_xxt_lua("ra_home.lua", lua, timeout=15)
        self.wait(1.2)

    def double_home(self, gap_ms: int = 280) -> None:
        """Open app switcher on iPhone 7. 80ms is too fast; 280ms worked."""
        self.check_cancelled()
        if self.backend != "xxtouch":
            raise PhoneError("Double-Home needs XXTouch Elite.")
        lua = (
            "screen.init(0)\n"
            'key.press("HOMEBUTTON")\n'
            f"sys.msleep({int(gap_ms)})\n"
            'key.press("HOMEBUTTON")\n'
        )
        self._run_xxt_lua("ra_home.lua", lua, timeout=15)
        self.wait(1.5)

    def open_app_switcher(self) -> str:
        """Open Recents using the verified method for the screen shape."""
        self.check_cancelled()
        if self.backend != "xxtouch":
            raise PhoneError("App switcher needs XXTouch Elite.")
        _, native_h = self._xxt_screen_size()
        if native_h <= 1600:
            self.log("app_switcher double-home (home-button device)")
            self.double_home(280)
            return "double-home"

        # Live iPhone X / iOS 15.8 evidence: injected bottom-edge drags are
        # delivered to Reddit (or ignored on SpringBoard), while two immediate
        # synthetic Home presses open Recents reliably.  A timing gap changes
        # the interpretation back to two independent Home actions.
        self.log("app_switcher immediate double-home (Face ID device)")
        lua = (
            "screen.init(0)\n"
            'key.press("HOMEBUTTON")\n'
            'key.press("HOMEBUTTON")\n'
        )
        self._run_xxt_lua("ra_switcher.lua", lua, timeout=15)
        self.wait(1.5)
        return "double-home-nogap"

    def clear_app_memory(self, bundle_id: str) -> str:
        """Quit one proven app, force-closing only if graceful quit is ineffective.

        XXTouch's ``app.quit`` uses swipe-up quit semantics and asks the app to
        save state.  On this iPhone/iOS build it can leave a background process
        running, so verify ``app.is_running`` and use ``app.close`` only as the
        bounded fallback.  Never accept ``*`` or another broad target.
        """
        self.check_cancelled()
        if self.backend != "xxtouch":
            raise PhoneError("Clearing app memory needs XXTouch Elite.")
        bundle = str(bundle_id or "").strip()
        if bundle == "*" or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]+", bundle) is None:
            raise PhoneError("Refused an invalid or broad app-memory target.")

        out_path = f"{self.xxt_scripts}/ra_clear_app_memory.txt"
        self.phone.exec_ok(f"rm -f '{out_path}'", timeout=8)
        lua = f"""local bid = {json.dumps(bundle)}
local before = app.is_running(bid)
local after_quit = before
local graceful = "skipped"
local forced = "skipped"
if before then
  graceful = app.quit(bid)
  sys.msleep(3500)
  after_quit = app.is_running(bid)
  if after_quit then
    forced = app.close(bid)
    sys.msleep(1200)
  end
end
local after_close = app.is_running(bid)
local fh = io.open({json.dumps(out_path)}, "w")
if fh == nil then error("cannot write app-memory status") end
fh:write(
  tostring(before) .. " " ..
  tostring(after_quit) .. " " ..
  tostring(after_close) .. " " ..
  tostring(graceful) .. " " ..
  tostring(forced)
)
fh:close()
"""
        raw = self._run_xxt_lua(
            "ra_clear_app_memory.lua", lua, timeout=15, wait_path=out_path
        ).strip()
        fields = raw.split()
        if len(fields) != 5 or any(value not in {"true", "false"} for value in fields[:3]):
            raise PhoneError("XXTouch returned an unreadable app-memory status.")
        was_running = fields[0] == "true"
        running_after_quit = fields[1] == "true"
        running_after_close = fields[2] == "true"
        if running_after_close:
            raise PhoneError("Reddit remained in memory after one graceful and one forced close.")
        if not was_running:
            return "already-stopped"
        if running_after_quit:
            return "force-close"
        return "graceful-quit"

    def force_close_app(self, bundle_id: str) -> str:
        """Force-close one exact bundle and verify that its process stopped.

        Unlike :meth:`clear_app_memory`, this operation deliberately invokes
        ``app.close`` even when the process is already stopped.  It is used by
        the completed-swipe Reddit cleanup only after the app-switcher card has
        been dismissed.  Never accept ``*`` or another broad target.
        """
        self.check_cancelled()
        if self.backend != "xxtouch":
            raise PhoneError("Force-closing an application needs XXTouch Elite.")
        bundle = str(bundle_id or "").strip()
        if bundle == "*" or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]+", bundle) is None:
            raise PhoneError("Refused an invalid or broad force-close target.")

        out_path = f"{self.xxt_scripts}/ra_force_close_app.txt"
        self.phone.exec_ok(f"rm -f '{out_path}'", timeout=8)
        lua = f"""local bid = {json.dumps(bundle)}
local before = app.is_running(bid)
app.close(bid)
sys.msleep(1200)
local after_close = app.is_running(bid)
local fh = io.open({json.dumps(out_path)}, "w")
if fh == nil then error("cannot write force-close status") end
fh:write(tostring(before) .. " " .. tostring(after_close))
fh:close()
"""
        raw = self._run_xxt_lua(
            "ra_force_close_app.lua", lua, timeout=15, wait_path=out_path
        ).strip()
        fields = raw.split()
        if len(fields) != 2 or any(value not in {"true", "false"} for value in fields):
            raise PhoneError("XXTouch returned an unreadable force-close status.")
        was_running = fields[0] == "true"
        running_after_close = fields[1] == "true"
        if running_after_close:
            raise PhoneError("Reddit remained in memory after one exact force close.")
        return "force-close" if was_running else "already-stopped"

    def app_is_running(self, bundle_id: str) -> bool:
        """Read whether one exact bundle process is running without stopping it."""
        self.check_cancelled()
        if self.backend != "xxtouch":
            raise PhoneError("Application process verification needs XXTouch Elite.")
        bundle = str(bundle_id or "").strip()
        if bundle == "*" or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]+", bundle) is None:
            raise PhoneError("Refused an invalid or broad application-memory target.")

        out_path = f"{self.xxt_scripts}/ra_app_running.txt"
        self.phone.exec_ok(f"rm -f '{out_path}'", timeout=8)
        lua = f"""local running = app.is_running({json.dumps(bundle)})
local fh = io.open({json.dumps(out_path)}, "w")
if fh == nil then error("cannot write application status") end
fh:write(tostring(running))
fh:close()
"""
        result = self._run_xxt_lua(
            "ra_app_running.lua",
            lua,
            timeout=15,
            wait_path=out_path,
        ).strip().lower()
        if result not in {"true", "false"}:
            raise PhoneError("XXTouch returned an unreadable application process status.")
        return result == "true"

    def tap_xy(self, x: int, y: int) -> None:
        self.check_cancelled()
        x, y = int(x), int(y)
        self.log(f"tap ({x},{y}) via {self.backend}")
        if self.backend == "wda":
            self._tap_wda(x, y)
            return
        if self.backend == "xxtouch":
            self._tap_xxtouch(x, y)
            return
        if self.backend == "zxtouch":
            self._tap_zxtouch(x, y)
            return
        raise PhoneError(
            "Missing piece: no tap backend. Dump succeeded but cannot tap. "
            "Install TrollStore WebDriverAgent, AutoTouch, XXTouch, or ZXTouch."
        )

    def semantic_keyboard_present(self) -> bool | None:
        """Ask XXTouch accessibility whether keyboard keys are visible.

        ``None`` means the semantic API is unavailable, allowing callers to
        retain their existing OCR fallback. No field value or key text is
        returned or logged.
        """
        self.check_cancelled()
        hierarchy = self.structured_dump_xml()
        if hierarchy:
            try:
                root = ET.fromstring(hierarchy)
                visible_keyboard = any(
                    element.tag.endswith("Keyboard")
                    and str(element.attrib.get("visible", "true")).lower() != "false"
                    for element in root.iter()
                )
                self.log(
                    "WDA accessibility keyboard: "
                    + ("visible" if visible_keyboard else "absent")
                )
                return visible_keyboard
            except ET.ParseError:
                self.log("WDA accessibility hierarchy was malformed; using OCR fallback")
        if self.backend != "xxtouch":
            return None
        out_path = f"{self.xxt_scripts}/ra_keyboard_state.txt"
        lua = f'''local outcome = "unsupported"
local loaded, ui_element = pcall(require, "ui_element")
if loaded and type(ui_element) == "table" then
  local queried, items = pcall(function()
    return ui_element.find_all({{ role = "keyboard_key", visible = true }}, {{
      max_level = 1,
      max_elements = 120
    }})
  end)
  if queried and type(items) == "table" then
    outcome = (#items > 0) and "yes" or "no"
  end
end
local fh = io.open("{out_path}", "w")
if fh == nil then error("cannot write keyboard state") end
fh:write(outcome)
fh:close()
'''
        self.phone.exec_ok(f"rm -f '{out_path}'", timeout=8)
        result = self._run_xxt_lua(
            "ra_keyboard_state.lua", lua, timeout=15, wait_path=out_path
        ).strip().casefold()
        if result == "yes":
            self.log("XXTouch accessibility keyboard: visible")
            return True
        if result == "no":
            self.log("XXTouch accessibility keyboard: absent")
            return False
        self.log("XXTouch accessibility keyboard: unavailable; using OCR fallback")
        return None

    def _tap_wda(self, x: int, y: int) -> None:
        self.check_cancelled()
        s = self.session
        for fn_name in ("tap", "click"):
            fn = getattr(s, fn_name, None)
            if callable(fn):
                try:
                    fn(x, y)
                    return
                except TypeError:
                    try:
                        fn(x=x, y=y)
                        return
                    except Exception:
                        pass
                except Exception:
                    pass
        import urllib.request

        sid = getattr(s, "session_id", "") or getattr(self.wda, "session_id", "")
        if not sid:
            raise PhoneError("WDA session id missing; cannot tap.")
        data = json.dumps({"x": x, "y": y}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{WDA_LOCAL_PORT}/session/{sid}/wda/tap/0",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()

    def _tap_zxtouch(self, x: int, y: int) -> None:
        self.check_cancelled()
        line = f"1{int(x):05d}{int(y):05d}001"
        up = f"0{int(x):05d}{int(y):05d}001"
        cmd = (
            "python3 - <<'PY'\n"
            "import socket,time\n"
            "s=socket.create_connection(('127.0.0.1',6000),2)\n"
            f"s.sendall(b'{line}\\n')\n"
            "time.sleep(0.05)\n"
            f"s.sendall(b'{up}\\n')\n"
            "s.close()\n"
            "PY"
        )
        code, out = self.phone.exec_ok(cmd, timeout=10)
        if code != 0:
            raise PhoneError(f"ZXTouch tap failed: {out[:300]}")

    def home(self) -> None:
        self.check_cancelled()
        if self.backend == "wda":
            for obj in (self.session, self.wda):
                fn = getattr(obj, "home", None)
                if callable(fn):
                    try:
                        fn()
                        self.wait(1.0)
                        return
                    except OperationCancelled:
                        raise
                    except Exception:
                        pass
        self.check_cancelled()
        self.phone.exec_ok("activator send libactivator.system.homebutton", timeout=8)
        self.wait(1.0)


def parse_nodes(blob: str) -> list[Node]:
    blob = blob or ""
    stripped = blob.lstrip()
    if stripped.startswith("["):
        return _parse_xxtouch_json(blob)
    if "<" in blob[:200] and "XCUIElementType" in blob:
        return _parse_wda_xml(blob)
    return _parse_recursive_description(blob)


def _parse_xxtouch_json(blob: str) -> list[Node]:
    items = json.loads(blob)
    nodes: list[Node] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        cx, cy = int(float(it.get("x") or 0)), int(float(it.get("y") or 0))
        w, h = int(float(it.get("w") or 0)), int(float(it.get("h") or 0))
        if w < 8:
            w = 48
        if h < 8:
            h = 22
        n = Node(
            ntype="ocr",
            name=text,
            label=text,
            text=text,
            x=max(0, cx - w // 2),
            y=max(0, cy - h // 2),
            w=w,
            h=h,
            raw=json.dumps(it, ensure_ascii=False),
        )
        nodes.append(n)
    # Merge same-row words so "Create New Address" is one node.
    rows: list[list[Node]] = []
    for n in sorted(nodes, key=lambda a: (a.y, a.x)):
        placed = False
        for row in rows:
            if abs(row[0].y - n.y) <= 18:
                row.append(n)
                placed = True
                break
        if not placed:
            rows.append([n])
    for row in rows:
        row.sort(key=lambda a: a.x)
        phrase = " ".join(a.text for a in row)
        if len(row) < 2:
            continue
        x1 = min(a.x for a in row)
        y1 = min(a.y for a in row)
        x2 = max(a.x + a.w for a in row)
        y2 = max(a.y + a.h for a in row)
        nodes.append(
            Node(
                ntype="ocr-line",
                name=phrase,
                label=phrase,
                text=phrase,
                x=x1,
                y=y1,
                w=max(8, x2 - x1),
                h=max(8, y2 - y1),
                raw=phrase,
            )
        )
    return nodes


def _parse_wda_xml(xml: str) -> list[Node]:
    nodes: list[Node] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        xml2 = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;)", "&amp;", xml)
        root = ET.fromstring(xml2)

    def walk(el: ET.Element) -> None:
        a = el.attrib
        n = Node(
            ntype=el.tag.replace("XCUIElementType", ""),
            name=a.get("name") or a.get("label") or "",
            label=a.get("label") or "",
            value=a.get("value") or "",
            text=a.get("name") or a.get("label") or a.get("value") or "",
            visible=str(a.get("visible", "true")).lower() != "false",
            enabled=str(a.get("enabled", "true")).lower() != "false",
            raw=str(a),
        )
        if "x" in a and "width" in a:
            n.x, n.y = _int(a.get("x")), _int(a.get("y"))
            n.w, n.h = _int(a.get("width")), _int(a.get("height"))
        else:
            b = a.get("bounds") or ""
            m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                n.x, n.y, n.w, n.h = x1, y1, x2 - x1, y2 - y1
        nodes.append(n)
        for child in list(el):
            walk(child)

    walk(root)
    return nodes


def _parse_recursive_description(text: str) -> list[Node]:
    nodes: list[Node] = []
    for line in text.splitlines():
        m = re.search(r"frame = \( ?([0-9.]+) ([0-9.]+); ([0-9.]+) ([0-9.]+) \)", line)
        if not m:
            m2 = re.search(
                r"frame = \{\{([0-9.]+), ([0-9.]+)\}, \{([0-9.]+), ([0-9.]+)\}\}",
                line,
            )
            if not m2:
                continue
            x, y, w, h = (float(m2.group(i)) for i in range(1, 5))
        else:
            x, y, w, h = (float(m.group(i)) for i in range(1, 5))
        textm = re.search(r"(?:text|label|name) = '([^']*)'", line, re.I)
        cls = ""
        cm = re.search(r"<([^: >]+)", line)
        if cm:
            cls = cm.group(1)
        n = Node(
            ntype=cls,
            name=textm.group(1) if textm else "",
            label=textm.group(1) if textm else "",
            text=textm.group(1) if textm else "",
            x=int(x),
            y=int(y),
            w=int(w),
            h=int(h),
            raw=line.strip()[:400],
        )
        nodes.append(n)
    return nodes


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def find_label(nodes: list[Node], labels: tuple[str, ...] | list[str]) -> Node | None:
    want = [collapsed_text(s) for s in labels if collapsed_text(s)]
    visible = [n for n in nodes if n.on_screen() and n.visible]
    for n in visible:
        blob = collapsed_text(n.labels())
        if not blob:
            continue
        for w in want:
            if blob == w or blob.startswith(w) or w in blob:
                return n
    return None


def collapsed_text(s: str) -> str:
    words = (s or "").casefold().split()
    out: list[str] = []
    for w in words:
        if not out or out[-1] != w:
            out.append(w)
    n = len(out)
    if n >= 4 and n % 3 == 0:
        k = n // 3
        if out[:k] == out[k : 2 * k] == out[2 * k :]:
            out = out[:k]
    elif n >= 4 and n % 2 == 0:
        k = n // 2
        if out[:k] == out[k:]:
            out = out[:k]
    return " ".join(out)


def find_label_exact(nodes: list[Node], labels: tuple[str, ...] | list[str]) -> Node | None:
    want = {collapsed_text(s) for s in labels if collapsed_text(s)}
    visible = [n for n in nodes if n.on_screen() and n.visible]
    for n in visible:
        blob = collapsed_text(n.labels())
        if blob in want:
            return n
    return None


def find_continue_gate(nodes) -> Node | None:
    """NSFW-gate Continue button: labeled node first; else the word-level
    OCR 'continue' node (app sheet buttons often miss the text tree, and
    merged OCR rows span both buttons — proven live on the MomSonIncest
    gate). Only call after NSFW markers are proven in the dump. Blue
    primary sits right of Go Back: prefer the rightmost exact word. Pure."""
    hit = find_label_exact(nodes or [], ("Continue",))
    if hit is not None:
        return hit
    best = None
    for n in nodes or []:
        if getattr(n, "ntype", "") != "ocr" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) == "continue":
            if best is None or n.x > best.x:
                best = n
    if best is not None:
        return best
    # Last resort: one merged OCR row holding exactly the two buttons.
    for n in nodes or []:
        if getattr(n, "ntype", "") != "ocr-line" or not n.on_screen():
            continue
        words = set(collapsed_text(n.labels()).split())
        if words <= {"go", "back", "continue"} and "continue" in words:
            cx = n.x + (n.w * 3) // 4
            n.center = lambda cx=cx, cy=n.y + n.h // 2: (cx, cy)
            return n
    return None


def _merged_pair_row(nodes, words: set, want: str):
    """Merged OCR row holding exactly a button pair: tap the right side
    where the wanted button sits. Pure (right-side primary convention)."""
    for n in nodes or []:
        if getattr(n, "ntype", "") != "ocr-line" or not n.on_screen():
            continue
        if set(collapsed_text(n.labels()).split()) <= words \
                and want in collapsed_text(n.labels()).split():
            cx = n.x + (n.w * 3) // 4
            n.center = lambda cx=cx, cy=n.y + n.h // 2: (cx, cy)
            return n
    return None


def find_update_settings(nodes) -> Node | None:
    """Update Settings gate button. Labeled first, OCR words next."""
    hit = find_label_exact(nodes or [], ("Update Settings",))
    if hit is not None:
        return hit
    best = None
    for n in nodes or []:
        if getattr(n, "ntype", "") != "ocr" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if t not in ("update", "settings"):
            continue
        if t == "settings":
            if best is None or n.x > best.x:
                best = n
        elif best is None:
            best = n
    if best is not None:
        return best
    return _merged_pair_row(nodes, {"go", "back", "update", "settings"},
                            "settings")


def email_nodes(nodes: list[Node]) -> list[str]:
    found: list[str] = []
    pat = re.compile(r"[A-Za-z0-9._%+\-]+@icloud\.com", re.I)
    for n in nodes:
        for m in pat.findall(n.labels() + " " + n.raw):
            addr = m.strip().lower()
            if addr not in found:
                found.append(addr)
    return found


def save_dump(name: str, blob: str, log: LogFn, screenshot: bytes | None = None) -> str:
    os.makedirs(DUMP_DIR, exist_ok=True)
    path = os.path.join(DUMP_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(blob or "")
    log(f"Wrote {path} ({len(blob or '')} chars)")
    if screenshot:
        png = os.path.splitext(path)[0] + ".png"
        with open(png, "wb") as f:
            f.write(screenshot)
        log(f"Wrote {png}")
    return path


def maybe_screenshot(phone: Phone, log: LogFn) -> bytes | None:
    try:
        import asyncio

        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.screenshot import ScreenshotService

        async def _shot() -> bytes:
            lockdown = await create_using_usbmux(serial=phone.serial)
            svc = ScreenshotService(lockdown)
            return await svc.take_screenshot()

        data = asyncio.run(_shot())
        if isinstance(data, bytes):
            return data
    except Exception as e:
        log(f"Screenshot skipped: {type(e).__name__}: {e}")
    return None


def dump_named(
    stack: TapStack,
    phone: Phone,
    stem: str,
    log: LogFn,
    shot: bool | None = None,
    *,
    persist: bool = True,
    preview: bool = True,
) -> tuple[str, list[Node]]:
    """Capture and parse a screen dump.

    Transient probes can disable persistence and label previews so Home Screen
    text is available for state detection without being saved or logged.
    """
    blob = stack.dump_xml()
    png = None
    want_shot = persist and (
        bool(os.environ.get("XXT_SHOT")) if shot is None else shot
    )
    if want_shot:
        if stack.backend == "xxtouch":
            png = stack.screenshot_png()
        elif not os.environ.get("SKIP_SHOT"):
            png = maybe_screenshot(phone, log)
    stripped = (blob or "").lstrip()
    if stripped.startswith("<"):
        ext = ".xml"
    elif stripped.startswith("["):
        ext = ".json"
    else:
        ext = ".txt"
    if persist:
        save_dump(stem + ext, blob, log, png)
    nodes = parse_nodes(blob)
    clickable = [n for n in nodes if n.labels() and n.on_screen()]
    mode = "" if persist else " (transient; not saved)"
    detailed = preview and preview_detail_enabled()
    if detailed:
        log(f"== frame begin: {stem} ==")
    log(f"{stem}: {len(nodes)} nodes, {len(clickable)} labeled{mode}")
    if preview:
        shown = clickable if detailed else clickable[:12]
        for n in shown:
            log(f"  - {n.ntype} {n.labels()[:80]!r} [{n.x},{n.y} {n.w}x{n.h}]")
        if detailed:
            hidden = len(clickable) - len(shown)
            if hidden > 0:
                log(f"  - ... {hidden} more labeled node(s) omitted")
            log(f"== frame end: {stem} ({len(shown)} of {len(clickable)} labeled) ==")
    else:
        # Privacy-sensitive screens keep preview=False; report safe metadata only
        # so the suppression is explained without exposing any label text.
        log(
            f"  - frame preview suppressed (privacy-sensitive); "
            f"{len(nodes)} nodes, {len(clickable)} labeled"
        )
    return blob, nodes


def dump_inventory_screens(stack: TapStack, phone: Phone, log: LogFn) -> None:
    """SpringBoard, Settings, Reddit (dump only — no signup taps)."""
    try:
        stack.home()
    except Exception as e:
        log(f"home() skipped: {e}")
    time.sleep(1.0)
    try:
        dump_named(stack, phone, "01_springboard", log)
    except PhoneError as e:
        log(f"SpringBoard dump: {e}")
        return
    try:
        stack.open_url(HME_SETTINGS_URL)
        dump_named(stack, phone, "02_settings_hidemyemail", log)
    except PhoneError as e:
        log(f"Settings dump: {e}")
        try:
            stack.open_bundle(SETTINGS_BUNDLE)
            dump_named(stack, phone, "02_settings", log)
        except PhoneError as e2:
            log(f"Settings launch: {e2}")
    try:
        stack.open_bundle(REDDIT_BUNDLE)
        dump_named(stack, phone, "10_reddit_front", log)
        log("Reddit dump only — not tapping Create account / SIWA.")
        stack.home()
    except PhoneError as e:
        log(f"Reddit dump skipped: {e}")
