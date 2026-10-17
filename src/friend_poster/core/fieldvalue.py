# UTF-8
"""Field verification standing law: value reads only, never OCR content.

Stack reality (locked, probed live 24 Sep): the live path is XXTouch OCR
(screen.ocr_text) — no accessibility tree, no value attribute. WDA XML
carries value attrs but WDA is not installed (AGENTS.md: XXTouch Elite is
the tap stack). Cmd-key combos (LEFTCOMMAND+A/C/V) are DEAD on this build:
proven by MARKV123 paste-no-show + sentinel type-ok-but-copy-stale. So the
value authority is edit-menu readback: tap the focused field -> Select
All -> Copy -> read returns the FULL field string byte-exact, viewport-independent; clear
is Select All -> Cut; paste is clipboard-write -> long-press -> Paste. OCR
keeps geometry (tap points) + presence signals (keyboard rows, switcher
open) only. No content verdict may rest on OCR, ever.

Covers all fields stack-wide: URL, title, comment, bio, search — any
scrollable text field. Close-out taps get the same discipline via
require_open (verify the target UI is open before tapping it).
"""

from __future__ import annotations

import os
import re
import time

from friend_poster.core.config import XXT_SCRIPTS  # noqa: E402
from friend_poster.device.ssh_device import PhoneError  # noqa: E402

FV_DIR = XXT_SCRIPTS + "/fieldvalue"


def norm_url(url: str) -> str:
    """Scheme/host-normalized URL for exact compare. Pure."""
    s = (url or "").strip().lower()
    s = re.sub(r"^[a-z0-9+.-]+://", "", s)
    return s.rstrip("/")


def verdict_empty(value: str, clipboard_before: bytes) -> tuple[bool, str]:
    """Empty-check on the value only. Pure. A non-empty read that equals the
    pre-copy clipboard means the copy never happened (blocked) — named."""
    if value == "":
        return True, ""
    if value.encode("utf-8", "replace") == (clipboard_before or b""):
        return False, (
            "field not provably empty (read %r == pre-copy clipboard: "
            "copy blocked or focus lost)" % value[:80])
    return False, "field not empty (read %r)" % value[:80]


def verdict_exact_url(value: str, want_url: str) -> tuple[bool, str]:
    """URL exact on normalized values. Pure. Both strings attach on fail."""
    got, want = norm_url(value), norm_url(want_url)
    if got == want:
        return True, ""
    return False, "want %r got %r" % (want_url, value)


def verdict_exact_text(value: str, want_text: str) -> tuple[bool, str]:
    """Literal exact. Pure. Both strings attach on fail."""
    if value == want_text:
        return True, ""
    return False, "want %r got %r" % (want_text, value)


def keyboard_present(nodes) -> bool:
    """Focus signal: >=10 short key rows low on screen. Presence only —
    never a content verdict. Pure."""
    keys = 0
    for n in nodes:
        if getattr(n, "ntype", "") == "ocr-line" or not n.on_screen():
            continue
        t = "".join((n.text or "").split())
        if 1 <= len(t) <= 6 and n.y > 900:
            keys += 1
    return keys >= 10


def require_open(nodes, pred, target: str) -> None:
    """Close-out discipline: target UI must test open before tapping it."""
    try:
        is_open = bool(pred(nodes))
    except Exception:
        is_open = False
    if not is_open:
        raise PhoneError(
            "%s not open in dump. Refusing blind tap (field law 4)." % target)


SELECT_ALL_LABELS = ("Select All", "Select all")
COPY_LABELS = ("Copy", "copy")
CUT_LABELS = ("Cut", "cut")
PASTE_LABELS = ("Paste", "paste")

FV_SENTINEL = b"FV-SENTINEL-9f3a"


_SCHEME_RE = re.compile(r'^[a-z0-9+.-]+://', re.I)


def _is_full_url(t: str) -> bool:
    """True only when the text STARTS with a scheme. A Find row like
    `Find "https://x"` contains :// but is not a URL. Pure."""
    return bool(_SCHEME_RE.match((t or "").strip().strip('"\u201c\u201d')))


def _is_placeholder(t: str) -> bool:
    """Field-placeholder text (empty signal), never content. Pure."""
    low = (t or "").strip().lower()
    return low in ("search", "address", "url") or low.startswith("search or enter")


TOP_STRIP_Y = 300
BAND_Y0 = 750
BAND_Y1 = 950
BAND_W_MIN = 200


