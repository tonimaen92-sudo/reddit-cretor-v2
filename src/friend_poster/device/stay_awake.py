# UTF-8
"""Keep the laptop awake while a GUI bat is open. Windows only, no-op
elsewhere. Prevents system sleep (screen may still dim/off); cleared
when the GUI closes (process exit clears it regardless).
"""

from __future__ import annotations

import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _api():
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return ctypes.windll.kernel32.SetThreadExecutionState
    except Exception:
        return None


def stay_awake() -> bool:
    """Block system sleep until allow_sleep() or process exit."""
    fn = _api()
    if fn is None:
        return False
    try:
        fn(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        return True
    except Exception:
        return False


def allow_sleep() -> None:
    """Release the sleep block. Best-effort."""
    fn = _api()
    if fn is None:
        return
    try:
        fn(ES_CONTINUOUS)
    except Exception:
        pass
