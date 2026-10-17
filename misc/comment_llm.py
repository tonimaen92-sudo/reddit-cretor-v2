# UTF-8
"""Reply-text helpers for the fast comment path.

Port of reddit-auto-android/misc/comment_llm.py: OpenRouter endpoint
(openrouter.ai/api/v1), models openai/gpt-4o-mini (vision) +
xiaomi/mimo-v2.5-pro (text). Prompts match misc/mimo_comment_prompt.txt
verbatim; _cleanup is identical to Android.

Key chain: gitignored secrets.json openrouter_api_key -> env
OPENROUTER_API_KEY. No key -> "" so the agent fills reply_text manually
in logs/comment_queue.json (last-resort fallback, same as the verified
22 Sep live run).

Vision rule: crop the dump image node, never the whole screenshot
(crop_dump_image_node refuses near-full-frame boxes).
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.request

from config import SECRETS_PATH

MIMO_TEXT_MODEL = "xiaomi/mimo-v2.5-pro"
OPENAI_VISION_MODEL = "openai/gpt-4o-mini"
NSFW_MODEL = "xiaomi/mimo-v2.5-pro"
COMMENT_TEXT_MODEL = (os.environ.get("OPENROUTER_TEXT_MODEL") or MIMO_TEXT_MODEL).strip()
COMMENT_VISION_MODEL = (os.environ.get("OPENROUTER_VISION_MODEL") or OPENAI_VISION_MODEL).strip()
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

_PUNCT = re.compile(r"[.,!?;:'\"`()\[\]]")


def load_api_key():
    """secrets.json openrouter_api_key first, then env OPENROUTER_API_KEY."""
    try:
        if os.path.isfile(SECRETS_PATH):
            with open(SECRETS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            key = str(data.get("openrouter_api_key") or "").strip()
            if key and "your-key" not in key:
                return key
    except Exception:
        pass
    env = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if env:
        return env
    return ""


XAI_API = "https://api.x.ai/v1/chat/completions"
XAI_FAST_MODEL = "grok-4-fast"


def load_xai_key():
    """secrets.json xai_api_key first, then env XAI_API_KEY. Never logged."""
    try:
        if os.path.isfile(SECRETS_PATH):
            with open(SECRETS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            key = str(data.get("xai_api_key") or "").strip()
            if key and "your-key" not in key:
                return key
    except Exception:
        pass
    env = (os.environ.get("XAI_API_KEY") or "").strip()
    if env:
        return env
    return ""


def xai_chat(api_key, model, messages, max_tokens, temperature, timeout,
             reasoning=True):
    """Direct xAI call for NSFW-side work (farm code never uses this)."""
    import requests
    body = {
        "model": model or XAI_FAST_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if reasoning:
        body["reasoning"] = {"enabled": True}
    r = requests.post(XAI_API, headers={
        "Authorization": "Bearer %s" % api_key,
        "Content-Type": "application/json",
    }, json=body, timeout=timeout)
    if r.status_code != 200:
        return None, "xAI HTTP error %d: %s" % (r.status_code, (r.text or "")[:300])
    try:
        data = r.json()
    except Exception:
        return None, "xAI returned invalid JSON: %s" % ((r.text or "")[:300])
    if "error" in data:
        err = data["error"]
        if isinstance(err, dict):
            err = err.get("message", str(err))
        return None, "xAI error: %s" % str(err)[:300]
    try:
        return (data.get("choices") or [{}])[0].get("message", {}).get("content"), None
    except Exception:
        return None, "xAI returned no content"


def _cleanup(text):
    text = (text or "").strip()
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.I | re.S)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    candidates = []
    for line in reversed(lines):
        low = line.lower()
        if line.startswith("<") or low.startswith("think"):
            continue
        candidates.append(line)
    if not candidates:
        candidates = [text]
    for picked in candidates:
        comment = re.sub(r"<[^>]+>", "", picked).strip()
        comment = comment.strip('"').strip("'").strip(".")
        comment = _PUNCT.sub("", comment)
        comment = re.sub(r"\s+", " ", comment).strip().lower()
        words = comment.split()
        if len(words) > 12:
            words = words[:12]
        if len(words) < 3:
            continue
        first = words[0]
        words[0] = first[:1].upper() + first[1:]
        return " ".join(words)
    return ""


def _chat(api_key, model, messages, max_tokens, temperature, timeout):
    import requests

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/reddit-bot",
        "X-Title": "Reddit iPhone Commenter",
    }
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    r = requests.post(OPENROUTER, headers=headers, json=body, timeout=timeout)
    if r.status_code != 200:
        return None, f"API HTTP error {r.status_code}: {(r.text or '')[:300]}"
    try:
        data = r.json()
    except Exception:
        return None, f"API returned invalid JSON: {(r.text or '')[:300]}"
    if "error" in data:
        err = data["error"]
        if isinstance(err, dict):
            err = err.get("message", str(err))
        return None, f"OpenRouter error: {str(err)[:300]}"
    try:
        return (data.get("choices") or [{}])[0].get("message", {}).get("content"), None
    except Exception:
        return None, "API returned no content"


def build_mimo_prompt(title, body, image_analysis, sub="", top_comment=""):
    """Exact model input: title + body + vision analysis + top comment + rules."""
    body_text = (body or "").strip()
    if len(body_text) > 2000:
        body_text = body_text[:2000] + "..."
    parts = []
    if sub:
        parts.append(f"Subreddit: r/{sub}")
    parts.append(f"Post title: {title}")
    if body_text:
        parts.append(f"Post body: {body_text}")
    else:
        parts.append("Post body: (no body text)")
    if image_analysis:
        parts.append(f"Image analysis:\n{image_analysis}")
    else:
        parts.append("Image: (no image — text post)")
    if (top_comment or "").strip():
        parts.append(f"Top comment (highest upvoted, thread context only — "
                     f"do NOT reply to it, post a top-level comment):\n"
                     f"{(top_comment or '').strip()[:2000]}")
    context = "\n\n".join(parts)
    return f"""You are a real Reddit user casually scrolling. Read the following post and write a short comment reacting to it.

