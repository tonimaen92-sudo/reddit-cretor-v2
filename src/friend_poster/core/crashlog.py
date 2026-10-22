# UTF-8
"""Crash capture: uncaught exceptions (main + worker threads) go to
logs/crash.log with timestamps. The black console window is no longer
the only copy. No phone, no GUI at import."""

from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime

from friend_poster.core.paths import PROJECT_ROOT

LOG_DIR = str(PROJECT_ROOT / "logs")
CRASH_PATH = os.path.join(LOG_DIR, "crash.log")
_INSTALLED = False
_SINKS: "list" = []


def add_sink(callback) -> None:
    """Register a callback(kind, text) that also receives captured crashes.

    Sinks are optional observers (e.g. the Creator diagnostic panel).  A broken
    sink must never break crash-file handling or the process, so every call is
    guarded and duplicate registrations are ignored.
    """
    if callback not in _SINKS:
        _SINKS.append(callback)


def remove_sink(callback) -> None:
    try:
        _SINKS.remove(callback)
    except ValueError:
        pass


def _notify(kind: str, text: str) -> None:
    for callback in list(_SINKS):
        try:
            callback(kind, text)
        except Exception:
            pass


def _write(kind: str, text: str) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(CRASH_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n%s\n" % (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind, text))
    except OSError:
        pass
    _notify(kind, text)


def _excepthook(etype, value, tb) -> None:
    try:
        _write("UNCAUGHT",
               "".join(traceback.format_exception(etype, value, tb)))
    except Exception:
        pass
    try:
        sys.__excepthook__(etype, value, tb)
    except Exception:
        pass


def _thread_hook(args) -> None:
    try:
        _write("THREAD %s" % getattr(args.thread, "name", "?"),
               "".join(traceback.format_exception(
                   args.exc_type, args.exc_value, args.exc_traceback)))
    except Exception:
        pass


def install() -> bool:
    """Install process-wide crash capture. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return True
    _INSTALLED = True
    try:
        sys.excepthook = _excepthook
        threading.excepthook = _thread_hook
        _write("START", "crash capture on")
        return True
    except Exception:
        return False
