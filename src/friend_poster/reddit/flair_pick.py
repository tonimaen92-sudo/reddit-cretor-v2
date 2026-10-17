# UTF-8
"""Flair auto-pick for forced-flair subs. First encounter per sub asks
Grok (sub name + visible options only, common sense); the choice is
cached in post_settings and reused verbatim forever after. Matching is
always against visible sheet options — never invented, never guessed.
"""

from __future__ import annotations

import os
import sys
import time
from friend_poster.device.ssh_device import PhoneError  # noqa: E402
from friend_poster.device.ui_tree import collapsed_text, dump_named, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"


def find_sheet_x(nodes):
    """Sheet X button: x-text, top-right band (100<=y<=200, x>600).
    Excludes the Post pill behind (y<100) and bottom junk. Rightmost
    wins. Pure."""
    best = None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) == "x" \
                and 100 <= n.y <= 200 and n.x > 600:
            if best is None or n.x > best.x:
                best = n
    return best


def sheet_open(blob, nodes) -> bool:
    """True while the flair sheet is up: sheet-only markers. Pure."""
    import re as _re
    low = (blob or "").lower()
    if "search flair" in low or "view all flair" in low:
        return True
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if t in ("search flair", "view all flair"):
            return True
        if _re.search(r"r/[a-z0-9_]+ flair", t):
            return True
    return False


def close_sheet(stack, phone, lg, stem: str = "957_close"):
    """Dismiss the flair sheet: X up to 3x with re-dump, then one
    header-drag fallback. Returns post-close (blob, nodes). Raises
    PhoneError when the sheet will not go."""
    blob, nodes = dump_named(stack, phone, stem + "_0", lg, shot=False)
    for attempt in (1, 2, 3):
        if not sheet_open(blob, nodes):
            return blob, nodes
        xbtn = find_sheet_x(nodes)
        if xbtn is None:
            break
        lg("TAP sheet X (try %d)" % attempt)
        stack.tap_xy(*xbtn.center())
        time.sleep(2.0)
        blob, nodes = dump_named(
            stack, phone, "%s_%d" % (stem, attempt), lg, shot=True)
    if sheet_open(blob, nodes):
        lg("X misses, one sheet drag-down fallback")
        stack.interp_flick(375, 140, 375, 700, steps=14, ms=22)
        time.sleep(1.5)
        blob, nodes = dump_named(stack, phone, stem + "_drag", lg, shot=True)
    if sheet_open(blob, nodes):
        raise PhoneError("Flair sheet stuck after X x3 + drag. "
                         "Close it manually. See %s dumps." % stem)
    return blob, nodes


def single_form(text: str) -> str:
    """Collapse XXTouch's tripled OCR ('Amateur Amateur Amateur' ->
    'Amateur'), preserving original case. Pure."""
    words = str(text or "").split()
    out: list[str] = []
    for w in words:
        if not out or out[-1] != w:
            out.append(w)
    n = len(out)
    if n >= 4 and n % 3 == 0:
        k = n // 3
        if out[:k] == out[k:2 * k] == out[2 * k:]:
            out = out[:k]
    elif n >= 4 and n % 2 == 0:
        k = n // 2
        if out[:k] == out[k:]:
            out = out[:k]
    return " ".join(out)


def list_flair_options(nodes) -> list:
    """Visible flair sheet options in original case. Header rows (sheet
    title, r/<sub> flair, search rows, y<=400) never count. Pure-ish."""
    out: list = []
    seen: set = set()
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = single_form(n.labels())
        if not t or "flair" in t.casefold() or n.y <= 400:
            continue
        if t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
    return out


def match_option(reply: str, options: list):
    """Exact (casefold, quote-stripped) match of a Grok reply against
    visible options. Tripled-echo replies collapse first. None when no
    exact match — never fuzzy. Pure."""
    want = single_form((reply or "").strip().strip("\"' ")).casefold()
    if not want:
        return None
    for opt in options or []:
        if single_form((opt or "").strip().strip("\"' ")).casefold() == want:
            return opt
    return None


def pick_flair_option(sub: str, options: list, log=print) -> str:
    """One Grok text call: sub name + options in, one exact option out.
    '' on any failure. No title, no image — common sense only."""
    opts = [o for o in (options or []) if (o or "").strip()]
    if not opts:
        return ""
    try:
        from friend_poster.services.comment_llm import XAI_FAST_MODEL, load_xai_key, xai_chat
    except Exception as e:
        log("Flair pick ERROR imports: %s" % e)
        return ""
    try:
        key = load_xai_key()
    except Exception:
        key = ""
    if not key:
        log("Flair pick: no xai key.")
        return ""
    prompt = (
        "You post in r/%s. Pick exactly one flair from this list that "
        "fits a normal post there. Use common sense. Reply with ONLY "
        "the exact line, nothing else.\n%s"
        % ((sub or "").strip(), "\n".join("- " + o for o in opts)))
    try:
        cap, err = xai_chat(key, XAI_FAST_MODEL, [{
            "role": "user", "content": prompt}],
            max_tokens=40, temperature=0.2, timeout=90, reasoning=False)
    except Exception as e:
        log("Flair pick ERROR %s: %s" % (type(e).__name__, str(e)[:100]))
        return ""
    if not cap:
        log("Flair pick FAIL %s" % (err or "")[:100])
        return ""
    hit = match_option(cap, opts)
    if hit is None:
        log("Flair pick no exact match for %r (seen: %s)."
            % (cap.strip()[:60], ", ".join(opts[:12])))
        return ""
    log("Flair pick: %r" % hit)
    return hit


def ensure_flair(stack, phone, lg, nodes, sub: str, save_fn=None):
    """Flair row demanded but no saved flair: open the sheet, list
    options, Grok-pick one, tap it, cache via save_fn(sub, flair).
    Returns composer nodes. Raises PhoneError with seen options when
    nothing pickable (caller aborts + reports)."""
    row = None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if "flair" in collapsed_text(n.labels()).lower():
            row = n
            break
    if row is None:
        lg("FLAIR_SKIP no flair row on composer (demanded upstream?)")
        return nodes
    lg("TAP flair row " + collapsed_text(row.labels())[:40])
    stack.tap_xy(*row.center())
    time.sleep(1.2)
    _blob, nodes = dump_named(stack, phone, "957_flair_auto", lg, shot=True)
    opts = list_flair_options(nodes)
    if not opts:
        seen = sorted({collapsed_text(n.labels())[:40] for n in nodes
                       if n.ntype != "ocr-line" and n.on_screen()})
        raise PhoneError(
            "Flair sheet empty (seen: %s). See 957_flair_auto. Did not guess."
            % ", ".join(seen[:12]))
    pick = pick_flair_option(sub, opts, log=lg)
    if not pick:
        raise PhoneError(
            "Flair pick failed (seen: %s). Map it manually once."
            % ", ".join(opts[:12]))
    target = None
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) == pick.casefold():
            target = n
            break
    if target is None:
        raise PhoneError("Flair option vanished. Did not guess.")
    lg("TAP flair option " + pick[:40])
    stack.tap_xy(*target.center())
    time.sleep(1.2)
    if save_fn is not None:
        try:
            save_fn(sub, pick)
        except Exception as e:
            lg("Flair cache note: %s" % e)
    blob, nodes = dump_named(stack, phone, "957_flaired_auto", lg, shot=True)
    blob, nodes = close_sheet(stack, phone, lg, stem="957_close")
    post = find_label_exact(nodes, ("Post",))
    if post is None or post.y >= 160:
        raise PhoneError("Composer back but no Post button. See 957_close.")
    lg("Composer back with flair applied, Post present.")
    return nodes
