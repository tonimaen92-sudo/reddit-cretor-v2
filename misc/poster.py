# UTF-8
"""GUI: image poster. Post tab (container, title + Generate, images,
LOCK IN) + Batch automation tab (queue, 1 post per container).

Entry is uiopen-first (like the commenter), never Safari search:
staged container open + login probe, then cold
`uiopen --url 'https://www.reddit.com/r/<sub>/'`, dump land-proof,
then the proven Create -> Image -> Recents -> title -> flair -> Post
pieces from reddit_post. Profile posts reuse run_profile_post.
Link posts are NOT built yet (manual link post proves that path first).

  poster.bat
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from tkinter import filedialog, scrolledtext, ttk

MISC_DIR = os.path.dirname(os.path.abspath(__file__))
if MISC_DIR not in sys.path:
    sys.path.insert(0, MISC_DIR)

from config import LOG_DIR  # noqa: E402
from config import ROOT_DIR, SUBREDDITS_DIR  # noqa: E402
from config import USED_DIR  # noqa: E402
from farm_stats import (  # noqa: E402
    failed_poster_today,
    load_marks_posts,
    load_poster_items,
    nyc_today,
    stats_from_marks,
    stats_from_poster,
    today_summary,
)
from reddit_open import close_reddit, open_container, warn_siwa  # noqa: E402
from reddit_post import (  # noqa: E402
    CREATE_LABELS,
    apply_flair,
    archive_used,
    clear_local_photos,
    community_name,
    import_to_camera_roll,
    refuse_join,
    tap_create,
    tap_image_type,
    tap_open_banner,
    tap_recents_photo,
    tap_submit,
    type_title,
)
from reddit_profile_post import run_profile_post  # noqa: E402
from reddit_restore import drawer_login_probe  # noqa: E402 (same as commenter)
from reddit_restore import restore_until_alive  # noqa: E402 (same flow as commenter)
from nsfw_ledger import is_posted as nsfw_is_posted  # noqa: E402
from nsfw_ledger import mark_posted as nsfw_mark_posted  # noqa: E402
from nsfw_ledger import mark_staged as nsfw_mark_staged  # noqa: E402
from nsfw_liveness import check_slug as nsfw_check_slug  # noqa: E402
from nsfw_liveness import get_token as nsfw_get_token  # noqa: E402
from reddit_profile_post import (  # noqa: E402
    confirm_profile_composer,
    dismiss_keyboard,
    tap_avatar,
    tap_create_bottom,
    tap_profile_row,
)
from slots import get_slot, load_slots, retired_ids, slot_reddit_ready  # noqa: E402
from ssh_device import Phone, PhoneError, diagnose_usb, usb_status_line  # noqa: E402
from stay_awake import allow_sleep, stay_awake  # noqa: E402
from title_gen import generate_title  # noqa: E402
from flair_pick import ensure_flair  # noqa: E402
from ui_tree import TapStack, collapsed_text, dump_named  # noqa: E402
from ui_tree import find_continue_gate, find_label_exact  # noqa: E402
from ui_tree import find_update_settings  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

POSTER_QUEUE_PATH = os.path.join(LOG_DIR, "poster_queue.json")
POSTER_LOG_PATH = os.path.join(LOG_DIR, "poster.log")
POSTER_SUBMAP_PATH = os.path.join(LOG_DIR, "poster_sub_map.json")
# Communities whose karma floor our accounts don't meet yet. Refused at
# validate + LOCK IN with a plain message. Expand as floors are found.
KARMA_GATED = {"momsonincest"}


def community_display(sub: str) -> str:
    """Map's stored casing for a sub (exact-case uiopen URLs + logs).
    load_sub_map() lowercases everything, so read the raw file here.
    Falls back to the input. Every comparison downstream casefolds,
    so casing never affects gates. Pure-ish."""
    want = (sub or "").strip().lower()
    if not want:
        return (sub or "").strip()
    try:
        d = json.load(open(POSTER_SUBMAP_PATH, encoding="utf-8"))
        if isinstance(d, dict):
            for _slot, subs in d.items():
                for s in subs or []:
                    if str(s or "").strip() and \
                            str(s or "").strip().lower() == want:
                        return str(s).strip()
    except (OSError, ValueError):
        pass
    return (sub or "").strip()


def load_sub_map() -> dict:
    """{slot: [allowed subs]}. Missing file = no assignments. Pure-ish."""
    try:
        d = json.load(open(POSTER_SUBMAP_PATH, encoding="utf-8"))
        if isinstance(d, dict):
            return {str(k).strip().lower(): [str(s).strip().lower()
                                             for s in (v or [])
                                             if str(s or "").strip()]
                    for k, v in d.items()}
    except (OSError, ValueError):
        pass
    return {}


POOL_EXTS = {".jpg", ".jpeg", ".png"}


def pool_pics(sub: str) -> list:
    """Sorted image paths in subreddits/<sub>/. Pure-ish (dir read)."""
    out: list = []
    try:
        base = os.path.join(SUBREDDITS_DIR,
                            (sub or "").strip().lower())
        for entry in sorted(os.listdir(base)):
            if os.path.splitext(entry)[1].lower() in POOL_EXTS:
                out.append(os.path.join(base, entry))
    except OSError:
        pass
    return out


def used_names(slot: str, sub: str) -> set:
    """Basenames already posted from this pool (used/<slot>/<sub>/)."""
    found: set = set()
    try:
        base = os.path.join(USED_DIR, (slot or "").strip().lower(),
                            (sub or "").strip().lower())
        for root, _dirs, files in os.walk(base):
            for f in files:
                found.add(f.lower())
    except OSError:
        pass
    return found


def available_pics(pool: list, used: set, picked: set) -> list:
    """Pool files not yet posted and not picked this fill. Pure."""
    used_low = {str(u or "").lower() for u in (used or set())}
    picked_low = {str(p or "").lower() for p in (picked or set())}
    return [p for p in (pool or [])
            if os.path.basename(p).lower() not in used_low
            and os.path.basename(p).lower() not in picked_low]


def flair_for(sub: str) -> str:
    """Saved flair for a community (post_settings row). '' when none."""
    want = (sub or "").strip().lower()
    try:
        d = json.load(open(POSTER_COMMUNITIES_PATH, encoding="utf-8"))
        if isinstance(d, dict):
            for item in d.get("communities") or []:
                if str((item or {}).get("community") or "").strip().lower() \
                        == want:
                    return str((item or {}).get("flair") or "")[:64]
    except (OSError, ValueError):
        pass
    return ""


def niche_for(sub: str) -> str:
    """Stored per-sub note/rules for Grok (post_settings row). '' when none."""
    want = (sub or "").strip().lower()
    try:
        d = json.load(open(POSTER_COMMUNITIES_PATH, encoding="utf-8"))
        if isinstance(d, dict):
            for item in d.get("communities") or []:
                if str((item or {}).get("community") or "").strip().lower() \
                        == want:
                    return str((item or {}).get("niche") or "").strip()[:500]
    except (OSError, ValueError):
        pass
    return ""


def save_niche_for(sub: str, text: str, overwrite: bool = False) -> bool:
    """Save per-sub Grok note. Appends when a note exists (no silent loss).
    Returns True when the file changed."""
    want = (sub or "").strip().lower()
    val = (text or "").strip()[:500]
    if not want or not val:
        return False
    try:
        d = json.load(open(POSTER_COMMUNITIES_PATH, encoding="utf-8"))
        rows = (d if isinstance(d, dict) else {}).get("communities") or []
        sel = str((d if isinstance(d, dict) else {}).get("selected") or "")
    except (OSError, ValueError):
        return False
    changed = False
    hit = False
    for item in rows:
        if not isinstance(item, dict):
            continue
        if str(item.get("community") or "").strip().lower() == want:
            hit = True
            cur = str(item.get("niche") or "").strip()
            if not cur or overwrite:
                if cur != val:
                    item["niche"] = val
                    changed = True
            elif val not in cur and len(cur) < 450:
                item["niche"] = (cur + " | " + val)[:500]
                changed = True
    if not hit:
        rows.append({"community": (sub or "").strip(), "niche": val,
                     "flair": "", "flair_required": ""})
        changed = True
    if not changed:
        return False
    try:
        with open(POSTER_COMMUNITIES_PATH, "w", encoding="utf-8") as f:
            json.dump({"community": sel, "selected": sel,
                       "communities": rows}, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except OSError:
        return False
    return True


def extract_title_rules(dump_text: str) -> str:
    """Pull community title rules from composer/about dump text, e.g.
    'This community requires "feet", "feet pics", ... in post titles'.
    Returns a Grok-ready rule line or ''. Pure."""
    import re as _re
    t = _re.sub(r"\s+", " ", str(dump_text or "")).strip()
    if not t:
        return ""
    m = _re.search(r"[Tt]his community requires (.+?) in post titles?", t)
    if not m:
        m = _re.search(r"[Rr]equires (.+?) in (post )?titles?", t)
        if not m:
            return ""
    clause = m.group(1).strip().rstrip(".")
    if len(clause) > 200:
        return ""
    return "Title rule for this sub: must include one of %s in the title." \
        % clause


def record_community_rules(sub: str, dump_text: str, log=print) -> str:
    """Detect title rules from a live dump and persist per-sub. Returns
    the rule line or ''. Never overwrites unrelated notes (appends)."""
    rule = extract_title_rules(dump_text)
    if not rule:
        return ""
    try:
        if save_niche_for(sub, rule):
            log("Rules saved r/%s: %s" % ((sub or "").strip(), rule))
        else:
            log("Rules already known r/%s." % (sub or "").strip())
    except Exception as e:
        log("Rules save note: %s" % e)
    return rule


def sub_holder(sub: str, exclude_sid: str = "") -> str:
    """Container holding a sub (queue + ledger). '' when free."""
    want = (sub or "").strip().lower()
    if not want:
        return ""
    exc = (exclude_sid or "").strip().lower()
    try:
        assigned = set()
        for subs in load_sub_map().values():
            assigned.update(s.strip().lower() for s in subs)
    except Exception:
        assigned = set()
    if want not in assigned:
        return ""
    try:
        from nsfw_ledger import load as _ledger_load
        for _key, row in ((_ledger_load().get("urls") or {}).items()):
            if not isinstance(row, dict):
                continue
            if str(row.get("sub") or "").strip().lower() != want:
                continue
            if str(row.get("status") or "") != "posted":
                continue
            holder = str(row.get("account") or "").strip().lower()
            if holder and holder != exc:
                return holder
    except Exception:
        pass
    try:
        for it in load_queue():
            if str(it.get("community") or "").strip().lower() != want:
                continue
            if str(it.get("status") or "") not in ("queued", "posted"):
                continue
            holder = str(it.get("container") or "").strip().lower()
            if holder and holder != exc:
                return holder
    except Exception:
        pass
    return ""
VERIFY_WALL = ("verify your email", "verification code")
LOGOUT_MARKERS = ("log in to add", "log in", "login", "sign up",
                  "create an account to continue")


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def load_queue() -> list:
    try:
        d = json.load(open(POSTER_QUEUE_PATH, encoding="utf-8"))
        items = d.get("items") if isinstance(d, dict) else []
        return [i for i in items if isinstance(i, dict)]
    except (OSError, ValueError):
        return []


def save_queue(items: list) -> None:
    with open(POSTER_QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _row_stale_posted(it: dict) -> bool:
    """True when a posted row is older than 24h (posted_ts, else ts) and
    should roll off the batch table. Unparseable/missing stamps stay
    visible — never hide what we cannot date. Pure."""
    try:
        if not isinstance(it, dict):
            return False
        if str(it.get("status") or "") != "posted":
            return False
        ts = str(it.get("posted_ts") or it.get("ts") or "")
        day = datetime.fromisoformat(
            ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        return (datetime.now(timezone.utc) - day).total_seconds() > 86400
    except Exception:
        return False


RETRYABLE = ("queued", "failed")


def _retryable(it) -> bool:
    """Batch-eligible rows: queued + failed (failed retries on next click).
    Posted rows never rerun. Pure."""
    return str((it or {}).get("status") or "") in RETRYABLE


POSTER_COMMUNITIES_PATH = os.path.join(LOG_DIR, "post_settings.json")


def load_community_names() -> tuple:
    """(names, selected): shared post_settings + subreddits dirs. Pure-ish."""
    names: list = []
    selected = ""
    try:
        d = json.load(open(POSTER_COMMUNITIES_PATH, encoding="utf-8"))
        if isinstance(d, dict):
            for item in d.get("communities") or []:
                c = str((item or {}).get("community") or "").strip()
                if c and c not in names:
                    names.append(c)
            selected = str(d.get("selected") or "").strip()
    except (OSError, ValueError):
        pass
    try:
        subs = SUBREDDITS_DIR
        for entry in sorted(os.listdir(subs)):
            if entry not in names and os.path.isdir(os.path.join(subs, entry)):
                names.append(entry)
    except OSError:
        pass
    return names, selected


def save_community_names(names: list, selected: str = "") -> None:
    """Persist community list + selection, preserving niche/flair rows."""
    rows: list = []
    try:
        d = json.load(open(POSTER_COMMUNITIES_PATH, encoding="utf-8"))
        if isinstance(d, dict):
            for item in d.get("communities") or []:
                c = str((item or {}).get("community") or "").strip()
                if c:
                    rows.append({"community": c,
                                 "niche": str((item or {}).get("niche") or ""),
                                 "flair": str((item or {}).get("flair") or "")[:64],
                                 "flair_required": str((item or {}).get(
                                     "flair_required") or "").strip().lower()[:16]})
    except (OSError, ValueError):
        rows = []
    have = {r["community"] for r in rows}
    for c in names:
        if c and c not in have:
            rows.append({"community": c, "niche": "", "flair": "",
                         "flair_required": ""})
            have.add(c)
    rows = [r for r in rows if r["community"] in set(names)]
    try:
        with open(POSTER_COMMUNITIES_PATH, "w", encoding="utf-8") as f:
            json.dump({"community": selected, "selected": selected,
                       "communities": rows}, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except OSError:
        pass


def save_flair_for(sub: str, flair: str, overwrite: bool = False) -> None:
    """Cache a Grok-picked flair onto the sub's settings row. Without
    overwrite, manual values are never replaced. Empty flair with
    overwrite=True clears a stale cache entry."""
    want = (sub or "").strip().lower()
    val = (flair or "").strip()[:64]
    if not want:
        return
    try:
        d = json.load(open(POSTER_COMMUNITIES_PATH, encoding="utf-8"))
        rows = (d if isinstance(d, dict) else {}).get("communities") or []
    except (OSError, ValueError):
        return
    hit = False
    for item in rows:
        if not isinstance(item, dict):
            continue
        if str(item.get("community") or "").strip().lower() == want:
            if not str(item.get("flair") or "").strip() or overwrite:
                item["flair"] = val
            hit = True
    if not hit and val:
        rows.append({"community": (sub or "").strip(), "niche": "",
                     "flair": val, "flair_required": ""})
    try:
        sel = ""
        try:
            sel = str((d or {}).get("selected") or "")
        except Exception:
            sel = ""
        with open(POSTER_COMMUNITIES_PATH, "w", encoding="utf-8") as f:
            json.dump({"community": sel, "selected": sel,
                       "communities": rows}, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except OSError:
        pass


def flair_demand_for(sub: str) -> str:
    """Persisted flair demand for a community: 'required', 'optional',
    or '' (unknown, never observed). '' when the row is missing."""
    want = (sub or "").strip().lower()
    try:
        d = json.load(open(POSTER_COMMUNITIES_PATH, encoding="utf-8"))
        if isinstance(d, dict):
            for item in d.get("communities") or []:
                if str((item or {}).get("community") or "").strip().lower() \
                        == want:
                    got = str((item or {}).get("flair_required") or "") \
                        .strip().lower()
                    return got if got in ("required", "optional") else ""
    except (OSError, ValueError):
        pass
    return ""


def save_flair_demand_for(sub: str, demand: str) -> bool:
    """Persist an observed flair demand level. Only 'required'/'optional'
    are stored; anything else is ignored. Never clears a known value
    with unknown. Returns True when the file changed."""
    want = (sub or "").strip().lower()
    demand = (demand or "").strip().lower()
    if not want or demand not in ("required", "optional"):
        return False
    try:
        d = json.load(open(POSTER_COMMUNITIES_PATH, encoding="utf-8"))
        rows = (d if isinstance(d, dict) else {}).get("communities") or []
        sel = str((d if isinstance(d, dict) else {}).get("selected") or "")
    except (OSError, ValueError):
        return False
    changed = False
    hit = False
    for item in rows:
        if not isinstance(item, dict):
            continue
        if str(item.get("community") or "").strip().lower() == want:
            hit = True
            if str(item.get("flair_required") or "").strip().lower() != demand:
                item["flair_required"] = demand
                changed = True
    if not hit:
        rows.append({"community": (sub or "").strip(), "niche": "",
                     "flair": "", "flair_required": demand})
        changed = True
    if not changed:
        return False
    try:
        with open(POSTER_COMMUNITIES_PATH, "w", encoding="utf-8") as f:
            json.dump({"community": sel, "selected": sel,
                       "communities": rows}, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except OSError:
        return False
    return True


def record_community_flair(sub: str, dump_text: str, log=print) -> str:
    """Detect flair demand from a live composer dump and persist per-sub.
    Returns the demand level or ''. Unknown stays unknown, never guessed."""
    demand = flair_demand(dump_text)
    if not demand:
        return ""
    try:
        if save_flair_demand_for(sub, demand):
            log("Flair demand saved r/%s: %s" % ((sub or "").strip(),
                                                 demand))
        else:
            log("Flair demand already known r/%s." % (sub or "").strip())
    except Exception as e:
        log("Flair demand save note: %s" % e)
    return demand


def subreddits_table_rows() -> list:
    """One row per known sub (post_settings communities + subreddits/
    dirs). Posted-first, then alpha. Pure-ish (file reads only).

    Row: {"sub", "posts", "assigned", "flair", "titles"} where flair is
    'required' / 'optional' / 'unknown' plus the saved flair value when
    present, and titles is 'yes: <first 60>' / 'no' from the niche note.
    """
    display: dict = {}
    try:
        d = json.load(open(POSTER_COMMUNITIES_PATH, encoding="utf-8"))
        if isinstance(d, dict):
            for item in d.get("communities") or []:
                c = str((item or {}).get("community") or "").strip()
                if c and c.lower() not in display:
                    display[c.lower()] = c
    except (OSError, ValueError):
        pass
    try:
        subs = SUBREDDITS_DIR
        for entry in sorted(os.listdir(subs)):
            if os.path.isdir(os.path.join(subs, entry)):
                if entry.lower() not in display:
                    display[entry.lower()] = entry
    except OSError:
        pass
    counts: dict = {}
    try:
        for it in load_queue():
            if not isinstance(it, dict):
                continue
            if str(it.get("status") or "") != "posted":
                continue
            key = str(it.get("community") or "").strip().lower()
            if key:
                counts[key] = counts.get(key, 0) + 1
    except Exception:
        pass
    holders: dict = {}
    try:
        for slot, subs_list in load_sub_map().items():
            for s in subs_list or []:
                key = str(s or "").strip().lower()
                if key and key not in holders:
                    holders[key] = str(slot or "").strip().lower()
    except Exception:
        pass
    rows = []
    for key, name in display.items():
        demand = flair_demand_for(name)
        saved = flair_for(name)
        flair_txt = demand if demand else "unknown"
        if saved.strip():
            flair_txt += " (%s)" % saved.strip()[:64]
        niche = niche_for(name)
        titles = ("yes: %s" % niche[:60]) if niche.strip() else "no"
        rows.append({"sub": name, "posts": counts.get(key, 0),
                     "assigned": holders.get(key, ""),
                     "flair": flair_txt, "titles": titles})
    rows.sort(key=lambda r: (-r["posts"], str(r["sub"] or "").lower()))
    return rows


PERMANENT_FAIL_NOTES = (
    "already posted", "ledger", "assignment", "karma", "exclusive",
    "held by",
)


def retryable_fail(note: str) -> bool:
    """A failed queue note worth retrying: anything transient. Permanent
    failures (posted elsewhere, ledger, assignment) stay buried. Pure."""
    low = (note or "").lower()
    return not any(p in low for p in PERMANENT_FAIL_NOTES)


def reset_retryable(items: list) -> int:
    """Flip transient-failed items back to queued. Returns reset count."""
    n = 0
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if str(it.get("status") or "") != "failed":
            continue
        if not retryable_fail(str(it.get("note") or "")):
            continue
        it["status"] = "queued"
        it.pop("failed_ts", None)
        it["note"] = "auto-retry"
        n += 1
    return n


def login_probe(stack: TapStack, phone: Phone, lg, sid: str,
                username: str) -> str:
    """Drawer probe: same tech as the commenter (shared drawer_login_probe:
    wrap-collapsed markers + header/community structure). Username presence
    is bonus confirmation only, never a fail reason. Never guesses."""
    reason = drawer_login_probe(stack, phone, lg, sid, "pp")
    if reason:
        return reason
    if (username or "").strip():
        lg("Drawer alive for u/%s." % username)
    return ""


def open_container_rotated(phone, stack, lg, name: str, email: str,
                           stop=None, rotate: bool = True) -> None:
    """Airplane IP rotation on the live connection (container switches
    only), then container open. Fail-open to the old IP. Raises
    PhoneError on stop."""
    if rotate:
        try:
            from airplane import cycle_on
            ok, detail = cycle_on(phone, stack, lg, stop=stop)
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, str(e)[:100])
        lg("IP rotation %s (%s)." % ("OK" if ok else "FAILED, staying on old IP",
                                     detail))
    else:
        lg("IP kept (same container).")
    if stop is not None and stop.is_set():
        raise PhoneError("stopped")
    open_container(stack, phone, lg, name, email)


def ensure_login(stack: TapStack, phone: Phone, lg, sid: str, stem: str,
                 stop=None) -> str:
    """Full login chain (same as commenter): drawer probe -> ACCOUNTS box
    switch -> Crane restore -> post-restore probe -> ACCOUNTS box again.
    '' when the session verifies alive, else the reason."""
    slot = get_slot(sid)
    username = str((slot or {}).get("username") or "").strip()
    reason = login_probe(stack, phone, lg, sid, username)
    if not reason:
        return ""
    if reason == "inconclusive drawer":
        return reason  # never restore on inconclusive
    if "verify" in reason or "verification" in reason:
        return reason  # verify wall is not a logout, never restore over it
    lg("Login dead (%s), full chain on %s." % (reason, sid))
    return restore_until_alive(stack, phone, lg, sid, stem, stop=stop)

def community_landed(blob, nodes, community: str) -> bool:
    """Land-proof: r/<sub> header AND Create in the same dump. Create alone
    proves nothing (bottom-bar Create is on every screen). Long headers
    ellipsize (r/RateMyTitsOutOf...), so names over 14 chars also pass on
    an exact r/<first-14> prefix. Pure."""
    if find_label_exact(nodes, CREATE_LABELS) is None:
        return False
    low = (blob or "").casefold().replace(" ", "")
    name = (community or "").casefold()
    if ("r/" + name) in low:
        return True
    return len(name) > 14 and ("r/" + name[:14]) in low


def flair_demand(blob) -> str:
    """Flair demand level from composer text: 'required', 'optional',
    or ''. 'add flair (required)' is the hard gate. Pure."""
    import re as _re
    low = _re.sub(r"[^a-z ]+", " ", (blob or "").lower())
    low = _re.sub(r"\s+", " ", low)
    if "flair required" in low:
        return "required"
    if "add flair" in low:
        return "optional"
    return ""


def tap_create_safe(stack, phone, lg, nodes, stem: str = "pp_create"):
    """Create tap constrained above the home indicator (y<=1320): the old
    max-y pick could land on the indicator and Home out mid-flow. Then
    proves the composer opened (title field present). Returns nodes."""
    cands = [n for n in nodes or []
             if n.ntype != "ocr-line" and n.on_screen()
             and collapsed_text(n.labels()) == "create" and n.y <= 1320]
    if not cands:
        raise PhoneError("Create not above home indicator. See %s." % stem)
    pick = max(cands, key=lambda n: n.y)
    lg("TAP Create %s" % (pick.center(),))
    stack.tap_xy(*pick.center())
    time.sleep(1.5)
    _blob, nodes = dump_named(stack, phone, stem, lg, shot=True)
    tit = find_label_exact(nodes, ("Add a title", "Title",
                                   "An interesting title"))
    if tit is None:
        raise PhoneError("Composer did not open after Create. See %s."
                         % stem)
    lg("Composer open, title field present.")
    return nodes


def uiopen_community(stack: TapStack, phone: Phone, lg,
                     community: str) -> str:
    """Cold uiopen dispatch from the iPhone home screen. Dump land-proof
    (r/<sub> header + Create), then the caller taps Create. A missed
    landing retries once from Home. Nothing sends on ambiguity."""
    url = "https://www.reddit.com/r/" + community + "/"
    lg("ENTRY uiopen from Home " + url)
    stack.press_home()
    time.sleep(0.7)
    code, out = phone.exec_ok("uiopen --url '%s'" % url, timeout=15)
    if code != 0:
        return "uiopen dispatch failed (%s)" % (out or "").strip()[:80]
    time.sleep(3.0)
    ok, reason = _uiopen_attempt(stack, phone, lg, url, community, "pp_entry")
    if ok:
        lg("COMMUNITY_OPEN " + community)
        return ""
    lg("ENTRY missed (%s), Home + one re-dispatch." % reason)
    stack.press_home()
    time.sleep(1.0)
    code, out = phone.exec_ok("uiopen --url '%s'" % url, timeout=15)
    if code != 0:
        return "not on community with Create (re-dispatch failed)"
    time.sleep(3.0)
    ok, reason = _uiopen_attempt(stack, phone, lg, url, community,
                                 "pp_entryB")
    if ok:
        lg("COMMUNITY_OPEN " + community + " (retry)")
        return ""
    return "not on community with Create (%s)" % reason


def _uiopen_attempt(stack, phone, lg, url: str, community: str,
                    stem: str) -> tuple:
    """One dispatch landing attempt: gates, banners, land-proof.
    Returns (True, '') or (False, reason). Pure flow, no Home presses."""
    toggled = False
    for _round in range(5):
        blob, nodes = dump_named(stack, phone, "%s%d" % (stem, _round), lg,
                                 shot=True)
        warn_siwa(nodes, lg)
        refuse_join(nodes, lg)
        low = (blob or "").lower()
        gate = find_continue_gate(nodes)
        if gate is not None and ("nsfw" in low or "not safe for work" in low
                                 or "over 18" in low):
            lg("TAP Continue (NSFW gate, round %d)" % (_round + 1))
            stack.tap_xy(*gate.center())
            time.sleep(2.0)
            continue
        upd = find_update_settings(nodes)
        if upd is not None and ("nsfw" in low or "not safe for work" in low
                                or "over 18" in low):
            if toggled:
                return False, "NSFW gate persists after toggle"
            lg("TAP Update Settings (NSFW gate, round %d)" % (_round + 1))
            stack.tap_xy(*upd.center())
            time.sleep(2.0)
            blob, nodes = dump_named(stack, phone, "pp_settings", lg,
                                     shot=True)
            row = find_label_exact(
                nodes, ("Show NSFW Content (18+)", "Show NSFW Content"))
            if row is None:
                return False, "NSFW switch row missing"
            stack.tap_xy(615, row.center()[1])
            time.sleep(1.5)
            lg("NSFW switch tapped, uiopen again")
            code, out = phone.exec_ok("uiopen --url '%s'" % url, timeout=15)
            if code != 0:
                return False, "uiopen re-dispatch failed"
            time.sleep(3.0)
            toggled = True
            continue
        op = tap_open_banner(nodes)
        if op is not None:
            lg("TAP OPEN")
            stack.tap_xy(*op.center())
            time.sleep(2.0)
            continue
        break
    blob, nodes = dump_named(stack, phone, "pp_community", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    low = (blob or "").lower()
    gate = find_continue_gate(nodes)
    if gate is not None and ("nsfw" in low or "not safe for work" in low
                             or "over 18" in low):
        lg("TAP Continue (NSFW gate, final)")
        stack.tap_xy(*gate.center())
        time.sleep(2.0)
        blob, nodes = dump_named(stack, phone, "pp_community_gate", lg,
                                 shot=True)
        refuse_join(nodes, lg)
    if not community_landed(blob, nodes, community):
        return False, "no header+Create"
    return True, ""


def discard_composer(stack: TapStack, phone: Phone, lg) -> None:
    """Close the composer via X -> Discard (never Save draft). Raises
    PhoneError with the phone left alone when anything is ambiguous."""
    blob, nodes = dump_named(stack, phone, "pp_dry_x", lg, shot=False)
    xbtn = None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) == "x" and n.y < 200 and n.x < 200:
            xbtn = n
            break
    if xbtn is None:
        raise PhoneError("Composer X missing, left open (discard manually).")
    stack.tap_xy(*xbtn.center())
    time.sleep(1.5)
    blob, nodes = dump_named(stack, phone, "pp_dry_confirm", lg, shot=False)
    d = None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) in ("discard", "discard post",
                                           "delete draft"):
            d = n
            break
    if d is None:
        raise PhoneError("Discard confirm missing, left open (discard manually).")
    stack.tap_xy(*d.center())
    time.sleep(1.5)
    blob, nodes = dump_named(stack, phone, "pp_dry_gone", lg, shot=True)
    if find_label_exact(nodes, ("Add a title", "Title")) is not None:
        raise PhoneError("Composer still up after discard.")
    stack.press_home()


def run_community_item(phone: Phone, stack: TapStack, lg, item: dict,
                       stop, fresh: bool = True,
                       staged: bool = False,
                       dry: bool = False,
                       linger: bool = False) -> str:
    """One image post to a community. '' = submitted. Raises PhoneError.
    fresh=False skips IP rotation + login chain (same container back to
    back, session already proven). staged=True skips open + login
    entirely (session already open on-device, FIRE no-login). dry=True
    runs everything except the Post tap, then discards the composer:
    returns 'dry-tested', archives nothing, marks nothing. linger=True
    is accepted (batch passes it) but the pause itself lives with the
    caller AFTER the queue mark, so a crash mid-break can never
    double-post: by the time we return, the batch has already saved
    posted + archived the file."""
    name = str(item.get("container") or "").strip()
    community = community_display(community_name(str(item.get("community") or "")))
    title = str(item.get("title") or "").strip()
    images = [p for p in (item.get("images") or []) if str(p or "").strip()]
    flair = str(item.get("flair") or "").strip()[:64]
    if not name or not title or not images:
        raise PhoneError("Queue item missing container/title/image.")
    image = images[0]
    if len(images) > 1:
        lg("Community post takes one image; using %s." %
           os.path.basename(images[0]))
    slot = get_slot(name)
    if not slot or not slot.get("ghost"):
        raise PhoneError(f"{name} has no Ghost/Crane container.")
    if not slot.get("email"):
        raise PhoneError(f"{name} has no Hide My Email.")
    if not slot_reddit_ready(slot):
        raise PhoneError(f"{name} does not have a completed Reddit account.")
    email = str(slot.get("email") or "")
    username = str(slot.get("username") or "").strip()
    lg(f"POST container={name} community={community} title_len={len(title)}")
    if stop is not None and stop.is_set():
        return "stopped"
    import_to_camera_roll(stack, image, lg)
    if stop is not None and stop.is_set():
        return "stopped"
    blob, nodes = dump_named(stack, phone, "pp_now", lg, shot=True)
    warn_siwa(nodes, lg)
    low = (blob or "").lower()
    if any(w in low for w in VERIFY_WALL):
        raise PhoneError("Reddit is on Verify. Will not post. Will not close.")
    close_reddit(stack, phone, lg)
    if stop is not None and stop.is_set():
        return "stopped"
    if staged:
        lg("Staged session, skipping open + login.")
    else:
        open_container_rotated(phone, stack, lg, name, email, stop=stop,
                               rotate=fresh)
        if stop is not None and stop.is_set():
            return "stopped"
    if dry:
        lg("Dry run, skipping login chain.")
        reason = ""
    elif fresh and not staged:
        reason = ensure_login(stack, phone, lg, name, "pp_login", stop=stop)
        if reason:
            raise PhoneError("Login chain failed: " + reason)
    else:
        lg("Login proven this container, skipping chain.")
        reason = ""
    if stop is not None and stop.is_set():
        return "stopped"
    reason = uiopen_community(stack, phone, lg, community)
    if reason:
        raise PhoneError("Entry failed: " + reason)
    blob, nodes = dump_named(stack, phone, "pp_feed", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    if find_label_exact(nodes, CREATE_LABELS) is None:
        raise PhoneError("Not on the community with Create. See pp_feed.")
    if stop is not None and stop.is_set():
        return "stopped"
    nodes = tap_create_safe(stack, phone, lg, nodes, stem="pp_create")
    nodes = tap_image_type(stack, phone, lg, nodes)
    nodes = tap_recents_photo(stack, phone, lg, nodes)
    nodes = type_title(stack, phone, lg, nodes, title)
    blob, _n = dump_named(stack, phone, "pp_flair_check", lg, shot=False)
    try:
        record_community_rules(community, blob, lg)
    except Exception as e:
        lg("Rules save note: %s" % e)
    try:
        record_community_flair(community, blob, lg)
    except Exception as e:
        lg("Flair demand save note: %s" % e)
    demand = flair_demand(blob)
    if demand == "required" and not flair:
        lg("Flair REQUIRED, no saved flair: Grok-pick from sheet.")
        nodes = ensure_flair(stack, phone, lg, nodes, community,
                             save_fn=save_flair_for)
    elif demand == "optional" and not flair:
        lg("Flair optional, none saved: skipping.")
    else:
        try:
            nodes = apply_flair(stack, phone, lg, nodes, flair)
        except PhoneError as e:
            if demand == "required" and item.get("flair_auto") \
                    and "not on sheet" in str(e):
                lg("Saved flair stale, re-asking Grok.")
                save_flair_for(community, "", overwrite=True)
                nodes = ensure_flair(stack, phone, lg, nodes, community,
                                     save_fn=save_flair_for)
            else:
                raise
    nodes = tap_submit(stack, phone, lg, nodes)
    low_after = " ".join(
        collapsed_text(n.labels()) for n in nodes or []
        if n.ntype != "ocr-line" and n.on_screen()).lower()
    if "flair" in low_after and (
            "requir" in low_after or "select" in low_after
            or "add flair" in low_after):
        lg("Post revealed flair demand, Grok-picking.")
        nodes = ensure_flair(stack, phone, lg, nodes, community,
                             save_fn=save_flair_for)
        if dry:
            lg("DRY STOP before Post (flair picked).")
            discard_composer(stack, phone, lg)
            return "dry-tested"
        nodes = tap_submit(stack, phone, lg, nodes)
    if dry:
        lg("DRY STOP before Post.")
        discard_composer(stack, phone, lg)
        return "dry-tested"
    archive_used(image, name, community, lg)
    try:
        clear_local_photos(stack, lg)
    except PhoneError as e:
        lg("Wipe after Post failed: " + str(e))
    stack.press_home()
    lg(f"POST_OK {name} {community}")
    return ""


def home_linger(stack, phone, lg, stop, lo_s: float = 60.0,
                 hi_s: float = 180.0) -> None:
    """Breather on the iPhone Home screen: press Home after the post,
    then a fresh random 60-180s pause with Reddit out of sight (no
    accidental taps, no feed drift). Stop-aware (already parked, just
    returns so the batch halts with the post already marked)."""
    import random as _random
    stack.press_home()
    time.sleep(1.5)
    dump_named(stack, phone, "pp_iphone_home", lg, shot=False)
    mins = round(lo_s + _random.Random().random() * (hi_s - lo_s), 1)
    lg("iPhone Home break %.1f min." % (mins / 60.0))
    end = time.monotonic() + mins * 60.0
    next_beat = time.monotonic() + 30.0
    while time.monotonic() < end:
        if stop is not None and stop.is_set():
            lg("Break cut short by Stop, post already landed.")
            return
        left = end - time.monotonic()
        if left <= 0:
            break
        # heartbeat: a silent break is indistinguishable from a dead
        # worker, so narrate the wait every ~30s.
        if time.monotonic() >= next_beat:
            lg("Break %.0fs left." % left)
            next_beat = time.monotonic() + 30.0
        time.sleep(min(5.0, left))
    lg("Break over, resuming batch.")


def link_slug(url: str) -> str:
    """Slug from a redgifs watch URL. '' when unparseable. Pure."""
    u = (url or "").strip()
    if "/watch/" not in u.lower():
        return ""
    return u.split("/watch/")[-1].split("?")[0].strip("/").lower()


def generate_link_title(url: str, log=print) -> str:
    """Background 5-frames -> Grok caption for a link URL (same flow as
    nsfw_frames single clip). Capitalized first letter. '' on failure."""
    import base64
    import shutil
    import subprocess
    import urllib.request
    slug = link_slug(url)
    if not slug:
        log("Link title: unparseable URL.")
        return ""
    try:
        from comment_llm import (  # noqa
            XAI_FAST_MODEL, load_xai_key, xai_chat)
        from nsfw_frames import CAPTION_RULES, PCTS, STAGE
        from nsfw_liveness import _get as _rget, API as _RAPI
    except Exception as e:
        log("Link title ERROR imports: %s" % e)
        return ""
    key = load_xai_key()
    if not key:
        log("Link title: no xai key.")
        return ""
    tag = "linktitle"
    try:
        token = _rget(_RAPI + "/auth/temporary").get("token", "")
        g = (_rget("%s/gifs/%s" % (_RAPI, slug), token).get("gif") or {})
        urls = g.get("urls") or {}
        mp4 = urls.get("hd") or urls.get("sd") or ""
        if not mp4.lower().endswith(".mp4"):
            log("Link title: no mp4 for slug.")
            return ""
        os.makedirs(STAGE, exist_ok=True)
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
            log("Link title describe FAIL %s" % (err or "")[:100])
            return ""
        cap, cerr = xai_chat(key, XAI_FAST_MODEL, [{
            "role": "user", "content": (
                "%s\n\nClip facts:\n%s" % (CAPTION_RULES, desc.strip()[:1500]))}],
            max_tokens=60, temperature=0.9, timeout=120, reasoning=False)
        if not cap:
            log("Link title caption FAIL %s" % (cerr or "")[:100])
            return ""
        title = cap.strip().strip('"').strip(".")[:120]
        if title:
            title = title[0].upper() + title[1:]
        log("Link title: %s" % title)
        return title
    except Exception as e:
        log("Link title ERROR %s: %s" % (type(e).__name__, str(e)[:120]))
        return ""
    finally:
        try:
            for f in os.listdir(STAGE):
                if f.startswith(tag + ".") or f.startswith(tag + "_"):
                    try:
                        os.remove(os.path.join(STAGE, f))
                    except OSError:
                        pass
        except OSError:
            pass


def run_link_item(phone: Phone, stack: TapStack, lg, item: dict,
                  stop, fresh: bool = True,
                  staged: bool = False) -> str:
    """One hotlink post to a community (proven 6-step flow). '' = posted.
    Ledger-gated (one URL once ever) + liveness-checked at post time.
    fresh=False skips rotation + login chain. staged=True skips open +
    login entirely (FIRE no-login)."""
    name = str(item.get("container") or "").strip()
    community = community_display(community_name(str(item.get("community") or "")))
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    slug = link_slug(url)
    if not name or not title or not url or not slug:
        raise PhoneError("Queue link item missing container/title/URL.")
    if nsfw_is_posted(url):
        raise PhoneError("URL already posted once, skipping (ledger).")
    token = nsfw_get_token()
    if not token:
        raise PhoneError("Liveness: no redgifs token.")
    verdict = nsfw_check_slug(slug, token)
    if not verdict.get("live"):
        raise PhoneError("URL gone at post time (%s), next URL."
                         % verdict.get("status"))
    slot = get_slot(name)
    if not slot or not slot.get("ghost"):
        raise PhoneError(f"{name} has no Ghost/Crane container.")
    if not slot.get("email"):
        raise PhoneError(f"{name} has no Hide My Email.")
    if not slot_reddit_ready(slot):
        raise PhoneError(f"{name} does not have a completed Reddit account.")
    email = str(slot.get("email") or "")
    lg(f"LINK container={name} community={community} slug={slug[:20]}")
    if stop is not None and stop.is_set():
        return "stopped"
    blob, nodes = dump_named(stack, phone, "pp_now", lg, shot=True)
    warn_siwa(nodes, lg)
    low = (blob or "").lower()
    if any(w in low for w in VERIFY_WALL):
        raise PhoneError("Reddit is on Verify. Will not post. Will not close.")
    close_reddit(stack, phone, lg)
    if stop is not None and stop.is_set():
        return "stopped"
    if not staged:
        open_container_rotated(phone, stack, lg, name, email, stop=stop,
                               rotate=fresh)
        if stop is not None and stop.is_set():
            return "stopped"
    if staged:
        lg("Staged session, skipping open.")
    if fresh and not staged:
        reason = ensure_login(stack, phone, lg, name, "pp_login", stop=stop)
        if reason:
            raise PhoneError("Login chain failed: " + reason)
    else:
        lg("Login proven this container, skipping chain.")
        reason = ""
    if stop is not None and stop.is_set():
        return "stopped"
    reason = uiopen_community(stack, phone, lg, community)
    if reason:
        raise PhoneError("Entry failed: " + reason)
    blob, nodes = dump_named(stack, phone, "pp_feed", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    if find_label_exact(nodes, CREATE_LABELS) is None:
        raise PhoneError("Not on the community with Create. See pp_feed.")
    if stop is not None and stop.is_set():
        return "stopped"
    nodes = tap_create(stack, phone, lg, nodes)
    blob, nodes = dump_named(stack, phone, "pp_link_comp", lg, shot=False)
    tit = find_label_exact(nodes, ("Add a title", "Title",
                                   "An interesting title"))
    if tit is None:
        raise PhoneError("Title field not on composer. See pp_link_comp.")
    stack.tap_xy(*tit.center())
    time.sleep(0.35)
    stack.type_human(title, ms=95)
    time.sleep(0.8)
    blob, nodes = dump_named(stack, phone, "pp_link_typed", lg, shot=True)
    cands = [n for n in nodes or []
             if n.ntype != "ocr-line" and n.on_screen()
             and 700 <= n.y <= 900 and 20 <= n.x <= 120]
    if not cands:
        raise PhoneError("Link (chain) icon not in composer. See dump.")
    pick = min(cands, key=lambda n: n.x)
    stack.tap_xy(*pick.center())
    time.sleep(1.2)
    blob, nodes = dump_named(stack, phone, "pp_link_url", lg, shot=False)
    stack.type_text_block(url)
    time.sleep(1.0)
    blob, nodes = dump_named(stack, phone, "pp_link_sent", lg, shot=True)
    if slug not in (blob or "").lower():
        raise PhoneError("URL unverified in composer field. Did not post.")
    lg("URL verified in field.")
    if stop is not None and stop.is_set():
        return "stopped"
    post = None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) != "post":
            continue
        if n.y < 160:
            post = n
            break
    if post is None:
        post = find_label_exact(nodes, ("Post",))
    if post is None:
        raise PhoneError("Post button not on composer. See pp_link_sent.")
    stack.tap_xy(*post.center())
    time.sleep(4.0)
    blob, nodes = dump_named(stack, phone, "pp_link_after", lg, shot=True)
    still = find_label_exact(nodes, ("Add a title", "Title"))
    if still is not None:
        raise PhoneError("Composer still up after Post. NOT confirmed.")
    nsfw_mark_posted(url, name)
    stack.press_home()
    lg(f"LINK_OK {name} {community} {slug[:20]}")
    return ""


class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("iPhone Reddit poster (image)")
        self.root.geometry("980x720")
        self.root.minsize(820, 600)
        self.log_q: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.phone: Phone | None = None
        self.post_images: list[str] = []
        self._post_container_ids: dict[str, str] = {}

        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="USB: probing…")
        ttk.Label(frm, textvariable=self.status,
                  wraplength=920).pack(fill="x", padx=8, pady=4)

        row = ttk.Frame(frm)
        row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text="SSH password (not git):").pack(side="left")
        self.pw = ttk.Entry(row, show="*", width=24)
        self.pw.pack(side="left", padx=6)
        ttk.Button(row, text="Refresh containers",
                   command=self.on_refresh).pack(side="left", padx=6)
        self.prep_btn = ttk.Button(row, text="PREP selected",
                                   command=self.on_prep)
        self.prep_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(row, text="Stop", command=self.on_stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=6)

        nb = ttk.Notebook(frm)
        nb.pack(fill="both", expand=True, padx=8, pady=4)
        self.nb = nb
        post = ttk.Frame(nb)
        batch = ttk.Frame(nb)
        test = ttk.Frame(nb)
        logtab = ttk.Frame(nb)
        nb.add(post, text="Post")
        nb.add(batch, text="Batch automation")
        nb.add(test, text="Test workflow")
        nb.add(logtab, text="Log")

        grid = ttk.Frame(post)
        grid.pack(fill="x", padx=8, pady=8)
        ttk.Label(grid, text="Container").grid(row=0, column=0, sticky="w",
                                               pady=3)
        self.post_container = ttk.Combobox(grid, width=40, state="readonly")
        self.post_container.grid(row=0, column=1, sticky="w", padx=8, pady=3)
        self.post_container.bind("<<ComboboxSelected>>",
                                 lambda _e: self._refresh_community_choices())
        ttk.Label(grid, text="Post to").grid(row=0, column=2, sticky="e",
                                             padx=(16, 4), pady=3)
        self.post_dest = tk.StringVar(value="community")
        ttk.Radiobutton(grid, text="Community", variable=self.post_dest,
                        value="community",
                        command=self._on_dest_change).grid(row=0, column=3,
                                                          sticky="w", pady=3)
        ttk.Radiobutton(grid, text="My Profile", variable=self.post_dest,
                        value="profile",
                        command=self._on_dest_change).grid(row=0, column=4,
                                                          sticky="w", pady=3)
        ttk.Label(grid, text="Type").grid(row=0, column=5, sticky="e",
                                             padx=(16, 4), pady=3)
        self.post_kind = tk.StringVar(value="image")
        ttk.Radiobutton(grid, text="Image", variable=self.post_kind,
                        value="image",
                        command=self._on_kind_change).grid(row=0, column=6,
                                                          sticky="w", pady=3)
        self.link_radio = ttk.Radiobutton(grid, text="Link",
                                          variable=self.post_kind,
                                          value="link",
                                          command=self._on_kind_change,
                                          state="disabled")
        self.link_radio.grid(row=0, column=7, sticky="w", pady=3)
        ttk.Label(grid, text="Community").grid(row=1, column=0, sticky="w",
                                               pady=3)
        self.post_community = ttk.Combobox(grid, width=26)
        self.post_community.grid(row=1, column=1, sticky="w", padx=8, pady=3)
        self.comm_add_btn = ttk.Button(grid, text="Add",
                                       command=self.on_community_add)
        self.comm_add_btn.grid(row=1, column=2, sticky="w", pady=3)
        self.comm_rm_btn = ttk.Button(grid, text="Remove",
                                      command=self.on_community_remove)
        self.comm_rm_btn.grid(row=1, column=3, sticky="w", pady=3)
        self.comm_hint = tk.StringVar(value="")
        ttk.Label(grid, textvariable=self.comm_hint).grid(
            row=1, column=4, columnspan=4, sticky="w", padx=8, pady=3)
        self.post_community.bind("<<ComboboxSelected>>",
                                 lambda _e: self._save_community_sel())
        self.post_community.bind("<FocusOut>",
                                 lambda _e: self._save_community_sel())
        ttk.Label(grid, text="Niche / note").grid(row=2, column=0, sticky="w",
                                                  pady=3)
        self.post_niche = ttk.Entry(grid, width=28)
        self.post_niche.grid(row=2, column=1, sticky="w", padx=8, pady=3)
        ttk.Label(grid, text="Title").grid(row=3, column=0, sticky="w", pady=3)
        self.post_title = ttk.Entry(grid, width=52)
        self.post_title.grid(row=3, column=1, sticky="w", padx=8, pady=3)
        self.gen_btn = ttk.Button(grid, text="Generate",
                                  command=self.on_generate_title)
        self.gen_btn.grid(row=3, column=2, sticky="w", pady=3)
        ttk.Label(grid, text="Flair").grid(row=4, column=0, sticky="w", pady=3)
        self.post_flair = ttk.Entry(grid, width=28)
        self.post_flair.grid(row=4, column=1, sticky="w", padx=8, pady=3)
        ttk.Label(grid, text="Image(s)").grid(row=5, column=0, sticky="w",
                                              pady=3)
        img_row = ttk.Frame(grid)
        img_row.grid(row=5, column=1, sticky="we", padx=8, pady=3)
        self.post_image = tk.StringVar(value="")
        ttk.Entry(img_row, textvariable=self.post_image,
                  width=48).pack(side="left")
        ttk.Button(img_row, text="Browse…",
                   command=self.on_browse_image).pack(side="left", padx=6)
        ttk.Label(grid, text="Link URL").grid(row=6, column=0, sticky="w",
                                              pady=3)
        self.post_url = ttk.Entry(grid, width=52)
        self.post_url.grid(row=6, column=1, sticky="w", padx=8, pady=3)
        self.fetch_btn = ttk.Button(grid, text="Fetch title",
                                    command=self.on_fetch_link_title)
        self.fetch_btn.grid(row=6, column=2, sticky="w", pady=3)
        btn_row = ttk.Frame(post)
        btn_row.pack(fill="x", padx=8, pady=4)
        self.post_btn = ttk.Button(btn_row, text="POST",
                                   command=self.on_post_single)
        self.post_btn.pack(side="left")
        self.lock_btn = ttk.Button(btn_row, text="LOCK IN",
                                   command=self.on_lock_in)
        self.lock_btn.pack(side="left", padx=8)
        ttk.Label(post, wraplength=900,
                  text="Generate fills the title (same Grok prompts as the "
                  "creator; re-click = new title). LOCK IN queues the "
                  "post for the Batch tab. Image posts only; link path "
                  "kept in code but switched off.").pack(
                      fill="x", padx=8, pady=4)

        srow = ttk.Frame(batch)
        srow.pack(fill="x", padx=8, pady=8)
        self.batch_btn = ttk.Button(srow, text="Start batch",
                                    command=self.on_batch)
        self.batch_btn.pack(side="left")
        self.prep_btn2 = ttk.Button(srow, text="Prepare posts",
                                    command=self.on_prepare_posts)
        self.prep_btn2.pack(side="left", padx=6)
        ttk.Button(srow, text="Remove selected",
                   command=self.on_queue_remove).pack(side="left", padx=6)
        ttk.Button(srow, text="Open pic",
                   command=self.on_queue_open_pic).pack(side="left", padx=6)
        ttk.Button(srow, text="FIRE no-login",
                   command=self.on_fire_selected).pack(side="left", padx=6)
        ttk.Button(srow, text="FIRE login",
                   command=self.on_fire_login).pack(side="left", padx=6)
        ttk.Button(srow, text="Test batch",
                   command=self.on_test_batch).pack(side="left", padx=6)
        ttk.Button(srow, text="Clear posted",
                   command=self.on_queue_clear_posted).pack(side="left")
        qcols = ("container", "dest", "community", "title", "images",
                 "status")
        self.qtree = ttk.Treeview(batch, columns=qcols, show="headings",
                                  selectmode="browse", height=12)
        for c, w in (("container", 80), ("dest", 80), ("community", 140),
                     ("title", 300), ("images", 70), ("status", 90)):
            self.qtree.heading(c, text=c.capitalize())
            self.qtree.column(c, width=w, anchor="w")
        self.qtree.pack(fill="both", expand=True, padx=8, pady=4)
        ttk.Label(batch, wraplength=900,
                  text="Batch: 1 post per queued item down the list, next "
                  "container after each post. Supervised.").pack(
                      fill="x", padx=8, pady=4)

        stats = ttk.Frame(nb)
        nb.add(stats, text="Stats")
        strow = ttk.Frame(stats)
        strow.pack(fill="x", padx=8, pady=8)
        ttk.Button(strow, text="Refresh stats",
                   command=self.on_stats_refresh).pack(side="left")
        self.stats_var = tk.StringVar(value="Today: —")
        ttk.Label(strow, textvariable=self.stats_var,
                  wraplength=700).pack(side="left", padx=8)
        stcols = ("slot", "comments", "ctoday", "posts", "ptoday",
                  "ftoday", "last")
        self.stree = ttk.Treeview(stats, columns=stcols, show="headings",
                                  selectmode="browse", height=12)
        for c, w in (("slot", 80), ("comments", 80), ("ctoday", 80),
                     ("posts", 70), ("ptoday", 70), ("ftoday", 70),
                     ("last", 320)):
            self.stree.heading(c, text={"slot": "Slot", "comments": "Comments",
                                        "ctoday": "C today", "posts": "Posts",
                                        "ptoday": "P today",
                                        "ftoday": "F today",
                                        "last": "Last activity"}.get(c, c))
            self.stree.column(c, width=w, anchor="w")
        self.stree.pack(fill="both", expand=True, padx=8, pady=4)
        rmrow = ttk.Frame(stats)
        rmrow.pack(fill="x", padx=8, pady=4)
        ttk.Button(rmrow, text="Remove slot",
                   command=self.on_stats_remove).pack(side="left")
        ttk.Button(rmrow, text="Open profile",
                   command=self.on_stats_open_profile).pack(side="left",
                                                            padx=6)
        ttk.Label(rmrow, wraplength=700,
                  text="Retires the selected slot from the UI. "
                  "Reversible via retired.json.").pack(side="left", padx=8)

        subs = ttk.Frame(nb)
        nb.add(subs, text="Subreddits")
        surow = ttk.Frame(subs)
        surow.pack(fill="x", padx=8, pady=8)
        ttk.Button(surow, text="Refresh subreddits",
                   command=self.on_subreddits_refresh).pack(side="left")
        ttk.Label(surow, wraplength=700,
                  text="One row per known sub (settings + pic pools, "
                  "posted-first). Flair shows observed demand + saved "
                  "value; unobserved stays unknown.").pack(side="left",
                                                           padx=8)
        sucols = ("sub", "posts", "assigned", "flair", "titles")
        self.sutree = ttk.Treeview(subs, columns=sucols, show="headings",
                                   selectmode="browse", height=12)
        for c, w in (("sub", 170), ("posts", 60), ("assigned", 90),
                     ("flair", 200), ("titles", 560)):
            self.sutree.heading(c, text={"sub": "Sub", "posts": "Posts",
                                         "assigned": "Assigned",
                                         "flair": "Flair",
                                         "titles": "Title rules"}.get(c, c))
            self.sutree.column(c, width=w, anchor="w", stretch=False)
        self.sutree.pack(fill="both", expand=True, padx=8, pady=(4, 0))
        sux = ttk.Scrollbar(subs, orient="horizontal",
                            command=self.sutree.xview)
        sux.pack(fill="x", padx=8, pady=(0, 4))
        self.sutree.configure(xscrollcommand=sux.set)

        trow = ttk.Frame(test)
        trow.pack(fill="x", padx=8, pady=8)
        self.test_btn = ttk.Button(trow, text="TEST WORKFLOW (stops before Post)",
                                   command=self.on_test_workflow)
        self.test_btn.pack(side="left")
        ttk.Label(test, wraplength=900,
                  text="Uses the Post tab values. Runs PREP login + every "
                  "post step except the Post tap, then leaves the composer "
                  "open for you to inspect. Nothing is submitted.").pack(
                      fill="x", padx=8, pady=4)
        self.test_box = scrolledtext.ScrolledText(test, wrap="word",
                                                  height=20,
                                                  font=("Consolas", 10))
        self.test_box.pack(fill="both", expand=True, padx=8, pady=8)
        self.test_box.configure(state="disabled")

        self.log_box = scrolledtext.ScrolledText(logtab, wrap="word",
                                                 height=24,
                                                 font=("Consolas", 10))
        self.log_box.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_box.configure(state="disabled")

        self._refresh_container_choices()
        self._refresh_queue_table()
        self._on_dest_change()
        self._on_kind_change()
        try:
            self.on_stats_refresh()
        except Exception:
            pass
        try:
            self.on_subreddits_refresh()
        except Exception:
            pass
        self.root.after(200, self._probe_usb_idle)
        self.root.after(120, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if stay_awake():
            self.log("Laptop sleep blocked while open.")

    # ---- plumbing (same worker/log discipline as creator) ----
    def log(self, msg: str) -> None:
        self.log_q.put(msg)

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self.log_q.get_nowait()
                if isinstance(msg, tuple) and msg[:1] == ("__gen_done__",):
                    self._gen_done(msg[1] if len(msg) > 1 else "")
                    continue
                s = stamp()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", f"{s}  {msg}\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
                try:
                    with open(POSTER_LOG_PATH, "a", encoding="utf-8") as f:
                        f.write(f"{s}  {msg}\n")
                except OSError:
                    pass
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
        for w in (self.post_btn, self.lock_btn, self.batch_btn,
                  self.prep_btn, self.gen_btn, self.test_btn,
                  self.fetch_btn, self.prep_btn2):
            try:
                w.configure(state="normal" if not on else "disabled")
            except (tk.TclError, AttributeError):
                pass
        try:
            self.stop_btn.configure(state="normal" if on else "disabled")
        except (tk.TclError, AttributeError):
            pass

    def _start_worker(self, target, *args) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.stop.clear()
        self._busy(True)
        self.worker = threading.Thread(target=target, args=args, daemon=True)
        self.worker.start()

    def _after_job(self) -> None:
        self._busy(False)
        self._refresh_container_choices()
        self._refresh_queue_table()

    def _connect(self, password: str):
        phone = Phone(log=self.log)
        self.phone = phone
        self.log("USB SSH…")
        phone.connect(password, also_wda=False)
        stack = TapStack(phone, self.log)
        self.log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend. Open XXTouch Elite, "
                             "Remote Service on.")
        return phone, stack

    def _close_phone(self, phone) -> None:
        try:
            if phone:
                phone.close()
        except Exception:
            pass
        self.phone = None

    # ---- post tab ----
    def on_refresh(self) -> None:
        self._refresh_container_choices()
        self.log("Containers refreshed.")

    def _refresh_container_choices(self, select: str = "") -> None:
        try:
            rows = load_slots()
        except Exception:
            rows = []
        ready = []
        try:
            refused = retired_ids()
        except Exception:
            refused = set()
        for r in rows:
            if not slot_reddit_ready(r):
                continue
            sid = str(r.get("id") or "")
            if not sid or sid.strip().lower() in refused:
                continue
            user = str(r.get("username") or "").strip()
            ready.append((f"{sid} ({user})" if user else sid, sid))
        self._post_container_ids = {label: sid for label, sid in ready}
        labels = [label for label, _ in ready]
        try:
            cur = self.post_container.get()
        except (tk.TclError, AttributeError):
            return
        try:
            self.post_container.configure(values=labels)
        except (tk.TclError, AttributeError):
            return
        if select:
            for label, sid in ready:
                if sid == select:
                    self.post_container.set(label)
                    self._refresh_community_choices()
                    return
        for label in labels:
            if label == cur:
                self._refresh_community_choices()
                return
        self.post_container.set(labels[0] if labels else "")
        self._refresh_community_choices()

    def _selected_container_id(self) -> str:
        try:
            disp = self.post_container.get().strip()
        except (tk.TclError, AttributeError):
            return ""
        return self._post_container_ids.get(disp, "")

    def _refresh_community_choices(self, select: str = "") -> None:
        """Dropdown = settings + subreddits dirs, assigned subs first for
        the selected container. Hint shows the assignment."""
        names, selected = load_community_names()
        sid = self._selected_container_id().strip().lower()
        assigned = []
        try:
            assigned = load_sub_map().get(sid, []) if sid else []
        except Exception:
            assigned = []
        ordered = [c for c in assigned if c in names]
        ordered += [c for c in names if c not in ordered]
        try:
            self.post_community.configure(values=ordered)
        except (tk.TclError, AttributeError):
            return
        want = select or selected
        if want not in ordered:
            try:
                cur = self.post_community.get().strip()
            except (tk.TclError, AttributeError):
                cur = ""
            want = cur if cur in ordered else (ordered[0] if ordered else "")
        if want:
            try:
                self.post_community.set(want)
            except (tk.TclError, AttributeError):
                pass
        try:
            if sid and assigned:
                self.comm_hint.set("%s → %s" % (
                    sid, ", ".join("r/" + s for s in assigned)))
            elif sid:
                self.comm_hint.set("%s: no subs assigned yet" % sid)
            else:
                self.comm_hint.set("")
        except (tk.TclError, AttributeError):
            pass

    def _save_community_sel(self) -> None:
        try:
            sel = self.post_community.get().strip()
        except (tk.TclError, AttributeError):
            return
        names, _ = load_community_names()
        if sel and sel not in names:
            return
        save_community_names(names, sel)

    def on_community_add(self) -> None:
        try:
            name = self.post_community.get().strip()
        except (tk.TclError, AttributeError):
            return
        try:
            name = community_name(name)
        except PhoneError as e:
            self.log("ERROR  " + str(e))
            return
        names, _ = load_community_names()
        if name not in names:
            names.append(name)
        save_community_names(names, name)
        self._refresh_community_choices(select=name)
        self.log(f"Community r/{name} added.")

    def on_community_remove(self) -> None:
        try:
            name = self.post_community.get().strip()
        except (tk.TclError, AttributeError):
            return
        names, selected = load_community_names()
        if name not in names:
            self.log("Community not in list.")
            return
        names = [c for c in names if c != name]
        save_community_names(names, "" if selected == name else selected)
        self._refresh_community_choices()
        self.log(f"Community r/{name} removed.")

    def _on_dest_change(self) -> None:
        try:
            profile = self.post_dest.get() == "profile"
        except (tk.TclError, AttributeError):
            return
        try:
            self.post_community.configure(
                state="disabled" if profile else "normal")
        except (tk.TclError, AttributeError):
            pass

    def _is_link(self) -> bool:
        try:
            return self.post_kind.get() == "link"
        except (tk.TclError, AttributeError):
            return False

    def _on_kind_change(self) -> None:
        link = self._is_link()
        if link:
            try:
                self.post_dest.set("community")
            except (tk.TclError, AttributeError):
                pass
            self._on_dest_change()
        for w, st in ((self.post_url, "normal" if link else "disabled"),
                      (self.fetch_btn, "normal" if link else "disabled"),
                      (self.gen_btn, "disabled" if link else "normal")):
            try:
                w.configure(state=st)
            except (tk.TclError, AttributeError):
                pass

    def _tab_values(self):
        try:
            dest = self.post_dest.get()
        except (tk.TclError, AttributeError):
            dest = "community"
        images = [p for p in self.post_images if str(p or "").strip()]
        if not images:
            try:
                single = self.post_image.get().strip()
            except (tk.TclError, AttributeError):
                single = ""
            if single and os.path.isfile(single):
                images = [single]
        try:
            community = self.post_community.get().strip()
        except (tk.TclError, AttributeError):
            community = ""
        try:
            niche = self.post_niche.get().strip()
        except (tk.TclError, AttributeError):
            niche = ""
        try:
            title = self.post_title.get().strip()
        except (tk.TclError, AttributeError):
            title = ""
        try:
            flair = self.post_flair.get().strip()[:64]
        except (tk.TclError, AttributeError):
            flair = ""
        try:
            url = self.post_url.get().strip()
        except (tk.TclError, AttributeError):
            url = ""
        try:
            kind = self.post_kind.get()
        except (tk.TclError, AttributeError):
            kind = "image"
        if kind not in ("image", "link"):
            kind = "image"
        return {"container": self._selected_container_id(), "dest": dest,
                "community": community, "niche": niche, "title": title,
                "flair": flair, "images": images, "kind": kind, "url": url}

    def on_browse_image(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Image(s) for Reddit (multi-select allowed for My Profile)",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.JPG *.JPEG *.PNG"),
                       ("All", "*.*")],
        )
        if paths:
            self.post_images = list(paths)
            names = [os.path.basename(p) for p in self.post_images]
            shown = ", ".join(names[:3]) + (" …" if len(names) > 3 else "")
            self.post_image.set(f"{len(names)} file(s): {shown}")

    def _gen_done(self, title: str) -> None:
        try:
            self._on_kind_change()
        except (tk.TclError, AttributeError, RuntimeError):
            try:
                self.gen_btn.configure(state="normal")
            except (tk.TclError, AttributeError):
                pass
        if title:
            title = title.strip()
            if title:
                title = title[0].upper() + title[1:]
            try:
                self.post_title.delete(0, "end")
                self.post_title.insert(0, title)
            except (tk.TclError, AttributeError):
                pass

    def on_generate_title(self) -> None:
        self.log("GENERATE CLICKED")
        try:
            self.gen_btn.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass
        v = self._tab_values()

        def live_log(line: str) -> None:
            try:
                self.log_q.put(line)
            except Exception:
                pass

        def work() -> None:
            title = ""
            try:
                niche = (v["niche"] or "").strip() or niche_for(
                    v["community"])
                title = generate_title(
                    v["community"], v["images"][0] if v["images"] else "",
                    log=live_log, niche=niche)
            except BaseException as e:
                try:
                    self.log_q.put("Generate ERROR  %s: %s"
                                   % (type(e).__name__, str(e)[:150]))
                except Exception:
                    pass
            finally:
                try:
                    self.log_q.put(("__gen_done__", title))
                except Exception:
                    pass

        self._gen_run = getattr(self, "_gen_run", 0) + 1

        def watchdog(run: int = self._gen_run) -> None:
            try:
                if run == self._gen_run and self.gen_btn.instate(["disabled"]):
                    self.log("WATCHDOG: worker never returned")
                    self.gen_btn.configure(state="normal")
            except (tk.TclError, AttributeError, RuntimeError):
                pass

        threading.Thread(target=work, daemon=True).start()
        try:
            self.root.after(180000, watchdog)
        except (tk.TclError, AttributeError, RuntimeError):
            pass

    def on_fetch_link_title(self) -> None:
        self.log("FETCH CLICKED")
        try:
            self.fetch_btn.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass
        v = self._tab_values()
        if not link_slug(v.get("url") or ""):
            self.log("Fetch needs a redgifs watch URL first.")
            try:
                self.fetch_btn.configure(state="normal")
            except (tk.TclError, AttributeError):
                pass
            return

        def live_log(line: str) -> None:
            try:
                self.log_q.put(line)
            except Exception:
                pass

        def work() -> None:
            title = ""
            try:
                title = generate_link_title(v.get("url") or "",
                                            log=live_log)
            except BaseException as e:
                try:
                    self.log_q.put("Fetch ERROR  %s: %s"
                                   % (type(e).__name__, str(e)[:150]))
                except Exception:
                    pass
            finally:
                try:
                    self.log_q.put(("__gen_done__", title))
                except Exception:
                    pass

        self._gen_run = getattr(self, "_gen_run", 0) + 1

        def watchdog(run: int = self._gen_run) -> None:
            try:
                for btn in (self.fetch_btn, self.gen_btn):
                    try:
                        if run == self._gen_run and btn.instate(["disabled"]):
                            btn.configure(state="normal")
                    except (tk.TclError, AttributeError, RuntimeError):
                        pass
                if run == self._gen_run:
                    self.log("WATCHDOG: worker never returned")
            except (tk.TclError, AttributeError, RuntimeError):
                pass

        threading.Thread(target=work, daemon=True).start()
        try:
            self.root.after(300000, watchdog)
        except (tk.TclError, AttributeError, RuntimeError):
            pass

    def _validate_tab(self, v: dict) -> str:
        if not v["container"]:
            return "Pick a container from the dropdown (needs a completed Reddit account)."
        if not v["title"]:
            return "Post needs a title."
        if str(v.get("community") or "").strip().lower() in KARMA_GATED:
            return ("r/%s needs karma our accounts don't have yet."
                    % v["community"].strip())
        if v.get("dest") != "profile" and v.get("kind") != "link":
            allowed = load_sub_map().get(
                str(v["container"] or "").strip().lower(), [])
            if not allowed:
                return (f"{v['container']} has no subs assigned yet — "
                        f"assign first.")
            if str(v.get("community") or "").strip().lower() not in allowed:
                return (f"r/{v['community'].strip()} not assigned to "
                        f"{v['container']} (assigned: "
                        f"{', '.join('r/' + s for s in allowed)}).")
            holder = sub_holder(v.get("community") or "", v["container"])
            if holder:
                return (f"r/{v['community'].strip()} held by {holder} "
                        f"(exclusive, forever).")
        if v.get("kind") == "link":
            return "Link posts are off — image posts only."
        if v.get("kind") == "link":
            if not link_slug(v.get("url") or ""):
                return "Link post needs a redgifs watch URL."
            if nsfw_is_posted(v.get("url") or ""):
                return "URL already posted once (ledger)."
            try:
                community_name(v["community"])
            except PhoneError as e:
                return "ERROR  " + str(e)
            slot = get_slot(v["container"])
            if not slot or not slot.get("ghost"):
                return f"{v['container']} has no Ghost/Crane container."
            if not slot_reddit_ready(slot):
                return f"{v['container']} does not have a completed Reddit account."
            return ""
        if not v["images"]:
            return "Post needs JPG/PNG file(s) from this PC (Browse…)."
        for fp in v["images"]:
            if not os.path.isfile(fp):
                return f"Post image not on this PC: {fp}"
        if v["dest"] != "profile":
            try:
                community_name(v["community"])
            except PhoneError as e:
                return "ERROR  " + str(e)
        slot = get_slot(v["container"])
        if not slot or not slot.get("ghost"):
            return f"{v['container']} has no Ghost/Crane container."
        if not slot_reddit_ready(slot):
            return f"{v['container']} does not have a completed Reddit account."
        return ""

    def on_prep(self) -> None:
        v = self._tab_values()
        err = self._validate_tab(v)
        if err and "title" not in err.lower():
            self.log(err)
            return
        self._start_worker(self._run_prep, self.pw.get(), v["container"])

    def _run_prep(self, password: str, name: str) -> None:
        phone = None
        try:
            phone, stack = self._connect(password)
            if self.stop.is_set():
                return
            slot = get_slot(name)
            email = str((slot or {}).get("email") or "")
            username = str((slot or {}).get("username") or "").strip()
            self.log(f"=== PREP {name} ===")
            close_reddit(stack, phone, self.log)
            open_container_rotated(phone, stack, self.log, name, email,
                                   stop=self.stop)
            reason = ensure_login(stack, phone, self.log, name, "pp_prep",
                                  stop=self.stop)
            if reason:
                self.log(f"PREP {name}: login failed ({reason}).")
            else:
                self.log(f"PREP {name}: staged u/{username}.")
            stack.press_home()
        except PhoneError as e:
            self.log("ERROR  " + str(e))
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
        finally:
            self._close_phone(phone)
            try:
                self.root.after(0, self._after_job)
            except (tk.TclError, RuntimeError):
                pass

    def on_post_single(self) -> None:
        v = self._tab_values()
        err = self._validate_tab(v)
        if err:
            self.log(err)
            return
        self._start_worker(self._run_single, self.pw.get(), v)

    def _run_single(self, password: str, item: dict) -> None:
        phone = None
        try:
            phone, stack = self._connect(password)
            if self.stop.is_set():
                return
            self.log(f"=== Post {item['container']} ===")
            verdict = self._send_item(phone, stack, item)
            self.status.set(f"{item['container']}: {verdict}")
        except PhoneError as e:
            self.log("ERROR  " + str(e))
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
        finally:
            self._close_phone(phone)
            try:
                self.root.after(0, self._after_job)
            except (tk.TclError, RuntimeError):
                pass

    def on_fire_selected(self) -> None:
        """FIRE the selected queue row on the staged session: no open, no
        login chain, no IP change. PREP the container first."""
        try:
            sel = self.qtree.selection()
        except (tk.TclError, AttributeError):
            return
        if not sel:
            self.log("FIRE no-login: select a queue row first.")
            return
        try:
            idx = int(sel[0])
            item = load_queue()[idx]
        except (ValueError, IndexError, AttributeError):
            return
        if item.get("status") not in ("queued", "failed"):
            self.log("FIRE no-login: row is %s, only queued/failed fire."
                     % item.get("status"))
            return
        self.log(f"FIRE no-login {item.get('container')} "
                 f"r/{item.get('community') or item.get('dest')}.")
        self._start_worker(self._run_fire_selected, self.pw.get(), idx)

    def on_fire_login(self) -> None:
        """FIRE the selected queue row with the full fresh path: IP
        rotation, container open, login chain, then post. For first-time
        containers (nothing staged)."""
        try:
            sel = self.qtree.selection()
        except (tk.TclError, AttributeError):
            return
        if not sel:
            self.log("FIRE login: select a queue row first.")
            return
        try:
            idx = int(sel[0])
            item = load_queue()[idx]
        except (ValueError, IndexError, AttributeError):
            return
        if item.get("status") not in ("queued", "failed"):
            self.log("FIRE login: row is %s, only queued/failed fire."
                     % item.get("status"))
            return
        self.log(f"FIRE login {item.get('container')} "
                 f"r/{item.get('community') or item.get('dest')}.")
        self._start_worker(self._run_fire_login, self.pw.get(), idx)

    def on_test_batch(self) -> None:
        """Dry batch: every queued community-image item runs to composer,
        flair picked for real, Post never tapped, composer discarded.
        Statuses untouched: Start batch still sends afterwards."""
        items = [i for i in load_queue() if i.get("status") == "queued"]
        if not items:
            self.log("Test batch: nothing queued.")
            return
        self.log(f"Test batch: {len(items)} queued, Post never tapped.")
        self._start_worker(self._run_test_batch, self.pw.get())

    def _run_test_batch(self, password: str) -> None:
        phone = None
        try:
            phone, stack = self._connect(password)
            n = 0
            for item in load_queue():
                if self.stop.is_set():
                    break
                if item.get("status") != "queued":
                    continue
                n += 1
                self.log(f"=== Test {n}: {item.get('container')} "
                         f"r/{item.get('community')} ===")
                try:
                    verdict = self._send_item(phone, stack, dict(item),
                                              fresh=True, dry=True)
                except PhoneError as e:
                    verdict = str(e)[:160]
                    self.log("ERROR  " + str(e))
                except Exception as e:
                    verdict = f"{type(e).__name__}: {e}"[:160]
                    self.log(f"ERROR  {type(e).__name__}: {e}")
                self.log(f"Test {n}: {verdict}.")
            self.log("Test batch done: nothing posted, nothing marked.")
        except PhoneError as e:
            self.log("ERROR  " + str(e))
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
        finally:
            self._close_phone(phone)
            try:
                self.root.after(0, self._after_job)
            except (tk.TclError, RuntimeError):
                pass

    def _run_fire_selected(self, password: str, idx: int) -> None:
        phone = None
        try:
            items = load_queue()
            try:
                item = items[idx]
            except IndexError:
                self.log("FIRE no-login: row gone.")
                return
            phone, stack = self._connect(password)
            if self.stop.is_set():
                return
            verdict = self._send_item(phone, stack, item, fresh=False,
                                      staged=True)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if verdict in ("posted", "posted to profile"):
                item["status"] = "posted"
                item["posted_ts"] = now
            else:
                item["status"] = "failed"
                item["failed_ts"] = now
            item["note"] = verdict
            save_queue(items)
            self.status.set(f"{item.get('container')}: {verdict}")
        except PhoneError as e:
            self.log("ERROR  " + str(e))
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
        finally:
            self._close_phone(phone)
            try:
                self.root.after(0, self._after_job)
            except (tk.TclError, RuntimeError):
                pass

    def _run_fire_login(self, password: str, idx: int) -> None:
        phone = None
        try:
            items = load_queue()
            try:
                item = items[idx]
            except IndexError:
                self.log("FIRE login: row gone.")
                return
            phone, stack = self._connect(password)
            if self.stop.is_set():
                return
            verdict = self._send_item(phone, stack, item, fresh=True,
                                      staged=False)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if verdict in ("posted", "posted to profile"):
                item["status"] = "posted"
                item["posted_ts"] = now
            else:
                item["status"] = "failed"
                item["failed_ts"] = now
            item["note"] = verdict
            save_queue(items)
            self.status.set(f"{item.get('container')}: {verdict}")
        except PhoneError as e:
            self.log("ERROR  " + str(e))
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
        finally:
            self._close_phone(phone)
            try:
                self.root.after(0, self._after_job)
            except (tk.TclError, RuntimeError):
                pass

    def _send_item(self, phone, stack, item: dict, fresh: bool = True,
                     staged: bool = False, dry: bool = False,
                     linger: bool = False) -> str:
        if self.stop.is_set():
            return "stopped"
        if dry and (str(item.get("kind") or "image") == "link"
                    or str(item.get("dest") or "community") == "profile"):
            return "dry supports community image only, skipped"
        if str(item.get("kind") or "image") == "link":
            reason = run_link_item(phone, stack, self.log, item, self.stop,
                                   fresh=fresh, staged=staged)
            return "posted" if not reason else reason
        if str(item.get("dest") or "community") == "profile":
            name = str(item["container"])
            slot = get_slot(name)
            email = str((slot or {}).get("email") or "")
            if staged:
                self.log("Staged session, skipping open + login.")
            else:
                close_reddit(stack, phone, self.log)
                open_container_rotated(phone, stack, self.log, name, email,
                                       stop=self.stop, rotate=fresh)
            if fresh and not staged:
                reason = ensure_login(stack, phone, self.log, name,
                                      "pp_login", stop=self.stop)
                if reason:
                    return "login chain failed: " + reason
            else:
                self.log("Login proven this container, skipping chain.")
            run_profile_post(phone, stack, self.log, name,
                             str(item.get("title") or ""),
                             [str(p) for p in (item.get("images") or [])],
                             stop=self.stop, reuse_open=True)
            return "posted to profile"
        reason = run_community_item(phone, stack, self.log, item, self.stop,
                                    fresh=fresh, staged=staged, dry=dry,
                                    linger=linger)
        return "posted" if not reason else reason

    def _test_proof(self, line: str) -> None:
        try:
            self.root.after(0, self._test_proof_sync, line)
        except (tk.TclError, RuntimeError):
            pass

    def _test_proof_sync(self, line: str) -> None:
        try:
            self.test_box.configure(state="normal")
            self.test_box.insert("end", line + "\n")
            self.test_box.see("end")
            self.test_box.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass

    def on_test_workflow(self) -> None:
        v = self._tab_values()
        err = self._validate_tab(v)
        if err:
            self.log(err)
            return
        try:
            self.test_box.configure(state="normal")
            self.test_box.delete("1.0", "end")
            self.test_box.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass
        self._start_worker(self._run_test, self.pw.get(), v)

    def _run_test(self, password: str, item: dict) -> None:
        phone = None
        proof = self._test_proof
        try:
            phone, stack = self._connect(password)
            name = str(item["container"])
            slot = get_slot(name)
            email = str((slot or {}).get("email") or "")
            username = str((slot or {}).get("username") or "").strip()
            profile = str(item.get("dest") or "community") == "profile"
            proof(f"TEST {name} ({'profile' if profile else item['community']})")
            if self.stop.is_set():
                proof("STOPPED"); return
            if str(item.get("kind") or "image") == "link":
                url = str(item.get("url") or "")
                slug = link_slug(url)
                community = community_display(community_name(str(item.get("community") or "")))
                if not slug:
                    proof("FAIL unparseable URL"); return
                if nsfw_is_posted(url):
                    proof("FAIL URL already posted (ledger)"); return
                proof("PROOF URL parses, ledger clear")
                blob, nodes = dump_named(stack, phone, "pp_test_now",
                                         self.log, shot=True)
                low = (blob or "").lower()
                if any(w in low for w in VERIFY_WALL):
                    proof("FAIL Reddit is on Verify"); return
                proof("PROOF no verify wall")
                close_reddit(stack, phone, self.log)
                open_container(stack, phone, self.log, name, email)
                proof("PROOF container open")
                reason = login_probe(stack, phone, self.log, name, username)
                if reason:
                    proof("FAIL login probe: " + reason); return
                proof("PROOF login alive u/%s" % username)
                if self.stop.is_set():
                    proof("STOPPED"); return
                reason = uiopen_community(stack, phone, self.log, community)
                if reason:
                    proof("FAIL entry: " + reason); return
                proof("PROOF community open " + community)
                blob, nodes = dump_named(stack, phone, "pp_test_feed",
                                         self.log, shot=True)
                nodes = tap_create(stack, phone, self.log, nodes)
                proof("PROOF composer open")
                blob, nodes = dump_named(stack, phone, "pp_test_comp",
                                         self.log, shot=False)
                tit = find_label_exact(nodes, ("Add a title", "Title",
                                               "An interesting title"))
                if tit is None:
                    proof("FAIL no title field"); return
                stack.tap_xy(*tit.center())
                time.sleep(0.35)
                stack.type_human(str(item.get("title") or ""), ms=95)
                time.sleep(0.8)
                blob, nodes = dump_named(stack, phone, "pp_test_typed",
                                         self.log, shot=True)
                proof("PROOF title typed")
                cands = [n for n in nodes or []
                         if n.ntype != "ocr-line" and n.on_screen()
                         and 700 <= n.y <= 900 and 20 <= n.x <= 120]
                if not cands:
                    proof("FAIL no link icon"); return
                stack.tap_xy(*min(cands, key=lambda n: n.x).center())
                time.sleep(1.2)
                blob, nodes = dump_named(stack, phone, "pp_test_url",
                                         self.log, shot=False)
                stack.type_text_block(url)
                time.sleep(1.0)
                blob, nodes = dump_named(stack, phone, "pp_test_sent",
                                         self.log, shot=True)
                if slug not in (blob or "").lower():
                    proof("FAIL URL unverified in field"); return
                proof("PROOF URL verified in field. STOPPING before Post. "
                      "Inspect the phone, discard the composer yourself.")
                return
            if profile:
                images = [str(p) for p in (item.get("images") or [])]
                from reddit_post import read_image_bytes  # noqa
                for fp in images:
                    read_image_bytes(fp)
                proof("PROOF images readable: %d" % len(images))
                close_reddit(stack, phone, self.log)
                open_container(stack, phone, self.log, name, email)
                proof("PROOF container open")
                blob, nodes = dump_named(stack, phone, "pp_test_feed",
                                         self.log, shot=True)
                reason = login_probe(stack, phone, self.log, name, username)
                if reason:
                    proof("FAIL login probe: " + reason); return
                proof("PROOF login alive u/%s" % username)
                nodes = tap_avatar(stack, phone, self.log, nodes)
                nodes = tap_profile_row(stack, phone, self.log, nodes)
                nodes = tap_create_bottom(stack, phone, self.log, nodes)
                confirm_profile_composer(nodes, username, self.log)
                proof("PROOF profile composer targets u/%s" % username)
                nodes = type_title(stack, phone, self.log, nodes,
                                   str(item.get("title") or ""))
                proof("PROOF title typed")
                nodes = dismiss_keyboard(stack, phone, self.log, nodes)
                proof("PROOF keyboard down, composer open. "
                      "STOPPING before image attach + Post. "
                      "Inspect the phone, discard the composer yourself.")
            else:
                community = community_display(community_name(str(item.get("community") or "")))
                image = str((item.get("images") or [""])[0])
                flair = str(item.get("flair") or "").strip()[:64]
                import_to_camera_roll(stack, image, self.log)
                proof("PROOF camera roll holds exactly 1 photo")
                if self.stop.is_set():
                    proof("STOPPED"); return
                blob, nodes = dump_named(stack, phone, "pp_test_now",
                                         self.log, shot=True)
                low = (blob or "").lower()
                if any(w in low for w in VERIFY_WALL):
                    proof("FAIL Reddit is on Verify"); return
                proof("PROOF no verify wall")
                close_reddit(stack, phone, self.log)
                open_container(stack, phone, self.log, name, email)
                proof("PROOF container open")
                reason = login_probe(stack, phone, self.log, name, username)
                if reason:
                    proof("FAIL login probe: " + reason); return
                proof("PROOF login alive u/%s" % username)
                if self.stop.is_set():
                    proof("STOPPED"); return
                reason = uiopen_community(stack, phone, self.log, community)
                if reason:
                    proof("FAIL entry: " + reason); return
                proof("PROOF community open " + community)
                blob, nodes = dump_named(stack, phone, "pp_test_feed",
                                         self.log, shot=True)
                nodes = tap_create_safe(stack, phone, self.log, nodes,
                                        stem="pp_test_create")
                proof("PROOF composer open")
                nodes = tap_image_type(stack, phone, self.log, nodes)
                nodes = tap_recents_photo(stack, phone, self.log, nodes)
                proof("PROOF image attached")
                if self.stop.is_set():
                    proof("STOPPED"); return
                nodes = type_title(stack, phone, self.log, nodes,
                                   str(item.get("title") or ""))
                proof("PROOF title typed")
                blob, _n = dump_named(stack, phone, "pp_test_flair",
                                      self.log, shot=False)
                try:
                    record_community_rules(community, blob, self.log)
                except Exception as e:
                    self.log("Rules save note: %s" % e)
                try:
                    record_community_flair(community, blob, self.log)
                except Exception as e:
                    self.log("Flair demand save note: %s" % e)
                _demand = flair_demand(blob)
                if _demand == "required":
                    if not flair:
                        proof("FAIL flair REQUIRED and Flair empty"); return
                    proof("PROOF flair required, applying %r" % flair)
                elif _demand == "optional" and flair:
                    proof("PROOF optional flair, applying %r" % flair)
                nodes = apply_flair(stack, phone, self.log, nodes, flair)
                proof("PROOF flair done. STOPPING before Post. "
                      "Inspect the phone, discard the composer yourself.")
            self.log(f"TEST {name} complete to composer, nothing submitted.")
        except PhoneError as e:
            proof("FAIL %s" % e)
            self.log("ERROR  " + str(e))
        except Exception as e:
            proof("FAIL %s: %s" % (type(e).__name__, e))
            self.log(f"ERROR  {type(e).__name__}: {e}")
        finally:
            self._close_phone(phone)
            try:
                self.root.after(0, self._after_job)
            except (tk.TclError, RuntimeError):
                pass

    def on_lock_in(self) -> None:
        v = self._tab_values()
        err = self._validate_tab(v)
        if err:
            self.log(err)
            return
        items = load_queue()
        items.append({"container": v["container"], "dest": v["dest"],
                      "community": v["community"], "niche": v["niche"],
                      "title": v["title"], "flair": v["flair"],
                      "flair_auto": False,
                      "images": v["images"], "kind": v.get("kind", "image"),
                      "url": v.get("url", ""), "status": "queued",
                      "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")})
        if v.get("kind") == "link" and v.get("url"):
            try:
                nsfw_mark_staged([{"sub": v["community"],
                                   "slug": link_slug(v["url"]),
                                   "url": v["url"]}])
            except Exception as e:
                self.log("Ledger stage note: %s" % e)
        save_queue(items)
        self._refresh_queue_table()
        self.log(f"LOCKED IN {v['container']} ({len(items)} queued).")
        try:
            self.post_title.delete(0, "end")
        except (tk.TclError, AttributeError):
            pass
        try:
            self.post_url.delete(0, "end")
        except (tk.TclError, AttributeError):
            pass
        self.post_images = []
        self.post_image.set("")

    # ---- batch tab ----
    def _refresh_queue_table(self) -> None:
        try:
            for iid in self.qtree.get_children():
                self.qtree.delete(iid)
            for i, it in enumerate(load_queue()):
                if _row_stale_posted(it):
                    continue
                imgs = it.get("images") or []
                what = link_slug(it.get("url") or "")[:24] \
                    if it.get("kind") == "link" else len(imgs)
                self.qtree.insert("", "end", iid=str(i), values=(
                    it.get("container", ""), it.get("dest", ""),
                    it.get("community", ""), str(it.get("title", ""))[:60],
                    what, it.get("status", "")))
        except (tk.TclError, AttributeError):
            pass

    def on_queue_open_pic(self) -> None:
        """Open the selected queued item's image(s) in the PC viewer."""
        try:
            sel = self.qtree.selection()
        except (tk.TclError, AttributeError):
            return
        if not sel:
            self.log("Open pic: select a queue row first.")
            return
        try:
            idx = int(sel[0])
            images = [p for p in (load_queue()[idx].get("images") or [])
                      if str(p or "").strip()]
        except (ValueError, IndexError, AttributeError):
            return
        if not images:
            self.log("Open pic: no image on this row.")
            return
        opened = 0
        for fp in images:
            if not os.path.isfile(fp):
                self.log(f"Open pic: missing {fp}")
                continue
            try:
                os.startfile(fp)  # noqa: S606 (local viewer, user click)
                opened += 1
            except Exception as e:
                self.log(f"Open pic failed: {type(e).__name__}")
        if opened:
            self.log(f"Opened {opened} pic(s).")

    def on_queue_remove(self) -> None:
        try:
            sel = self.qtree.selection()
        except (tk.TclError, AttributeError):
            return
        if not sel:
            return
        items = load_queue()
        try:
            items.pop(int(sel[0]))
        except (ValueError, IndexError):
            return
        save_queue(items)
        self._refresh_queue_table()

    def on_queue_clear_posted(self) -> None:
        items = [i for i in load_queue() if i.get("status") != "posted"]
        save_queue(items)
        self._refresh_queue_table()
        self.log("Cleared posted items.")

    def on_batch(self) -> None:
        from farm_stats import day_of as _day_of

        def _pair(it):
            return (str(it.get("container") or "").strip().lower(),
                    str(it.get("community") or "").strip().lower())

        try:
            _all = load_queue()
            _n_reset = reset_retryable(_all)
            if _n_reset:
                save_queue(_all)
                self.log("Batch: %d failed reset to queued." % _n_reset)
                try:
                    self._refresh_queue_table()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            today = nyc_today()
            done = {_pair(i)
                    for i in load_queue()
                    if str(i.get("status") or "") == "posted"
                    and _day_of(i.get("posted_ts", i.get("ts"))) == today
                    and str(i.get("dest") or "community") != "profile"}
        except Exception:
            done = set()
        items = [i for i in load_queue()
                 if _retryable(i)
                 and _pair(i) not in done]
        if not items:
            self.log("Batch: nothing postable (no queued/failed rows, or "
                     "every account+sub already posted today).")
            return
        if len(items) != len([i for i in load_queue()
                              if _retryable(i)]):
            skipped = sorted({str(i.get("container") or "") + ":"
                              + str(i.get("community") or "") for i in
                              load_queue()
                              if _retryable(i)
                              and _pair(i) in done})
            self.log("Batch: 1 post/account/sub/day, skipping posted today: %s."
                     % ",".join(skipped))
        bad = []
        for i in items:
            if str(i.get("dest") or "community") == "profile":
                continue
            allowed = load_sub_map().get(
                str(i.get("container") or "").strip().lower(), [])
            if str(i.get("community") or "").strip().lower() not in allowed:
                bad.append("%s:%s" % (i.get("container"),
                                      i.get("community")))
                continue
            holder = sub_holder(i.get("community") or "", i.get("container"))
            if holder:
                bad.append("%s:%s(held by %s)" % (i.get("container"),
                                                 i.get("community"), holder))
        if bad:
            self.log("Batch: sub assignment violated, not starting: %s."
                     % ", ".join("r/%s" % b for b in bad))
            return
        self.log(f"Batch: {len(items)} postable, 1 post each down the list.")
        self._start_worker(self._run_batch, self.pw.get())

    def on_prepare_posts(self) -> None:
        self._start_worker(self._run_prepare)

    def _run_prepare(self) -> None:
        """Auto-fill queue: per mapped container × assigned sub, one item
        each (1/account/sub/day). Random pool pic, Grok title, gates
        enforced. Review the table, then Start batch. No phone needed."""
        import random as _random
        from farm_stats import day_of as _day_of
        rng = _random.Random()
        try:
            today = nyc_today()
        except Exception:
            today = ""
        try:
            sub_map = load_sub_map()
        except Exception:
            sub_map = {}
        if not sub_map:
            self.log("Prepare: sub map empty, nothing assigned.")
            return
        try:
            self.root.after(0, self._refresh_queue_table)
        except (tk.TclError, RuntimeError):
            pass
        made = 0
        picked: set = set()
        try:
            for sid in sorted(sub_map):
                if self.stop.is_set():
                    break
                try:
                    slot = get_slot(sid)
                except Exception:
                    slot = None
                if not slot or not slot.get("ghost") \
                        or not slot_reddit_ready(slot):
                    self.log("Prepare: %s not postable, skipped." % sid)
                    continue
                for sub in (sub_map.get(sid) or []):
                    if self.stop.is_set():
                        break
                    sub = str(sub or "").strip().lower()
                    if not sub:
                        continue
                    if sub in KARMA_GATED:
                        self.log("Prepare: r/%s karma-gated, skipped." % sub)
                        continue
                    items = load_queue()
                    pair = (sid, sub)
                    if any(str(i.get("status") or "") == "posted"
                           and (str(i.get("container") or "").strip().lower(),
                                str(i.get("community") or "").strip().lower()
                                ) == pair
                           and _day_of(i.get("posted_ts", i.get("ts")))
                           == today for i in items):
                        self.log("Prepare: %s r/%s already posted today."
                                 % (sid, sub))
                        continue
                    if any(str(i.get("status") or "") == "queued"
                           and (str(i.get("container") or "").strip().lower(),
                                str(i.get("community") or "").strip().lower()
                                ) == pair for i in items):
                        self.log("Prepare: %s r/%s already queued."
                                 % (sid, sub))
                        continue
                    holder = sub_holder(sub, sid)
                    if holder:
                        self.log("Prepare: r/%s held by %s, skipped."
                                 % (sub, holder))
                        continue
                    pool = pool_pics(sub)
                    choices = available_pics(
                        pool, used_names(sid, sub),
                        {p for (s, p) in picked if s == sid})
                    if not choices:
                        self.log("Prepare: r/%s pool empty for %s." % (sub, sid))
                        continue
                    image = rng.choice(choices)
                    picked.add((sid, os.path.basename(image).lower()))
                    self.log("Prepare: %s r/%s pic %s, Grok title…"
                             % (sid, sub, os.path.basename(image)[:40]))
                    try:
                        title = generate_title(sub, image, log=self.log,
                                               niche=niche_for(sub))
                    except Exception as e:
                        self.log("Prepare: Grok fail %s r/%s (%s)."
                                 % (sid, sub, str(e)[:100]))
                        continue
                    title = (title or "").strip()
                    if title:
                        title = title[0].upper() + title[1:]
                    if not title:
                        self.log("Prepare: empty title %s r/%s, skipped."
                                 % (sid, sub))
                        continue
                    item = {"container": sid, "dest": "community",
                            "community": sub, "niche": niche_for(sub),
                            "title": title,
                            "flair": flair_for(sub), "flair_auto": True,
                            "images": [image],
                            "kind": "image", "url": "", "status": "queued",
                            "ts": datetime.now(timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%SZ")}
                    v = {"container": sid, "dest": "community",
                         "community": sub, "niche": niche_for(sub),
                         "title": title,
                         "flair": item["flair"], "flair_auto": True,
                         "images": [image],
                         "kind": "image", "url": ""}
                    err = self._validate_tab(v)
                    if err:
                        self.log("Prepare: %s r/%s refused (%s)."
                                 % (sid, sub, err))
                        continue
                    items = load_queue()
                    items.append(item)
                    save_queue(items)
                    made += 1
                    self.log("Prepare: LOCKED IN %s r/%s." % (sid, sub))
                    try:
                        self.root.after(0, self._refresh_queue_table)
                    except (tk.TclError, RuntimeError):
                        pass
        finally:
            self.log("Prepare done: %d item(s) queued." % made)
            try:
                self.root.after(0, self._after_job)
            except (tk.TclError, RuntimeError):
                pass

    def _run_batch(self, password: str) -> None:
        phone = None
        try:
            phone, stack = self._connect(password)
            n = 0
            posted_run: set = set()
            skipped_run: set = set()
            last_container = ""

            def _pair(it):
                return (str(it.get("container") or "").strip().lower(),
                        str(it.get("community") or "").strip().lower())

            while not self.stop.is_set():
                items = load_queue()
                nxt = next((i for i in items
                            if _retryable(i)
                            and str(i.get("container") or "")
                            not in skipped_run), None)
                if nxt is None:
                    break
                if _pair(nxt) in posted_run:
                    skipped_run.add(str(nxt.get("container") or ""))
                    self.log("Skip %s: account+sub already posted this run, "
                             "left queued for another day."
                             % nxt.get("container"))
                    continue
                n += 1
                if str(nxt.get("dest") or "community") != "profile":
                    allowed = load_sub_map().get(
                        str(nxt.get("container") or "").strip().lower(), [])
                    holder = sub_holder(nxt.get("community") or "",
                                        nxt.get("container"))
                    if str(nxt.get("community") or "").strip().lower() \
                            not in allowed or holder:
                        skipped_run.add(str(nxt.get("container") or ""))
                        self.log("Skip %s: sub assignment changed mid-run."
                                 % nxt.get("container"))
                        continue
                self.log(f"=== Batch {n}: {nxt.get('container')} ===")
                fresh = str(nxt.get("container") or "") != last_container
                if not fresh:
                    self.log("Same container, skipping IP + login repeat.")
                linger = any(
                    str(o.get("container") or "") == str(nxt.get("container") or "")
                    and str(o.get("community") or "").strip().lower()
                    != str(nxt.get("community") or "").strip().lower()
                    and o is not nxt
                    for o in items if _retryable(o))
                # container is now the open one whether this item posts or
                # fails: same-container retries must skip IP + login repeat.
                last_container = str(nxt.get("container") or "")
                try:
                    verdict = self._send_item(phone, stack, nxt, fresh=fresh,
                                              linger=linger)
                    if verdict in ("posted", "posted to profile"):
                        nxt["status"] = "posted"
                        nxt["posted_ts"] = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ")
                    else:
                        nxt["status"] = "failed"
                        nxt["failed_ts"] = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ")
                    nxt["note"] = verdict
                except PhoneError as e:
                    nxt["status"] = "failed"
                    nxt["failed_ts"] = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                    nxt["note"] = str(e)[:160]
                    self.log("ERROR  " + str(e))
                except Exception as e:
                    nxt["status"] = "failed"
                    nxt["failed_ts"] = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                    nxt["note"] = f"{type(e).__name__}: {e}"[:160]
                    self.log(f"ERROR  {type(e).__name__}: {e}")
                save_queue(items)
                if str(nxt.get("status") or "") == "posted":
                    posted_run.add(_pair(nxt))
                    if linger:
                        # break AFTER the mark: a crash mid-break resumes
                        # safely instead of double-posting.
                        home_linger(stack, phone, self.log, self.stop)
                try:
                    self.root.after(0, self._refresh_queue_table)
                except (tk.TclError, RuntimeError):
                    pass
            self.log("Batch done.")
        except PhoneError as e:
            self.log("ERROR  " + str(e))
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
        finally:
            self._close_phone(phone)
            try:
                self.root.after(0, self._after_job)
            except (tk.TclError, RuntimeError):
                pass

    def on_stats_remove(self) -> None:
        """Retire the Stats-selected slot from the UI. Reversible via retired.json."""
        try:
            sel = self.stree.selection()
            sid = (sel[0] if sel else "").strip().lower()
        except (tk.TclError, AttributeError):
            return
        if not sid:
            self.log("Remove slot: select a row in Stats first.")
            return
        if self.worker and self.worker.is_alive():
            self.log("Remove refused while worker busy.")
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
            self.log(f"Removed {sid} from UI (retired, reversible).")
        except Exception as e:
            self.log(f"Remove failed: {type(e).__name__}: {str(e)[:100]}")
            return
        self._refresh_container_choices()
        self.on_stats_refresh()

    def on_stats_open_profile(self) -> None:
        import webbrowser as _wb
        try:
            sel = self.stree.selection()
            sid = (sel[0] if sel else "").strip().lower()
        except (tk.TclError, AttributeError):
            return
        if not sid:
            self.log("Open profile: select a row in Stats first.")
            return
        slot = get_slot(sid)
        username = str((slot or {}).get("username") or "").strip().lstrip("u/")
        if not username:
            self.log(f"PROFILE {sid}: no username on slot.")
            return
        self.log(f"PROFILE {username} opening…")

        def _open() -> None:
            try:
                _wb.open("https://www.reddit.com/user/%s/" % username)
            except Exception as e:
                self.log(f"PROFILE open failed: {type(e).__name__}")

        threading.Thread(target=_open, daemon=True).start()

    def on_stop(self) -> None:
        self.stop.set()
        self.log("Stop requested.")

    def on_stats_refresh(self) -> None:
        marks = stats_from_marks(load_marks_posts())
        posts = stats_from_poster(load_poster_items())
        fails = failed_poster_today(load_poster_items())
        summ = today_summary()
        today = summ["day"]
        try:
            self.stats_var.set(
                "Today %s (NYC): %d comments (%s) | %d posts (%s)" % (
                    today, summ["comments"],
                    ",".join(summ["comment_slots"]) or "—",
                    summ["posts"],
                    ",".join(summ["post_slots"]) or "—"))
        except (tk.TclError, AttributeError):
            pass
        try:
            rows = load_slots()
        except Exception:
            rows = []
        cids = {str(r.get("id") or "") for r in rows if str(r.get("id") or "")}
        cids |= set(marks) | set(posts)
        try:
            refused = retired_ids()
        except Exception:
            refused = set()
        cids = {c for c in cids
                if c and c != "(none)" and c.strip().lower() not in refused}
        try:
            for iid in self.stree.get_children():
                self.stree.delete(iid)
            for cid in sorted(cids):
                cm = marks.get(cid, {"total": 0, "by_day": {}, "last": None})
                pm = posts.get(cid, {"total": 0, "by_day": {}, "last": None})
                cl = cm.get("last") or {}
                pl = pm.get("last") or {}
                last = "—"
                if cl or pl:
                    use_c = str(cl.get("when", cl.get("ts", "")) or "") >= str(
                        pl.get("posted_ts", pl.get("ts", "")) or "")
                    last = ("comment r/%s" % cl.get("sub") if use_c and cl
                            else "post %s" % (pl.get("community")
                                              or pl.get("dest", "")))
                self.stree.insert("", "end", iid=cid, values=(
                    cid, cm.get("total", 0),
                    cm.get("by_day", {}).get(today, 0),
                    pm.get("total", 0),
                    pm.get("by_day", {}).get(today, 0),
                    len(fails.get(cid, [])), last[:60]))
        except (tk.TclError, AttributeError):
            pass

    def on_subreddits_refresh(self) -> None:
        try:
            rows = subreddits_table_rows()
        except Exception:
            rows = []
        try:
            for iid in self.sutree.get_children():
                self.sutree.delete(iid)
            for r in rows:
                self.sutree.insert("", "end", values=(
                    r.get("sub", ""), r.get("posts", 0),
                    r.get("assigned", ""), r.get("flair", ""),
                    r.get("titles", "")))
        except (tk.TclError, AttributeError):
            pass

    def on_close(self) -> None:
        self.stop.set()
        if self.phone:
            try:
                self.phone.close()
            except Exception:
                pass
        allow_sleep()
        try:
            if str(open(GUI_LOCK_PATH, encoding="utf-8").read()
                   or "").strip() == str(os.getpid()):
                os.remove(GUI_LOCK_PATH)
        except OSError:
            pass
        self.root.destroy()


GUI_LOCK_PATH = os.path.join(LOG_DIR, "poster_gui.lock")


def _another_poster_running() -> bool:
    """True when a different live process holds the GUI lock. Two poster
    GUIs drive one phone + one queue file with no locking, so the second
    launch must refuse. Lockfile holds the owner's PID; a stale lock from
    a dead process is stolen, never blocking. Pure stdlib, instant."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        pass
    me = os.getpid()
    try:
        fd = os.open(GUI_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(me).encode("utf-8"))
        finally:
            os.close(fd)
        return False
    except FileExistsError:
        pass
    except OSError:
        return False
    try:
        with open(GUI_LOCK_PATH, encoding="utf-8") as f:
            owner = int((f.read() or "").strip() or 0)
    except (OSError, ValueError):
        owner = 0
    if owner and owner != me:
        import ctypes as _ct
        try:
            _proc = _ct.windll.kernel32.OpenProcess(0x1000, False, owner)
            alive = bool(_proc)
            if _proc:
                _ct.windll.kernel32.CloseHandle(_proc)
        except Exception:
            alive = False
        if alive:
            return True
    try:
        os.remove(GUI_LOCK_PATH)
    except OSError:
        pass
    try:
        fd = os.open(GUI_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(me).encode("utf-8"))
        finally:
            os.close(fd)
        return False
    except OSError:
        return True


def main() -> int:
    if _another_poster_running():
        try:
            import ctypes as _ct
            _ct.windll.user32.MessageBoxW(
                None, "Poster is already open (another window is running "
                "this script). Close it first — two copies drive one phone.",
                "Poster already running", 0x40)
        except Exception:
            pass
        print("Poster already running in another window, refusing to start "
              "a second copy.", flush=True)
        return 2
    app = App()
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
