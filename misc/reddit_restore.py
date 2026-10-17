# UTF-8
"""Shared login/restore flow for commenter + poster (same process, one copy).

Workflow per dead session: drawer detection is the caller's job; then
login_via_accounts (in-app ACCOUNTS switch, no password) -> Crane
auto_restore fallback -> post-restore detection -> accounts switch again.
Nothing here posts, sends, or marks. Dump-first throughout; any
inconclusive state aborts instead of guessing.

  import reddit_restore  (no GUI, no phone at import)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DUMP_DIR, LOG_DIR  # noqa: E402
from reddit_open import switcher_changed  # noqa: E402
from slots import get_slot  # noqa: E402
from ssh_device import PhoneError  # noqa: E402
from ui_tree import collapsed_text, dump_named, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

DRAWER_LOGIN_MARKER = "log in to add your communities"
DRAWER_INCONCLUSIVE = "drawer didn't open (probe inconclusive)"
BACKUP_MAP_PATH = os.path.join(LOG_DIR, "slot_backup_map.json")
RESTORE_BUDGET = 180.0
RESTORE_ATTEMPTS = 4
# Login/verify walls (mirrors commenter WALL; keep in sync).
WALL = ("create an account to continue", "verify your email",
        "verification code", "create your username")


def _dump_saved(stem: str) -> bool:
    """True when dump_named persisted stem.* under dumps/. Pure-ish."""
    try:
        for ext in (".json", ".xml", ".txt"):
            if os.path.isfile(os.path.join(DUMP_DIR, stem + ext)):
                return True
        return False
    except OSError:
        return False


def drawer_logged_out(blob: str) -> bool:
    """Drawer ground truth: the login marker means no session, even when the
    feed renders fine. Whitespace-collapsed (OCR wraps mid-phrase).
    Case-insensitive. Pure."""
    return DRAWER_LOGIN_MARKER in re.sub(r"\s+", " ", str(blob or "")).lower()


def drawer_text(blob, nodes) -> str:
    """Whitespace-collapsed drawer text from node texts + raw blob.
    Markers match against this, never the raw blob alone. Pure-ish."""
    parts = []
    for n in nodes or []:
        try:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            t = collapsed_text(n.labels())
        except Exception:
            continue
        if t:
            parts.append(t)
    parts.append(blob or "")
    return re.sub(r"\s+", " ", " ".join(parts)).lower()


def drawer_communities(text: str) -> set:
    """Distinct r/sub tokens in drawer text. Pure."""
    return set(re.findall(r"r/([a-z0-9_]{2,})", text or ""))


def drawer_verdict(text: str, username: str = "") -> str:
    """Pure verdict on collapsed drawer text: 'out' | 'in' | ''.
    Marker first; then username/header/community structure."""
    t = (text or "").lower()
    if DRAWER_LOGIN_MARKER in t:
        return "out"
    if (username or "").strip() and username.casefold() in t:
        return "in"
    for alive in ("your communities", "create community", "custom feeds",
                  "recently visited"):
        if alive in t:
            return "in"
    if len(drawer_communities(t)) >= 3:
        return "in"
    return ""


def resolve_backup_file(sid: str, registry: dict) -> str:
    """Frozen-authority lookup: verified PC backup filename for a slot, or ''.
    No derivation, no guessing - unlisted slots cannot auto-restore. Pure."""
    try:
        row = (registry or {}).get((sid or "").strip().lower(), {})
        if not isinstance(row, dict):
            return ""
        verified = row.get("verified") or []
        if verified and isinstance(verified, list) and verified[0]:
            return str(verified[0])
    except Exception:
        pass
    return ""


def load_backup_map() -> dict:
    try:
        with open(BACKUP_MAP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def list_row(nodes, want: str):
    """Container-list row: exact name match below the Active-header zone
    (y>400). The Active value duplicates the name at y~233; the header
    itself may be scrolled off, so position floor (not header sighting)
    is the guard. Pure."""
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()).strip().lower() == (want or "").strip().lower() \
                and n.y > 400:
            return n
    return None


def result_rows(nodes, token: str):
    """File-result rows for a typed query: the results band only
    (300 < y < 800). Excludes the search-field echo, the iOS
    `Contains "..."` section header, and keyboard rows. Pure.

    Long filenames OCR-wrap across stacked fragments (e.g. 'Reddit_a003'
    + '4_09…. anect'), so nodes within 50px vertically are joined before
    matching; the top fragment anchors the tap. Count/canonical guards
    downstream still refuse ambiguity."""
    tok = (token or "").lower().replace(" ", "")
    if not tok:
        return []
    band = []
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if not (300 < n.y < 800):
            continue
        if collapsed_text(n.labels()).lower().replace(" ", "").startswith(
                "contains"):
            continue
        band.append(n)
    band.sort(key=lambda n: (n.y, n.x))
    groups: list = []
    for n in band:
        if groups and abs(n.y - groups[-1][-1].y) <= 50:
            groups[-1].append(n)
        else:
            groups.append([n])
    out = []
    for g in groups:
        joined = "".join(
            collapsed_text(n.labels()) for n in sorted(g, key=lambda n: n.x)
        ).lower().replace(" ", "")
        if tok in joined:
            out.append(sorted(g, key=lambda n: n.y)[0])
    return out


def canonical_pick(names: list) -> int:
    """Among file rows, the canonical Crane export (`reddit_...`) wins over
    renamed copies (`a0014_Reddit_...`). Returns index or -1. Pure."""
    hits = [i for i, t in enumerate(names or [])
            if (t or "").lstrip().lower().replace(" ", "").startswith("reddit_")]
    return hits[0] if len(hits) == 1 else -1


def accounts_row_username(text: str) -> str:
    """Username from an ACCOUNTS-sheet row ('u/<name>' OCR, often tripled).
    '' when the row is not an account row. Pure."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    m = re.search(r"\bu/([A-Za-z0-9_-]+)", t)
    return m.group(1) if m else ""


