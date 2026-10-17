# UTF-8
"""Fast pre-scrape for the iOS reply-only commenter (DOM version).

Why DOM + headed Chrome: plain requests get 403 from this network, Arctic
scores are stale for fresh posts, and headless Chrome gets Reddit's
"blocked by network security" page. A VISIBLE bot Chrome
(subscraper/chrome_bot_profile, same as the subscraper) loads the Home feed
fine — 28 posts on first paint.

Gates (user locked): post = approved sub + picture + >=20 upvotes + <=30min.
Top comment >=2 upvotes, so the iPhone only finds it + presses Reply.

  python misc/comment_precache.py --show
  python misc/comment_precache.py --limit 5 --show
  python misc/comment_precache.py --sub ben10 --show
  python misc/comment_precache.py --reply "manual text" --show

Output: logs/comment_queue.json. reply_text is filled when OPENAI_API_KEY /
MIMO_API_KEY is set, else "" for the agent to fill in chat (same as the
verified 22 Sep live run).

After .py edits: python -m py_compile.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import random
import re
import sys
import threading
import time
from friend_poster.services.comment_store_ios import already_commented  # noqa: E402
from friend_poster.core.config import LOG_DIR  # noqa: E402
from friend_poster.core.paths import PROJECT_ROOT

try:
    from friend_poster.services.comment_llm import (describe_image_bytes, fetch_image_bytes,  # noqa: E402
                             fetch_post_image, generate_comment, generate_reply,
                             load_api_key, mime_of)
except Exception:  # pragma: no cover

    def fetch_image_bytes(url, timeout=30):  # type: ignore
        raise RuntimeError("comment_llm missing")

    def fetch_post_image(img_url="", page=None, perma="", log=None,  # type: ignore
                         timeout=30):
        return b"", "miss:comment_llm missing"

    def describe_image_bytes(image_bytes, sub_name="", title="", body="",  # type: ignore
                             api_key="", mime="image/jpeg"):
        return None, "comment_llm missing"

    def generate_reply(post_title="", post_body="", image_analysis="",  # type: ignore
                       parent_comment="", api_key=""):
        return None, "comment_llm missing"

    def generate_comment(title="", body="", image_analysis="", api_key=""):  # type: ignore
        return None, "comment_llm missing"

    def load_api_key():  # type: ignore
        return ""


PHASE1 = [
    "fivenightsatfreddys",
    "thewalkingdeadgame",
    "animequestions",
    "peakyblinders",
]
PHASE2 = PHASE1 + [
    "scoopwhoop",
    "collegerant",
    "eatsandwiches",
    "moonpissing",
    "plantsvszombies",
]
PHASE1_NORM = {re.sub(r"\s+", "", a.lower()) for a in PHASE1}
PHASE2_NORM = {re.sub(r"\s+", "", a.lower()) for a in PHASE2}
PHASES = {"Phase1": PHASE1, "Phase2": PHASE2}
PHASE_NORMS = {"Phase1": PHASE1_NORM, "Phase2": PHASE2_NORM}
APPROVED = list(PHASE1)
APPROVED_NORM = set(PHASE1_NORM)

QUEUE_PATH = os.path.join(LOG_DIR, "comment_queue.json")
ROOT_DIR = str(PROJECT_ROOT)
BOT_PROFILE_DIR = os.path.join(ROOT_DIR, "subscraper", "chrome_bot_profile")
BOT_CDP_PORT = 9223
HOME_URL = "https://www.reddit.com/new/?feed=home"
HOME_FALLBACK_URL = "https://www.reddit.com/"
AUTOMOD = ("automoderator", "modteam")

POST_JS = (
    "() => [...document.querySelectorAll('shreddit-post')].map(e => ({"
    "sub: e.getAttribute('subreddit-name'), "
    "title: e.getAttribute('post-title'), "
    "score: parseInt(e.getAttribute('score') || '0', 10), "
    "ptype: e.getAttribute('post-type'), "
    "created: e.getAttribute('created-timestamp'), "
    "perma: e.getAttribute('permalink'), "
    "comments: parseInt(e.getAttribute('comment-count') || '0', 10), "
    "author: e.getAttribute('author'), "
    "hasVideo: !!e.querySelector('video, shreddit-player'), "
    "hasRedditImg: !!e.querySelector(\"img[src*='redd.it']:not([src*='styles.redditmedia'])\"), "
    "hasGallery: !!e.querySelector('gallery-carousel, shreddit-media-lightbox')})) "
)

COMMENT_JS = (
    "() => [...document.querySelectorAll('shreddit-comment')].map(e => ({"
    "author: e.getAttribute('author'), "
    "score: parseInt(e.getAttribute('score') || '0', 10), "
    "depth: parseInt(e.getAttribute('depth') || '0', 10), "
    "created: e.getAttribute('created-timestamp'), "
    "text: (e.innerText || '').slice(0, 2000)})) "
)

POST_BODY_JS = (
    "() => { const e = document.querySelector('shreddit-post');"
    " if (!e) return '';"
    " const t = e.querySelector('[slot=\"text-body\"]')"
    " || e.querySelector('div[id$=\"-post-rtjson-content\"]');"
    " return (((t && t.innerText) || '').trim().slice(0, 4000)); } "
)

POST_IMG_JS = (    "() => { const pick = (e) => {"
    " const ss = e.getAttribute('srcset') || '';"
    " let best = '', bestW = -1;"
    " for (const part of ss.split(',')) {"
    " const m = part.trim().match(/^(https?:\\S+)\\s+(\\d+)w$/);"
    " if (m && +m[2] > bestW) { bestW = +m[2]; best = m[1]; } }"
    " return best || e.currentSrc || e.src || ''; };"
    " const imgs = [...document.querySelectorAll("
    "'gallery-carousel img, shreddit-media-lightbox img, "
    "img[src*=\\\"preview.redd.it\\\"], img[src*=\\\"i.redd.it\\\"]')]"
    ".map(pick).filter(u => u && !/styles[.]redditmedia/.test(u));"
    " return imgs.length ? imgs[0] : ''; } "
)


def log(msg: str) -> None:
    print(msg, flush=True)


def age_min_of(created_iso: str) -> float:
    try:
        dt = datetime.datetime.fromisoformat(created_iso)
        return (time.time() - dt.timestamp()) / 60.0
    except Exception:
        return 1e9


def is_picture(r: dict) -> bool:
    pt = (r.get("ptype") or "").lower()
    if pt in ("image", "gallery"):
        return True
    if pt == "multi_media" and r.get("hasRedditImg") and not r.get("hasVideo"):
        return True
    return False


_BROWSER_LOCK = threading.Lock()


def is_profile_lock_error(msg: str) -> bool:
    """True when a launch failure is profile contention. Pure."""
    low = (msg or "").lower()
    return ("existing browser session" in low
            or "profile is already in use" in low
            or "profile" in low and "already in use" in low)


def profile_in_cmdline(cmdline: str, profile_dir: str = "") -> bool:
    """True when a process command line carries our bot profile. Pure.

    Matches the full profile path or its basename; never matches a plain
    browsing Chrome (which carries a different --user-data-dir)."""
    cmd = (cmdline or "")
    prof = profile_dir or BOT_PROFILE_DIR
    if not cmd or not prof:
        return False
    low_cmd = cmd.replace("/", "\\").lower()
    low_prof = prof.replace("/", "\\").lower().rstrip("\\")
    if low_prof in low_cmd:
        return True
    base = os.path.basename(low_prof)
    return bool(base) and ("--user-data-dir" in low_cmd and base in low_cmd)


def _chrome_holders():
    """(pid, cmdline) of chrome.exe processes carrying our profile."""
    out = []
    try:
        import json as _json
        import subprocess as _sp
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
              "Select-Object ProcessId,CommandLine | ConvertTo-Json")
        raw = _sp.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            stderr=open(os.devnull, "w"), timeout=30).decode("utf-8", "replace")
        data = _json.loads(raw) if raw.strip() else []
        if isinstance(data, dict):
            data = [data]
        for row in data or []:
            try:
                if profile_in_cmdline(str(row.get("CommandLine") or "")):
                    out.append((int(row.get("ProcessId") or 0),
                                str(row.get("CommandLine") or "")))
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return [h for h in out if h[0] and h[0] != os.getpid()]


def reap_stale_bot_chrome(log=None):
    """Kill chrome.exe holders of OUR bot profile only. Browsing Chrome
    never matches (different --user-data-dir). Returns kills count."""
    lg = log if callable(log) else (lambda _m: None)
    kills = 0
    try:
        import subprocess as _sp
        for pid, cmd in _chrome_holders():
            lg("bot Chrome holder: pid=%d cmd=%.100s" % (pid, cmd))
            try:
                _sp.check_output(["taskkill", "/F", "/PID", str(pid)],
                                 stderr=_sp.STDOUT, timeout=30)
                kills += 1
                lg("bot Chrome holder killed: pid=%d" % pid)
            except Exception as e:
                lg("bot Chrome holder kept pid=%d (%s)" % (pid, str(e)[:80]))
    except Exception as e:
        lg("bot Chrome reap failed: %s" % str(e)[:100])
    return kills


def launch_ctx(pw):
    if not os.path.isdir(BOT_PROFILE_DIR):
        raise RuntimeError("bot Chrome profile missing: " + BOT_PROFILE_DIR)
    return pw.chromium.launch_persistent_context(
        user_data_dir=BOT_PROFILE_DIR,
        channel="chrome",
        headless=False,  # headless gets Reddit's network-security block page
        args=["--no-first-run", "--no-default-browser-check",
              "--start-minimized"],  # headed but never steals focus
    )


def cdp_endpoint() -> str:
    """Local CDP address of the kept bot-Chrome window. Pure."""
    return "http://127.0.0.1:%d" % BOT_CDP_PORT


def _cdp_alive(timeout: float = 3.0) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(cdp_endpoint() + "/json/version",
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _chrome_exe() -> str:
    from shutil import which
    for cand in ("chrome", "chrome.exe"):
        found = which(cand)
        if found:
            return found
    for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if os.path.isfile(path):
            return path
    return ""


def ensure_bot_browser(log=None) -> None:
    """The bot-Chrome window is kept open across runs: reuse it when its CDP
    answers, else launch it once (minimized) and wait for CDP. Never killed
    by a run - closing it is a manual act."""
    lg = log if callable(log) else (lambda _m: None)
    if _cdp_alive():
        lg("bot Chrome reused (kept window)")
        return
    exe = _chrome_exe()
    if not exe:
        raise RuntimeError("chrome binary missing")
    if not os.path.isdir(BOT_PROFILE_DIR):
        raise RuntimeError("bot Chrome profile missing: " + BOT_PROFILE_DIR)
    import subprocess
    cmd = [exe, "--remote-debugging-port=%d" % BOT_CDP_PORT,
           "--user-data-dir=%s" % BOT_PROFILE_DIR,
           "--no-first-run", "--no-default-browser-check",
           "--start-minimized", "about:blank"]
    lg("bot Chrome launch (once, minimized - kept for future runs)")
    with open(os.devnull, "w") as dn:
        subprocess.Popen(cmd, stdout=dn, stderr=dn, stdin=dn)
    deadline = time.time() + 30.0
    while time.time() < deadline and not _cdp_alive():
        time.sleep(1.0)
    if not _cdp_alive():
        raise RuntimeError("bot Chrome CDP unreachable")


def open_scout_context(pw, log=None):
    """(ctx, owned): kept CDP window preferred (tabs never steal focus);
    one-off minimized launch only when keeping is impossible. Single-flight
    process-wide; profile-lock contention reaps stale holders and retries
    the same container instead of aborting."""
    lg = log if callable(log) else (lambda _m: None)
    with _BROWSER_LOCK:
        try:
            ensure_bot_browser(lg)
            browser = pw.chromium.connect_over_cdp(cdp_endpoint(), timeout=30000)
            ctxs = browser.contexts
            ctx = ctxs[0] if ctxs else browser.new_context()
            return ctx, False
        except Exception as e:
            lg("bot CDP unavailable (%s: %s), one-off launch"
               % (type(e).__name__, str(e)[:80]))
            try:
                return launch_ctx(pw), True
            except Exception as e2:
                if not is_profile_lock_error(str(e2)):
                    raise
                lg("bot profile locked, reaping stale holders + retry once")
                reap_stale_bot_chrome(lg)
                time.sleep(3.0)
                try:
                    ensure_bot_browser(lg)
                    browser = pw.chromium.connect_over_cdp(
                        cdp_endpoint(), timeout=30000)
                    ctxs = browser.contexts
                    return (ctxs[0] if ctxs else browser.new_context()), False
                except Exception:
                    return launch_ctx(pw), True


def dedupe_raws(raws: list[dict]) -> list[dict]:
    """Drop repeat permalinks across merged listings. Pure (fixture-pinned)."""
    seen: set[str] = set()
    out: list[dict] = []
    for r in raws or []:
        key = (r or {}).get("perma") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _is_throttle(err) -> bool:
    """True when an exception smells like feed throttling / grey error
    (Reddit 429s served as response-code failures). Pure."""
    low = str(err or "").lower()
    return ("429" in low or "response_code_failure" in low
            or "too many requests" in low or "err_http" in low)


class _FeedThrottled(Exception):
    """Feed loads keep failing — caller should restart the browser once
    and retry (nothing is marked, nothing is sent)."""


def one_pass(page, listing_url: str, min_score: int, max_age_min: float,
             scrolls: int = 10) -> list[dict]:
    """One slow scroll of the feed. Returns raw post dicts (newest first).

    A throttled/grey load gets a force-refresh plus backoff (up to 2
    retries); persistent throttling raises _FeedThrottled. Non-throttle
    failures raise immediately, as before."""
    loads = 0
    while True:
        try:
            page.goto(listing_url, wait_until="domcontentloaded", timeout=45000)
            break
        except Exception as e:
            if not _is_throttle(e):
                raise
            loads += 1
            if loads > 2:
                raise _FeedThrottled(str(e)[:150])
            log(f"feed grey (throttled?), force-refresh {loads}/2…")
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                break
            except Exception:
                pass
            wait = 30 * loads + random.uniform(0, 10)
            log(f"feed still grey, sleeping {wait:.0f}s…")
            time.sleep(wait)
    page.wait_for_timeout(6000)
    raws: list[dict] = []
    seen: set[str] = set()
    for _ in range(max(1, scrolls)):
        for r in page.evaluate(POST_JS):
            key = r.get("perma") or ""
            if key and key not in seen:
                seen.add(key)
                raws.append(r)
        if len(raws) >= 100:
            break
        page.evaluate("window.scrollBy(0, 2500)")
        page.wait_for_timeout(2000)
    log(f"feed: {len(raws)} posts seen (slow scroll)")
    # near-miss table: every approved-sub post with its verdict,
    # so a rejection is auditable and never a silent skip.
    for r in sorted(raws, key=lambda x: x.get("created") or "", reverse=True):
        sub = (r.get("sub") or "").strip()
        if re.sub(r"\s+", "", sub.lower()) not in APPROVED_NORM:
            continue
        age = age_min_of(r.get("created") or "")
        reasons = []
        if int(r.get("score") or 0) < min_score:
            reasons.append(f"{r.get('score')}up<{min_score}")
        if age > max_age_min:
            reasons.append(f"{age:.0f}m>{max_age_min:.0f}m")
        if not is_picture(r):
            reasons.append(f"type={r.get('ptype')}")
        if not reasons and already_commented(sub, r.get("title") or ""):
            reasons.append("USED")
        verdict = "CAND" if not reasons else "SKIP " + "+".join(reasons)
        log(f"  {verdict} r/{sub} {r.get('score')}up {age:.0f}m "
            f"{r.get('ptype')} :: {(r.get('title') or '')[:60]}")
    raws.sort(key=lambda x: x.get("created") or "", reverse=True)
    return raws


def scrape(limit=5, min_score=15, max_age_min=30.0, single_sub="",
           manual_reply="", wait_mins=0.0, interval_s=180):
    from playwright.sync_api import sync_playwright

    if single_sub:
        listing_url = "https://www.reddit.com/r/%s/new/" % re.sub(r"\s+", "", single_sub.lower())
    else:
        listing_url = HOME_URL
    deadline = time.time() + max(0.0, float(wait_mins)) * 60
    stay_open = wait_mins > 0
    with sync_playwright() as pw:
        ctx = launch_ctx(pw)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            attempt = 0
            while True:
                attempt += 1
                log(f"--- pass {attempt} ({time.strftime('%H:%M:%S')}) ---")
                raws = one_pass(page, listing_url, min_score, max_age_min)
                # walk newest-first; click into each post that passes the gate
                out = []
                for r in raws:
                    if len(out) >= limit:
                        break
                    c = passes_post_gate(r, min_score, max_age_min)
                    if not c:
                        continue
                    if any((o["permalink"] == c["permalink"]) for o in out):
                        continue
                    log(f"OPEN r/{c['sub']} {c['score']}up {c['age_min']:.0f}m :: {c['title'][:70]}")
                    try:
                        top = fetch_top_comment(page, c["permalink"])
                    except Exception as e:
                        log(f"WARN comments: {str(e)[:100]}")
                        continue
                    img_url = (top or {}).get("img_url", "")
                    full = enrich(c, top, manual_reply=manual_reply) if (top or {}).get("author") else None
                    if full:
                        out.append(full)
                        log(f"QUEUED [reply] top u/{full['top_author']} {full['top_score']}up :: {c['title'][:60]}")
                    else:
                        # few/no comments -> top-level comment, never force a reply
                        item = enrich_comment_only(c, manual_reply=manual_reply, img_url=img_url)
                        out.append(item)
                        log(f"QUEUED [comment] no reply target, top-level :: {c['title'][:60]}")
                if out:
                    return out
                # nothing qualified: stay in the profile and look again
                if not stay_open or time.time() >= deadline:
                    return out
                left = (deadline - time.time()) / 60
                log(f"EMPTY — staying open, re-scrolling in {interval_s}s ({left:.0f}m left)…")
                time.sleep(interval_s)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return []


def passes_post_gate(r: dict, min_score: int, max_age_min: float) -> dict | None:
    sub = (r.get("sub") or "").strip()
    if re.sub(r"\s+", "", sub.lower()) not in APPROVED_NORM:
        return None
    score = int(r.get("score") or 0)
    if score < min_score:
        return None
    age_min = age_min_of(r.get("created") or "")
    if age_min > max_age_min:
        return None
    title = (r.get("title") or "").strip()
    if not title or not is_picture(r):
        return None
    if already_commented(sub, title):
        return None
    perma = r.get("perma") or ""
    if not perma:
        return None
    return {
        "sub": sub.lower(),
        "title": title[:160],
        "permalink": perma,
        "full_url": "https://www.reddit.com" + perma,
        "score": score,
        "age_min": round(age_min, 1),
        "num_comments": int(r.get("comments") or 0),
        "author": r.get("author") or "",
        "header": f"u/{r.get('author', '')} - {age_min:.0f}m - r/{sub} - "
                  f"{score} upvotes - {r.get('comments', 0)} comments",
        "body": "",
        "img_url": "",
        "post_id": perma.strip("/").split("/")[-2] if "/" in perma else "",
    }


def pick_post_body(cand_body: str, top_body: str) -> str:
    """Post self-text precedence: locked/queue body first, else the live
    post-page capture. '' when neither exists (pure, fixture-pinned)."""
    if (cand_body or "").strip():
        return (cand_body or "").strip()[:2000]
    return (top_body or "").strip()[:2000]


def clean_comment_text(text: str, author: str) -> str:
    """innerText includes vote widgets; strip the head/tail chrome."""
    t = (text or "").replace("\r", "\n")
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    # drop leading author/age/vote lines and trailing Reply/Share rows
    while lines and (
        lines[0].lower() == (author or "").lower()
        or re.match(r"^[\d.]+[khm]?$", lines[0].lower())
        or "ago" in lines[0].lower()
        or lines[0].lower() in ("upvote", "downvote", "reply", "share", "award")
    ):
        lines.pop(0)
    while lines and lines[-1].lower() in ("reply", "share", "award", "upvote", "downvote"):
        lines.pop()
    return "\n".join(lines)[:3000].strip()


def fetch_top_comment(page, perma: str) -> dict:
    page.goto("https://www.reddit.com" + perma, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(6000)
    page.evaluate("window.scrollBy(0, 1200)")
    page.wait_for_timeout(2500)
    rows = page.evaluate(COMMENT_JS)
    # live post score refresh too
    live_score = page.evaluate(
        "() => { const e = document.querySelector('shreddit-post');"
        " return e ? parseInt(e.getAttribute('score') || '0', 10) : 0; }")
    try:
        img_url = page.evaluate(POST_IMG_JS) or ""
    except Exception:
        img_url = ""
    try:
        post_body = (page.evaluate(POST_BODY_JS) or "").strip()[:4000]
    except Exception:
        post_body = ""
    for c in rows:
        if int(c.get("depth") or 0) != 0:
            continue
        author = (c.get("author") or "").strip()
        if not author or author.lower() in AUTOMOD or author == "[deleted]":
            continue
        if int(c.get("score") or 0) < 2:
            continue
        body = clean_comment_text(c.get("text") or "", author)
        if len(body) < 4:
            continue
        return {"author": author, "body": body,
                "score": int(c["score"]),
                "age_min": round(age_min_of(c.get("created") or ""), 1),
                "id": "", "img_url": img_url, "post_body": post_body,
                "live_score": int(live_score or 0)}
    return {"img_url": img_url, "post_body": post_body}


def enrich_comment_only(cand: dict, manual_reply: str = "", img_url: str = "") -> dict:
    """Top-level comment mode: no reply target, react to the post itself."""
    cand["mode"] = "comment"
    cand["top_author"] = ""
    cand["top_body"] = ""
    cand["top_score"] = 0
    cand["top_age_min"] = 0.0
    cand["top_id"] = ""
    if manual_reply:
        cand["reply_text"] = manual_reply
        cand["reply_source"] = "manual"
        return cand
    img_desc: dict = {"text_in_image": "", "content": "", "scene": ""}
    img_text = ""
    key = ""
    try:
        key = load_api_key()
    except Exception:
        key = ""
    if key and img_url:
        try:
            raw, _vsrc = fetch_post_image(img_url, log=log)
            desc, _derr = describe_image_bytes(
                raw, cand["sub"], cand["title"], cand.get("body", ""), key,
                mime=mime_of(raw)) if raw else (None, None)
        except Exception as e:
            log(f"WARN vision: {str(e)[:100]} :: {cand['title'][:60]}")
            desc = None
        if desc:
            img_text = desc
            for line in desc.splitlines():
                up = line.strip().upper()
                if up.startswith("TEXT_IN_IMAGE"):
                    img_desc["text_in_image"] = line.split(":", 1)[-1].strip()
                elif up.startswith("CONTENT"):
                    img_desc["content"] = line.split(":", 1)[-1].strip()
                elif up.startswith("SCENE"):
                    img_desc["scene"] = line.split(":", 1)[-1].strip()
            log(f"VISION {len(desc)} chars :: {cand['title'][:60]}")
        else:
            log(f"WARN vision blank :: {cand['title'][:60]}")
    cand["image_analysis"] = img_desc
    try:
        reply, _err = generate_comment(
            cand["title"], cand.get("body", ""), img_text, key,
            sub=cand["sub"]) if key else (None, "no key")
    except Exception:
        reply = None
    cand["reply_text"] = reply or ""
    cand["reply_source"] = "llm" if reply else "agent-todo"
    return cand


def enrich(cand: dict, top: dict, manual_reply: str = "") -> dict | None:
    if not (top or {}).get("author"):
        return None
    cand["mode"] = "reply"
    # re-check live score: post may have moved while we scrolled
    if top.get("live_score") and top["live_score"] < 20:
        log(f"SKIP (live score dropped to {top['live_score']}) :: {cand['title'][:60]}")
        return None
    if top.get("live_score"):
        cand["score"] = top["live_score"]
    cand["top_author"] = top["author"]
    cand["top_body"] = top["body"]
    cand["top_score"] = top["score"]
    cand["top_age_min"] = top["age_min"]
    cand["top_id"] = top["id"]
    if manual_reply:
        cand["reply_text"] = manual_reply
        cand["reply_source"] = "manual"
        return cand
    img_desc: dict = {"text_in_image": "", "content": "", "scene": ""}
    img_text = ""
    key = ""
    try:
        key = load_api_key()
    except Exception:
        key = ""
    if key and top.get("img_url"):
        try:
            raw, _vsrc = fetch_post_image(top["img_url"], log=log)
            desc, _derr = describe_image_bytes(
                raw, cand["sub"], cand["title"], cand.get("body", ""), key,
                mime=mime_of(raw)) if raw else (None, None)
        except Exception as e:
            log(f"WARN vision: {str(e)[:100]} :: {cand['title'][:60]}")
            desc = None
        if desc:
            img_text = desc
            for line in desc.splitlines():
                up = line.strip().upper()
                if up.startswith("TEXT_IN_IMAGE"):
                    img_desc["text_in_image"] = line.split(":", 1)[-1].strip()
                elif up.startswith("CONTENT"):
                    img_desc["content"] = line.split(":", 1)[-1].strip()
                elif up.startswith("SCENE"):
                    img_desc["scene"] = line.split(":", 1)[-1].strip()
            log(f"VISION {len(desc)} chars :: {cand['title'][:60]}")
        else:
            log(f"WARN vision blank :: {cand['title'][:60]}")
    # Full-context: download the post image captured on the permalink page,
    # describe via gpt-4o-mini, feed the analysis to the reply call.
    cand["image_analysis"] = img_desc
    try:
        parent = "u/%s: %s" % (top["author"], top["body"])
        reply, _err = generate_reply(
            cand["title"], cand.get("body", ""), img_text, parent, key,
            sub=cand["sub"]) if key else (None, "no key")
    except Exception:
        reply = None
    cand["reply_text"] = reply or ""
    cand["reply_source"] = "llm" if reply else "agent-todo"
    return cand


def save_queue(items, phase: str = "Phase1", approved: list | None = None):
    os.makedirs(LOG_DIR, exist_ok=True)
    payload = {
        "gates": {"min_score": 20, "max_age_min": 30, "top_min": 2},
        "phase": phase if phase in PHASES else "Phase1",
        "approved": list(approved) if approved is not None else list(PHASES.get(phase, PHASE1)),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(items),
        "items": items,
    }
    tmp = QUEUE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, QUEUE_PATH)
    return QUEUE_PATH


def main():
    ap = argparse.ArgumentParser(description="Pre-scrape Home/new -> comment_queue.json")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--min-score", type=int, default=15)
    ap.add_argument("--max-age-min", type=float, default=30)
    ap.add_argument("--wait", type=float, default=0,
                    help="stay open and re-scroll until something queues (minutes, 0=one pass)")
    ap.add_argument("--interval", type=int, default=180,
                    help="seconds between re-scrolls while waiting")
    ap.add_argument("--sub", default="")
    ap.add_argument("--reply", default="")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    items = scrape(limit=max(1, args.limit), min_score=args.min_score,
                   max_age_min=args.max_age_min, single_sub=args.sub,
                   manual_reply=args.reply, wait_mins=args.wait,
                   interval_s=args.interval)
    path = save_queue(items)
    log(f"WROTE {path} count={len(items)}")
    if args.show:
        for i, c in enumerate(items, 1):
            print(f"--- [{i}] r/{c['sub']} {c['score']}up {c['age_min']}m")
            print(f"TITLE {c['title']}")
            print(f"URL {c['full_url']}")
            print(f"TOP u/{c['top_author']} ({c['top_score']}up): {c['top_body'][:200]}")
            print(f"REPLY [{c.get('reply_source')}]: {c.get('reply_text') or '(agent-todo)'}")
    if not items:
        log("EMPTY queue: nothing passing gates right now. Retry in a few min.")
        return 1
    if [c for c in items if not c.get("reply_text")]:
        log(f"NOTE: fill reply_text in {path} (no LLM key set).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
