# UTF-8
"""NSFW 5+5 selection (step 2): momsonincest 5 + publicblowjobs 5.

Source: latest logs/supply_bank_*.json. Ranked by views, one per creator
(rotation breadth). Liveness-filtered via nsfw_liveness so day-one
postable: dead candidates replaced immediately from the next in rank.
Writes logs/nsfw_5plus5.json (sub/slug/url/creator/views/published only).

  python misc/nsfw_select.py
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
from friend_poster.core.config import LOG_DIR  # noqa: E402
from friend_poster.services.nsfw_liveness import check_many  # noqa: E402

TARGETS = (("momsonincest", 5), ("publicblowjobs", 5))


def latest_bank() -> dict:
    fs = sorted(glob.glob(os.path.join(LOG_DIR, "supply_bank_*.json")))
    if not fs:
        raise RuntimeError("no supply_bank file")
    return json.load(open(fs[-1], encoding="utf-8"))


def pick_live(sub: str, need: int, clips: list[dict], log=print) -> list[dict]:
    pool = sorted(
        (c for c in clips if c.get("sub") == sub and c.get("slug")),
        key=lambda c: -((c.get("views") or 0)),
    )
    # One per creator for breadth; relax to duplicates only if exhausted.
    seen: set[str] = set()
    ranked: list[dict] = []
    overflow: list[dict] = []
    for c in pool:
        cr = str(c.get("creator") or "").lower()
        if cr and cr not in seen:
            seen.add(cr)
            ranked.append(c)
        else:
            overflow.append(c)
    ranked += overflow
    chosen: list[dict] = []
    idx = 0
    while len(chosen) < need and idx < len(ranked):
        # Check next few candidates in one paced batch, take live ones.
        batch = ranked[idx:idx + need - len(chosen) + 2]
        idx += len(batch)
        verdicts = check_many(
            [str(c.get("slug") or "").lower() for c in batch], log=log)
        live = {v["slug"]: v for v in verdicts if v.get("live")}
        for c in batch:
            if len(chosen) >= need:
                break
            slug = str(c.get("slug") or "").lower()
            if slug in live:
                v = live[slug]
                chosen.append({
                    "sub": sub,
                    "slug": slug,
                    "url": "https://www.redgifs.com/watch/%s/" % slug,
                    "creator": c.get("creator") or "",
                    "views": v.get("views", c.get("views")),
                    "published": v.get("published", ""),
                    "bank_views": c.get("views"),
                })
        dead = len(batch) - len([c for c in batch
                                 if str(c.get("slug") or "").lower() in live])
        if dead:
            log("%s: replaced %d dead, continuing" % (sub, dead))
    if len(chosen) < need:
        raise RuntimeError("%s: only %d/%d live" % (sub, len(chosen), need))
    return chosen


def main() -> int:
    bank = latest_bank()
    clips = bank.get("clips") or []
    log = print
    all_rows: list[dict] = []
    for sub, need in TARGETS:
        all_rows.extend(pick_live(sub, need, clips, log=log))
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out = {"stamp": stamp, "targets": dict(TARGETS), "posts": all_rows}
    path = os.path.join(LOG_DIR, "nsfw_5plus5.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")
    log("WROTE logs/nsfw_5plus5.json (%d posts)" % len(all_rows))
    for r in all_rows:
        log("%s | %s | views=%s | age=%s" % (
            r["sub"], r["creator"] or "?", r.get("views"),
            r.get("published") or "?"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
