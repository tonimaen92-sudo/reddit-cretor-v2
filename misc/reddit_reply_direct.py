# UTF-8
"""iPhone fast-reply: open a precached permalink via Safari OPEN, reply to top.

Replaces COMMENTER_RUNBOOK steps 2-3 (Latest feed scroll) with a direct
open. Everything else stays per runbook:

  open_container -> safari_open(permalink) -> comment button -> BEST top
  comment match -> Reply -> verify "Replying to <user>" -> type_human 95ms
  -> screenshot verify (Vietnamese drop guard) -> blue Reply -> verify
  nested "<you> - now" -> close_reddit -> mark_commented ->
  close the Safari Reddit tab.

Step-by-step dumps (so we prove each part live):
  3000_pick / 3010_after_open / 3020_post / 3030_comments /
  3035_reply_composer / 3040_typed / 3050_sent /
  3060_safari / 3061_tabs / 3062_start

Usage (phone part, after precache):
  .\\.venv\\Scripts\\python.exe misc\\reddit_reply_direct.py a0003 --pick --dry-run
  .\\.venv\\Scripts\\python.exe misc\\reddit_reply_direct.py a0003 --pick
  .\\.venv\\Scripts\\python.exe misc\\reddit_reply_direct.py a0003 --url "https://www.reddit.com/r/.../..."
  .\\.venv\\Scripts\\python.exe misc\\reddit_reply_direct.py a0003 --pick --auto --reply "yeah justice for my boy monty"

After .py edits: python -m py_compile.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from comment_store_ios import already_commented, mark_commented  # noqa: E402
from config import DUMP_DIR, LOG_DIR  # noqa: E402
from reddit_open import close_reddit, open_container  # noqa: E402
from reddit_post import close_safari_reddit_tab, safari_open, tap_open_banner  # noqa: E402
from slots import get_slot, slot_reddit_ready  # noqa: E402
from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

QUEUE_PATH = os.path.join(LOG_DIR, "comment_queue.json")


def log(msg: str) -> None:
    print(msg, flush=True)


def load_queue() -> list[dict]:
    try:
        with open(QUEUE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items") if isinstance(data, dict) else data
        return items if isinstance(items, list) else []
    except Exception:
        return []


def pick_item(url: str = "", newest_free: bool = True,
              sub: str = "", title: str = "", mode: str = "") -> dict:
    items = load_queue()
    if url:
        low = url.strip().lower()
        for c in items:
            if low in str(c.get("full_url", "")).lower() or low in str(c.get("permalink", "")).lower():
                return c
        # ad-hoc URL (manual override): build item from flags, skip queue.
        if sub and title:
            perma = url if url.startswith("/") else "/" + url.split("reddit.com")[-1]
            return {"sub": sub.strip().lower(), "title": title.strip()[:160],
                    "permalink": perma,
                    "full_url": url if url.startswith("http") else "https://www.reddit.com" + perma,
                    "score": 0, "age_min": 0, "mode": mode or "comment",
                    "top_author": "", "top_body": "", "top_score": 0,
                    "reply_text": "", "reply_source": "manual-adhoc"}
        raise PhoneError("URL not in queue. Check --dry-run list, or pass --sub + --title for ad-hoc.")
    if not items:
        raise PhoneError(f"Queue empty. Run misc/comment_precache.py first. ({QUEUE_PATH})")
    if newest_free:
        for c in sorted(items, key=lambda x: x.get("age_min") or 999):
            if not already_commented(c.get("sub", ""), c.get("title", "")):
                return c
        raise PhoneError("All queued posts already USED. Re-run precache.")
    return items[0]


def dump(stack, phone, stem, shot=False):
    blob, nodes = dump_named(stack, phone, stem, log, shot=shot)
    log(f"DUMP {stem}: {len(nodes)} nodes blob={len(blob or '')} chars")
    return blob, nodes


def confirm_step(text: str, auto: bool) -> None:
    log("STEP " + text)
    if auto:
        return
    try:
        input("ENTER to continue (Ctrl+C aborts)… ")
    except EOFError:
        return


def wait_go(stem: str, timeout: int = 180) -> None:
    """Block until dumps/comment_go.txt exists so the typed screenshot can be read first."""
    path = os.path.join(DUMP_DIR, "comment_go.txt")
    try:
        os.remove(path)
    except OSError:
        pass
    log(f"WAIT_GO check {stem} then create dumps/comment_go.txt")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
            log("WAIT_GO received")
            return
        time.sleep(1)
    raise PhoneError(f"No go signal before send. Check {stem}. Did not send.")


def find_comment_button(nodes):
    """Comment bubble on the bottom action bar.

    OCR often reads the bubble as a short token plus the count ('L 5'),
    not the word comment. The live 22 Sep miss was the old (335,1215)
    point, which sits on the left edge above that node. Locked 23 Sep
    (TDC Ragatha): the bubble sits at y=1128, so the fallback band
    starts at y>1040, not y>1150.
    """
    best = None
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if "comment" in t and n.y > 1000:
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
        tl = t.lower()
        if any(k in tl for k in ("\u2022", "ago", "mod", "minute", "hour", "day")):
            continue
        if any(ch.isdigit() for ch in t) and 250 < n.x < 450:
            bar.append(n)
    if not bar:
        return None
    pick = min(bar, key=lambda n: abs(n.center()[0] - 360))
    return pick.center()


def classify_post_layout(nodes, blob: str = "") -> tuple[str, str]:
    """Phase 1 layout gate (locked 24 Sep, live Jen10 screen).

    Dump -> one of:
    COMMENTS_LIST - Reply rows (or BEST COMMENTS) on screen -> reply route
      (find the top comment's Reply).
    COMMENTS_BELOW - comments COUNT label >0 with zero Reply rows on
      screen -> NOT a verdict. Scroll down into the comments, re-dump,
      THEN classify. The count label is truth for "replies exist",
      Reply rows for "reachable". Never classify an unscrolled header.
    BUBBLE_DIRECT - no Reply rows, count 0, but the comment bubble / Join
      field is present and no composer is open -> bubble route (tap
      bubble; it opens the composer directly, so top-level is correct).
    BARE_POST - neither -> report only, no hunt, no blind taps.

    Pure function: no taps, no dumps.
    """
    import re
    low = (blob or "").lower()
    if "replying to" in low or "commenting on" in low:
        return ("BARE_POST", "composer already open - discard first, then re-classify")
    replies = [n for n in nodes
               if n.ntype != "ocr-line" and n.on_screen()
               and collapsed_text(n.labels()) == "reply" and n.y > 300]
    if replies or "best comments" in low:
        return ("COMMENTS_LIST",
                "%d reply rows - reply route" % len(replies))
    count = 0
    bubble = None
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if (any(ch.isdigit() for ch in t) and 250 < n.x < 450
                and n.y > 1000 and n.h <= 80 and n.w < 150
                and not any(k in t.lower() for k in
                            ("\u2022", "ago", "mod", "minute", "hour", "day"))):
            if bubble is None:
                bubble = n
            for m in re.findall(r"\d+", t):
                count = max(count, int(m))
    if count > 0:
        return ("COMMENTS_BELOW",
                "count=%d with zero Reply rows - scroll down, re-dump, then classify" % count)
    join = find_label_exact(nodes, ("Join the conversation",)) is not None
    if bubble is not None or join or "join the conversation" in low:
        hit = collapsed_text(bubble.labels())[:30] if bubble is not None else "join-field"
        return ("BUBBLE_DIRECT",
                "bubble %s - bubble route (composer-direct, top-level)" % hit)
    return ("BARE_POST", "no Reply rows, no bubble, no Join field - report only")


ADD_COMMENT_LABELS = ("Add a comment", "Add comment", "Join the conversation",
                      "Be the first to comment", "Comment")


def sent_ok(blob: str, username: str, reply_text: str) -> bool:
    """Strict send proof: username + 'now' + a distinctive reply word.

    Lesson 22 Sep: first-word matching false-positives on the subreddit
    name (e.g. 'freddy' inside 'r/fivenightsatfreddys'). Require all three.
    """
    low = (blob or "").lower()
    words = [w.strip(".,!?\"'").lower() for w in (reply_text or "").split()]
    distinctive = next((w for w in sorted(words, key=len, reverse=True) if len(w) >= 5), "")
    need = [ (username or "").strip().lower(), "now" ]
    if distinctive:
        need.append(distinctive)
    need = [n for n in need if n]
    if not need:
        return False
    return all(n in low for n in need)


def find_add_comment(nodes):
    """Top-level composer field on the comments screen."""
    hit = find_label_exact(nodes, ADD_COMMENT_LABELS)
    if hit is not None:
        return hit.center()
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if "add a comment" in collapsed_text(n.labels()):
            return n.center()
    return None


def find_post_button(nodes):
    """Send button of the top-level composer: Post top-right, else blue Reply."""
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
        best = max(cands, key=lambda n: n.x)
        return best.center()
    return None


def list_comment_rows(nodes) -> list[dict]:
    """Dump every visible comment row as (author, score, Reply-xy).

    Row block layout (locked 24 Sep, live Jen10 dump): author node +
    age node on one row, body rows, then an action row with Reply
    (x~470) + score digits right of it. First-row grabs are banned;
    callers must match by author (fallback: highest score).
    Pure function: no taps.
    """
    import re
    rows = []
    for r in nodes:
        if r.ntype == "ocr-line" or not r.on_screen():
            continue
        if collapsed_text(r.labels()) != "reply" or r.y <= 300:
            continue
        rxy = r.center()
        author, age, score = "", "", -1
        best_dy = 1e9
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen() or n is r:
                continue
            t = collapsed_text(n.labels())
            dy = r.y - n.y
            if dy <= 0 or dy > 420 or n.x > 420:
                continue
            single = t.split()[0] if t.split() else ""
            if re.fullmatch(r"[\w-]{3,24}", single or "") and dy < best_dy:
                # age node must sit on the same row to the right
                same_row = [m for m in nodes
                            if m.ntype != "ocr-line" and m.on_screen()
                            and abs(m.y - n.y) < 25 and m.x > n.x
                            and re.fullmatch(r"\d+[mh]", collapsed_text(m.labels()).split()[0] if collapsed_text(m.labels()).split() else "")]
                if same_row:
                    author = single
                    age = collapsed_text(same_row[0].labels()).split()[0]
                    best_dy = dy
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen() or n is r:
                continue
            t = (collapsed_text(n.labels()).split() or [""])[0]
            if (re.fullmatch(r"\d+", t or "") and n.x > r.x
                    and abs(n.y - r.y) < 30):
                score = max(score, int(t))
        rows.append({"author": author, "age": age, "score": score,
                     "reply_xy": rxy})
    return rows


def select_row_target(rows: list[dict], top_author: str) -> dict | None:
    """Row matching queued top_author (fallback: highest score)."""
    want = (top_author or "").strip().lower()
    if want:
        for row in rows:
            if (row.get("author") or "").lower() == want:
                row["match"] = "author"
                return row
    scored = [r for r in rows if (r.get("score") or -1) >= 0]
    if scored:
        pick = max(scored, key=lambda r: r["score"])
        pick["match"] = "topscore"
        return pick
    if rows:
        rows[0]["match"] = "only-row"
        return rows[0]
    return None


def find_top_reply_button(nodes, top_author: str):
    want = (top_author or "").strip().lower()
    author_node = None
    if want:
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            if want in collapsed_text(n.labels()).lower() and n.y < 1100:
                author_node = n
                break
    replies = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) == "reply" and n.y > 300:
            replies.append(n)
    if not replies:
        return None, author_node
    if author_node is None:
        return replies[0].center(), None
    # first Reply below the author row
    below = [n for n in replies if n.y > author_node.y]
    pick = min(below, key=lambda n: n.y) if below else replies[0]
    return pick.center(), author_node


def find_blue_reply(nodes):
    cands = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) == "reply" and n.y < 1000 and n.x > 400:
            cands.append(n)
    if cands:
        return max(cands, key=lambda n: n.x)
    return find_label_exact(nodes, ("Reply", "reply"))


REPLY_FALLBACK = (673, 776)


def find_hide_keyboard(nodes):
    """Keyboard cover guard: Done / Hide keyboard, before seeking Reply."""
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels()).lower()
        if t in ("done", "hide keyboard", "hidekeyboard") or "hide keyboard" in t:
            return n.center()
    return None


def hide_keyboard_if_present(stack, phone, log, nodes):
    """Tap Done/Hide-keyboard first so Reply is visible. Returns fresh nodes."""
    hxy = find_hide_keyboard(nodes)
    if hxy is None:
        return nodes
    t0 = time.time()
    log(f"KB_HIDE tap {hxy} before Reply seek")
    stack.tap_xy(*hxy)
    time.sleep(1.0)
    _, fresh = dump(stack, phone, "3041_kbhide", shot=False)
    log(f"REPLY_TIMING kbhide_done elapsed={time.time() - t0:.1f}s")
    return fresh


def poll_blue_reply(stack, phone, log, base_xy=REPLY_FALLBACK):
    """Poll Reply enabled every 2s x10 (text dumps). Returns (x, y)."""
    t0 = time.time()
    xy = base_xy
    for attempt in range(1, 11):
        _, live = dump(stack, phone, "3041_pre_send", shot=False)
        live = hide_keyboard_if_present(stack, phone, log, live)
        blue = find_blue_reply(live)
        if blue is not None:
            try:
                xy = blue.center()
            except Exception:
                xy = base_xy
            log(f"REPLY_TIMING poll_ok attempt={attempt} xy={xy} elapsed={time.time() - t0:.1f}s")
            return xy
        log(f"REPLY_POLL {attempt}/10 no Reply yet elapsed={time.time() - t0:.1f}s")
        if attempt < 10:
            time.sleep(2.0)
    log(f"REPLY_TIMING poll_fallback xy={xy} elapsed={time.time() - t0:.1f}s")
    return xy


def tap_reply_with_retry(stack, phone, log, xy, username, reply_text):
    """Tap Reply, wait 3s, verify <user>-now. Retry 3x +10px; tap_xy recycles XXTouch."""
    last = None
    for i in range(3):
        x, y = xy[0] + 10 * i, xy[1] + 10 * i
        t0 = time.time()
        log(f"REPLY_TAP try={i + 1}/3 xy=({x},{y})")
        try:
            stack.tap_xy(x, y)
        except Exception as e:
            last = e
            log(f"REPLY_TAP collision try={i + 1} recycled ({str(e)[:80]})")
            continue
        time.sleep(3.0)
        blob, _ = dump(stack, phone, "3050_sent", shot=True)
        if sent_ok(blob, username, reply_text):
            log(f"REPLY_TIMING sent_ok try={i + 1} elapsed={time.time() - t0:.1f}s")
            return blob
        last = PhoneError("Send not proven in dump. See 3050_sent.png.")
        log(f"REPLY_TAP try={i + 1} not proven, offset+10 retry")
    raise PhoneError(str(last or "Send not proven after 3 Reply taps."))


def run_direct(slot_name: str, item: dict, reply_override: str = "", auto: bool = False,
              resume: bool = False) -> int:
    slot = get_slot(slot_name)
    if not slot or not slot.get("ghost"):
        print(f"ERROR Slot {slot_name} has no Ghost container.")
        return 1
    if not slot_reddit_ready(slot):
        print(
            f"ERROR Slot {slot_name} is not ready: account creation and verified "
            "app cleanup must both be complete."
        )
        return 1
    email = str(slot.get("email") or "").strip()
    username = str(slot.get("username") or "").strip()
    if not email:
        print(f"ERROR Slot {slot_name} has no Hide My Email.")
        return 1
    sub = item.get("sub", "")
    title = item.get("title", "")
    full_url = item.get("full_url", "")
    top_author = item.get("top_author", "")
    mode = (item.get("mode") or "reply").strip().lower()
    if mode not in ("reply", "comment"):
        mode = "reply"
    reply_text = (reply_override or item.get("reply_text") or "").strip()
    if already_commented(sub, title):
        print(f"SKIP USED r/{sub} :: {title[:80]}")
        return 2
    if not full_url:
        print("ERROR queue item has no full_url.")
        return 1

    log(f"SLOT {slot_name} email={email}")
    log(f"POST r/{sub} :: {title[:100]}")
    log(f"URL {full_url}")
    log(f"TOP u/{top_author} ({item.get('top_score')}up): {str(item.get('top_body'))[:160]}")
    if not reply_text:
        print("ERROR no reply_text. Fill reply_text in logs/comment_queue.json or pass --reply.")
        return 1
    log(f"MODE {mode}")
    log(f"REPLY [{item.get('reply_source')}] {reply_text}")

    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend. Open XXTouch Elite, Remote Service on.")

        dump(stack, phone, "3000_pick", shot=False)
        if resume:
            confirm_step("1/7 resume on the current Safari / post screen", auto)
            blob, nodes = dump(stack, phone, "3000_resume", shot=True)
            op = tap_open_banner(nodes)
            if op is None:
                time.sleep(1.2)
                blob, nodes = dump(stack, phone, "3001_resume_retry", shot=True)
                op = tap_open_banner(nodes)
            if op is not None:
                log(f"TAP OPEN {op.center()}")
                stack.tap_xy(*op.center())
                time.sleep(2.0)
                blob, nodes = dump(stack, phone, "974_resume_open", shot=True)
                cont = find_label_exact(nodes, ("Continue",))
                if cont is not None:
                    log(f"TAP Continue {cont.center()}")
                    stack.tap_xy(*cont.center())
                    time.sleep(1.4)
            elif "finally met freddy" not in (blob or "").lower():
                raise PhoneError("Resume screen has no OPEN banner and no post. See 3000_resume.png.")
        else:
            confirm_step("1/7 open container " + slot_name, auto)
            open_container(stack, phone, log, slot_name, email)
            dump(stack, phone, "3010_after_open", shot=True)

            confirm_step("2/7 Safari OPEN permalink (skips Latest scroll)", auto)
            safari_open(stack, phone, log, full_url)
        blob, nodes = dump(stack, phone, "3020_post", shot=True)
        log(f"POST_VERIFY r/{sub} score={item.get('score')} age={item.get('age_min')}m (screenshot ground truth)")

        confirm_step("3/7 tap comment button, open comments", auto)
        hit = find_comment_button(nodes)
        composer_on_post = find_add_comment(nodes) if mode == "comment" else None
        if hit is None and composer_on_post is None:
            raise PhoneError("Comment button not in dump. See 3020_post.png. Did not guess.")
        if hit is not None:
            cx, cy = hit
            log(f"TAP comment button ({cx},{cy})")
            stack.tap_xy(cx, cy)
            time.sleep(1.6)
            blob, nodes = dump(stack, phone, "3030_comments", shot=True)
            low = (blob or "").lower()
            if "best comments" not in low and "comments" not in low:
                log("WARN comments header not in dump text; check 3030_comments.png, continuing dump-first")
            if ("best comments" not in low and "commenting on" in low
                    and "replying to" not in low):
                log("BUBBLE_DIRECT_COMPOSER bubble opened the composer, no comments "
                    "list; queue reply mode overridden to top-level comment")
                mode = "comment"
            composer_on_post = None
        else:
            log(f"TAP Join the conversation {composer_on_post} (composer already on the post)")

        if mode == "comment":
            confirm_step("4/7 tap Add-a-comment (top-level, NOT a Reply)", auto)
            axy = composer_on_post or find_add_comment(nodes)
            if axy is None:
                raise PhoneError("Add-a-comment field not in dump. See 3030_comments.png. Did not guess.")
            stack.tap_xy(*axy)
            time.sleep(1.2)
            blob, nodes = dump(stack, phone, "3035_toplevel_composer", shot=True)
            if "replying to" in (blob or "").lower():
                raise PhoneError("Composer shows 'Replying to' — this would nest. "
                                 "See 3035_toplevel_composer.png. Aborting top-level.")
            log("COMPOSER_OK top-level (no Replying-to line)")

            confirm_step("5/7 type comment (type_human 95ms), screenshot verify", auto)
            stack.type_human(reply_text, ms=95)
            time.sleep(0.8)
            dump(stack, phone, "3040_typed", shot=True)
            log("TYPED_VERIFY check 3040_typed.png for Vietnamese drops before sending")
            wait_go("3040_typed.png")

            confirm_step("6/7 tap Post, verify live", auto)
            _, live_nodes = dump(stack, phone, "3041_pre_send", shot=False)
            live_nodes = hide_keyboard_if_present(stack, phone, log, live_nodes)
            send = find_post_button(live_nodes)
            if send is None:
                send = poll_blue_reply(stack, phone, log)
            blob = tap_reply_with_retry(stack, phone, log, send, username, reply_text)
            log(f"SENT_OK top-level now :: {reply_text[:80]}")

            confirm_step("7/7 close Reddit, close the Safari Reddit tab, lock post", auto)
            close_reddit(stack, phone, log)
            key = mark_commented(sub, title, slot_name, "comment")
            log(f"MARKED {key}")
            close_safari_reddit_tab(stack, phone, log)
            log("COMMENT_DIRECT_OK")
            return 0

        rxy, anode = find_top_reply_button(nodes, top_author)
        if rxy is None:
            raise PhoneError("Top comment Reply not in dump. See 3030_comments.png. Did not guess.")
        log(f"TOP_MATCH u/{top_author} author_node={'yes' if anode else 'fallback-first-reply'}")

        confirm_step(f"4/7 tap Reply under u/{top_author}, verify Replying-to", auto)
        stack.tap_xy(*rxy)
        time.sleep(1.2)
        blob, nodes = dump(stack, phone, "3035_reply_composer", shot=True)
        if "replying to" not in (blob or "").lower():
            raise PhoneError("No 'Replying to' line; refusing top-level. See 3035_reply_composer.png.")
        log(f"COMPOSER_OK Replying to {top_author}")

        confirm_step("5/7 type reply (type_human 95ms), screenshot verify", auto)
        stack.type_human(reply_text, ms=95)
        time.sleep(0.8)
        dump(stack, phone, "3040_typed", shot=True)
        log("TYPED_VERIFY check 3040_typed.png for Vietnamese drops before sending")
        wait_go("3040_typed.png")

        confirm_step("6/7 tap blue Reply, verify nested", auto)
        xy = poll_blue_reply(stack, phone, log)
        blob = tap_reply_with_retry(stack, phone, log, xy, username, reply_text)
        log(f"SENT_OK nested now :: {reply_text[:80]}")

        confirm_step("7/7 close Reddit, close the Safari Reddit tab, lock post", auto)
        close_reddit(stack, phone, log)
        key = mark_commented(sub, title, slot_name, "reply")
        log(f"MARKED {key}")
        close_safari_reddit_tab(stack, phone, log)
        log("REPLY_DIRECT_OK")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="iPhone fast-reply via precached permalink")
    ap.add_argument("slot", help="container e.g. a0003")
    ap.add_argument("--url", default="", help="permalink/full URL from queue")
    ap.add_argument("--pick", action="store_true", help="pick newest FREE queued post")
    ap.add_argument("--reply", default="", help="override reply_text")
    ap.add_argument("--mode", default="", help="reply | comment (ad-hoc default comment)")
    ap.add_argument("--sub", default="", help="sub for ad-hoc --url")
    ap.add_argument("--title", default="", help="title for ad-hoc --url")
    ap.add_argument("--auto", action="store_true", help="no ENTER pauses (agent run)")
    ap.add_argument("--resume", action="store_true",
                    help="phone is already on the Safari OPEN banner or the post")
    ap.add_argument("--dry-run", action="store_true", help="no phone, just show pick + steps")
    args = ap.parse_args()
    name = args.slot.strip().lower()
    try:
        item = pick_item(args.url, newest_free=args.pick or not args.url,
                         sub=args.sub, title=args.title, mode=args.mode)
    except PhoneError as e:
        print("ERROR", e)
        return 1
    if args.mode and args.mode.strip().lower() in ("reply", "comment"):
        item["mode"] = args.mode.strip().lower()
    if args.dry_run:
        print(f"SLOT {name}")
        print(f"POST r/{item.get('sub')} {item.get('score')}up {item.get('age_min')}m")
        print(f"TITLE {item.get('title')}")
        print(f"URL {item.get('full_url')}")
        print(f"TOP u/{item.get('top_author')} ({item.get('top_score')}up): {item.get('top_body', '')[:300]}")
        print(f"REPLY [{item.get('reply_source')}]: {item.get('reply_text') or '(empty: fill or --reply)'}")
        print("STEPS 3000_pick > open_container > safari_open > 3020_post > comment > 3030_comments > Reply > 3035_composer > type > 3040_typed > send > 3050_sent > close Reddit > close Safari Reddit tab > mark")
        return 0
    return run_direct(name, item, reply_override=args.reply, auto=args.auto, resume=args.resume)


if __name__ == "__main__":
    raise SystemExit(main())
