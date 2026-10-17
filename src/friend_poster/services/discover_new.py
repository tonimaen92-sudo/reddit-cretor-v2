# UTF-8
"""New-clip discovery per target sub (newest-first redgifs search).

Teen and underage-adjacent tags are banned from all queries, no exceptions.
Output: logs/discover_<stamp>.json (per-sub banks).

  python misc/discover_new.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from friend_poster.core.config import LOG_DIR  # noqa: E402

API = "https://api.redgifs.com/v2"

SUB_TAGS = {
    "momsonincest": ["Mom", "Son", "Cheating", "Caption"],
    "daddysdarkfiction": ["Daddy", "Amateur", "Pussy", "Caption"],
    "publicblowjobs": ["Blowjob", "Public", "Amateur", "Big Dick"],
    "gothblowjobs": ["Blowjob", "Amateur", "Homemade", "Goth"],
    "tittydrop": ["Big Tits", "Tits", "Amateur", "Boobs"],
    "trulybigtits": ["Big Tits", "Amateur", "Tits", "Babe"],
    "blackedpawgs": ["BBC", "Doggystyle", "Interracial", "POV"],
    "analvidz": ["Anal", "Homemade", "Amateur", "Hardcore"],
    "ignore_my_pussy": ["Anal", "Big Dick", "Homemade", "Hardcore"],
    "faceofpain": ["Anal", "Hardcore", "Rough", "BDSM"],
    "gawkgawkgawk": ["Deepthroat", "Sloppy", "Throat Fuck", "Blowjob"],
    "deepthroattears": ["Deepthroat", "Throat Fuck", "Face Fuck", "Hard"],
    "peeandsquirtgifs": ["Squirting", "Squirt", "Pee", "Amateur"],
    "pissdrinkingsluts": ["Pee", "Piss", "Submissive", "Golden Shower"],
    "bimbo_pornstars": ["Big Tits", "Fake Tits", "Bimbo", "Latina"],
    "asshole_closeup": ["Ass", "Amateur", "Asshole", "Pussy"],
    "couplesporn": ["Amateur", "OnlyFans", "Ass", "Babe"],
    "titfuck": ["Big Tits", "MILF", "Redhead"],
    "amateursdoingit": ["Amateur", "Homemade", "Babe"],
}
BANNED = ("teen", "lolita", "schoolgirl", "barely", "young", "jailbait", "tinyteen")
PAGES = 2
PER_PAGE = 50


def _get(url: str, token: str = ""):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Authorization": "Bearer %s" % token,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def clean_tag(t: str) -> str:
    low = (t or "").lower()
    if any(b in low for b in BANNED):
        return ""
    return (t or "").strip()


def main() -> int:
    token = _get(API + "/auth/temporary").get("token", "")
    if not token:
        print("NO TOKEN")
        return 1
    ckpt = os.path.join(LOG_DIR, "discover_checkpoint.json")
    bank: dict = {}
    try:
        if os.path.isfile(ckpt):
            bank = json.load(open(ckpt, encoding="utf-8"))
    except Exception:
        bank = {}
    for sub, tags in SUB_TAGS.items():
        if sub in bank:
            print("r/%s: done already (%d)" % (sub, len(bank[sub])), flush=True)
            continue
        tags = tags[:2]
        seen: dict = {}
        for tag in tags:
            tag = clean_tag(tag)
            if not tag:
                continue
            for page in range(1, PAGES + 1):
                q = urllib.parse.urlencode({
                    "search_text": tag, "order": "latest",
                    "count": PER_PAGE, "page": page})
                try:
                    data = _get("%s/gifs/search?%s" % (API, q), token)
                except Exception as e:
                    if "429" in str(e):
                        print("r/%s [%s] p%d 429, backing off 60s" % (
                            sub, tag, page), flush=True)
                        time.sleep(60.0)
                        try:
                            data = _get("%s/gifs/search?%s" % (API, q), token)
                        except Exception as e2:
                            print("r/%s [%s] p%d FAIL %s" % (
                                sub, tag, page, str(e2)[:80]), flush=True)
                            break
                    else:
                        print("r/%s [%s] p%d FAIL %s" % (sub, tag, page, str(e)[:80]),
                              flush=True)
                        break
                for g in data.get("gifs") or []:
                    gid = str(g.get("id") or "").lower()
                    if gid and gid not in seen:
                        seen[gid] = {
                            "slug": gid,
                            "creator": g.get("userName") or "",
                            "views": g.get("views"),
                            "likes": g.get("likes"),
                            "published": g.get("createDate"),
                            "tags": g.get("tags") or [],
                            "query": tag,
                            "url": "https://www.redgifs.com/watch/%s/" % gid,
                        }
                time.sleep(2.5)
        bank[sub] = sorted(seen.values(),
                           key=lambda r: -((r.get("published") or 0)))
        print("r/%s: %d newest clips" % (sub, len(seen)), flush=True)
        with open(ckpt, "w", encoding="utf-8") as f:
            json.dump(bank, f, indent=2, ensure_ascii=False)
            f.write("\n")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    with open(os.path.join(LOG_DIR, "discover_%s.json" % stamp),
              "w", encoding="utf-8") as f:
        json.dump({"stamp": stamp, "subs": bank}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("WROTE logs/discover_%s.json (%d subs)" % (stamp, len(bank)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
