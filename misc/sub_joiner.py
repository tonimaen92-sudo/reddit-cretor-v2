# UTF-8
"""GUI: sub quick-opener. Click a sub = uiopen on the phone, nothing else.
Tap Join on the phone yourself, click the sub again = marked joined.
State is one global list in logs/sub_joins.json.

  sub_joiner.bat
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import scrolledtext, ttk

MISC_DIR = os.path.dirname(os.path.abspath(__file__))
if MISC_DIR not in sys.path:
    sys.path.insert(0, MISC_DIR)

from comment_precache import PHASE2  # noqa: E402 (single source of truth)
from config import LOG_DIR  # noqa: E402
from ssh_device import Phone, PhoneError, diagnose_usb, usb_status_line  # noqa: E402
from ui_tree import TapStack  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

JOINS_PATH = os.path.join(LOG_DIR, "sub_joins.json")
JOIN_LOG_PATH = os.path.join(LOG_DIR, "sub_joiner.log")


def load_joins() -> list:
    try:
        d = json.load(open(JOINS_PATH, encoding="utf-8"))
        if isinstance(d, dict):
            got = d.get("joined")
            if isinstance(got, list):
                return [str(s) for s in got]
        if isinstance(d, list):
            return [str(s) for s in d]
    except (OSError, ValueError):
        pass
    return []


def save_joins(subs: list) -> None:
    try:
        with open(JOINS_PATH, "w", encoding="utf-8") as f:
            json.dump({"joined": sorted(set(subs))}, f, indent=2,
                      ensure_ascii=False)
            f.write("\n")
    except OSError:
        pass


class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Sub opener (11 subs, manual Join)")
        self.root.geometry("480x600")
        self.log_q: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.phone: Phone | None = None
        self.stack = None
        self._pending: str | None = None

        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="USB: probing…")
        ttk.Label(frm, textvariable=self.status,
                  wraplength=440).pack(fill="x", padx=8, pady=4)

        row = ttk.Frame(frm)
        row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text="SSH password (not git):").pack(side="left")
        self.pw = ttk.Entry(row, show="*", width=20)
        self.pw.pack(side="left", padx=6)

        ttk.Label(frm, wraplength=440,
                  text="Click a sub = uiopen on the phone. Tap Join on the "
                  "phone yourself, then click the sub again = marked joined."
                  ).pack(fill="x", padx=8, pady=4)

        self.btns: dict[str, ttk.Button] = {}
        grid = ttk.Frame(frm)
        grid.pack(fill="both", expand=True, padx=8, pady=4)
        for i, sub in enumerate(PHASE2):
            b = ttk.Button(grid, text="r/" + sub, width=24,
                           command=lambda s=sub: self.on_sub(s))
            b.grid(row=i // 2, column=i % 2, sticky="we", padx=4, pady=3)
            self.btns[sub] = b
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        self.log_box = scrolledtext.ScrolledText(frm, wrap="word", height=8,
                                                 font=("Consolas", 10))
        self.log_box.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_box.configure(state="disabled")

        self._paint_buttons()
        self.root.after(200, self._probe_usb_idle)
        self.root.after(120, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- plumbing ----
    def log(self, msg: str) -> None:
        try:
            self.log_q.put(msg)
        except Exception:
            pass

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self.log_q.get_nowait()
                stamp = datetime.now().strftime("%H:%M:%S")
                self.log_box.configure(state="normal")
                self.log_box.insert("end", f"{stamp}  {msg}\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
                try:
                    with open(JOIN_LOG_PATH, "a", encoding="utf-8") as f:
                        f.write(f"{stamp}  {msg}\n")
                except OSError:
                    pass
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    def _probe_usb_idle(self) -> None:
        def work() -> None:
            try:
                report = diagnose_usb()
                self.status.set(usb_status_line(report))
            except Exception as e:
                self.log(f"USB idle probe: {type(e).__name__}: {e}")
        threading.Thread(target=work, daemon=True).start()

    def _start_worker(self, target, *args) -> None:
        if self.worker and self.worker.is_alive():
            self.log("Busy, wait.")
            return
        self.stop.clear()
        self.worker = threading.Thread(target=target, args=args, daemon=True)
        self.worker.start()

    def _ensure_connection(self, password: str):
        """One persistent connection for the session. Reconnects if dead."""
        if self.phone is not None:
            try:
                self.phone.exec_ok("echo alive", timeout=8)
                return self.phone, self.stack
            except Exception:
                self._drop_connection()
        phone = Phone(log=self.log)
        self.phone = phone
        self.log("USB SSH… (once, reused for every click)")
        phone.connect(password, also_wda=False)
        stack = TapStack(phone, self.log)
        stack.attach()
        self.stack = stack
        return phone, stack

    def _drop_connection(self) -> None:
        try:
            if self.phone:
                self.phone.close()
        except Exception:
            pass
        self.phone = None
        self.stack = None

    def _paint_buttons(self) -> None:
        joined = load_joins()
        for sub, btn in self.btns.items():
            mark = "✓ " if sub in joined else ""
            try:
                btn.configure(text=mark + "r/" + sub)
            except (tk.TclError, AttributeError):
                pass

    # ---- uiopen + mark ----
    def on_sub(self, sub: str) -> None:
        if self._pending == sub:
            joined = load_joins()
            if sub not in joined:
                joined.append(sub)
            save_joins(joined)
            self._pending = None
            self._paint_buttons()
            self.log(f"Marked r/{sub} joined.")
            return
        self._start_worker(self._run_open, self.pw.get(), sub)

    def _run_open(self, password: str, sub: str) -> None:
        try:
            phone, _stack = self._ensure_connection(password)
            url = "https://www.reddit.com/r/%s/" % sub
            code, out = phone.exec_ok("uiopen --url '%s'" % url, timeout=15)
            if code != 0:
                self.log("ENTRY dispatch failed (%s)."
                         % (out or "").strip()[:80])
                return
            time.sleep(2.5)
            self._pending = sub
            self.log(f"r/{sub} open — tap Join on the phone, "
                     f"then click r/{sub} again.")
        except PhoneError as e:
            self._drop_connection()
            self.log("ERROR  " + str(e))
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")

    def on_close(self) -> None:
        self.stop.set()
        self._drop_connection()
        self.root.destroy()


def main() -> int:
    app = App()
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
