# UTF-8
"""Regression gate for reply_state.py. Offline: archived dumps + synthetic
eyes payloads, no phone, no API. Every case must abort-or-route correctly.
Run: python misc/reply_regress.py
"""

from __future__ import annotations

import json
import os
import sys
from friend_poster.device.ui_tree import Node  # noqa: E402
from friend_poster.core.reply_state import Abort, Ctx, gate_ok, note_frozen, st_acquire  # noqa: E402
from friend_poster.reddit.reddit_reply_direct import classify_post_layout, list_comment_rows  # noqa: E402
from friend_poster.core.paths import PROJECT_ROOT

ROOT = str(PROJECT_ROOT)
PASS = []
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" :: " + detail if detail else ""), flush=True)


def nodes_of(stem: str) -> tuple[list, str]:
    raw = json.load(open(os.path.join(ROOT, "dumps", stem + ".json"), encoding="utf-8"))
    nodes = [Node(text=d.get("text", ""), x=d.get("x", 0), y=d.get("y", 0),
                  w=d.get("w", 0), h=d.get("h", 0)) for d in raw]
    return nodes, " ".join(d.get("text", "") for d in raw)


def ctx0() -> Ctx:
    return Ctx(None, None, print, {"reply_text": "x", "full_url": "http://x/"}, "u")


