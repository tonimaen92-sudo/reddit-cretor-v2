# UTF-8
"""Reply state machine: post-open -> sent. Replaces the tree-targeting path.

States (every transition gated on observed state, illegal = abort + timeline):
  ENTER -> CLASSIFY -> ACQUIRE -> GATE -> TYPE -> SEND -> VERIFY -> MARK -> CLOSE

Sensing law: layout + gate/composer state = tree text signals (reliable);
rows + controls = screenshot pixels via in-code eyes (geometry). Tree never
targets - corroboration only confirms a control exists near eyes xy, then the
tap snaps to tree geometry. Locked 24 Sep: qwen layout verdicts ran 3-of-3
wrong, so layout left eyes entirely.
In-code eyes: gpt-4o-mini via OpenRouter (SFW doctrine, Grok never touches
this path), key from secrets.json, never printed. Reads author rows +
Reply positions off screenshots. No agent-eyeball dependency.
Touch law: slow-drag only (interp_flick, no swipe_up/swipe anywhere in this
module). Scroll only when the count label proves rows below. Every loop
bounded + timeout + abort-with-timeline. Frozen webview (identical dumps)
= report, never grind.
Target law: first-row/author-on-screen wins; queue author = post selection
only. Reply text comes from queue reply_text (engine-filled); empty = abort.

Bounds: MAX_EYES=12 calls, MAX_SCROLLS=8 drags, per-state timeouts, 12min total.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import time

from comment_llm import load_api_key
from comment_store_ios import mark_commented
from reddit_open import close_reddit
from reddit_post import close_safari_reddit_tab
from ui_tree import TapStack, collapsed_text, dump_named, find_label_exact

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
# Eyes model: qwen/qwen2.5-vl-72b-instruct. Offline bench 24 Sep 2026
# (misc/eyes_bench.py, 20 frozen frames): qwen-vl 15/20, ui-tars 11/20,
# gemini-flash 0/20, gpt-4o-mini 0/20. Losers stay in the harness.
EYES_MODEL = "qwen/qwen2.5-vl-72b-instruct"

MAX_EYES = 30
MAX_SCROLLS = 8
STATE_TIMEOUT = 120.0
TOTAL_TIMEOUT = 12 * 60.0
FROZEN_LIMIT = 3
AUDIT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "logs", "eyes_audit.jsonl")
AUDIT_N = 10
AUDIT_MEAN_PX = 60.0

# Standing Law 3 budgets (seconds). TYPE go-signal wait is human-paced, exempt.
STATE_BUDGETS = {"ACQUIRE": 180.0, "GATE": 60.0, "TYPE": 90.0,
                 "SEND": 90.0, "VERIFY": 60.0}

EYES_PROMPT = """You are reading an iPhone screenshot (750x1334) of a Reddit post in mobile Safari.
Return STRICT JSON only, no other text:
{"count": <int comments count from the header, 0 if none shown>,
"rows": [{"author": "<username, exact case>", "score": <int, -1 if unknown>, "reply_xy": [x, y]}],
"bubble_xy": [x, y]|null, "send_xy": [x, y]|null}
Rules: rows = EVERY visible comment row with its Reply button center in device pixels (750x1334 space). NEVER include the post header row (post author + age + title + body at the top of the screen): comment rows live BELOW the post body, each with its own author-age line and its own Reply control. Do NOT report a composer/send state; report controls only. Author names never start with 'u/': 'u/<name>' at the top is the POST header, not a comment. Exclude it.
Measure every reply_xy/bubble_xy/send_xy in exact device pixels READ OFF the glyph: never default to screen center (x=375) or round numbers. A made-up coordinate fails the tap and burns the run."""


def prep_frame(png: bytes, size: tuple[int, int] = (750, 1334)) -> bytes:
    """Deterministic preprocessing part 1: fixed size + normalized contrast.

    Every eyes read goes through this so variance comes from the model,
    not the pixels. Fixed resize, autocontrast, fixed PNG encoding.
    """
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(png)).convert("RGB") as im:
        norm = ImageOps.autocontrast(im.resize(size), cutoff=1)
        buf = io.BytesIO()
        norm.save(buf, format="PNG", optimize=False)
        return buf.getvalue()


def prep_crop(png: bytes, x0: int, y0: int, box: int = 300) -> tuple[bytes, tuple[int, int, int, int]]:
    """Deterministic crop: fixed 300px box (clamped + padded), 2x => 600x600."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(png)).convert("RGB") as im:
        fw, fh = im.size
        x0c, y0c = max(0, x0), max(0, y0)
        x1c, y1c = min(fw, x0c + box), min(fh, y0c + box)
        crop = Image.new("RGB", (box, box), (0, 0, 0))
        crop.paste(im.crop((x0c, y0c, x1c, y1c)), (0, 0))
        norm = ImageOps.autocontrast(crop.resize((box * 2, box * 2)), cutoff=1)
        buf = io.BytesIO()
        norm.save(buf, format="PNG", optimize=False)
        return buf.getvalue(), (x0c, y0c, x1c, y1c)