Rules:
- Between 3 and 12 words ONLY
- No punctuation (no periods, commas, exclamation marks, question marks)
- No emojis
- Capitalize the first word only; the rest stays lowercase (phone typing)
- Sound like a real person on your phone, casual and natural
- Do NOT use phrases like "I think", "I feel", "Great post", "Thanks for sharing"
- Do NOT use the words "ngl", "chaotic", "honestly", "tbh". Just talk normally.
- Do NOT copy or closely rephrase the post title or body text — say something ORIGINAL
- Never narrate the medium: BANNED "this fanart", "this art", "this post", "this pic", "this image", "this drawing". React to the subject directly like a human (say "pomni looking extra shy today", never "this fanart has pomni looking extra shy today")
- Do NOT just agree with or validate the examples the poster already mentioned. Bring your own perspective or reaction.
- If it's a discussion, add a brief opinion or reaction without just repeating their examples
- If it's news, react to the news
- If it's an image/meme, react to what the image shows and the joke
- Do not just restate what the post says — add your own take, reaction, or feeling
- Do not output anything except the comment itself
- If you have no genuine reaction to this post, output exactly: PASS (nothing posted, item skipped)
- Be a 19yo american girl don't talk about coding or other sus stuff be a normal human
- Student who just moved to houston studying social work. Only use that if it is relevant to the post.
- GLOBAL kind-only for ALL subs. Be kind warm supportive encouraging always. Never snarky rude mean superior backhanded or gatekeeping. Never put down the poster or their effort on any sub.
- BANNED put-down shapes ALL subs: "thats just X", "you got a ways to go", "not a real", "thats not", "thats barely", "good luck with that". If tempted to correct, praise effort + cheer them on instead.
- No gatekeeping what counts on any sub. Hype effort, wish a good result, stay friendly.
- Do NOT criticize the community, the fandom, or any person — be kind, warm, and good-natured, never snarky, mean, or superior
- Your character leans left socially (kind, inclusive, community-minded) as passive background only: never volunteer politics, never preach, never pick fights; on political threads stay gentle with kind reactions, never takes hot takes

{context}