def main() -> int:
    # 1. ghost author: authorless Reply row must never be tappable
    nodes, _b = nodes_of("3031_hunt4")
    named = [r for r in list_comment_rows(nodes) if r.get("author")]
    check("ghost-author", not named, "authorless Reply refused")
    # 2. tree-missing rows: unscrolled header with count>0 is BELOW, not a verdict
    nodes, blob = nodes_of("980_phase1")
    check("tree-missing-rows",
          classify_post_layout(nodes, blob)[0] == "COMMENTS_BELOW", "980 -> BELOW")
    # 3. frozen swipes: 3 identical blobs abort
    c = ctx0()
    try:
        for _ in range(3):
            note_frozen(c, "sameblob", "ACQUIRE")
        check("frozen", False, "did not abort")
    except Abort as a:
        check("frozen", "frozen" in a.reason, a.reason)
    # 4. overshoot: rows gone + count 0 + no bubble aborts BARE
    c = ctx0()
    try:
        st_acquire(c, {"layout": "post", "rows": [], "count": 0, "bubble_xy": None})
        check("overshoot", False, "did not abort")
    except Abort as a:
        check("overshoot", "BARE_POST" in a.reason, a.reason)
    # 5. wrong-row tap: gate blocks mismatched Replying-to
    # (inline fixture: dumps/3035_*.json is overwritten every live run)
    blob_hit = ("r/Ben10 Jen10 Crimson Wrath v3 by @carpinchor "
                "KrimxonRath 1h Rath Replying to KrimxonRath Join the conversation")
    check("gate-hit", gate_ok(blob_hit, "krimxonrath"), "matched author passes")
    check("gate-block", not gate_ok(blob_hit, "someotheruser") and not gate_ok("no composer", "krimxonrath"),
          "mismatch + no-composer blocked")
    # 6. bubble-direct: post + count 0 + bubble routes top-level
    c = ctx0()
    tgt = st_acquire(c, {"layout": "post", "rows": [], "count": 0, "bubble_xy": [310, 1195]})
    check("bubble-direct", c.mode == "comment" and tgt["reply_xy"] == [310, 1195], "top-level route")
    # 7. comments-below: count>0 zero rows requests scroll, never taps
    c = ctx0()
    out = st_acquire(c, {"layout": "post", "rows": [], "count": 7, "bubble_xy": None})
    check("comments-below", c.mode == "scroll" and out == {}, "scroll sentinel")
    # 8. header-author: eyes row matching the post header is filtered, not tapped
    c = ctx0()
    out = st_acquire(c, {"layout": "comments_list",
                         "rows": [{"author": "u/5_meo", "score": 30,
                                   "reply_xy": [375, 1260]}],
                         "count": 13, "bubble_xy": None},
                     header="r/animequestions u/5_meo 46m what anime had you like this")
    check("header-author", c.mode == "scroll" and out == {}, "post-header row filtered")
    # 9. bottom-bar: Reply xy in the Join-field zone is chrome, not a row
    c = ctx0()
    out = st_acquire(c, {"layout": "comments_list",
                         "rows": [{"author": "CrimsonThar", "score": 4,
                                   "reply_xy": [375, 1230]}],
                         "count": 14, "bubble_xy": None},
                     header="r/animequestions u/5_meo 51m")
    check("bottom-bar", c.mode == "scroll" and out == {}, "Join-zone Reply filtered")
    # 10. top-chrome: Reply xy in the header zone is chrome, not a row
    c = ctx0()
    out = st_acquire(c, {"layout": "comments_list",
                         "rows": [{"author": "CrimsonThar", "score": 4,
                                   "reply_xy": [375, 100]}],
                         "count": 14, "bubble_xy": None},
                     header="r/animequestions u/5_meo 51m")
    check("top-chrome", c.mode == "scroll" and out == {}, "header-zone Reply filtered")
    # 11. composer corroboration: keyboard rows prove open composer
    from friend_poster.core.reply_state import composer_open_in_tree, corroborated_rows  # noqa: E402
    kb = [Node(text=t, x=100 + 40 * i, y=1100, w=30, h=30)
          for i, t in enumerate("qwertyuiopas".upper())]
    check("composer-kb", composer_open_in_tree("", kb), "12 key rows = open")
    plain = [Node(text="r/animequestions", x=100, y=150, w=200, h=27),
             Node(text="Join the conversation", x=43, y=1270, w=293, h=27)]
    check("composer-shut", not composer_open_in_tree("r/animequestions join the conversation", plain),
          "no keys/lines = shut")
    # 12. tap-point corroboration: eyes xy with no tree control nearby refused
    seen12 = {"layout": "comments_list",
              "rows": [{"author": "CrimsonThar", "score": 4, "reply_xy": [375, 1020]}],
              "count": 14, "bubble_xy": None}
    far = [Node(text="CrimsonThar", x=115, y=760, w=212, h=23),
           Node(text="Me when someone", x=32, y=825, w=681, h=26)]
    check("uncorroborated", corroborated_rows(seen12, "r/animequestions u/5_meo", far) == [],
          "no control within 120px refused")
    near = far + [Node(text="Reply", x=430, y=1000, w=80, h=31)]
    got = corroborated_rows(seen12, "r/animequestions u/5_meo", near)
    check("corroborated", len(got) == 1 and got[0]["reply_xy"] == [470, 1015],
          "snapped to tree geometry (470,1015)")
    # 13. association: Reply belongs to nearest author above, not eyes' pick
    seen13 = {"layout": "comments_list",
              "rows": [{"author": "SwimmingDrink", "score": -1, "reply_xy": [460, 500]}],
              "count": 15, "bubble_xy": None}
    nodes13 = [Node(text="Swimming Drink", x=100, y=150, w=200, h=28),
               Node(text="issanoir_", x=100, y=350, w=166, h=26),
               Node(text="For me it was Sword Art Online", x=33, y=400, w=552, h=33),
               Node(text="< Reply", x=400, y=480, w=110, h=30)]
    got13 = corroborated_rows(seen13, "r/animequestions u/5_meo", nodes13)
    check("reassign", len(got13) == 1 and got13[0]["author"] == "issanoir_"
          and got13[0].get("reassigned") is True,
          "off-by-one row reassigned to tree owner")
    # 13. carry-forward: 27 rows then 0 rows, same identity = sensor miss
    from friend_poster.core.reply_state import carry_forward, post_ident  # noqa: E402
    item13 = {"title": "What anime had you like this?", "sub": "animequestions"}
    blob13 = "r/animequestions u/5_meo 51m What anime had you like this? Discussion"
    check("identity", post_ident(blob13, item13) != "", "title+sub on screen")
    check("identity-lost", post_ident("r/animequestions something else", item13) == "",
          "title gone = lost")
    prev13 = {"layout": "comments_list",
              "rows": [{"author": "CrimsonThar", "score": 4, "reply_xy": [375, 900]}],
              "count": 27, "bubble_xy": None}
    cur13 = {"layout": "comments_list", "rows": [], "count": 0, "bubble_xy": None}
    eff, note = carry_forward(prev13, cur13)
    check("carry-27-0", len(eff["rows"]) == 1 and note.startswith("sensor-miss"),
          "rows persist across miss")
    eff2, note2 = carry_forward(prev13, dict(cur13, rows=[{"author": "X", "reply_xy": [1, 2]}]))
    check("carry-fresh", note2 == "fresh", "fresh rows win")
    # 14. variance: verdict key + 2-of-3 tally
    from friend_poster.core.reply_state import verdict_key, vote_tally, audit_check, prep_frame, prep_crop  # noqa: E402
    a = {"layout": "comments_list",
         "rows": [{"author": "CrimsonThar", "score": 4, "reply_xy": [1, 2]}],
         "count": 14}
    b = {"layout": "comments_list",
         "rows": [{"author": "crimsonthar", "score": 9, "reply_xy": [7, 8]}],
         "count": 14}
    c = {"layout": "post", "rows": [], "count": 14}
    check("verdict-key", verdict_key(a) == verdict_key(b) != verdict_key(c),
          "scores/xy ignored, layout+authors+count rule")
    win, tally = vote_tally([a, b, c])
    check("vote-2-1", win is not None and tally == "2-of-3", "majority wins")
    win0, tally0 = vote_tally([a, c, {"layout": "composer", "rows": [], "count": 0}])
    check("vote-unstable", win0 is None and tally0 == "1-1-1", "disagreement = unstable")
    # 15. variance: snap audit states
    check("audit-cold", audit_check([{"snap_delta": 200}]) == (200.0, 1, False),
          "n<5 never miscalibrated")
    big = [{"snap_delta": d} for d in (90, 110, 95, 105, 100, 120)]
    mean, n, mis = audit_check(big)
    check("audit-miscal", mis and n == 6 and round(mean) == 103, "mean>60 over 6 = EYES_MISCALIBRATED")
    small = [{"snap_delta": d} for d in (10, 20, 15, 25, 12, 18)]
    check("audit-ok", audit_check(small)[2] is False, "tight snaps = calibrated")
    # 16. variance: deterministic preprocessing (fixed sizes, stable bytes)
    from PIL import Image  # noqa: E402
    im = Image.new("RGB", (750, 1334), (120, 130, 140))
    import io as _io  # noqa: E402
    buf = _io.BytesIO()
    im.save(buf, format="PNG")
    raw = buf.getvalue()
    f1, f2 = prep_frame(raw), prep_frame(raw)
    check("prep-deterministic", f1 == f2, "same pixels in = same bytes out")
    with Image.open(_io.BytesIO(f1)) as o:
        check("prep-size", o.size == (750, 1334), "fixed frame size")
    cr, box = prep_crop(raw, 400, 800)
    with Image.open(_io.BytesIO(cr)) as o:
        check("crop-size", o.size == (600, 600) and box == (400, 800, 700, 1100),
              "fixed 600x600 crop + box")
    print("REGRESS %d pass %d fail" % (len(PASS), len(FAIL)), flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
