# UTF-8
"""Logged-out detector demo on a0007 (natural specimen).

Detection + restore only. Zero sends/marks/posts/comments, no composer.
Staged-first, stuck-law throughout (budgets, halt+safe-park, no retries).

Usage: python misc/detect7.py
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
os.environ["SKIP_SHOT"] = "1"

from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402
from friend_poster.reddit.reddit_open import find_picker_row, springboard_reddit, tap_home_icon  # noqa: E402
from friend_poster.device.crane_restore import restore_r1  # noqa: E402

SID = "a0007"
EXPECTED_USER = "PleasantCourse3429"
WALL = ("create an account to continue", "verify your email",
        "verification code", "create your username")
LOGIN_MARKERS = ("log in", "sign up", "create account", "continue with",
                 "log into", "create your username")
ACQUIRE_BUDGET = 180.0
DETECT_BUDGET = 60.0


def log(msg: str) -> None:
    print(msg, flush=True)


def login_state(blob: str, expected_user: str) -> str:
    """Account state. healthy = expected user present; dead = login markers;
    unknown = feed context (proves nothing either way - feed authors are not
    account identity); wrong-container = other u/ identity outside any feed.
    Pure (fixture-pinned)."""
    b = (blob or "").lower()
    want = (expected_user or "").strip().lower()
    if want and want in b:
        return "healthy"
    if any(m in b for m in LOGIN_MARKERS):
        return "dead"
    if "popular" in b and ("suggested" in b or "join" in b):
        return "unknown"
    others = {m.group(1).lower() for m in re.finditer(r"u/([A-Za-z0-9_-]{3,})", b)}
    others.discard(want)
    if others:
        return "wrong-container"
    return "unknown"


def pick_avatar(nodes):
    """Top-right account control from the live dump. None when absent -
    callers never hardcode-tap."""
    best = None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if 40 <= n.y <= 130 and n.x >= 600:
            if best is None or n.x > best.x:
                best = n
    return best


def pick_account(nodes):
    """Account control, dump-derived: top-right cluster first (avatar side),
    else top-left cluster (this build puts it left when logged out).
    Returns (xy, side) or (None, "")."""
    right = pick_avatar(nodes)
    if right is not None:
        return right.center(), "right"
    best = None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if 40 <= n.y <= 130 and n.x <= 150:
            if best is None or n.x < best.x:
                best = n
    if best is not None:
        return best.center(), "left"
    return None, ""


def freeze(stem: str, frozen: str) -> None:
    from friend_poster.core.paths import PROJECT_ROOT
    root = str(PROJECT_ROOT)
    for ext in (".json", ".png"):
        src = os.path.join(root, "dumps", stem + ext)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(root, "dumps", frozen + ext))
            log("FROZEN dumps/%s%s" % (frozen, ext))


def revive_control(stack, phone) -> int:
    """Reopen + avatar sheet + login probe after a restore. Returns 0 on
    IDENTITY_OK, 2/3 with fingerprint otherwise. No sends/marks."""
    stack.press_home()
    time.sleep(0.7)
    blob, nodes = dump_named(stack, phone, "det7_re_home", log, shot=False)
    icon = springboard_reddit(nodes)
    page = 0
    while icon is None and page < 4:
        stack.interp_flick(640, 700, 110, 700, steps=14, ms=22)
        time.sleep(0.6)
        blob, nodes = dump_named(stack, phone, "det7_re_pg%d" % page, log, shot=False)
        icon = springboard_reddit(nodes)
        page += 1
    if icon is None:
        log("ABORT post-restore icon missing. fingerprint=revive,icon-missing")
        return 2
    tap_home_icon(stack, icon)
    time.sleep(1.4)
    blob, nodes = dump_named(stack, phone, "det7_re_picker", log, shot=False)
    named = find_picker_row(nodes, SID)
    if named is None:
        log("ABORT a0007 missing post-restore. fingerprint=revive,picker-miss")
        stack.press_home()
        return 2
    stack.tap_xy(named.center()[0], named.center()[1])
    time.sleep(2.4)
    blob, nodes = dump_named(stack, phone, "det7_re_feed", log, shot=False)
    if any(w in (blob or "").lower() for w in WALL):
        log("REVIVE FAIL: login wall still up. fingerprint=revive,still-dead")
        stack.press_home()
        return 3
    av_xy, av_side = pick_account(nodes)
    if av_xy is None:
        log("REVIVE UNCLEAR: no account control. fingerprint=revive,no-avatar")
        stack.press_home()
        return 3
    log("REVIVE account control (%s side) at %s" % (av_side, av_xy))
    stack.tap_xy(*av_xy)
    time.sleep(2.0)
    blob, nodes = dump_named(stack, phone, "det7_re_sheet", log, shot=True)
    state = login_state(blob, EXPECTED_USER)
    log("REVIVE state=%s" % state)
    if state != "healthy":
        log("REVIVE FAIL: u/%s absent. fingerprint=revive,%s"
            % (EXPECTED_USER, state))
        stack.press_home()
        return 3
    freeze("det7_re_sheet", "det7_revived_frozen")
    log("IDENTITY_OK u/%s - login probe pass, a0007 rejoins rotation."
        % EXPECTED_USER)
    stack.press_home()
    return 0


def main() -> int:
    from_open = "--from-open" in sys.argv
    revive_only = "--revive-only" in sys.argv
    t0 = time.monotonic()
    phone = Phone(log=log)
    stack = None
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            log("ABORT no tap backend. fingerprint=detect,no-tap")
            return 2
        if revive_only:
            log("REVIVE-ONLY control run.")
            return revive_control(stack, phone)
        if from_open:
            log("RESUME from open Reddit (staging skipped).")
            blob, nodes = dump_named(stack, phone, "det7_feed", log, shot=False)
        else:
            # --- open container (no login assumed) ---
            stack.press_home()
            time.sleep(0.7)
            blob, nodes = dump_named(stack, phone, "det7_home", log, shot=False)
            icon = springboard_reddit(nodes)
            if icon is None:
                stack.press_home()
                time.sleep(0.6)
                blob, nodes = dump_named(stack, phone, "det7_home2", log, shot=False)
                icon = springboard_reddit(nodes)
            page = 0
            while icon is None and page < 4:
                stack.interp_flick(640, 700, 110, 700, steps=14, ms=22)
                time.sleep(0.6)
                blob, nodes = dump_named(stack, phone, "det7_pg%d" % page, log, shot=False)
                icon = springboard_reddit(nodes)
                page += 1
            if icon is None:
                log("ABORT Reddit icon missing. fingerprint=detect,icon-missing")
                stack.press_home()
                return 2
            tap_home_icon(stack, icon)
            time.sleep(1.4)
            blob, nodes = dump_named(stack, phone, "det7_picker", log, shot=False)
            named = find_picker_row(nodes, SID)
            if named is None:
                log("ABORT a0007 missing from picker. fingerprint=detect,picker-miss")
                stack.press_home()
                return 2
            stack.tap_xy(named.center()[0], named.center()[1])
            time.sleep(2.4)
            blob, nodes = dump_named(stack, phone, "det7_feed", log, shot=False)
        if time.monotonic() - t0 > ACQUIRE_BUDGET:
            log("ABORT over ACQUIRE budget. fingerprint=detect,over-budget")
            stack.press_home()
            return 2
        log("STAGED %s (login unassumed) in %.0fs." % (SID, time.monotonic() - t0))
        # --- account control -> sheet (skip the tap when a menu is open) ---
        t_det = time.monotonic()
        if any(m in (blob or "").lower() for m in LOGIN_MARKERS):
            log("Menu already open with login markers - capturing sheet as-is.")
            blob, nodes = dump_named(stack, phone, "det7_sheet", log, shot=True)
            xy, side = "menu-open", "menu"
        else:
            xy, side = pick_account(nodes)
            if xy is None:
                dump_named(stack, phone, "det7_noavatar", log, shot=True)
                log("ABORT no account control in dump (refusing blind tap). "
                    "fingerprint=detect,no-avatar-node")
                stack.press_home()
                return 2
            log("ACCOUNT control from dump (%s side) at %s" % (side, xy))
            stack.tap_xy(*xy)
            time.sleep(2.0)
            blob, nodes = dump_named(stack, phone, "det7_sheet", log, shot=True)
        state = login_state(blob, EXPECTED_USER)
        el = time.monotonic() - t_det
        log("DETECT state=%s elapsed=%.0fs" % (state, el))
        if el > DETECT_BUDGET:
            log("ABORT over DETECT budget. fingerprint=detect,over-budget")
            stack.press_home()
            return 2
        if state == "wrong-container":
            log("ABORT DIFFERENT user on sheet - not proceeding. "
                "fingerprint=detect,wrong-container")
            stack.press_home()
            return 2
        if state == "healthy":
            log("SPECIMEN HEALTHY (u/%s present) - no restore needed. "
                "fingerprint=detect,healthy" % EXPECTED_USER)
            freeze("det7_sheet", "det7_healthy_frozen")
            stack.press_home()
            return 0
        if state != "dead":
            log("ABORT unknown sheet state. fingerprint=detect,unknown-sheet")
            stack.press_home()
            return 2
        # --- dead ground truth frozen, restore from a0007's own file ---
        freeze("det7_sheet", "det7_dead_frozen")
        log("DEAD signature pinned (dumps/det7_dead_frozen.*). Restoring a0007 "
            "from Reddit_r7_09.15.2026_05-57-50.cranect (own file only).")
        restore_r1(stack, phone, SID)
        log("RESTORE-ran, proving revival.")
        return revive_control(stack, phone)
    except PhoneError as e:
        log("ABORT PhoneError: " + str(e))
        return 2
    except KeyboardInterrupt:
        log("INTERRUPT - safe-parking.")
        try:
            if stack is not None:
                stack.press_home()
        except Exception:
            pass
        return 130
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
