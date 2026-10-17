# UTF-8
"""NSFW URL-once-ever ledger (step 4).

One URL posts once ever. Statuses: staged (reserved by selection) ->
posted (consumed, with account + stamp). Selection and poster both gate
on is_used(). No commenter imports. Nothing posts from here.

  python misc/nsfw_ledger.py --seed   # seed current 5+5 as staged
  python misc/nsfw_ledger.py --list   # show ledger counts
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import LOG_DIR  # noqa: E402

LEDGER = os.path.join(LOG_DIR, "nsfw_url_ledger.json")
BANK = os.path.join(LOG_DIR, "nsfw_5plus5.json")


def load() -> dict:
    try:
        d = json.load(open(LEDGER, encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("urls", {})
            return d
    except (OSError, ValueError):
        pass
    return {"urls": {}}


def save(data: dict) -> None:
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def canon(url: str) -> str:
    """Canonical key: lowercase slug of a redgifs watch URL."""
    u = (url or "").strip().lower()
    if "/watch/" in u:
        u = u.split("/watch/")[-1].split("?")[0].strip("/")
        return "watch:" + u
    return u


def is_used(url: str) -> bool:
    """Reserved or consumed (staged or posted). For selection-time dedup."""
    return canon(url) in load().get("urls", {})


def is_posted(url: str) -> bool:
    """Consumed (posted). For pre-send gating: staged URLs are still sendable."""
    row = load().get("urls", {}).get(canon(url)) or {}
    return row.get("status") == "posted"


def mark_staged(posts: list[dict]) -> int:
    data = load()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    n = 0
    for p in posts:
        key = canon(p.get("url") or "")
        if not key or key in data["urls"]:
            continue
        data["urls"][key] = {
            "slug": (p.get("slug") or "").lower(),
            "sub": p.get("sub") or "",
            "status": "staged",
            "account": "",
            "ts": now,
        }
        n += 1
    save(data)
    return n


def mark_posted(url: str, account: str) -> bool:
    data = load()
    key = canon(url)
    if key not in data["urls"]:
        return False
    data["urls"][key]["status"] = "posted"
    data["urls"][key]["account"] = account
    data["urls"][key]["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime())
    save(data)
    return True


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--seed":
        try:
            bank = json.load(open(BANK, encoding="utf-8"))
        except Exception as e:
            print("ERROR reading bank: %s" % e)
            return 1
        n = mark_staged(bank.get("posts") or [])
        total = len(load()["urls"])
        print("STAGED %d new, ledger holds %d urls" % (n, total))
        return 0
    if len(argv) >= 2 and argv[1] == "--list":
        data = load()
        urls = data.get("urls", {})
        staged = sum(1 for v in urls.values() if v.get("status") == "staged")
        posted = sum(1 for v in urls.values() if v.get("status") == "posted")
        print("LEDGER urls=%d staged=%d posted=%d" % (len(urls), staged,
                                                      posted))
        return 0
    print("Usage: python misc/nsfw_ledger.py --seed | --list")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
