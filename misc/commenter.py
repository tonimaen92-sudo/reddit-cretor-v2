# UTF-8
"""Top-level commenter GUI: roster, PREP (scout), LOCK IN, FIRE.

Top-level/tree-only by construction: no reply path (never taps Reply rows,
never top-level-nests), qwen never called. Replies module absent on purpose.

Conventions mirror creator.py: ttk, worker threads do the work, Tk updates
only via root.after queue-drain, SSH password typed not git, UTF-16 readout
on the locked text.

Flow per slot: PREP (PC scout + read-only login probe, then engine fill for
the auto-pick) -> LOCK IN (candidate facts + editable engine text) -> FIRE
(entry incl. live login probe -> bubble -> composer check -> TYPE locked text
-> screenshot verify -> Post -> strict local VERIFY -> MARK -> close-out).
Budgets + stuck->abort+report + safe-park + no auto-retry + no web-verify.
One JSON run-log line per FIRE.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import scrolledtext, ttk

MISC_DIR = os.path.dirname(os.path.abspath(__file__))
if MISC_DIR not in sys.path:
    sys.path.insert(0, MISC_DIR)

from config import DUMP_DIR, LOG_DIR  # noqa: E402
from crashlog import install as crash_install  # noqa: E402
from reddit_restore import (  # noqa: E402  (shared login/restore flow)
    BACKUP_MAP_PATH,
    DRAWER_INCONCLUSIVE,
    DRAWER_LOGIN_MARKER,
    RESTORE_ATTEMPTS,
    RESTORE_BUDGET,
    _dump_saved,
    accounts_row_username,
    auto_restore,
    canonical_pick,
    drawer_communities,
    drawer_logged_out,
    drawer_login_probe,
    drawer_text,
    drawer_verdict,
    list_row,
    load_backup_map,
    login_via_accounts,
    resolve_backup_file,
    restore_until_alive,
    resume_reddit_card,
    result_rows,
    search_field,
)
from airplane import cycle_on  # noqa: E402
from farm_stats import today_summary as farm_today_summary  # noqa: E402
from stay_awake import allow_sleep, stay_awake  # noqa: E402
from comment_store_ios import already_commented, mark_commented, recently_taken  # noqa: E402
from slots import get_slot, load_slots, retired_ids, slot_reddit_ready  # noqa: E402
from ssh_device import Phone, PhoneError, diagnose_usb, usb_status_line  # noqa: E402
from ui_tree import TapStack, collapsed_text, dump_named  # noqa: E402
from ui_tree import find_continue_gate, find_label_exact  # noqa: E402
from ui_tree import find_update_settings  # noqa: E402
from reddit_open import (  # noqa: E402
    close_reddit, find_picker_row, open_container, slow_list_nudge,
    springboard_reddit, switcher_changed, tap_home_icon, warn_siwa,
)
from reddit_post import (  # noqa: E402
    refuse_join,
)
from comment_precache import (  # noqa: E402
    APPROVED, BOT_PROFILE_DIR, HOME_FALLBACK_URL, HOME_URL, PHASE1,
    PHASE2, PHASE_NORMS, PHASES, _FeedThrottled, age_min_of,
    ensure_bot_browser, fetch_top_comment, is_picture, launch_ctx, one_pass,
    open_scout_context, pick_post_body, reap_stale_bot_chrome,
)

RUNLOG_PATH = os.path.join(LOG_DIR, "commenter_runs.jsonl")
ERROR_LOG_PATH = os.path.join(LOG_DIR, "errors.log")
COMMENTER_SETTINGS_PATH = os.path.join(LOG_DIR, "commenter_settings.json")
DEBUG_PATH = os.path.join(LOG_DIR, "commenter_debug.log")

# UI log discipline: Log tab shows signals + OpenAI/Mimo engine traces.
# Noisy dumps stay in commenter_debug.log only. Matched by prefix.
VERBOSE_PREFIX = (
    "XXTouch OpenAPI", "XXTouch live text dump", "XXTouch screen.size",
    "interp_flick dump", "  - ocr", "Wrote ", "SCOUT skip",
    "tap (", "touch (",
)

# Chilled tab: verdicts + engine stay, scout/entry/fire internals filed.
# Verdict words always win except the never-verdict families below.
CHILL_NEVER = (
    "SCOUT ", "ENTRY ", "FIRE_T+", "STAGED ", "QUARANTINE ", "ROTATION ",
    "FAV ", "USB ", "PnP:", "usbmux ", "Starting ", "Forward ",
    "SSH banner", "SSH up as", "Darwin ", "uid=", "PATH=",
    "XXTouch probe:", "ls: ", "drwx", "/var/", "ii  ", "XXTExplorer",
    "Tap stack:", "BACKEND ", "type_human ", "XXTouch screenshot",
    "ICON-TAP ", "TAP ", "bot Chrome launch", "BODY post-page",
    "OK",
)
CHILL_NOISY = (
    "BATCH ", "FIRE ", "NOLOGIN ", "PREP ",
)
CHILL_VERDICT_WORDS = (
    "verdict", "posted", "skip", "finish", "abort", "marked", "stop",
    "refused", "failed", "fail",
)


def ui_important(msg: str) -> bool:
    """True when a log line belongs on the UI tab. Pure (fixture-pinned)."""
    m = str(msg or "")
    if " nodes, " in m and "labeled" in m:
        return False
    if m.startswith(VERBOSE_PREFIX):
        return False
    if m.startswith(CHILL_NEVER):
        return False
    low = m.lower()
    for w in CHILL_VERDICT_WORDS:
        if w in low:
            return True
    if m.startswith(CHILL_NOISY):
        return False
    return True


def debug_open() -> None:
    """Session separator + size cap for the debug file. Best-effort."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.path.isfile(DEBUG_PATH) and os.path.getsize(DEBUG_PATH) > 5 * 1024 * 1024:
            bak = DEBUG_PATH + ".prev"
            try:
                if os.path.isfile(bak):
                    os.remove(bak)
            except OSError:
                pass
            os.replace(DEBUG_PATH, bak)
        with open(DEBUG_PATH, "a", encoding="utf-8") as f:
            f.write("===== %s =====\n"
                    % datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    except OSError:
        pass


def load_commenter_settings() -> dict:
    try:
        with open(COMMENTER_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_commenter_settings(selected_slot: str = "") -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(COMMENTER_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump({"selected_slot": (selected_slot or "").strip()}, f, indent=2)
            f.write("\n")
    except OSError:
        pass
WALL = ("create an account to continue", "verify your email",
        "verification code", "create your username")
BUDGETS = {"ACQUIRE": 180.0, "GATE": 60.0, "TYPE": 90.0, "SEND": 90.0,
           "VERIFY": 60.0}
TIERS = (("T1", 12, 30.0), ("T2", 8, 60.0), ("T3", 5, 120.0))
APPROVED_NORM = {re.sub(r"\s+", "", a.lower()) for a in APPROVED}
PHASE_DEFAULT = "Phase2"
BATCH_ORDER = ("a0003", "a0008", "a0009", "a0010",
               "a0014", "a0015", "a0017", "a0018",
               "a0021", "a0022", "a0023", "a0024",
               "a0025", "a0026", "a0027", "a0028",
               "a0029", "a0030", "a0031", "a0032",
               "a0033", "a0034", "a0035")


def phase_approved(phase: str) -> list:
    """Approved subs for a phase. Pure."""
    return list(PHASES.get(phase, PHASE1))


def phase_norm(phase: str) -> frozenset:
    """Normalized allow-set for a phase. Pure."""
    return frozenset(PHASE_NORMS.get(phase, PHASE_NORMS["Phase1"]))


WF1_TRIES_PER_STAGE = 6


def seeded_shuffle(items: list, seed: int) -> list:
    """Deterministic shuffle: same seed gives the same order. Pure."""
    import random as _random
    out = list(items or [])
    _random.Random(int(seed)).shuffle(out)
    return out


def rotation_pick_in_order(passing: list, order: list, idx: int) -> tuple:
    """Rotation over a caller-supplied sub order (per-run shuffle).
    Returns (pick_or_None, next_idx). Pure, no file state."""
    cands = [c for c in (passing or []) if c.get("tier")]
    if not cands:
        return None, int(idx or 0)
    norm_order = [re.sub(r"\s+", "", str(a).lower()) for a in (order or [])]
    if not norm_order:
        top = sorted(cands, key=lambda c: (-(c.get("score") or 0),))
        return top[0], int(idx or 0)
    n = len(norm_order)
    i = int(idx or 0) % n
    for off in range(n):
        want = norm_order[(i + off) % n]
        hits = [c for c in cands
                if re.sub(r"\s+", "", str(c.get("sub") or "").lower()) == want]
        if hits:
            best = max(hits, key=lambda c: (c.get("score") or 0,))
            return best, (norm_order.index(want) + 1) % n
    top = sorted(cands, key=lambda c: (-(c.get("score") or 0),))
    return top[0], int(idx or 0)


def try_order(ranked: list, auto: dict | None) -> list:
    """Workflow-1 candidate order: rotation autopick first (best suited),
    then remaining passing posts by score desc. Pure."""
    passing = [c for c in (ranked or []) if c.get("tier")]
    ordered: list = []
    auto_perma = (auto or {}).get("permalink") if isinstance(auto, dict) else ""
    if isinstance(auto, dict) and auto.get("tier"):
        ordered.append(auto)
    rest = sorted(
        (c for c in passing
         if c is not auto and (not auto_perma or c.get("permalink") != auto_perma)),
        key=lambda c: (-(c.get("score") or 0),),
    )
    ordered.extend(rest)
    return ordered


def parse_dry_perma(url: str) -> str:
    """Permalink path from a full post URL or bare path. '' when unparseable."""
    m = re.search(r"/r/[A-Za-z0-9_]+/comments/[A-Za-z0-9]+(?:/[A-Za-z0-9_]+)?/?",
                  (url or "").strip())
    if not m:
        return ""
    perma = m.group(0)
    if not perma.endswith("/"):
        perma += "/"
    return perma
TEST_SUBS = ("bossfights", "scoopwhoop", "mylittlepony", "goodvibes",
             "CasualConversation", "CollegeRant", "GetStudying",
             "eatsandwiches", "MoonPissing", "PlantsVSZombies",
             "desperatehousewives", "cryptids", "planespotting",
             "momentsbeforedisaster", "miniatures", "motiondesign",
             "indiedev", "2sentence2horror", "metroid", "2000s")
TEST_SUBS_NORM = {re.sub(r"\s+", "", a.lower()) for a in TEST_SUBS}

# uiopen entry (locked 26 Sep 2026, T1-T6 trial: routing 6/6, verification
# 5/5, fallback 0; Safari fully removed same day - no Safari taps, launches,
# bookmarks, or tabs anywhere in this flow). Dispatch on every FIRE: slot
# staged + login probe first (always), then `uiopen --url <permalink>`.
# Entry miss aborts + quarantines (ENTRY_ABORTS); removed posts are DEAD.
DEAD_REASON = "post removed (DEAD)"


def uiopen_land_proof(blob: str, sub: str, title: str = "",
                      author: str = "") -> tuple[str, str]:
    """Landing proof for uiopen dispatch. App-post = sub + title words + author
    leg. Comment-bar tokens are bonus only, never required (T1 lesson).
    Author leg tolerates OCR ellipsis ('u/Parsl...e6746'): exact prefix-5, else
    any 'u/' header token when the expected author is unknown. Removed-post =
    dispatch-OK/content-gone: item DEAD/skipped, never retried, counted in
    routing stats, excluded from verification stats."""
    b = (blob or "").lower()
    nospace = b.replace(" ", "")
    if "search or enter website" in b or "favorites" in b and "reddit.com" not in b:
        return "browser-start-page", "safari-start-markers"
    if "open in the reddit" in b or ("reddit.com" in b and "safari" in b):
        return "browser", "safari-reddit-markers"
    sub_hit = sub.lower().replace(" ", "") in nospace
    alnum = re.sub(r"[^a-z0-9 ]+", "", b)
    alnospace = alnum.replace(" ", "")
    twords = [re.sub(r"[^a-z0-9]+", "", w.lower()) for w in (title or "").split()]
    twords = [w for w in twords if len(w) >= 4]
    thits = [w for w in twords if w in alnospace]
    apref = "".join(ch for ch in (author or "").lower() if ch.isalnum())[:5]
    author_hit = bool(apref) and apref in nospace.replace("/", "")
    uloose = "u/" in b
    bonus = [t for t in ("best comments", "add a comment", "join the conversation",
                         "reply", "upvote") if t in b]
    proof = "sub=%s title=%d/%d author=%s bonus=%s" % (
        sub_hit, len(thits), len(twords),
        "exact" if author_hit else ("loose-u/" if uloose else "miss"),
        "+".join(bonus) if bonus else "none")
    removed_markers = ("removed by moderator", "this post was removed",
                       "removed by reddit", "content has been removed")
    if sub_hit and len(thits) < 2 and any(m in b for m in removed_markers):
        return "removed-post", proof + " removed-markers"
    if sub_hit and len(thits) >= 2 and (author_hit or uloose or not (author or "").strip()):
        return "app-post", proof
    if sub_hit and twords and len(thits) == len(twords) and (author_hit or uloose or bonus):
        return "app-post", proof + " short-title-clause"
    if sub_hit and 0 < len(twords) <= 2 and len(thits) >= 1 \
            and (author_hit or uloose or bonus):
        return "app-post", proof + " short-title-1hit"
    if sub_hit and not twords and (author_hit or uloose) and bonus:
        return "app-post", proof + " no-title-words"
    if "reddit" in b:
        return "app-ambiguous", proof
    return "unknown", proof


def distinct_word(text: str) -> str:
    words = [w.strip(".,!?\"'").lower() for w in (text or "").split()]
    return next((w for w in sorted(words, key=len, reverse=True) if len(w) >= 5), "")


def pick_another(ranked, exclude: str):
    """Next-best tiered candidate excluding one permalink (pure)."""
    pool = [c for c in (ranked or []) if c.get("permalink") != (exclude or "")]
    for tier in ("T1", "T2", "T3"):
        for c in pool:
            if c.get("tier") == tier:
                return c
    return None


def need_vision(img_url: str, tier: str) -> bool:
    """Picture posts need vision: URL present, or a tiered (picture-gated) pick."""
    return bool(img_url) or tier in ("T1", "T2", "T3")


def lockin_fireable(locked, text) -> bool:
    """FIRE gate: locked text present; image posts need vision_ok."""
    if not locked or not (text or "").strip():
        return False
    eng = (locked or {}).get("_engine") or {}
    if eng and eng.get("has_image") and not eng.get("vision_ok"):
        return False
    return True


def slot_usable(row: dict | None) -> bool:
    """Roster-read guard: retired ids never batch (a0019 frozen-scroll)."""
    sid = str((row or {}).get("id") or "").strip().lower()
    if not sid or sid in retired_ids():
        return False
    return True


def slot_refused(sid: str) -> bool:
    return (sid or "").strip().lower() in retired_ids()


BUILD_TAG = "20260926-auto-restore"


def streak_step(pos, verdict: str) -> int:
    """Cleans extend toward the 20-bar; aborts reset."""
    if verdict == "posted":
        return min(int(pos or 0) + 1, 20)
    return 0


def typed_echo_ok(blob: str, text: str) -> bool:
    """Value-law tree echo: every content word + capital-first in the dump."""
    low = (blob or "").lower()
    words = [w.strip(".,!?\"'").lower() for w in (text or "").split()]
    need = [w for w in words if len(w) >= 4]
    if not need or not all(w in low for w in need):
        return False
    first = (text or "").split()
    return bool(first) and first[0] in (blob or "")


QUARANTINE_RUNS = 3
QUARANTINE_PATH = os.path.join(LOG_DIR, "commenter_quarantine.json")
ENTRY_ABORTS = {"uiopen entry miss", "wrong post"}
LOGOUT_FLAGS = ("logged-out at feed", "logged-out at composer", "logged-out at entry")


def same_container(verified: str, sid: str) -> bool:
    """Login is verified once per container switch: a send may only run on
    the verified session (empty verified allows the first open). Pure."""
    if not (verified or "").strip():
        return True
    return (verified or "").strip().lower() == (sid or "").strip().lower()


HAM_CHARS = set("=≡-–—|")
# SYSTEM-WIDE RULE (user order): after a successful restore, Reddit is
# reopened by resuming its switcher card (the already-opened app). Fresh
# icon-tap relaunch goes through the container list and bugs the restored
# session, so it is banned on the post-restore path.
RESUME_VIA_SWITCHER = True


# _dump_saved / drawer_logged_out live in reddit_restore (re-exported above).


ROTATION_PATH = os.path.join(LOG_DIR, "sub_rotation.json")


def rotation_pick(passing: list) -> dict | None:
    """Even rotation across phase subs: next passing sub at/after the saved
    pointer wins (best score among its rows), pointer advances past it.
    Subs outside the phase order fall back to score order. Best-effort."""
    cands = [c for c in (passing or []) if c.get("tier")]
    if not cands:
        return None
    order = [re.sub(r"\s+", "", a.lower()) for a in PHASE2]
    idx = 0
    try:
        with open(ROTATION_PATH, "r", encoding="utf-8") as f:
            idx = int((json.load(f) or {}).get("idx") or 0)
    except (OSError, ValueError, TypeError):
        idx = 0
    idx = idx % max(1, len(order))
    for off in range(len(order)):
        want = order[(idx + off) % len(order)]
        hits = [c for c in cands
                if re.sub(r"\s+", "", str(c.get("sub") or "").lower()) == want]
        if hits:
            best = max(hits, key=lambda c: (c.get("score") or 0,))
            nxt = (order.index(want) + 1) % len(order)
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                with open(ROTATION_PATH, "w", encoding="utf-8") as f:
                    json.dump({"idx": nxt}, f)
                    f.write("\n")
            except OSError:
                pass
            return best
    top = sorted(cands, key=lambda c: (-(c.get("score") or 0),))
    return top[0]
SPACING_PATH = os.path.join(LOG_DIR, "batch_spacing.json")
BATCH_EXTRA_HUNTS = 4


def batch_attempt_cap(n: int, extra: int = BATCH_EXTRA_HUNTS) -> int:
    """Batch hunts until N posted, not N attempts: cap bounds the hunt so an
    empty feed ends the batch instead of looping forever. Pure."""
    return max(1, int(n or 0)) + max(0, int(extra or 0))


def fleet_sids(start_sid: str) -> list:
    """Usable BATCH_ORDER tail from the selected slot; behind skipped."""
    try:
        rows = load_slots()
    except Exception:
        rows = []
    usable = {str(r.get("id") or "") for r in rows if slot_usable(r)}
    start = (start_sid or "").strip().lower()
    order = [o.lower() for o in BATCH_ORDER]
    if start and start in order:
        tail = list(BATCH_ORDER)[order.index(start):]
    elif start:
        return [start_sid] if start_sid in usable else []
    else:
        tail = list(BATCH_ORDER)
    return [o for o in tail if o in usable and not slot_refused(o)]


# accounts_row_username lives in reddit_restore (re-exported above).


def _last_posted(outcomes: list) -> bool:
    """True when the most recent outcome posted. Skips/errors wait nothing."""
    try:
        return bool(outcomes) and outcomes[-1].get("verdict") == "posted"
    except (AttributeError, IndexError, TypeError):
        return False


def batch_order_add(path: str, sid: str) -> tuple | None:
    """Append a slot to the BATCH_ORDER tuple in source. Returns the new
    tuple, or None when sid is already listed / file unparseable. Pure-ish
    (one file write, caller backups first)."""
    import re as _re
    sid = (sid or "").strip().lower()
    if not sid:
        return None
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    m = _re.search(r"BATCH_ORDER\s*=\s*\((.*?)\)", text, _re.DOTALL)
    if not m:
        return None
    have = _re.findall(r'"([^"]+)"', m.group(1))
    if sid in [h.strip().lower() for h in have]:
        return None
    have = have + [sid]
    lines = ["BATCH_ORDER = ("]
    for i in range(0, len(have), 4):
        chunk = ", ".join('"%s"' % h for h in have[i:i + 4])
        lines.append("               " + chunk + ("," if i + 4 < len(have) else ")"))
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text[:m.start()] + "\n".join(lines) + text[m.end():])
    except OSError:
        return None
    return tuple(have)


def posted_today(rows: list) -> set:
    """Slots with a posted verdict today (UTC day). Pure.

    Source is the run log (commenter_runs.jsonl rows): batch summary and
    alert rows carry 'batch'/'alert' keys and never count. Undated rows
    cannot prove today and never count."""
    from datetime import datetime as _dt, timezone as _tz
    today = _dt.now(_tz.utc).date().isoformat()
    out: set = set()
    for r in rows or []:
        if not isinstance(r, dict) or "batch" in r or "alert" in r:
            continue
        if str(r.get("verdict") or "") != "posted":
            continue
        sid = str(r.get("slot") or "").strip().lower()
        if not sid:
            continue
        ts = str(r.get("ts") or "")
        day = ""
        try:
            day = _dt.fromisoformat(ts.replace("Z", "+00:00")).date().isoformat()
        except (ValueError, TypeError):
            day = ts[:10] if len(ts) >= 10 else ""
        if day == today:
            out.add(sid)
    return out


def voice_ok(text: str, evidence: str = "") -> tuple:
    """Deterministic voice self-check for batch auto-send (no per-comment
    approval). Fail = skip the item, never send. Rules: 3-40 words, capital
    first, no links/mentions/hashtags, not shouty, no stacked punctuation.
    With evidence (title+body+image-text): any capitalized non-first word
    must appear there, else ungrounded-name (never invent characters)."""
    t = (text or "").strip()
    words = t.split()
    if not t:
        return False, "empty"
    if not (3 <= len(words) <= 40):
        return False, "word-count %d" % len(words)
    if not t[0].isupper():
        return False, "capital-first"
    low = t.lower()
    if "http" in low or "www." in low:
        return False, "link"
    if any(w.startswith(("@", "#")) for w in words):
        return False, "mention-tag"
    letters = [c for c in t if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.8:
        return False, "shouty"
    import re as _re
    if _re.search(r"[!?.]{2,}", t):
        return False, "stacked-punct"
    if evidence:
        ev = " " + _re.sub(r"[^a-z0-9]+", " ", (evidence or "").lower()) + " "
        for w in words[1:]:
            base = _re.sub(r"[^a-z]+", "", w.lower())
            if len(base) >= 4 and w[:1].isupper() and (" " + base + " " not in ev):
                return False, "ungrounded-name %s" % w.strip(".,!?\"'")
    return True, ""


def draw_break_minutes(rng, escalated: bool = False) -> float:
    """BREAK RULE: uniform random 2.0-3.0 min per interval, fresh draw every
    time (fixed cadences are machine signatures). Escalated floor: 5.0-6.0."""
    if escalated:
        return 5.0 + rng.random() * 1.0
    return 2.0 + rng.random() * 1.0


SPREAD_LO_MIN = 20.0
SPREAD_HI_MIN = 60.0
SPREAD_START_H = 9
SPREAD_END_H = 23
SPREAD_RESERVE_MIN = 8.0


def spread_day_end_ts(now_ts: float | None = None) -> float:
    """PC-local 23:00 today as epoch. Pure-ish (clock default)."""
    import time as _t
    now = now_ts if now_ts is not None else _t.time()
    lt = _t.localtime(now)
    end = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, SPREAD_END_H,
                     0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
    return float(end)


def draw_spread_minutes(rng, remaining_slots: int,
                        now_ts: float | None = None) -> tuple[float, bool]:
    """Uniform 20-60min gap, fresh draw every time, clamped to fit the
    9am-11pm window (8min work reserve per remaining slot). Returns
    (minutes, clamped). Floor 5.0 (post-send cooldown subsumed). Pure."""
    import time as _t
    lo, hi = SPREAD_LO_MIN, SPREAD_HI_MIN
    now = now_ts if now_ts is not None else _t.time()
    remain = max(0, int(remaining_slots or 0))
    clamped = False
    if remain > 0:
        left = (spread_day_end_ts(now) - now) / 60.0
        fit = (left - SPREAD_RESERVE_MIN * remain) / remain
        if fit < hi:
            hi = max(5.0, fit)
            clamped = True
    lo = min(lo, hi)
    lo = max(5.0, lo)
    if hi <= lo:
        return round(hi, 1), clamped
    return round(lo + rng.random() * (hi - lo), 1), clamped


def escalation_due(spacings, reason: str, now_ts: float) -> bool:
    """True on logout/flag abort within 24h of sub-5 spacing: escalate to
    5-min + immediate report. Pure (fixture-pinned)."""
    if not any((reason or "").startswith(f) for f in LOGOUT_FLAGS):
        return False
    for s in spacings or []:
        try:
            if now_ts - float(s.get("ts") or 0) < 86400 and float(s.get("minutes") or 0) < 5.0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def spacing_load() -> dict:
    try:
        with open(SPACING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("spacings", [])
            data.setdefault("escalated", False)
            return data
    except (OSError, ValueError):
        pass
    return {"spacings": [], "escalated": False}


def spacing_save(state: dict) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(SPACING_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
    except OSError:
        pass


def q_tick_dict(d: dict) -> dict:
    """One PREP run passes: every quarantine counter drops, zeros release."""
    out = {}
    for k, v in (d or {}).items():
        try:
            left = int(v) - 1
        except (TypeError, ValueError):
            continue
        if left > 0:
            out[k] = left
    return out


def q_blocked(d: dict, perma: str) -> bool:
    return int((d or {}).get(perma or "", 0) or 0) > 0


def _q_load() -> dict:
    try:
        if os.path.isfile(QUARANTINE_PATH):
            with open(QUARANTINE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


def _q_save(d: dict) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(QUARANTINE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.write("\n")


def quarantine_tick() -> None:
    try:
        _q_save(q_tick_dict(_q_load()))
    except OSError:
        pass


def quarantine_add(perma: str, k: int = QUARANTINE_RUNS) -> None:
    if not perma:
        return
    try:
        d = _q_load()
        d[perma] = int(k)
        _q_save(d)
    except OSError:
        pass


NEVER_PATH = os.path.join(LOG_DIR, "never_touch.json")
BREAKS_PATH = os.path.join(LOG_DIR, "sub_breaks.json")
NSFW_PATH = os.path.join(LOG_DIR, "nsfw_posts.json")


def _nsfw_load() -> dict:
    try:
        if os.path.isfile(NSFW_PATH):
            with open(NSFW_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


def nsfw_add(sub: str, title: str) -> None:
    """Permanent explicit-content verdict. Best-effort, never raises."""
    try:
        from comment_store_ios import post_key as _pk
        key = _pk(sub, title)
        if not key:
            return
        d = _nsfw_load()
        d[key] = {"reason": "explicit", "when": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")}
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(NSFW_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except Exception:
        pass


def nsfw_block(sub: str, title: str) -> bool:
    try:
        from comment_store_ios import post_key as _pk
        key = _pk(sub, title)
    except Exception:
        return False
    if not key:
        return False
    return key in _nsfw_load()


def _never_load() -> dict:
    try:
        if os.path.isfile(NEVER_PATH):
            with open(NEVER_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


def never_block(sub: str, title: str) -> bool:
    """Permanent never-touch list (mod/bot-callout incidents). Pure-ish."""
    try:
        from comment_store_ios import post_key as _pk
        key = _pk(sub, title)
    except Exception:
        return False
    if not key:
        return False
    return key in _never_load()


def _breaks_load() -> dict:
    """Slot -> [subs] on break (user-ordered pauses). Best-effort."""
    try:
        if os.path.isfile(BREAKS_PATH):
            with open(BREAKS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


def sub_break_block(sid: str, sub: str) -> bool:
    """True when sid is on break from sub. Pure-ish."""
    try:
        subs = _breaks_load().get((sid or "").strip().lower()) or []
        sn = re.sub(r"\s+", "", (sub or "").lower())
        return any(sn == re.sub(r"\s+", "", str(s).lower()) for s in subs)
    except Exception:
        return False


BASELINE_PATH = os.path.join(LOG_DIR, "commenter_baseline.json")


def streak_update(verdict: str, reason: str = "", fingerprint: str = "") -> int:
    """Persist streak pos/fingerprint next to BASELINE #1. Pure IO."""
    pos = 0
    try:
        data: dict = {}
        if os.path.isfile(BASELINE_PATH):
            with open(BASELINE_PATH, encoding="utf-8") as f:
                data = json.load(f) or {}
        if not isinstance(data, dict):
            data = {}
        st = data.setdefault("streak", {"pos": 0, "bar": 20, "fingerprint": ""})
        pos = streak_step(st.get("pos", 0), verdict)
        st["pos"] = pos
        st["bar"] = 20
        st["fingerprint"] = "" if verdict == "posted" else str(fingerprint or reason or verdict)[:300]
        with open(BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except OSError:
        pass
    return pos


def utf16_len(text: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def last_mark_for(container: str) -> dict:
    """Newest commented_posts row for a slot. Pure read. {} when none."""
    try:
        with open(os.path.join(LOG_DIR, "commented_posts.json"), encoding="utf-8") as f:
            posts = (json.load(f) or {}).get("posts") or {}
    except (OSError, ValueError):
        return {}
    best: dict = {}
    for row in posts.values():
        if not isinstance(row, dict) or str(row.get("container") or "") != container:
            continue
        if not best or float(row.get("ts") or 0) > float(best.get("ts") or 0):
            best = row
    return best


def run_log(entry: dict) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        entry = dict(entry)
        entry["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(RUNLOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def error_line(slot: str, sub: str, title: str, verdict: str, reason: str) -> str:
    """One errors.log row: timestamp, slot, sub, title, verdict, reason. Pure."""
    return "%s | %s | r/%s | %s | %s | %s" % (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        (slot or "?").strip() or "?",
        (sub or "?").strip() or "?",
        ((title or "?").strip() or "?")[:80],
        (verdict or "?").strip() or "?",
        ((reason or verdict) or "?").strip()[:160] or "?",
    )


def error_log(slot: str, sub: str, title: str, verdict: str, reason: str) -> None:
    """Append-only errors notepad for fix updates. Best-effort."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(error_line(slot, sub, title, verdict, reason) + "\n")
    except OSError:
        pass

# list_row / result_rows / canonical_pick live in reddit_restore
# (re-exported above).


def profile_url(username: str) -> str:
    """Reddit profile permalink for a slot username. '' when blank. Pure."""
    u = (username or "").strip().lstrip("u/")
    return "https://www.reddit.com/user/%s/" % u if u else ""


def _nyc_tz():
    """America/New_York for stats days (Today resets at NYC midnight)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return timezone.utc


def nyc_today() -> str:
    """Current day in NYC. Pure-ish (clock read)."""
    try:
        return datetime.now(_nyc_tz()).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def mark_day(ts) -> str:
    """NYC day bucket for a mark timestamp. '' when unparseable."""
    try:
        return datetime.fromtimestamp(float(ts), tz=_nyc_tz()).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def stats_from_marks(posts: dict) -> dict:
    """Per-container totals + per-day counts from the marks store.
    {container: {"total": n, "by_day": {day: n}, "last": row}}."""
    out: dict = {}
    for row in (posts or {}).values():
        if not isinstance(row, dict):
            continue
        cid = str(row.get("container") or "").strip() or "(none)"
        day = mark_day(row.get("ts"))
        st = out.setdefault(cid, {"total": 0, "by_day": {}, "last": None})
        st["total"] += 1
        if day:
            st["by_day"][day] = st["by_day"].get(day, 0) + 1
        try:
            cur = float((st["last"] or {}).get("ts") or 0)
            new = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            cur, new = 0, 0
        if new >= cur:
            st["last"] = row
    return out


def stats_from_runs(rows: list) -> dict:
    """Per-slot posted/abort counts from run-log rows. Batch summary rows
    (which carry a 'batch' key) are skipped. {slot: {verdict: n}}."""
    out: dict = {}
    for r in rows or []:
        if not isinstance(r, dict) or "batch" in r or "alert" in r:
            continue
        sid = str(r.get("slot") or "").strip() or "(none)"
        v = str(r.get("verdict") or "").strip() or "?"
        st = out.setdefault(sid, {})
        st[v] = st.get(v, 0) + 1
    return out


class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("iPhone Reddit commenter (top-level only)")
        self.root.geometry("980x720")
        self.root.minsize(820, 600)
        self._apply_dark_theme()
        self.log_q: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.phone: Phone | None = None
        self.cands: list[dict] = []
        self.locked: dict | None = None
        self.last_fire: tuple | None = None
        self.last_verdict = ""
        self.batch_auto_path = ""
        self._prog_select = False
        self._wf1_sub_order: list = []
        self._wf1_sub_idx = 0
        self._instance_lock = None
        try:
            import msvcrt as _msvcrt
            _lf = open(os.path.join(LOG_DIR, "commenter.lock"), "a+b")
            try:
                _msvcrt.locking(_lf.fileno(), _msvcrt.LK_NBLCK, 1)
                self._instance_lock = _lf
            except OSError:
                try:
                    _lf.close()
                except Exception:
                    pass
                self._second_instance = True
            else:
                self._second_instance = False
        except Exception:
            self._second_instance = False

        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True)

        self.status = tk.StringVar(value="USB: probing…")
        ttk.Label(frm, textvariable=self.status, wraplength=920).pack(fill="x", **pad)

        row = ttk.Frame(frm)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="SSH password (not git):").pack(side="left")
        self.pw = ttk.Entry(row, show="*", width=24)
        self.pw.pack(side="left", padx=6)
        ttk.Button(row, text="Refresh roster", command=self.on_refresh).pack(side="left", padx=6)
        self.phase = ttk.Combobox(row, width=8, state="readonly",
                                  values=("Phase1", "Phase2"))
        self.phase.set(PHASE_DEFAULT)
        self.phase.pack(side="left", padx=6)
        self.prep_btn = ttk.Button(row, text="PREP selected", command=self.on_prep)
        self.prep_btn.pack(side="left", padx=6)
        self.prep_nologin_btn = ttk.Button(row, text="PREP no-login", command=self.on_prep_nologin)
        self.prep_nologin_btn.pack(side="left", padx=6)
        self.batch_n = ttk.Combobox(row, width=3, state="readonly",
                                    values=("1", "2", "3", "4", "5", "6", "7", "8"))
        self.batch_n.set("3")
        self.batch_n.pack(side="left")
        ttk.Label(row, text="Min").pack(side="left", padx=(6, 0))
        self.batch_floor = ttk.Combobox(row, width=3, state="readonly",
                                        values=("2", "3", "4", "5"))
        self.batch_floor.set("5")
        self.batch_floor.pack(side="left")
        ttk.Label(row, text="Age").pack(side="left", padx=(6, 0))
        self.batch_age = ttk.Combobox(row, width=4, state="readonly",
                                      values=("120", "180", "360"))
        self.batch_age.set("360")
        self.batch_age.pack(side="left")
        self.batch_btn = ttk.Button(row, text="Start batch", command=self.on_batch)
        self.batch_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(row, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left")

        ttk.Label(
            frm,
            text="Top-level comments only. PREP scouts (tiered T1/T2/T3) + fills engine text. "
            "LOCK IN shows facts + editable text. FIRE posts it. "
            "Keep the black console window open (crash log only) — all progress shows in Log.",
            wraplength=920,
        ).pack(fill="x", **pad)

        nb = ttk.Notebook(frm)
        nb.pack(fill="both", expand=True, padx=8, pady=4)
        self.nb = nb
        self.roster_tab = ttk.Frame(nb)
        self.lock_tab = ttk.Frame(nb)
        self.log_tab = ttk.Frame(nb)
        nb.add(self.roster_tab, text="Roster")
        nb.add(self.lock_tab, text="Lock in")
        nb.add(self.log_tab, text="Log")
        self.stats_tab = ttk.Frame(nb)
        nb.add(self.stats_tab, text="Stats")

        cols = ("slot", "user", "last")
        self.tree = ttk.Treeview(
            self.roster_tab, columns=cols, show="headings", selectmode="browse", height=10
        )
        self.tree.heading("slot", text="Slot")
        self.tree.heading("user", text="Reddit user")
        self.tree.heading("last", text="Last comment (when / where)")
        self.tree.column("slot", width=70, anchor="w")
        self.tree.column("user", width=180, anchor="w")
        self.tree.column("last", width=560, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.tree.bind("<<TreeviewSelect>>", self._on_roster_chosen)

        cand_box = ttk.LabelFrame(self.roster_tab, text="Candidates (auto-pick highlighted)")
        cand_box.pack(fill="both", expand=True, padx=8, pady=4)
        ccols = ("pick", "sub", "score", "age", "verdict", "title")
        self.ctree = ttk.Treeview(
            cand_box, columns=ccols, show="headings", selectmode="browse", height=7
        )
        self.ctree.heading("pick", text="")
        self.ctree.heading("sub", text="Sub")
        self.ctree.heading("score", text="Up")
        self.ctree.heading("age", text="Age")
        self.ctree.heading("verdict", text="Verdict")
        self.ctree.heading("title", text="Title")
        self.ctree.column("pick", width=36, anchor="center")
        self.ctree.column("sub", width=150, anchor="w")
        self.ctree.column("score", width=50, anchor="e")
        self.ctree.column("age", width=70, anchor="e")
        self.ctree.column("verdict", width=170, anchor="w")
        self.ctree.column("title", width=330, anchor="w")
        self.ctree.pack(fill="both", expand=True, padx=8, pady=8)
        self.ctree.bind("<<TreeviewSelect>>", self._on_cand_chosen)

        lock = ttk.Frame(self.lock_tab)
        lock.pack(fill="both", expand=True, padx=12, pady=10)
        ttk.Label(lock, text="Container").grid(row=0, column=0, sticky="w", pady=2)
        self._lock_slot_ids: dict[str, str] = {}
        self.lock_slot = ttk.Combobox(lock, width=40, state="readonly")
        self.lock_slot.grid(row=0, column=1, sticky="w", padx=8, pady=2)
        self.lock_slot.bind("<<ComboboxSelected>>", lambda _e: self._on_lock_slot())
        ttk.Label(lock, text="Post").grid(row=1, column=0, sticky="w", pady=2)
        self.lock_link = ttk.Entry(lock, width=90)
        self.lock_link.grid(row=1, column=1, sticky="we", padx=8, pady=2)
        ttk.Label(lock, text="Facts").grid(row=2, column=0, sticky="w", pady=2)
        self.lock_facts = ttk.Label(lock, text="—", wraplength=760, justify="left")
        self.lock_facts.grid(row=2, column=1, sticky="w", padx=8, pady=2)
        ttk.Label(lock, text="Text").grid(row=3, column=0, sticky="nw", pady=2)
        self.lock_text = scrolledtext.ScrolledText(lock, wrap="word", height=4, font=("Consolas", 11))
        self.lock_text.grid(row=3, column=1, sticky="we", padx=8, pady=2)
        self._darken_text(self.lock_text)
        self.lock_len = ttk.Label(lock, text="UTF-16: 0")
        self.lock_len.grid(row=4, column=1, sticky="w", padx=8)
        self.lock_text.bind("<KeyRelease>", lambda _e: (self._update_len(), self._sync_buttons()))
        self.lock_link.bind("<KeyRelease>", lambda _e: self._sync_buttons())
        self.lock_link.bind("<FocusOut>", lambda _e: self._sync_buttons())
        self.thumb = ttk.Label(lock, text="no image", background="#2d2d2d",
                               foreground="#e0e0e0", anchor="center",
                               width=40)
        self.thumb.grid(row=1, column=2, rowspan=3, sticky="ns", padx=8, pady=2)
        self._thumb_ref = None
        self.engine_open = False
        self.engine_btn = ttk.Button(lock, text="Engine ▸", command=self._toggle_engine, width=12)
        self.engine_btn.grid(row=6, column=0, sticky="w", pady=2)
        self.engine_box = scrolledtext.ScrolledText(lock, wrap="word", height=8,
                                                     font=("Consolas", 10))
        self._darken_text(self.engine_box)
        self.engine_box.configure(state="disabled")
        brow = ttk.Frame(lock)
        brow.grid(row=7, column=1, sticky="w", padx=8, pady=10)
        self.fire_btn = ttk.Button(brow, text="FIRE", command=self.on_fire, state="disabled")
        self.fire_btn.pack(side="left")
        self.try_btn = ttk.Button(brow, text="Try again", command=self.on_try_again, state="disabled")
        self.try_btn.pack(side="left", padx=6)
        self.anyway_btn = ttk.Button(brow, text="Post anyway", command=self.on_post_anyway, state="disabled")
        self.anyway_btn.pack(side="left", padx=6)
        self.another_btn = ttk.Button(brow, text="Find another", command=self.on_find_another, state="disabled")
        self.another_btn.pack(side="left", padx=6)
        self.nologin_btn = ttk.Button(brow, text="FIRE no-login", command=self.on_fire_nologin,
                                      state="disabled")
        self.nologin_btn.pack(side="left", padx=6)

        self.log_box = scrolledtext.ScrolledText(
            self.log_tab, wrap="word", height=24, font=("Consolas", 10)
        )
        self.log_box.pack(fill="both", expand=True, padx=8, pady=8)
        self._darken_text(self.log_box)
        self.log_box.configure(state="disabled")

        srow = ttk.Frame(self.stats_tab)
        srow.pack(fill="x", padx=8, pady=4)
        ttk.Button(srow, text="Refresh stats", command=self.on_stats_refresh).pack(side="left")
        ttk.Label(srow, text="NYC days (Today resets NYC midnight). Marks = sends; runs = attempts.").pack(side="left", padx=8)
        self.today_var = tk.StringVar(value="Today: —")
        ttk.Label(srow, textvariable=self.today_var).pack(side="left", padx=8)
        scols = ("slot", "total", "today", "7d", "failed", "last")
        self.stats_tree = ttk.Treeview(
            self.stats_tab, columns=scols, show="headings", selectmode="browse", height=10
        )
        self.stats_tree.heading("slot", text="Slot")
        self.stats_tree.heading("total", text="Total")
        self.stats_tree.heading("today", text="Today")
        self.stats_tree.heading("7d", text="7d")
        self.stats_tree.heading("failed", text="Failed")
        self.stats_tree.heading("last", text="Last send")
        self.stats_tree.column("slot", width=80, anchor="w")
        self.stats_tree.column("total", width=60, anchor="e")
        self.stats_tree.column("today", width=60, anchor="e")
        self.stats_tree.column("7d", width=60, anchor="e")
        self.stats_tree.column("failed", width=60, anchor="e")
        self.stats_tree.column("last", width=500, anchor="w")
        self.stats_tree.pack(fill="both", expand=True, padx=8, pady=4)
        self.stats_tree.bind("<<TreeviewSelect>>", self._on_stats_chosen)
        sprof = ttk.Frame(self.stats_tab)
        sprof.pack(fill="x", padx=8, pady=4)
        self.profile_btn = ttk.Button(sprof, text="Open profile",
                                      command=self.on_open_profile, state="disabled")
        self.profile_btn.pack(side="left")
        self.addbatch_btn = ttk.Button(sprof, text="Add slot",
                                       command=self.on_stats_add_to_batch, state="disabled")
        self.addbatch_btn.pack(side="left", padx=6)
        self.delete_btn = ttk.Button(sprof, text="Delete slot",
                                     command=self.on_stats_delete, state="disabled")
        self.delete_btn.pack(side="left", padx=6)
        self._stats_sel = ""
        self.stats_box = scrolledtext.ScrolledText(
            self.stats_tab, wrap="word", height=10, font=("Consolas", 10)
        )
        self.stats_box.pack(fill="both", expand=True, padx=8, pady=4)
        self._darken_text(self.stats_box)
        self.stats_box.configure(state="disabled")
        self._stats_cache: dict = {}

        self.test_tab = ttk.Frame(nb)
        nb.add(self.test_tab, text="Test")
        trow = ttk.Frame(self.test_tab)
        trow.pack(fill="x", padx=8, pady=8)
        ttk.Label(trow, text="Sub:").pack(side="left")
        self.test_sub = ttk.Combobox(trow, state="readonly", width=24,
                                     values=tuple(TEST_SUBS))
        try:
            self.test_sub.set(TEST_SUBS[0] if TEST_SUBS else "")
        except (tk.TclError, AttributeError, IndexError):
            pass
        self.test_sub.pack(side="left", padx=6)
        ttk.Label(trow, text="20up / 120m / pic").pack(side="left", padx=6)
        self.test_btn = ttk.Button(trow, text="PREP test", command=self.on_prep_test)
        self.test_btn.pack(side="left", padx=6)
        self.test_msg = tk.StringVar(value="Single top-level trial. Uses roster slot.")
        ttk.Label(self.test_tab, textvariable=self.test_msg,
                  wraplength=900).pack(fill="x", padx=8, pady=4)

        self.batch_tab = ttk.Frame(nb)
        nb.add(self.batch_tab, text="Batch automation")
        srow = ttk.Frame(self.batch_tab)
        srow.pack(fill="x", padx=8, pady=8)
        ttk.Label(srow, text="Start slot:").pack(side="left")
        self._batch_slot_ids: dict[str, str] = {}
        self.batch_slot = ttk.Combobox(srow, width=40, state="readonly")
        self.batch_slot.pack(side="left", padx=6)
        self.batch_slot.bind("<<ComboboxSelected>>", lambda _e: self._on_batch_slot())
        brow1 = ttk.Frame(self.batch_tab)
        brow1.pack(fill="x", padx=8, pady=4)
        ttk.Label(brow1, text="Phase:").pack(side="left")
        self.phase2 = ttk.Combobox(brow1, width=8, state="readonly",
                                   values=("Phase1", "Phase2"))
        try:
            self.phase2.set(self.phase.get())
        except (tk.TclError, AttributeError):
            self.phase2.set(PHASE_DEFAULT)
        self.phase2.pack(side="left", padx=6)
        ttk.Label(brow1, text="Min").pack(side="left", padx=(6, 0))
        self.batch_floor2 = ttk.Combobox(brow1, width=3, state="readonly",
                                         values=("2", "3", "4", "5"))
        try:
            self.batch_floor2.set(self.batch_floor.get())
        except (tk.TclError, AttributeError):
            self.batch_floor2.set("5")
        self.batch_floor2.pack(side="left", padx=6)
        ttk.Label(brow1, text="Age").pack(side="left", padx=(6, 0))
        self.batch_age2 = ttk.Combobox(brow1, width=4, state="readonly",
                                       values=("120", "180", "360"))
        try:
            self.batch_age2.set(self.batch_age.get())
        except (tk.TclError, AttributeError):
            self.batch_age2.set("360")
        self.batch_age2.pack(side="left", padx=6)
        self.fresh_var = tk.BooleanVar(value=False)
        self.fresh_chk = ttk.Checkbutton(brow1, text="Only no-comment-today",
                                         variable=self.fresh_var)
        self.fresh_chk.pack(side="left", padx=6)
        self.spread_var = tk.BooleanVar(value=True)
        self.spread_chk = ttk.Checkbutton(brow1, text="Spread 20-60min",
                                          variable=self.spread_var)
        self.spread_chk.pack(side="left", padx=6)
        self.wf1_btn = ttk.Button(brow1, text="Start batch", command=self.on_workflow1)
        self.wf1_btn.pack(side="left", padx=6)
        self.resume_btn = ttk.Button(brow1, text="Resume spread",
                                     command=self.on_resume_spread)
        self.resume_btn.pack(side="left", padx=6)
        self.next_var = tk.StringVar(value="Next: —")
        ttk.Label(brow1, textvariable=self.next_var).pack(side="left", padx=12)
        ttk.Label(self.batch_tab,
                  text="Workflow 1: 1 comment per slot down the fleet from the start slot. "
                  "Per slot: PREP login -> Home -> Chrome PREP -> FIRE no-login -> Home. "
                  "Spread gaps 20-60min between slots (9am-11pm fit); logout/flag skips the slot. "
                  "Supervised, top-level only.",
                  wraplength=900).pack(fill="x", padx=8, pady=4)
        self.batch_auto_var = tk.StringVar(value="Session log: (none yet)")
        ttk.Label(self.batch_tab, textvariable=self.batch_auto_var,
                  wraplength=900).pack(fill="x", padx=8, pady=4)

        self.dry_tab = ttk.Frame(nb)
        nb.add(self.dry_tab, text="Dry run")
        drow = ttk.Frame(self.dry_tab)
        drow.pack(fill="x", padx=8, pady=8)
        ttk.Label(drow, text="Post URL:").pack(side="left")
        self.dry_url = ttk.Entry(drow, width=80)
        self.dry_url.pack(side="left", padx=6)
        self.dry_btn = ttk.Button(drow, text="Dry run", command=self.on_dry_run)
        self.dry_btn.pack(side="left", padx=6)
        ttk.Label(self.dry_tab,
                  text="Read-only: scout + vision + text for any post. "
                  "No phone, no FIRE, no marks, nothing sent.",
                  wraplength=900).pack(fill="x", padx=8, pady=4)
        self.dry_box = scrolledtext.ScrolledText(
            self.dry_tab, wrap="word", height=24, font=("Consolas", 10)
        )
        self.dry_box.pack(fill="both", expand=True, padx=8, pady=8)
        self._darken_text(self.dry_box)
        self.dry_box.configure(state="disabled")

        self.notes_tab = ttk.Frame(nb)
        nb.add(self.notes_tab, text="Notes")
        self.notes_box = scrolledtext.ScrolledText(
            self.notes_tab, wrap="word", height=24, font=("Consolas", 10)
        )
        self.notes_box.pack(fill="both", expand=True, padx=8, pady=8)
        self._darken_text(self.notes_box)
        ttk.Label(self.notes_tab,
                  text="Scratchpad. Saves on every exit + every 60 seconds.",
                  wraplength=900).pack(fill="x", padx=8, pady=4)
        self._notes_load()
        try:
            self.phase.bind("<<ComboboxSelected>>", lambda _e: self._mirror_phase(True))
            self.phase2.bind("<<ComboboxSelected>>", lambda _e: self._mirror_phase(False))
            self.batch_floor.bind("<<ComboboxSelected>>", lambda _e: self._mirror_batch(True))
            self.batch_floor2.bind("<<ComboboxSelected>>", lambda _e: self._mirror_batch(False))
            self.batch_age.bind("<<ComboboxSelected>>", lambda _e: self._mirror_batch(True))
            self.batch_age2.bind("<<ComboboxSelected>>", lambda _e: self._mirror_batch(False))
        except (tk.TclError, AttributeError):
            pass

        self._refresh_roster()
        self.on_stats_refresh()
        self._next_due = 0.0
        self._next_sid = ""
        self.root.after(200, self._probe_usb_idle)
        self.root.after(120, self._drain_log)
        self.root.after(1000, self._tick_next)
        self.root.after(60000, self._notes_autosave)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        debug_open()
        if stay_awake():
            self.log("Laptop sleep blocked while open.")
        if getattr(self, "_second_instance", False):
            self.log("WARNING: another commenter is already running. "
                     "Close the other window or bot-Chrome fights will abort slots.")

    def _apply_dark_theme(self) -> None:
        """Dark UI: clam base + dark palette, text widgets hand-tuned.
        Best-effort, never raises (falls back to system theme)."""
        bg, bg2, fg, acc = "#1e1e1e", "#2d2d2d", "#e0e0e0", "#0e639c"
        try:
            self.root.configure(background=bg)
            style = ttk.Style(self.root)
            try:
                style.theme_use("clam")
            except (tk.TclError, AttributeError):
                pass
            style.configure(".", background=bg, foreground=fg,
                            fieldbackground=bg2, bordercolor="#3e3e3e")
            style.configure("TFrame", background=bg)
            style.configure("TLabel", background=bg, foreground=fg)
            style.configure("TButton", background=bg2, foreground=fg,
                            bordercolor="#3e3e3e")
            style.map("TButton",
                      background=[("active", "#3e3e3e"), ("disabled", bg)],
                      foreground=[("disabled", "#6e6e6e")])
            style.configure("TNotebook", background=bg, bordercolor=bg)
            style.configure("TNotebook.Tab", background=bg2, foreground=fg,
                            padding=(10, 4))
            style.map("TNotebook.Tab",
                      background=[("selected", "#3e3e3e")],
                      foreground=[("selected", "#ffffff")])
            style.configure("Treeview", background=bg2, foreground=fg,
                            fieldbackground=bg2, bordercolor="#3e3e3e")
            style.configure("Treeview.Heading", background="#3e3e3e",
                            foreground="#ffffff")
            style.map("Treeview",
                      background=[("selected", acc)],
                      foreground=[("selected", "#ffffff")])
            style.configure("TEntry", fieldbackground=bg2, foreground=fg,
                            insertcolor=fg)
            style.configure("TCombobox", fieldbackground=bg2, foreground=fg,
                            arrowcolor=fg)
            try:
                self.root.option_add("*TCombobox*Listbox.background", bg2)
                self.root.option_add("*TCombobox*Listbox.foreground", fg)
                self.root.option_add("*TCombobox*Listbox.selectBackground", acc)
            except (tk.TclError, AttributeError):
                pass
            style.configure("TLabelframe", background=bg, foreground=fg)
            style.configure("TLabelframe.Label", background=bg, foreground=fg)
            style.configure("TScrollbar", background=bg2, troughcolor=bg,
                            bordercolor=bg, arrowcolor=fg)
            self._dark_text = {"bg": "#252526", "fg": fg, "insert": "#ffffff",
                               "selbg": acc}
        except Exception:
            pass

    def _darken_text(self, widget) -> None:
        """Apply dark colors to a ScrolledText. Best-effort."""
        try:
            pal = getattr(self, "_dark_text",
                          {"bg": "#252526", "fg": "#e0e0e0",
                           "insert": "#ffffff", "selbg": "#0e639c"})
            widget.configure(background=pal["bg"], foreground=pal["fg"],
                             insertbackground=pal["insert"],
                             selectbackground=pal["selbg"],
                             selectforeground="#ffffff")
        except (tk.TclError, AttributeError):
            pass

    # ----- plumbing (creator conventions) -----
    def log(self, msg: str) -> None:
        m = str(msg)
        try:
            with open(DEBUG_PATH, "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S"), m))
        except OSError:
            pass
        self._batch_auto_write(m)
        if ui_important(m):
            self.log_q.put(m)

    def log_debug(self, msg: str) -> None:
        """Debug file only: full tracebacks and dumps that must never
        flood the chilled tab. Batch session file untouched."""
        try:
            with open(DEBUG_PATH, "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S"),
                                       str(msg)))
        except OSError:
            pass

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self.log_q.get_nowait()
                stamp = datetime.now().strftime("%H:%M:%S")
                self.log_box.configure(state="normal")
                self.log_box.insert("end", f"{stamp}  {msg}\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    def _probe_usb_idle(self) -> None:
        def work() -> None:
            try:
                report = diagnose_usb()
                self.status.set(usb_status_line(report))
                self.log(usb_status_line(report))
            except Exception as e:
                self.log(f"USB idle probe: {type(e).__name__}: {e}")
        threading.Thread(target=work, daemon=True).start()

    def _busy(self, on: bool) -> None:
        try:
            self.stop_btn.configure(state="normal" if on else "disabled")
        except (tk.TclError, AttributeError):
            pass
        self._sync_buttons()

    def _start_worker(self, target, *args) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.stop.clear()
        self._busy(True)
        self.worker = threading.Thread(target=target, args=args, daemon=True)
        self.worker.start()

    def _after_job(self) -> None:
        self.worker = None
        self._busy(False)

    def _refresh_slot_choices(self) -> None:
        """Lock-in dropdown, roster-sourced. Preserves + persists selection."""
        try:
            rows = load_slots()
        except Exception:
            rows = []
        cur = ""
        try:
            cur = self._lock_slot_ids.get(self.lock_slot.get().strip(), "")
        except (tk.TclError, AttributeError):
            cur = ""
        if not cur:
            cur = str(load_commenter_settings().get("selected_slot") or "")
        labels: list[str] = []
        ids: dict[str, str] = {}
        for r in rows:
            if not slot_usable(r):
                continue
            sid = str(r.get("id") or "")
            if not sid:
                continue
            user = str(r.get("username") or "").strip() or sid
            disp = "%s — %s" % (sid, user)
            labels.append(disp)
            ids[disp] = sid
        if cur and cur not in ids.values():
            cur = ""
            try:
                save_commenter_settings("")
            except Exception:
                pass
            self.log("Retired slot purged from UI selection.")
        try:
            self._lock_slot_ids = ids
            self.lock_slot.configure(values=labels)
            want = ""
            for disp, sid in ids.items():
                if sid == cur:
                    want = disp
                    break
            self.lock_slot.set(want)
        except (tk.TclError, AttributeError):
            pass
        try:
            self._batch_slot_ids = dict(ids)
            self.batch_slot.configure(values=labels)
            bcur = ""
            try:
                bcur = self._batch_slot_ids.get(self.batch_slot.get().strip(), "")
            except (tk.TclError, AttributeError):
                bcur = ""
            if not bcur:
                for ordered in BATCH_ORDER:
                    if ordered in self._batch_slot_ids.values():
                        bcur = ordered
                        break
                if not bcur:
                    bcur = cur
            bwant = ""
            for disp, sid in self._batch_slot_ids.items():
                if sid == bcur:
                    bwant = disp
                    break
            if bwant:
                self.batch_slot.set(bwant)
        except (tk.TclError, AttributeError):
            pass

    def _on_lock_slot(self) -> None:
        try:
            sid = self._lock_slot_ids.get(self.lock_slot.get().strip(), "")
        except (tk.TclError, AttributeError):
            sid = ""
        save_commenter_settings(sid)
        self.log(f"Lock-in slot: {sid or '(none)'}")
        self._sync_buttons()

    def _on_roster_chosen(self, _event=None) -> None:
        try:
            sel = self.tree.selection()
            sid = sel[0] if sel else ""
        except (tk.TclError, AttributeError):
            sid = ""
        if sid:
            try:
                for disp, iid in self._lock_slot_ids.items():
                    if iid == sid:
                        self.lock_slot.set(disp)
                        break
                self._on_lock_slot()
            except (tk.TclError, AttributeError):
                pass
            self._set_batch_tab_slot(sid)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        busy = bool(self.worker and self.worker.is_alive())
        has_sel = bool(self._selected_slot())
        try:
            self.prep_btn.configure(state="disabled" if busy or not has_sel else "normal")
        except (tk.TclError, AttributeError):
            pass
        try:
            self.prep_nologin_btn.configure(state="disabled" if busy or not has_sel else "normal")
        except (tk.TclError, AttributeError):
            pass
        try:
            self.batch_btn.configure(state="disabled" if busy or not has_sel else "normal")
        except (tk.TclError, AttributeError):
            pass
        try:
            self.profile_btn.configure(
                state="disabled" if not self._stats_sel else "normal")
        except (tk.TclError, AttributeError):
            pass
        try:
            self.addbatch_btn.configure(
                state="disabled" if busy or not self._stats_sel else "normal")
        except (tk.TclError, AttributeError):
            pass
        try:
            ok = (bool(self.locked) and bool(self._selected_slot()) and not busy
                  and lockin_fireable(self.locked, self._locked_text()))
            self.fire_btn.configure(state="normal" if ok else "disabled")
        except (tk.TclError, AttributeError):
            pass
        try:
            self.nologin_btn.configure(state="normal" if ok else "disabled")
        except (tk.TclError, AttributeError, NameError):
            pass
        try:
            again = (self.last_fire is not None and self.last_verdict != "posted"
                     and not busy)
            self.try_btn.configure(state="normal" if again else "disabled")
        except (tk.TclError, AttributeError):
            pass
        try:
            any_ok = (not busy and bool(self._selected_slot())
                      and self._parse_post_url(self._post_link())[0] is not None
                      and self._locked_text() != "")
            self.anyway_btn.configure(state="normal" if any_ok else "disabled")
        except (tk.TclError, AttributeError):
            pass
        try:
            self.another_btn.configure(
                state="normal" if (not busy and has_sel) else "disabled")
        except (tk.TclError, AttributeError):
            pass
        try:
            self.test_btn.configure(
                state="disabled" if busy or not has_sel else "normal")
        except (tk.TclError, AttributeError, AttributeError):
            pass
        try:
            has_btab = bool(self._batch_tab_slot())
            self.wf1_btn.configure(state="disabled" if busy or not has_btab else "normal")
        except (tk.TclError, AttributeError):
            pass
        try:
            self.dry_btn.configure(state="disabled" if busy else "normal")
        except (tk.TclError, AttributeError):
            pass
        try:
            self.delete_btn.configure(
                state="disabled" if busy or not self._stats_sel else "normal")
        except (tk.TclError, AttributeError):
            pass

    def on_stats_add_to_batch(self) -> None:
        """Append the Stats-selected slot to BATCH_ORDER (persisted)."""
        global BATCH_ORDER
        sid = (self._stats_sel or "").strip().lower()
        if not sid:
            return
        if self.worker and self.worker.is_alive():
            self.log("Add refused while worker busy.")
            return
        if sid in [o.strip().lower() for o in BATCH_ORDER]:
            self.log(f"Add {sid}: already in batch.")
            return
        if slot_refused(sid):
            self.log(f"Add {sid}: refused, slot retired.")
            return
        try:
            rows = load_slots()
        except Exception:
            rows = []
        row = next((r for r in rows
                    if str(r.get("id") or "").strip().lower() == sid), None)
        if row is None or not slot_usable(row):
            self.log(f"Add {sid}: no usable slot row.")
            return
        try:
            import shutil
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            src = os.path.join(MISC_DIR, "commenter.py")
            shutil.copyfile(src, src + ".bak-" + stamp)
        except OSError as e:
            self.log(f"Add failed: backup error {e}.")
            return
        try:
            new_order = batch_order_add(
                os.path.join(MISC_DIR, "commenter.py"), sid)
        except Exception as e:
            self.log(f"Add failed: {type(e).__name__}: {str(e)[:100]}")
            return
        if not new_order:
            self.log(f"Add {sid}: already listed or order unparseable.")
            return
        BATCH_ORDER = new_order
        self.log(f"Added {sid} to batch ({len(new_order)} slots).")
        self._refresh_roster()

    def on_stats_delete(self) -> None:
        """Retire the Stats-selected slot from all UI. Reversible via retired.json."""
        sid = (self._stats_sel or "").strip().lower()
        if not sid:
            return
        if self.worker and self.worker.is_alive():
            self.log("Delete refused while worker busy.")
            return
        try:
            import shutil
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            src = os.path.join(LOG_DIR, "retired.json")
            try:
                shutil.copyfile(src, src + ".bak-" + stamp)
            except OSError:
                pass
            cur: list = []
            try:
                with open(src, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    cur = data
            except (OSError, ValueError):
                cur = []
            if sid not in [str(x).strip().lower() for x in cur]:
                cur.append(sid)
            with open(src, "w", encoding="utf-8") as f:
                json.dump(cur, f, indent=2)
                f.write("\n")
            self.log(f"Deleted {sid} from UI (retired, reversible).")
        except Exception as e:
            self.log(f"Delete failed: {type(e).__name__}: {str(e)[:100]}")
            return
        self._stats_sel = ""
        self._refresh_roster()
        self.on_stats_refresh()

    def _notes_path(self) -> str:
        return os.path.join(LOG_DIR, "commenter_notes.txt")

    def _notes_load(self) -> None:
        try:
            if os.path.isfile(self._notes_path()):
                with open(self._notes_path(), encoding="utf-8") as f:
                    self.notes_box.delete("1.0", "end")
                    self.notes_box.insert("1.0", f.read())
        except (OSError, tk.TclError, AttributeError):
            pass

    def _notes_save(self) -> None:
        try:
            text = self.notes_box.get("1.0", "end-1c")
        except (tk.TclError, AttributeError):
            return
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(self._notes_path(), "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    def _notes_autosave(self) -> None:
        try:
            self._notes_save()
        except Exception:
            pass
        try:
            self.root.after(60000, self._notes_autosave)
        except Exception:
            pass

    def on_close(self) -> None:
        try:
            self._notes_save()
        except Exception:
            pass
        try:
            self.stop.set()
        except Exception:
            pass
        try:
            if getattr(self, "_instance_lock", None) is not None:
                import msvcrt as _msvcrt
                try:
                    _msvcrt.locking(self._instance_lock.fileno(), _msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
                try:
                    self._instance_lock.close()
                except Exception:
                    pass
                self._instance_lock = None
        except Exception:
            pass
        if self.phone:
            try:
                self.phone.close()
            except Exception:
                pass
        allow_sleep()
        self.root.destroy()

    def on_stop(self) -> None:
        self.stop.set()
        self.log("Stop requested.")

    # ----- roster -----
    def on_refresh(self) -> None:
        self._refresh_roster()

    def _refresh_roster(self) -> None:
        try:
            rows = load_slots()
        except Exception:
            rows = []
        try:
            for iid in self.tree.get_children():
                self.tree.delete(iid)
            for r in rows:
                if not slot_usable(r):
                    continue
                sid = str(r.get("id") or "")
                user = str(r.get("username") or "").strip() or sid
                mark = last_mark_for(sid)
                if mark:
                    when = str(mark.get("when") or "?")
                    last = "%s r/%s :: %s" % (when, mark.get("sub"), str(mark.get("title") or "")[:60])
                else:
                    last = "—"
                if sid:
                    self.tree.insert("", "end", iid=sid, values=(sid, user, last))
        except (tk.TclError, AttributeError):
            pass
        self._refresh_slot_choices()
        self._sync_buttons()

    def _load_marks(self) -> dict:
        try:
            with open(os.path.join(LOG_DIR, "commented_posts.json"), encoding="utf-8") as f:
                return (json.load(f) or {}).get("posts") or {}
        except (OSError, ValueError):
            return {}

    def _load_runs(self) -> list:
        rows = []
        try:
            with open(RUNLOG_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return rows

    def on_stats_refresh(self) -> None:
        marks = stats_from_marks(self._load_marks())
        runs = stats_from_runs(self._load_runs())
        self._stats_cache = {"marks": marks, "runs": runs}
        try:
            from farm_stats import failed_today as _failed_today
            fails = _failed_today(self._load_runs())
        except Exception:
            fails = {}
        self._stats_cache["fails"] = fails
        try:
            summ = farm_today_summary()
            self.today_var.set(
                "Today %s: %d comments (%s) | %d posts (%s)" % (
                    summ["day"], summ["comments"],
                    ",".join(summ["comment_slots"]) or "—",
                    summ["posts"],
                    ",".join(summ["post_slots"]) or "—"))
        except Exception:
            pass
        try:
            rows = load_slots()
        except Exception:
            rows = []
        cids = {str(r.get("id") or "") for r in rows if str(r.get("id") or "")}
        cids |= set(marks) | set(fails)
        cids = {c for c in cids if c and not slot_refused(c)}
        today = nyc_today()
        week = set()
        try:
            _now_nyc = datetime.now(_nyc_tz())
        except Exception:
            _now_nyc = datetime.now(timezone.utc)
        for d in range(7):
            week.add((_now_nyc - timedelta(days=d)).strftime("%Y-%m-%d"))
        try:
            for iid in self.stats_tree.get_children():
                self.stats_tree.delete(iid)
            brk = _breaks_load()
            for cid in sorted(cids):
                st = marks.get(cid, {"total": 0, "by_day": {}, "last": None})
                by = st.get("by_day", {})
                last = st.get("last") or {}
                when = str(last.get("when") or "—")
                what = ("r/%s :: %s" % (last.get("sub"), str(last.get("title") or "")[:50])
                        if last else "—")
                bn = brk.get((cid or "").strip().lower()) or []
                if bn:
                    what = "BREAK from %s | %s" % (", ".join("r/%s" % s for s in bn), what)
                att = runs.get(cid, {})
                extra = (" (%d posted/%d abort)" % (att.get("posted", 0), att.get("abort", 0))
                         if att else "")
                self.stats_tree.insert("", "end", iid=cid, values=(
                    cid, st.get("total", 0), by.get(today, 0),
                    sum(v for k, v in by.items() if k in week),
                    len(fails.get(cid, [])),
                    ("%s %s%s" % (when, what, extra))[:90]))
        except (tk.TclError, AttributeError):
            pass

    def _on_stats_chosen(self, _event=None) -> None:
        try:
            sel = self.stats_tree.selection()
            cid = sel[0] if sel else ""
        except (tk.TclError, AttributeError):
            cid = ""
        self._stats_sel = cid
        self._sync_buttons()
        try:
            self.stats_box.configure(state="normal")
            self.stats_box.delete("1.0", "end")
            st = (self._stats_cache.get("marks") or {}).get(cid)
            if not st:
                self.stats_box.insert("end", "No sends recorded for %s." % (cid or "—"))
            else:
                lines = ["%s: %d total" % (cid, st.get("total", 0))]
                try:
                    _bn = _breaks_load().get((cid or "").strip().lower()) or []
                    if _bn:
                        lines.append("BREAK from %s (user order, until reversed)"
                                     % ", ".join("r/%s" % s for s in _bn))
                except Exception:
                    pass
                for day in sorted(st.get("by_day", {}), reverse=True)[:14]:
                    lines.append("  %s  %d" % (day, st["by_day"][day]))
                fl = (self._stats_cache.get("fails") or {}).get(cid) or []
                if fl:
                    lines.append("FAILED today (%d):" % len(fl))
                    for reason in fl[:10]:
                        lines.append("  ! %s" % str(reason)[:100])
                self.stats_box.insert("end", "\n".join(lines))
            self.stats_box.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass

    def _selected_slot(self) -> str:
        """Explicit Lock-in dropdown triple source. Empty until chosen."""
        try:
            return self._lock_slot_ids.get(self.lock_slot.get().strip(), "")
        except (tk.TclError, AttributeError):
            return ""

    def _phase_name(self) -> str:
        """Current UI phase, default Phase1. Pure-ish."""
        try:
            ph = (self.phase.get() or "").strip()
        except (tk.TclError, AttributeError):
            ph = ""
        return ph if ph in PHASES else PHASE_DEFAULT

    def _phase_allow(self) -> frozenset:
        return phase_norm(self._phase_name())

    def _mirror_phase(self, main_to_tab: bool) -> None:
        """Keep top-row phase and Batch-tab phase in sync. Best-effort."""
        try:
            src = self.phase if main_to_tab else self.phase2
            dst = self.phase2 if main_to_tab else self.phase
            dst.set(src.get())
        except (tk.TclError, AttributeError):
            pass

    def _mirror_batch(self, main_to_tab: bool) -> None:
        """Keep top-row floor/age and Batch-tab copies in sync."""
        try:
            if main_to_tab:
                self.batch_floor2.set(self.batch_floor.get())
                self.batch_age2.set(self.batch_age.get())
            else:
                self.batch_floor.set(self.batch_floor2.get())
                self.batch_age.set(self.batch_age2.get())
        except (tk.TclError, AttributeError):
            pass

    def _write_phase_queue(self) -> None:
        """Write phase+approved[] into logs/comment_queue.json. Best-effort."""
        try:
            import comment_precache as _pre
            ph = self._phase_name()
            _pre.save_queue([], phase=ph, approved=phase_approved(ph))
            self.log("QUEUE phase=%s approved=%d" % (ph, len(phase_approved(ph))))
        except Exception as e:
            self.log("QUEUE write failed: %s" % str(e)[:100])

    def _batch_tab_slot(self) -> str:
        """Explicit Batch-tab slot. Empty until chosen."""
        try:
            return self._batch_slot_ids.get(self.batch_slot.get().strip(), "")
        except (tk.TclError, AttributeError):
            return ""

    def _set_batch_tab_slot(self, sid: str) -> None:
        """Select sid in the Batch-tab dropdown. Best-effort."""
        try:
            for disp, iid in self._batch_slot_ids.items():
                if iid == sid:
                    self.batch_slot.set(disp)
                    break
        except (tk.TclError, AttributeError):
            pass

    def _on_batch_slot(self) -> None:
        """Batch-tab slot chosen: drive Lock-in too, single source stays synced."""
        try:
            sid = self._batch_slot_ids.get(self.batch_slot.get().strip(), "")
        except (tk.TclError, AttributeError):
            sid = ""
        if sid:
            try:
                for disp, iid in self._lock_slot_ids.items():
                    if iid == sid:
                        self.lock_slot.set(disp)
                        break
                self._on_lock_slot()
            except (tk.TclError, AttributeError):
                pass
            self.log(f"Batch slot: {sid}")
        self._sync_buttons()

    def _batch_tab_params(self) -> tuple[int, int, float]:
        """Min/Age from the Batch tab (workflow 1 forces 1 per slot)."""
        n = 1
        try:
            floor = int(self.batch_floor2.get().strip())
        except (tk.TclError, AttributeError, ValueError):
            floor = 5
        floor = 5 if floor not in (2, 3, 4, 5) else floor
        try:
            age = float(self.batch_age2.get().strip())
        except (tk.TclError, AttributeError, ValueError):
            age = 360.0
        age = 360.0 if age not in (120.0, 180.0, 360.0) else age
        return n, floor, age

    def _batch_auto_start(self, sid: str, n: int) -> str:
        """Open logs/batch_auto_<ts>_<slot>.log. Returns path. Best-effort."""
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9_-]+", "", sid or "slot") or "slot"
            path = os.path.join(LOG_DIR, "batch_auto_%s_%s.log" % (stamp, safe))
            with open(path, "a", encoding="utf-8") as f:
                f.write("===== BATCH-AUTO %s slot=%s n=%d phase=%s =====\n" % (
                    stamp, sid, n, self._phase_name()))
            self.batch_auto_path = path
            try:
                self.batch_auto_var.set("Session log: %s" % path)
            except (tk.TclError, AttributeError):
                pass
            return path
        except OSError:
            return ""

    def _batch_auto_write(self, msg: str) -> None:
        """Mirror one line into the session log. Best-effort, never raises."""
        try:
            path = getattr(self, "batch_auto_path", "")
            if not path:
                return
            with open(path, "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))
        except OSError:
            pass

    def _batch_auto_finish(self, verdict: str) -> None:
        try:
            self._batch_auto_write("FINISH %s" % verdict)
            path = getattr(self, "batch_auto_path", "")
            if path:
                try:
                    self.batch_auto_var.set("Session log done: %s" % path)
                except (tk.TclError, AttributeError):
                    pass
        except Exception:
            pass
        finally:
            self.batch_auto_path = ""

    # ----- PREP -----
    def on_prep(self) -> None:
        sid = self._selected_slot()
        if not sid:
            return
        if slot_refused(sid):
            self.log(f"PREP {sid}: refused, slot retired.")
            return
        self._start_worker(self._run_prep, self.pw.get(), sid)

    def on_prep_nologin(self) -> None:
        """PREP without login check: PC-only scout + engine fill, no phone."""
        sid = self._selected_slot()
        if not sid:
            return
        if slot_refused(sid):
            self.log(f"PREP NOLOGIN {sid}: refused, slot retired.")
            return
        self.log(f"PREP NOLOGIN {sid} (no phone, user order).")
        self._start_worker(self._run_prep_nologin, sid)

    def _run_prep_nologin(self, sid: str) -> None:
        """PC-only PREP: slot checks + phase-filtered scout + engine fill."""
        import comment_precache as _pre
        _prev_log = _pre.log
        _pre.log = self.log
        try:
            slot = get_slot(sid)
            if slot_refused(sid):
                self.log(f"PREP NOLOGIN {sid}: skip, slot retired.")
                return
            if not slot or not slot.get("ghost"):
                self.log(f"PREP NOLOGIN {sid}: skip, no Ghost row.")
                return
            if not slot.get("email"):
                self.log(f"PREP NOLOGIN {sid}: skip, no Hide My Email.")
                return
            if not slot_reddit_ready(slot):
                self.log(f"PREP NOLOGIN {sid}: skip, Create+Day1 unfinished.")
                return
            self.log(f"=== PREP NOLOGIN {sid} {self._phase_name()} ===")
            self._write_phase_queue()
            if self.stop.is_set():
                return
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                self.log("PREP NOLOGIN: playwright missing. Abort.")
                return
            if not os.path.isdir(BOT_PROFILE_DIR):
                self.log("PREP NOLOGIN: bot Chrome profile missing. Abort.")
                return
            allow = self._phase_allow()
            self.log("PHASE %s approved=%d" % (self._phase_name(), len(phase_approved(self._phase_name()))))
            with sync_playwright() as pw:
                ctx, _owned = open_scout_context(pw, self.log)
                _pg_owned = not ctx.pages
                try:
                    page_w = ctx.pages[0] if ctx.pages else ctx.new_page()
                    auto, ranked, _raws = self._scout_pass(
                        page_w, HOME_URL, 5, 120, sid, "nologin", allow)
                    if auto is None:
                        self.log("PREP NOLOGIN: nothing passing even T3. Abort.")
                        return
                    self.log("SCOUT %d rows, auto-pick [%s] r/%s" % (
                        len(ranked), auto["tier"], auto["sub"]))
                    filled = self._engine_fill(page_w, auto)
                    if filled is None:
                        self.log("PREP NOLOGIN: engine fill failed. Abort.")
                        return
                    self.log_q.put(("candidates", ranked))
                    self.log_q.put(("lockin", filled))
                finally:
                    try:
                        if _owned:
                            ctx.close()
                        elif _pg_owned:
                            page_w.close()
                    except Exception:
                        pass
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
        finally:
            _pre.log = _prev_log
            try:
                quarantine_tick()
            except Exception:
                pass
            self.root.after(0, self._after_job)

    def on_batch(self) -> None:
        sid = self._selected_slot()
        if not sid:
            return
        if slot_refused(sid):
            self.log(f"BATCH {sid}: refused, slot retired.")
            return
        try:
            n = int(self.batch_n.get().strip())
        except (tk.TclError, AttributeError, ValueError):
            n = 3
        n = min(max(n, 1), 8)
        try:
            floor = int(self.batch_floor.get().strip())
        except (tk.TclError, AttributeError, ValueError):
            floor = 5
        floor = 5 if floor not in (2, 3, 4, 5) else floor
        try:
            age = float(self.batch_age.get().strip())
        except (tk.TclError, AttributeError, ValueError):
            age = 360.0
        age = 360.0 if age not in (120.0, 180.0, 360.0) else age
        self._start_worker(self._run_batch, self.pw.get(), sid, n, floor, age)

    def on_workflow1(self) -> None:
        sid = self._batch_tab_slot()
        if not sid:
            self.log("WORKFLOW 1: pick a start slot first.")
            return
        if slot_refused(sid):
            self.log(f"WORKFLOW 1 {sid}: refused, slot retired.")
            return
        self._mirror_phase(False)
        self._mirror_batch(False)
        _, floor, age = self._batch_tab_params()
        try:
            fresh_only = bool(self.fresh_var.get())
        except (tk.TclError, AttributeError):
            fresh_only = False
        try:
            spread = bool(self.spread_var.get())
        except (tk.TclError, AttributeError):
            spread = True
        self.log(f"WORKFLOW 1 from {sid} (1 each, selected down)"
                 + (" [only no-comment-today]" if fresh_only else "")
                 + ("" if spread else " [no spread]"))
        self._start_worker(self._run_workflow1, self.pw.get(), sid, floor,
                           age, fresh_only, spread)

    def on_resume_spread(self) -> None:
        """One-click resume: only no-comment-today + spread, then start."""
        try:
            self.fresh_var.set(True)
            self.spread_var.set(True)
        except (tk.TclError, AttributeError):
            pass
        self.on_workflow1()

    def _tick_next(self) -> None:
        """Countdown label: next scheduled slot. Main thread only."""
        try:
            due = float(getattr(self, "_next_due", 0) or 0)
            sid = str(getattr(self, "_next_sid", "") or "")
            if due and sid:
                rem = int(due - time.time())
                if rem > 0:
                    self.next_var.set("Next: %s in %d:%02d"
                                      % (sid, rem // 60, rem % 60))
                else:
                    self.next_var.set("Posting %s…" % sid)
            else:
                self.next_var.set("Next: —")
        except (tk.TclError, AttributeError):
            pass
        try:
            self.root.after(1000, self._tick_next)
        except (tk.TclError, RuntimeError):
            pass

    def _spread_wait(self, minutes: float, sid: str) -> bool:
        """Stop-aware gap sleep. Publishes the countdown. True = stopped."""
        import time as _t
        due = _t.time() + max(0.0, minutes) * 60.0
        self._next_due = due
        self._next_sid = sid
        self.log("Next: %s in %d min (at %s)." % (
            sid, int(round(minutes)),
            _t.strftime("%H:%M", _t.localtime(due))))
        try:
            while _t.time() < due:
                if self.stop.is_set():
                    return True
                _t.sleep(5.0)
        finally:
            self._next_due = 0.0
            self._next_sid = ""
        return bool(self.stop.is_set())

    def on_prep_test(self) -> None:
        sid = self._selected_slot()
        if not sid:
            return
        if slot_refused(sid):
            self.log(f"TEST {sid}: refused, slot retired.")
            return
        try:
            sub = (self.test_sub.get() or "").strip().lower()
        except (tk.TclError, AttributeError):
            sub = ""
        if not sub:
            self.log("TEST: pick a sub first.")
            return
        self._start_worker(self._run_prep_test, self.pw.get(), sid, sub)

    def on_dry_run(self) -> None:
        try:
            url = self.dry_url.get().strip()
        except (tk.TclError, AttributeError):
            url = ""
        perma = parse_dry_perma(url)
        if not perma:
            self.log("DRY: paste a reddit post URL first.")
            return
        self._start_worker(self._run_dryrun, perma)

    def _run_dryrun(self, perma: str) -> None:
        """Dry-run tab: scout + vision + text for one permalink.
        Read-only: no phone, no FIRE, no marks, nothing sent."""
        import comment_precache as _pre
        _prev_log = _pre.log
        _pre.log = self.log
        out: list[str] = []
        try:
            m = re.search(r"/r/([A-Za-z0-9_]+)/comments/([A-Za-z0-9]+)",
                          perma or "")
            if not m:
                self.log("DRY: unparseable permalink.")
                return
            sub = m.group(1).lower()
            snorm = re.sub(r"\s+", "", sub)
            if snorm not in APPROVED_NORM and snorm not in TEST_SUBS_NORM:
                self.log(f"DRY: r/{sub} outside approved + test subs. Abort.")
                return
            self.log(f"=== DRY {perma} ===")
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                self.log("DRY: playwright missing. Abort.")
                return
            if not os.path.isdir(BOT_PROFILE_DIR):
                self.log("DRY: bot Chrome profile missing. Abort.")
                return
            with sync_playwright() as pw:
                ctx, _owned = open_scout_context(pw, self.log)
                _pg_owned = not ctx.pages
                try:
                    page_w = ctx.pages[0] if ctx.pages else ctx.new_page()
                    top = fetch_top_comment(page_w, perma)
                    pbody = pick_post_body("", (top or {}).get("post_body", ""))
                    title = str((top or {}).get("post_title") or "").strip()
                    if not title:
                        title = perma.strip("/").split("/")[-1].replace("_", " ")
                    out.append("post title: %s" % title)
                    out.append("author: %s  score: %s" % (
                        (top or {}).get("author"), (top or {}).get("score")))
                    out.append("body: %s" % ((pbody or "(none)")[:300]))
                    img_url = (top or {}).get("img_url", "")
                    out.append("img: %s" % ((img_url or "(none)")[:120]))
                    cand = {"sub": sub, "title": title[:160], "permalink": perma,
                            "full_url": "https://www.reddit.com" + perma,
                            "score": int((top or {}).get("score") or 0),
                            "age_min": 0.0,
                            "num_comments": int((top or {}).get("num_comments") or 0),
                            "author": (top or {}).get("author") or "",
                            "tier": "T3" if img_url else ""}
                    filled = self._engine_fill(page_w, cand)
                    if filled is None:
                        out.append("verdict: %s" % (
                            getattr(self, "_last_engine_note", "") or "SKIP (see Log)"))
                        if getattr(self, "_last_vision_raw", ""):
                            out.append("")
                            out.append("===== VISION RAW =====")
                            out.append(self._last_vision_raw)
                    else:
                        from comment_llm import build_mimo_prompt as _bmp
                        out.append("")
                        out.append("===== ENGINE PROOF =====")
                        out.append(self._engine_proof(filled))
                        out.append("")
                        out.append("===== VISION RAW =====")
                        out.append(str(filled.get("_vision_raw") or "(none)"))
                        out.append("")
                        out.append("===== TEXT PROMPT =====")
                        out.append(_bmp(
                            filled.get("title", ""), filled.get("post_body", ""),
                            str(filled.get("_vision_raw") or ""),
                            filled.get("sub", "")))
                        out.append("")
                        out.append("reply: %s" % filled.get("reply_text", ""))
                        out.append("verdict: WOULD POST (nothing sent)")
                    out.append("DRY RUN ONLY: nothing sent, nothing marked.")
                    self.log_q.put(("dry", "\n".join(out)))
                finally:
                    try:
                        if _owned:
                            ctx.close()
                        elif _pg_owned:
                            page_w.close()
                    except Exception:
                        pass
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
        finally:
            _pre.log = _prev_log
            try:
                quarantine_tick()
            except Exception:
                pass
            self.root.after(0, self._after_job)

    def _run_prep_test(self, password: str, sid: str, sub: str) -> None:
        """Test-tab full run: session open incl crane restores + 3-stage scout."""
        import comment_precache as _pre
        _prev_log = _pre.log
        _pre.log = self.log
        phone = None
        try:
            slot = get_slot(sid)
            if slot_refused(sid):
                self.log(f"TEST {sid}: skip, slot retired.")
                return
            if not slot or not slot.get("ghost"):
                self.log(f"TEST {sid}: skip, no Ghost row.")
                return
            if not slot.get("email"):
                self.log(f"TEST {sid}: skip, no Hide My Email.")
                return
            if not slot_reddit_ready(slot):
                self.log(f"TEST {sid}: skip, Create+Day1 unfinished.")
                return
            norm = re.sub(r"\s+", "", sub.lower())
            if norm not in APPROVED_NORM and norm not in TEST_SUBS_NORM:
                self.log(f"TEST {sid}: skip, r/{sub} not approved.")
                return
            self.log(f"=== TEST {sid} r/{norm} (20up/120m/pic) ===")
            if self.stop.is_set():
                return
            phone, stack, _user, _t0, _reason = self._session_open(password, sid)
            if phone is None:
                self.log(f"TEST {sid}: skip, {_reason}.")
                return
            if _reason:
                try:
                    phone.close()
                except Exception:
                    pass
                self.phone = None
                self.log(f"TEST {sid}: skip, {_reason}.")
                return
            try:
                stack.press_home()
                time.sleep(0.7)
            finally:
                try:
                    phone.close()
                except Exception:
                    pass
                self.phone = None
            if self.stop.is_set():
                return
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                self.log("TEST: playwright missing. Abort.")
                return
            if not os.path.isdir(BOT_PROFILE_DIR):
                self.log("TEST: bot Chrome profile missing. Abort.")
                return
            with sync_playwright() as pw:
                ctx, _owned = open_scout_context(pw, self.log)
                _pg_owned = not ctx.pages
                try:
                    page_w = ctx.pages[0] if ctx.pages else ctx.new_page()
                    auto, ranked, _raws = self._scout_pass(
                        page_w, HOME_URL, 20, 120, sid, "test-hot r/%s" % norm,
                        TEST_SUBS_NORM)
                    only = [c for c in ranked
                            if re.sub(r"\s+", "", str(c.get("sub") or "").lower()) == norm]
                    self.log_q.put(("candidates", only))
                    passing = [c for c in only if c.get("tier")]
                    best = self._autopick(passing) if passing else None
                    home_raws = None
                    if best is None:
                        self.log("TEST sweeping home fallback (5/120) r/%s" % norm)
                        _hauto, _hranked, home_raws = self._scout_pass(
                            page_w, HOME_FALLBACK_URL, 5, 120, sid,
                            "test-home r/%s" % norm, TEST_SUBS_NORM)
                        honly = [c for c in _hranked
                                 if re.sub(r"\s+", "", str(c.get("sub") or "").lower()) == norm]
                        self.log_q.put(("candidates", honly))
                        for _cand in self.top_candidates(
                                [c for c in honly if c.get("tier")], 3):
                            filled = self._engine_fill(page_w, _cand)
                            if filled is not None:
                                self.log_q.put(("lockin", filled))
                                try:
                                    self.test_msg.set("TEST r/%s locked: %s" % (
                                        norm, str(_cand.get("title", ""))[:70]))
                                except (tk.TclError, AttributeError):
                                    pass
                                return
                        if home_raws is not None:
                            loose = self._rank(home_raws, 0, 1e9, sid, TEST_SUBS_NORM)
                            lonly = [c for c in loose
                                     if re.sub(r"\s+", "", str(c.get("sub") or "").lower()) == norm]
                            for _cand in self.top_candidates(lonly, 3):
                                self.log("TEST LAST-DITCH r/%s %sup" % (
                                    _cand.get("sub"), _cand.get("score")))
                                filled = self._engine_fill(page_w, _cand)
                                if filled is not None:
                                    self.log_q.put(("lockin", filled))
                                    try:
                                        self.test_msg.set("TEST r/%s locked: %s" % (
                                            norm, str(_cand.get("title", ""))[:70]))
                                    except (tk.TclError, AttributeError):
                                        pass
                                    return
                        self.log(f"TEST: nothing passing r/{norm} even last-ditch. Abort.")
                        try:
                            self.test_msg.set("TEST r/%s: 0 passing." % norm)
                        except (tk.TclError, AttributeError):
                            pass
                        return
                    self.log("TEST auto-pick [%s] r/%s %sup" % (
                        best.get("tier"), best.get("sub"), best.get("score")))
                    filled = self._engine_fill(page_w, best)
                    if filled is None:
                        self.log("TEST: engine fill failed. Abort.")
                        return
                    self.log_q.put(("lockin", filled))
                    try:
                        self.test_msg.set("TEST r/%s locked: %s" % (norm, best.get("title", "")[:70]))
                    except (tk.TclError, AttributeError):
                        pass
                finally:
                    try:
                        if _owned:
                            ctx.close()
                        elif _pg_owned:
                            page_w.close()
                    except Exception:
                        pass
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
        finally:
            _pre.log = _prev_log
            try:
                quarantine_tick()
            except Exception:
                pass
            self.root.after(0, self._after_job)

    def on_open_profile(self) -> None:
        import webbrowser as _wb
        sid = self._stats_sel
        if not sid:
            return
        slot = get_slot(sid)
        username = str((slot or {}).get("username") or "").strip()
        url = profile_url(username)
        if not url:
            self.log(f"PROFILE {sid}: no username on slot.")
            return
        self.log(f"PROFILE {username} opening…")

        def _open() -> None:
            try:
                _wb.open(url)
            except Exception as e:
                self.log(f"PROFILE open failed: {type(e).__name__}")

        threading.Thread(target=_open, daemon=True).start()

    def _connect(self, password: str):
        phone = Phone(log=self.log)
        self.phone = phone
        self.log("USB SSH…")
        phone.connect(password, also_wda=False)
        stack = TapStack(phone, self.log)
        self.log("BACKEND " + stack.attach())
        return phone, stack

    def _rotate_ip_live(self, phone, stack) -> bool:
        """Airplane IP rotation on the live connection, before every
        container open. Fail-open to the old IP. True = stopped."""
        try:
            ok, detail = cycle_on(phone, stack, self.log, stop=self.stop)
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, str(e)[:100])
        self.log("IP rotation %s (%s)."
                 % ("OK" if ok else "FAILED, staying on old IP", detail))
        return bool(self.stop.is_set())

    def _login_stage(self, password: str, sid: str, tag: str = "800_login") -> str:
        """Shared PREP-login sequence (manual PREP button + workflow 1):
        connect -> Home -> Reddit icon -> picker row -> drawer probe with
        always-restore -> Home. Phone closed on exit. '' when alive."""
        phone = stack = None
        try:
            phone, stack = self._connect(password)
            if self.stop.is_set():
                return "stopped"
            if not stack.can_tap():
                return "no tap backend (open XXTouch Elite)"
            self.log("LOGIN probe: container open (read-only, no typing).")
            if self._rotate_ip_live(phone, stack):
                return "stopped"
            stack.press_home()
            time.sleep(0.7)
            blob, nodes = dump_named(stack, phone, tag + "_home", self.log, shot=False)
            icon = springboard_reddit(nodes)
            if icon is None:
                stack.press_home()
                time.sleep(0.6)
                blob, nodes = dump_named(stack, phone, tag + "_home2", self.log,
                                         shot=False)
                icon = springboard_reddit(nodes)
            page = 0
            while icon is None and page < 4:
                stack.interp_flick(640, 700, 110, 700, steps=14, ms=22)
                time.sleep(0.6)
                blob, nodes = dump_named(stack, phone, "%s_pg%d" % (tag, page),
                                         self.log, shot=False)
                icon = springboard_reddit(nodes)
                page += 1
            if icon is None:
                return "Reddit icon missing"
            tap_home_icon(stack, icon)
            time.sleep(1.4)
            reason = self._open_container(stack, phone, sid, tag + "_pick", tag + "_feed")
            if reason:
                return reason
            self.log(f"LOGIN probe pass ({sid} session alive, drawer-proven).")
            stack.press_home()
            time.sleep(0.7)
            return ""
        except PhoneError as e:
            return str(e)[:150]
        except Exception as e:
            return "%s: %s" % (type(e).__name__, str(e)[:120])
        finally:
            if phone:
                try:
                    phone.close()
                except Exception:
                    pass
            self.phone = None

    def _fire_stage(self, password: str, sid: str, item: dict) -> tuple:
        """Shared FIRE no-login sequence (manual FIRE button + workflow 1):
        fresh connect -> container stamp -> entry/type/VERIFY/mark -> park.
        Phone closed on exit. Returns (verdict, reason)."""
        phone = stack = None
        try:
            text = (item.get("reply_text") or "").strip()
            if not text:
                return "abort", "empty engine text"
            slot = get_slot(sid)
            username = str((slot or {}).get("username") or "").strip()
            if not username:
                return "abort", "no username on slot"
            phone, stack = self._connect(password)
            if self.stop.is_set():
                return "abort", "stopped"
            if not stack.can_tap():
                return "abort", "no tap backend (open XXTouch Elite)"
            t0 = time.monotonic()
            self._verified_container = sid
            verdict, reason = self._fire_live(phone, stack, sid, username, text,
                                              item, t0)
            if verdict == "posted":
                try:
                    close_reddit(stack, phone, self.log)
                except Exception as e:
                    self.log("close skipped: %s" % str(e)[:100])
            else:
                try:
                    if stack is not None:
                        stack.press_home()
                except Exception:
                    pass
            return verdict, reason
        except PhoneError as e:
            return "abort", str(e)[:150]
        except Exception as e:
            return "abort", "%s: %s" % (type(e).__name__, str(e)[:120])
        finally:
            if phone:
                try:
                    phone.close()
                except Exception:
                    pass
            self.phone = None

    def _run_prep(self, password: str, sid: str) -> None:
        import comment_precache as _pre
        _prev_log = _pre.log
        _pre.log = self.log
        try:
            slot = get_slot(sid)
            if slot_refused(sid):
                self.log(f"PREP {sid}: skip, slot retired.")
                return
            if not slot or not slot.get("ghost"):
                self.log(f"PREP {sid}: skip, no Ghost row.")
                return
            if not slot.get("email"):
                self.log(f"PREP {sid}: skip, no Hide My Email.")
                return
            if not slot_reddit_ready(slot):
                self.log(f"PREP {sid}: skip, Create+Day1 unfinished.")
                return
            self.log(f"=== PREP {sid} {self._phase_name()} ===")
            self._write_phase_queue()
            reason = self._login_stage(password, sid)
            if reason:
                if reason != "stopped":
                    self.log(f"PREP {sid}: skip, {reason}.")
                return
            if self.stop.is_set():
                return
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                self.log("PREP: playwright missing. Abort.")
                return
            self.log("SCOUT home feed (tiered T1/T2/T3)…")
            if not os.path.isdir(BOT_PROFILE_DIR):
                self.log("PREP: bot Chrome profile missing. Abort.")
                return
            allow = self._phase_allow()
            self.log("PHASE %s approved=%d" % (self._phase_name(), len(phase_approved(self._phase_name()))))
            with sync_playwright() as pw:
                ctx, _owned = open_scout_context(pw, self.log)
                _pg_owned = not ctx.pages
                try:
                    page_w = ctx.pages[0] if ctx.pages else ctx.new_page()
                    raws = one_pass(page_w, HOME_URL, 5, 120)
                    ranked = self._rank(raws, 5, 120, sid, allow)
                    auto = self._autopick(ranked)
                    if auto is None:
                        self.log("PREP: nothing passing even T3. Abort.")
                        return
                    self.log("SCOUT %d rows, auto-pick [%s] r/%s" % (
                        len(ranked), auto["tier"], auto["sub"]))
                    filled = self._engine_fill(page_w, auto)
                    if filled is None:
                        self.log("PREP: engine fill failed. Abort.")
                        return
                    self.log_q.put(("candidates", ranked))
                    self.log_q.put(("lockin", filled))
                finally:
                    try:
                        if _owned:
                            ctx.close()
                        elif _pg_owned:
                            page_w.close()
                    except Exception:
                        pass
        except PhoneError as e:
            self.log("ERROR  " + str(e))
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
        finally:
            _pre.log = _prev_log
            try:
                quarantine_tick()
            except Exception:
                pass
            self.phone = None
            self.root.after(0, self._after_job)

    def _rank(self, raws: list[dict], min_score: int = 5,
              max_age: float = 360, sid: str = "",
              extra_allow: frozenset = frozenset()) -> list[dict]:
        out: list[dict] = []
        try:
            qmap = _q_load()
        except Exception:
            qmap = {}
        for r in sorted(raws, key=lambda x: x.get("created") or "", reverse=True):
            sub = (r.get("sub") or "").strip()
            snorm = re.sub(r"\s+", "", sub.lower())
            if snorm not in APPROVED_NORM and snorm not in (extra_allow or frozenset()):
                continue
            try:
                age = age_min_of(r.get("created") or "")
            except Exception:
                age = 1e9
            score = int(r.get("score") or 0)
            title = (r.get("title") or "").strip()
            perma = r.get("perma") or ""
            if not title or not perma:
                continue
            reasons: list[str] = []
            if score < min_score:
                reasons.append(f"{score}up<{min_score}")
            if age > max_age:
                reasons.append(f"{age:.0f}m>{max_age:.0f}m")
            if not is_picture(r):
                reasons.append(f"type={r.get('ptype')}")
            try:
                used = already_commented(sub, title, sid or None)
            except Exception:
                used = False
            if used:
                reasons.append("USED")
            elif sid:
                try:
                    cooling = recently_taken(sub, title)
                except Exception:
                    cooling = False
                if cooling:
                    reasons.append("cooling-24h")
            if q_blocked(qmap, perma):
                reasons.append("quarantine")
            try:
                if never_block(sub, title):
                    reasons.append("never-touch")
            except Exception:
                pass
            try:
                if nsfw_block(sub, title):
                    reasons.append("nsfw")
            except Exception:
                pass
            try:
                if sid and sub_break_block(sid, sub):
                    reasons.append("sub-break")
            except Exception:
                pass
            tier = ""
            if not reasons:
                if score >= 12 and age <= 30:
                    tier = "T1"
                elif score >= 8 and age <= 60:
                    tier = "T2"
                else:
                    tier = "T3"
            out.append({
                "sub": sub.lower(), "title": title[:160], "permalink": perma,
                "full_url": "https://www.reddit.com" + perma,
                "score": score, "age_min": round(age, 1),
                "num_comments": int(r.get("comments") or 0),
                "author": r.get("author") or "", "tier": tier,
                "verdict": ("CAND " + tier) if tier else ("SKIP " + "+".join(reasons)),
            })
        return out

    @staticmethod
    def _autopick(ranked: list[dict]) -> dict | None:
        """Even rotation across phase subs (tiers are labels only).
        Falls back to score order for off-phase subs; score ties keep newest."""
        return rotation_pick(ranked)

    @staticmethod
    def top_candidates(ranked: list[dict], k: int) -> list:
        """Top-k passing posts by score desc (newest breaks ties). Pure."""
        passing = [c for c in ranked or [] if c.get("tier")]
        passing.sort(key=lambda c: (-(c.get("score") or 0),))
        return passing[:max(1, k)]

    def _engine_fill(self, page_w, cand: dict) -> dict | None:
        from comment_llm import (COMMENT_TEXT_MODEL, COMMENT_VISION_MODEL,
                                 build_mimo_prompt, describe_image_bytes,
                                 fetch_post_image, generate_comment, load_api_key,
                                 is_image_bytes, mime_of, parse_analysis, vision_bytes_ok,
                                 vision_refused)
        top = fetch_top_comment(page_w, cand["permalink"])
        top_author = (top or {}).get("author", "")
        top_body = (top or {}).get("body", "")
        img_url = (top or {}).get("img_url", "")
        try:
            key = load_api_key()
        except Exception:
            key = ""
        item = dict(cand)
        item["mode"] = "comment"
        item["top_author"] = top_author
        item["top_body"] = top_body
        item["top_score"] = (top or {}).get("score", 0)
        pbody = pick_post_body(cand.get("body", ""), (top or {}).get("post_body", ""))
        item["post_body"] = pbody
        if pbody and not (cand.get("body") or "").strip():
            self.log("BODY post-page capture %d chars" % len(pbody))
        self._last_engine_note = ""
        self._last_vision_raw = ""
        img_desc = {"text_in_image": "", "content": "", "scene": ""}
        img_text = ""
        raw = None
        src = ""
        t0 = time.monotonic()
        if key and img_url:
            raw, vsrc = fetch_post_image(
                img_url, page=page_w, perma=cand.get("permalink") or "",
                log=self.log)
            if raw:
                src = vsrc
            else:
                self.log("VISION ladder %s" % vsrc)
            if not raw:
                try:
                    shot = (page_w.locator("shreddit-post").first
                            .locator("img").first.screenshot(timeout=15000))
                    if shot and is_image_bytes(shot):
                        raw = shot
                        src = "element"
                    elif shot:
                        self.log("VISION element-shot rejected (%dB, bad magic)" % len(shot))
                except Exception as e:
                    self.log(f"VISION element-shot fail: {e}")
            if raw and not vision_bytes_ok(raw):
                self.log("VISION bytes rejected (%dB via %s, tiny/corrupt)" % (len(raw), src))
                raw = None
                src = ""
            if raw:
                self.log("VISION bytes %dB via %s" % (len(raw), src))
                try:
                    with open(os.path.join(LOG_DIR, "vision_last.jpg"), "wb") as f:
                        f.write(raw)
                except OSError:
                    pass
            if need_vision(img_url, cand.get("tier")) and not raw:
                self.log("PREP: no vision bytes for image post. Abort.")
                return None
        if raw:
            desc, derr, seen = None, "", 0
            for attempt in (1, 2):
                try:
                    desc, derr = describe_image_bytes(
                        raw, cand["sub"], cand["title"], pbody, key,
                        mime=mime_of(raw))
                except Exception as e:
                    desc, derr = None, str(e)
                    self.log(f"VISION describe fail: {e}")
                if desc and vision_refused(desc):
                    self.log("VISION refused: %s" % desc.strip()[:160])
                    self.log("NSFW refusal verdict, skipping post forever.")
                    nsfw_add(cand.get("sub"), cand.get("title"))
                    self._last_vision_raw = desc
                    self._last_engine_note = "SKIP vision-refused (NSFW verdict)"
                    return None
                if desc:
                    break
                seen = attempt
                self.log(f"VISION describe empty ({len(raw)}B try{attempt}): {derr}")
            if not desc:
                self.log(f"PREP: no vision for image post after {seen} tries ({derr}). Abort.")
                self._last_engine_note = "SKIP no vision (%s)" % (derr or "empty")[:120]
                return None
            self._vision_model_used = COMMENT_VISION_MODEL
            if desc:
                img_text = desc
                self._last_vision_raw = desc
                img_desc = parse_analysis(desc)
                if "TEXT_IN_IMAGE" not in desc.upper():
                    self.log("VISION unparsed format, blob kept in CONTENT")
            from comment_llm import classify_nsfw as _nsfw
            try:
                _is_nsfw, _nerr = _nsfw(
                    cand.get("title"), pbody,
                    img_desc.get("text_in_image") or "", key) if key else (False, "")
            except Exception as e:
                _is_nsfw, _nerr = False, str(e)
            if _is_nsfw:
                self.log("NSFW classifier verdict, skipping post forever.")
                nsfw_add(cand.get("sub"), cand.get("title"))
                self._last_engine_note = "SKIP nsfw-classifier"
                return None
        t_vision = time.monotonic() - t0
        header = "u/%s - %.0fm - r/%s - %sup" % (
            cand.get("author") or "?", cand.get("age_min") or 0,
            cand.get("sub") or "?", cand.get("score") or 0)
        t1 = time.monotonic()
        reply, _err = None, ""
        top_ctx = ""
        if top_author or top_body:
            top_ctx = "u/%s (%s upvotes): %s" % (
                top_author or "?", (top or {}).get("score", "?"),
                (top_body or "").strip()[:2000])
        try:
            reply, _err = generate_comment(
                cand["title"], pbody, img_text, key,
                sub=cand["sub"], top_comment=top_ctx) if key else (None, "no key")
        except Exception:
            reply = None
        t_text = time.monotonic() - t1
        text = (reply or "").strip()
        if not text:
            if (_err or "") == "pass":
                self.log("ENGINE pass: model had nothing to say, skipping post.")
                self._last_engine_note = "SKIP model PASS"
            else:
                self._last_engine_note = "SKIP empty text (%s)" % (_err or "no text")[:120]
            return None
        item["reply_text"] = text
        item["reply_source"] = "llm"
        item["image_analysis"] = img_desc
        item["_vision_raw"] = img_text
        item["_thumb_png"] = self._thumb_png(raw)
        words = text.split()
        nc = int(cand.get("num_comments") or 0)
        footer = ("%sup · %s comments" % (cand.get("score") or 0, nc)
                  if (cand.get("score") or nc) else "")
        item["_engine"] = {
            "post_title": cand.get("title", ""), "header": header,
            "body": pbody, "footer": footer,
            "sub": cand.get("sub", ""), "score": cand.get("score", 0),
            "age_min": cand.get("age_min", 0.0),
            "top_author": top_author, "top_body": top_body,
            "top_score": (top or {}).get("score", 0),
            "vision_model": getattr(self, "_vision_model_used", COMMENT_VISION_MODEL),
            "vision_s": round(t_vision, 1),
            "image_analysis": img_desc,
            "text_model": COMMENT_TEXT_MODEL, "text_s": round(t_text, 1),
            "reply_text": text, "reply_source": "llm",
            "has_image": need_vision(img_url, cand.get("tier")), "vision_ok": bool(img_text),
            "cleanup": "%d words, lowercased, first capitalized, no punctuation" % len(words),
        }
        def _show(v):
            s = str(v if v is not None else "").strip()
            return s if s else "—"
        self.log("ENGINE post title: %s" % _show(cand.get("title")))
        self.log("ENGINE body: %s" % _show(pbody))
        self.log("ENGINE top comment: %s" % _show(top_ctx))
        self.log("ENGINE footer: %s" % _show(footer))
        self.log("ENGINE TEXT_IN_IMAGE: %s" % _show(img_desc["text_in_image"]))
        self.log("ENGINE CONTENT: %s" % _show(img_desc["content"]))
        self.log("ENGINE SCENE: %s" % _show(img_desc["scene"]))
        self.log("ENGINE text-prompt: %s" % build_mimo_prompt(
            cand.get("title", ""), pbody, img_text,
            cand.get("sub", "")))
        self.log("OPENAI vision: %s (%.1fs)" % (
            getattr(self, "_vision_model_used", COMMENT_VISION_MODEL), t_vision))
        self.log("TEXT model: %s (%.1fs): %s" % (COMMENT_TEXT_MODEL, t_text, text))
        return item

    @staticmethod
    def _thumb_png(raw: bytes | None) -> bytes | None:
        """~300px PNG for the lock-in thumbnail. Worker-safe (no Tk)."""
        if not raw:
            return None
        try:
            from PIL import Image
            import io as _io
            im = Image.open(_io.BytesIO(raw))
            im.thumbnail((300, 300))
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            buf = _io.BytesIO()
            im.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return None

    # ----- candidate select / lock-in -----
    def _on_cand_chosen(self, _event=None) -> None:
        """Display-only: fills run in PREP/batch workers, never from clicks.
        Clicking a filled row previews it; unfilled rows need no action
        (auto-pick already fills to Lock-in). Find another switches posts."""
        if getattr(self, "_prog_select", False):
            return
        try:
            sel = self.ctree.selection()
            if not sel or not self.cands:
                return
            idx = int(sel[0])
        except (tk.TclError, AttributeError, ValueError):
            return
        if not (0 <= idx < len(self.cands)):
            return
        cand = self.cands[idx]
        if cand.get("reply_text"):
            self.log_q.put(("lockin", cand))
        else:
            self.log("Candidate preview only (auto-fill runs in PREP).")

    def _run_fill(self, cand: dict) -> None:
        """Retired: fills run in PREP/batch workers only. Never started."""
        self.log("FILL retired, use PREP or Find another.")

    def _toggle_engine(self) -> None:
        try:
            self.engine_open = not self.engine_open
            self.engine_btn.configure(text="Engine ▾" if self.engine_open else "Engine ▸")
            if self.engine_open:
                self.engine_box.grid(row=6, column=1, sticky="we", padx=8, pady=2)
            else:
                self.engine_box.grid_forget()
        except (tk.TclError, AttributeError):
            pass

    def _engine_proof(self, item: dict) -> str:
        eng = item.get("_engine") or {}
        if not eng:
            return "(engine proof absent — text is manual)"
        ia = eng.get("image_analysis") or {}

        def full(v) -> str:
            s = str(v if v is not None else "").strip()
            return s if s else "—"

        return "\n".join([
            "post title: %s" % full(eng.get("post_title")),
            "header: %s" % full(eng.get("header")),
            "body: %s" % full(eng.get("body")),
            "footer: %s" % full(eng.get("footer")),
            "sub/score/age: %s / %s / %s" % (
                full(eng.get("sub")), full(eng.get("score")), full(eng.get("age_min"))),
            "top comment: %s / %s / %s" % (
                full(eng.get("top_author")), full(eng.get("top_body")),
                full(eng.get("top_score"))),
            "openai vision: %s (%.1fs)" % (
                full(eng.get("vision_model")), float(eng.get("vision_s") or 0)),
            "TEXT_IN_IMAGE: %s" % full(ia.get("text_in_image")),
            "CONTENT: %s" % full(ia.get("content")),
            "SCENE: %s" % full(ia.get("scene")),
            "text model: %s (%.1fs)" % (
                full(eng.get("text_model")), float(eng.get("text_s") or 0)),
            "reply: %s" % full(eng.get("reply_text")),
            "reply_source: %s" % full(eng.get("reply_source")),
            "cleanup: %s" % full(eng.get("cleanup")),
        ])

    def _show_lockin(self, item: dict) -> None:
        self.locked = {k: v for k, v in dict(item).items() if not k.startswith("_")}
        self.locked["_engine"] = item.get("_engine")
        try:
            self.lock_link.delete(0, "end")
            self.lock_link.insert(0, item.get("full_url", ""))
            self.lock_facts.configure(text="r/%s  %sup  %.0fm  u/%s" % (
                item.get("sub"), item.get("score"), item.get("age_min"),
                item.get("author")))
            self.lock_text.delete("1.0", "end")
            self.lock_text.insert("1.0", item.get("reply_text", ""))
            self._update_len()
            png = item.get("_thumb_png")
            if png:
                import base64
                self._thumb_ref = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
                self.thumb.configure(image=self._thumb_ref, text="")
            else:
                self._thumb_ref = None
                self.thumb.configure(image="", text="no image")
            self.engine_box.configure(state="normal")
            self.engine_box.delete("1.0", "end")
            self.engine_box.insert("1.0", self._engine_proof(item))
            self.engine_box.configure(state="disabled")
            self.nb.select(self.lock_tab)
        except (tk.TclError, AttributeError):
            pass
        self._sync_buttons()

    def _update_len(self) -> None:
        try:
            t = self.lock_text.get("1.0", "end-1c")
            self.lock_len.configure(text="UTF-16: %d" % utf16_len(t))
        except (tk.TclError, AttributeError):
            pass

    # ----- FIRE -----
    def _locked_text(self) -> str:
        try:
            return self.lock_text.get("1.0", "end-1c").strip()
        except (tk.TclError, AttributeError):
            return ""

    @staticmethod
    def _parse_post_url(url: str):
        """(sub, post_id, slug_title) or (None, None, None). Pure."""
        m = re.search(r"/r/([A-Za-z0-9_]+)/comments/([A-Za-z0-9]+)/([A-Za-z0-9_]+)?",
                      (url or "").strip())
        if not m:
            return None, None, None
        slug = (m.group(3) or "").replace("_", " ").strip()
        return m.group(1).lower(), m.group(2), slug[:160]

    def _post_link(self) -> str:
        try:
            return self.lock_link.get().strip()
        except (tk.TclError, AttributeError):
            return ""

    def on_fire(self) -> None:
        sid = self._selected_slot()
        if not sid or not self.locked:
            return
        if slot_refused(sid):
            self.log(f"FIRE {sid}: refused, slot retired.")
            return
        text = self._locked_text()
        if not text:
            self.log("FIRE needs locked text.")
            return
        if not lockin_fireable(self.locked, text):
            self.log("FIRE refused: image post without vision. Re-PREP.")
            return
        item = dict(self.locked)
        item["reply_text"] = text
        self._start_worker(self._run_fire, self.pw.get(), sid, item)

    def on_fire_nologin(self) -> None:
        """FIRE without login check: skip session open/probe, send on the
        live app state. User-ordered bypass; strict VERIFY + mark still bind."""
        sid = self._selected_slot()
        if not sid or not self.locked:
            return
        if slot_refused(sid):
            self.log(f"NOLOGIN {sid}: refused, slot retired.")
            return
        text = self._locked_text()
        if not text:
            self.log("NOLOGIN needs locked text.")
            return
        if not lockin_fireable(self.locked, text):
            self.log("NOLOGIN refused: image post without vision. Re-PREP.")
            return
        item = dict(self.locked)
        item["reply_text"] = text
        self.log(f"NOLOGIN bypass {sid}: no session open/probe (user order).")
        self._start_worker(self._run_fire_nologin, self.pw.get(), sid, item)

    def on_post_anyway(self) -> None:
        """FIRE without PREP: explicit triple from Lock-in fields only."""
        sid = self._selected_slot()
        text = self._locked_text()
        sub, _pid, slug = self._parse_post_url(self._post_link())
        if not sid or not text or not sub:
            return
        if slot_refused(sid):
            self.log(f"Post anyway {sid}: refused, slot retired.")
            return
        if re.sub(r"\s+", "", sub.lower()) not in self._phase_allow():
            self.log(f"Post anyway refused: r/{sub} outside {self._phase_name()} subs.")
            return
        url = self._post_link()
        perma = "/" + url.split("reddit.com", 1)[-1] if "reddit.com" in url else url
        item = {"sub": sub, "title": slug or perma[:80], "permalink": perma,
                "full_url": url, "score": 0, "age_min": 0.0, "author": "",
                "mode": "comment", "top_author": "", "top_body": "",
                "reply_text": text, "reply_source": "manual-anyway"}
        self.log(f"POST ANYWAY {sid} r/{sub} (no PREP gates)")
        self._start_worker(self._run_fire, self.pw.get(), sid, item)

    def on_try_again(self) -> None:
        """Refire the exact last triple. Never after a posted run."""
        if self.last_fire is None or self.last_verdict == "posted":
            return
        if self.worker and self.worker.is_alive():
            return
        sid, item = self.last_fire
        if slot_refused(sid):
            self.log(f"TRY AGAIN {sid}: refused, slot retired.")
            return
        self.log(f"TRY AGAIN {sid} r/{item.get('sub')}")
        self._start_worker(self._run_fire, self.pw.get(), sid, dict(item))

    def on_find_another(self) -> None:
        """Re-scout and lock the next-best candidate, skipping the locked one."""
        sid = self._selected_slot()
        if not sid:
            return
        excl = ""
        if self.locked:
            excl = self.locked.get("permalink") or ""
        self._start_worker(self._run_find_another, sid, excl)

    def _run_find_another(self, sid: str, exclude: str) -> None:
        import comment_precache as _pre
        _prev_log = _pre.log
        _pre.log = self.log
        try:
            slot = get_slot(sid)
            if slot_refused(sid):
                self.log(f"FIND ANOTHER {sid}: refused, slot retired.")
                return
            if not slot or not slot.get("ghost") or not slot.get("email") \
                    or not slot_reddit_ready(slot):
                self.log(f"FIND ANOTHER {sid}: skip, slot not ready.")
                return
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                self.log("FIND ANOTHER: playwright missing.")
                return
            self.log(f"=== FIND ANOTHER (skip {exclude or 'none'}) ===")
            with sync_playwright() as pw:
                ctx, _owned = open_scout_context(pw, self.log)
                _pg_owned = not ctx.pages
                try:
                    page_w = ctx.pages[0] if ctx.pages else ctx.new_page()
                    ranked = self._rank(one_pass(page_w, HOME_URL, 5, 120))
                    pick = pick_another(ranked, exclude)
                    if pick is None:
                        self.log("FIND ANOTHER: no other passing candidate.")
                        return
                    self.log("SCOUT next [%s] r/%s :: %s" % (
                        pick["tier"], pick["sub"], pick["title"][:60]))
                    filled = self._engine_fill(page_w, pick)
                    if filled is None:
                        self.log("FIND ANOTHER: engine fill failed.")
                        return
                    self.locked = None
                    self.log_q.put(("candidates", ranked))
                    self.log_q.put(("lockin", filled))
                finally:
                    try:
                        if _owned:
                            ctx.close()
                        elif _pg_owned:
                            page_w.close()
                    except Exception:
                        pass
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
        finally:
            _pre.log = _prev_log
            try:
                quarantine_tick()
            except Exception:
                pass
            self.root.after(0, self._after_job)

    def _stage(self, t0: float, name: str, budget: float) -> None:
        el = time.monotonic() - t0
        self.log("FIRE_T+%.1fs %s" % (el, name))
        if el > budget:
            raise PhoneError("over budget %.0fs>%ds" % (el, budget))

    def _enter_uiopen(self, phone, stack, item: dict):
        """PRIMARY entry: cold-dispatch the permalink via `uiopen --url`.

        Slot is already staged (container open + login probe) by the caller —
        cold dispatch cannot target containers, so staged-first is the rule.
        Returns ("ok", blob, nodes) on app-post, ("dead", None, None) when the
        post is mod-removed (DEAD/skipped, never retried), ("miss-wall", None,
        None) when the landing dump shows a login wall (logout attribution
        for escalation, zero extra probes), ("miss", None, None) for
        everything else (caller aborts + quarantines - no fallback exists).
        One dispatch only, never re-fired. Zero sends/marks/typing in here."""
        url = (item.get("full_url") or "").strip()
        if not url.startswith("http"):
            return ("miss", None, None)
        t_e = time.monotonic()
        self.log("ENTRY primary uiopen")
        code, out = phone.exec_ok("uiopen --url '%s'" % url.replace("'", ""),
                                  timeout=15)
        if code != 0:
            self.log("ENTRY uiopen dispatch failed (%s), hot fallback"
                     % (out or "").strip()[:100])
            return ("miss", None, None)
        time.sleep(3.0)
        blob, nodes = dump_named(stack, phone, "971_fire_uiopen", self.log, shot=False)
        low = (blob or "").lower()
        if "not safe for work" in low or "nsfw community" in low:
            gate = find_continue_gate(nodes)
            if gate is not None:
                stack.tap_xy(*gate.center())
                time.sleep(2.0)
                blob, nodes = dump_named(stack, phone, "971_fire_gate",
                                         self.log, shot=False)
                self.log("ENTRY NSFW gate Continue from dump %s"
                         % (gate.center(),))
            else:
                upd = find_update_settings(nodes)
                if upd is None:
                    self.log("ENTRY NSFW gate w/o Continue node, hot fallback")
                    return ("miss", None, None)
                self.log("ENTRY NSFW Update Settings from dump %s"
                         % (upd.center(),))
                stack.tap_xy(*upd.center())
                time.sleep(2.0)
                blob, nodes = dump_named(stack, phone, "971_fire_nsfwset",
                                         self.log, shot=False)
                row = find_label_exact(
                    nodes, ("Show NSFW Content (18+)", "Show NSFW Content"))
                if row is None:
                    self.log("ENTRY NSFW switch row missing, hot fallback")
                    return ("miss", None, None)
                stack.tap_xy(615, row.center()[1])
                time.sleep(1.5)
                self.log("ENTRY NSFW switch tapped, re-dispatch once")
                code, out = phone.exec_ok(
                    "uiopen --url '%s'" % url.replace("'", ""), timeout=15)
                if code != 0:
                    return ("miss", None, None)
                time.sleep(3.0)
                blob, nodes = dump_named(stack, phone, "971_fire_reentry",
                                         self.log, shot=False)
                gate = find_continue_gate(nodes)
                if gate is not None and "not safe for work" in (
                        blob or "").lower():
                    stack.tap_xy(*gate.center())
                    time.sleep(2.0)
                    blob, nodes = dump_named(stack, phone, "971_fire_gate2",
                                             self.log, shot=False)
        verdict, proof = uiopen_land_proof(
            blob, item.get("sub") or "", item.get("title") or "",
            item.get("author") or "")
        el = time.monotonic() - t_e
        self.log("ENTRY %s elapsed=%.0fs proof=[%s]" % (verdict, el, proof))
        if verdict == "removed-post":
            return ("dead", None, None)
        if verdict == "app-post" and el <= 120.0:
            return ("ok", blob, nodes)
        if any(w in (blob or "").lower() for w in WALL):
            return ("miss-wall", None, None)
        return ("miss", None, None)

    def _find_reddit_icon(self, stack, phone, stem0: str):
        """SpringBoard hunt for the Reddit icon. Same taps/dumps everywhere."""
        blob, nodes = dump_named(stack, phone, stem0 + "_home", self.log, shot=False)
        icon = springboard_reddit(nodes)
        if icon is None:
            stack.press_home()
            time.sleep(0.6)
            blob, nodes = dump_named(stack, phone, stem0 + "_home2", self.log, shot=False)
            icon = springboard_reddit(nodes)
        page = 0
        while icon is None and page < 4:
            stack.interp_flick(640, 700, 110, 700, steps=14, ms=22)
            time.sleep(0.6)
            blob, nodes = dump_named(stack, phone, "%s_pg%d" % (stem0, page),
                                     self.log, shot=False)
            icon = springboard_reddit(nodes)
            page += 1
        return icon

    def _drawer_login_probe(self, stack, phone, sid: str, stem: str) -> str:
        return drawer_login_probe(stack, phone, self.log, sid, stem)

    def _login_via_accounts(self, stack, phone, sid: str, stem: str) -> str:
        return login_via_accounts(stack, phone, self.log, sid, stem)

    def _restore_until_alive(self, stack, phone, sid: str, stem: str) -> str:
        return restore_until_alive(stack, phone, self.log, sid, stem,
                                   stop=self.stop)

    def _resume_reddit_card(self, stack, phone, sid: str, stem: str) -> str:
        return resume_reddit_card(stack, phone, self.log, sid, stem)

    def _auto_restore(self, stack, phone, sid: str, stem: str) -> str:
        return auto_restore(stack, phone, self.log, sid, stem)
    @staticmethod
    def _icon_middle(png_path, lx, ly, lw):
        """White doc icon above its grid label (dark-mode aware): densest
        bright band wins, text rows are too sparse to qualify. Returns
        (x, y) or None - callers abort instead of text-tapping. Pure."""
        try:
            from PIL import Image
            im = Image.open(png_path).convert("RGB")
        except Exception:
            return None
        W, H = im.size
        try:
            x0, x1 = max(0, int(lx) - 80), min(W, int(lx) + int(lw) + 80)
            y1 = max(0, int(ly) - 10)
        except (TypeError, ValueError):
            return None
        y0 = max(0, y1 - 260)
        if x1 <= x0 or y1 <= y0:
            return None
        try:
            px = im.load()
        except Exception:
            return None
        dense_rows = []
        for yy in range(y0, y1, 2):
            bright = 0
            total = 0
            for xx in range(x0, x1, 4):
                total += 1
                r, g, b = px[xx, yy]
                if r > 150 and g > 150 and b > 150:
                    bright += 1
            if total and bright / total > 0.3:
                dense_rows.append(yy)
        best, cur = (0, 0), None
        for yy in dense_rows + [10 ** 9]:
            if cur is None:
                cur = [yy, yy]
            elif yy - cur[1] <= 4:
                cur[1] = yy
            else:
                if cur[1] - cur[0] > best[1] - best[0]:
                    best = (cur[0], cur[1])
                cur = [yy, yy]
        if best[1] - best[0] < 80:
            return None
        by0, by1 = best
        dense_cols = []
        for xx in range(x0, x1, 2):
            bright = 0
            total = 0
            for yy in range(by0, by1, 4):
                total += 1
                r, g, b = px[xx, yy]
                if r > 150 and g > 150 and b > 150:
                    bright += 1
            if total and bright / total > 0.3:
                dense_cols.append(xx)
        bestc, curc = (0, 0), None
        for xx in dense_cols + [10 ** 9]:
            if curc is None:
                curc = [xx, xx]
            elif xx - curc[1] <= 4:
                curc[1] = xx
            else:
                if curc[1] - curc[0] > bestc[1] - bestc[0]:
                    bestc = (curc[0], curc[1])
                curc = [xx, xx]
        if bestc[1] - bestc[0] < 50:
            return None
        return ((bestc[0] + bestc[1]) // 2, (by0 + by1) // 2)

    @staticmethod
    def _search_field(nodes):
        return search_field(nodes)
    def _open_container(self, stack, phone, sid: str, pick_stem: str,
                        feed_stem: str, allow_restore: bool = True,
                        settle_s: float = 30.0) -> str:
        """Tap the slot row in the Crane picker + login probe. '' when open.
        Scrolls the picker (bounded 4 rounds) so low rows like a0016 are
        found instead of aborting at the first screenful. Settles slow feeds,
        re-probes once on inconclusive, always restores anything not alive."""
        blob, nodes = dump_named(stack, phone, pick_stem, self.log, shot=False)
        if find_label_exact(nodes, ("Select Container",)) is None:
            return "no Crane picker"
        named = None
        # Mapped live 29 Sep (screenshots fl_afterswipe/fl_afterflick):
        # stack.swipe teleports top->a0025+, skipping the middle;
        # interp_flick 900->700 advances exactly ~4 rows. Five checks
        # cover Default..a0035 with zero skipped rows.
        for rnd in range(5):
            named = find_picker_row(nodes, sid)
            if named is not None:
                break
            stack.interp_flick(375, 900, 375, 700, steps=14, ms=22)
            time.sleep(1.2)
            blob, nodes = dump_named(stack, phone, "%s_more%d" % (pick_stem, rnd),
                                     self.log, shot=False)
        if named is None:
            # Fallback for mid/bottom-list rows (mapped live 29 Sep:
            # small 900->700 flicks advance ~4 rows; big swipes jump
            # over a0018-a0024). Sweep up then back down with stall
            # detection. Old path above is untouched.
            prev = blob
            still = 0
            for _i in range(8):
                named = find_picker_row(nodes, sid)
                if named is not None:
                    break
                slow_list_nudge(stack)
                time.sleep(0.55)
                blob, nodes = dump_named(
                    stack, phone, "%s_up%d" % (pick_stem, _i),
                    self.log, shot=False)
                if blob == prev:
                    still += 1
                    if still >= 2:
                        break
                else:
                    still = 0
                prev = blob
            prev = blob
            still = 0
            for _i in range(8):
                if named is not None:
                    break
                stack.interp_flick(375, 400, 375, 700, steps=14, ms=22)
                time.sleep(0.55)
                blob, nodes = dump_named(
                    stack, phone, "%s_down%d" % (pick_stem, _i),
                    self.log, shot=False)
                named = find_picker_row(nodes, sid)
                if blob == prev:
                    still += 1
                    if still >= 2:
                        break
                else:
                    still = 0
                prev = blob
            if named is not None:
                self.log("%s HIT in picker fallback" % sid)
        if named is None:
            return "slot row missing from picker"
        stack.tap_xy(named.center()[0], named.center()[1])
        time.sleep(2.4)
        blob, nodes = dump_named(stack, phone, feed_stem, self.log, shot=False)
        if settle_s > 0:
            end = time.monotonic() + settle_s
            while len(nodes or []) <= 10 and time.monotonic() < end:
                if self.stop.is_set():
                    return "stopped"
                time.sleep(3.0)
                blob, nodes = dump_named(stack, phone, feed_stem + "_settle",
                                         self.log, shot=False)
        if any(w in (blob or "").lower() for w in WALL):
            return "logged-out at feed"
        dead = self._drawer_login_probe(stack, phone, sid, feed_stem + "_login")
        if not dead:
            return ""
        if dead == DRAWER_INCONCLUSIVE:
            if self.stop.is_set():
                return "stopped"
            time.sleep(3.0)
            dead = self._drawer_login_probe(stack, phone, sid, feed_stem + "_login_retry")
            if not dead:
                self.log("LOGIN probe pass on retry (%s session alive)." % sid)
                return ""
        if not dead:
            return ""
        if not allow_restore:
            return dead
        self.log("RESTORE trigger on %s (%s)" % (sid, dead))
        return self._restore_until_alive(stack, phone, sid, feed_stem + "_rs")

    def _session_open(self, password: str, sid: str):
        """Container-once session open: connect + icon + picker + login probe.
        Returns (phone, stack, username, t0, reason); phone None only when the
        SSH connect itself failed (nothing to park). Stems match single FIRE."""
        phone = None
        stack = None
        try:
            slot = get_slot(sid)
            username = str((slot or {}).get("username") or "").strip()
            if slot_refused(sid):
                return None, None, "", 0.0, "slot retired"
            if not username:
                return None, None, "", 0.0, "no username on slot"
            phone, stack = self._connect(password)
            if self.stop.is_set():
                return phone, stack, username, 0.0, "stopped"
            if not stack.can_tap():
                self.log("FIRE: no tap backend. Open XXTouch Elite.")
                return phone, stack, username, 0.0, "no tap backend"
            t0 = time.monotonic()
            self.log(f"=== SESSION {sid} === BUILD {BUILD_TAG}")
            if self._rotate_ip_live(phone, stack):
                return phone, stack, username, t0, "stopped"
            stack.press_home()
            time.sleep(0.7)
            icon = self._find_reddit_icon(stack, phone, "800_fire")
            if icon is None:
                return phone, stack, username, t0, "Reddit icon missing"
            tap_home_icon(stack, icon)
            time.sleep(1.4)
            reason = self._open_container(stack, phone, sid, "810_fire", "820_fire",
                                            allow_restore=True)
            if reason:
                return phone, stack, username, t0, reason
            self.log(f"LOGIN probe pass ({sid} session alive, drawer-proven).")
            self._stage(t0, "container open", BUDGETS["ACQUIRE"])
            if self.stop.is_set():
                return phone, stack, username, t0, "stopped"
            self._verified_container = sid
            return phone, stack, username, t0, ""
        except (PhoneError, Exception):
            if phone is not None:
                try:
                    phone.close()
                except Exception:
                    pass
                self.phone = None
            raise

    def _run_fire(self, password: str, sid: str, item: dict) -> None:
        phone = None
        stack = None
        verdict = "abort"
        reason = ""
        self.last_fire = (sid, dict(item))
        self.last_verdict = ""
        try:
            text = (item.get("reply_text") or "").strip()
            if not text:
                reason = "empty locked text"
                self.log(f"FIRE {sid}: skip, {reason}.")
                return
            phone, stack, username, t0, reason = self._session_open(password, sid)
            if phone is None:
                self.log(f"FIRE {sid}: skip, {reason}.")
                return
            if reason:
                return
            verdict, reason = self._fire_live(phone, stack, sid, username, text,
                                              item, t0)
            if verdict == "posted":
                try:
                    close_reddit(stack, phone, self.log)
                except Exception as e:
                    self.log("close skipped: %s" % str(e)[:100])
            else:
                try:
                    if stack is not None:
                        stack.press_home()
                except Exception:
                    pass
        except PhoneError as e:
            reason = reason or str(e)[:150]
            self.log("ABORT  " + str(e)[:200])
        except Exception as e:
            reason = reason or "%s: %s" % (type(e).__name__, str(e)[:120])
            self.log(f"ABORT  {type(e).__name__}: {str(e)[:150]}")
        finally:
            if phone:
                try:
                    phone.close()
                except Exception:
                    pass
            self.phone = None
            self._close_item(sid, item, verdict, reason)
            self.last_verdict = verdict
            if verdict == "posted":
                try:
                    self._refresh_roster()
                except Exception:
                    pass
            self.root.after(0, self._after_job)

    def _run_fire_nologin(self, password: str, sid: str, item: dict) -> None:
        verdict = "abort"
        reason = ""
        self.last_fire = (sid, dict(item))
        self.last_verdict = ""
        if not (item.get("reply_text") or "").strip():
            reason = "empty locked text"
            self.log(f"NOLOGIN {sid}: skip, {reason}.")
        else:
            try:
                verdict, reason = self._fire_stage(password, sid, item)
            except PhoneError as e:
                reason = str(e)[:150]
                self.log("ABORT  " + str(e)[:200])
            except Exception as e:
                reason = "%s: %s" % (type(e).__name__, str(e)[:120])
                self.log(f"ABORT  {type(e).__name__}: {str(e)[:150]}")
        self._close_item(sid, item, verdict, reason)
        self.last_verdict = verdict
        if verdict == "posted":
            try:
                self._refresh_roster()
            except Exception:
                pass
        self.root.after(0, self._after_job)

    def _close_item(self, sid: str, item: dict, verdict: str, reason: str) -> None:
        """Per-item bookkeeping shared by single FIRE and batch: streak,
        entry-abort quarantine, run log. No GUI refresh, no worker release."""
        fp = "slot=%s sub=%s reason=%s" % (sid, (item or {}).get("sub"), reason or verdict)
        streak_update(verdict, reason, fp)
        if verdict != "posted" and reason in ENTRY_ABORTS:
            try:
                quarantine_add((item or {}).get("permalink") or "", QUARANTINE_RUNS)
                self.log("QUARANTINE %s K=%d" % ((item or {}).get("permalink"),
                                                 QUARANTINE_RUNS))
            except OSError:
                pass
        _verb = {"slot": sid, "sub": (item or {}).get("sub"),
                 "title": (item or {}).get("title"), "url": (item or {}).get("full_url"),
                 "verdict": verdict, "reason": reason,
                 "text": (item or {}).get("reply_text", "")[:160]}
        run_log(_verb)
        if verdict != "posted":
            error_log(sid, (item or {}).get("sub"), (item or {}).get("title"),
                      verdict, reason)
        try:
            self._batch_auto_write("RUNVERB %s" % json.dumps(_verb, ensure_ascii=False))
        except Exception:
            pass
        self.log("RUN %s %s" % (verdict, reason or (item or {}).get("full_url", "")))

    def _scout_pass(self, page_w, url: str, min_score: int, max_age: float,
                    sid: str, tag: str, extra_allow: frozenset = frozenset()):
        """One scout sweep of a listing: scroll, rank, rotation auto-pick.
        Returns (auto_item_or_None, ranked, raws)."""
        from collections import Counter as _Counter
        raws = one_pass(page_w, url, min_score, max_age)
        ranked = self._rank(raws, min_score, max_age, sid, extra_allow)
        tiers = _Counter(c.get("tier") or "SKIP" for c in ranked)
        self.log("SCOUT %s %d raws: %s" % (tag, len(raws), dict(tiers)))
        for _c in [c for c in ranked if not c.get("tier")][:4]:
            self.log("SCOUT skip r/%s %s" % (_c.get("sub"), _c.get("verdict")))
        auto = self._autopick(ranked)
        if auto is not None:
            self.log("ROTATION r/%s [%s]" % (auto.get("sub"), auto.get("tier")))
        return auto, ranked, raws

    def _prep_next(self, sid: str, min_score: int = 5, max_age: float = 360):
        """Lean batch scout: one PC-side home-feed pass + engine fill. No phone,
        no GUI puts, no waiting: empty feed skips the item fast and the batch
        moves on. Returns (filled_item, "") or (None, reason)."""
        try:
            slot = get_slot(sid)
            if slot_refused(sid):
                return None, "slot retired"
            if not slot or not slot.get("ghost"):
                return None, "no Ghost row"
            if not slot.get("email"):
                return None, "no Hide My Email"
            if not slot_reddit_ready(slot):
                return None, "Create+Day1 unfinished"
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                return None, "playwright missing"
            if not os.path.isdir(BOT_PROFILE_DIR):
                return None, "bot Chrome profile missing"
            allow = self._phase_allow()
            with sync_playwright() as pw:
                try:
                    return self._prep_scout_run(pw, sid, min_score, max_age,
                                                allow)
                except _FeedThrottled as e:
                    self.log("Feed throttled (%s), restarting bot Chrome "
                             "once and resuming…" % str(e)[:80])
                    try:
                        reap_stale_bot_chrome(self.log)
                    except Exception:
                        pass
                    try:
                        ensure_bot_browser(self.log)
                    except Exception as e2:
                        return None, ("bot Chrome restart failed: %s"
                                      % str(e2)[:80])
                    return self._prep_scout_run(pw, sid, min_score, max_age,
                                                allow)
        finally:
            try:
                quarantine_tick()
            except Exception:
                pass

    def _prep_scout_run(self, pw, sid: str, min_score: int, max_age: float,
                        allow: frozenset):
        """One scout+fill attempt on an open playwright handle. _FeedThrottled
        (after one_pass's own refresh+backoff) propagates so the caller can
        restart the browser once; ctx/page cleanup still runs via finally."""
        ctx, _owned = open_scout_context(pw, self.log)
        _pg_owned = not ctx.pages
        try:
            page_w = ctx.pages[0] if ctx.pages else ctx.new_page()
            auto, _ranked, _raws = self._scout_pass(
                page_w, HOME_URL, min_score, max_age, sid, "hot", allow)
            if auto is None:
                self.log("Sweeping %s (2h, hottest first)"
                         % HOME_FALLBACK_URL)
                _home_auto, _home_ranked, home_raws = self._scout_pass(
                    page_w, HOME_FALLBACK_URL, min_score, 120,
                    sid, "home", allow)
                for _cand in self.top_candidates(_home_ranked, 3):
                    filled = self._engine_fill(page_w, _cand)
                    if filled is not None:
                        return filled, ""
                loose = self._rank(home_raws, 0, 1e9, sid, allow)
                for _cand in self.top_candidates(loose, 3):
                    self.log("LAST-DITCH r/%s %sup"
                             % (_cand.get("sub"), _cand.get("score")))
                    filled = self._engine_fill(page_w, _cand)
                    if filled is not None:
                        return filled, ""
                return None, "nothing passing"
            filled = self._engine_fill(page_w, auto)
            if filled is None:
                return None, "engine fill failed"
            return filled, ""
        finally:
            try:
                if _owned:
                    ctx.close()
                elif _pg_owned:
                    page_w.close()
            except Exception:
                pass

    def _scout_rank(self, page_w, url: str, min_score: int, max_age: float,
                    sid: str, tag: str, allow: frozenset):
        """Scout sweep without autopick (workflow 1 uses run-local rotation).
        Returns (ranked, raws)."""
        from collections import Counter as _Counter
        raws = one_pass(page_w, url, min_score, max_age)
        ranked = self._rank(raws, min_score, max_age, sid, allow)
        tiers = _Counter(c.get("tier") or "SKIP" for c in ranked)
        self.log("SCOUT %s %d raws: %s" % (tag, len(raws), dict(tiers)))
        for _c in [c for c in ranked if not c.get("tier")][:4]:
            self.log("SCOUT skip r/%s %s" % (_c.get("sub"), _c.get("verdict")))
        return ranked, raws

    def _wf1_autopick(self, ranked: list) -> dict | None:
        """Run-local rotation pick for workflow 1 (no file pointer)."""
        order = list(getattr(self, "_wf1_sub_order", []) or [])
        idx = int(getattr(self, "_wf1_sub_idx", 0) or 0)
        pick, nxt = rotation_pick_in_order(ranked, order, idx)
        try:
            self._wf1_sub_idx = int(nxt)
        except Exception:
            pass
        return pick

    def _prep_workflow1(self, sid: str, min_score: int = 5, max_age: float = 360):
        """Workflow-1 hunt: tiered sweep, run-order rotation pick first,
        retries stay on the same container until a postable candidate fills
        (nsfw/voice/PASS skip to the next candidate). Last-ditch closes.
        Returns (filled_item, "") or (None, reason)."""
        try:
            slot = get_slot(sid)
            if slot_refused(sid):
                return None, "slot retired"
            if not slot or not slot.get("ghost"):
                return None, "no Ghost row"
            if not slot.get("email"):
                return None, "no Hide My Email"
            if not slot_reddit_ready(slot):
                return None, "Create+Day1 unfinished"
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                return None, "playwright missing"
            if not os.path.isdir(BOT_PROFILE_DIR):
                return None, "bot Chrome profile missing"
            allow = self._phase_allow()
            with sync_playwright() as pw:
                ctx, _owned = open_scout_context(pw, self.log)
                _pg_owned = not ctx.pages
                try:
                    page_w = ctx.pages[0] if ctx.pages else ctx.new_page()
                    stages = []
                    ranked, _raws = self._scout_rank(
                        page_w, HOME_URL, min_score, max_age, sid, "wf1-hot", allow)
                    auto = self._wf1_autopick(ranked)
                    if auto is not None:
                        self.log("ROTATION r/%s [%s]" % (auto.get("sub"), auto.get("tier")))
                    stages.append((ranked, auto))
                    if auto is None:
                        self.log("WF1 sweeping %s (2h, hottest first)"
                                 % HOME_FALLBACK_URL)
                        ranked2, raws2 = self._scout_rank(
                            page_w, HOME_FALLBACK_URL, min_score, 120,
                            sid, "wf1-home", allow)
                        auto2 = self._wf1_autopick(ranked2)
                        if auto2 is not None:
                            self.log("ROTATION r/%s [%s]" % (
                                auto2.get("sub"), auto2.get("tier")))
                        stages.append((ranked2, auto2))
                        loose = self._rank(raws2, 0, 1e9, sid, allow)
                        stages.append((loose, None))
                    tried: set = set()
                    for ranked_i, auto_i in stages:
                        for cand in try_order(ranked_i, auto_i)[:WF1_TRIES_PER_STAGE]:
                            if self.stop.is_set():
                                return None, "stopped"
                            perma = cand.get("permalink") or ""
                            if not perma or perma in tried:
                                continue
                            tried.add(perma)
                            filled = self._engine_fill(page_w, cand)
                            if filled is None:
                                self.log("WF1 retry same container (fill failed).")
                                continue
                            ok, why = voice_ok(filled.get("reply_text") or "")
                            if not ok:
                                self.log("WF1 retry same container (voice: %s)." % why)
                                continue
                            return filled, ""
                    return None, "nothing postable"
                finally:
                    try:
                        if _owned:
                            ctx.close()
                        elif _pg_owned:
                            page_w.close()
                    except Exception:
                        pass
        finally:
            try:
                quarantine_tick()
            except Exception:
                pass

    def _batch_resume(self, stack, phone, sid: str) -> str:
        """No-login resume: no taps, no dumps, no probe, no restore.
        First item's session-open probe covers the run; uiopen entry
        foregrounds the app per item. Next sid gets a fresh probe."""
        self.log("BATCH resume: no-login, uiopen entry foregrounds per item.")
        return ""

    def _run_batch(self, password: str, sid: str, n: int,
                   min_score: int = 5, max_age: float = 360,
                   _chain: bool = False) -> None:
        import random as _random
        rng = _random.Random()
        bid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        outcomes: list = []
        breaks: list = []
        phone = None
        stack = None
        _auto_path = self._batch_auto_start(sid, n)
        if _auto_path:
            self.log("SESSION-LOG %s" % _auto_path)
        try:
            self.log(f"=== BATCH {bid} {sid} x{n} === BUILD {BUILD_TAG}")
            self.log("BATCH gates: min_score=%d max_age=%.0fm" % (min_score, max_age))
            phone, stack, username, _t0, reason = self._session_open(password, sid)
            if phone is None or reason:
                self.log(f"BATCH {sid}: session failed, {reason}.")
                _verb0 = {"batch": bid, "slot": sid, "n": n, "outcomes": outcomes,
                          "breaks": breaks, "verdict": "abort", "reason": reason}
                run_log(_verb0)
                self._batch_auto_write("RUNVERB %s" % json.dumps(_verb0, ensure_ascii=False))
                self._batch_auto_finish("abort-session")
                return
            state = spacing_load()
            posted_count = 0
            need_break = False
            i = -1
            cap = batch_attempt_cap(n)
            while posted_count < n and i + 1 < cap:
                i += 1
                if self.stop.is_set():
                    self.log("BATCH STOP halts after current.")
                    break
                used_break = 0.0
                if i == 0:
                    self.log("BATCH item 1: login-checked session already open.")
                    filled, prep_reason = self._prep_next(sid, min_score, max_age)
                else:
                    if not need_break:
                        self.log("BATCH item %d: no-login PREP." % (i + 1))
                        filled, prep_reason = self._prep_next(sid, min_score, max_age)
                    else:
                        used_break = draw_break_minutes(rng, bool(state.get("escalated")))
                        need_break = False
                        breaks.append(round(used_break, 1))
                        self.log("BATCH break %.1f min (phone idles, PC scouts next)"
                                 % used_break)
                        prep_box: dict = {}

                        def _prep_work() -> None:
                            try:
                                prep_box["item"], prep_box["reason"] = self._prep_next(
                                    sid, min_score, max_age)
                            except Exception as e:
                                prep_box["item"] = None
                                prep_box["reason"] = "%s: %s" % (type(e).__name__,
                                                                 str(e)[:80])

                        th = threading.Thread(target=_prep_work, daemon=True)
                        th.start()
                        deadline = time.monotonic() + used_break * 60.0
                        while th.is_alive():
                            if self.stop.is_set():
                                break
                            time.sleep(5.0)
                        th.join(timeout=300.0)
                        if self.stop.is_set():
                            self.log("BATCH STOP halts after current.")
                            break
                        if th.is_alive():
                            filled, prep_reason = None, "prep thread timeout"
                        else:
                            filled = prep_box.get("item")
                            prep_reason = prep_box.get("reason", "prep lost")
                        remaining = deadline - time.monotonic()
                        if remaining > 0:
                            self.log("BATCH break rest %.1f min (prep done early)"
                                     % (remaining / 60.0))
                        while remaining > 0:
                            if self.stop.is_set():
                                break
                            time.sleep(min(5.0, remaining))
                            remaining = deadline - time.monotonic()
                        if self.stop.is_set():
                            self.log("BATCH STOP halts after current.")
                            break
                if filled is None:
                    self.log("BATCH item %d: prep skip (%s)." % (i + 1, prep_reason))
                    outcomes.append({"item": i + 1, "verdict": "skip",
                                     "reason": prep_reason})
                    continue
                ok, why = voice_ok(filled.get("reply_text") or "")
                if not ok:
                    self.log("BATCH item %d: VOICE skip (%s)." % (i + 1, why))
                    outcomes.append({"item": i + 1, "verdict": "skip",
                                     "reason": "voice: " + why})
                    continue
                if i > 0:
                    reason = self._batch_resume(stack, phone, sid)
                    if reason:
                        self.log("BATCH item %d: resume abort (%s), pausing batch."
                                 % (i + 1, reason))
                        self._close_item(sid, filled, "abort", reason)
                        outcomes.append({"item": i + 1, "verdict": "abort",
                                         "reason": reason})
                        if escalation_due(state.get("spacings", []), reason,
                                          time.time()):
                            state["escalated"] = True
                            spacing_save(state)
                            self.log("ESCALATE to 5-min breaks + immediate report: "
                                     "logout/flag within 24h of sub-5 spacing.")
                            _esc1 = {"batch": bid, "slot": sid,
                                     "alert": "break-escalation", "reason": reason}
                            run_log(_esc1)
                            self._batch_auto_write("RUNVERB %s" % json.dumps(_esc1, ensure_ascii=False))
                        break
                text = (filled.get("reply_text") or "").strip()
                if not text:
                    outcomes.append({"item": i + 1, "verdict": "skip",
                                     "reason": "empty engine text"})
                    continue
                state.setdefault("spacings", []).append(
                    {"ts": time.time(), "minutes": round(used_break, 1)})
                state["spacings"] = state["spacings"][-50:]
                spacing_save(state)
                verdict, reason = self._fire_live(
                    phone, stack, sid, username, text, filled, time.monotonic())
                self._close_item(sid, filled, verdict, reason)
                outcomes.append({"item": i + 1, "verdict": verdict, "reason": reason})
                if verdict == "posted":
                    posted_count += 1
                    need_break = True
                if verdict != "posted" and reason != DEAD_REASON:
                    self.log("BATCH abort pauses batch (%s)." % reason)
                    if escalation_due(state.get("spacings", []), reason, time.time()):
                        state["escalated"] = True
                        spacing_save(state)
                        self.log("ESCALATE to 5-min breaks + immediate report: "
                                 "logout/flag within 24h of sub-5 spacing.")
                        _esc2 = {"batch": bid, "slot": sid,
                                 "alert": "break-escalation", "reason": reason}
                        run_log(_esc2)
                        self._batch_auto_write("RUNVERB %s" % json.dumps(_esc2, ensure_ascii=False))
                    break
            posted = sum(1 for o in outcomes if o.get("verdict") == "posted")
            self.log("BATCH %s done: %d/%d posted in %d attempts, breaks=%s"
                     % (bid, posted, n, len(outcomes), breaks))
            _verb_done = {"batch": bid, "slot": sid, "n": n, "outcomes": outcomes,
                          "breaks": breaks, "verdict": "done"}
            run_log(_verb_done)
            self._batch_auto_write("RUNVERB %s" % json.dumps(_verb_done, ensure_ascii=False))
            self._batch_auto_finish("done-%d/%d" % (posted, n))
        except PhoneError as e:
            self.log("BATCH ABORT " + str(e)[:200])
            _verb_ab = {"batch": bid, "slot": sid, "n": n, "outcomes": outcomes,
                        "breaks": breaks, "verdict": "abort", "reason": str(e)[:150]}
            run_log(_verb_ab)
            self._batch_auto_write("RUNVERB %s" % json.dumps(_verb_ab, ensure_ascii=False))
            self._batch_auto_finish("abort")
        except Exception as e:
            self.log(f"BATCH ABORT {type(e).__name__}: {str(e)[:150]}")
            _verb_ex = {"batch": bid, "slot": sid, "n": n, "outcomes": outcomes,
                        "breaks": breaks, "verdict": "abort",
                        "reason": "%s: %s" % (type(e).__name__, str(e)[:120])}
            run_log(_verb_ex)
            self._batch_auto_write("RUNVERB %s" % json.dumps(_verb_ex, ensure_ascii=False))
            self._batch_auto_finish("abort")
        finally:
            try:
                if stack is not None:
                    stack.press_home()
            except Exception:
                pass
            if phone:
                try:
                    phone.close()
                except Exception:
                    pass
            self.phone = None
            try:
                self._refresh_roster()
            except Exception:
                pass
            if not _chain:
                self.root.after(0, self._after_job)

    def _wf1_slot(self, password: str, wid: str, sid: str, n: int, total: int,
                  min_score: int, max_age: float, state: dict,
                  outcomes: list, attempt: int = 1) -> None:
        """One workflow-1 slot attempt: PREP login -> Home -> Chrome PREP ->
        FIRE no-login -> Home. Skips (never raises) on any error."""
        self.log("WF1 %d/%d starting %s (attempt %d)." % (n, total, sid, attempt))
        self._set_batch_tab_slot(sid)
        _auto_path = self._batch_auto_start(sid, 1)
        if _auto_path:
            self.log("SESSION-LOG %s" % _auto_path)
        try:
            reason = self._login_stage(password, sid)
            if reason:
                self.log(f"WF1 {sid}: login failed, skipping slot ({reason}).")
                _v0 = {"wf1": wid, "slot": sid, "attempt": attempt,
                       "verdict": "skip", "reason": reason}
                run_log(_v0)
                error_log(sid, "", "", "skip", reason)
                self._batch_auto_write("RUNVERB %s" % json.dumps(_v0, ensure_ascii=False))
                self._batch_auto_finish("skip-login")
                outcomes.append({"slot": sid, "verdict": "skip", "reason": reason})
                return
            filled, prep_reason = self._prep_workflow1(sid, min_score, max_age)
            if filled is None:
                self.log(f"WF1 {sid}: no postable candidate, skipping slot ({prep_reason}).")
                _v1 = {"wf1": wid, "slot": sid, "attempt": attempt,
                       "verdict": "skip", "reason": prep_reason}
                run_log(_v1)
                error_log(sid, "", "", "skip", prep_reason)
                self._batch_auto_write("RUNVERB %s" % json.dumps(_v1, ensure_ascii=False))
                self._batch_auto_finish("skip-prep")
                outcomes.append({"slot": sid, "verdict": "skip", "reason": prep_reason})
                return
            verdict, reason = self._fire_stage(password, sid, filled)
            self._close_item(sid, filled, verdict, reason)
            outcomes.append({"slot": sid, "verdict": verdict, "reason": reason})
            if verdict != "posted" and reason != DEAD_REASON:
                self.log("WF1 %s abort (%s), skipping slot." % (sid, reason))
                if escalation_due(state.get("spacings", []), reason, time.time()):
                    state["escalated"] = True
                    spacing_save(state)
                    self.log("ESCALATE to 5-min breaks + immediate report: "
                             "logout/flag within 24h of sub-5 spacing.")
                    _esc = {"wf1": wid, "slot": sid, "attempt": attempt,
                            "alert": "break-escalation", "reason": reason}
                    run_log(_esc)
                    self._batch_auto_write("RUNVERB %s" % json.dumps(_esc, ensure_ascii=False))
        except PhoneError as e:
            self.log("WF1 %s ABORT %s, skipping slot." % (sid, str(e)[:150]))
            _ve = {"wf1": wid, "slot": sid, "attempt": attempt,
                   "verdict": "skip", "reason": str(e)[:150]}
            run_log(_ve)
            error_log(sid, "", "", "skip", str(e)[:150])
            self._batch_auto_write("RUNVERB %s" % json.dumps(_ve, ensure_ascii=False))
            outcomes.append({"slot": sid, "verdict": "skip", "reason": str(e)[:150]})
        except Exception as e:
            self.log(f"WF1 {sid} ABORT {type(e).__name__}: {str(e)[:150]}, skipping slot.")
            try:
                import traceback as _tb
                self.log_debug("TRACEBACK %s:\n%s" % (
                    sid, "".join(_tb.format_exception(type(e), e,
                                                       e.__traceback__))))
            except Exception:
                pass
            _er = "%s: %s" % (type(e).__name__, str(e)[:120])
            _ve = {"wf1": wid, "slot": sid, "attempt": attempt, "verdict": "skip",
                   "reason": _er}
            run_log(_ve)
            error_log(sid, "", "", "skip", _er)
            self._batch_auto_write("RUNVERB %s" % json.dumps(_ve, ensure_ascii=False))
            outcomes.append({"slot": sid, "verdict": "skip", "reason": _er})
        finally:
            self.phone = None
            self._batch_auto_finish("slot-done")
        self.log("WF1 finished %s (attempt %d)." % (sid, attempt))

    @staticmethod
    def _wf1_retry_list(sids: list, outcomes: list) -> list:
        """Non-posted slots with fewer than 2 fails, in run order. A slot
        that fails twice parks until the next batch click (fresh outcomes
        unpark it). Pure."""
        posted = {o.get("slot") for o in (outcomes or [])
                  if isinstance(o, dict) and o.get("verdict") == "posted"}
        fails: dict = {}
        for o in (outcomes or []):
            if isinstance(o, dict) and o.get("verdict") != "posted":
                fails[o.get("slot")] = fails.get(o.get("slot"), 0) + 1
        return [s for s in (sids or [])
                if s not in posted and fails.get(s, 0) < 2]

    @staticmethod
    def _wf1_parked(sids: list, outcomes: list) -> list:
        """Non-posted slots with 2+ fails: parked until next run. Pure."""
        posted = {o.get("slot") for o in (outcomes or [])
                  if isinstance(o, dict) and o.get("verdict") == "posted"}
        fails: dict = {}
        for o in (outcomes or []):
            if isinstance(o, dict) and o.get("verdict") != "posted":
                fails[o.get("slot")] = fails.get(o.get("slot"), 0) + 1
        return sorted(s for s in (sids or [])
                      if s not in posted and fails.get(s, 0) >= 2)

    def _run_workflow1(self, password: str, start_sid: str,
                       min_score: int = 5, max_age: float = 360,
                       fresh_only: bool = False,
                       spread: bool = True) -> None:
        """Workflow 1: one comment per slot down the fleet tail, then retry
        passes over non-posted slots until all posted or a pass makes no
        progress (cap 5 passes). A slot that fails twice parks until the
        next batch click.
        Per slot: PREP login -> Home -> Chrome PREP -> FIRE no-login -> Home.
        Spread gaps 20-60min between slots (9am-11pm fit, fresh draw each);
        logout/flag skips the slot and continues.
        fresh_only: keep only slots with no posted verdict today (UTC),
        fully shuffled (no pinned start)."""
        wid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        outcomes: list = []
        try:
            sids = fleet_sids("" if fresh_only else start_sid)
            if not sids:
                self.log("WF1: no usable slots from %s." % (start_sid or "?"))
                return
            import os as _os
            import random as _random
            seed = int.from_bytes(_os.urandom(4), "big")
            rng = _random.Random(seed)
            if fresh_only:
                done = posted_today(self._load_runs())
                before = list(sids)
                sids = [s for s in sids if s not in done]
                skipped = [s for s in before if s in done]
                if skipped:
                    self.log("WF1 fresh-only: already commented today: %s."
                             % ",".join(skipped))
                if not sids:
                    self.log("WF1 fresh-only: every slot already commented today.")
                    return
                sids = seeded_shuffle(sids, seed)
            else:
                sids = [sids[0]] + seeded_shuffle(sids[1:], seed)
            self._wf1_sub_order = seeded_shuffle(phase_approved(self._phase_name()), seed)
            self._wf1_sub_idx = 0
            state = spacing_load()
            self.log("=== WF1 %s from %s === BUILD %s" % (wid, start_sid, BUILD_TAG))
            self.log("WF1 seed %d." % seed)
            self.log("WF1 order: %s (1 each, %s)" % (",".join(sids), self._phase_name()))
            self.log("WF1 subs: %s." % ",".join(self._wf1_sub_order))
            self.log("WF1 gates: min_score=%d max_age=%.0fm" % (min_score, max_age))
            if spread:
                self.log("Scheduled %d slot(s), spread 20-60min gaps." % len(sids))
            _vd_seed = {"wf1": wid, "seed": seed, "order": sids,
                        "subs": list(self._wf1_sub_order)}
            run_log(_vd_seed)
            self._batch_auto_write("RUNVERB %s" % json.dumps(_vd_seed, ensure_ascii=False))
            for idx, sid in enumerate(sids):
                if self.stop.is_set():
                    self.log("WF1 STOP before %s." % sid)
                    break
                if spread and idx > 0 and _last_posted(outcomes):
                    gap, clamped = draw_spread_minutes(rng, len(sids) - idx)
                    if clamped:
                        self.log("Window tight: gap clamped to %.1f min." % gap)
                    if self._spread_wait(gap, sid):
                        self.log("WF1 STOP during spread gap before %s." % sid)
                        break
                self._wf1_slot(password, wid, sid, idx + 1, len(sids),
                               min_score, max_age, state, outcomes, 1)
            retry = self._wf1_retry_list(sids, outcomes)
            announced: set = set()
            passes = 0
            while retry and not self.stop.is_set() and passes < 5:
                passes += 1
                posted_before = sum(1 for o in outcomes
                                    if o.get("verdict") == "posted")
                self.log("WF1 retry pass %d: %s." % (passes, ",".join(retry)))
                for idx, sid in enumerate(retry):
                    if self.stop.is_set():
                        self.log("WF1 STOP before retry %s." % sid)
                        break
                    if spread and _last_posted(outcomes):
                        gap, clamped = draw_spread_minutes(rng, len(retry) - idx)
                        if clamped:
                            self.log("Window tight: gap clamped to %.1f min." % gap)
                        if self._spread_wait(gap, sid):
                            self.log("WF1 STOP during spread gap before %s." % sid)
                            break
                    self._wf1_slot(password, wid, sid, idx + 1, len(retry),
                                   min_score, max_age, state, outcomes, 2)
                posted_now = sum(1 for o in outcomes
                                 if o.get("verdict") == "posted")
                retry = self._wf1_retry_list(sids, outcomes)
                for sid in self._wf1_parked(sids, outcomes):
                    if sid not in announced:
                        announced.add(sid)
                        self.log("WF1 %s failed twice, parked until next run."
                                 % sid)
                if retry and posted_now == posted_before:
                    self.log("WF1 retry pass %d: no progress, ending." % passes)
                    break
            if passes >= 5 and retry and not self.stop.is_set():
                self.log("WF1 retry cap reached (5 passes), ending with: %s."
                         % ",".join(retry))
            posted = sum(1 for o in outcomes if o.get("verdict") == "posted")
            self.log("WF1 %s done: %d posted, %d attempts." % (wid, posted, len(outcomes)))
            _vd = {"wf1": wid, "from": start_sid, "outcomes": outcomes, "verdict": "done"}
            run_log(_vd)
            self._batch_auto_write("RUNVERB %s" % json.dumps(_vd, ensure_ascii=False))
        except PhoneError as e:
            self.log("WF1 ABORT " + str(e)[:200])
        except Exception as e:
            self.log(f"WF1 ABORT {type(e).__name__}: {str(e)[:150]}")
            try:
                import traceback as _tb
                self.log_debug("TRACEBACK run %s:\n%s" % (
                    wid, "".join(_tb.format_exception(type(e), e,
                                                       e.__traceback__))))
            except Exception:
                pass
        finally:
            try:
                self._refresh_roster()
            except Exception:
                pass
            self.root.after(0, self._after_job)

    def _fire_live(self, phone, stack, sid: str, username: str, text: str,
                   item: dict, t0: float):
        """One item send on an open session: entry -> composer -> type ->
        strict local VERIFY -> mark. Returns (verdict, reason). No closeout.
        Login is verified once per container switch (_session_open stamps
        _verified_container); per-item send proof backstops the rest."""
        reason = ""
        if not same_container(getattr(self, "_verified_container", ""), sid):
            return "abort", "container switch unverified"
        try:
            entry, _eb, _en = self._enter_uiopen(phone, stack, item)
            if entry == "dead":
                reason = DEAD_REASON
                self.log("DEAD skip, never re-fire (counted in routing, out of verification)")
                return "abort", reason
            if entry == "miss-wall":
                reason = "logged-out at entry"
                self.log("ENTRY login wall, abort (logout-flagged, no quarantine)")
                return "abort", reason
            if entry != "ok":
                reason = "uiopen entry miss"
                self.log("ENTRY miss, abort + quarantine (no fallback exists)")
                return "abort", reason
            blob, nodes = _eb, _en
            self.log("ENTRY uiopen landed")
            cont = find_label_exact(nodes, ("Continue",))
            if cont is not None and any(k in (blob or "").lower()
                                        for k in ("nsfw", "not safe for work", "over 18")):
                stack.tap_xy(*cont.center())
                time.sleep(1.4)
                blob, nodes = dump_named(stack, phone, "975_fire", self.log, shot=False)
                refuse_join(nodes, self.log)
            self._stage(t0, "app open", 300.0)
            t_gate = time.monotonic()
            blob, nodes = dump_named(stack, phone, "3020_fire", self.log, shot=False)
            low = (blob or "").lower()
            if ("r/" + item["sub"]) not in low.replace(" ", ""):
                reason = "wrong post"
                return "abort", reason
            hit = self._bubble(nodes)
            join = self._join_field(nodes)
            if hit is None and join is None:
                # Action bar below the fold: nudge the post up/down a
                # little until Join/bubble surfaces. Bounded + stall-break.
                prev = blob
                still = 0
                found = False
                for _di, (_x1, _y1, _x2, _y2) in enumerate(
                        [(375, 900, 375, 650)] * 3
                        + [(375, 400, 375, 650)] * 3):
                    stack.interp_flick(_x1, _y1, _x2, _y2,
                                       steps=14, ms=22)
                    time.sleep(1.0)
                    blob, nodes = dump_named(
                        stack, phone, "3025_fire_hunt%d" % _di, self.log,
                        shot=False)
                    hit = self._bubble(nodes)
                    join = self._join_field(nodes)
                    if hit is not None or join is not None:
                        self.log("FIRE action bar HIT after scroll %d."
                                 % (_di + 1))
                        found = True
                        break
                    if blob == prev:
                        still += 1
                        if still >= 2:
                            break
                    else:
                        still = 0
                    prev = blob
                if not found:
                    reason = "no Join, no bubble"
                    return "abort", reason
            if join is not None:
                stack.tap_xy(*join)
                time.sleep(2.0)
                blob, nodes = dump_named(stack, phone, "3030_fire", self.log, shot=False)
            elif hit is not None:
                if hit[1] <= 1100:
                    reason = "bubble outside action bar"
                    return "abort", reason
                stack.tap_xy(hit[0], hit[1])
                time.sleep(1.6)
                blob, nodes = dump_named(stack, phone, "3030_fire", self.log, shot=False)
            else:
                reason = "no Join, no bubble"
                return "abort", reason
            low = (blob or "").lower()
            if "replying to" in low:
                reason = "nested composer (reply path)"
                return "abort", reason
            if not ("commenting on" in low or "join the conversation" in low or "add a comment" in low):
                reason = "composer absent"
                return "abort", reason
            self._stage(t_gate, "composer ok", BUDGETS["GATE"])
            if self.stop.is_set():
                reason = "stopped"
                return "abort", reason
            add = join if join is not None else self._join_field(nodes)
            if add is not None and "commenting on" not in low:
                stack.tap_xy(*add)
                time.sleep(1.2)
                blob, nodes = dump_named(stack, phone, "3035_fire", self.log, shot=False)
                if "replying to" in (blob or "").lower():
                    reason = "nested composer"
                    return "abort", reason
            t_type = time.monotonic()
            from fieldvalue import keyboard_present
            if not keyboard_present(nodes):
                if add is not None:
                    stack.tap_xy(*add)
                    time.sleep(1.0)
                    blob, nodes = dump_named(stack, phone, "3036_fire", self.log, shot=False)
            if not keyboard_present(nodes):
                reason = "focus lost (no keyboard)"
                return "abort", reason
            stack.type_human(text, ms=95)
            time.sleep(0.8)
            blob, nodes = dump_named(stack, phone, "3040_fire", self.log, shot=True)
            dist = distinct_word(text)
            if not typed_echo_ok(blob, text):
                reason = "typed text unverifiable"
                return "abort", reason
            self._stage(t_type, "typed " + dist, BUDGETS["TYPE"])
            if self.stop.is_set():
                reason = "stopped"
                return "abort", reason
            t_send = time.monotonic()
            _, live = dump_named(stack, phone, "3041_fire", self.log, shot=False)
            send = self._post_button(live)
            if send is None:
                reason = "no Post/Reply button"
                return "abort", reason
            stack.tap_xy(int(send[0]), int(send[1]))
            time.sleep(3.0)
            blob, nodes = dump_named(stack, phone, "3050_fire", self.log, shot=True)
            need = [username.strip().lower(), "now"] + ([dist] if dist else [])
            need = [n for n in need if n]
            if not all(n in (blob or "").lower() for n in need):
                reason = "local VERIFY missing " + "+".join(need)
                return "abort", reason
            self._stage(t_send, "sent", BUDGETS["SEND"])
            self._stage(t0, "total", 720.0)
            verdict = "posted"
            self.log("VERIFY_OK %s" % "+".join(need))
            mark_commented(item["sub"], item["title"], sid, "comment")
            self.log("MARKED %s" % item["title"][:60])
            return "posted", reason
        except (PhoneError, Exception):
            raise

    @staticmethod
    def _bubble(nodes):
        best = None
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            if "comment" in collapsed_text(n.labels()) and n.y > 1000:
                best = n
                break
        if best is not None:
            return best.center()
        bar = []
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            if n.y < 950 or n.h > 80 or n.w >= 150:
                continue
            t = collapsed_text(n.labels())
            if any(k in t.lower() for k in ("•", "ago", "mod", "minute", "hour", "day")):
                continue
            if any(ch.isdigit() for ch in t) and 250 < n.x < 450:
                bar.append(n)
        if not bar:
            return None
        return min(bar, key=lambda n: abs(n.center()[0] - 360)).center()

    @staticmethod
    def _join_field(nodes):
        hit = find_label_exact(nodes, ("Add a comment", "Add comment",
                                       "Join the conversation", "Be the first to comment",
                                       "Comment"))
        if hit is not None:
            return hit.center()
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            if "add a comment" in collapsed_text(n.labels()):
                return n.center()
        return None

    @staticmethod
    def _post_button(nodes):
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            if collapsed_text(n.labels()) == "post" and n.y < 200:
                return n.center()
        cands = []
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            if collapsed_text(n.labels()) == "reply" and n.y < 1000 and n.x > 400:
                cands.append(n)
        if cands:
            return max(cands, key=lambda n: n.x).center()
        return None

    # ----- queue-drain fan-out -----
    def _drain_log(self) -> None:
        try:
            while True:
                msg = self.log_q.get_nowait()
                if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == "candidates":
                    self._show_candidates(msg[1])
                    continue
                if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == "lockin":
                    self._show_lockin(msg[1])
                    continue
                if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == "dry":
                    try:
                        self.dry_box.configure(state="normal")
                        self.dry_box.delete("1.0", "end")
                        self.dry_box.insert("1.0", msg[1])
                        self.dry_box.configure(state="disabled")
                        self.nb.select(self.dry_tab)
                    except (tk.TclError, AttributeError):
                        pass
                    continue
                stamp = datetime.now().strftime("%H:%M:%S")
                self.log_box.configure(state="normal")
                self.log_box.insert("end", f"{stamp}  {msg}\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    def _show_candidates(self, ranked: list[dict]) -> None:
        self.cands = list(ranked)
        try:
            for iid in self.ctree.get_children():
                self.ctree.delete(iid)
            auto = App._autopick_like(ranked)
            for i, c in enumerate(ranked):
                mark = "▶" if auto is not None and c is auto else ""
                self.ctree.insert("", "end", iid=str(i), values=(
                    mark, "r/" + c["sub"], c["score"], "%.0fm" % c["age_min"],
                    c["verdict"], c["title"][:70]))
            if auto is not None:
                idx = ranked.index(auto)
                self._prog_select = True
                try:
                    self.ctree.selection_set(str(idx))
                    self.ctree.see(str(idx))
                finally:
                    self._prog_select = False
        except (tk.TclError, AttributeError):
            pass

    @staticmethod
    def _autopick_like(ranked: list[dict]) -> dict | None:
        for tier in ("T1", "T2", "T3"):
            for c in ranked:
                if c.get("tier") == tier:
                    return c
        return None

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    try:
        crash_install()
    except Exception:
        pass
    App().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
