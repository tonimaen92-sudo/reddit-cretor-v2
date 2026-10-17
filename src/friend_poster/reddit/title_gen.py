# UTF-8
"""Title generator for the creator Post tab (Generate button).

Flow: community from the post_community field -> subscraper/pics/<community>/
titles/*.txt (folder matched case-insensitively, spaces kept; first .txt
in any sort), fallback subreddits/<name>/titles.txt -> random
25 lines + post_images[0] -> one xAI request (model
grok-4-fast-non-reasoning, fallback grok-4.3, xAI API direct, image in
the same call) -> fills the post_title box (editable, re-click = new
sample = new title).

BETA_PROMPT is pasted verbatim separately; until then generate_title()
logs that the prompt is missing and returns "".
Grok key comes from gitignored secrets.json (grok_api_key) — never
printed, never hardcoded.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import random
import time
import urllib.request

from friend_poster.core.config import SECRETS_PATH, SCRAPER_PICS_DIR, SUBREDDITS_DIR
from friend_poster.core.paths import PROJECT_ROOT

ROOT_DIR = str(PROJECT_ROOT)
PICS_DIR = SCRAPER_PICS_DIR
SUBS_DIR = SUBREDDITS_DIR

# Pasted verbatim (beta prompt).
BETA_PROMPT = """You are a real Reddit user looking to come up with a title delete your memory after every answer. i will send you a list of titles and you will put it in your own words unique