def verdict_key(seen: dict) -> tuple:
    """Canonical verdict for voting: sorted authors + count. Pure.

    Layout deliberately excluded: qwen votes 3-of-3 composer on plain
    post screens (locked 24 Sep). Layout belongs to the tree.
    """
    rows = sorted((r.get("author") or "").strip().lower() for r in seen.get("rows", []))
    return (tuple(rows), seen.get("count") or 0)


def vote_tally(votes: list[dict]) -> tuple[dict | None, str]:
    """2-of-3 majority over verdict keys. Pure. Returns (winner, tally)."""
    keys = [verdict_key(v) for v in votes]
    for i, k in enumerate(keys):
        if sum(1 for j in keys if j == k) >= 2:
            agree = sum(1 for j in keys if j == k)
            return votes[i], "%d-of-%d" % (agree, len(votes))
    return None, "1-1-1"


def audit_entries() -> list[dict]:
    try:
        with open(AUDIT_PATH, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except Exception:
        return []


def audit_check(entries: list[dict]) -> tuple[float, int, bool]:
    """Snap-distance audit. Returns (mean_px, n, miscalibrated). Pure."""
    ds = [e["snap_delta"] for e in entries[-AUDIT_N:]
          if isinstance(e.get("snap_delta"), (int, float))]
    if len(ds) < 5:
        return (sum(ds) / len(ds) if ds else 0.0, len(ds), False)
    mean = sum(ds) / len(ds)
    return (mean, len(ds), mean > AUDIT_MEAN_PX)


def audit_append(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


class Abort(Exception):
    def __init__(self, state: str, reason: str, timeline: list):
        super().__init__("[%s] %s" % (state, reason))
        self.state = state
        self.reason = reason
        self.timeline = timeline


class Ctx:
    def __init__(self, stack: TapStack, phone, log, item: dict, username: str):
        self.stack = stack
        self.phone = phone
        self.log = log
        self.item = item
        self.username = username
        self.t0 = time.time()
        self.timeline: list[str] = []
        self.eyes_used = 0
        self.scrolls_used = 0
        self.hashes: list[str] = []
        self.target: dict | None = None
        self.mode = "reply"

    def mark(self, event: str) -> None:
        line = "+%.1fs %s" % (time.time() - self.t0, event)
        self.timeline.append(line)
        self.log("STATE " + line)

    def fail(self, state: str, reason: str):
        self.mark("ABORT [%s] %s" % (state, reason))
        raise Abort(state, reason, list(self.timeline))

    def check_total(self, state: str) -> None:
        if time.time() - self.t0 > TOTAL_TIMEOUT:
            self.fail(state, "total timeout 12min")

    def check_budget(self, state: str, since: float) -> None:
        limit = STATE_BUDGETS.get(state, 0)
        if limit and time.time() - since > limit:
            self.fail(state, "over budget %.0fs>%ds (Standing Law 1)" % (
                time.time() - since, limit))


def _shot_png(ctx: Ctx):
    data = ctx.stack.screenshot_png()
    if not data:
        ctx.fail("CLASSIFY", "screenshot empty")
    return data


def eyes(ctx: Ctx, png: bytes) -> dict:
    """In-code eyes: rows + count + controls off screenshot pixels.

    Layout is NOT read here (tree owns it). Rows carry author/score/xy;
    controls are bubble/send points or null.
    """
    import requests

    if ctx.eyes_used >= MAX_EYES:
        ctx.fail("CLASSIFY", "eyes budget %d exhausted" % MAX_EYES)
    key = load_api_key()
    if not key:
        ctx.fail("CLASSIFY", "no openrouter_api_key in secrets.json")
    ctx.eyes_used += 1
    b64 = base64.b64encode(prep_frame(png)).decode("ascii")
    body = {
        "model": EYES_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": EYES_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + b64}},
            ],
        }],
        "max_tokens": 800,
        "temperature": 0.0,
    }
    ctx.mark("eyes call %d/%d" % (ctx.eyes_used, MAX_EYES))
    try:
        r = requests.post(
            OPENROUTER,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json",
                     "HTTP-Referer": "https://github.com/reddit-bot",
                     "X-Title": "Reddit iPhone Eyes"},
            json=body, timeout=90)
    except Exception as e:
        ctx.fail("CLASSIFY", "eyes transport: %s" % str(e)[:120])
    if r.status_code != 200:
        ctx.fail("CLASSIFY", "eyes HTTP %d" % r.status_code)
    try:
        text = (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception:
        ctx.fail("CLASSIFY", "eyes bad JSON envelope")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        ctx.fail("CLASSIFY", "eyes non-JSON reply")
    try:
        seen = json.loads(m.group(0))
    except Exception:
        ctx.fail("CLASSIFY", "eyes JSON parse failed")
    if not isinstance(seen, dict):
        ctx.fail("CLASSIFY", "eyes bad envelope")
    rows = [r for r in (seen.get("rows") or []) if isinstance(r, dict)
            and r.get("author") and isinstance(r.get("reply_xy"), list)]
    seen["rows"] = rows
    return seen


def tree_blob(ctx: Ctx, stem: str):
    blob, nodes = dump_named(ctx.stack, ctx.phone, stem, ctx.log, shot=False)
    return blob or "", nodes


def slow_drag_down(ctx: Ctx) -> None:
    if ctx.scrolls_used >= MAX_SCROLLS:
        ctx.fail("ACQUIRE", "scroll budget %d exhausted" % MAX_SCROLLS)
    ctx.scrolls_used += 1
    ctx.mark("slow-drag down %d/%d" % (ctx.scrolls_used, MAX_SCROLLS))
    ctx.stack.interp_flick(375, 800, 375, 650, steps=20, ms=40)
    time.sleep(1.0)


def slow_drag_up(ctx: Ctx) -> None:
    if ctx.scrolls_used >= MAX_SCROLLS:
        ctx.fail("ACQUIRE", "scroll budget %d exhausted" % MAX_SCROLLS)
    ctx.scrolls_used += 1
    ctx.mark("slow-drag up %d/%d" % (ctx.scrolls_used, MAX_SCROLLS))
    ctx.stack.interp_flick(375, 450, 375, 850, steps=20, ms=40)
    time.sleep(1.0)


def note_frozen(ctx: Ctx, blob: str, state: str) -> None:
    h = hashlib.md5((blob or "").encode("utf-8", "replace")).hexdigest()
    ctx.hashes.append(h)
    if len(ctx.hashes) >= FROZEN_LIMIT and len(set(ctx.hashes[-FROZEN_LIMIT:])) == 1:
        ctx.fail(state, "frozen webview: %d identical dumps" % FROZEN_LIMIT)


def st_enter(ctx: Ctx) -> None:
    ctx.mark("ENTER slot=item check")
    text = (ctx.item.get("reply_text") or "").strip()
    if not text:
        ctx.fail("ENTER", "queue reply_text empty (engine did not fill)")
    if not (ctx.item.get("full_url") or "").strip():
        ctx.fail("ENTER", "queue item has no full_url")
    blob, _nodes = tree_blob(ctx, "900_enter")
    title = (ctx.item.get("title") or "").strip().lower()
    words = [w for w in re.findall(r"[a-z0-9]{4,}", title)][:4]
    if words and not any(w in (blob or "").lower() for w in words):
        ctx.fail("ENTER", "post title not on screen (wrong screen?)")
    ctx.mark("ENTER ok")


def composer_open_in_tree(blob: str, nodes) -> bool:
    """Tree corroboration for an eyes composer verdict. Pure.
    A real open composer shows keyboard rows or a commenting-on line.
    Locked 24 Sep: eyes said composer with no taps made (no keyboard,
    no commenting-on) and the machine aborted a healthy screen."""
    low = (blob or "").lower()
    if "commenting on" in low or "replying to" in low:
        return True
    keys = 0
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if len(t) <= 4 and n.y > 900:
            keys += 1
    return keys >= 10


def vote_eyes(ctx: Ctx, png: bytes) -> dict:
    """2-of-3 majority vote on the classify verdict (preprocessed pixels).

    Disagreement = EYES_UNSTABLE: abort + report, no tap. Always 3 reads
    so the tally is honest.
    """
    votes = [eyes(ctx, png) for _ in range(3)]
    winner, tally = vote_tally(votes)
    ctx.mark("VOTE tally=%s key=%s" % (tally, verdict_key(winner) if winner else None))
    if winner is None:
        ctx.fail("CLASSIFY", "EYES_UNSTABLE verdicts %s (no tap)" % (
            [verdict_key(v) for v in votes],))
    return winner


def tree_count(blob: str) -> int:
    """Comment count off tree text ('N comments'). Pure. 0 = none shown."""
    low = (blob or "").lower()
    m = re.search(r"(\d+)\s*comments", low)
    return int(m.group(1)) if m else 0


def st_classify(ctx: Ctx) -> dict:
    # Layout belongs to the tree (coarse text signals are reliable);
    # eyes owns rows/controls (geometry). Locked 24 Sep: qwen voted
    # 3-of-3 composer on plain post screens twice.
    blob, nodes = tree_blob(ctx, "901_tree")
    if composer_open_in_tree(blob, nodes):
        ctx.mark("CLASSIFY layout=composer (tree)")
        png = _shot_png(ctx)
        ctx.last_png = png
        seen = vote_eyes(ctx, png)
        seen["layout"] = "composer"
        return seen
    ctx.mark("CLASSIFY layout!=composer (tree), voting rows")
    png = _shot_png(ctx)
    ctx.last_png = png
    seen = vote_eyes(ctx, png)
    rows = [r for r in seen.get("rows", []) if r.get("author")]
    count = seen.get("count") or 0
    tcount = tree_count(blob)
    if tcount > count:
        count = tcount
    seen = dict(seen, layout="comments_list" if (rows or count) else "post",
                count=count)
    ctx.mark("CLASSIFY layout=%s rows=%d count=%s" % (
        seen["layout"], len(rows), count))
    return seen


CROP_PROMPT = """You see a 2x zoomed crop of an iPhone Reddit screenshot. Find the single Reply button (reply-arrow glyph + 'Reply' text) in this crop. Return STRICT JSON only: {"reply_xy": [x, y]} in ORIGINAL 750x1334 device pixels. The crop covers original pixels x=%d..%d, y=%d..%d and was scaled 2x: original_x = %d + crop_x/2, original_y = %d + crop_y/2. Measure the glyph center exactly, never guess or round."""


def refine_xy(ctx: Ctx, xy: list) -> list:
    """Crop-zoom refine: 2x crop around the eyes point, re-read exact pixels.

    Locked 24 Sep: full-frame eyes xy burned 5 runs (all dead taps). The
    manual tap that worked was measured off a 2x crop. Bounded: 2 attempts.
    """
    from PIL import Image
    raw = getattr(ctx, "last_png", b"")
    if not raw:
        return xy
    for attempt in (1, 2):
        x0 = max(0, int(xy[0]) - 150)
        y0 = max(0, int(xy[1]) - 150)
        try:
            crop_bytes, (cx0, cy0, cx1, cy1) = prep_crop(raw, x0, y0)
        except Exception:
            return xy
        buf = io.BytesIO(crop_bytes)
        import requests
        key = load_api_key()
        if not key:
            return xy
        if ctx.eyes_used >= MAX_EYES:
            ctx.mark("REFINE eyes budget exhausted, keeping xy")
            return xy
        ctx.eyes_used += 1
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        ctx.mark("REFINE crop-zoom attempt %d (eyes %d/%d)" % (attempt, ctx.eyes_used, MAX_EYES))
        try:
            r = requests.post(
                OPENROUTER,
                headers={"Authorization": "Bearer " + key,
                         "Content-Type": "application/json",
                         "HTTP-Referer": "https://github.com/reddit-bot",
                         "X-Title": "Reddit iPhone Eyes"},
                json={"model": EYES_MODEL,
                      "messages": [{"role": "user", "content": [
                          {"type": "text", "text": CROP_PROMPT % (cx0, cx1, cy0, cy1, cx0, cy0)},
                          {"type": "image_url", "image_url": {
                              "url": "data:image/png;base64," + b64}}]}],
                      "max_tokens": 100, "temperature": 0.0},
                timeout=90)
        except Exception as e:
            ctx.mark("REFINE transport fail: %s" % str(e)[:80])
            return xy
        if r.status_code != 200:
            ctx.mark("REFINE HTTP %d, keeping xy" % r.status_code)
            return xy
        try:
            text = (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
            m = re.search(r"\{.*\}", text, re.S)
            got = json.loads(m.group(0))["reply_xy"] if m else None
        except Exception:
            got = None
        if (isinstance(got, list) and len(got) == 2
                and all(isinstance(v, (int, float)) for v in got)):
            dx, dy = abs(got[0] - xy[0]), abs(got[1] - xy[1])
            if dx <= 150 and dy <= 150:
                ctx.mark("REFINE (%s,%s)->(%s,%s) dx=%d dy=%d" % (
                    xy[0], xy[1], got[0], got[1], got[0] - xy[0], got[1] - xy[1]))
                return [int(got[0]), int(got[1])]
            ctx.mark("REFINE out-of-crop answer, keeping xy")
            return xy
        ctx.mark("REFINE no parse, keeping xy")
    return xy


def post_ident(blob: str, item: dict) -> str:
    """Same-post identity: title words + sub both on screen. Pure."""
    low = (blob or "").lower()
    title = (item.get("title") or "").strip().lower()
    words = [w for w in re.findall(r"[a-z0-9]{4,}", title)][:4]
    sub = (item.get("sub") or "").strip().lower()
    if words and sub and all(w in low for w in words) and ("r/" + sub) in low:
        return title[:60] + "|" + sub
    return ""


def carry_forward(prev: dict | None, cur: dict) -> tuple[dict, str]:
    """Same-post persistence: later rows=0 is a sensor miss, not reality.

    cur with rows wins (fresh). cur empty + prev with rows wins (carried).
    Both empty stays empty. Caller only invokes on matching post identity;
    identity change resets persistence (full re-classify).
    """
    cur_rows = [r for r in (cur.get("rows") or []) if r.get("author")]
    if cur_rows:
        return cur, "fresh"
    if prev:
        prev_rows = [r for r in (prev.get("rows") or []) if r.get("author")]
        if prev_rows:
            out = dict(prev)
            out["count"] = cur.get("count") or prev.get("count") or 0
            return out, "sensor-miss: carried %d row(s) forward" % len(prev_rows)
    return cur, "empty"


def header_text(nodes, y_max: int = 400) -> str:
    """Lowercased text of the post-header zone (tree). Pure."""
    parts = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if n.y < y_max:
            parts.append(collapsed_text(n.labels()))
    return " ".join(parts).lower()


def visible_rows(seen: dict, header: str = "") -> list[dict]:
    """Eyes rows minus post-header authors minus chrome zones. Pure.

    A comment-row Reply lives mid-screen (300<=x<=650, 200<=y<=1150).
    Top chrome (y<200) and the bottom Join bar (y>1150) are never rows.
    Locked 24 Sep: eyes returned (375,100) top-chrome and (375,1230) Join.
    """
    rows = [r for r in seen.get("rows", []) if r.get("author")]
    if header:
        rows = [r for r in rows
                if (r.get("author") or "").lstrip("u/").strip().lower() not in header]
    out = []
    for r in rows:
        xy = r.get("reply_xy")
        if not (isinstance(xy, list) and len(xy) == 2):
            continue
        if xy[1] < 200 or xy[1] > 1150 or xy[0] < 300 or xy[0] > 650:
            continue
        out.append(r)
    return out


def corroborated_rows(seen: dict, header: str, nodes) -> list[dict]:
    """visible_rows + tree tap-point corroboration + snap + association. Pure.

    Eyes hallucinates plausible Reply coordinates (locked 24 Sep: dead
    taps at eyes-given points, all x=375 center-defaults). A row is
    tappable only if the tree shows a short control node within 120px
    of reply_xy — and the tap point SNAPS to that node center (tree
    geometry is exact; eyes geometry drifts). No nearby control =
    refused, never fired. The GATE (Replying-to match) stays backstop.
    Association (locked 24 Sep, animequestions): eyes attached a real
    Reply button to the author ABOVE its owner (off-by-one row). The
    Reply belongs to the nearest author-like tree node above it: if
    that name differs from the eyes author, the row is REASSIGNED to
    the tree name (its spelling also matches the gate best).
    """
    out = []
    for r in visible_rows(seen, header):
        xy = r["reply_xy"]
        best, best_d = None, 1e9
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            if n.w >= 150 or n.h > 80:
                continue
            cx, cy = n.center()
            d = abs(cx - xy[0]) + abs(cy - xy[1])
            if d < best_d:
                best, best_d = n.center(), d
        if best is None or abs(best[0] - xy[0]) > 120 or abs(best[1] - xy[1]) > 120:
            continue
        r = dict(r, reply_xy=[best[0], best[1]],
                 eyes_xy=xy, snap_delta=round(best_d))
        owner, ody = "", 1e9
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            t = collapsed_text(n.labels())
            single = "".join(t.split())
            if len(single) < 3 or len(single) > 20 or not single.replace("_", "").replace("-", "").isalnum():
                continue
            if n.x > best[0] + 100:
                continue
            dy = best[1] - n.y
            if 20 < dy < 450 and dy < ody:
                owner, ody = single, dy
        if owner and owner != (r.get("author") or "").lower():
            r = dict(r, author=owner, reassigned=True)
        out.append(r)
    return out


def st_acquire(ctx: Ctx, seen: dict, header: str = "", nodes=None) -> dict:
    layout = seen["layout"]
    if layout == "composer":
        ctx.fail("ACQUIRE", "composer already open at entry (discard first)")
    n_all = len([r for r in seen["rows"] if r.get("author")])
    rows = corroborated_rows(seen, header, nodes or [])
    if len(rows) != n_all:
        ctx.mark("ACQUIRE %d row(s) filtered (header/chrome/uncorroborated)" % (n_all - len(rows)))
    if rows:
        tgt = rows[0]
        ctx.target = tgt
        ctx.mode = "reply"
        ctx.mark("ACQUIRE author=%s score=%s reply_xy=%s (first-row wins)" % (
            tgt["author"], tgt.get("score"), tgt.get("reply_xy")))
        return tgt
    count = seen.get("count") or 0
    if isinstance(count, int) and count > 0:
        ctx.mark("ACQUIRE count=%d, rows below -> scroll then re-classify" % count)
        ctx.mode = "scroll"
        return {}
    if layout == "post" and seen.get("bubble_xy"):
        ctx.target = {"author": "", "score": -1,
                      "reply_xy": seen["bubble_xy"], "bubble": True}
        ctx.mode = "comment"
        ctx.mark("ACQUIRE bubble-direct at %s (top-level route)" % (seen["bubble_xy"],))
        return ctx.target
    ctx.fail("ACQUIRE", "BARE_POST: no rows, count 0, no bubble (report only)")


def gate_ok(blob: str, author: str) -> bool:
    """Hard gate: composer shows Replying-to <author>. Pure (regression-tested)."""
    low = (blob or "").lower()
    want = (author or "").strip().lower()
    return "replying to" in low and bool(want) and want in low


def st_gate(ctx: Ctx) -> None:
    t0 = time.time()
    tgt = ctx.target or {}
    xy = tgt.get("reply_xy")
    if not xy:
        ctx.fail("GATE", "no target xy")
    try:
        exy, rxy = tgt.get("eyes_xy", xy), xy
        dx, dy = rxy[0] - exy[0], rxy[1] - exy[1]
        ctx.mark("BIAS snap dx=%d dy=%d (eyes %s -> tap %s)" % (dx, dy, exy, rxy))
        audit_append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "sub": ctx.item.get("sub", ""), "author": tgt.get("author", ""),
                      "eyes_xy": tgt.get("eyes_xy", xy), "reply_xy": xy,
                      "dx": dx, "dy": dy,
                      "snap_delta": tgt.get("snap_delta", 0)})
    except Exception:
        pass
    ctx.mark("GATE tap Reply %s" % (xy,))
    ctx.stack.tap_xy(int(xy[0]), int(xy[1]))
    time.sleep(1.2)
    blob, _nodes = tree_blob(ctx, "3035_reply_composer")
    low = (blob or "").lower()
    if ctx.mode == "comment":
        if "replying to" in low:
            ctx.fail("GATE", "bubble route nested unexpectedly (Replying-to present)")
        ctx.mark("GATE_OK top-level composer (no Replying-to line)")
        return
    want = (tgt.get("author") or "").strip().lower()
    if not gate_ok(blob, want):
        discard_composer(ctx)
        ctx.fail("GATE", "Replying-to mismatch (want %s)" % (want or "?"))
    ctx.check_budget("GATE", t0)
    ctx.mark("GATE_OK Replying to %s" % want)


