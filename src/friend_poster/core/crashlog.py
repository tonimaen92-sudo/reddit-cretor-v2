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


def _write(kind: str, text: str) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(CRASH_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n%s\n" % (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind, text))
    except OSError:
        pass


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
