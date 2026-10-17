"""File persistence used by the Creator UI.

Paths are supplied by the controller so tests and legacy callers can continue
to patch the public path constants on ``friend_poster.apps.creator``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def load_qa_swipes(path: str, default: int = 14) -> int:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = int(json.load(handle).get("swipes", default))
    except (OSError, ValueError, TypeError, AttributeError):
        value = default
    return max(14, min(value, 250))


def save_qa_swipes(path: str, log_dir: str, value: int) -> None:
    value = max(14, min(int(value), 250))
    os.makedirs(log_dir, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"swipes": value}, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def load_post_settings(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {"communities": [], "selected": ""}
    except (OSError, ValueError):
        return {"communities": [], "selected": ""}
    raw_list = data.get("communities")
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, dict):
                community = str(item.get("community") or "").strip()
                niche = str(item.get("niche") or "").strip()
                flair = str(item.get("flair") or "").strip()[:64]
            else:
                community = str(item or "").strip()
                niche = ""
                flair = ""
            if not community or community.lower() in seen:
                continue
            seen.add(community.lower())
            cleaned.append({"community": community, "niche": niche, "flair": flair})
    legacy = str(data.get("selected") or data.get("community") or "").strip()
    if legacy and legacy.lower() not in seen:
        cleaned.insert(0, {"community": legacy, "niche": ""})
    selected = str(data.get("selected") or legacy or "").strip()
    return {"communities": cleaned, "selected": selected}


def save_post_settings(path: str, log_dir: str, communities: list[dict[str, str]], selected: str = "") -> None:
    try:
        os.makedirs(log_dir, exist_ok=True)
        cleaned: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in communities or []:
            if not isinstance(item, dict):
                continue
            community = str(item.get("community") or "").strip()
            niche = str(item.get("niche") or "").strip()
            flair = str(item.get("flair") or "").strip()[:64]
            if not community or community.lower() in seen:
                continue
            seen.add(community.lower())
            cleaned.append({"community": community, "niche": niche, "flair": flair})
        selected = (selected or "").strip()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {"community": selected, "selected": selected, "communities": cleaned},
                handle,
                indent=2,
            )
            handle.write("\n")
    except OSError:
        pass


def write_creator_state(path: str, log_dir: str, state: dict) -> str:
    try:
        os.makedirs(log_dir, exist_ok=True)
        payload = dict(state)
        payload["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        return path
    except OSError:
        return ""