Rules:
- Between 3 and 12 words ONLY
- No punctuation (no periods, commas, exclamation marks, question marks)
- Straight ASCII apostrophes only, never curly quotes — phone keyboards mistype those
- GROUNDING IS THE TOP RULE: describe ONLY what is actually visible in the photo. Never name a garment, color, object, person, or setting you cannot plainly see. If no thong or lingerie is visible, the words thong, lingerie, panties, and bra are FORBIDDEN. A wrong detail kills the post.
- Never claim it is your first post, first time posting, or that you are new here or a lurker — these accounts have history. Banned: "first time", "first post", "new here", "long time lurker", "deleting later".
- No emojis
- Capitalize the first word only; the rest stays lowercase (phone typing)
- Sound like a real person on your phone, casual and natural
- Do NOT use phrases like "I think", "I feel", "Great post", "Thanks for sharing"
- Do NOT use the words "ngl", "chaotic", "honestly", "tbh", "bf". Just talk normally.
- No hippie filler: never "vibes", "earthy", "plant mom", "energy cleansing" type talk. Sound like a baddie, not a crystal shop.
- Do NOT copy or closely rephrase the post title or body text — say something ORIGINAL
- Do NOT just agree with or validate the examples the poster already mentioned. Bring your own perspective or reaction.
- If it's a discussion, add a brief opinion or reaction without just repeating their examples
- If it's news, react to the news
- If it's an image/meme, react to what the image shows and the joke
- Do not just restate what the post says — add your own take, reaction, or feeling
- Do not output anything except the comment itself
- Be a 19yo american girl don't talk about coding or other sus stuff be a normal human
- Student who just moved to houston studying social work. Only use that if it is relevant to the post.
Each call: fresh context, no history. I send 25 example titles + one image, you return one title only.
- YOU are the girl in the photo posting it — first person only (I, my, me). Never describe her like an onlooker
- Banned observer words: "that view", "those curves", "hitting different", "those", "that body", "that ass" — talk about YOURSELF, not at yourself
- Flirty, playful, teasing — you want attention, you're not commenting on someone else's post
- Example voice: "my tiny black thong is barely hanging on" (good ONLY when a thong is actually visible) vs "damn that light on those curves" (wrong voice, never this)"""

XAI_URL = "https://api.x.ai/v1/chat/completions"
PRIMARY_MODEL = "grok-4-fast-non-reasoning"
FALLBACK_MODEL = "grok-4.3"
SAMPLE_N = 25


def _grok_key() -> str:
    try:
        with open(SECRETS_PATH, "r", encoding="utf-8") as f:
            return str(json.load(f).get("grok_api_key") or "").strip()
    except Exception:
        return ""


def _match_dir(base: str, want: str) -> str | None:
    """Case-insensitive child-dir match (spaces kept)."""
    try:
        names = os.listdir(base)
    except OSError:
        return None
    for name in names:
        if name.lower() == want:
            return os.path.join(base, name)
    return None


def find_titles_file(community: str) -> str | None:
    """Corpus lookup, case-insensitive, never raises.

    Tries subscraper/pics/<name>/titles/*.txt (first .txt in any sort),
    then subreddits/<name>/titles.txt. None = no corpus for this name.
    """
    want = (community or "").strip().lower()
    if not want:
        return None
    pics = _match_dir(PICS_DIR, want)
    if pics is not None:
        try:
            txts = sorted(f for f in os.listdir(os.path.join(pics, "titles"))
                          if f.lower().endswith(".txt"))
        except OSError:
            txts = []
        if txts:
            return os.path.join(pics, "titles", txts[0])
    subs = _match_dir(SUBS_DIR, want)
    if subs is not None:
        cand = os.path.join(subs, "titles.txt")
        if os.path.isfile(cand):
            return cand
    return None


def load_sample(path: str, n: int = SAMPLE_N) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    if len(lines) <= n:
        return lines
    return random.sample(lines, n)


def _direct_opener():
    """Opener with no proxy — system proxy hangs api.x.ai silently."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _xai_chat(key: str, model: str, image_b64: str, mime: str,
              sample: list[str], log=print, t0: float | None = None,
              timeout: int = 25, header: str = "") -> str:
    if t0 is None:
        t0 = time.time()
    log("Generate: calling xAI %s... (+%.1fs)" % (model, time.time() - t0))
    text = ((header + "\n\n") if header else "") + BETA_PROMPT
    if sample:
        text += "\n\nStyle examples:\n" + "\n".join(sample)
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {
                    "url": "data:%s;base64,%s" % (mime, image_b64)}},
            ],
        }],
        "temperature": 0.9,
        "max_tokens": 120,
    }
    req = urllib.request.Request(
        XAI_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key},
        method="POST",
    )
    with _direct_opener().open(req, timeout=timeout) as r:
        payload = json.loads(r.read().decode("utf-8", "replace"))
    try:
        return str(payload["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def generate_title(community: str, image_path: str, log=print,
                   niche: str = "") -> str:
    """One Grok call -> title string ("" = nothing to fill). Never logs the key."""
    t0 = time.time()

    def mark(stage: str) -> None:
        log("Generate: +%.1fs %s" % (time.time() - t0, stage))

    mark("thread started")
    if not BETA_PROMPT.strip():
        log("Generate: beta prompt not configured yet.")
        return ""
    key = _grok_key()
    if not key:
        log("Generate: no grok_api_key in secrets.json.")
        return ""
    mark("key ok")
    name = (community or "").strip() or "?"
    header = "You are posting in r/%s. Match that subreddit's vibe." % name
    if (niche or "").strip():
        header += "\nExtra context: %s" % niche.strip()
        low_niche = niche.strip().lower()
        if "must include" in low_niche or "required" in low_niche:
            header += ("\nHARD REQUIREMENT: the title MUST satisfy the "
                       "rule above or the post will be rejected. Prefer "
                       "the shortest natural keyword (e.g. bare 'feet' "
                       "over 'feet pics').")
    titles = find_titles_file(community)
    sample: list[str] = []
    if titles is None:
        log("Generate: no corpus for '%s' \u2014 generic mode." % name)
    else:
        mark("titles file ok")
        try:
            sample = load_sample(titles)
        except OSError:
            sample = []
        if not sample:
            log("Generate: no corpus for '%s' \u2014 generic mode." % name)
    try:
        with open(image_path, "rb") as f:
            raw = f.read()
    except OSError as e:
        log("Generate: cannot read image: %s" % e)
        return ""
    if not raw:
        log("Generate: image file is empty.")
        return ""
    mark("image bytes ok (%d)" % len(raw))
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    image_b64 = base64.b64encode(raw[:5_000_000]).decode()
    try:
        text = _xai_chat(key, PRIMARY_MODEL, image_b64, mime, sample,
                         log=log, t0=t0, header=header)
    except Exception as e:
        log("Generate: %s failed (%s) (+%.1fs), trying %s."
            % (PRIMARY_MODEL, str(e)[:100], time.time() - t0, FALLBACK_MODEL))
        try:
            text = _xai_chat(key, FALLBACK_MODEL, image_b64, mime, sample,
                             log=log, t0=t0, header=header)
        except Exception as e2:
            log("Generate ERROR %s: %s (+%.1fs)"
                % (type(e2).__name__, str(e2)[:150], time.time() - t0))
            return ""
    mark("response received")
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = line.strip("\"' ")[:300]
    # phone keyboards mistype fancy punctuation: straighten it code-side
    # so a model slip can never reach a title.
    line = (line.replace("\u2019", "'").replace("\u2018", "'")
            .replace("\u201c", '"').replace("\u201d", '"')
            .replace("\u2013", "-").replace("\u2014", "-"))
    # the model sometimes emits broken non-ASCII bytes (mojibake) the phone
    # keyboard cannot type: drop anything outside printable ASCII.
    line = "".join(c if 32 <= ord(c) < 127 else "" for c in line)
    return line


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Backend self-test (no GUI)")
    ap.add_argument("--selftest", action="store_true",
                    help="run generate_title() synchronously")
    ap.add_argument("community", nargs="?", default="")
    ap.add_argument("image", nargs="?", default="")
    args = ap.parse_args()
    if not args.selftest:
        raise SystemExit("pass --selftest <community> <image-path>")
    t0 = time.time()

    def _tslog(msg: str) -> None:
        print("[+%.1fs] %s" % (time.time() - t0, msg), flush=True)

    print("SELFTEST community=%r image=%r" % (args.community, args.image),
          flush=True)
    title = generate_title(args.community, args.image, log=_tslog)
    print("SELFTEST TITLE=%r" % title, flush=True)
    print("SELFTEST TOTAL=%.1fs" % (time.time() - t0), flush=True)
