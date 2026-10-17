"""Single-child Creator watchdog; it never changes phone or batch state itself."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

from friend_poster.core.paths import LEGACY_MISC_DIR, PROJECT_ROOT

MISC_DIR = LEGACY_MISC_DIR
PROJECT_DIR = PROJECT_ROOT

from friend_poster.core.bulk_creation import BulkError, load_state  # noqa: E402

_HANDLE = None
ACTIVE_STAGES = {
    "ip_pending", "may_have_triggered", "ip_verified", "creating",
    "between", "retry_ip", "retry_creation",
}


def acquire_watchdog() -> bool:
    global _HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\DataLostCreatorWatchdog")
    if not handle:
        return True
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        return False
    _HANDLE = handle
    return True


def should_relaunch() -> bool:
    try:
        state = load_state()
    except BulkError:
        return False
    if state.get("stage") in ACTIVE_STAGES:
        return True
    recovery = state.get("recovery") or {}
    return recovery.get("status") == "recovering"


def child_command(*, resume: bool) -> list[str]:
    python = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    command = [str(python), str(MISC_DIR / "creator.py"), "--watchdog-child"]
    if resume:
        command.append("--resume-batch")
    return command


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not acquire_watchdog():
        return 2
    resume_once = "--resume-once" in argv
    first = True
    while True:
        if not ((first and resume_once) or should_relaunch()):
            return 0
        child = subprocess.Popen(
            child_command(resume=(first and resume_once) or should_relaunch()),
            cwd=str(PROJECT_DIR),
        )
        first = False
        child.wait()
        # A manual Stop/Close persists stopped before the visible child exits.
        if not should_relaunch():
            return child.returncode
        time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())