Your comment:"""


def generate_comment(title, body, image_analysis, api_key, sub="",
                     top_comment=""):
    if not api_key:
        return None, "No API key provided"
    prompt = build_mimo_prompt(title, body, image_analysis, sub,
                               top_comment=top_comment)
    last_err = "AI failed"
    for attempt in range(3):
        raw, err = _chat(
            api_key,
            COMMENT_TEXT_MODEL,
            [{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.9,
            timeout=180,
        )
        if err:
            last_err = err
            time.sleep(2)
            continue
        if (raw or "").strip().upper() == "PASS":
            return None, "pass"
        cleaned = _cleanup(raw)
        if cleaned:
            return cleaned, None
        last_err = "AI returned empty comment after cleanup"
        if raw:
            last_err += f" raw={str(raw)[:160]!r}"
        time.sleep(2)
    return None, last_err


def generate_reply(post_title, post_body, image_analysis, parent_comment, api_key,
                   sub=""):
    if not api_key:
        return None, "No API key provided"
    context = f"Subreddit: r/{sub}\n" if sub else ""
    prompt = f"""{context}You are a 19yo girl replying to a Reddit comment. Keep it 3-12 words, capitalize the first word only, rest lowercase, no punctuation, no emoji, casual, not formal.
Student who just moved to houston studying social work. Only use that if it is relevant to this thread.
GLOBAL kind-only for ALL subs: kind warm supportive encouraging always, never snarky rude mean superior or gatekeeping. Banned: "thats just X", "you got a ways to go", "not a real", "thats not".
Post title: {(post_title or "")[:200]}
Post body: {(post_body or "")[:800]}
Image analysis: {(image_analysis or "")[:800]}
Parent comment you are replying to: {(parent_comment or "")[:400]}
Reply in 3-12 words that feels like a natural Reddit reply, referencing the parent comment if relevant, but stay casual and short."""
    last_err = "AI failed"
    for attempt in range(3):
        raw, err = _chat(
            api_key,
            COMMENT_TEXT_MODEL,
            [{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.9,
            timeout=120,
        )
        if err:
            last_err = err
            time.sleep(2)
            continue
        cleaned = _cleanup(raw)
        if cleaned:
            return cleaned, None
        last_err = "AI returned empty reply after cleanup"
        if raw:
            last_err += f" raw={str(raw)[:160]!r}"
        time.sleep(2)
    return None, last_err


def build_vision_prompt(sub_name: str, title: str, body: str) -> str:
    return f"""Analyze this Reddit post image from r/{sub_name} in detail. More info is better.

Post Title: {title}
Post Body Text: {body or 'none'}

Based on the title, body text, and the subreddit (r/{sub_name}), analyze the image:

1. TEXT_IN_IMAGE: quote ALL text visible inside the image itself, verbatim — speech bubbles, overlays, captions, meme text, watermarks, artist signatures/handles. If none, say "none".
2. CONTENT: thorough visual breakdown — every character, person, object, animal, scenery element; appearance, clothing, colors, art style and medium (fanart, screenshot, photo, meme format); composition, setting, mood. Also explain the subreddit context: what r/{sub_name} is about and what kind of posts it celebrates.
3. SCENE: what is happening in the image — the joke, story, or moment; how it connects to the title and body; why it fits r/{sub_name}; what a fan of the sub would notice or feel about it.

