"""Durable settings and debug-log handling for the Commenter UI."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def open_debug_log(path: str, log_dir: str) -> None:
    try:
        os.makedirs(log_dir, exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) > 5 * 1024 * 1024:
            backup = path + ".prev"
            try:
                if os.path.isfile(backup):
                    os.remove(backup)
            except OSError:
                pass
            os.replace(path, backup)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                "===== %s =====\n"
                % datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
    except OSError:
        pass


def load_settings(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_settings(path: str, log_dir: str, selected_slot: str = "") -> None:
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"selected_slot": (selected_slot or "").strip()}, handle, indent=2)
            handle.write("\n")
    except OSError:
        pass