def discard_composer(ctx: Ctx) -> bool:
    blob, nodes = tree_blob(ctx, "3036_discard")
    x = find_label_exact(nodes, ("X", "x", "Cancel", "cancel"))
    if x is None:
        ctx.mark("DISCARD no X/Cancel found")
        return False
    try:
        xy = x.center()
    except Exception:
        xy = (x.x, x.y)
    ctx.stack.tap_xy(*xy)
    time.sleep(1.2)
    blob, _n = tree_blob(ctx, "3037_discarded")
    low = (blob or "").lower()
    ok = "replying to" not in low and "commenting on" not in low
    ctx.mark("DISCARDED=%s" % ok)
    return ok


def st_type(ctx: Ctx) -> None:
    from reddit_reply_direct import wait_go
    text = (ctx.item.get("reply_text") or "").strip()
    t0 = time.time()
    ctx.stack.type_human(text, ms=95)
    time.sleep(0.8)
    ctx.check_budget("TYPE", t0)
    blob, _n = tree_blob(ctx, "3040_typed")
    words = [w.strip(".,!?'\"").lower() for w in text.split()]
    distinctive = next((w for w in sorted(words, key=len, reverse=True) if len(w) >= 5), "")
    if distinctive and distinctive not in (blob or "").lower():
        ctx.mark("TYPE retry slower (drop guard)")
        ctx.stack.type_human(text, ms=150)
        time.sleep(0.8)
        blob, _n = tree_blob(ctx, "3040_retyped")
        if distinctive not in (blob or "").lower():
            ctx.fail("TYPE", "typed text not verifiable in tree")
    if time.time() - t0 > STATE_TIMEOUT:
        ctx.fail("TYPE", "state timeout")
    ctx.mark("TYPE_OK verify path: dumps/comment_go.txt (go wait is human-paced, exempt)")
    wait_go("3040_typed.png")