Format your response exactly like this:
TEXT_IN_IMAGE: [all text found in image, or "none"]
CONTENT: [detailed visual breakdown + subreddit context]
SCENE: [what's happening, the joke/story, fit with the sub]
Begin your response with the TEXT_IN_IMAGE: line.

Be thorough and specific."""


def vision_bytes_ok(data: bytes) -> bool:
    """Bytes actually worth sending: image magic + sane dimensions.

    Validation is by CONTENT, never size: error pages fail at any byte count,
    thumbnail-grade (sub-200px) crops are rejected even when the magic matches.
    Kills tiny icons/avatars/broken-element shots before they draw a
    model refusal that would waste the run.
    """
    if not is_image_bytes(data):
        return False
    try:
        from PIL import Image
        import io as _io
        with Image.open(_io.BytesIO(data)) as im:
            w, h = im.size
        return w >= 200 and h >= 200
    except Exception:
        return False


def vision_refused(desc: str) -> bool:
    """True when the model declined instead of describing (ask-backs,
    inability claims). Such replies must never feed Mimo as analysis."""
    low = (desc or "").lower()
    return any(k in low for k in (
        "unable to analyze", "can't see", "cannot see", "can't view",
        "cannot view", "not able to view", "as an ai", "describe the image",
        "provide details", "can't help with images", "cannot help with images",
        "can't assist", "cannot assist", "content policy", "against my",
        "not appropriate", "sexually explicit", "i'm sorry, but i can't"))


def parse_analysis(desc: str) -> dict:
    """Split a vision reply into the 3 panel lines. Unformatted blobs are
    kept whole in CONTENT rather than trashed (format drift happens)."""
    out = {"text_in_image": "", "content": "", "scene": ""}
    for line in (desc or "").splitlines():
        up = line.strip().upper()
        if up.startswith("TEXT_IN_IMAGE"):
            out["text_in_image"] = line.split(":", 1)[-1].strip()
        elif up.startswith("CONTENT"):
            out["content"] = line.split(":", 1)[-1].strip()
        elif up.startswith("SCENE"):
            out["scene"] = line.split(":", 1)[-1].strip()
    if desc and not any(out.values()):
        out["content"] = desc.strip()[:2000]
    return out


def classify_nsfw(title: str, body: str, image_text: str, api_key: str) -> tuple:
    """Explicit-only NSFW check on text signals (no image tokens, fractions
    of a cent). Returns (True, '') when explicit sexual content or nudity is
    present. Swimwear, kissing, ship-tease, and romance stay commentable."""
    if not api_key:
        return False, "No API key provided"
    prompt = (
        "You moderate Reddit image posts for a comment bot. Answer YES or NO only.\n"
        "YES only when the text below shows explicit sexual content, sexual acts, "
        "explicit nudity, or pornographic detail.\n"
        "NO for swimwear, bikinis, kissing, romance, shipping, flirting, jokes, "
        "and suggestive-but-not-explicit content.\n"
        "Title: %s\nBody: %s\nText in image: %s\nYES or NO:"
        % ((title or "")[:300], (body or "")[:500], (image_text or "")[:500])
    )
    raw, err = _chat(api_key, NSFW_MODEL, [{"role": "user", "content": prompt}],
                     max_tokens=10, temperature=0.0, timeout=30)
    if err:
        return False, err
    first = (raw or "").strip().upper().split()
    if first and first[0].startswith("YES"):
        return True, ""
    return False, ""


def describe_image_bytes(image_bytes, sub_name, title, body, api_key, mime="image/jpeg",
                         model: str = ""):
    if not api_key:
        return None, "No API key provided"
    if not image_bytes:
        return None, None
    if not vision_bytes_ok(image_bytes):
        return None, "not vision-grade bytes (magic/dimensions failed)"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    url = f"data:{mime};base64,{b64}"
    prompt = build_vision_prompt(sub_name, title, body)
    try:
        raw, err = _chat(
            api_key,
            model or COMMENT_VISION_MODEL,
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ],
            max_tokens=2000,
            temperature=0.3,
            timeout=90,
        )
    except Exception as e:
        return None, f"Vision API error: {e}"
    if err:
        return None, err
    if not raw:
        return None, "Vision API returned no description"
    text = raw.strip()
    if len(text) > 4000:
        text = text[:4000]
    return text, None


def crop_dump_image_node(png_path: str, x: int, y: int, w: int, h: int) -> bytes:
    """Crop a dump image node out of a dump PNG. Never the whole screenshot:
    boxes covering >=90% of the frame are refused (b"")."""
    from PIL import Image

    with Image.open(png_path) as im:
        fw, fh = im.size
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(fw, x + w), min(fh, y + h)
        if x1 <= x0 or y1 <= y0:
            return b""
        if (x1 - x0) * (y1 - y0) >= 0.9 * fw * fh:
            return b""
        import io

        buf = io.BytesIO()
        im.crop((x0, y0, x1, y1)).convert("RGB").save(buf, format="JPEG")
        return buf.getvalue()


def is_image_bytes(data: bytes, ctype: str = "") -> bool:
    """Magic-byte gate: HTML/error pages must never reach vision.

    JPEG / PNG / GIF / WEBP by magic. A bare RIFF header (WAV/AVI) is NOT
    enough - WEBP must follow at offset 8, else error pages slip through.
    """
    if not data or len(data) < 64:
        return False
    if ctype and "image" not in str(ctype).lower():
        return False
    head = bytes(data[:16])
    return (head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n"
            or head[:6] in (b"GIF87a", b"GIF89a")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


def mime_of(data: bytes) -> str:
    """Content-sniffed MIME for data-URL describe calls. Never guessed."""
    head = bytes((data or b"")[:16])
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def fetch_image_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.reddit.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = (r.headers.get("Content-Type") or "")
        data = r.read()
    if not is_image_bytes(data, ctype):
        raise ValueError("not image bytes (%s, %dB)" % (ctype, len(data)))
    return data


# ---- Vision-fetch ladder (locked 26 Sep 2026): first-valid-wins ----
#
# Per image post, slowest last: direct URL -> preview-param strip to source ->
# in-page candidates largest-first (the single-pick finder grabs stickers) ->
# gallery JSON full image (gallery_data/media s.u) -> v.redd.it poster frame
# (flagged, acceptable) -> authed browser-context fetch via the running
# bot-Chrome profile (CDP download carrying session - kills hotlink 403s
# structurally). Every rung validated by CONTENT (vision_bytes_ok); error
# pages fail at any byte count. Corrupt bytes never reach describe.

GALLERY_JS = (
    "() => { const g = document.querySelector('gallery-carousel, shreddit-media-lightbox');"
    " if (!g) return ''; const imgs = [...g.querySelectorAll('img')];"
    " let best = '', bestW = -1;"
    " for (const e of imgs) { const ss = e.getAttribute('srcset') || '';"
    " for (const part of ss.split(',')) {"
    " const m = part.trim().match(/^(https?:\\S+)\\s+(\\d+)w$/);"
    " if (m && +m[2] > bestW && !/styles[.]redditmedia/.test(m[1]))"
    " { bestW = +m[2]; best = m[1]; } } }"
    " return best; } "
)

POSTER_JS = (
    "() => { const v = document.querySelector('shreddit-player, video');"
    " if (!v) return ''; return v.getAttribute('poster') || v.poster || ''; } "
)

POST_CANDS_JS = (
    "() => [...document.querySelectorAll('shreddit-post img')].map(e => ({"
    " src: e.currentSrc || e.src || '',"
    " w: e.naturalWidth || 0, h: e.naturalHeight || 0})) "
)


def rank_candidates(cands) -> list:
    """Pure rank of in-page post images: dedup, drop subreddit style chrome,
    largest rendered area first. The single-pick finder grabs stickers;
    the ladder tries every candidate until content validates."""
    seen, rows = set(), []
    for c in cands or []:
        if not isinstance(c, dict):
            continue
        u = str(c.get("src") or "").strip()
        if not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        if "styles.redditmedia" in u:
            continue
        try:
            area = int(c.get("w") or 0) * int(c.get("h") or 0)
        except (TypeError, ValueError):
            area = 0
        rows.append((area, u))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [u for _, u in rows]


def strip_preview_params(url: str) -> str:
    """preview.redd.it serves thumbnails while params hang on (?width=&crop=&
    auto=); the bare path is the fuller source. Returns the stripped URL,
    or '' when the URL is not a parametrized preview."""
    u = (url or "").strip()
    if "preview.redd.it" not in u.lower() or "?" not in u:
        return ""
    return u.split("?", 1)[0]


def gallery_full_url(media_metadata) -> str:
    """Pure pick of the full-res gallery image from .json-style metadata:
    {id: {status, s: {u, x, y}}}. Escaped &amp; unescaped, invalid entries
    skipped, largest area wins. '' when nothing valid."""
    best, best_area = "", -1
    meta = media_metadata if isinstance(media_metadata, dict) else {}
    for _mid, m in meta.items():
        if not isinstance(m, dict) or str(m.get("status") or "").lower() != "valid":
            continue
        s = m.get("s") or {}
        if not isinstance(s, dict):
            continue
        u = str(s.get("u") or "").replace("&amp;", "&")
        if not u.startswith("http"):
            continue
        try:
            area = int(s.get("x") or 0) * int(s.get("y") or 0)
        except (TypeError, ValueError):
            area = 0
        if area >= best_area:
            best, best_area = u, area
    return best


def ladder_plan(img_url: str, has_page: bool) -> list:
    """Ordered rung tags for an image fetch. Pure (fixture-pinned)."""
    plan: list = []
    if (img_url or "").strip():
        plan.append("direct")
        if strip_preview_params(img_url):
            plan.append("preview-source")
    if has_page:
        plan += ["page-candidates", "gallery-full", "video-poster"]
        if (img_url or "").strip():
            plan.append("authed-fetch")
    return plan


def _fetch_url(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.reddit.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_post_image(img_url: str, page=None, perma: str = "",
                     log=None, timeout: int = 30) -> tuple:
    """Run the ladder, first-valid-wins. Returns (bytes, source_tag);
    (b'', 'miss:<why>') when every rung fails. `page` is the running
    bot-Chrome page (may be None: page rungs skipped, direct+strip only)."""
    lg = log or (lambda _m: None)
    tags = ladder_plan(img_url, page is not None)
    if not tags:
        return b"", "miss:no-url-no-page"
    stripped = strip_preview_params(img_url or "")
    http_urls: list = []
    for tag in tags:
        raw = b""
        try:
            if tag == "direct":
                http_urls.append(img_url)
                raw = _fetch_url(img_url, timeout)
            elif tag == "preview-source":
                http_urls.append(stripped)
                raw = _fetch_url(stripped, timeout)
            elif tag == "page-candidates":
                cands = []
                try:
                    cands = page.evaluate(POST_CANDS_JS) or []
                except Exception as e:
                    lg("ladder page-candidates js fail: %s" % type(e).__name__)
                hit = b""
                for u in rank_candidates(cands):
                    http_urls.append(u)
                    try:
                        body = _fetch_url(u, timeout)
                    except Exception:
                        continue
                    if body and vision_bytes_ok(body):
                        hit, raw = u, body
                        break
                    lg("ladder page-candidates skip (%dB) :: %s"
                       % (len(body or b""), u[:60]))
                if not hit:
                    lg("ladder page-candidates: none validated")
                    continue
            elif tag == "gallery-full":
                gurl = ""
                try:
                    gurl = (page.evaluate(GALLERY_JS) or "").strip()
                except Exception as e:
                    lg("ladder gallery-full js fail: %s" % type(e).__name__)
                if not gurl:
                    lg("ladder gallery-full: none in page")
                    continue
                http_urls.append(gurl)
                raw = _fetch_url(gurl, timeout)
            elif tag == "video-poster":
                purl = ""
                try:
                    purl = (page.evaluate(POSTER_JS) or "").strip()
                except Exception as e:
                    lg("ladder video-poster js fail: %s" % type(e).__name__)
                if not purl:
                    lg("ladder video-poster: none in page")
                    continue
                http_urls.append(purl)
                raw = _fetch_url(purl, timeout)
            elif tag == "authed-fetch":
                seen = set()
                for u in [x for x in http_urls + [img_url, stripped] if x]:
                    if u in seen:
                        continue
                    seen.add(u)
                    try:
                        resp = page.request.get(u, timeout=timeout * 1000)
                    except Exception as e:
                        lg("ladder authed-fetch fail: %s: %s"
                           % (type(e).__name__, str(e)[:80]))
                        continue
                    if not resp.ok:
                        lg("ladder authed-fetch http %s :: %s"
                           % (resp.status, u[:60]))
                        continue
                    body = resp.body() or b""
                    if vision_bytes_ok(body):
                        return body, "authed-fetch"
                    lg("ladder authed-fetch bytes rejected (%dB)" % len(body))
                continue
        except Exception as e:
            lg("ladder %s fail: %s: %s" % (tag, type(e).__name__, str(e)[:100]))
            continue
        if raw and vision_bytes_ok(raw):
            flag = ":flagged-acceptable" if tag == "video-poster" else ""
            return raw, tag + flag
        lg("ladder %s rejected (%dB)" % (tag, len(raw or b"")))
    return b"", "miss:all-rungs"