def search_field(nodes):
    """Crane/file-browser search field: exact Search label, else a
    search-marked row above the fold. None when absent (never guess)."""
    hit = find_label_exact(nodes or [], ("Search",))
    if hit is None:
        hit = find_label_exact(nodes or [], ("Q Search",))
    if hit is not None:
        return hit
    for n in nodes or []:
        if n.ntype != "ocr-line" and n.on_screen() \
                and "search" in collapsed_text(n.labels()).lower() and n.y < 300:
            return n
    return None


def drawer_login_probe(stack, phone, lg, sid: str, stem: str) -> str:
    """Drawer ground truth probe: one hamburger tap, read the drawer,
    tap hamburger again to return to the feed (never exit Reddit).
    Marker = logged out even when the feed renders. Both dumps must be
    saved or the verdict is inconclusive (never restore on inconclusive)."""
    stack.tap_xy(56, 83)
    time.sleep(1.2)
    blob, _nodes = dump_named(stack, phone, stem + "_drawer", lg, shot=False)
    if not _dump_saved(stem + "_drawer"):
        lg("DRAWER %s missing dump %s_drawer, inconclusive" % (sid, stem))
        try:
            stack.tap_xy(56, 83)
            time.sleep(1.0)
        except Exception:
            pass
        return DRAWER_INCONCLUSIVE
    text = drawer_text(blob, _nodes)
    low = text
    stack.tap_xy(56, 83)
    time.sleep(1.0)
    dump_named(stack, phone, stem + "_back", lg, shot=False)
    if not _dump_saved(stem + "_back"):
        lg("DRAWER %s missing dump %s_back, inconclusive" % (sid, stem))
        return DRAWER_INCONCLUSIVE
    if drawer_logged_out(text):
        lg("DRAWER %s marker hit '%s' in %s_drawer" % (sid, DRAWER_LOGIN_MARKER, stem))
        return "logged-out at feed"
    if drawer_verdict(text) == "in":
        lg("DRAWER %s alive in %s_drawer" % (sid, stem))
        return ""
    lg("DRAWER %s inconclusive, no marker in %s_drawer" % (sid, stem))
    return DRAWER_INCONCLUSIVE