def st_send(ctx: Ctx) -> None:
    t0 = time.time()
    png = _shot_png(ctx)
    seen = eyes(ctx, png)
    xy = seen.get("send_xy")
    if not xy:
        ctx.fail("SEND", "no send button in eyes")
    last = None
    for i in range(3):
        x, y = int(xy[0]) + 10 * i, int(xy[1]) + 10 * i
        ctx.check_budget("SEND", t0)
        t1 = time.time()
        ctx.mark("SEND tap %d/3 (%d,%d)" % (i + 1, x, y))
        try:
            ctx.stack.tap_xy(x, y)
        except Exception as e:
            last = e
            continue
        time.sleep(3.0)
        blob, _n = tree_blob(ctx, "3050_sent")
        if sent_ok(blob):
            ctx.mark("SEND_OK elapsed=%.1fs" % (time.time() - t1))
            ctx._sent_blob = blob
            return
        last = "send not proven"
    ctx.fail("SEND", "send not proven after 3 taps (%s)" % str(last)[:80])


def sent_ok(blob: str) -> bool:
    ctx_text = getattr(sent_ok, "_ctx_text", "")
    ctx_user = getattr(sent_ok, "_ctx_user", "")
    low = (blob or "").lower()
    words = [w.strip(".,!?'\"").lower() for w in ctx_text.split()]
    distinctive = next((w for w in sorted(words, key=len, reverse=True) if len(w) >= 5), "")
    need = [ctx_user.strip().lower(), "now"]
    if distinctive:
        need.append(distinctive)
    need = [n for n in need if n]
    return bool(need) and all(n in low for n in need)


