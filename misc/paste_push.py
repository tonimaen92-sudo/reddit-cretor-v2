# UTF-8
"""Quick paste bot: PC text -> phone note field. Typing path only.

UI: paste/type text, see live lengths, stage the phone field, press PUSH.
Worker thread does the push; every GUI update goes through root.after
(never touch GUI from the worker — the Generate grey-hang lesson).

Mechanism (discovered live, not assumed):
  Device clipboard-set does NOT exist on this stack: no WDA running
  (:8100 refuses), no pbcopy/pbpaste on device, Ghost blocks paste
  inside containers. So: whole-line key.send_text per line + RETURN
  between lines (literal UTF-8 bytes in Lua source = byte-transparent
  for ALL Unicode incl. supplementary PUA; quotes/backslash escaped
  exactly like ui_tree.type_text_block). Verification: dump before +
  after, length-delta must match exactly + distinctive tokens present.
Screen: types ONLY into the field YOU staged (open Notes/new note,
tap the field first). No navigation, no taps, no app launches.

Secrets: SSH password entry prefilled from secrets.json ssh_password.
Push log: logs/paste_log.jsonl, metadata ONLY (timestamp, lengths,
sha256, mechanism). Full strings are never logged.

  python misc/paste_push.py                      # GUI
  python misc/paste_push.py --dump-field         # dump only, no typing
  python misc/paste_push.py --selftest "text"    # headless push + verify
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import LOG_DIR  # noqa: E402
from stay_awake import allow_sleep, stay_awake  # noqa: E402
from ssh_device import Phone, PhoneError, load_secrets  # noqa: E402
from ui_tree import TapStack, collapsed_text, dump_named  # noqa: E402

PASTE_LOG = os.path.join(LOG_DIR, "paste_log.jsonl")
MAX_INPUT_BYTES = 1_000_000


def lengths(text: str) -> tuple[int, int, int]:
    """(code points, UTF-16 units, UTF-8 bytes). Reddit limits count UTF-16."""
    return (
        len(text),
        len(text.encode("utf-16-le")) // 2,
        len(text.encode("utf-8")),
    )


def lengths_line(text: str) -> str:
    cp, u16, u8 = lengths(text)
    return "chars %d | UTF-16 units %d | UTF-8 bytes %d" % (cp, u16, u8)


def screen_text(nodes) -> str:
    """Visible text mass of a dump (field content check, not OCR art)."""
    parts: list[str] = []
    for n in nodes or []:
        try:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            t = collapsed_text(n.labels())
        except Exception:
            continue
        if t:
            parts.append(t)
    return "\n".join(parts)


def distinctive_tokens(text: str, limit: int = 3) -> list[str]:
    """Longest alphanumeric runs — presence-checked after typing."""
    runs = re.findall(r"[A-Za-z0-9@_]{6,}", text or "")
    runs = sorted(set(runs), key=len, reverse=True)
    return runs[:limit]


def send_line(stack: TapStack, line: str) -> None:
    """One whole line via literal-bytes send_text (type_text_block twin)."""
    esc = (line or "").replace("\\", "\\\\").replace('"', '\\"')
    lua = 'screen.init(0)\nkey.send_text("%s")\n' % esc
    stack._run_xxt_lua("ra_type.lua", lua, timeout=20)
    try:
        stack.phone.exec_ok("rm -f '/var/mobile/Media/1ferver/lua/scripts/ra_type.lua'",
                            timeout=8)
    except Exception:
        pass
    time.sleep(0.4)


def send_return(stack: TapStack) -> None:
    lua = 'screen.init(0)\nkey.press("RETURN")\n'
    stack._run_xxt_lua("ra_type.lua", lua, timeout=15)
    try:
        stack.phone.exec_ok("rm -f '/var/mobile/Media/1ferver/lua/scripts/ra_type.lua'",
                            timeout=8)
    except Exception:
        pass
    time.sleep(0.3)


def type_text(stack: TapStack, text: str, log=print) -> int:
    """Type preserving newlines. Returns line count. No clipboard involved."""
    lines = (text or "").split("\n")
    log("typing %d line(s)…" % len(lines))
    for i, ln in enumerate(lines):
        send_line(stack, ln)
        if i < len(lines) - 1:
            send_return(stack)
    return len(lines)


def push_text(password: str, text: str, log=print) -> dict:
    """Type into the staged field, verify by dump delta. Returns receipt."""
    if not text:
        raise ValueError("empty text — nothing to push")
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("over 1MB — refused")
    cp, u16, u8 = lengths(text)
    tokens = distinctive_tokens(text)
    log("PUSH %s" % lengths_line(text))
    phone = Phone(log=log)
    log("USB SSH…")
    phone.connect(password)  # also_wda default True; harmless if WDA down
    try:
        log("SSH up serial=%s" % getattr(phone, "serial", "?"))
        stack = TapStack(phone, log)
        backend = stack.attach()
        log("backend: %s" % backend)
        if not stack.can_tap():
            raise RuntimeError("no XXTouch/WDA backend — open XXTouch Elite "
                               "on the phone with Remote Service ON")
        t0 = time.time()
        _blob0, nodes0 = dump_named(stack, phone, "paste_before", log)
        base = screen_text(nodes0)
        base_cp, base_u16 = len(base), len(base.encode("utf-16-le")) // 2
        log("baseline field text: %d chars / %d UTF-16" % (base_cp, base_u16))
        nlines = type_text(stack, text, log=log)
        _blob1, nodes1 = dump_named(stack, phone, "paste_after", log)
        after = screen_text(nodes1)
        dcp = len(after) - base_cp
        du16 = (len(after.encode("utf-16-le")) // 2) - base_u16
        log("delta: +%d chars / +%d UTF-16 (expected +%d / +%d)"
            % (dcp, du16, cp, u16))
        missing = [t for t in tokens if t not in after]
        if dcp != cp or du16 != u16 or missing:
            raise RuntimeError(
                "VERIFY MISMATCH delta=+%d/+%d expected=+%d/+%d missing=%r "
                "(autocorrect? wrong field? cursor moved?)"
                % (dcp, du16, cp, u16, missing))
        log("VERIFY MATCH — text landed byte-exact in %.1fs"
            % (time.time() - t0))
        receipt = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mechanism": "send_text_lines",
            "lines": nlines,
            "chars": cp,
            "utf16": u16,
            "utf8": u8,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(PASTE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(receipt) + "\n")
        except Exception as e:
            log("push-log write failed (push itself OK): %s" % e)
        return receipt
    finally:
        try:
            phone.close()
        except Exception:
            pass


def run_dump_field() -> int:
    def lg(m: str) -> None:
        print(m, flush=True)

    try:
        sec = load_secrets()
    except Exception:
        sec = {}
    phone = Phone(log=lg)
    try:
        phone.connect(str(sec.get("ssh_password") or ""))
        stack = TapStack(phone, lg)
        lg("backend: %s" % stack.attach())
        _blob, nodes = dump_named(stack, phone, "paste_field", lg)
        shown = 0
        for n in nodes or []:
            try:
                if n.ntype == "ocr-line" or not n.on_screen():
                    continue
                t = collapsed_text(n.labels())
            except Exception:
                continue
            if t and len(t) < 120:
                print("NODE :: %s" % t[:120], flush=True)
                shown += 1
                if shown >= 30:
                    break
        return 0
    except (PhoneError, RuntimeError) as e:
        print("DUMP FAIL %s: %s" % (type(e).__name__, e), flush=True)
        return 1
    finally:
        try:
            phone.close()
        except Exception:
            pass


def run_selftest(text: str) -> int:
    def lg(m: str) -> None:
        print(m, flush=True)

    try:
        sec = load_secrets()
    except Exception:
        sec = {}
    try:
        receipt = push_text(str(sec.get("ssh_password") or ""), text, log=lg)
    except (PhoneError, ValueError, RuntimeError) as e:
        print("SELFTEST FAIL %s: %s" % (type(e).__name__, e), flush=True)
        return 1
    print("SELFTEST OK mechanism=%s %s sha=%s" % (
        receipt["mechanism"], lengths_line(text), receipt["sha256"][:12]),
        flush=True)
    return 0


class PasteApp:
    def __init__(self, root) -> None:
        import tkinter as tk
        from tkinter import scrolledtext, ttk

        self.root = root
        root.title("Paste Push — PC to phone note field")
        try:
            sec = load_secrets()
        except Exception:
            sec = {}

        top = ttk.Frame(root)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="SSH password:").pack(side="left")
        self.pw = ttk.Entry(top, width=24, show="•")
        self.pw.pack(side="left", padx=8)
        self.pw.insert(0, str(sec.get("ssh_password") or ""))

        help_txt = ("1. On the phone: open Notes, new note, tap the field. "
                    "2. Paste/type text below. 3. PUSH. "
                    "Types into YOUR open field; navigates nothing.")
        ttk.Label(root, text=help_txt, wraplength=560).pack(anchor="w",
                                                            padx=10)

        box_frame = ttk.Frame(root)
        box_frame.pack(fill="both", expand=True, padx=10, pady=4)
        self.box = scrolledtext.ScrolledText(box_frame, wrap="word",
                                             height=10, font=("Consolas", 10))
        self.box.pack(fill="both", expand=True)
        self.box.bind("<KeyRelease>", lambda _e: self._refresh_lengths())

        self.lengths_var = tk.StringVar(value=lengths_line(""))
        ttk.Label(root, textvariable=self.lengths_var).pack(anchor="w",
                                                             padx=10)

        btn_row = ttk.Frame(root)
        btn_row.pack(fill="x", padx=10, pady=4)
        self.push_btn = ttk.Button(btn_row, text="PUSH to phone field",
                                   command=self.on_push)
        self.push_btn.pack(side="left")
        self.status_var = tk.StringVar(value="idle — stage the phone field first")
        ttk.Label(btn_row, textvariable=self.status_var).pack(side="left",
                                                              padx=8)

        ttk.Label(root, text="Log:").pack(anchor="w", padx=10)
        self.log_box = scrolledtext.ScrolledText(root, wrap="word", height=10,
                                                 font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_box.configure(state="disabled")
        self._refresh_lengths()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        stay_awake()

    def on_close(self) -> None:
        allow_sleep()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _refresh_lengths(self) -> None:
        text = self.box.get("1.0", "end-1c")
        self.lengths_var.set(lengths_line(text))

    def _append_log(self, line: str) -> None:
        try:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        except Exception:
            pass

    def live_log(self, line: str) -> None:
        try:
            self.root.after(0, self._append_log, line)
        except Exception:
            pass

    def on_push(self) -> None:
        text = self.box.get("1.0", "end-1c")
        if not text:
            self.status_var.set("empty — nothing to push")
            return
        if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            self.status_var.set("over 1MB — refused")
            return
        try:
            self.push_btn.configure(state="disabled")
        except Exception:
            pass
        self.status_var.set("pushing — hands off the phone…")
        password = self.pw.get()

        def work() -> None:
            try:
                receipt = push_text(password, text, log=self.live_log)
            except Exception as e:
                self.live_log("PUSH FAIL %s: %s" % (type(e).__name__, e))

                def fail() -> None:
                    try:
                        self.push_btn.configure(state="normal")
                    except Exception:
                        pass
                    self.status_var.set("failed — see log")
                try:
                    self.root.after(0, fail)
                except Exception:
                    pass
                return

            def done() -> None:
                try:
                    self.push_btn.configure(state="normal")
                except Exception:
                    pass
                self.status_var.set("in phone field verified (%s)"
                                    % lengths_line(text))
            try:
                self.root.after(0, done)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--dump-field":
        return run_dump_field()
    if len(argv) >= 2 and argv[1] == "--selftest":
        return run_selftest(argv[2] if len(argv) >= 3 else "paste-push selftest")
    import tkinter as tk

    root = tk.Tk()
    PasteApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
