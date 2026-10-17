# UTF-8
"""NSFW caption template + banned list (step 3).

Rules (locked): third-person short roleplay lines, reworded never copied,
no first-person claims, no model names. One caption per URL in
logs/nsfw_5plus5.json. No commenter imports.

  python misc/nsfw_captions.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from friend_poster.core.config import LOG_DIR  # noqa: E402

# Original lines, written for this run — never copied from source titles.
# Chill pass: casual observational third-person, not headlines.
CAPTIONS = {
    "momsonincest": [
        "She keeps teasing him on the polka dot bed.",
        "She bends over in the pink dress for him.",
        "She sneaks a taste through the door hole.",
        "She rewards him with a slow ride.",
        "She turns a shower tease steamy.",
    ],
    "publicblowjobs": [
        "She likes it when he tugs her hair.",
        "She takes it messy on the gravel.",
        "She tastes him in the car.",
        "She takes him under the sunny sky.",
        "They team up and share the moment.",
    ],
}

# Banned: first-person claims, model-name slots, underage-adjacent terms.
BANNED = (
    "teen", "lolita", "schoolgirl", "barely", "young", "jailbait",
    "tinyteen", "school", "18yo",
)
FIRST_PERSON = re.compile(
    r"(^|[\s\"'(\[])(i|me|my|mine|i'm|im|i've|i'll|myself)(\s|[.,!?;:\"')\]]|$)",
    re.I,
)


def validate(text: str) -> str:
    """Return '' if clean, else the reason. Never raises on text."""
    t = (text or "").strip()
    if not t:
        return "empty"
    low = t.lower()
    for b in BANNED:
        if b in low:
            return "banned word: %s" % b
    if FIRST_PERSON.search(t):
        return "first-person claim"
    if len(t.split()) > 14:
        return "too long (keep short)"
    return ""


def main() -> int:
    path = os.path.join(LOG_DIR, "nsfw_5plus5.json")
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print("ERROR reading nsfw_5plus5.json: %s" % e)
        return 1
    posts = data.get("posts") or []
    per_sub: dict[str, int] = {}
    for p in posts:
        sub = str(p.get("sub") or "")
        lines = CAPTIONS.get(sub) or []
        if not lines:
            print("ERROR no template for sub %r" % sub)
            return 1
        i = per_sub.get(sub, 0)
        if i >= len(lines):
            print("ERROR not enough template lines for %s" % sub)
            return 1
        cap = lines[i]
        bad = validate(cap)
        if bad:
            print("ERROR caption failed (%s): %r" % (bad, cap))
            return 1
        p["caption"] = cap
        per_sub[sub] = i + 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("CAPTIONED %d posts" % len(posts))
    for p in posts:
        print("%s :: %s" % (p.get("sub"), p.get("caption")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