def st_verify(ctx: Ctx) -> None:
    # Standing Law 2: local VERIFY stands alone, no web-verify. Strict:
    # username + 'now' + reply word, all three, or FAIL.
    t0 = time.time()
    blob = getattr(ctx, "_sent_blob", "")
    low = (blob or "").lower()
    words = [w.strip(".,!?'\"").lower()
             for w in (ctx.item.get("reply_text") or "").split()]
    distinctive = next((w for w in sorted(words, key=len, reverse=True) if len(w) >= 5), "")
    need = [ctx.username.strip().lower(), "now"]
    if distinctive:
        need.append(distinctive)
    need = [n for n in need if n]
    if not (need and all(n in low for n in need)):
        ctx.fail("VERIFY", "local proof missing (need %s)" % "+".join(need))
    ctx.check_budget("VERIFY", t0)
    ctx.mark("VERIFY local strict ok (%s)" % "+".join(need))


def web_comment_present(url: str, username: str, text: str) -> bool:
    from playwright.sync_api import sync_playwright
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, os.path.join(root, "misc"))
    from comment_precache import launch_ctx
    words = [w.strip(".,!?'\"").lower() for w in (text or "").split()]
    distinctive = next((w for w in sorted(words, key=len, reverse=True) if len(w) >= 5), "")
    with sync_playwright() as pw:
        pctx = launch_ctx(pw)
        try:
            page = pctx.pages[0] if pctx.pages else pctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(7000)
            page.evaluate("window.scrollBy(0, 1500)")
            page.wait_for_timeout(3000)
            rows = page.evaluate(
                "() => [...document.querySelectorAll('shreddit-comment')].map(e => ({"
                "a: e.getAttribute('author'), "
                "t: (e.innerText || '').slice(0, 300)}))")
            for r in rows:
                if ((r.get("a") or "").lower() == (username or "").lower()
                        and distinctive and distinctive in (r.get("t") or "").lower()):
                    return True
            return False
        finally:
            try:
                pctx.close()
            except Exception:
                pass


