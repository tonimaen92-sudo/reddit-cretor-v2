"""Read-only catalog helpers for the Poster UI."""

from __future__ import annotations

import json


def community_display(path: str, sub: str) -> str:
    want = (sub or "").strip().lower()
    if not want:
        return (sub or "").strip()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            for subs in data.values():
                for stored in subs or []:
                    value = str(stored or "").strip()
                    if value and value.lower() == want:
                        return value
    except (OSError, ValueError):
        pass
    return (sub or "").strip()


def load_sub_map(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {
                str(key).strip().lower(): [
                    str(sub).strip().lower()
                    for sub in (values or [])
                    if str(sub or "").strip()
                ]
                for key, values in data.items()
            }
    except (OSError, ValueError):
        pass
    return {}
