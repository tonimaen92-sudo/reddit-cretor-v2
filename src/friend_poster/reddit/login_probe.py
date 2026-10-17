# UTF-8
"""No-login detector (phase 1 - detection ONLY, feeds phase-2 restore).

Per slot: open container -> locate avatar top-right (per live dump, never
hardcoded) -> read state. Zero sends/marks/posts, zero container writes.
Verdicts are machine-readable JSON (status enum + evidence + slot + ts):

  {"tool": "login_probe", "slot": ..., "status": "HEALTHY|NO_LOGIN|WRONG_USER|UNKNOWN",
   "expected": ..., "seen": ..., "evidence": [...], "dump": ..., "ts": ..., "build": ...}

Usage: python misc/login_probe.py a0007 [a0003 ...]
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
os.environ["SKIP_SHOT"] = "1"

from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.core.slots import get_slot, retired_ids  # noqa: E402
from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named  # noqa: E402
from friend_poster.reddit.reddit_open import find_picker_row, springboard_reddit, tap_home_icon  # noqa: E402

BUILD_TAG = "20260926-login-probe"
ACQUIRE_BUDGET = 180.0
LOGIN_MARKERS = ("log in", "login", "logged out", "create an account to continue",
                 "verify your email", "create your username")


def log(msg: str) -> None:
    print(msg, flush=True)


def find_avatar(nodes, username: str = ""):
    """Avatar control top-right carrying the username, located per live
    dump only. DEMOTED path: username match or None (never taps status-bar
    lookalikes again)."""
    want = (username or "").strip().lower()
    if not want:
        return None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        x, y = n.center()
        if y <= 25 or y > 220 or x < 400:
            continue
        t = collapsed_text(n.labels())
        if want in t.lower():
            return (x, y, t)
    return None


def find_hamburger(nodes):
    """Drawer control top-left (x<260, 30<y<220), per live dump, never
    hardcoded. Prefers a menu-glyph text node; else the topmost-leftmost
    control in the band; else None (-> avatar fallback)."""
    cands = []
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        x, y = n.center()
        if y < 30 or y > 220 or x > 260:
            continue
        t = collapsed_text(n.labels()).strip()
        if t.lower() in ("=", "≡", "☰", "menu", "|||"):
            return (x, y, t)
        cands.append((y, x, t))
    if not cands:
        return None
    cands.sort()
    return (cands[0][1], cands[0][0], cands[0][2])


def drawer_markers(blob: str, expected: str) -> list:
    """Drawer-open evidence: expected user rows, Log-in entry, profile menu.
    A feed-switcher menu (home/popular/...) reports feedmenu, NOT a drawer."""
    low = (blob or "").lower()
    exp = (expected or "").strip().lower()
    found: list = []
    if exp and exp in low:
        found.append("user:" + exp)
    for m in LOGIN_MARKERS:
        if m in low:
            found.append("login:" + m)
            break
    if "my profile" in low:
        found.append("menu:my-profile")
    if all(w in low for w in ("home", "popular", "watch", "news", "latest")):
        found.append("feedmenu")
    return found


def drawer_closed_ok(pre_blob: str, post_blob: str, expected: str) -> tuple:
    """Hygiene gate: drawer evidence gone AND feed context back. Returns
    (ok, detail). Dirty -> abort, never handed downstream."""
    marks = drawer_markers(post_blob, expected)
    lingering = [m for m in marks if m != "feedmenu"]
    if lingering:
        return False, "drawer-lingering:" + "|".join(lingering)
    low = (post_blob or "").lower()
    feed_ok = (post_blob or "") == (pre_blob or "") or any(
        k in low for k in ("best comments", "add a comment",
                           "join the conversation", "latest", "home"))
    if not feed_ok:
        return False, "feed-context-missing"
    return True, "clean-return"


def login_verdict(blob: str, nodes, expected: str, avatar=None) -> tuple:
    """Pure state reader. Returns (status, seen, evidence list).

    HEALTHY: expected username present. WRONG_USER: a different u/<user>
    present. NO_LOGIN: login markers, no username. UNKNOWN: none of the
    above (stuck law treats it as abort, never as healthy)."""
    b = (blob or "")
    low = b.lower()
    exp = (expected or "").strip().lower()
    ev: list = []
    if avatar is not None:
        ev.append("avatar@%d,%d %s" % (avatar[0], avatar[1], str(avatar[2])[:40]))
    else:
        ev.append("avatar-missing")
    tokens = [t for t in re.findall(r"u/([a-z0-9_\-]+)", low) if len(t) >= 3]
    if exp and exp in low:
        ev.append("expected-user-visible")
        return "HEALTHY", exp, ev
    others = [t for t in tokens if t != exp]
    if exp and others:
        ev.append("other-user-visible")
        return "WRONG_USER", others[0], ev
    marks = [m for m in LOGIN_MARKERS if m in low]
    if marks:
        ev.append("login-markers:" + "|".join(marks[:3]))
        return "NO_LOGIN", marks[0], ev
    if others:
        ev.append("user-visible-no-expected")
        return "WRONG_USER", others[0], ev
    ev.append("no-user-no-markers")
    return "UNKNOWN", "", ev


def verdict_record(slot: str, status: str, expected: str, seen: str,
                   evidence: list, dump: str, path: str = "drawer") -> dict:
    return {"tool": "login_probe", "slot": slot, "status": status,
            "expected": expected, "seen": seen, "evidence": evidence,
            "dump": dump, "path": path,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "build": BUILD_TAG}


def probe_slot(stack: TapStack, phone: Phone, sid: str, expected: str) -> dict:
    """Open container, tap avatar, read profile. Detection only."""
    t0 = time.monotonic()
    stack.press_home()
    time.sleep(0.7)
    blob, nodes = dump_named(stack, phone, "90_probe_home", log, shot=False)
    icon = springboard_reddit(nodes)
    if icon is None:
        stack.press_home()
        time.sleep(0.6)
        blob, nodes = dump_named(stack, phone, "91_probe_home2", log, shot=False)
        icon = springboard_reddit(nodes)
    page = 0
    while icon is None and page < 4:
        stack.interp_flick(640, 700, 110, 700, steps=14, ms=22)
        time.sleep(0.6)
        blob, nodes = dump_named(stack, phone, "92_probe_pg%d" % page, log, shot=False)
        icon = springboard_reddit(nodes)
        page += 1
    if icon is None:
        return verdict_record(sid, "UNKNOWN", expected, "no-icon",
                              ["springboard-icon-missing"], "92_probe_pg", "none")
    tap_home_icon(stack, icon)
    time.sleep(1.4)
    blob, nodes = dump_named(stack, phone, "93_probe_picker", log, shot=False)
    named = find_picker_row(nodes, sid)
    if named is None:
        stack.press_home()
        return verdict_record(sid, "UNKNOWN", expected, "no-picker-row",
                              ["container-missing-from-picker"], "93_probe_picker",
                              "none")
    stack.tap_xy(named.center()[0], named.center()[1])
    time.sleep(2.4)
    blob, nodes = dump_named(stack, phone, "94_probe_feed", log, shot=False)
    for rnd in range(3):  # feed dump <=5 nodes = app still launching
        if len(nodes or []) > 5:
            break
        log("PROBE feed loading (%d nodes) -> wait + redump %d/3"
            % (len(nodes or []), rnd + 1))
        time.sleep(2.0)
        blob, nodes = dump_named(stack, phone, "94_probe_feed_r%d" % rnd,
                                 log, shot=False)
    if len(nodes or []) <= 5:
        stack.press_home()
        return verdict_record(sid, "UNKNOWN", expected, "feed-loading",
                              ["feed-never-loaded"], "94_probe_feed", "none")
    if time.monotonic() - t0 > ACQUIRE_BUDGET:
        stack.press_home()
        return verdict_record(sid, "UNKNOWN", expected, "over-budget",
                              ["acquire-budget-exceeded"], "94_probe_feed", "none")
    ham = find_hamburger(nodes)
    if ham is not None:
        stack.tap_xy(int(ham[0]), int(ham[1]))
        time.sleep(1.6)
        blob, nodes = dump_named(stack, phone, "95_probe_drawer", log, shot=False)
        marks = drawer_markers(blob, expected)
        dev = ["drawer@%d,%d %s" % (ham[0], ham[1], str(ham[2])[:40]),
               "drawer-markers:" + ("|".join(marks) if marks else "none")]
        if any(m != "feedmenu" for m in marks):
            status, seen, ev = login_verdict(blob, nodes, expected, None)
            ev = dev + ev
            ham2 = find_hamburger(nodes)
            if ham2 is None:
                stack.press_home()
                return verdict_record(sid, "UNKNOWN", expected, "drawer-dirty",
                                      ev + ["hygiene:no-close-control"],
                                      "95_probe_drawer", "drawer")
            stack.tap_xy(int(ham2[0]), int(ham2[1]))
            time.sleep(1.2)
            blob2, _n2 = dump_named(stack, phone, "96_probe_return", log, shot=False)
            ok, detail = drawer_closed_ok(blob, blob2, expected)
            ev = ev + ["hygiene:" + detail]
            stack.press_home()
            if not ok:
                return verdict_record(sid, "UNKNOWN", expected, "drawer-dirty",
                                      ev, "96_probe_return", "drawer")
            return verdict_record(sid, status, expected, seen, ev,
                                  "95_probe_drawer", "drawer")
        log("PROBE hamburger opened no drawer (%s) -> avatar fallback"
            % ("|".join(marks) if marks else "no-markers"))
    avatar = find_avatar(nodes, expected)
    if avatar is None:
        stack.press_home()
        return verdict_record(sid, "UNKNOWN", expected, "avatar-missing",
                              ["avatar-missing"], "94_probe_feed", "avatar")
    stack.tap_xy(int(avatar[0]), int(avatar[1]))
    time.sleep(1.6)
    blob, nodes = dump_named(stack, phone, "96_probe_avatar", log, shot=False)
    status, seen, ev = login_verdict(blob, nodes, expected, avatar)
    stack.press_home()  # safe-park, profile left unmodified
    return verdict_record(sid, status, expected, seen, ev, "96_probe_avatar",
                          "avatar")


def main(argv: list) -> int:
    sids = [a.strip().lower() for a in argv if a.strip()]
    if not sids:
        print("usage: python misc/login_probe.py <slot> [slot ...]")
        return 2
    phone = Phone(log=log)
    phone.connect("", also_wda=False)
    stack = TapStack(phone, log)
    log("BACKEND " + stack.attach())
    if not stack.can_tap():
        log("ABORT no tap backend. Open XXTouch Elite.")
        phone.close()
        return 2
    code = 0
    try:
        for sid in sids:
            if sid in retired_ids():
                rec = verdict_record(sid, "UNKNOWN", "", "retired", ["slot-retired"], "-",
                                     "none")
                print("VERDICT " + json.dumps(rec), flush=True)
                code = 3
                continue
            slot = get_slot(sid)
            expected = str((slot or {}).get("username") or "").strip()
            if not slot or not expected:
                rec = verdict_record(sid, "UNKNOWN", "", "no-username",
                                     ["roster-username-missing"], "-", "none")
                print("VERDICT " + json.dumps(rec), flush=True)
                code = 3
                continue
            try:
                rec = probe_slot(stack, phone, sid, expected)
            except PhoneError as e:
                rec = verdict_record(sid, "UNKNOWN", expected, "phone-error",
                                     [str(e)[:150]], "-", "none")
                code = 3
            print("VERDICT " + json.dumps(rec), flush=True)
            if rec["status"] == "WRONG_USER":
                log("WRONG_USER on %s - stopping run, never proceeding on a wrong account."
                    % sid)
                code = 3
                break
            if rec["status"] != "HEALTHY":
                code = 3
            time.sleep(0.8)
    finally:
        try:
            stack.press_home()
        except Exception:
            pass
        phone.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
