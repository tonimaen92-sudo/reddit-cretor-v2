# UTF-8
"""NSFW liveness lookup (step 1 of NSFW post line).

Post-time check: is this redgifs slug still live? Skip-if-gone, next URL.
Temp token + one v2 gif lookup per slug, paced. No commenter imports.
No handles/titles logged — slugs + verdicts only.

  python misc/nsfw_liveness.py <slug> [<slug> ...]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

API = "https://api.redgifs.com/v2"
PACE_S = 1.5


def _get(url: str, token: str = ""):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Authorization": ("Bearer %s" % token) if token else "",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get_token() -> str:
    return str(_get(API + "/auth/temporary").get("token", "") or "")


def check_slug(slug: str, token: str) -> dict:
    """One slug -> verdict. Never raises; never logs handles/titles."""
    slug = (slug or "").strip().strip("/").lower()
    if not slug:
        return {"slug": "", "live": False, "status": "error",
                "http": 0, "error": "empty slug"}
    try:
        g = (_get("%s/gifs/%s" % (API, slug), token).get("gif") or {})
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return {"slug": slug, "live": False, "status": "gone",
                    "http": e.code}
        return {"slug": slug, "live": False, "status": "error",
                "http": e.code, "error": str(e)[:80]}
    except Exception as e:
        msg = str(e)
        if "429" in msg:
            return {"slug": slug, "live": False, "status": "error",
                    "http": 429, "error": "429 throttled"}
        return {"slug": slug, "live": False, "status": "error",
                "http": 0, "error": msg[:80]}
    if not g:
        return {"slug": slug, "live": False, "status": "gone", "http": 200}
    return {"slug": slug, "live": True, "status": "live", "http": 200,
            "views": g.get("views"), "likes": g.get("likes"),
            "published": g.get("createDate") or g.get("published") or ""}


def check_many(slugs: list[str], log=print, pace: float = PACE_S) -> list[dict]:
    """Live-filter a candidate list. 429 -> one 60s backoff + retry once."""
    token = get_token()
    if not token:
        raise RuntimeError("NO TOKEN")
    out: list[dict] = []
    for i, slug in enumerate(slugs):
        v = check_slug(slug, token)
        if v.get("http") == 429:
            log("liveness 429, backing off 60s")
            time.sleep(60.0)
            v = check_slug(slug, token)
        out.append(v)
        log("%d/%d %s -> %s" % (i + 1, len(slugs), slug[:28], v["status"]))
        time.sleep(pace)
    return out


def main(argv: list[str]) -> int:
    slugs = [a.strip().lower() for a in argv[1:] if a.strip()]
    if not slugs:
        print("Usage: python misc/nsfw_liveness.py <slug> [<slug> ...]")
        return 2
    try:
        verdicts = check_many(slugs)
    except RuntimeError as e:
        print("ERROR %s" % e)
        return 1
    live = sum(1 for v in verdicts if v.get("live"))
    print("LIVE %d/%d" % (live, len(verdicts)))
    return 0 if live == len(verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