def login_via_accounts(stack, phone, lg, sid: str, stem: str) -> str:
    """In-app session switch: avatar -> Sign up/Log in -> tap the
    slot's own u/<username> row. No password, no Crane. Returns '' when
    the drawer verifies alive, 'account row missing' when the username
    is not on the sheet (caller runs normal restore), else the reason."""
    slot = get_slot(sid)
    username = str((slot or {}).get("username") or "").strip()
    if not username:
        return "no username on slot"
    lg("ACCOUNTS trying in-app switch for %s." % sid)
    blob, nodes = dump_named(stack, phone, stem + "_feed", lg, shot=False)
    cands = [n for n in nodes or []
             if n.ntype != "ocr-line" and n.on_screen()
             and 40 <= n.y <= 140 and n.x > 400 and n.w < 200]
    if not cands:
        # Dead sessions often sit on a login wall with no feed search
        # icon: one bounded back-tap toward the feed, then re-check.
        # Never more than one tap, never blind.
        back = None
        for n in nodes or []:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            if collapsed_text(n.labels()) == "<" and n.y < 140 and n.x < 100:
                back = n
                break
        if back is not None:
            lg("ACCOUNTS no search anchor, one back-tap toward feed")
            stack.tap_xy(*back.center())
            time.sleep(1.5)
            blob, nodes = dump_named(stack, phone, stem + "_feed_back", lg,
                                     shot=False)
            cands = [n for n in nodes or []
                     if n.ntype != "ocr-line" and n.on_screen()
                     and 40 <= n.y <= 140 and n.x > 400 and n.w < 200]
    if not cands:
        return "search anchor missing"
    q = min(cands, key=lambda c: c.x)
    stack.tap_xy(min(q.x + q.w + 90, 700), q.center()[1])
    time.sleep(1.4)
    blob, nodes = dump_named(stack, phone, stem + "_sheet", lg, shot=False)
    sheet = None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels()).lower()
        if "sign up" in t and "log in" in t:
            sheet = n
            break
    if sheet is None:
        return "signup sheet missing"
    stack.tap_xy(*sheet.center())
    time.sleep(1.5)
    blob, nodes = dump_named(stack, phone, stem + "_accounts", lg, shot=False)
    want = username.casefold()
    hit = None
    seen: list = []
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        got = accounts_row_username(collapsed_text(n.labels()))
        if not got:
            continue
        if got not in seen:
            seen.append(got)
        if got.casefold() == want:
            hit = n
            break
    if hit is None:
        try:
            stack.tap_xy(56, 83)
            time.sleep(0.8)
        except Exception:
            pass
        if seen:
            return ("account row mismatch (sheet: %s, want u/%s)"
                    % (", ".join("u/" + s for s in seen[:3]), username))
        return "account row missing"
    stack.tap_xy(*hit.center())
    time.sleep(2.5)
    blob, nodes = dump_named(stack, phone, stem + "_switched", lg, shot=False)
    reason = drawer_login_probe(stack, phone, lg, sid, stem + "_verify")
    if reason:
        return "switch unverified (%s)" % reason
    lg("ACCOUNTS %s session alive (u/%s)." % (sid, username))
    return ""


def resume_reddit_card(stack, phone, lg, sid: str, stem: str) -> str:
    """Post-restore reopen via the switcher card (system-wide rule).
    Taps the already-opened Reddit card, never a fresh icon launch.
    '' when the restored feed verifies (drawer re-probe, no restore)."""
    stack.press_home()
    time.sleep(0.7)
    blob_home, _ = dump_named(stack, phone, stem + "_switcher_home", lg,
                              shot=False)
    stack.double_home(280)
    time.sleep(0.5)
    blob, nodes = dump_named(stack, phone, stem + "_switcher", lg, shot=False)
    if not switcher_changed(blob_home, blob):
        stack.double_home(280)
        time.sleep(0.5)
        blob, nodes = dump_named(stack, phone, stem + "_switcher_retry", lg,
                                 shot=False)
        if not switcher_changed(blob_home, blob):
            return "switcher did not open"
    crane = None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels()).strip().lower()
        if t in ("crane", "settings") and 40 < n.y < 600:
            if crane is None or n.y < crane.y:
                crane = n
    if crane is not None:
        cx, _cy = crane.center()
        lg("Switcher: flicking Crane card away from its body.")
        stack.interp_flick(cx, 780, cx, 60, steps=16, ms=22)
        time.sleep(0.9)
        blob, nodes = dump_named(stack, phone, stem + "_switcher_solo", lg,
                                 shot=False)
    card = None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) == "reddit" and 40 < n.y < 600:
            if card is None or n.y < card.y:
                card = n
    if card is None:
        return "Reddit card missing from switcher"
    stack.tap_xy(*card.center())
    time.sleep(2.4)
    blob, _nodes = dump_named(stack, phone, stem + "_refeed", lg, shot=False)
    if any(w in (blob or "").lower() for w in WALL):
        return "logged-out at feed"
    dead = drawer_login_probe(stack, phone, lg, sid, stem + "_relogin")
    if dead:
        return dead
    return ""