def st_mark(ctx: Ctx) -> None:
    key = mark_commented(ctx.item.get("sub", ""), ctx.item.get("title", ""),
                         ctx.item.get("slot", "a0003"), ctx.mode)
    ctx.mark("MARKED " + str(key))


def st_close(ctx: Ctx) -> None:
    close_reddit(ctx.stack, ctx.phone, ctx.log)
    close_safari_reddit_tab(ctx.stack, ctx.phone, ctx.log)
    ctx.mark("CLOSE ok (reddit shut, one Start Page tab)")


def safe_park(ctx: Ctx) -> None:
    """Standing Law 1: halt taps instantly, park, no auto-retry. One-tab
    rule: aborts also close the Safari Reddit tab (locked 24 Sep: every
    GATE abort left its tab, breaking the next safari_open). Best-effort."""
    ctx.mark("SAFE_PARK press_home + one-tab close, waiting for human paste")
    try:
        from reddit_post import close_safari_reddit_tab
        close_safari_reddit_tab(ctx.stack, ctx.phone, ctx.log)
    except Exception as e:
        ctx.mark("SAFE_PARK tab close skipped: %s" % str(e)[:100])
    try:
        ctx.stack.press_home()
    except Exception as e:
        ctx.mark("SAFE_PARK home failed: %s" % str(e)[:80])


