# UTF-8
"""Project-scoped, process-lifetime lock for batch ownership.

The durable batch state records a per-process ``writer_id``.  On its own that
id cannot tell a live second Creator from the stale id a crashed or restarted
Creator left behind, which is why a restart currently blocks Start Batch and
Save & Extend with a foreign-writer refusal.

Ownership-changing batch operations therefore also require this advisory OS
lock, whose file lives beside the project's batch state.  It is taken once per
process and held for the whole process lifetime; the operating system drops it
the instant the process exits (normally, by crash, or by kill), so a dead
Creator can never leave the batch permanently locked, while the restarted
Creator can prove the old owner is gone and safely take its writer id over.

Advisory note: this coordinates processes on ONE machine.  When the project
folder is synced to another machine (for example OneDrive), the durable
revision CAS remains the cross-machine protection; the lock alone cannot prove
liveness there, so a foreign writer on a genuinely different machine is still
only taken over from a process that holds this lock.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

from friend_poster.core.config import LOG_DIR

LOCK_PATH = Path(LOG_DIR) / "bulk_creator.lock"

_handle = None
_held_path: Path | None = None


def _try_os_lock(handle) -> bool:
    """Take a non-blocking exclusive OS lock on one byte of ``handle``."""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def acquire_bulk_owner_lock(path: Path | None = None) -> bool:
    """Take the process-lifetime ownership lock; idempotent within a process.

    Returns True when this process holds the lock (now or already) and False
    when another live Creator process holds it — in which case this process
    must not perform any ownership-changing batch operation.  Failure to open
    or lock the file is reported as False so the caller fails closed.
    """
    global _handle, _held_path
    if _handle is not None:
        return True
    target = Path(path) if path is not None else LOCK_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = open(target, "a+b")
    except OSError:
        return False
    if not _try_os_lock(handle):
        try:
            handle.close()
        except OSError:
            pass
        return False
    _handle, _held_path = handle, target
    # Informational holder stamp only; liveness comes from the OS lock above.
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(
            f"{os.getpid()} {socket.gethostname()}\n".encode("ascii", "replace")
        )
        handle.flush()
    except OSError:
        pass
    return True


def bulk_owner_lock_held() -> bool:
    """True while THIS process holds the project ownership lock."""
    return _handle is not None


def release_bulk_owner_lock() -> None:
    """Drop the lock; the operating system would release it at exit anyway."""
    global _handle, _held_path
    handle, _handle, _held_path = _handle, None, None
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass
