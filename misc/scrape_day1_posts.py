# UTF-8
"""Day-1 submitted-post scrape for purchased-account listings.

Same recipe: JSON via bot Chrome, no scroll. Per user submitted.json.
Output: logs/day1_subs/post_raw_<user>.json + post_summary_<stamp>.json.

  python misc/scrape_day1_posts.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from comment_precache import (  # noqa: E402
    BOT_PROFILE_DIR,
    open_scout_context,
)
from config import LOG_DIR  # noqa: E402
from scrape_day1_subs import USERS, fetch_json  # noqa: E402


def main() -> int:
    from playwright.sync_api import sync_playwright
    if not os.path.isdir(BOT_PROFILE_DIR):
        print("bot Chrome profile missing")
        return 1
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    outdir = os.path.join(LOG_DIR, "day1_subs")
    os.makedirs(outdir, exist_ok=True)
    summary = {"stamp": stamp, "users": {}}
    with sync_playwright() as pw:
        ctx, owned = open_scout_context(pw, print)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            for user in USERS:
                posts, err = fetch_json(
                    page, "https://www.reddit.com/user/%s/submitted.json?limit=100" % user)
                if posts is None:
                    print("--- %s: FAIL %s" % (user, err), flush=True)
                    summary["users"][user] = {"posts_n": 0, "error": err}
                else:
                    kids = (posts.get("data") or {}).get("children") or []
                    rows = []
                    for k in kids:
                        d = k.get("data") or {}
                        url = d.get("url") or ""
                        rows.append({
                            "sub": d.get("subreddit") or "",
                            "title": (d.get("title") or "")[:160],
                            "url": url[:200],
                            "is_redgifs": ("redgifs" in url.lower()),
                            "is_video": bool(d.get("is_video")),
                            "is_self": bool(d.get("is_self")),
                            "score": d.get("score"),
                            "comments": d.get("num_comments"),
                            "created_utc": d.get("created_utc"),
                        })
                    with open(os.path.join(outdir, "post_raw_%s.json" % user.lower()),
                              "w", encoding="utf-8") as f:
                        json.dump({"user": user, "posts": rows}, f, indent=2,
                                  ensure_ascii=False)
                        f.write("\n")
                    summary["users"][user] = {"posts_n": len(rows)}
                    print("--- %s: %d posts" % (user, len(rows)), flush=True)
                time.sleep(3.0)
        finally:
            try:
                if owned:
                    ctx.close()
            except Exception:
                pass
    with open(os.path.join(outdir, "post_summary_%s.json" % stamp),
              "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("WROTE post_summary_%s.json" % stamp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
