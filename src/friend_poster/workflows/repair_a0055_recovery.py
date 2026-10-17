"""One-shot evidence-checked repair for the interrupted a0055 batch."""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from friend_poster.core.bulk_creation import archive_state, empty_state, load_draft, load_state, save_state
from friend_poster.core.config import LOG_DIR
from friend_poster.reddit.reddit_interests import (
    _save_customize_checkpoint,
    _selected_customize_topics,
    customize_screen,
    customize_topic_label,
    customize_topics,
)
from friend_poster.core.slots import get_slot, slot_reddit_ready
from friend_poster.core.paths import PROJECT_ROOT
from friend_poster.device.ui_tree import parse_nodes

PROJECT_DIR = PROJECT_ROOT
DUMP_DIR = PROJECT_DIR / "dumps"


def main() -> int:
    a0054 = get_slot("a0054")
    a0055 = get_slot("a0055")
    if not a0054 or not slot_reddit_ready(a0054):
        raise SystemExit("Refused: a0054 is not independently proven complete.")
    if not a0055 or a0055.get("reddit") is True:
        raise SystemExit("Refused: a0055 is missing or already complete.")
    if a0055.get("reddit_birth_date") != "31.07.2003":
        raise SystemExit("Refused: a0055 birth checkpoint differs from the queue evidence.")
    if not all(
        str(a0055.get(field) or "").strip()
        for field in (
            "username", "password", "email", "email_label",
            "ghost_container_instance", "falcon_proxy_verified_ip",
            "falcon_proxy_verified_at_utc",
        )
    ):
        raise SystemExit("Refused: an a0055 durable identity checkpoint is missing.")

    fresh_path = DUMP_DIR / "a0055_recovery_fresh.json"
    before_path = DUMP_DIR / "925_customize_verified_3.json"
    if not fresh_path.exists() or not before_path.exists():
        raise SystemExit("Refused: required fresh or pre-tap evidence is missing.")
    fresh = parse_nodes(fresh_path.read_text(encoding="utf-8"))
    before = parse_nodes(before_path.read_text(encoding="utf-8"))
    if not customize_screen(fresh):
        raise SystemExit("Refused: phone is not on the proven Customize screen.")
    smartphone = next(
        (
            node for node in customize_topics(before)
            if customize_topic_label(node) == "smartphones"
        ),
        None,
    )
    if smartphone is None:
        raise SystemExit("Refused: the canonical pre-tap Smartphones pill is missing.")
    pending = {
        "label": "smartphones",
        "bounds": [smartphone.x, smartphone.y, smartphone.w, smartphone.h],
    }
    expected = {"baking", "pr§ut", "world news", "smartphones"}
    selected = _selected_customize_topics(
        fresh,
        "a0055_recovery_fresh",
        expected,
        pending=pending,
    )
    if selected != expected:
        raise SystemExit("Refused: fresh screen does not prove exactly four expected topics.")

    draft = load_draft()
    queue = copy.deepcopy(draft.get("baseline_queue") or [])
    if len(queue) < 2 or queue[0].get("birth_date") != "31.07.2003":
        raise SystemExit("Refused: durable draft does not begin with a0055's queue row.")
    if queue[0].get("username") != a0055.get("username"):
        raise SystemExit("Refused: a0055 requested username differs from the durable draft.")

    current = load_state()
    archive = archive_state(current)
    repaired = empty_state()
    repaired.update(
        revision=current["revision"] + 1,
        run_id=str(uuid.uuid4()),
        queue=queue,
        requested=8,
        remaining=2,
        stage="paused",
        slot_id="a0055",
        baseline_ip=str(a0054["falcon_proxy_verified_ip"]),
        verified_ip=str(a0055["falcon_proxy_verified_ip"]),
        verified_at_utc=str(a0055["falcon_proxy_verified_at_utc"]),
        failure_phase="creation",
        failure_reason=(
            "Recovered a0055 Customize checkpoint after one bounded OCR drift."
        ),
    )
    save_state(repaired)
    checkpoint = {
        "version": 2,
        "slot_id": "a0055",
        "initial": ["food", "technology", "news", "travel"],
        "customize": sorted(expected),
        "pending_customize": pending,
    }
    _save_customize_checkpoint("a0055", checkpoint)
    print(f"Repaired a0055 with two remaining items; archived {archive.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
