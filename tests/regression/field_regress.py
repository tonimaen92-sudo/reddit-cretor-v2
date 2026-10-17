# UTF-8
"""Regression for the field-verification standing law. Offline: fixtures only,
no phone, no API. Every case must pass before shipping.
Run: python misc/field_regress.py
"""

from __future__ import annotations

import os
import sys
from friend_poster.device.ssh_device import PhoneError  # noqa: E402
from friend_poster.device.ui_tree import Node  # noqa: E402
from friend_poster.core.fieldvalue import (  # noqa: E402
    anchor_has_text, echo_present, field_anchor, keyboard_present, norm_url,
    pick_wda_value, require_open, verdict_empty, verdict_exact_text,
    verdict_exact_url,
)

PASS = []
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" :: " + detail if detail else ""), flush=True)


def kb_nodes(n: int = 12):
    keys = "qwertyuiopas"
    return [Node(ntype="ocr", text=keys[i % len(keys)], x=60 + 50 * (i % 6),
                 y=1000 + 60 * (i // 6), w=30, h=30) for i in range(n)]


def main() -> int:
    # 1. scrolled-field: full value wins, partial OCR is irrelevant (never a verdict)
    value = ("https://www.reddit.com/r/TheDigitalCircus/comments/1wp8fz7/"
             "playing_with_dolls_by_dawnopaldraws/")
    want = "https://www.reddit.com/r/TheDigitalCircus/comments/1wp8fz7/playing_with_dolls_by_dawnopaldraws"
    ocr_partial = "ents/1wp8fz7/playing_with_dolls"
    ok, _ = verdict_exact_url(value, want)
    check("scrolled-value", ok, "full value matches despite clipped OCR")
    check("scrolled-ocr-irrelevant", "comments/1wp8fz7" not in ocr_partial,
          "OCR lacks the id yet the verdict still passes on value")
    # 2. empty-field: provable empty vs stale/blocked copy
    ok, _ = verdict_empty("", b"old-clipboard-bytes")
    check("empty-clean", ok, "empty value passes against foreign clipboard")
    ok, reason = verdict_empty("https://x", b"old-clipboard-bytes")
    check("empty-dirty", not ok and "not empty" in reason, "non-empty value fails")
    ok, reason = verdict_empty("old-clipboard-bytes", b"old-clipboard-bytes")
    check("empty-stale", not ok and "blocked" in reason,
          "read == pre-copy clipboard names the blocked copy")
    # 3. focus-lost: keyboard presence heuristic on fixtures
    check("focus-present", keyboard_present(kb_nodes(12)), "12 key rows = focused")
    check("focus-absent", not keyboard_present(kb_nodes(3)), "3 key rows = lost")
    plain = [Node(ntype="ocr", text="r/Ben10", x=100, y=150, w=200, h=27)]
    check("focus-no-keys", not keyboard_present(plain), "no key rows = lost")
    # 4. close-out: target must test open before the tap
    try:
        require_open([], lambda n: False, "tab switcher")
        check("closeout-closed", False, "did not refuse")
    except PhoneError as e:
        check("closeout-closed", "tab switcher" in str(e), "refusal names the target")
    try:
        require_open([1], lambda n: True, "tab switcher")
        check("closeout-open", True, "open target passes")
    except PhoneError:
        check("closeout-open", False, "refused an open target")
    # 5. url normalization
    check("norm-url", norm_url("HTTPS://www.Reddit.com/r/TDC/") == "www.reddit.com/r/tdc",
          "scheme/host case + trailing slash")
    check("norm-id", norm_url("https://www.reddit.com/r/x/comments/1Ab2C/") ==
          "www.reddit.com/r/x/comments/1ab2c", "casefold keeps id comparable")
    # 6. literal text exact
    ok, _ = verdict_exact_text("Hello", "Hello")
    check("text-exact", ok, "identical passes")
    ok, reason = verdict_exact_text("Hello", "hello")
    check("text-case", not ok and "want 'hello' got 'Hello'" in reason,
          "case differs, both strings attached")
    # 7. anchor geometry: editable field only. Top-strip full URL (y<300)
    # wins; the iOS 15 editor ALSO renders the field window above the
    # keyboard (y750-950, w>=200 — keys stay under 100px) showing an
    # arbitrary scrolled substring, so text markers are never required and
    # placeholder pill text never anchors. Suggestion rows (narrower, or
    # outside the band) NEVER anchor. Clipboard stays the sole content
    # authority.
    band = Node(ntype="ocr", text="https://www.reddit.com/r/ben10/", x=51, y=833, w=498, h=41)
    sugg = Node(ntype="ocr", text="reddit.com", x=180, y=250, w=132, h=22)
    find = Node(ntype="ocr", text="Find \"https://x\"", x=63, y=245, w=597, h=42)
    top = Node(ntype="ocr", text="https://a.b/r/ben10/", x=114, y=110, w=351, h=25)
    got = field_anchor([top, band, sugg, find])
    check("anchor-top", got is top, "top full URL wins")
    got = field_anchor([band])
    check("anchor-band", got is band, "field-window band anchors (long-press target)")
    tail = Node(ntype="ocr", text="r_on_the_bottomsee_descriptio", x=49, y=834, w=498, h=38)
    got = field_anchor([tail])
    check("anchor-band-tail", got is tail and anchor_has_text(got),
          "marker-free slug tail anchors and counts as text")
    check("anchor-top-wins", field_anchor([top, tail]) is top, "top full URL still wins over band")
    check("echo-band-tail", echo_present([tail]), "band tail proves keystrokes (presence only)")
    pill = Node(ntype="ocr", text="Search or enter website name", x=285, y=853, w=462, h=39)
    check("pillband-noanchor", field_anchor([pill]) is None, "placeholder pill never anchors")
    check("pillband-noecho", not echo_present([pill]), "placeholder pill is not an echo")
    narrow = Node(ntype="ocr", text="Some long suggestion text here", x=100, y=800, w=150, h=30)
    check("narrow-noanchor", field_anchor([narrow]) is None, "narrow band text never anchors")
    got = field_anchor([sugg, find])
    check("anchor-nosugg", got is None, "domain suggestion + Find row never anchor")
    ph = Node(ntype="ocr", text="Search", x=317, y=69, w=114, h=29)
    got = field_anchor([ph])
    check("anchor-placeholder", got is ph and not anchor_has_text(got),
          "Search placeholder anchors an empty field")
    check("anchor-hasttext", anchor_has_text(top) and not anchor_has_text(ph),
          "URL-ish = content, placeholder = empty")
    check("echo-present", echo_present([band]) and echo_present([top]),
          "echo anywhere proves keystrokes landed")
    check("echo-absent", not echo_present([ph]), "placeholder is not an echo")
    # 8. WDA value-attr reader on fixture XML
    xml = ('<XCUIElementTypeApplication><XCUIElementTypeTextField value="r/ben10" '
           'hasKeyboardFocus="true" x="1" y="2" width="3" height="4"/>'
           '<XCUIElementTypeTextField value="stale" x="1" y="2" width="3" height="4"/>'
           '</XCUIElementTypeApplication>')
    check("wda-focused", pick_wda_value(xml) == "r/ben10", "focused value wins")
    xml0 = '<XCUIElementTypeApplication><XCUIElementTypeButton/></XCUIElementTypeApplication>'
    check("wda-novalue", pick_wda_value(xml0) == "", "no input = empty")
    print("REGRESS %d pass %d fail" % (len(PASS), len(FAIL)), flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
