# UTF-8
"""Redgifs creator attribution for banked farm URLs.

Temp token + one v2 gif lookup per slug (paced). Output:
logs/day1_subs/redgifs_attr_<stamp>.json (per-gif creators + tables).

  python misc/redgifs_attr.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from friend_poster.core.config import LOG_DIR  # noqa: E402

API = "https://api.redgifs.com/v2"


def _get(url: str, token: str = ""):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Authorization": ("Bearer %s" % token) if token else "",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    import glob
    urls = []
    for f in sorted(glob.glob(os.path.join(LOG_DIR, "day1_subs", "post_raw_*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        for p in d["posts"]:
            if "redgifs" in (p.get("url") or "").lower():
                urls.append(p["url"])
    seen, slugs = set(), []
    for u in urls:
        slug = (u.split("/watch/")[-1].split("?")[0].strip("/").lower()
                if "/watch/" in u else "")
        if slug and slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    print("UNIQUE SLUGS", len(slugs), flush=True)
    token = _get(API + "/auth/temporary").get("token", "")
    if not token:
        print("NO TOKEN")
        return 1
    rows = []
    for i, slug in enumerate(slugs):
        try:
            g = (_get("%s/gifs/%s" % (API, slug), token).get("gif") or {})
            rows.append({
                "slug": slug,
                "creator": g.get("userName") or "",
                "gallery": g.get("gallery") or "",
                "tags": g.get("tags") or [],
                "published": g.get("createDate") or g.get("published") or "",
                "views": g.get("views"),
                "likes": g.get("likes"),
            })
            print("%d/%d %s -> %s" % (
                i + 1, len(slugs), slug[:30],
                rows[-1]["creator"] or "?"), flush=True)
        except Exception as e:
            rows.append({"slug": slug, "error": str(e)[:120]})
            print("%d/%d %s FAIL %s" % (i + 1, len(slugs), slug[:30],
                                        str(e)[:80]), flush=True)
        time.sleep(1.5)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    with open(os.path.join(LOG_DIR, "day1_subs", "redgifs_attr_%s.json" % stamp),
              "w", encoding="utf-8") as f:
        json.dump({"stamp": stamp, "gifs": rows}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("WROTE redgifs_attr_%s.json" % stamp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
