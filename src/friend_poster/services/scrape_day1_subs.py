# UTF-8
"""Day-1 comment-sub scrape for purchased-account listings.

Same recipe as FARMED_STRAT_GUIDE.pdf: JSON via bot Chrome, no scroll.
Per user: comments.json (oldest first) + about.json (created_utc, karma).
Output: logs/day1_subs_<stamp>.json + logs/day1_subs/raw_<user>.json.

  python misc/scrape_day1_subs.py
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

USERS = [
    "Nervous-Cut-3761",
    "Separate-Bad-1573",
    "Winter-Pudding4479",
    "Complex-Cover9795",
    "Lonely_Drop6163",
    "Jumpy-Ear-9882",
    "TodayOk4724",
    "Sea_Bet_6292",
    "Critical_Instance732",
    "Deep-Ad9069",
    "SandQuiet7800",
    "External-Injury5929",
    "Clean_Confidence_258",
    "Objective_Mode1637",
    "Western-Broccoli-438",
]


def fetch_json(page, url: str):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    try:
        body = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception as e:
        return None, "evaluate: %s" % str(e)[:100]
    try:
        return json.loads(body), ""
    except Exception:
        return None, "non-JSON (%s)" % (body or "")[:80]


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
            for i, user in enumerate(USERS):
                row: dict = {"user": user}
                comments, err = fetch_json(
                    page, "https://www.reddit.com/user/%s/comments.json?limit=100" % user)
                if comments is None:
                    row["comments_error"] = err
                    print("--- %s: comments FAIL %s" % (user, err), flush=True)
                else:
                    kids = (comments.get("data") or {}).get("children") or []
                    row["comments_n"] = len(kids)
                    row["comments"] = [
                        {"sub": ((k.get("data") or {}).get("subreddit") or ""),
                         "created_utc": (k.get("data") or {}).get("created_utc"),
                         "body": ((k.get("data") or {}).get("body") or "")[:200],
                         "score": (k.get("data") or {}).get("score")}
                        for k in kids
                    ]
                    print("--- %s: %d comments" % (user, len(kids)), flush=True)
                time.sleep(3.0)
                about, err = fetch_json(
                    page, "https://www.reddit.com/user/%s/about.json" % user)
                if about is None:
                    row["about_error"] = err
                    print("--- %s: about FAIL %s" % (user, err), flush=True)
                else:
                    data = about.get("data") or {}
                    row["created_utc"] = data.get("created_utc")
                    row["comment_karma"] = data.get("comment_karma")
                    row["link_karma"] = data.get("link_karma")
                    print("--- %s: created=%s ckarma=%s lkarma=%s" % (
                        user, data.get("created_utc"), data.get("comment_karma"),
                        data.get("link_karma")), flush=True)
                with open(os.path.join(outdir, "raw_%s.json" % user.lower()),
                          "w", encoding="utf-8") as f:
                    json.dump(row, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                summary["users"][user] = {
                    "comments_n": row.get("comments_n", 0),
                    "created_utc": row.get("created_utc"),
                    "comment_karma": row.get("comment_karma"),
                    "link_karma": row.get("link_karma"),
                    "comments_error": row.get("comments_error", ""),
                    "about_error": row.get("about_error", ""),
                }
                time.sleep(3.0)
        finally:
            try:
                if owned:
                    ctx.close()
            except Exception:
                pass
    with open(os.path.join(outdir, "summary_%s.json" % stamp),
              "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("WROTE logs/day1_subs/summary_%s.json" % stamp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
