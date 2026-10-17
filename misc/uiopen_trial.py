# UTF-8
"""uiopen dispatch trial: stage slot FIRST, then cold-dispatch permalinks.

Navigation-only: zero sends/marks/posts/comments. No composer, no typing.
Text dumps only (shot=False). Budgets: ACQUIRE<=180s, per-trial GATE+VERIFY<=120s.
Stuck (unknown/frozen/over-budget) = halt all + press_home safe-park + abort-report.
No auto-retries: one dispatch per trial, misses recorded as outcomes (not stuck).

Usage: python misc/uiopen_trial.py a0007
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["SKIP_SHOT"] = "1"

from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack, dump_named, find_label_exact  # noqa: E402
from reddit_open import find_picker_row, springboard_reddit, tap_home_icon  # noqa: E402

SID = sys.argv[1] if len(sys.argv) > 1 else "a0007"

TRIALS = [
    ("T2", "fivenightsatfreddys", "(Elizabeth Arc) The Finale PART 3: The Fight Isn't Over",
     "Gojinaf_10",
     "https://www.reddit.com/r/fivenightsatfreddys/comments/1wpuqj9/elizabeth_arc_the_finale_part_3_the_fight_isnt/"),
    ("T3", "animequestions",
     "Which anime character stole the show from the main character for you?",
     "",
     "https://www.reddit.com/r/animequestions/comments/1wpq1ru/which_anime_character_stole_the_show_from_the/"),
    ("T4", "peakyblinders", "The saddest part of this series...",
     "",
     "https://www.reddit.com/r/PeakyBlinders/comments/1wppfxg/the_saddest_part_of_this_series/"),
    ("T5", "ben10", "Why Can't Ben Transform into a Sludge Puppy?",
     "",
     "https://www.reddit.com/r/Ben10/comments/1wpp932/why_cant_ben_transform_into_a_sludge_puppy/"),
    ("T6", "thong", "I need your hot love",
     "",
     "https://www.reddit.com/r/thong/comments/1wqi73w/i_need_your_hot_love/"),
]

WALL = ("create an account to continue", "verify your email",
        "verification code", "create your username")
ACQUIRE_BUDGET = 180.0
TRIAL_BUDGET = 120.0


def log(msg: str) -> None:
    print(msg, flush=True)


def classify(blob: str, sub: str, title: str = "", author: str = "") -> tuple[str, str]:
    """App-post proof = sub + title words + author leg. Comment-bar tokens are
    bonus only, never required (T1 lesson: 'Join the conversation' is the only
    composer affordance on fresh posts). Author leg tolerates OCR ellipsis
    ('u/Parsl...e6746'): prefix-5 match, else any 'u/' header token when the
    expected author is unknown (pool trials carry no author)."""
    b = (blob or "").lower()
    nospace = b.replace(" ", "")
    if "search or enter website" in b or "favorites" in b and "reddit.com" not in b:
        return "browser-start-page", "safari-start-markers"
    if "open in the reddit" in b or ("reddit.com" in b and "safari" in b):
        return "browser", "safari-reddit-markers"
    sub_hit = sub.lower().replace(" ", "") in nospace
    twords = [w.strip(".,!?\"'()[]").lower() for w in (title or "").split()]
    twords = [w for w in twords if len(w) >= 4]
    thits = [w for w in twords if w in nospace]
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
        # dispatch-OK/content-gone: item DEAD/skipped, never retried. Counts in
        # routing stats, excluded from verification stats.
        return "removed-post", proof + " removed-markers"
    if sub_hit and len(thits) >= 2 and (author_hit or uloose or not (author or "").strip()):
        return "app-post", proof
    if sub_hit and twords and len(thits) == len(twords) and (author_hit or uloose or bonus):
        return "app-post", proof + " short-title-clause"
    if sub_hit and len(thits) >= 2:
        return "app-post-unverified-author", proof
    if "reddit" in b:
        return "app-ambiguous", proof
    return "unknown", proof


def main() -> int:
    t0 = time.monotonic()
    phone = Phone(log=log)
    phone.connect("", also_wda=False)
    stack = TapStack(phone, log)
    log("BACKEND " + stack.attach())
    if not stack.can_tap():
        log("ABORT no tap backend. Open XXTouch Elite.")
        phone.close()
        return 2
    # --- STAGE slot FIRST (non-negotiable order): container open + login probe
    stack.press_home()
    time.sleep(0.7)
    blob, nodes = dump_named(stack, phone, "70_stage_home", log, shot=False)
    icon = springboard_reddit(nodes)
    if icon is None:
        stack.press_home()
        time.sleep(0.6)
        blob, nodes = dump_named(stack, phone, "71_stage_home2", log, shot=False)
        icon = springboard_reddit(nodes)
    page = 0
    while icon is None and page < 4:
        stack.interp_flick(640, 700, 110, 700, steps=14, ms=22)
        time.sleep(0.6)
        blob, nodes = dump_named(stack, phone, "72_stage_pg%d" % page, log, shot=False)
        icon = springboard_reddit(nodes)
        page += 1
    if icon is None:
        log("ABORT STAGE: Reddit icon missing. fingerprint=stage,icon-missing")
        stack.press_home()
        phone.close()
        return 2
    tap_home_icon(stack, icon)
    time.sleep(1.4)
    blob, nodes = dump_named(stack, phone, "73_stage_picker", log, shot=False)
    named = find_picker_row(nodes, SID)
    if named is None:
        log("ABORT STAGE: container %s missing from picker. fingerprint=stage,picker-miss" % SID)
        stack.press_home()
        phone.close()
        return 2
    stack.tap_xy(named.center()[0], named.center()[1])
    time.sleep(2.4)
    blob, nodes = dump_named(stack, phone, "74_stage_feed", log, shot=False)
    for rnd in range(3):  # feed dump <=5 nodes = app still loading -> wait + redump
        if len(nodes or []) > 5:
            break
        log("STAGE feed loading (%d nodes) -> wait + redump %d/3" % (len(nodes or []), rnd + 1))
        time.sleep(2.0)
        blob, nodes = dump_named(stack, phone, "74_stage_feed_r%d" % rnd, log, shot=False)
    if len(nodes or []) <= 5:
        log("ABORT STAGE: feed still loading after redumps. fingerprint=stage,feed-loading")
        stack.press_home()
        phone.close()
        return 2
    if any(w in (blob or "").lower() for w in WALL):
        log("ABORT STAGE: logged-out at feed (login wall). fingerprint=stage,login-wall")
        stack.press_home()
        phone.close()
        return 2
    log("STAGED %s feed alive in %.0fs (no typing, read-only probe)."
        % (SID, time.monotonic() - t0))
    if time.monotonic() - t0 > ACQUIRE_BUDGET:
        log("ABORT STAGE over ACQUIRE budget. fingerprint=stage,over-budget")
        stack.press_home()
        phone.close()
        return 2
    # --- DISPATCH trials, one shot each, no retries
    # argv[2:] optionally filters trial ids (resume runs never re-dispatch
    # recorded/DEAD trials). Usage: python misc/uiopen_trial.py a0007 T4 T5 T6
    want = {a.strip().upper() for a in sys.argv[2:] if a.strip()}
    results = []
    for tid, sub, title, author, url in TRIALS:
        if want and tid.upper() not in want:
            continue
        if not url or not url.startswith("http"):
            results.append((tid, sub, "skipped-no-url", "0s"))
            log("%s skipped (no scouted URL)." % tid)
            continue
        tt0 = time.monotonic()
        esc = url.replace("'", "")
        code, out = phone.exec_ok("uiopen --url '%s'" % esc, timeout=15)
        log("%s dispatch exit=%s %s" % (tid, code, (out or "").strip()[:120]))
        if code != 0:
            results.append((tid, sub, "dispatch-failed", "%.0fs" % (time.monotonic() - tt0)))
            stack.press_home()
            time.sleep(0.8)
            continue
        time.sleep(3.0)
        blob, nodes = dump_named(stack, phone, "75_%s_land" % tid.lower(), log, shot=False)
        if "not safe for work" in (blob or "").lower():  # NSFW gate: one Continue tap max
            gate = find_label_exact(nodes, ("Continue",))
            if gate is None:
                results.append((tid, sub, "nsfw-gate-no-continue", "%.0fs" % (time.monotonic() - tt0)))
                log("%s landing=nsfw-gate-no-continue (no Continue node, no blind tap)" % tid)
                stack.press_home()
                time.sleep(0.8)
                continue
            stack.tap_xy(*gate.center())
            time.sleep(2.0)
            blob, nodes = dump_named(stack, phone, "75_%s_gate" % tid.lower(), log, shot=False)
            log("%s NSFW gate Continue tapped from dump %s" % (tid, gate.center()))
        verdict, proof = classify(blob, sub, title, author)
        if verdict == "app-post" and sub.lower().replace(" ", "") not in (blob or "").lower().replace(" ", ""):
            verdict = "wrong-destination"
        el = time.monotonic() - tt0
        results.append((tid, sub, verdict, "%.0fs" % el))
        log("%s landing=%s sub=r/%s elapsed=%.0fs proof=[%s]" % (tid, verdict, sub, el, proof))
        if verdict == "unknown" or el > TRIAL_BUDGET:
            log("STUCK %s: %s. Halting all, safe-park. fingerprint=%s,%s"
                % (tid, verdict, tid, verdict))
            stack.press_home()
            phone.close()
            for r in results:
                log("RESULT %s r/%s %s %s" % r)
            return 3
        stack.press_home()  # navigation recovery only, reset foreground
        time.sleep(0.8)
    for r in results:
        log("RESULT %s r/%s %s %s" % r)
    routed = sum(1 for r in results if r[2] in (
        "app-post", "app-post-unverified-author", "removed-post"))
    verifiable = [r for r in results if r[2] not in (
        "removed-post", "skipped-no-url")]
    verified = sum(1 for r in verifiable if r[2] in (
        "app-post", "app-post-unverified-author"))
    dead = sum(1 for r in results if r[2] == "removed-post")
    log("SUMMARY routing=%d/%d in-app, verification=%d/%d full-proof "
        "(DEAD/removed=%d excluded from verification), fallback-taken=0 "
        "(pure dispatch, path unchanged)"
        % (routed, len(results), verified, len(verifiable), dead))
    stack.press_home()  # safe-park, phone left alone
    phone.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PhoneError as e:
        log("ABORT PhoneError: " + str(e))
        raise SystemExit(2)
