# UTF-8
"""NSFW frame describe + grounded captions (grok-4-fast, direct xAI key).

Per clip: download mp4, 5 spread frames (10/30/50/70/90%), multi-image
describe with reasoning on, rewrite caption grounded in visible content.
Staging video/frames live in TEMP and are deleted; summaries + captions
persist into logs/nsfw_5plus5.json (backed up first).

  python misc/nsfw_frames.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from friend_poster.core.config import LOG_DIR  # noqa: E402
from friend_poster.services.comment_llm import load_xai_key, xai_chat, XAI_FAST_MODEL  # noqa: E402

API = "https://api.redgifs.com/v2"
STAGE = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")),
                     "opencode", "nsfw_frames")
BANK = os.path.join(LOG_DIR, "nsfw_5plus5.json")
PCTS = (10, 30, 50, 70, 90)

CAPTION_RULES = (
    "Write one Reddit post title in a chill casual observational voice, "
    "like \"she loves a quick dare outside\". Third person, one short "
    "line, normal sentence caps (never headline caps). React to what is "
    "actually visible in the clip. Never first-person claims, never "
    "model names, never copy the old caption. Vary your opener, never "
    "start two titles the same way."
)


def _get(url: str, token: str = ""):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Authorization": ("Bearer %s" % token) if token else "",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main(argv: list[str] | None = None) -> int:
    key = load_xai_key()
    if not key:
        print("no xai key")
        return 1
    only = None
    args = argv[1:] if argv else []
    if "--clip" in args:
        try:
            only = int(args[args.index("--clip") + 1])
        except (ValueError, IndexError):
            print("Usage: python misc/nsfw_frames.py [--clip N]")
            return 2
    try:
        shutil.copyfile(BANK, BANK + ".bak-frames")
    except OSError:
        pass
    bank = json.load(open(BANK, encoding="utf-8"))
    posts = bank.get("posts", [])
    os.makedirs(STAGE, exist_ok=True)
    token = _get(API + "/auth/temporary").get("token", "")
    for i, post in enumerate(posts):
        if only is not None and i != only - 1:
            continue
        slug = (post.get("url") or "").split("/watch/")[-1].strip("/")
        tag = "clip%02d" % (i + 1)
        try:
            g = (_get("%s/gifs/%s" % (API, slug), token).get("gif") or {})
            urls = g.get("urls") or {}
            mp4 = urls.get("hd") or urls.get("sd") or urls.get("vthumbnail") or ""
            if not mp4.lower().endswith(".mp4"):
                print("%s: no mp4 (%s)" % (tag, (mp4 or "")[:60]), flush=True)
                post["frames_error"] = "no mp4"
                continue
            vid = os.path.join(STAGE, tag + ".mp4")
            urllib.request.urlretrieve(mp4, vid)
            dur = float(__import__("json").loads(subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", vid],
                timeout=60).decode().strip() or 0))
            imgs = []
            for p in PCTS:
                fp = os.path.join(STAGE, "%s_%d.jpg" % (tag, p))
                subprocess.check_output(
                    ["ffmpeg", "-y", "-v", "error", "-ss", str(dur * p / 100.0),
                     "-i", vid, "-frames:v", "1", "-q:v", "5", fp], timeout=120)
                with open(fp, "rb") as f:
                    imgs.append({"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64,"
                               + base64.b64encode(f.read()).decode()}})
            desc, err = xai_chat(key, XAI_FAST_MODEL, [{
                "role": "user", "content": [
                    {"type": "text", "text": (
                        "Describe what happens in this clip across its 5 frames: "
                        "setting, people, key action beat by beat, visible text. "
                        "Plain facts only, no names, no backstory.")},
                    *imgs,
                ]}], max_tokens=600, temperature=0.3, timeout=180,
                reasoning=True)
            if err or not desc:
                print("%s: describe FAIL %s" % (tag, (err or "")[:100]), flush=True)
                post["frames_error"] = (err or "empty")[:160]
                continue
            post["vision_summary"] = desc.strip()[:2000]
            cap, cerr = xai_chat(key, XAI_FAST_MODEL, [{
                "role": "user", "content": (
                    "%s\n\nClip facts:\n%s\n\nOld caption (do not copy): %s"
                    % (CAPTION_RULES, desc.strip()[:1500],
                       post.get("caption") or ""))}],
                max_tokens=60, temperature=0.9, timeout=120, reasoning=False)
            if cap:
                post["caption"] = cap.strip().strip('"').strip(".")[:120]
            print("%s: captioned: %s" % (tag, post.get("caption")), flush=True)
        except Exception as e:
            print("%s: ERROR %s: %s" % (tag, type(e).__name__, str(e)[:120]),
                  flush=True)
            post["frames_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
        finally:
            for f in os.listdir(STAGE):
                if f.startswith(tag + ".") or f.startswith(tag + "_"):
                    try:
                        os.remove(os.path.join(STAGE, f))
                    except OSError:
                        pass
        with open(BANK, "w", encoding="utf-8") as f:
            json.dump(bank, f, indent=2, ensure_ascii=False)
            f.write("\n")
        time.sleep(2.0)
    print("WROTE", BANK)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