def field_anchor(nodes):
    """Long-press target = the editable field ONLY: a full-URL node in the
    top strip (y<300) wins; else the widest above-keyboard field-window
    node (y750-950, w>=200 — the iOS 15 editor renders the field there;
    keyboard keys stay under 100px). Text markers are deliberately NOT
    used: any URL substring can fill the window (slug-only tails carry no
    scheme/slash), while placeholder pill text never anchors. Suggestion
    rows (narrower, or outside the band) NEVER anchor. Geometry only;
    content always comes from the clipboard. Pure."""
    from friend_poster.device.ui_tree import collapsed_text
    content, band, placeholder = [], [], []
    for n in nodes:
        if getattr(n, "ntype", "") == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if n.y < TOP_STRIP_Y:
            if _is_full_url(t):
                content.append(n)
            elif t in ("search", "address", "url"):
                placeholder.append(n)
        elif (BAND_Y0 <= n.y <= BAND_Y1 and n.w >= BAND_W_MIN
                and not _is_placeholder(t)):
            band.append(n)
    if content:
        return max(content, key=lambda n: n.w)
    if band:
        return max(band, key=lambda n: n.w)
    if placeholder:
        return max(placeholder, key=lambda n: n.w)
    return None


def anchor_has_text(anchor) -> bool:
    """Full-URL anchor = content; wide band field-window = content (the
    window shows an arbitrary scrolled substring — scheme, slashes and
    dots cannot be required); placeholder = empty. Pure."""
    from friend_poster.device.ui_tree import collapsed_text
    if anchor is None:
        return False
    t = collapsed_text(anchor.labels())
    if _is_full_url(t):
        return True
    return (BAND_Y0 <= anchor.y <= BAND_Y1 and anchor.w >= BAND_W_MIN
            and not _is_placeholder(t))


def echo_present(nodes) -> bool:
    """A full URL echoed anywhere, or a wide non-placeholder node in the
    above-keyboard field band (y750-950, w>=200 — keyboard keys stay under
    100px). Proves keystrokes landed somewhere. Presence only, never a
    content verdict. Pure."""
    from friend_poster.device.ui_tree import collapsed_text
    for n in nodes:
        if getattr(n, "ntype", "") == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if _is_full_url(t):
            return True
        if (BAND_Y0 <= n.y <= BAND_Y1 and n.w >= BAND_W_MIN
                and not _is_placeholder(t)):
            return True
    return False


def _lua_read_clipboard(out_path: str) -> str:
    return (
        "local ok, s = pcall(pasteboard.read)\n"
        "if (not ok) or (s == nil) then\n"
        "  ok, s = pcall(function() return pasteboard.read(\"public.utf8-plain-text\") end)\n"
        "end\n"
        "local fh = io.open(\"%s\", \"wb\")\n"
        "if ok and s ~= nil then fh:write(s) else fh:write(\"READ_ERR:\" .. tostring(s)) end\n"
        "fh:close()\n" % out_path
    )


