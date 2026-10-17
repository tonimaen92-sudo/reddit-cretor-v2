# UTF-8
"""Regression for the locked commenter path. Offline: archived dumps +
synthetic nodes only, no phone, no API. Green gate for run_commenter.bat.
Run: python misc/commenter_regress.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from friend_poster.device.ui_tree import Node  # noqa: E402
from friend_poster.device.ui_tree import find_continue_gate as _gate  # noqa: E402
from friend_poster.device.ui_tree import find_update_settings as _updg  # noqa: E402
from friend_poster.apps.commenter import (  # noqa: E402
    App,
    DEAD_REASON,
    ENTRY_ABORTS,
    LOGOUT_FLAGS,
    batch_attempt_cap,
    canonical_pick,
    accounts_row_username,
    draw_break_minutes,
    draw_spread_minutes,
    _last_posted,
    spread_day_end_ts,
    drawer_logged_out,
    error_line,
    escalation_due,
    lockin_fireable,
    mark_day,
    parse_dry_perma,
    never_block,
    need_vision,
    pick_another,
    profile_url,
    q_blocked,
    q_tick_dict,
    list_row,
    resolve_backup_file,
    result_rows,
    same_container,
    nsfw_block,
    rotation_pick_in_order,
    seeded_shuffle,
    slot_refused,
    slot_usable,
    try_order,
    stats_from_marks,
    stats_from_runs,
    streak_step,
    sub_break_block,
    typed_echo_ok,
    ui_important,
    uiopen_land_proof,
    voice_ok,
)
from friend_poster.services.comment_llm import build_mimo_prompt, build_vision_prompt, describe_image_bytes, gallery_full_url, is_image_bytes, ladder_plan, mime_of, parse_analysis, rank_candidates, strip_preview_params, vision_bytes_ok, vision_refused  # noqa: E402
from friend_poster.services.comment_precache import POST_BODY_JS, POST_IMG_JS, cdp_endpoint, dedupe_raws, is_profile_lock_error, pick_post_body, profile_in_cmdline  # noqa: E402
from friend_poster.services import comment_store_ios as _store  # noqa: E402
from friend_poster.reddit.login_probe import drawer_closed_ok, drawer_markers, find_avatar, find_hamburger, login_verdict, verdict_record  # noqa: E402
from friend_poster.services.safari_bookmarks import FARM_WORD, _parse_md5, build_modified, delete_url_sql, entry_words, safari_card, wipe_leaves_sql  # noqa: E402
from friend_poster.reddit.reddit_open import switcher_changed  # noqa: E402
from friend_poster.core.slots import retired_ids  # noqa: E402
from friend_poster.core.farm_stats import failed_poster_today, failed_today  # noqa: E402
from friend_poster.core.paths import PROJECT_ROOT

ROOT = str(PROJECT_ROOT)
PASS = []
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" :: " + detail if detail else ""), flush=True)


def nodes_of(stem: str):
    raw = json.load(open(os.path.join(ROOT, "dumps", stem + ".json"), encoding="utf-8"))
    return [Node(text=d.get("text", ""), x=d.get("x", 0), y=d.get("y", 0),
                 w=d.get("w", 0), h=d.get("h", 0)) for d in raw]


def blob_of(stem: str) -> str:
    raw = json.load(open(os.path.join(ROOT, "dumps", stem + ".json"), encoding="utf-8"))
    return " ".join(d.get("text", "") for d in raw)


def main() -> int:
    # 1. retired: a0007 excluded per user order, a0019 restored to rotation
    check("retired-a0007", "a0007" in retired_ids(), "a0007 in retired.json")
    check("guard-a0007", not slot_usable({"id": "a0007", "ghost": True})
          and slot_refused("a0007"), "unusable + refused")
    check("retired-a0019", "a0019" in retired_ids(), "a0019 retired")
    check("active-a0021", "a0021" not in retired_ids(), "a0021 active")
    check("guard-a0003", slot_usable({"id": "a0003", "ghost": True})
          and not slot_refused("a0003"), "a0003 batches")
    check("guard-empty", not slot_usable({}) and not slot_usable(None), "empty row refused")
    # 4. streak: cleans climb to 20-bar, aborts reset
    check("streak-up", streak_step(1, "posted") == 2, "1 -> 2")
    check("streak-cap", streak_step(20, "posted") == 20, "caps at 20")
    check("streak-reset", streak_step(7, "abort") == 0, "abort resets")
    # 5. post URL parse (lock-in manual path guard)
    sub, pid, slug = App._parse_post_url(
        "https://www.reddit.com/r/Ben10/comments/1wpoy0s/selfie_heatblast_and_upgrade_mind_868/")
    check("url-parse", (sub, pid) == ("ben10", "1wpoy0s") and "selfie" in slug,
          "sub+id+slug")
    check("url-bad", App._parse_post_url("not a url") == (None, None, None), "junk refused")
    check("dry-full", parse_dry_perma(
        "https://www.reddit.com/r/TheDigitalCircus/comments/1wqo5ys/ship_post_not_mine/")
        == "/r/TheDigitalCircus/comments/1wqo5ys/ship_post_not_mine/", "full URL")
    check("dry-bare", parse_dry_perma(
        "/r/ben10/comments/abc123") == "/r/ben10/comments/abc123/", "bare path")
    check("dry-bad", parse_dry_perma("not a url") == "", "junk refused")
    # 6. value-law tree echo: full words + capital-first, no drops
    typed_blob = blob_of("a3851_typed")
    text = "Fire and tech aliens bonding over a selfie"
    check("echo-full", typed_echo_ok(typed_blob, text), "live typed dump echoes")
    check("echo-nocaps", not typed_echo_ok(typed_blob, text[:1].lower() + text[1:]),
          "lowercased first fails capital rule")
    check("echo-missing", not typed_echo_ok(typed_blob, "Fire and tech aliens bonding over a zebra"),
          "absent word fails")
    check("echo-empty", not typed_echo_ok("", text) and not typed_echo_ok(typed_blob, ""),
          "empty sides fail")
    # 8. abort-stickiness: quarantine ticks down, blocks, releases
    q0 = {"/r/x/comments/1abc/t/": 3, "/r/y/comments/2def/t/": 1}
    q1 = q_tick_dict(q0)
    check("q-tick", q1 == {"/r/x/comments/1abc/t/": 2}, "3->2, 1 releases")
    check("q-block", q_blocked(q1, "/r/x/comments/1abc/t/")
          and not q_blocked(q1, "/r/y/comments/2def/t/"), "block while held")
    check("q-junk", q_tick_dict({"k": "zzz"}) == {}, "non-numeric dropped")
    # 9. (retired: bookmark-route retired with Safari - uiopen is the only route)
    # 10. switcher-open proof: identical blobs mean double_home did nothing
    check("switch-same", not switcher_changed("abc", "abc"), "identical = unopened")
    check("switch-diff", switcher_changed("abc", "abd"), "changed = opened")
    # 11. engine panel: untruncated full context, missing renders dash
    long_title = "T" * 200
    proof = App._engine_proof(None, {"_engine": {
        "post_title": long_title, "header": "u/a - 5m - r/b - 9up",
        "body": "", "footer": "",
        "sub": "b", "score": 9, "age_min": 5.0,
        "top_author": "", "top_body": "", "top_score": 0,
        "vision_model": "openai/gpt-4o-mini", "vision_s": 3.2,
        "image_analysis": {"text_in_image": "none", "content": "C" * 200,
                           "scene": "S"},
        "text_model": "xiaomi/mimo-v2.5-pro", "text_s": 4.1,
        "reply_text": "Some fresh words here", "reply_source": "llm",
        "cleanup": "4 words, lowercased, first capitalized, no punctuation"}})
    check("panel-full", long_title in proof and "C" * 200 in proof
          and "…" not in proof, "200-char fields untruncated")
    check("panel-missing", "body: —" in proof and "footer: —" in proof
          and "top comment: — / — / 0" in proof, "missing renders dash")
    check("panel-blocks", "openai vision: openai/gpt-4o-mini (3.2s)" in proof
          and "text model: xiaomi/mimo-v2.5-pro (4.1s)" in proof
          and "reply: Some fresh words here" in proof, "model+ms+reply blocks")
    # 13. vision byte gate: magic bytes only, HTML refused
    check("img-jpg", is_image_bytes(b"\xff\xd8\xff" + b"\x00" * 100, "image/jpeg"),
          "jpeg passes")
    check("img-png", is_image_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "image/png"),
          "png passes")
    check("img-gif", is_image_bytes(b"GIF89a" + b"\x00" * 100, "image/gif"), "gif passes")
    check("img-html", not is_image_bytes(b"<html>error</html>" + b"\x00" * 100,
                                          "text/html"), "html refused")
    check("img-ctype", not is_image_bytes(b"\xff\xd8\xff" + b"\x00" * 100,
                                           "text/html"), "wrong ctype refused")
    check("img-tiny", not is_image_bytes(b"\xff\xd8", "image/jpeg"), "tiny refused")
    # 14. FIRE gate: vision-less image locks refused, manual/text allowed
    check("fire-clean", lockin_fireable(
        {"reply_text": "x", "_engine": {"has_image": True, "vision_ok": True}}, "x"),
        "vision-backed fires")
    check("fire-blind", not lockin_fireable(
        {"reply_text": "x", "_engine": {"has_image": True, "vision_ok": False}}, "x"),
        "blind image lock refused")
    check("fire-textpost", lockin_fireable(
        {"reply_text": "x", "_engine": {"has_image": False, "vision_ok": False}}, "x"),
        "text post fires")
    check("fire-manual", lockin_fireable({"reply_text": "x"}, "x"), "manual fires")
    check("fire-empty", not lockin_fireable({"reply_text": "x"}, "  ")
          and not lockin_fireable(None, "x"), "empty refused")
    # 15. vision prompt: detail + subreddit context, stable 3-line format
    vp = build_vision_prompt("ben10", "Selfie Heatblast", "")
    check("vp-headers", all(h in vp for h in ("TEXT_IN_IMAGE:", "CONTENT:", "SCENE:")),
          "format stable")
    check("vp-detail", all(k in vp.lower() for k in
                           ("verbatim", "subreddit context", "composition", "what a fan")),
          "detail cues")
    # 16. bookmark stage: tokenize, insert w/ order+words, dedup by URL
    import sqlite3 as _sq
    import tempfile as _tf
    check("bm-words", entry_words("Selfie Heatblast and Upgrade [@mind_868].")
          == ["selfie", "heatblast", "and", "upgrade", "mind", "868"], "tokenize")
    tmp = _tf.mkdtemp()
    db = os.path.join(tmp, "t.db")
    c = _sq.connect(db)
    c.execute("CREATE TABLE bookmarks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " parent INTEGER, type INTEGER, title TEXT, url TEXT,"
              " order_index INTEGER, syncable INTEGER, locally_added INTEGER,"
              " editable INTEGER, deletable INTEGER, hidden INTEGER,"
              " hidden_ancestor_count INTEGER, num_children INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE bookmark_title_words (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " bookmark_id INTEGER, word TEXT, word_index INTEGER)")
    c.execute("INSERT INTO bookmarks (parent, type, title, url, order_index)"
              " VALUES (1, 0, 'Old', 'https://x/', 7)")
    c.commit()
    c.close()
    nid, ch = build_modified(db, "Selfie Heatblast", "https://www.reddit.com/r/x/")
    check("bm-insert", ch and nid == 2, "new row id=2")
    c = _sq.connect(db)
    check("bm-order", c.execute(
        "SELECT order_index FROM bookmarks WHERE id = 2").fetchone()[0] == 8,
        "order follows max")
    check("bm-twords", [r[0] for r in c.execute(
        "SELECT word FROM bookmark_title_words WHERE bookmark_id = 2"
        " ORDER BY word_index")] == ["selfie", "heatblast"], "title words")
    c.close()
    nid2, ch2 = build_modified(db, "Selfie Heatblast", "https://www.reddit.com/r/x/")
    check("bm-dedup", not ch2 and nid2 == 2, "same URL returns id, zero writes")
    import shutil as _sh
    _sh.rmtree(tmp, ignore_errors=True)
    # 22. wipe: leaves + their words go, folders stay
    tmp2 = _tf.mkdtemp()
    db2 = os.path.join(tmp2, "w.db")
    c = _sq.connect(db2)
    c.execute("CREATE TABLE bookmarks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " parent INTEGER, type INTEGER, title TEXT, url TEXT,"
              " order_index INTEGER, num_children INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE bookmark_title_words (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " bookmark_id INTEGER, word TEXT, word_index INTEGER)")
    c.execute("INSERT INTO bookmarks (parent, type, title, url, order_index) VALUES "
              "(0, 1, 'Root', NULL, 0), (0, 1, 'BookmarksBar', NULL, 0),"
              " (1, 0, 'Yahoo', 'https://yahoo.com/', 3),"
              " (1, 0, 'FARM X', 'https://r/x/', 8)")
    c.execute("INSERT INTO bookmark_title_words (bookmark_id, word, word_index) VALUES "
              "(3, 'yahoo', 0), (4, 'farm', 0)")
    c.commit()
    n = wipe_leaves_sql(c)
    check("wipe-count", n == 2, "two leaves deleted")
    check("wipe-left", c.execute("SELECT COUNT(*) FROM bookmarks WHERE type = 0"
                                 ).fetchone()[0] == 0, "no leaves remain")
    check("wipe-folders", c.execute("SELECT COUNT(*) FROM bookmarks WHERE type = 1"
                                    ).fetchone()[0] == 2, "folders kept")
    check("wipe-words", c.execute("SELECT COUNT(*) FROM bookmark_title_words"
                                  ).fetchone()[0] == 0, "words cascaded")
    c.close()
    _sh.rmtree(tmp2, ignore_errors=True)
    # 23. single delete: exact URL + words go, neighbor stays
    tmp3 = _tf.mkdtemp()
    db3 = os.path.join(tmp3, "d.db")
    c = _sq.connect(db3)
    c.execute("CREATE TABLE bookmarks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " parent INTEGER, type INTEGER, title TEXT, url TEXT,"
              " order_index INTEGER, num_children INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE bookmark_title_words (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " bookmark_id INTEGER, word TEXT, word_index INTEGER)")
    c.execute("INSERT INTO bookmarks (parent, type, title, url, order_index) VALUES "
              "(1, 0, 'FARM A', 'https://r/a/', 0),"
              " (1, 0, 'FARM B', 'https://r/b/', 1)")
    c.execute("INSERT INTO bookmark_title_words (bookmark_id, word, word_index) VALUES "
              "(1, 'farm', 0), (2, 'farm', 0)")
    c.commit()
    check("del-one", delete_url_sql(c, "https://r/a/") == 1, "one row deleted")
    check("del-left", [r[0] for r in c.execute("SELECT title FROM bookmarks")]
          == ["FARM B"], "neighbor kept")
    check("del-words", [r[0] for r in c.execute(
        "SELECT word FROM bookmark_title_words")] == ["farm"], "words cleaned")
    check("del-miss", delete_url_sql(c, "https://r/zzz/") == 0, "absent is zero")
    c.close()
    _sh.rmtree(tmp3, ignore_errors=True)
    # 24. recount: folder counts follow actual children (Safari renders by count)
    tmp4 = _tf.mkdtemp()
    db4 = os.path.join(tmp4, "r.db")
    c = _sq.connect(db4)
    c.execute("CREATE TABLE bookmarks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " parent INTEGER, type INTEGER, title TEXT, url TEXT,"
              " order_index INTEGER, syncable INTEGER, locally_added INTEGER,"
              " editable INTEGER, deletable INTEGER, hidden INTEGER,"
              " hidden_ancestor_count INTEGER, num_children INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE bookmark_title_words (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " bookmark_id INTEGER, word TEXT, word_index INTEGER)")
    c.execute("INSERT INTO bookmarks (id, parent, type, title, order_index, num_children)"
              " VALUES (1, 0, 1, 'Bar', 0, 0)")
    c.commit()
    nid, ch = build_modified(db4, "FARM X", "https://r/x/")
    check("rec-insert", ch and c.execute(
        "SELECT num_children FROM bookmarks WHERE id = 1").fetchone()[0] == 1,
        "parent counted on insert")
    check("rec-del", delete_url_sql(c, "https://r/x/") == 1 and c.execute(
        "SELECT num_children FROM bookmarks WHERE id = 1").fetchone()[0] == 0,
        "parent recounted on delete")
    c.close()
    _sh.rmtree(tmp4, ignore_errors=True)
    # 25. single-pick: staging prunes every other Bar leaf
    tmp5 = _tf.mkdtemp()
    db5 = os.path.join(tmp5, "s.db")
    c = _sq.connect(db5)
    c.execute("CREATE TABLE bookmarks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " parent INTEGER, type INTEGER, title TEXT, url TEXT,"
              " order_index INTEGER, syncable INTEGER, locally_added INTEGER,"
              " editable INTEGER, deletable INTEGER, hidden INTEGER,"
              " hidden_ancestor_count INTEGER, num_children INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE bookmark_title_words (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " bookmark_id INTEGER, word TEXT, word_index INTEGER)")
    c.execute("INSERT INTO bookmarks (parent, type, title, url, order_index) VALUES "
              "(1, 0, 'FARM Old One', 'https://r/old1/', 0),"
              " (1, 0, 'FARM Old Two', 'https://r/old2/', 1)")
    c.execute("INSERT INTO bookmark_title_words (bookmark_id, word, word_index) VALUES "
              "(1, 'farm', 0), (2, 'farm', 0)")
    c.commit()
    c.close()
    nid, ch = build_modified(db5, "FARM New", "https://r/new/")
    c = _sq.connect(db5)
    check("prune-one", ch and [r[0] for r in c.execute(
        "SELECT title FROM bookmarks WHERE parent = 1")] == ["FARM New"],
        "only the staged row remains")
    check("prune-words", [r[0] for r in c.execute(
        "SELECT DISTINCT word FROM bookmark_title_words")] == ["farm", "new"],
        "stale words pruned")
    c.close()
    nid2, ch2 = build_modified(db5, "FARM New", "https://r/new/")
    check("prune-idempotent", not ch2 and nid2 == nid, "re-stage writes nothing")
    # 17. analysis parse: headers split, drift blobs kept whole
    fmt = "TEXT_IN_IMAGE: none\nCONTENT: two aliens posing\nSCENE: selfie fun"
    got = parse_analysis(fmt)
    check("parse-fmt", got == {"text_in_image": "none", "content": "two aliens posing",
                               "scene": "selfie fun"}, "headers split")
    blob = "The bottom right panel shows a surprised alien face, hilarious"
    got2 = parse_analysis(blob)
    check("parse-drift", got2["content"] == blob and got2["text_in_image"] == ""
          and got2["scene"] == "", "unformatted blob kept in CONTENT")
    check("parse-empty", parse_analysis("") == {"text_in_image": "",
          "content": "", "scene": ""}, "empty stays empty")
    # 24. vision refusal: declines never count as analysis
    check("refuse-decline", vision_refused(
        "I'm unable to analyze the image directly. Describe it please"), "refusal caught")
    check("refuse-askback", vision_refused(
        "I cannot see images, provide details and I can assist"), "ask-back caught")
    check("refuse-assist", vision_refused(
        "I'm sorry, but I can't assist with that"), "cant-assist caught")
    check("refuse-policy", vision_refused(
        "Rejected per content policy guidelines"), "policy caught")
    check("refuse-clean", not vision_refused(
        "TEXT_IN_IMAGE: none\nCONTENT: two aliens posing\nSCENE: selfie fun")
        and not vision_refused(""), "real analysis + empty pass")
    # 27. scout hands vision the largest srcset source, never currentSrc-small
    check("img-fullres", "srcset" in POST_IMG_JS and "bestW" in POST_IMG_JS
          and POST_IMG_JS.index("srcset") < POST_IMG_JS.index("currentSrc"),
          "srcset-max picker before fallback (node-verified 640 over 108)")
    # 25. vision bytes: magic + dimensions, tiny/corrupt refused
    import io as _bio
    from PIL import Image as _Image
    _im = _Image.new("RGB", (400, 300), (10, 20, 30))
    _buf = _bio.BytesIO()
    _im.save(_buf, format="JPEG")
    check("vbytes-ok", vision_bytes_ok(_buf.getvalue()), "400x300 jpeg passes")
    _im2 = _Image.new("RGB", (10, 10), (10, 20, 30))
    _buf2 = _bio.BytesIO()
    _im2.save(_buf2, format="PNG")
    check("vbytes-tiny", not vision_bytes_ok(_buf2.getvalue()), "10x10 refused")
    check("vbytes-html", not vision_bytes_ok(b"<html>" + b"\x00" * 100), "html refused")
    # 26. need-vision: URL or tiered picture pick, never blind
    check("need-url", need_vision("https://i/x.jpg", ""), "url needs vision")
    check("need-tier", need_vision("", "T1") and not need_vision("", ""),
          "tiered needs it, untiered text does not")
    # 18. mimo prompt: exact context Mimo receives (logged per run)
    mp = build_mimo_prompt("Selfie Heatblast", "", "CONTENT: two aliens", "ben10")
    check("mimo-ctx", all(k in mp for k in
                           ("Selfie Heatblast", "r/ben10", "CONTENT: two aliens",
                            "Your comment:", "3 and 12 words")),
          "title+sub+analysis+rules")
    mp2 = build_mimo_prompt("T", "", "", "")
    check("mimo-empty", "Post body: (no body text)" in mp2 and "no image" in mp2,
          "missing marked")
    # 19. (retired: bookmark entry words retired with Safari)
    # 21. farm tag: deterministic first token on pushed rows
    check("farm-word", FARM_WORD == "farm" and entry_words("FARM Chet Lives!")[0]
          == "farm", "farm leads pushed titles")
    check("md5-parse", _parse_md5("95247fd6fb18b3a80db0137853bd4aa4  /var/mobile/Library/Safari/Bookmarks.db\n"
                                  "8f98d5e547ef924200c647a59f112be3  /var/mobile/Library/Safari/Bookmarks.db-wal\n",
                                  "Bookmarks.db-wal") == "8f98d5e547ef924200c647a59f112be3"
          and _parse_md5("garbage", "Bookmarks.db") == "", "md5 pick + miss")
    # 20. find another: next-best tier skipping the locked permalink
    ranked = [
        {"permalink": "/p/locked", "tier": "T1", "sub": "a"},
        {"permalink": "/p/next", "tier": "T2", "sub": "b"},
        {"permalink": "/p/skip", "tier": "", "sub": "c"},
    ]
    pick = pick_another(ranked, "/p/locked")
    check("another-skip", pick is not None and pick["permalink"] == "/p/next",
          "locked skipped, T2 picked")
    check("another-none", pick_another(
        [{"permalink": "/p/x", "tier": ""}], "/p/y") is None,
          "nothing passing returns None")
    check("another-empty", pick_another([], "/p/x") is None, "empty returns None")
    # 20. safari card: recents card matches, home icon never does
    check("scard-hit", safari_card(
        [Node(text="Safari", x=199, y=107, w=84, h=25)]) is not None, "card found")
    check("scard-home", safari_card(
        [Node(text="Safari", x=300, y=1074, w=84, h=25)]) is None, "home icon ignored")
    # 30. uiopen PRIMARY entry (26 Sep 2026 trial, routing 6/6, verification 5/5):
    # T1-T6 landing proofs pinned. Synthetic blobs carry the exact proof legs
    # seen live (sub + title words + u/ author + Join affordance). Short titles
    # and ellipsis authors verified; DEAD never quarantines.
    t1 = ("r/TheDigitalCircus u/ParsleySame6746 Since episode nine had an open ending "
          "what questions do you still have about the series Join the conversation")
    check("entry-t1", uiopen_land_proof(
        t1, "thedigitalcircus",
        "Since episode nine had an open ending, what questions do you still have about the series?",
        "ParsleySame6746")[0] == "app-post", "trial T1 proof")
    t2 = ("r/fivenightsatfreddys u/G...af_10 (Elizabeth Arc) The Finale PART 3 "
          "The Fight Isn't Over Join the conversation")
    check("entry-t2", uiopen_land_proof(
        t2, "fivenightsatfreddys",
        "(Elizabeth Arc) The Finale PART 3: The Fight Isn't Over", "Gojinaf_10")[0]
        == "app-post", "trial T2 ellipsis-author via u/ leg")
    t3 = ("r/animequestions u/Neostic_Fever Removed by moderator Discussion "
          "animequestions-ModTeam contact the mod team via modmail Join the conversation")
    check("entry-t3", uiopen_land_proof(
        t3, "animequestions",
        "Which anime character stole the show from the main character for you?", "")[0]
        == "removed-post", "trial T3 DEAD class")
    t4 = ("r/PeakyBlinders u/AzaleaMartin The saddest part of this series "
          "Join the conversation")
    check("entry-t4", uiopen_land_proof(
        t4, "peakyblinders", "The saddest part of this series...", "")[0]
        == "app-post", "trial T4 proof")
    t5 = ("r/Ben10 u/Superb_Designer_3845 Why Can't Ben Transform into a Sludge Puppy "
          "Join the conversation")
    check("entry-t5", uiopen_land_proof(
        t5, "ben10", "Why Can't Ben Transform into a Sludge Puppy?", "")[0]
        == "app-post", "trial T5 proof")
    t6 = "r/thong u/beautifulllymodel 18 NSFW need your hot love Join the conversation"
    check("entry-t6", uiopen_land_proof(
        t6, "thong", "I need your hot love", "")[0] == "app-post",
        "trial T6 short-title proof")
    check("entry-oneword", uiopen_land_proof(
        "r/thong u/x Abcdef Join", "thong", "Abcdef", "")[0] == "app-post",
        "single-word title via short-title clause")
    check("entry-notitle", uiopen_land_proof(
        "r/TheDigitalCircus u/someone Art by me Join the conversation",
        "thedigitalcircus", "Art by me", "")[0] == "app-post",
        "no long words: sub+author+bar proves it")
    check("entry-notitle-weak", uiopen_land_proof(
        "r/TheDigitalCircus u/someone Art by me", "thedigitalcircus",
        "Art by me", "")[0] != "app-post",
        "no bar affordance, no proof")
    check("entry-browser", uiopen_land_proof(
        "Search or enter website Favorites Yahoo", "thong",
        "I need your hot love", "")[0] == "browser-start-page", "start page refused")
    check("entry-unknown", uiopen_land_proof(
        "something entirely unrelated", "thong", "I need your hot love", "")[0]
        == "unknown", "unrelated refused")
    _emo = ("r/fivenightsatfreddys u/Revilgame Springtrap progress "
            "Join the conversation")
    check("entry-emoji", uiopen_land_proof(
        _emo, "fivenightsatfreddys", "Springtrap progress✏️", "Revilgame")[0]
        == "app-post", "emoji title matches alnum")
    _trunc = ("r/fivenightsatfreddys Hey Springtrap progr Join the conversation")
    check("entry-trunc", uiopen_land_proof(
        _trunc, "fivenightsatfreddys", "Springtrap progress", "")[0]
        == "app-post", "truncated 1/2 with bonus proves")
    _eline = error_line("a0015", "fivenightsatfreddys", "Springtrap progress",
                        "abort", "uiopen entry miss")
    check("error-fmt", _eline.startswith("20") and " | a0015 | r/fivenightsatfreddys | "
          "Springtrap progress | abort | uiopen entry miss" in _eline,
          "errors.log row shape")
    check("wf1-retry", App._wf1_retry_list(
        ["a", "b", "c"],
        [{"slot": "a", "verdict": "posted"}, {"slot": "b", "verdict": "skip"},
         {"slot": "c", "verdict": "abort"}]) == ["b", "c"],
          "retry covers non-posted in order")
    check("wf1-park", App._wf1_retry_list(
        ["a", "b"],
        [{"slot": "a", "verdict": "skip"}, {"slot": "a", "verdict": "skip"},
         {"slot": "b", "verdict": "skip"}]) == ["b"]
        and App._wf1_parked(
        ["a", "b"],
        [{"slot": "a", "verdict": "posted"}, {"slot": "a", "verdict": "skip"},
         {"slot": "b", "verdict": "skip"},
         {"slot": "b", "verdict": "abort"}]) == ["b"],
        "twice-failed parks until next run, posted never parks")
    check("entry-dead-noquar", DEAD_REASON not in ENTRY_ABORTS,
          "DEAD never quarantines")
    # 31. vision-fetch ladder (26 Sep 2026): routing per URL kind, corrupt
    # bytes never reach describe. Pure: no network, no phone, no API.
    thumb = ("https://preview.redd.it/abc123.jpg?width=108&crop=smart&auto=webp&s=deadbeef")
    check("lad-strip", strip_preview_params(thumb) == "https://preview.redd.it/abc123.jpg",
          "params stripped to source")
    check("lad-direct", strip_preview_params("https://i.redd.it/abc123.jpg") == "",
          "direct untouched")
    check("lad-query", strip_preview_params("https://example.com/x.jpg?w=1") == "",
          "non-preview untouched")
    check("lad-plan-full", ladder_plan(thumb, True) == [
        "direct", "preview-source", "page-candidates", "gallery-full",
        "video-poster", "authed-fetch"],
        "thumbnail+page slowest-last")
    check("lad-plan-direct", ladder_plan("https://i.redd.it/a.jpg", False) == ["direct"],
          "direct without page")
    check("lad-plan-page", ladder_plan("", True) == [
        "page-candidates", "gallery-full", "video-poster"],
        "page-only discovery")
    check("lad-plan-none", ladder_plan("", False) == [], "nothing to try")
    meta = {"m1": {"status": "valid", "s": {"u": "https://i.redd.it/small.jpg", "x": 100, "y": 100}},
            "m2": {"status": "valid", "s": {"u": "https://i.redd.it/big.jpg?a=1&amp;b=2", "x": 800, "y": 600}},
            "m3": {"status": "invalid", "s": {"u": "https://i.redd.it/huge.jpg", "x": 9999, "y": 9999}}}
    check("lad-gallery", gallery_full_url(meta) == "https://i.redd.it/big.jpg?a=1&b=2",
          "largest valid wins, amp unescaped, invalid skipped")
    check("lad-gallery-empty", gallery_full_url({}) == "" and gallery_full_url(None) == "",
          "empty stays empty")
    cands = [{"src": "https://styles.redditmedia.com/x.png", "w": 96, "h": 96},
             {"src": "https://i.redd.it/sticker.png", "w": 64, "h": 64},
             {"src": "https://i.redd.it/photo.jpeg", "w": 0, "h": 0},
             {"src": "https://i.redd.it/sticker.png", "w": 64, "h": 64}]
    check("lad-cands", rank_candidates(cands) == [
        "https://i.redd.it/sticker.png", "https://i.redd.it/photo.jpeg"],
        "chrome dropped, sticker outranks unmeasured, deduped")
    check("lad-mime", mime_of(b"\xff\xd8\xff" + b"\x00" * 64) == "image/jpeg"
          and mime_of(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64) == "image/png"
          and mime_of(b"GIF89a" + b"\x00" * 64) == "image/gif"
          and mime_of(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 64) == "image/webp",
          "content-sniffed mime")
    check("lad-riff", not is_image_bytes(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 64),
          "bare RIFF (wav) refused")
    corrupt_webp = b"RIFF" + b"\xff" * 4 + b"WEBP" + b"\xff" * 100
    check("lad-corrupt", is_image_bytes(corrupt_webp) and not vision_bytes_ok(corrupt_webp),
          "magic passes, PIL dims fail")
    check("lad-never-describe", describe_image_bytes(
        corrupt_webp, "ben10", "T", "", "k")[0] is None
        and describe_image_bytes(b"<html>403</html>" + b"\x00" * 100,
                                 "ben10", "T", "", "k")[0] is None,
        "corrupt/403 bodies refused before the model")
    # 32. post self-text: queue body wins, else the live post-page capture.
    check("body-cand", pick_post_body("queue words", "page words") == "queue words",
          "locked body first")
    check("body-page", pick_post_body("", "chat am I cooking with this idea?") ==
          "chat am I cooking with this idea?", "page capture fills the gap")
    check("body-empty", pick_post_body("", "") == "", "both empty stays empty")
    check("body-js", "text-body" in POST_BODY_JS and "shreddit-post" in POST_BODY_JS,
          "page reader targets the post text slot")
    # 33. batch mode: deterministic voice gate, random breaks, escalation.
    check("voice-pass", voice_ok("Freddy joining the literature club was not on my bingo card")[0],
          "clean engine text passes")
    check("voice-capital", voice_ok("freddy joining the club")[0] is False,
          "lowercase first fails")
    check("voice-short", voice_ok("Hi there")[0] is False, "under 3 words fails")
    check("voice-link", voice_ok("Check this out https://x.y for proof")[0] is False,
          "links fail")
    check("voice-mention", voice_ok("Hey @someone look at this thing")[0] is False,
          "mentions fail")
    check("voice-shouty", voice_ok("THIS IS ALL CAPS SHOUTING")[0] is False,
          "shouty fails")
    check("voice-stacked", voice_ok("This is amazing!!!! Really")[0] is False,
          "stacked punct fails")
    check("voice-grounded", voice_ok("Caine really going off on that thing",
                                     "YOU PARASITE Caine donut")[0],
          "evidenced names pass")
    check("voice-ungrounded", voice_ok("Jax thinking about Pomni makes me smile",
                                       "identities dresses comic")[0] is False,
          "invented names fail")
    check("never-touch", never_block("thedigitalcircus",
                                     "On the topic of identities and such (By @taxeduptrotter)"),
          "identities post blocked forever")
    check("prompt-names-free", "NAME:" not in build_mimo_prompt(
        "T", "B", "img", "s") and "ROSTER" not in build_mimo_prompt(
        "T", "B", "img", "s") and "GROUNDING" not in build_mimo_prompt(
        "T", "B", "img", "s"), "no character instructions in prompt")
    check("vision-names-free", "ROSTER" not in build_vision_prompt("s", "T", "B")
          and "NAME:" not in build_vision_prompt("s", "T", "B"),
          "no character instructions in vision prompt")
    check("break-circus", sub_break_block("a0016", "thedigitalcircus")
          and not sub_break_block("a0016", "ben10")
          and not sub_break_block("a0017", "thedigitalcircus"),
          "a0016 circus break only")
    check("nsfw-seed", nsfw_block("thedigitalcircus", "Ship post (not mine)")
          and not nsfw_block("thedigitalcircus", "Some wholesome fanart"),
          "explicit post blocked, clean passes")
    import random as _rng
    _r = _rng.Random(7)
    b1 = [draw_break_minutes(_r) for _ in range(3)]
    check("voice-break-range", all(2.0 <= b <= 3.0 for b in b1) and len(set(b1)) == 3,
          "uniform 2-3, fresh draws differ")
    check("voice-break-esc", all(5.0 <= draw_break_minutes(_rng.Random(s), True) <= 6.0
                                 for s in (1, 2, 3)), "escalated floor 5-6")
    now = 1758750000.0
    near = [{"ts": now - 3600.0, "minutes": 2.5}]
    far = [{"ts": now - 90000.0, "minutes": 2.5}]
    slow = [{"ts": now - 3600.0, "minutes": 5.5}]
    check("esc-hit", escalation_due(near, "logged-out at composer", now),
          "logout within 24h of sub-5 spacing")
    check("esc-far", not escalation_due(far, "logged-out at composer", now),
          "stale spacing no escalation")
    check("esc-slow", not escalation_due(slow, "logged-out at composer", now),
          "slow spacing no escalation")
    check("esc-reason", not escalation_due(near, "uiopen entry miss", now),
          "non-logout reason no escalation")
    # 34. rank gates: locked defaults hold, batch overrides open stock.
    import datetime as _dt
    def _raw(sub, score, age_min, title):
        ts = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(minutes=age_min)).isoformat()
        return {"sub": sub, "score": score, "created": ts, "ptype": "image",
                "title": title, "perma": "/r/%s/comments/fix9/%s/" % (sub, title[:6]),
                "comments": 3, "author": "u1"}
    _rows = [_raw("peakyblinders", 3, 46.0, "Fixture rank alpha one"),
             _raw("peakyblinders", 18, 166.0, "Fixture rank alpha two")]
    _dflt = App._rank(None, _rows)
    check("rank-default", _dflt[0].get("tier") == "" and _dflt[1].get("tier") == "T3"
          and "3up<5" in _dflt[0].get("verdict", ""),
          "defaults: floor 5 holds, 6h window opens the older post")
    _lo = App._rank(None, _rows, 3, 120)
    check("rank-floor", _lo[0].get("tier") == "T3" and not _lo[1].get("tier"),
          "floor 3 opens the young post only")
    _old = App._rank(None, _rows, 5, 180)
    check("rank-age", not _old[0].get("tier") and _old[1].get("tier") == "T3",
          "age 180 opens the older post only")
    # 36. per-container locks on a tmp store (production file untouched).
    import tempfile as _tf2
    _tmp = _tf2.mkdtemp()
    _saved_used, _saved_lock = _store.USED_PATH, _store.LOCK_PATH
    _store.USED_PATH = os.path.join(_tmp, "commented_posts.json")
    _store.LOCK_PATH = _store.USED_PATH + ".lock"
    try:
        _store.mark_commented("peakyblinders", "Fixture lock alpha post one", "a0003", "comment")
        check("lock-own", _store.already_commented(
            "peakyblinders", "Fixture lock alpha post one", "a0003"), "own slot locked")
        check("lock-other", not _store.already_commented(
            "peakyblinders", "Fixture lock alpha post one", "a0007"), "other slot free")
        check("lock-global", _store.already_commented(
            "peakyblinders", "Fixture lock alpha post one"), "global still sees it")
        check("lock-recent", _store.recently_taken(
            "peakyblinders", "Fixture lock alpha post one"), "fresh mark in window")
        check("lock-old", not _store.recently_taken(
            "peakyblinders", "Fixture lock alpha post one", 0.0), "zero window empty")
        _store.mark_commented("peakyblinders", "Fixture lock legacy post two", "", "comment")
        check("lock-legacy-own", not _store.already_commented(
            "peakyblinders", "Fixture lock legacy post two", "a0003"),
            "blank-container legacy binds nobody")
        check("lock-legacy-global", _store.already_commented(
            "peakyblinders", "Fixture lock legacy post two"), "global still sees legacy")
        def _rrow(title):
            ts = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(minutes=10)).isoformat()
            return {"sub": "peakyblinders", "score": 30, "created": ts, "ptype": "image",
                    "title": title, "perma": "/r/peakyblinders/comments/fix9/" + title[:6] + "/",
                    "comments": 3, "author": "u1"}
        _rk3 = App._rank(None, [_rrow("Fixture lock alpha post one")], 5, 120, "a0003")
        _rk7 = App._rank(None, [_rrow("Fixture lock alpha post one")], 5, 120, "a0007")
        _rkg = App._rank(None, [_rrow("Fixture lock alpha post one")], 5, 120, "")
        check("rank-used", "USED" in _rk3[0].get("verdict", ""), "own mark skips")
        check("rank-cooling", "cooling-24h" in _rk7[0].get("verdict", ""),
              "other slot cools 24h")
        check("rank-global", "USED" in _rkg[0].get("verdict", ""), "no-slot stays global")
        _rkf = App._rank(None, [_rrow("Fixture lock fresh post three")], 5, 120, "a0007")
        check("rank-cand", _rkf[0].get("tier") == "T1", "fresh post cands")
    finally:
        _store.USED_PATH, _store.LOCK_PATH = _saved_used, _saved_lock
        _sh.rmtree(_tmp, ignore_errors=True)
    # 35. autopick: most upvotes wins, any tier; ties keep newest.
    _cands = [{"permalink": "/p/new-t3", "tier": "T3", "score": 50},
              {"permalink": "/p/old-t1", "tier": "T1", "score": 12},
              {"permalink": "/p/mid-t2", "tier": "T2", "score": 30}]
    check("pick-score", App._autopick(_cands)["permalink"] == "/p/new-t3",
          "50up T3 beats smaller T1/T2")
    _tie = [{"permalink": "/p/first", "tier": "T1", "score": 20},
            {"permalink": "/p/second", "tier": "T1", "score": 20}]
    check("pick-tie", App._autopick(_tie)["permalink"] == "/p/first",
          "score tie keeps newest")
    _top = App.top_candidates(_cands + [{"permalink": "/p/skip", "tier": ""}], 2)
    check("pick-top3", [c["permalink"] for c in _top] == ["/p/new-t3", "/p/mid-t2"],
          "top-k by score, untiered excluded")
    check("pick-none", App._autopick(
        [{"permalink": "/p/x", "tier": ""}]) is None, "untiered never picks")
    _wauto = {"permalink": "/p/w", "tier": "T3", "score": 5}
    _word = [c["permalink"] for c in try_order(
        [{"permalink": "/p/a", "tier": "T1", "score": 50},
         {"permalink": "/p/w", "tier": "T3", "score": 5},
         {"permalink": "/p/b", "tier": "T2", "score": 30},
         {"permalink": "/p/x", "tier": ""}], _wauto)]
    check("tryorder-auto-first", _word == ["/p/w", "/p/a", "/p/b"],
          "rotation pick first, rest by score, untiered out")
    check("shuffle-determinism", seeded_shuffle(["a", "b", "c", "d"], 7)
          == seeded_shuffle(["a", "b", "c", "d"], 7)
          and sorted(seeded_shuffle(["a", "b", "c", "d"], 7)) == ["a", "b", "c", "d"],
          "same seed same order, all present")
    _rpool = [{"permalink": "/p/x", "tier": "T3", "score": 9, "sub": "ben10"},
              {"permalink": "/p/y", "tier": "T2", "score": 40, "sub": "fivenightsatfreddys"}]
    _rpick, _rnxt = rotation_pick_in_order(_rpool, ["ben10", "fivenightsatfreddys"], 0)
    check("runorder-pick", (_rpick or {}).get("permalink") == "/p/x" and _rnxt == 1,
          "run order honored, pointer advances")
    _rpick2, _rnxt2 = rotation_pick_in_order(_rpool, ["ben10", "fivenightsatfreddys"], 1)
    check("runorder-next", (_rpick2 or {}).get("permalink") == "/p/y" and _rnxt2 == 0,
          "pointer wraps around")
    # 38. batch hunts until N posted: bounded attempts, never infinite.
    check("batch-cap", batch_attempt_cap(3) == 7 and batch_attempt_cap(1) == 5
          and batch_attempt_cap(0) == 5, "n + 4 hunts max")
    # 40. once-per-container login: switch stamps, sends match, wall flagged.
    check("switchslot-first", same_container("", "a0007"), "empty allows first open")
    check("switchslot-same", same_container("a0007", "a0007")
          and same_container("A0007", "a0007"), "same slot (case-insensitive)")
    check("switchslot-diff", not same_container("a0003", "a0007"), "other slot refused")
    check("switchslot-flag", "logged-out at entry" in LOGOUT_FLAGS,
          "entry-wall logout feeds escalation")
    # 41. drawer ground truth: marker means logged out even on a live feed.
    check("drawer-dead", drawer_logged_out("Log in to add your communities"),
          "marker hits")
    check("drawer-dead-case", drawer_logged_out("LOG IN TO ADD YOUR COMMUNITIES"),
          "case-insensitive")
    check("drawer-healthy", not drawer_logged_out("Home Popular Latest r/cats"),
          "clean drawer passes")
    check("drawer-wrap", drawer_logged_out("Log in to add\nyour communities"),
          "OCR line-wrap still hits")
    from friend_poster.apps.commenter import drawer_text as _dtext, drawer_verdict as _dverdict
    check("drawer-alive-headers",
          _dverdict("Your Communities Create Community") == "in"
          and _dverdict("Recently Visited r/a r/b r/c") == "in",
          "headers rule")
    check("drawer-alive-subs",
          _dverdict("r/one r/two r/three Home") == "in"
          and _dverdict("r/one r/two Home") == "",
          "3+ community rows rule, 2 is not enough")
    check("drawer-marker-wins",
          _dverdict("Log in to add your communities r/a r/b r/c") == "out",
          "marker beats structure")
    check("drawer-text-join",
          _dtext("Log in to add\nyour communities", []) ==
          "log in to add your communities",
          "raw blob wraps collapse")
    try:
        from friend_poster.apps.poster import login_probe as _pp_probe
        import inspect as _insp2
        _pp_src = _insp2.getsource(_pp_probe)
    except Exception:
        _pp_src = ""
    check("poster-same-probe", "drawer_login_probe" in _pp_src,
          "poster delegates to the shared drawer probe")
    from friend_poster.apps.poster import load_sub_map as _poster_map, sub_holder as _holder
    check("submap-shape", isinstance(_poster_map(), dict),
          "allow-list loads as dict")
    check("subholder-free",
          _holder("", "a0003") == ""
          and _holder("r/__nonexistent_xyz__", "") == "",
          "empty/unknown subs never held")
    from friend_poster.apps.poster import load_community_names as _comm_names
    _cn, _cs = _comm_names()
    check("comm-list",
          isinstance(_cn, list) and isinstance(_cs, str)
          and "couplesporn" in _cn and "thong" in _cn,
          "dropdown merges settings + sub dirs")
    from friend_poster.apps.poster import available_pics as _avail
    from friend_poster.apps.poster import flair_for as _flair
    from friend_poster.reddit.flair_pick import find_sheet_x as _sheetx
    from friend_poster.reddit.flair_pick import sheet_open as _sheetopen
    _shx = Node()
    _shx.text = "X"
    _shx.x, _shx.y, _shx.w, _shx.h = 668, 129, 35, 34
    _shtop = Node()
    _shtop.text = "Post"
    _shtop.x, _shtop.y, _shtop.w, _shtop.h = 630, 60, 90, 40
    _shbot = Node()
    _shbot.text = "X"
    _shbot.x, _shbot.y, _shbot.w, _shbot.h = 650, 1200, 35, 34
    check("sheet-x",
          _sheetx([_shtop, _shx, _shbot]) is _shx
          and _sheetx([_shtop]) is None
          and _sheetx([]) is None,
          "sheet X band only, never Post pill or bottom junk")
    _son = Node()
    _son.text = "Search flair"
    _son.x, _son.y, _son.w, _son.h = 92, 370, 159, 30
    _soff = Node()
    _soff.text = "My Belly Button"
    _soff.x, _soff.y, _soff.w, _soff.h = 120, 574, 195, 25
    check("sheet-open",
          _sheetopen("search flair", [_soff])
          and _sheetopen("", [_son])
          and not _sheetopen("plain composer", [_soff]),
          "sheet markers decide, applied chip does not")
    from friend_poster.apps.poster import flair_demand as _fdemand
    check("flair-demand",
          _fdemand("Add Flair (REQUIRED)") == "required"
          and _fdemand("add flair") == "optional"
          and _fdemand("plain composer") == "",
          "required vs optional vs none")
    from friend_poster.reddit.flair_pick import list_flair_options as _fopts
    from friend_poster.reddit.flair_pick import match_option as _fmatch
    _fnodes = []
    for _t, _y in (("r/couplesporn flair", 200), ("Goth", 500),
                   ("Amateur", 600), ("Search flair", 100)):
        _nd = Node()
        _nd.text = _t
        _nd.x, _nd.y, _nd.w, _nd.h = 100, _y, 200, 30
        _fnodes.append(_nd)
    check("flair-opts", _fopts(_fnodes) == ["Goth", "Amateur"],
          "headers skipped, options listed")
    check("flair-match",
          _fmatch('  "GOTH" ', ["Goth", "Amateur"]) == "Goth"
          and _fmatch("Nobody", ["Goth", "Amateur"]) is None
          and _fmatch("", ["Goth"]) is None,
          "exact match only, never fuzzy")
    check("pool-avail",
          _avail(["a.JPG", "b.png"], {"a.jpg"}, {"b.PNG"}) == []
          and _avail(["a.JPG"], set(), set()) == ["a.JPG"],
          "used + picked excluded, case-insensitive")
    check("flair-map", _flair("bellybuttonworld") == "My belly button"
          and _flair("couplesporn") == "",
          "flair from settings rows")
    from friend_poster.apps.poster import community_landed as _landed
    _lnode = Node()
    _lnode.label = "Create"
    _lnode.x, _lnode.y, _lnode.w, _lnode.h = 343, 1310, 72, 17
    check("land-proof",
          _landed("r/CouplesPorn members Create", [_lnode], "couplesporn")
          and not _landed("home feed Create", [_lnode], "couplesporn")
          and not _landed("r/CouplesPorn members", [], "couplesporn"),
          "header + Create or nothing")
    _glab = Node()
    _glab.label = "Continue"
    _glab.x, _glab.y, _glab.w, _glab.h = 500, 900, 200, 60
    _gocr = Node()
    _gocr.ntype = "ocr"
    _gocr.text = "Continue Continue"
    _gocr.x, _gocr.y, _gocr.w, _gocr.h = 500, 900, 120, 30
    _gback = Node()
    _gback.ntype = "ocr"
    _gback.text = "Go Back"
    _gback.x, _gback.y, _gback.w, _gback.h = 60, 900, 120, 30
    check("gate-labeled", _gate([_glab, _gocr]) is _glab,
          "labeled node wins")
    check("gate-ocr-word", _gate([_gback, _gocr]) is _gocr,
          "word-level OCR fallback")
    check("gate-refuse", _gate([_gback]) is None
          and _gate([]) is None,
          "no continue, no tap")
    _ulab = Node()
    _ulab.label = "Update Settings"
    _ulab.x, _ulab.y, _ulab.w, _ulab.h = 400, 900, 290, 60
    _uw1 = Node()
    _uw1.ntype = "ocr"
    _uw1.text = "Update"
    _uw1.x, _uw1.y, _uw1.w, _uw1.h = 400, 900, 110, 30
    _uw2 = Node()
    _uw2.ntype = "ocr"
    _uw2.text = "Settings"
    _uw2.x, _uw2.y, _uw2.w, _uw2.h = 520, 900, 120, 30
    check("upd-labeled", _updg([_ulab, _uw1]) is _ulab,
          "labeled update settings wins")
    check("upd-ocr", _updg([_uw1, _uw2]) is _uw2,
          "rightmost settings word tapped")
    check("upd-refuse", _updg([_gback]) is None and _updg([]) is None,
          "no update control, no tap")
    # 42. auto-restore registry: frozen mapping only, never derived live.
    _reg = {"a0007": {"verified": ["Reddit_r7_09.15.2026_05-57-50.cranect"]},
            "a0026": {"verified": []}}
    check("restore-map", resolve_backup_file("a0007", _reg) ==
          "Reddit_r7_09.15.2026_05-57-50.cranect", "verified file resolves")
    check("restore-gap", resolve_backup_file("a0026", _reg) == ""
          and resolve_backup_file("zzz", _reg) == ""
          and resolve_backup_file("a0007", {}) == "", "gaps refuse")
    _srow = [Node(text="Q Search", x=44, y=148, w=158, h=35)]
    check("restore-search", App._search_field(_srow) is not None
          and App._search_field([Node(text="Foo", x=10, y=500, w=50, h=20)]) is None,
          "field found or refused, never guessed")
    # 43. list rows only: Active-header value never tappable, no header needed.
    _nodes = [Node(text="a0009", x=546, y=233, w=104, h=28),
              Node(text="CONTAINERS", x=63, y=465, w=168, h=21),
              Node(text="a0009", x=113, y=932, w=102, h=28)]
    _hit = list_row(_nodes, "a0009")
    check("rows-floor", _hit is not None and _hit.y == 932,
          "header value excluded, list row kept")
    check("rows-miss", list_row(_nodes, "zzz") is None
          and list_row([Node(text="a0009", x=546, y=233, w=104, h=28)],
                       "a0009") is None, "absent + header-only refuse")
    # 44. result band: search echo + keyboard never count as files.
    _frows = [Node(text="Q r9", x=42, y=131, w=90, h=44),
              Node(text='Contains "r9"', x=243, y=338, w=200, h=30),
              Node(text="Reddit_r9_09.", x=53, y=623, w=193, h=30),
              Node(text="q", x=100, y=962, w=20, h=20)]
    _fm = result_rows(_frows, "r9")
    check("result-band", len(_fm) == 1 and _fm[0].y == 623,
          "echo + header + keyboard excluded, file kept")
    check("canonical-one", canonical_pick(["Reddit_r7_09.x"]) == 0,
          "single canonical resolves")
    check("canonical-copy", canonical_pick(["a0014_Reddit_a0014_09", "Reddit_a0014_09"]) == 1,
          "renamed copy loses to canonical")
    check("canonical-none", canonical_pick(["a0014_Reddit_a0014_09"]) == -1
          and canonical_pick([]) == -1, "no canonical refuses")
    # 45. icon measurement: dense white blob wins, text-thin bands refuse.
    import tempfile as _tf3
    from PIL import Image as _Image3
    _im3 = _Image3.new("RGB", (750, 1334), (10, 10, 10))
    _px3 = _im3.load()
    for _yy in range(380, 545):
        for _xx in range(115, 245):
            _px3[_xx, _yy] = (240, 240, 240)
    for _xx in range(40, 260, 3):
        _px3[_xx, 620] = (240, 240, 240)
    _ip3 = os.path.join(_tf3.mkdtemp(), "icon.png")
    _im3.save(_ip3)
    _pt3 = App._icon_middle(_ip3, 53, 640, 217)
    check("icon-found", _pt3 is not None and 115 <= _pt3[0] <= 245
          and 380 <= _pt3[1] <= 545, "dense blob centered")
    _im4 = _Image3.new("RGB", (750, 1334), (10, 10, 10))
    _ip4 = os.path.join(_tf3.mkdtemp(), "blank.png")
    _im4.save(_ip4)
    check("icon-blank", App._icon_middle(_ip4, 53, 640, 217) is None
          and App._icon_middle("/nope/missing.png", 53, 640, 217) is None,
          "no blob, no tap")
    check("profile-url", profile_url("Alive_Fly7585") ==
          "https://www.reddit.com/user/Alive_Fly7585/"
          and profile_url("u/Someone") == "https://www.reddit.com/user/Someone/"
          and profile_url("") == "", "permalink builder")
    # 40. no-login detector: dead/live/wrong-user/unknown dumps pinned.
    _dead = "Log in to Reddit Continue with Google login Email or username"
    check("probe-dead", login_verdict(_dead, [], "Alive_Fly7585")[0] == "NO_LOGIN",
          "login markers read dead")
    _live = "u/Alive_Fly7585 1,234 karma Cake day Joined"
    check("probe-live", login_verdict(_live, [], "Alive_Fly7585")[0] == "HEALTHY",
          "expected user reads healthy")
    _wrong = "u/SomeoneElse 99 karma overview posts comments"
    check("probe-wrong", login_verdict(_wrong, [], "Alive_Fly7585")[0] == "WRONG_USER",
          "different user flagged")
    check("probe-unknown", login_verdict(
        "loading spinner retry", [], "Alive_Fly7585")[0] == "UNKNOWN",
        "ambiguous never reads healthy")
    _av = [Node(text="other", x=100, y=100, w=80, h=20),
           Node(ntype="ocr-line", text="Alive_Fly7585", x=600, y=100, w=100, h=30),
           Node(text="Alive_Fly7585", x=600, y=100, w=100, h=30)]
    check("probe-avatar", find_avatar(_av, "Alive_Fly7585") == (650, 115, "alive_fly7585"),
          "username node top-right, ocr-line skipped")
    check("probe-avatar-miss", find_avatar(
        [Node(text="other", x=100, y=100, w=80, h=20)], "Alive_Fly7585") is None,
        "nothing top-right")
    _bar = [Node(text="Q", x=560, y=70, w=40, h=36),
            Node(text="o", x=680, y=70, w=40, h=44),
            Node(text="72%", x=620, y=5, w=60, h=20)]
    check("probe-avatar-geometry", find_avatar(_bar, "Alive_Fly7585") is None,
          "demoted: no username node, never geometry-taps")
    _rec = verdict_record("a0007", "NO_LOGIN", "PleasantCourse3429", "log in",
                          ["login-markers:log in"], "95_probe_profile", "drawer")
    check("probe-schema", set(_rec) == {"tool", "slot", "status", "expected", "seen",
                                        "evidence", "dump", "path", "ts", "build"}
          and _rec["tool"] == "login_probe", "phase-2 schema exact")
    # 41. hamburger drawer path: locate, markers, hygiene gate.
    _ham = [Node(text="status", x=10, y=10, w=100, h=20),
            Node(text="=", x=60, y=90, w=30, h=30),
            Node(text="feed stuff", x=400, y=500, w=200, h=30)]
    check("probe-ham-glyph", find_hamburger(_ham) == (75, 105, "="),
          "menu glyph top-left")
    _geo = [Node(text="something", x=200, y=150, w=100, h=25),
            Node(text="other", x=100, y=60, w=100, h=25)]
    check("probe-ham-geo", find_hamburger(_geo) == (150, 72, "other"),
          "topmost-leftmost control")
    check("probe-ham-miss", find_hamburger(
        [Node(text="far", x=700, y=900, w=50, h=20)]) is None,
        "nothing in band")
    _dd = "u/Alive_Fly7585 My Profile Settings Log out"
    check("probe-drawer-user", drawer_markers(_dd, "Alive_Fly7585")
          == ["user:alive_fly7585", "menu:my-profile"], "user rows + menu")
    check("probe-drawer-login", drawer_markers(
        "Log in to continue Sign up", "Alive_Fly7585") == ["login:log in"],
        "login entry")
    check("probe-drawer-feedmenu", "feedmenu" in drawer_markers(
        "Home Popular Watch News Latest", "Alive_Fly7585"),
        "feed switcher is not a drawer")
    _pre = "r/sub best comments join the conversation"
    check("probe-hygiene-clean", drawer_closed_ok(_pre, _pre, "Alive_Fly7585")
          == (True, "clean-return"), "returned screen passes")
    check("probe-hygiene-dirty", drawer_closed_ok(
        _pre, "u/Alive_Fly7585 My Profile", "Alive_Fly7585")[0] is False,
        "lingering drawer aborts")
    check("probe-hygiene-lost", drawer_closed_ok(
        _pre, "blank mystery screen", "Alive_Fly7585") == (False, "feed-context-missing"),
        "lost context aborts")
    # 43. single-probe lock: resume never re-probes/restores (blind row tap).
    import inspect as _inspect
    try:
        _rsrc = _inspect.getsource(App._batch_resume)
    except (OSError, TypeError):
        _rsrc = ""
    check("resume-no-drawer", "_drawer_login_probe" not in _rsrc, "no drawer in resume")
    check("resume-no-restore", "_restore_until_alive" not in _rsrc
          and "RESTORE trigger" not in _rsrc, "no restore in resume")
    check("resume-no-allow", "allow_restore" not in _rsrc, "no restore flag in resume")
    check("resume-blind", "blind row tap" not in _rsrc and "find_picker_row" not in _rsrc
          and "uiopen entry" in _rsrc, "no-tap resume pinned")
    try:
        _osrc = _inspect.getsource(App._open_container)
    except (OSError, TypeError):
        _osrc = ""
    check("open-keeps-probe", "_drawer_login_probe" in _osrc, "first-open probe kept")
    try:
        from friend_poster.reddit import reddit_restore as _rr
        _restore_src = _inspect.getsource(_rr.auto_restore)
    except (OSError, TypeError, ImportError):
        _restore_src = ""
    check("reopen-switcher", "resume_reddit_card" in _restore_src,
          "post-restore reopen via switcher card")
    try:
        _app_restore_src = _inspect.getsource(App._auto_restore)
    except (OSError, TypeError):
        _app_restore_src = ""
    check("reopen-delegate", "auto_restore" in _app_restore_src,
          "commenter restores via the shared flow")
    try:
        _resume_src = _inspect.getsource(_rr.resume_reddit_card)
    except (OSError, TypeError, NameError):
        _resume_src = ""
    check("reopen-solo", "interp_flick" in _resume_src
          and "_switcher_solo" in _resume_src,
          "Crane flicked away before Reddit tap")
    check("reopen-no-icon", "tap_home_icon" not in _restore_src
          and "_find_reddit_icon" not in _restore_src
          and "Select Container" not in _restore_src,
          "no icon relaunch on post-restore path")
    check("dedupe", dedupe_raws(
        [{"perma": "/p/a"}, {"perma": "/p/a"}, {"perma": "/p/b"}, {}, {"perma": ""}]
    ) == [{"perma": "/p/a"}, {"perma": "/p/b"}], "repeat permalinks dropped")
    check("cdp-endpoint", cdp_endpoint() == "http://127.0.0.1:9223",
          "kept-window address pinned")
    check("lock-err", is_profile_lock_error(
        "BrowserType.launch_persistent_context: Opening in existing browser "
        "session. This usually means that the profile is already in use"),
        "profile contention detected")
    check("lock-clean", not is_profile_lock_error("bot Chrome reused (kept window)")
          and not is_profile_lock_error(""), "normal lines pass")
    _prof = os.path.join("subscraper", "chrome_bot_profile")
    check("lock-mine", profile_in_cmdline(
        'chrome.exe --user-data-dir=C:\\x\\subscraper\\chrome_bot_profile', _prof),
        "own profile matched")
    check("lock-browse", not profile_in_cmdline(
        'chrome.exe --user-data-dir=C:\\Users\\K\\AppData\\Local\\Google\\Chrome\\User Data',
        _prof), "browsing Chrome never matched")
    # 39. UI log discipline: verdicts + engine shown, scout/entry/fire
    # internals filed. Chilled tab, full detail stays in debug log.
    for _keep in ("=== BATCH b1 a0003 x3 ===",
                  "VERIFY_OK u+x", "MARKED title",
                  "RUN posted url", "ABORT PhoneError: x",
                  "VOICE skip (x)",
                  "WF1 a0021: login failed, skipping slot",
                  "WF1 retry pass 2: a0021,a0022",
                  "TEXT model: x (1.0s): Hi",
                  "OPENAI vision: gpt-4o-mini (1.2s)",
                  "Next: a0021 in 37:12", "Scheduled 20 slot(s), spread 20-60min gaps.",
                  "WF1 order: a0003,a0008 (1 each, Phase2)",
                  "FIRE a0021: skip, logged-out at feed",
                  "PREP a0021: login failed, skipping slot",
                  "SESSION-LOG /x/y.log", "LOGIN probe: container open",
                  "DRAWER a0031 marker hit 'x'",
                  "RESTORE dialog cleared, reopening container",
                  "ENGINE post title: T", "ENGINE body: b",
                  "ENGINE top comment: u/x (5 upvotes): hi",
                  "ENGINE footer: f", "ENGINE TEXT_IN_IMAGE: t",
                  "ENGINE CONTENT: xyz", "ENGINE text-prompt: xyz",
                  "ENGINE SCENE: s",
                  "ACCOUNTS trying in-app switch for a0021",
                  "ACCOUNTS no username row, normal restore",
                  "VISION bytes 123B via direct",
                  "ladder direct rejected (5B)"):
        check("ui-keep", ui_important(_keep), "shown: %s" % _keep[:24])
    for _drop in ('XXTouch OpenAPI select={"code":0}', "Wrote dumps/x.json (1 chars)",                  "  - ocr 'foo' [1,2 3x4]", "SCOUT skip r/a SKIP 1up<5",
                  "SCOUT 53 raws: {'T1': 1}", "ENTRY uiopen landed",
                  "FIRE_T+26.3s container open", "STAGED a0007 feed alive",
                  "BATCH break 2.5 min", "BATCH item 3: no-login PREP",
                  "QUARANTINE /p/1 K=3", "FAV word=test",
                  "PREP login container open", "NOLOGIN bypass a0021: no session",
                  "ROTATION r/x [T1]",
                  "USB iPhone: yes", "Forward ready: 127.0.0.1:2222",
                  "BACKEND xxtouch", "TAP Post (composer)",
                  "XXTouch screenshot 171949 bytes", "type_human 45 chars",
                  "ICON-TAP (189,540) hardcode", "bot Chrome launch (once",
                  "BODY post-page capture 124 chars",
                  "SSH up as mobile via device port 22",
                  "tap (114,1026) via xxtouch",
                  "800_fire_home: 46 nodes, 46 labeled"):
        check("ui-drop", not ui_important(_drop), "filed: %s" % _drop[:24])
    _mp = build_mimo_prompt("T", "B", "img", "s",
                            top_comment="u/x (5 upvotes): hi there")
    check("prompt-top", "Top comment" in _mp and "u/x (5 upvotes)" in _mp
          and "do NOT reply" in _mp,
          "top comment rides along, top-level enforced")
    _mp2 = build_mimo_prompt("T", "B" * 2500, "", "s")
    check("prompt-fullbody", ("B" * 2000) in _mp2 and "..." in _mp2,
          "post body context raised to 2000")
    # 37. stats log: per-container totals + UTC per-day from marks and runs.
    _marks = {"k1": {"container": "a0003", "sub": "ben10", "title": "T one",
                     "ts": 1790251200.0, "when": "2026-09-24T12:00:00Z"},
              "k2": {"container": "a0003", "sub": "ben10", "title": "T two",
                     "ts": 1790254800.0, "when": "2026-09-24T13:00:00Z"},
              "k3": {"container": "a0007", "sub": "ben10", "title": "T three",
                     "ts": 1790337600.0, "when": "2026-09-25T12:00:00Z"},
              "k4": "junk-row",
              "k5": {"container": "a0007", "sub": "x", "title": "T",
                     "ts": "bad-ts", "when": "?"},
              }
    _st = stats_from_marks(_marks)
    check("stats-total", _st["a0003"]["total"] == 2 and _st["a0007"]["total"] == 2,
          "totals per container")
    check("stats-day", _st["a0003"]["by_day"] == {"2026-09-24": 2}
          and _st["a0007"]["by_day"] == {"2026-09-25": 1},
          "UTC day buckets, bad ts untracked")
    check("stats-last", _st["a0003"]["last"]["title"] == "T two",
          "newest row wins last")
    check("stats-empty", stats_from_marks({}) == {} and mark_day("xx") == "",
          "empty + bad ts safe")
    _runs = [{"slot": "a0003", "verdict": "posted"}, {"slot": "a0003", "verdict": "abort"},
             {"slot": "a0003", "verdict": "posted"}, {"batch": "b1"},
             {"slot": "a0007", "verdict": "posted"}, "junk"]
    _rs = stats_from_runs(_runs)
    check("stats-runs", _rs == {"a0003": {"posted": 2, "abort": 1},
                                "a0007": {"posted": 1}},
          "runs split by verdict, batch rows skipped")
    # 40. spread gaps: uniform 20-60 fresh draws, clamped to the 11pm fit.
    import random as _random
    import time as _time
    _rng = _random.Random(7)
    _draws = [draw_spread_minutes(_rng, 20,
                                  now_ts=spread_day_end_ts() - 14 * 3600.0)[0]
              for _ in range(30)]
    check("spread-bounds", all(20.0 <= g <= 60.0 for g in _draws),
          "wide window draws stay 20-60")
    check("spread-vary", len(set(_draws)) > 5, "fresh draw every gap")
    _late = spread_day_end_ts() - 20 * 60.0
    _g, _clamped = draw_spread_minutes(_rng, 3, now_ts=_late)
    check("spread-clamp", _clamped and _g <= 60.0,
          "tight window clamps to fit")
    _edge = spread_day_end_ts() - 10 * 60.0
    _g2, _clamped2 = draw_spread_minutes(_rng, 3, now_ts=_edge)
    check("spread-overrun", _clamped2 and _g2 == 5.0,
          "no room left floors at 5.0")
    check("spread-gap-posted",
          _last_posted([{"verdict": "posted"}])
          and not _last_posted([{"verdict": "skip"}])
          and not _last_posted([]),
          "gaps only after posts, errors move on")
    _today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _frows = [
        {"slot": "a0003", "verdict": "abort", "reason": "entry miss",
         "ts": _today_iso},
        {"slot": "a0003", "verdict": "skip",
         "reason": "login failed, skipping slot (x)", "ts": _today_iso},
        {"slot": "a0008", "verdict": "skip",
         "reason": "no postable candidate", "ts": _today_iso},
        {"slot": "a0009", "verdict": "skip", "reason": "logged-out at feed",
         "ts": _today_iso},
        {"slot": "a0010", "verdict": "posted", "ts": _today_iso},
        {"slot": "a0014", "verdict": "abort", "reason": "x",
         "ts": "2020-01-01T00:00:00Z"},
        {"batch": "b1"},
    ]
    _ft = failed_today(_frows)
    check("failed-today",
          sorted(_ft) == ["a0003"]
          and len(_ft["a0003"]) == 2
          and "a0008" not in _ft and "a0009" not in _ft,
          "errors marked, benign skips + old rows excluded")
    _fp = failed_poster_today([
        {"container": "a0003", "status": "failed", "note": "entry miss",
         "failed_ts": _today_iso},
        {"container": "a0008", "status": "failed", "note": "x"},
        {"container": "a0009", "status": "posted", "posted_ts": _today_iso},
    ])
    check("failed-poster",
          sorted(_fp) == ["a0003"],
          "poster failures by failed_ts, unstamped excluded")
    check("accounts-row",
          accounts_row_username("u/Name_123 u/Name_123") == "Name_123"
          and accounts_row_username("Add account") == ""
          and accounts_row_username("Sign up / Log in") == "",
          "only u/ rows match, never Add account")
    print("REGRESS %d pass %d fail" % (len(PASS), len(FAIL)), flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