def run_reply(stack: TapStack, phone, log, item: dict, username: str) -> dict:
    """Full machine. Returns {ok, state, timeline, url?}. Never raises."""
    ctx = Ctx(stack, phone, log, dict(item), username)
    sent_ok._ctx_text = (item.get("reply_text") or "")
    sent_ok._ctx_user = username
    try:
        st_enter(ctx)
        ctx.check_total("ENTER")
        mean_px, n_audit, miscal = audit_check(audit_entries())
        if miscal:
            ctx.mark("EYES_MISCALIBRATED standing state: mean snap %.0fpx over %d runs" % (mean_px, n_audit))
        elif n_audit:
            ctx.mark("EYES calibration: mean snap %.0fpx over %d runs" % (mean_px, n_audit))
        # CLASSIFY -> ACQUIRE with bounded scroll loop (Standing Law 3: <=3min)
        # Carry-forward: same post identity (title+sub) -> rows/count persist;
        # later rows=0 is a sensor miss, not reality. Identity change resets.
        seen = None
        header = ""
        persist = None
        persist_ident = ""
        carry_streak = 0
        t_acq = time.time()
        for _round in range(MAX_SCROLLS + 1):
            ctx.check_total("CLASSIFY")
            ctx.check_budget("ACQUIRE", t_acq)
            blob0, n0 = tree_blob(ctx, "901_premark")
            note_frozen(ctx, blob0, "CLASSIFY")
            header = header_text(n0)
            ident = post_ident(blob0, ctx.item)
            seen = st_classify(ctx)
            if seen["layout"] == "composer":
                blob0low = (blob0 or "").lower()
                if not composer_open_in_tree(blob0, n0):
                    ctx.mark("CLASSIFY eyes-composer overruled by tree (no keyboard/composer line)")
                    seen = dict(seen, layout="comments_list")
                else:
                    ctx.fail("ACQUIRE", "composer already open at entry")
            if ident and ident == persist_ident and persist is not None:
                if carry_streak >= 2:
                    ctx.mark("CARRY stale (%d rounds), forcing fresh" % carry_streak)
                    persist, persist_ident, carry_streak = None, "", 0
                else:
                    seen, note = carry_forward(persist, seen)
                    ctx.mark("CARRY " + note)
                    if note.startswith("sensor-miss"):
                        carry_streak += 1
                    else:
                        carry_streak = 0
            elif ident:
                persist_ident = ident
            elif persist_ident:
                ctx.mark("CARRY identity latched (title scrolled off, same post)")
            else:
                persist, persist_ident, carry_streak = None, "", 0
                ctx.mark("CARRY identity lost, full re-classify")
            rows = corroborated_rows(seen, header, n0)
            if rows:
                persist = seen
                break
            count = seen.get("count") or 0
            bubble = seen.get("bubble_xy")
            if isinstance(count, int) and count > 0:
                ctx.mark("CLASSIFY count=%d below -> slow-drag" % count)
                slow_drag_down(ctx)
                continue
            if seen["layout"] == "post" and bubble:
                break
            ctx.fail("ACQUIRE", "BARE_POST (report only)")
        tgt = st_acquire(ctx, seen, header, n0)
        ctx.check_total("ACQUIRE")
        if ctx.mode == "scroll":
            slow_drag_down(ctx)
            seen = st_classify(ctx)
            blob1, n1 = tree_blob(ctx, "901_premark2")
            tgt = st_acquire(ctx, seen, header_text(n1), n1)
            if ctx.mode == "scroll":
                ctx.fail("ACQUIRE", "still no rows after scroll")
        if ctx.mode == "reply" and tgt.get("reply_xy"):
            tgt["reply_xy"] = refine_xy(ctx, list(tgt["reply_xy"]))
        st_gate(ctx)
        ctx.check_total("GATE")
        st_type(ctx)
        ctx.check_total("TYPE")
        st_send(ctx)
        ctx.check_total("SEND")
        st_verify(ctx)
        st_mark(ctx)
        st_close(ctx)
        ctx.mark("DONE url=%s" % ctx.item.get("full_url", ""))
        return {"ok": True, "state": "DONE", "timeline": ctx.timeline,
                "url": ctx.item.get("full_url", "")}
    except Abort as a:
        safe_park(ctx)
        return {"ok": False, "state": a.state, "reason": a.reason,
                "timeline": a.timeline}
    except Exception as e:
        ctx.mark("ABORT [CRASH] %s: %s" % (type(e).__name__, str(e)[:150]))
        return {"ok": False, "state": "CRASH",
                "reason": "%s: %s" % (type(e).__name__, str(e)[:150]),
                "timeline": ctx.timeline}