def raise_menu(stack, phone, log, anchor_node, stem: str) -> None:
    """Raise the edit menu on the focused field, dump-verified each step.
    Tap (seats the cursor; occasionally raises the menu outright), then a
    1500ms true hold (raises the menu, or silently selects), then a final
    tap (raises the menu on an active selection). Locked 25 Sep: single /
    double / spaced taps alone fail, 1100ms hold fails, 1500ms hold raises
    it most runs; the trailing tap covers silent-select runs. Raises with
    the dump stem when no menu appears."""
    from friend_poster.device.ui_tree import dump_named, find_label_exact
    cx = int(anchor_node.x + anchor_node.w // 2)
    cy = int(anchor_node.y + anchor_node.h // 2)
    stack.tap_xy(cx, cy)
    time.sleep(0.9)
    _blob, nodes = dump_named(stack, phone, "%s_menu0" % stem, log, shot=False)
    if find_label_exact(nodes, SELECT_ALL_LABELS + PASTE_LABELS + CUT_LABELS + ("Select",)) is not None:
        return
    (log or print)("MENU not up after tap, 1500ms hold")
    stack.long_tap(cx, cy, hold_ms=1500)
    time.sleep(1.0)
    _blob, nodes = dump_named(stack, phone, "%s_menu1" % stem, log, shot=False)
    if find_label_exact(nodes, SELECT_ALL_LABELS + PASTE_LABELS + CUT_LABELS + ("Select",)) is not None:
        return
    (log or print)("MENU not up after hold, final tap on selection")
    stack.tap_xy(cx, cy)
    time.sleep(0.9)
    _blob, nodes = dump_named(stack, phone, "%s_menu2" % stem, log, shot=False)
    if find_label_exact(nodes, SELECT_ALL_LABELS + PASTE_LABELS + CUT_LABELS + ("Select",)) is not None:
        return
    raise PhoneError("edit menu not raised (tap+hold+tap). See %s dumps." % stem)


def _ensure_selected(stack, phone, log, stem: str) -> None:
    """Select-All unless the menu already shows an active selection
    (Cut/Copy without Select All — hold-selected or pill-preselected text).
    The downstream exact-match verdict judges full-vs-partial: a partial
    selection can only fail closed (append never equals want), never pass.
    Fail-closed when neither is present."""
    from friend_poster.device.ui_tree import dump_named, find_label_exact
    _blob, nodes = dump_named(stack, phone, stem + "_menu", log, shot=False)
    if find_label_exact(nodes, SELECT_ALL_LABELS) is not None:
        _menu_tap(stack, phone, log, SELECT_ALL_LABELS, stem + "_select")
    elif find_label_exact(nodes, CUT_LABELS + COPY_LABELS) is None:
        raise PhoneError(
            "edit-menu Select All/Select all not in dump. See %s dumps. "
            "Menu raise may have missed the field." % stem)
    else:
        (log or print)("Select All absent, active selection; proceeding")


def _menu_tap(stack, phone, log, labels, stem: str):
    """Dump, find an edit-menu item, tap it. Returns fresh (blob, nodes)."""
    from friend_poster.device.ui_tree import dump_named, find_label_exact
    blob, nodes = dump_named(stack, phone, stem, log, shot=False)
    hit = find_label_exact(nodes, labels)
    if hit is None:
        raise PhoneError(
            "edit-menu %s not in dump. See %s. Menu raise may have missed "
            "the field." % ("/".join(labels), stem))
    stack.tap_xy(*hit.center())
    time.sleep(0.9)
    return dump_named(stack, phone, stem + "_tapped", log, shot=False)


def grab_clipboard(stack) -> bytes:
    """Read-only clipboard snapshot. No field touched."""
    out = FV_DIR + "/clip_saved.bin"
    stack.phone.exec_ok("mkdir -p '%s'" % FV_DIR, timeout=8)
    stack.phone.exec_ok("rm -f '%s'" % out, timeout=8)
    stack._run_xxt_lua(
        "fv_clip.lua",
        "local ok, s = pcall(pasteboard.read)\n"
        "if (not ok) or (s == nil) then\n"
        "  ok, s = pcall(function() return pasteboard.read(\"public.utf8-plain-text\") end)\n"
        "end\n"
        "local fh = io.open(\"%s\", \"wb\")\n"
        "if ok and s ~= nil then fh:write(s) else fh:write(\"READ_ERR:\" .. tostring(s)) end\n"
        "fh:close()\n" % out,
        timeout=25, wait_path=out)
    return stack.phone.sftp_read_bytes(out)


def restore_clipboard(stack, raw: bytes) -> None:
    """Put the pre-read clipboard back (readback is non-destructive)."""
    stack.phone.sftp_write_bytes(FV_DIR + "/clip_restore.bin", raw or b"")
    stack._run_xxt_lua(
        "fv_restore.lua",
        "local fh = io.open(\"%s/clip_restore.bin\", \"rb\")\n"
        "local s = fh:read(\"*a\")\n"
        "fh:close()\n"
        "pasteboard.write(s or \"\")\n" % FV_DIR,
        timeout=20)


def _sentinel_clipboard(stack) -> None:
    stack.phone.exec_ok("mkdir -p '%s'" % FV_DIR, timeout=8)
    stack.phone.sftp_write_bytes(FV_DIR + "/clip_sentinel.bin", FV_SENTINEL)
    stack._run_xxt_lua(
        "fv_sentinel.lua",
        "local fh = io.open(\"%s/clip_sentinel.bin\", \"rb\")\n"
        "local s = fh:read(\"*a\")\n"
        "fh:close()\n"
        "pasteboard.write(s)\n" % FV_DIR,
        timeout=20)


def field_value(stack, phone, log, anchor_node, stem: str) -> tuple[str, bytes]:
    """Full field string via tap-on-focused-field -> Select All -> Copy ->
    read. The pre-written sentinel proves the copy really happened (a read
    equal to it means the Copy tap missed). Returns (value,
    clipboard_before); clipboard is restored before return. The anchor is
    geometry only; content comes from the clipboard alone. Locked 25 Sep:
    menu raising = tap (seats cursor) then 1500ms hold; taps or short
    holds alone do not raise it."""
    before = grab_clipboard(stack)
    _sentinel_clipboard(stack)
    raise_menu(stack, phone, log, anchor_node, stem + "_raise")
    _ensure_selected(stack, phone, log, stem + "_sel")
    _menu_tap(stack, phone, log, COPY_LABELS, stem + "_copy")
    out = FV_DIR + "/field_read.bin"
    stack.phone.exec_ok("rm -f '%s'" % out, timeout=8)
    stack._run_xxt_lua("fv_read.lua", _lua_read_clipboard(out),
                       timeout=25, wait_path=out)
    raw = stack.phone.sftp_read_bytes(out)
    try:
        restore_clipboard(stack, before)
    except Exception:
        pass
    if raw.startswith(b"READ_ERR:"):
        raise PhoneError("pasteboard.read failed on device: %s (see %s dumps)"
                         % (raw[:120].decode("utf-8", "replace"), stem))
    if raw == FV_SENTINEL:
        raise PhoneError(
            "copy unverified: readback equals the sentinel (Copy tap missed). "
            "See %s dumps." % stem)
    return raw.decode("utf-8", "replace"), before


def field_clear(stack, phone, log, anchor_node, stem: str) -> bytes:
    """Tap focused field -> Select All -> Cut. Returns the removed text
    (clipboard right after Cut): non-sentinel content proves the menu taps
    landed and something was removed. Clears scrolled content a
    backspace-block cannot reach. When the menu is already up in the
    selection state (Cut/Copy without Select All — text stayed selected
    from a previous Select All), Cut applies directly to the selection."""
    _sentinel_clipboard(stack)
    raise_menu(stack, phone, log, anchor_node, stem + "_raise")
    _ensure_selected(stack, phone, log, stem + "_sel")
    _menu_tap(stack, phone, log, CUT_LABELS, stem + "_cut")
    out = FV_DIR + "/field_cut.bin"
    stack.phone.exec_ok("rm -f '%s'" % out, timeout=8)
    stack._run_xxt_lua("fv_cutread.lua", _lua_read_clipboard(out),
                       timeout=25, wait_path=out)
    return stack.phone.sftp_read_bytes(out)


def menu_paste(stack, phone, log, text: str, anchor_node, stem: str) -> None:
    """Clipboard-write (proven) -> tap focused field -> Paste. The no-Cmd
    paste path. Caller verifies content afterwards via field_value."""
    stack.phone.exec_ok("mkdir -p '%s'" % FV_DIR, timeout=8)
    stack.phone.sftp_write_bytes(FV_DIR + "/clip_paste.bin", text.encode("utf-8"))
    stack._run_xxt_lua(
        "fv_paste_write.lua",
        "local fh = io.open(\"%s/clip_paste.bin\", \"rb\")\n"
        "local s = fh:read(\"*a\")\n"
        "fh:close()\n"
        "pasteboard.write(s)\n" % FV_DIR,
        timeout=20)
    raise_menu(stack, phone, log, anchor_node, stem + "_raise")
    _menu_tap(stack, phone, log, PASTE_LABELS, stem + "_paste")


def ensure_focus(stack, phone, log, tap_xy, stem: str = "fv_focus"):
    """Dump-check keyboard present; absent = re-tap field once, never type
    blind (covers post-collision focus loss). Returns fresh nodes."""
    from friend_poster.device.ui_tree import dump_named
    blob, nodes = dump_named(stack, phone, stem, log, shot=False)
    if keyboard_present(nodes):
        return nodes
    (log or print)("FOCUS lost, re-tap field once")
    stack.tap_xy(int(tap_xy[0]), int(tap_xy[1]))
    time.sleep(0.6)
    blob, nodes = dump_named(stack, phone, stem + "_retap", log, shot=False)
    if not keyboard_present(nodes):
        raise PhoneError(
            "focus lost: no keyboard after re-tap. See %s dumps. "
            "Refusing blind keystrokes (field law 2)." % stem)
    return nodes


def enter_verified(stack, phone, log, *, text: str, want_desc: str,
                   match, type_fn, focus_xy, stem: str) -> str:
    """Value-verified entry. Clear only when stale text shows (long-press
    the text node -> Cut; the removed clipboard content proves it). Type
    once -> read back via the typed echo row -> exact-check. A blind append
    can never pass: stale+typed != want aborts. Bounded: max 2 send_text
    attempts, then one menu-paste attempt off the last echo anchor, then
    abort+report with both strings. match(value) -> (ok, reason).
    type_fn() performs the single typing. focus_xy re-taps on focus loss."""
    from friend_poster.device.ui_tree import dump_named
    ensure_focus(stack, phone, log, focus_xy, stem + "_focus")
    last_reason = ""
    last_value = ""
    anchor = None
    for attempt in (1, 2):
        blob, nodes = dump_named(stack, phone, stem + "_pre%d" % attempt, log, shot=False)
        anchor = field_anchor(nodes)
        if anchor is not None and anchor_has_text(anchor):
            (log or print)("FIELD stale text, Cut (attempt %d/2)" % attempt)
            removed = field_clear(stack, phone, log, anchor, stem + "_clear%d" % attempt)
            if removed == FV_SENTINEL:
                last_reason = "cut unverified (Cut tap missed)"
                (log or print)("CUT fail: clipboard still sentinel")
                continue
            (log or print)("CUT ok, removed %d bytes" % len(removed))
        else:
            (log or print)("FIELD empty/placeholder, TYPE directly (attempt %d/2)" % attempt)
        type_fn()
        time.sleep(0.8)
        blob, nodes = dump_named(stack, phone, stem + "_echo%d" % attempt, log, shot=False)
        if not echo_present(nodes):
            last_reason = "typed text has no echo (typing went nowhere?)"
            (log or print)("ECHO fail: no text after typing")
            continue
        anchor = field_anchor(nodes)
        if anchor is None or not anchor_has_text(anchor):
            last_reason = "top field not anchoring after typing"
            (log or print)("ANCHOR fail: top field has no text node")
            continue
        value, _before = field_value(stack, phone, log, anchor, stem + "_exact%d" % attempt)
        last_value = value
        ok, reason = match(value)
        if ok:
            (log or print)("VERIFY_EXACT ok (%s)" % want_desc)
            return value
        last_reason = "exact-check: " + reason
        (log or print)("VERIFY_EXACT fail: %s" % reason)
    if anchor is not None and anchor_has_text(anchor):
        (log or print)("PASTE fallback off top-field anchor")
        field_clear(stack, phone, log, anchor, stem + "_pclear")
        menu_paste(stack, phone, log, text, anchor, stem + "_mpaste")
        blob, nodes = dump_named(stack, phone, stem + "_pecho", log, shot=False)
        anchor = field_anchor(nodes)
        if anchor is not None and anchor_has_text(anchor):
            value, _before = field_value(stack, phone, log, anchor, stem + "_pasted")
            last_value = value
            ok, reason = match(value)
            if ok:
                (log or print)("VERIFY_EXACT ok (%s, menu-paste)" % want_desc)
                return value
            last_reason = "paste exact-check: " + reason
    raise PhoneError(
        "field entry mismatch (want %s). Last: %s "
        "[value %r]. See %s dumps. Did not act."
        % (want_desc, last_reason, last_value[:160], stem))


def pick_wda_value(xml: str) -> str:
    """Value attr of the focused text input from a WDA source dump. Pure."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    cands = []
    for el in root.iter():
        tag = (el.tag or "").replace("XCUIElementType", "")
        if tag in ("TextField", "SearchField", "TextView", "SecureTextField"):
            val = el.attrib.get("value") or ""
            if val:
                focused = str(el.attrib.get("hasKeyboardFocus", "")).lower() == "true"
                cands.append((0 if focused else 1, val))
    if not cands:
        return ""
    cands.sort(key=lambda t: t[0])
    return cands[0][1]


def wda_focused_value(stack) -> str:
    """WDA path: fetch source, read value attr. Best-effort; WDA is not
    installed on this stack (kept so the law has a value-attribute
    reader wherever an accessibility tree exists)."""
    import urllib.request
    sid = ""
    sess = getattr(stack, "session", None)
    if sess is not None and hasattr(sess, "session_id"):
        sid = sess.session_id or ""
    base = "http://127.0.0.1:8100"
    url = "%s/session/%s/source" % (base, sid) if sid else "%s/source" % base
    with urllib.request.urlopen(url, timeout=20) as r:
        payload = r.read().decode("utf-8", "replace")
    try:
        data = __import__("json").loads(payload)
        xml = data.get("value", payload) if isinstance(data, dict) else payload
    except Exception:
        xml = payload
    return pick_wda_value(xml)