def auto_restore(stack, phone, lg, sid: str, stem: str) -> str:
    """Restore-on-dead-session: drawer-proven logout only, one attempt.
    Frozen-registry file, phone-side search must match exactly 1, tap
    once, then gated confirm: restore-progress/container-detail proceeds
    to re-verify; any replace/confirm dialog or unknown screen halts
    (never guess a destructive tap). Reopens via the switcher card
    (system-wide rule, never icon relaunch). Returns '' when the session
    verifies after restore, else the abort reason."""
    t_r = time.monotonic()
    slot = get_slot(sid)
    alias = str((slot or {}).get("email_label") or "").strip() or sid
    fname = resolve_backup_file(sid, load_backup_map())
    if fname:
        lg("RESTORE %s from %s" % (sid, fname))
    else:
        # No verified entry: discover the file on the phone by alias.
        # The alias search + exactly-1 + canonical-reddit_ guards below
        # are the same safety as the verified path; ambiguity still
        # aborts (never guesses). The registry stays untouched.
        lg("RESTORE %s: no verified backup, discovering on-phone by alias"
           % sid)
    stack.press_home()
    time.sleep(0.7)
    code, _out = phone.exec_ok("uiopen 'prefs:root=Crane'", timeout=15)
    if code != 0:
        return "Crane settings unreachable"
    time.sleep(3.0)
    blob, nodes = dump_named(stack, phone, stem + "_crane", lg, shot=False)
    if "crane" not in (blob or "").lower():
        lg("RESTORE not in Crane pane, alternate deep-link once")
        phone.exec_ok("uiopen 'App-prefs:root=Crane'", timeout=15)
        time.sleep(3.0)
        blob, nodes = dump_named(stack, phone, stem + "_crane_alt", lg,
                                 shot=False)
    apps = find_label_exact(nodes, ("Applications",))
    if apps is None:
        # Crane pane opens scrolled down (Notifications/Backup&Restore
        # visible); Applications sits at the top. Scroll up bounded.
        prev = blob
        still = 0
        for _i in range(4):
            stack.interp_flick(375, 400, 375, 900, steps=14, ms=22)
            time.sleep(1.0)
            blob, nodes = dump_named(
                stack, phone, "%s_crane_top%d" % (stem, _i), lg, shot=False)
            apps = find_label_exact(nodes, ("Applications",))
            if apps is not None:
                lg("RESTORE Applications HIT after scroll-up %d" % (_i + 1))
                break
            if blob == prev:
                still += 1
                if still >= 2:
                    break
            else:
                still = 0
            prev = blob
    if apps is None:
        return "Crane Applications missing"
    stack.tap_xy(*apps.center())
    time.sleep(2.0)
    blob, nodes = dump_named(stack, phone, stem + "_apps", lg, shot=False)
    sfield = search_field(nodes)
    if sfield is None:
        return "Crane search missing"
    stack.tap_xy(*sfield.center())
    time.sleep(1.2)
    blob, nodes = dump_named(stack, phone, stem + "_search", lg, shot=False)
    from fieldvalue import keyboard_present
    if not keyboard_present(nodes):
        return "keyboard absent, no blind typing"
    stack.type_human("reddit", ms=95)
    time.sleep(1.0)
    blob, nodes = dump_named(stack, phone, stem + "_typed", lg, shot=False)
    row = None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if "reddit" in collapsed_text(n.labels()).lower() and n.y > 150:
            row = n
            break
    if row is None:
        return "Reddit app row missing"
    stack.tap_xy(*row.center())
    time.sleep(2.0)
    target = None
    prev = ""
    for rnd in range(8):
        blob, nodes = dump_named(stack, phone, "%s_list%d" % (stem, rnd), lg,
                                 shot=False)
        target = list_row(nodes, sid)
        if target is not None:
            break
        if blob == prev:
            lg("RESTORE list end reached (frozen dump)")
            break
        prev = blob
        stack.swipe(375, 900, 375, 800)
        time.sleep(0.8)
    if target is None:
        return "slot row missing from Crane list"
    stack.tap_xy(*target.center())
    time.sleep(2.0)
    frm = None
    for rnd in range(3):
        stack.swipe(375, 1000, 375, 400)
        time.sleep(0.9)
        blob, nodes = dump_named(stack, phone, "%s_bot%d" % (stem, rnd), lg,
                                 shot=False)
        for n in nodes or []:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            t = collapsed_text(n.labels()).strip().lower()
            if "backup" in t and "from" in t:
                frm = n
                break
        if frm is not None:
            break
    if frm is None:
        return "restore FROM row missing"
    stack.tap_xy(*frm.center())
    time.sleep(2.0)
    blob, nodes = dump_named(stack, phone, stem + "_files", lg, shot=False)
    sfield = search_field(nodes)
    if sfield is None:
        return "file search missing"
    stack.tap_xy(*sfield.center())
    time.sleep(1.0)
    stack.type_human(alias, ms=95)
    time.sleep(1.0)
    blob, nodes = dump_named(stack, phone, stem + "_filed", lg, shot=True)
    tok = alias.lower().replace(" ", "")
    matches = result_rows(nodes, tok)
    if len(matches) != 1:
        names = [collapsed_text(n.labels()) for n in matches]
        pick = canonical_pick(names)
        if pick < 0:
            return "backup file count != 1 (%d)" % len(matches)
        lg("CANONICAL pick %r" % names[pick][:60])
        matches = [matches[pick]]
    _mx, _my, _mw = int(matches[0].x), int(matches[0].y), int(matches[0].w)
    _ix, _iy = _mx + _mw // 2 + 30, _my - 150
    if not (60 <= _ix <= 320 and 340 <= _iy <= 620):
        return "file icon out of bounds, refusing tap"
    lg("ICON-TAP (%d,%d) hardcode" % (_ix, _iy))
    stack.tap_xy(_ix, _iy)
    time.sleep(2.5)
    if time.monotonic() - t_r > RESTORE_BUDGET:
        return "restore over budget"
    blob, _nodes = dump_named(stack, phone, stem + "_picked", lg, shot=False)
    low = (blob or "").lower()
    if "warning" in low and "overwrite" in low and "continue" in low:
        cont = None
        for n in _nodes or []:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            if collapsed_text(n.labels()).strip().lower() == "continue" \
                    and n.y > 500:
                cont = n
                break
        if cont is None:
            return "restore Continue missing"
        lg("RESTORE Continue at %s" % (cont.center(),))
        stack.tap_xy(*cont.center())
        deadline = time.monotonic() + 45.0
        while True:
            time.sleep(5.0)
            blob, _nd = dump_named(stack, phone, stem + "_settle", lg,
                                   shot=False)
            low = (blob or "").lower()
            if not ("warning" in low and "overwrite" in low):
                break
            if time.monotonic() >= deadline:
                return "restore dialog stuck"
        lg("RESTORE dialog cleared, reopening container")
    elif "container name" not in low and "container identifier" not in low:
        return "unknown post-tap screen, manual required"
    if time.monotonic() - t_r > RESTORE_BUDGET:
        return "restore over budget"
    return resume_reddit_card(stack, phone, lg, sid, stem + "_reopen")


