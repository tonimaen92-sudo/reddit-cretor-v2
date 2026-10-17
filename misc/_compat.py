"""Legacy-path bootstrap for the reorganized friend_poster package."""
from __future__ import annotations

import importlib
import runpy
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def dispatch(target: str, namespace: dict) -> None:
    """Run a canonical module or alias an imported legacy module to it."""
    caller_name = str(namespace.get("__name__", ""))
    if caller_name == "__main__":
        runpy.run_module(target, run_name="__main__")
        return
    sys.modules[caller_name] = importlib.import_module(target)
