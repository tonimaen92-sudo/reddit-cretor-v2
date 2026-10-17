"""Helpers for stable app-module entry points.

The public modules stay intentionally tiny.  Importing one returns its
controller module so legacy monkeypatching and public symbols keep working.
Executing one delegates to the same controller as ``python -m``.
"""

from __future__ import annotations

import importlib
import runpy
import sys


def expose(controller: str, module_name: str) -> None:
    if module_name == "__main__":
        runpy.run_module(controller, run_name="__main__")
        return
    sys.modules[module_name] = importlib.import_module(controller)
