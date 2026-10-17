# UTF-8
"""Supply bank builder: proven-sub top listings -> redgifs URLs + scores,
then creator attribution, engagement select, creator-breadth cap.

  python misc/source_bank.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from friend_poster.services.comment_precache import (  # noqa: E402
    BOT_PROFILE_DIR,
    open_scout_context,
)
from friend_poster.core.config import LOG_DIR  # noqa: E402
from friend_poster.services.scrape_day1_subs import fetch_json  # noqa: E402
from friend_poster.services.redgifs_attr import API, _get  # noqa: E402

SUBS = [
    "publicblowjobs", "gothblowjobs", "tittydrop", "momsonincest",
    "bimbo_pornstars", "trulybigtits", "ignore_my_pussy", "blackedpawgs",
    "asshole_closeup", "daddysdarkfiction", "analvidz", "gawkgawkgawk",
    "faceofpain", "peeandsquirtgifs", "pissdrinkingsluts", "deepthroattears",
    "couplesporn",
]
PER_CREATOR_CAP = 2


def known_slugs() -> set:
    import glob
    known = set()
    for f in glob.glob(os.path.join(LOG_DIR, "day1_subs", "post_raw_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for p in d.get("posts", []):
            u = p.get("url") or ""
            if "/watch/" in u:
                known.add(u.split("/watch/")[-1].split("?")[0].strip("/").lower())
    return known


def stage_pull() -> int:
    from playwright.sync_api import sync_playwright
    if not os.path.isdir(BOT_PROFILE_DIR):
        print("bot Chrome profile missing")
        return 1
    known = known_slugs()
    print("KNOWN slugs (skip):", len(known))
    pulls = []
    with sync_playwright() as pw:
        ctx, owned = open_scout_context(pw, print)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            for sub in SUBS:
                data, err = fetch_json(
                    page, "https://www.reddit.com/r/%s/top.json?sort=top&t=month&limit=50" % sub)
                if data is None:
                    print("--- r/%s FAIL %s" % (sub, err), flush=True)
                    continue
                n = 0
                for k in (data.get("data") or {}).get("children") or []:
                    d = k.get("data") or {}
                    url = d.get("url") or ""
                    if "redgifs" not in url.lower() or "/watch/" not in url:
                        continue
                    slug = url.split("/watch/")[-1].split("?")[0].strip("/").lower()
                    if not slug or slug in known:
                        continue
                    pulls.append({"sub": sub, "slug": slug,
                                  "url": url.split("?")[0],
                                  "title": (d.get("title") or "")[:120],
                                  "score": d.get("score") or 0})
                    n += 1
                print("--- r/%s: %d new redgifs" % (sub, n), flush=True)
                time.sleep(3.0)
        finally:
            try:
                if owned:
                    ctx.close()
            except Exception:
                pass
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    with open(os.path.join(LOG_DIR, "supply_pulls_%s.json" % stamp),
              "w", encoding="utf-8") as f:
        json.dump({"stamp": stamp, "pulls": pulls}, f, indent=2,
                  ensure_ascii=False)
        f.write("\n")
    print("PULLED", len(pulls), "-> logs/supply_pulls_%s.json" % stamp)
    return 0


def stage_attr(limit: int = 60) -> int:
    import glob
    pulls_files = sorted(glob.glob(os.path.join(LOG_DIR, "supply_pulls_*.json")))
    if not pulls_files:
        print("no pulls file, run --stage pull first")
        return 1
    pulls = json.load(open(pulls_files[-1], encoding="utf-8"))["pulls"]
    ckpt_path = os.path.join(LOG_DIR, "supply_attr_checkpoint.json")
    done = {}
    try:
        if os.path.isfile(ckpt_path):
            done = json.load(open(ckpt_path, encoding="utf-8"))
    except Exception:
        done = {}
    todo = [p for p in pulls if p["slug"] not in done][:max(1, limit)]
    print("TODO", len(todo), "DONE already", len(done))
    if not todo:
        print("nothing left")
        return 0
    token = _get(API + "/auth/temporary").get("token", "")
    if not token:
        print("NO TOKEN")
        return 1
    counts = {}
    for i, p in enumerate(todo):
        try:
            g = (_get("%s/gifs/%s" % (API, p["slug"]), token).get("gif") or {})
            creator = g.get("userName") or ""
            done[p["slug"]] = {**p, "creator": creator,
                               "views": g.get("views"), "likes": g.get("likes"),
                               "tags": g.get("tags") or []}
            print("%d/%d %s %s views=%s" % (
                i + 1, len(todo), p["slug"][:28], creator or "?",
                g.get("views")), flush=True)
        except Exception as e:
            done[p["slug"]] = {**p, "creator": "", "error": str(e)[:120]}
            print("%d/%d %s FAIL %s" % (i + 1, len(todo), p["slug"][:28],
                                        str(e)[:80]), flush=True)
        with open(ckpt_path, "w", encoding="utf-8") as f:
            json.dump(done, f, indent=2, ensure_ascii=False)
            f.write("\n")
        time.sleep(1.5)
    bank = [r for r in done.values() if r.get("creator")]
    bank.sort(key=lambda r: -((r.get("views") or 0)))
    final, counts = [], {}
    for r in bank:
        if counts.get(r["creator"], 0) >= PER_CREATOR_CAP:
            continue
        counts[r["creator"]] = counts.get(r["creator"], 0) + 1
        final.append(r)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    with open(os.path.join(LOG_DIR, "supply_bank_%s.json" % stamp),
              "w", encoding="utf-8") as f:
        json.dump({"stamp": stamp, "subs": SUBS, "clips": final}, f, indent=2,
                  ensure_ascii=False)
        f.write("\n")
    print("BANKED", len(final), "clips -> logs/supply_bank_%s.json" % stamp)
    return 0


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--stage", default="pull", choices=("pull", "attr"))
    _p.add_argument("--limit", type=int, default=60)
    _a = _p.parse_args()
    raise SystemExit(stage_attr(_a.limit) if _a.stage == "attr"
                     else stage_pull())