def restore_until_alive(stack, phone, lg, sid: str, stem: str,
                        stop=None) -> str:
    """Account-switch first, Crane restore on fallback, then detection +
    accounts switch again between restore attempts. Returns '' when alive,
    else final reason."""
    reason = login_via_accounts(stack, phone, lg, sid, "%s_acct" % stem)
    if not reason:
        return ""
    if reason == "account row missing":
        lg("ACCOUNTS no username row, normal restore")
    elif reason.startswith("account row mismatch"):
        lg("ACCOUNTS %s — never tapping another slot's session, "
           "normal restore" % reason)
    else:
        lg("ACCOUNTS switch failed (%s), normal restore" % reason)
    reason = "logged-out at feed"
    for att in range(1, RESTORE_ATTEMPTS + 1):
        if stop is not None and stop.is_set():
            return "stopped"
        lg("RESTORE attempt %d/%d on %s" % (att, RESTORE_ATTEMPTS, sid))
        reason = auto_restore(stack, phone, lg, sid, "%s_a%d" % (stem, att))
        if not reason:
            return ""
        if reason != "logged-out at feed":
            return reason
        # Post-restore detection + accounts switch before the next attempt.
        retry = login_via_accounts(stack, phone, lg, sid,
                                   "%s_a%d_acct" % (stem, att))
        if not retry:
            return ""
        if retry.startswith("account row mismatch"):
            lg("ACCOUNTS %s — never tapping another slot's session" % retry)
        lg("RESTORE still dead, retrying" if att < RESTORE_ATTEMPTS else
           "RESTORE still dead after %d attempts" % RESTORE_ATTEMPTS)
    return reason
