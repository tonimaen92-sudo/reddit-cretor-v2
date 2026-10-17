# UTF-8
"""Image post in the iOS Reddit app. Dump-first. One Crane container.

Wipe local Camera Roll, XXTouch-import one PC file, SpringBoard Reddit (not uiopen),
Crane name, Safari OPEN into the community, Create -> Image -> the one Recents cell,
type_human title, Post, wipe Recents again.

No SIWA. No Join. Do not delete Crane containers. Community is a name, not a stored URL.

  python misc/reddit_post.py r1 SomeCommunity "a title" C:\\path\\to\\photo.jpg
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
from typing import Callable
from friend_poster.core.config import FULLSEND_DIR, USED_DIR, XXT_LOCAL_PORT, XXT_SCRIPTS  # noqa: E402
from friend_poster.reddit.flair_pick import close_sheet  # noqa: E402
from friend_poster.reddit.reddit_open import close_reddit, open_container, warn_siwa  # noqa: E402
from friend_poster.core.slots import get_slot, slot_reddit_ready  # noqa: E402
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import (  # noqa: E402
    TapStack,
    collapsed_text,
    dump_named,
    find_label_exact,
)

os.environ["SKIP_SHOT"] = "1"

LogFn = Callable[[str], None]
DCIM_DIR = "/var/mobile/Media/DCIM/100APPLE"
JOIN = ("Join", "Joined")
CREATE_LABELS = ("Create",)
IMAGE_LABELS = ("Image", "Images & Video", "Image & video", "Photo", "Photos", "Upload image")
RECENTS_LABELS = ("Recents", "Recent", "Camera Roll", "All Photos")
COMMUNITY_LABELS = (
    "Choose a community",
    "Select a community",
    "Add a community",
    "Community",
    "An official community",
)
TITLE_LABELS = ("Add a title", "Title", "An interesting title")
POST_LABELS = ("Post",)


def log(msg: str) -> None:
    print(msg, flush=True)


def dump(stack: TapStack, phone: Phone, stem: str, lg: LogFn, shot: bool = False):
    return dump_named(stack, phone, stem, lg, shot=shot)


def community_name(raw: str) -> str:
    """Name only. Never a URL. Strip a leading r/ if the user typed one."""
    s = (raw or "").strip()
    if not s:
        raise PhoneError("Community name is empty.")
    low = s.casefold()
    if "reddit.com" in low or low.startswith("http://") or low.startswith("https://"):
        raise PhoneError("Community must be a name, not a URL.")
    s = re.sub(r"^/+", "", s)
    if re.match(r"^r/", s, re.I):
        s = s[2:]
    s = s.strip().strip("/")
    if not s or "/" in s or " " in s:
        raise PhoneError("Community must be a single name (no URL, no path).")
    if not re.match(r"^[A-Za-z0-9_]+$", s):
        raise PhoneError("Community name has characters the composer will not take.")
    return s


def safari_community_url(community: str) -> str:
    """Runtime address from the name the user typed. Not saved."""
    return "https://www.reddit.com/r/" + community_name(community) + "/"


def _safe_folder(text: str, fallback: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", (text or "").strip()).strip("._-")
    return s.lower() or fallback


def used_dest_path(src: str, name: str, dest: str) -> str:
    out_dir = os.path.join(
        USED_DIR, _safe_folder(name, "slot"), _safe_folder(dest, "post")
    )
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.basename(src)
    stem, ext = os.path.splitext(base)
    if not ext:
        ext = ".jpg"
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem)[:60] or "image"
    candidate = os.path.join(out_dir, base if base else f"{stem}{ext}")
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(out_dir, f"{stem}_{n}{ext}")
        n += 1
    return candidate


def archive_used(
    image_paths: list[str] | str, name: str, dest: str, lg: LogFn | None = None
) -> list[str]:
    """MOVE posted sources to repo-root used/<slot>/<dest>/. Never raises.

    dest is a community name or a username. The source leaves its folder,
    so out/ empties as pics are consumed. Clash-safe (_2, _3, ...).
    Every moved file is also COPIED to full-send/subreddits/used/<dest>/
    (same clash rule); a mirror failure never fails the post.
    """
    if isinstance(image_paths, str):
        image_paths = [image_paths]
    moved: list[str] = []
    for src in image_paths:
        try:
            if not os.path.isfile(src):
                if lg:
                    lg(f"USED_SKIP missing {src}")
                continue
            dst = used_dest_path(src, name, dest)
            shutil.move(src, dst)
            moved.append(dst)
            if lg:
                lg(f"USED {dst}")
            mirror_used(dst, dest, lg)
        except OSError as e:
            if lg:
                lg(f"USED_SKIP {src}: {e}")
    return moved


def mirror_used(moved_path: str, dest: str, lg: LogFn | None = None) -> str:
    """Copy an archived post into full-send/subreddits/used/<dest>/.
    Returns the mirror path or ''. Never raises."""
    try:
        out_dir = os.path.join(
            FULLSEND_DIR, "subreddits", "used", _safe_folder(dest, "post")
        )
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.basename(moved_path)
        stem, ext = os.path.splitext(base)
        if not ext:
            ext = ".jpg"
        candidate = os.path.join(out_dir, base)
        n = 2
        while os.path.exists(candidate):
            candidate = os.path.join(out_dir, f"{stem}_{n}{ext}")
            n += 1
        shutil.copy2(moved_path, candidate)
        if lg:
            lg(f"USED_MIRROR {candidate}")
        return candidate
    except OSError as e:
        if lg:
            lg(f"USED_MIRROR_SKIP {moved_path}: {e}")
        return ""


def refuse_join(nodes, lg: LogFn) -> None:
    j = find_label_exact(nodes, JOIN)
    if j is not None and collapsed_text(j.labels()) == "join":
        lg("Join is on screen. Will not tap Join.")


def next_dcim_name(phone: Phone, ext: str) -> str:
    code, out = phone.exec_ok(f"ls -1 '{DCIM_DIR}' 2>/dev/null | tail -80", timeout=15)
    nums: list[int] = []
    for ln in (out or "").splitlines():
        m = re.match(r"IMG_(\d+)\.", ln.strip(), re.I)
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    if n > 9999:
        n = 1
    return f"IMG_{n:04d}.{ext}"


def read_image_bytes(local_path: str) -> tuple[bytes, str]:
    path = os.path.abspath(local_path)
    if not os.path.isfile(path):
        raise PhoneError("Image file not on this PC: " + path)
    raw = open(path, "rb").read()
    if len(raw) < 32:
        raise PhoneError("Image file is empty.")
    ext = os.path.splitext(path)[1].lstrip(".").upper() or "JPG"
    if ext in ("JPEG", "JPE"):
        ext = "JPG"
    if ext not in ("JPG", "PNG"):
        raise PhoneError("Image must be JPG or PNG.")
    if raw[:3] == b"\xff\xd8\xff":
        ext = "JPG"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "PNG"
    return raw, ext


def clear_local_photos(stack: TapStack, lg: LogFn) -> None:
    """XXTouch clear.all_photos: all local Camera Roll. Not iCloud stream. Not keychain."""
    out = f"{XXT_SCRIPTS}/ra_wipe.txt"
    lua = (
        "screen.init(0)\n"
        "clear.all_photos()\n"
        f'local f = io.open("{out}", "w")\n'
        'f:write("OK")\n'
        "f:close()\n"
    )
    stack.phone.exec_ok(f"rm -f '{out}'", timeout=8)
    lg("XXTouch clear.all_photos (local library). Waiting.")
    stack._run_xxt_lua("ra_wipe.lua", lua, timeout=300, wait_path=out)
    got = ""
    try:
        got = stack.phone.sftp_read(out).strip()
    except Exception:
        got = ""
    if got != "OK":
        raise PhoneError("clear.all_photos did not finish. See ra_wipe.txt. Did not guess Recents.")
    lg("Camera Roll local wipe OK.")
    code, out = stack.phone.exec_ok(
        f"ls -1 '{DCIM_DIR}' 2>/dev/null | wc -l; ls -1 '{DCIM_DIR}' 2>/dev/null | head -12",
        timeout=15,
    )
    lg("DCIM after wipe: " + " ".join((out or "").split()))


def import_to_camera_roll(stack: TapStack, local_path: str, lg: LogFn) -> None:
    """Wipe local Recents, then XXTouch album import so Recents has exactly one photo."""
    clear_local_photos(stack, lg)
    raw, ext = read_image_bytes(local_path)
    mime = "image/jpeg" if ext == "JPG" else "image/png"
    lg(f"XXTouch /image_to_album bytes={len(raw)} {ext}")
    req = urllib.request.Request(
        f"http://127.0.0.1:{XXT_LOCAL_PORT}/image_to_album",
        data=raw,
        headers={"Content-Type": mime},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        lg(f"image_to_album HTTP failed: {e}")
        body = ""
    lg("image_to_album " + (body[:200] or "(empty)"))
    code = -1
    try:
        code = int((json.loads(body) or {}).get("code", -1))
    except Exception:
        code = -1
    if code == 0:
        lg("Camera Roll import OK (XXTouch album).")
        return
    remote = f"{XXT_SCRIPTS}/ra_album.{ext.lower()}"
    out = f"{XXT_SCRIPTS}/ra_album.txt"
    stack.phone.sftp_write_bytes(remote, raw)
    lua = (
        "screen.init(0)\n"
        f'local img = image.load_file("{remote}")\n'
        f'local f = io.open("{out}", "w")\n'
        "if image.is(img) then\n"
        "  img:save_to_album()\n"
        '  f:write("OK")\n'
        "else\n"
        '  f:write("FAIL load")\n'
        "end\n"
        "f:close()\n"
    )
    stack.phone.exec_ok(f"rm -f '{out}'", timeout=8)
    stack._run_xxt_lua("ra_album.lua", lua, timeout=25)
    deadline = time.time() + 12
    got = ""
    while time.time() < deadline:
        try:
            got = stack.phone.sftp_read(out).strip()
            if got:
                break
        except Exception:
            pass
        time.sleep(0.2)
    lg("save_to_album " + (got or "(no result)"))
    if got != "OK":
        raise PhoneError(
            "XXTouch did not import into Camera Roll. Keep XXTouch Elite open, Remote Service on. "
            "Did not install Filza. See image_to_album / ra_album.txt."
        )
    lg("Camera Roll import OK (save_to_album).")


def push_image(phone: Phone, local_path: str, lg: LogFn) -> str:
    path = os.path.abspath(local_path)
    if not os.path.isfile(path):
        raise PhoneError("Image file not on this PC: " + path)
    raw = open(path, "rb").read()
    if len(raw) < 32:
        raise PhoneError("Image file is empty.")
    ext = os.path.splitext(path)[1].lstrip(".").upper() or "JPG"
    if ext in ("JPEG", "JPE"):
        ext = "JPG"
    if ext not in ("JPG", "PNG"):
        raise PhoneError("Image must be JPG or PNG.")
    if raw[:3] == b"\xff\xd8\xff":
        ext = "JPG"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "PNG"
    phone.exec_ok(f"mkdir -p '{DCIM_DIR}'", timeout=10)
    remote_name = next_dcim_name(phone, ext)
    remote = f"{DCIM_DIR}/{remote_name}"
    lg(f"SFTP image -> {remote} bytes={len(raw)}")
    phone.sftp_write_bytes(remote, raw)
    phone.exec_ok(
        f"chown mobile:mobile '{remote}'; chmod 644 '{remote}'",
        timeout=10,
    )
    # Refresh Photos index. Not Reddit. Not Crane.
    phone.exec_ok("killall assetsd cameracaptured 2>/dev/null; true", timeout=10)
    time.sleep(1.2)
    lg("Camera Roll updated (assetsd).")
    return remote


def safari_start_page(blob: str) -> bool:
    """Start Page after the Reddit tab is gone. Status-bar 'Reddit' does not count."""
    low = (blob or "").lower()
    if "reddit.com" in low or "open in the reddit" in low:
        return False
    return ("favorites" in low) or ("search or enter website" in low)


def tab_switcher_open(nodes) -> bool:
    has_done = False
    has_tab = False
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if t == "done" and n.y > 1100:
            has_done = True
        if "tab" in t and n.y > 1100:
            has_tab = True
    return has_done and has_tab


# Locked 22 Sep 17:27 (dumps 3060/3061/3062). OCR never sees either control.
TAB_OVERVIEW = (690, 1290)
TAB_CARD_X = (597, 148)


def reddit_tab_card(nodes) -> bool:
    """Reddit card in the tab switcher (title sits under the status bar)."""
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if 70 < n.y < 280 and (t == "reddit" or "open in the reddit" in t):
            return True
    return False


def single_reddit_tab(nodes) -> bool:
    """Single-tab overview whose card carries reddit markers outside the
    title band (locked 25 Sep: title OCR dropped the y<280 lines, leaving
    only the card-preview 'Reddit' at y~800). A single Start Page tab has
    no reddit text and still takes the Done path."""
    texts = [collapsed_text(n.labels()) for n in nodes
             if n.ntype != "ocr-line" and n.on_screen()]
    low = " ".join(texts).lower()
    return "1 tab" in low and "reddit" in low


def close_safari_reddit_tab(stack: TapStack, phone: Phone, lg: LogFn) -> None:
    """Open Safari and close the Reddit tab left by safari_open.

    Locked path, verified live 22 Sep 17:27 on the Freddy comment:
    uiopen Safari -> (690,1290) tab overview -> (597,148) card X -> Start Page.
    Text dumps only until the Start Page proof shot.
    """
    lg("Close Safari Reddit tab: (690,1290) then (597,148).")
    phone.exec_ok("uiopen --bundleid com.apple.mobilesafari", timeout=10)
    time.sleep(1.0)
    blob, nodes = dump(stack, phone, "3060_safari", lg, shot=False)
    from friend_poster.core.fieldvalue import keyboard_present
    for _ in range(2):
        if not keyboard_present(nodes):
            break
        cancel = find_label_exact(nodes, ("Cancel",))
        if cancel is None:
            lg("Keyboard signal without Cancel node, proceeding")
            break
        lg("Keyboard up: TAP Cancel %s before tab overview" % (cancel.center(),))
        stack.tap_xy(*cancel.center())
        time.sleep(0.8)
        blob, nodes = dump(stack, phone, "3060_kbcancel", lg, shot=False)
    if safari_start_page(blob):
        lg("Safari already on Start Page. No Reddit tab.")
        stack.press_home()
        return
    if not tab_switcher_open(nodes):
        last = ""
        for _ in range(3):
            if blob and blob == last:
                break
            last = blob
            time.sleep(1.0)
            blob, nodes = dump(stack, phone, "3060_settle", lg, shot=False)
        lg(f"TAP tab overview {TAB_OVERVIEW}")
        stack.tap_xy(*TAB_OVERVIEW)
        time.sleep(1.0)
        blob, nodes = dump(stack, phone, "3061_tabs", lg, shot=False)
    if not tab_switcher_open(nodes) and not safari_start_page(blob):
        time.sleep(1.5)
        blob, nodes = dump(stack, phone, "3061_tabs_slow", lg, shot=False)
    if safari_start_page(blob):
        lg("Start Page. Reddit tab already gone.")
        stack.press_home()
        return
    if not tab_switcher_open(nodes):
        dump(stack, phone, "3061_tabs_fail", lg, shot=True)
        raise PhoneError("Safari tab switcher not open. See 3061_tabs_fail.png.")
    if reddit_tab_card(nodes):
        lg(f"TAP tab X {TAB_CARD_X}")
        stack.tap_xy(*TAB_CARD_X)
        time.sleep(0.5)
    elif single_reddit_tab(nodes):
        lg(f"TAP tab X {TAB_CARD_X} (single reddit tab, title OCR variant)")
        stack.tap_xy(*TAB_CARD_X)
        time.sleep(0.5)
    else:
        done = find_label_exact(nodes, ("Done",))
        if done is None or done.y < 1000:
            dump(stack, phone, "3061_tabs_fail", lg, shot=True)
            raise PhoneError("No Reddit card in the tab switcher. See 3061_tabs_fail.png.")
        lg(f"No Reddit card. TAP Done {done.center()}")
        stack.tap_xy(*done.center())
        time.sleep(0.4)
    blob, nodes = dump(stack, phone, "3062_start", lg, shot=True)
    if not safari_start_page(blob):
        raise PhoneError("Safari Reddit tab still open. See 3062_start.png.")
    lg("Safari Reddit tab closed. Start Page.")
    stack.press_home()


def _address_field(nodes):
    """URL bar / search field on a real Safari page (not switcher/grid).

    Content-bearing bar only: the pill ('search or enter …') or a field
    showing a full URL. A bare 'Search' header (top strip) never qualifies
    (locked 25 Sep: it misidentified an open editor as ready)."""
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels()).lower()
        if "search or enter" in t:
            return n
        if re.match(r"^[a-z0-9+.-]+://", t.strip().strip("\"“”")):
            return n
    return None


def safari_walk_back(stack: TapStack, phone: Phone, lg: LogFn,
                     blob: str, nodes, rounds: int = 3):
    """Dump-first guard before any URL-bar tap (locked 23 Sep: blind tap
    landed in tab-groups view and typed nowhere).

    Empty-tab state always needs the walk-back, never a blind coordinate:
    tab-groups view (no address field) -> tap Done, re-dump; tab grid with
    an unfocused tab (Done low, still no address field) -> tap Done again,
    re-dump. Only return once an address/search field is present.
    """
    for i in range(1, rounds + 1):
        if _address_field(nodes) is not None:
            lg(f"SAFARI_GUARD address field present (round {i}).")
            return blob, nodes
        low = (blob or "").lower()
        if "tab groups" in low:
            done = find_label_exact(nodes, ("Done",))
            if done is None:
                raise PhoneError(
                    "Tab-groups view, no Done. See 970_walkback.")
            lg(f"SAFARI_GUARD tab-groups view, TAP Done {done.center()}")
            stack.tap_xy(*done.center())
            time.sleep(1.0)
            blob, nodes = dump(stack, phone, "970_walkback", lg, shot=False)
            continue
        lg(f"SAFARI_GUARD no address field (round {i}), TAP Done, re-dump")
        done = find_label_exact(nodes, ("Done",))
        if done is None:
            raise PhoneError("Safari URL bar not in dump. See 970_safari.")
        stack.tap_xy(*done.center())
        time.sleep(1.0)
        blob, nodes = dump(stack, phone, "970_walkback", lg, shot=False)
    if _address_field(nodes) is None:
        raise PhoneError("Safari URL bar not in dump. See 970_walkback.")
    lg("SAFARI_GUARD address field present after walk-back.")
    return blob, nodes


def _field_band_text(nodes) -> str:
    """Typed-URL text lives in the editor band (y 750-950). Suggestions sit
    higher (y<500); the keyboard sits lower (y>950). Band-scoping keeps a
    stale suggestion row from passing as field content."""
    parts = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if 750 <= n.y <= 950 and ("/" in t or ".com" in t or "comments" in t):
            parts.append(t)
    return " ".join(parts)


def _post_ids(blob: str) -> set[str]:
    # OCR clips the head off narrow URL nodes ("omments/…", "ents/…").
    return set(re.findall(
        r"(?:comments|omments|mments|ments|ents)/([a-z0-9]{5,8})",
        (blob or "").lower()))


def type_url_verified(stack: TapStack, phone: Phone, lg: LogFn, url: str,
                      stem: str = "972", focus_xy=None) -> None:
    """Idempotent Safari URL entry. The single choke point for URL typing.
    Field law: clear -> verify-empty-by-VALUE -> type once ->
    verify-exact-by-VALUE (pasteboard readback, never OCR). Bounded retype
    (max 2 send_text attempts), then one edit-menu paste attempt, then abort
    with both strings. No blind appends, ever."""
    from friend_poster.core.fieldvalue import (enter_verified, field_value, menu_paste,
                            verdict_exact_url)
    low = (url or "").lower()
    m = re.search(r"/r/([a-z0-9_]+)/", low)
    sub = m.group(1) if m else ""
    m = re.search(r"comments/([a-z0-9]{5,8})", low)
    want_id = m.group(1) if m else ""
    want_desc = want_id or ("r/" + sub)
    if focus_xy is None:
        raise PhoneError("type_url_verified needs focus_xy (long-press point).")
    enter_verified(
        stack, phone, lg, text=url, want_desc=want_desc,
        match=lambda v: verdict_exact_url(v, url),
        type_fn=lambda: stack.type_text_block(url),
        focus_xy=focus_xy, stem=stem)


def tap_open_banner(nodes):
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen() or n.y > 220:
            continue
        if collapsed_text(n.labels()) == "open":
            return n
    return None


def safari_open(stack: TapStack, phone: Phone, lg: LogFn, url: str) -> None:
    """Home then Safari. OPEN into Reddit. Not uiopen Reddit (that skips Crane)."""
    lg("Home then Safari (container stays in recents). Not uiopen Reddit.")
    stack.press_home()
    time.sleep(0.7)
    phone.exec_ok("uiopen --bundleid com.apple.mobilesafari", timeout=10)
    time.sleep(1.3)
    blob, nodes = dump(stack, phone, "970_safari", lg, shot=True)
    d = find_label_exact(nodes, ("Dismiss",))
    if d is not None:
        stack.tap_xy(*d.center())
        time.sleep(0.4)
        blob, nodes = dump(stack, phone, "970_dismiss", lg, shot=True)
    blob, nodes = safari_walk_back(stack, phone, lg, blob, nodes)
    bar = None
    bottom = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        b = collapsed_text(n.labels())
        if n.y > 1140 and b not in ("aa", "b", "1") and n.w > 50:
            bottom.append(n)
        if 160 < n.y < 240 and n.x < 250 and n.w > 40 and b not in ("x", "o"):
            bar = n
    if bottom:
        bar = max(bottom, key=lambda n: n.w)
    if bar is None:
        raise PhoneError("Safari URL bar not in dump. See 970_safari.")
    lg("TAP Safari URL bar")
    stack.tap_xy(*bar.center())
    time.sleep(0.5)
    blob, nodes = dump(stack, phone, "971_editor", lg, shot=True)
    focus_xy = bar.center()
    field = find_label_exact(nodes, ("Search", "Address", "URL", "Cancel"))
    if field is not None and collapsed_text(field.labels()) != "cancel":
        if field.y < 140:
            stack.tap_xy(*field.center())
            time.sleep(0.25)
            focus_xy = field.center()
    type_url_verified(stack, phone, lg, url, focus_xy=focus_xy)
    blob, nodes = dump(stack, phone, "972_go", lg, shot=False)
    go = find_label_exact(nodes, ("Go", "go"))
    if go is not None:
        lg("TAP Go")
        stack.tap_xy(*go.center())
    else:
        stack._run_xxt_lua(
            "ra_type.lua", 'screen.init(0)\nkey.press("RETURN")\n', timeout=10
        )
        lg("RETURN")
    time.sleep(2.4)
    blob, nodes = dump(stack, phone, "973_page", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    op = tap_open_banner(nodes)
    if op is None:
        raise PhoneError("OPEN not on Safari page. See 973_page.")
    lg("TAP OPEN")
    stack.tap_xy(*op.center())
    time.sleep(2.0)
    blob, nodes = dump(stack, phone, "974_after_open", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    cont = find_label_exact(nodes, ("Continue",))
    if cont is not None:
        lg("TAP Continue")
        stack.tap_xy(*cont.center())
        time.sleep(1.4)
        blob, nodes = dump(stack, phone, "975_community", lg, shot=True)
        refuse_join(nodes, lg)


def tap_create(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    creates = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) == "create":
            creates.append(n)
    if not creates:
        raise PhoneError("Create not on the community. See 950_feed dump.")
    n = max(creates, key=lambda nd: nd.y)
    lg("TAP Create y=%s" % n.center()[1])
    stack.tap_xy(*n.center())
    time.sleep(1.2)
    blob, nodes = dump(stack, phone, "951_create_sheet", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    return nodes


def tap_image_type(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    n = find_label_exact(nodes, IMAGE_LABELS)
    if n is None:
        n = composer_image_icon(nodes)
    if n is None:
        raise PhoneError("Image type not on Create sheet. See 951_create_sheet.")
    lg("TAP Image")
    stack.tap_xy(*n.center())
    time.sleep(1.2)
    blob, nodes = dump(stack, phone, "952_photos", lg, shot=True)
    warn_siwa(nodes, lg)
    return nodes


def composer_image_icon(nodes):
    """Toolbar image icon: 2nd from left (x~=144). Keyboard-up parks it
    at y~700-860, keyboard-down at y~1200-1330; OCR sees icon junk
    ('9' for image, '2' link). Small nodes near x=144 only, so body
    text never wins. Pure."""
    n = find_label_exact(nodes, IMAGE_LABELS)
    if n is not None:
        return n
    cands = []
    for nd in nodes:
        if nd.ntype == "ocr-line" or not nd.on_screen():
            continue
        if not ((700 <= nd.y <= 860) or (1200 <= nd.y <= 1330)):
            continue
        if not (60 <= nd.x <= 220):
            continue
        try:
            if nd.w > 90 or nd.h > 90:
                continue
        except Exception:
            pass
        cands.append(nd)
    if not cands:
        return None
    return min(cands, key=lambda nd: abs(nd.x - 144))


_PICKER_CHROME = {
    "cancel",
    "folder",
    "folder v",
    "next",
    "recents",
    "recent",
    "camera roll",
    "all photos",
    "camera",
    "0",
    "add",
    "done",
}


def only_recents_photo_cell(nodes) -> tuple[int, int]:
    """After wipe+import: Recents is Camera + one photo. Tap that photo. Do not pick among thumbs."""
    cam = find_label_exact(nodes, ("Camera",))
    if cam is None or not cam.on_screen() or cam.y > 500:
        raise PhoneError(
            "Recents picker dump has no Camera cell. See 952. Will not guess a thumb."
        )
    extras = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if t in _PICKER_CHROME or t.startswith("folder"):
            continue
        if n.y < 140 or n.y > 1100:
            continue
        extras.append(t[:40])
    if extras:
        raise PhoneError(
            "Recents dump is not Camera + one photo (extra: "
            + ", ".join(extras[:8])
            + "). Wipe local Camera Roll, import one file, keep iCloud Photos off. "
            "Will not guess which thumb is newest."
        )
    # 3-col grid: Camera is col 0; the only imported photo is col 1, same row.
    x = min(cam.x + 250, 500)
    y = max(cam.y - 80, 160)
    return (x, y)


def tap_recents_photo(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    refuse_join(nodes, lg)
    allow = find_label_exact(nodes, ("Enable Access", "Allow", "Allow Access to All Photos"))
    if allow is not None:
        lg("TAP " + collapsed_text(allow.labels()))
        stack.tap_xy(*allow.center())
        time.sleep(1.2)
        blob, nodes = dump(stack, phone, "952_after_allow", lg, shot=True)
    rec = find_label_exact(nodes, RECENTS_LABELS)
    if rec is not None and rec.x < 280 and rec.y < 200:
        lg("TAP Recents album")
        stack.tap_xy(*rec.center())
        time.sleep(1.0)
        blob, nodes = dump(stack, phone, "952_recents", lg, shot=True)
    x, y = only_recents_photo_cell(nodes)
    lg(f"TAP the only Recents photo (right of Camera) ({x},{y})")
    stack.tap_xy(x, y)
    time.sleep(1.2)
    blob, nodes = dump(stack, phone, "953_after_photo", lg, shot=True)
    warn_siwa(nodes, lg)
    go = None
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) in ("next", "add", "done", "use", "choose"):
            if collapsed_text(n.labels()) != "cancel":
                go = n
                if n.y > 1100:
                    break
    if go is not None:
        lg("TAP " + collapsed_text(go.labels()))
        stack.tap_xy(*go.center())
        time.sleep(1.2)
        blob, nodes = dump(stack, phone, "953_after_next", lg, shot=True)
    add = find_label_exact(nodes, ("Add",))
    if add is not None and collapsed_text(add.labels()) == "add":
        lg("TAP Add")
        stack.tap_xy(*add.center())
        time.sleep(1.2)
        blob, nodes = dump(stack, phone, "953_after_add", lg, shot=True)
    return nodes


def type_community_and_title(
    stack: TapStack, phone: Phone, lg: LogFn, nodes, community: str, title: str
):
    comm = find_label_exact(nodes, COMMUNITY_LABELS)
    if comm is None:
        raise PhoneError("Community field not on composer. See 953 dump.")
    lg("TAP community field")
    stack.tap_xy(*comm.center())
    time.sleep(0.7)
    blob, nodes = dump(stack, phone, "954_community", lg, shot=True)
    field = find_label_exact(
        nodes, ("Search", "Search for a community", "Community") + COMMUNITY_LABELS
    )
    if field is not None:
        stack.tap_xy(*field.center())
        time.sleep(0.35)
    stack.type_text_block(community)
    time.sleep(0.8)
    blob, nodes = dump(stack, phone, "955_community_typed", lg, shot=True)
    hit = find_label_exact(nodes, (community, "r/" + community, "r/" + community.lower()))
    if hit is None:
        # Result row often repeats the name; take a labeled node containing it.
        for n in nodes:
            if n.ntype == "ocr-line" or not n.on_screen():
                continue
            blob = collapsed_text(n.labels())
            if community.casefold() in blob and n.y > 140:
                hit = n
                break
    if hit is None:
        raise PhoneError(
            "Typed community is not in the result dump. See 955_community_typed."
        )
    lg("TAP community result")
    stack.tap_xy(*hit.center())
    time.sleep(0.9)
    blob, nodes = dump(stack, phone, "956_title", lg, shot=True)
    return type_title(stack, phone, lg, nodes, title)


def type_title(stack: TapStack, phone: Phone, lg: LogFn, nodes, title: str):
    tit = find_label_exact(nodes, TITLE_LABELS)
    if tit is None:
        raise PhoneError("Title field not on composer. See 956_title.")
    lg("TAP title")
    stack.tap_xy(*tit.center())
    time.sleep(0.35)
    stack.type_human(title, ms=95)
    blob, nodes = dump(stack, phone, "957_ready", lg, shot=True)
    return nodes


def apply_flair(stack: TapStack, phone: Phone, lg: LogFn, nodes, flair: str):
    """Tap the flair row, pick the matching sheet option, return composer nodes.

    Matching is contains-match case-insensitive. Header rows (sheet title,
    "r/<sub> flair", "Search flair", "View all flair") never count: any row
    whose text contains "flair" or which sits at y<=400 is skipped. This
    absorbs the Celebrity header-count bug by construction. No match, or no
    flair row when one is wanted: abort + report, never guess.
    """
    want = (flair or "").strip()
    if not want:
        return nodes
    row = None
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if "flair" in collapsed_text(n.labels()).lower():
            row = n
            break
    if row is None:
        lg("FLAIR_SKIP no flair row on composer (flair=%s kept, not demanded)" % want)
        return nodes
    lg("TAP flair row " + collapsed_text(row.labels())[:40])
    stack.tap_xy(*row.center())
    time.sleep(1.2)
    blob, nodes = dump(stack, phone, "957_flair", lg, shot=True)
    opts = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if want.casefold() not in t.casefold():
            continue
        if "flair" in t.lower() or n.y <= 400:
            continue
        opts.append(n)
    if not opts:
        seen = sorted({collapsed_text(n.labels())[:40] for n in nodes
                       if n.ntype != "ocr-line" and n.on_screen()})
        raise PhoneError(
            "Flair %r not on sheet (seen: %s). See 957_flair. Did not guess."
            % (want, ", ".join(seen[:12])))
    pick = min(opts, key=lambda n: (0 if collapsed_text(n.labels()).casefold() == want.casefold() else 1, n.y))
    lg("TAP flair option " + collapsed_text(pick.labels())[:40])
    stack.tap_xy(*pick.center())
    time.sleep(1.2)
    blob, nodes = dump(stack, phone, "957_flaired", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    # the option tap selects but never dismisses the sheet: X-close it
    # (x3 + drag fallback) and prove the composer is back. Proof is Post
    # present with the sheet gone (close_sheet raises while it persists):
    # never the title placeholder, which is absent once text is typed.
    blob, nodes = close_sheet(stack, phone, lg, stem="957_close")
    post = find_label_exact(nodes, ("Post",))
    if post is None or post.y >= 160:
        raise PhoneError("Composer back but no Post button. See 957_close.")
    lg("Composer back with flair applied, Post present.")
    return nodes


def tap_submit(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    refuse_join(nodes, lg)
    post = None
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) != "post":
            continue
        if n.y < 160:
            post = n
            break
    if post is None:
        post = find_label_exact(nodes, POST_LABELS)
    if post is None:
        raise PhoneError("Post button not on composer. See 957_ready.")
    if collapsed_text(post.labels()) == "join":
        raise PhoneError("Refusing Join.")
    lg("TAP Post (composer)")
    stack.tap_xy(*post.center())
    time.sleep(2.2)
    blob, nodes = dump(stack, phone, "958_after_post", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    lg("AFTER_POST " + " | ".join(
        collapsed_text(n.labels())[:40]
        for n in nodes
        if n.ntype != "ocr-line" and n.on_screen()
    )[:500])
    return nodes


def already_on_feed(nodes) -> bool:
    if find_label_exact(nodes, ("Select Container",)):
        return False
    if find_label_exact(nodes, CREATE_LABELS):
        return True
    blob = " ".join(collapsed_text(n.labels()) for n in nodes if n.on_screen())
    return "popular near you" in blob or "home" in blob.split()[:12]


def tap_search_communities_tab(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    """Search results filter: Posts | Communities | Comments | Media. Not the bottom bar."""
    hit = None
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        blob = collapsed_text(n.labels())
        if "communities" not in blob:
            continue
        if n.y > 1050:
            continue
        if n.y < 100:
            continue
        hit = n
        break
    if hit is None:
        posts = None
        for n in nodes:
            if not n.on_screen():
                continue
            blob = collapsed_text(n.labels())
            if blob == "posts" or blob.startswith("posts "):
                if 100 < n.y < 400:
                    posts = n
                    break
        if posts is None:
            raise PhoneError(
                "Search Communities tab not in dump (OCR often misses it). "
                "Not tapping the bottom Communities icon. See 965_now."
            )
        x = min(posts.x + posts.w + 70, 420)
        y = posts.center()[1]
        lg("OCR missed Communities; TAP right of Posts tab")
        stack.tap_xy(x, y)
    else:
        lg("TAP search Communities tab " + collapsed_text(hit.labels()))
        stack.tap_xy(*hit.center())
    time.sleep(1.0)
    blob, nodes = dump(stack, phone, "966_search_communities", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    return nodes


def tap_feed_search(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    q = find_label_exact(nodes, ("Q", "Search", "Search Reddit"))
    if q is None:
        raise PhoneError("Search (Q) not on the feed. See 960_now. Did not guess.")
    lg("TAP Search")
    stack.tap_xy(*q.center())
    time.sleep(1.0)
    blob, nodes = dump(stack, phone, "961_search", lg, shot=True)
    warn_siwa(nodes, lg)
    return nodes


def enter_community(stack: TapStack, phone: Phone, lg: LogFn, community: str):
    """Feed Search -> type community name -> tap the community. No URL. No Join."""
    community = community_name(community)
    blob, nodes = dump(stack, phone, "960_now", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    if find_label_exact(nodes, ("Select Container",)):
        raise PhoneError("Crane picker is up. Open r1 first.")
    nodes = tap_feed_search(stack, phone, lg, nodes)
    field = find_label_exact(
        nodes, ("Search", "Search Reddit", "Search for communities", "Q")
    )
    if field is not None:
        stack.tap_xy(*field.center())
        time.sleep(0.35)
    lg("TYPE community name (not a URL)")
    stack.type_text_block(community)
    time.sleep(1.1)
    blob, nodes = dump(stack, phone, "962_results", lg, shot=True)
    refuse_join(nodes, lg)
    comm_tab = find_label_exact(nodes, ("Communities", "Communities tab"))
    if comm_tab is not None:
        lg("Search defaults to posts/feed — TAP Communities tab")
        stack.tap_xy(*comm_tab.center())
        time.sleep(0.9)
        blob, nodes = dump(stack, phone, "962_communities", lg, shot=True)
        refuse_join(nodes, lg)
    hit = None
    want = community.casefold()
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        blob = collapsed_text(n.labels())
        if want not in blob.replace(" ", ""):
            continue
        if blob in ("join", "joined"):
            continue
        if n.y < 120:
            continue
        hit = n
        break
    if hit is None:
        raise PhoneError("Community not in Search results. See 962_results.")
    lg("TAP community row " + collapsed_text(hit.labels()))
    stack.tap_xy(min(max(hit.center()[0], 200), 520), hit.center()[1])
    time.sleep(1.3)
    blob, nodes = dump(stack, phone, "963_community", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    nsfw = find_label_exact(
        nodes, ("Continue", "Yes", "Enter", "I understand", "OK")
    )
    if nsfw is not None and collapsed_text(nsfw.labels()) != "cancel":
        if find_label_exact(nodes, ("Join",)) and collapsed_text(nsfw.labels()) == "join":
            nsfw = None
        if nsfw is not None and collapsed_text(nsfw.labels()) != "join":
            lg("NSFW gate -> " + collapsed_text(nsfw.labels()))
            stack.tap_xy(*nsfw.center())
            time.sleep(1.0)
            blob, nodes = dump(stack, phone, "964_after_nsfw", lg, shot=True)
            refuse_join(nodes, lg)
    lg("COMMUNITY_OPEN " + community)
    return nodes


def run_post(
    phone: Phone,
    stack: TapStack,
    lg: LogFn,
    name: str,
    community: str,
    title: str,
    image_path: str,
    stop: threading.Event | None = None,
    flair: str = "",
) -> None:
    name = (name or "").strip()
    title = (title or "").strip()
    flair = (flair or "").strip()[:64]
    community = community_name(community)
    if not name:
        raise PhoneError("Container name is empty.")
    if not title:
        raise PhoneError("Title is empty.")
    slot = get_slot(name)
    if not slot or not slot.get("ghost"):
        raise PhoneError(f"{name} has no Ghost/Crane container.")
    if not slot.get("email"):
        raise PhoneError(f"{name} has no Hide My Email.")
    if not slot_reddit_ready(slot):
        raise PhoneError(f"{name} does not have a completed Reddit account.")
    email = str(slot.get("email") or "")
    t0 = time.monotonic()
    def stage(label: str) -> None:
        lg("POST_T+%.1fs %s" % (time.monotonic() - t0, label))
    lg(f"POST container={name} community={community} title_len={len(title)}")
    stage("start")
    if stop is not None and stop.is_set():
        return
    import_to_camera_roll(stack, image_path, lg)
    stage("roll ready")
    if stop is not None and stop.is_set():
        return
    blob, nodes = dump(stack, phone, "949_now", lg, shot=True)
    warn_siwa(nodes, lg)
    low = (blob or "").lower()
    if "verify your email" in low or "verification code" in low:
        raise PhoneError("Reddit is on Verify. Will not post. Will not close.")
    need_open = True
    if already_on_feed(nodes) and find_label_exact(nodes, ("Select Container",)) is None:
        lg("Feed already up — still closing Reddit so Crane picker is clean.")
    close_reddit(stack, phone, lg)
    stage("reddit closed")
    if stop is not None and stop.is_set():
        return
    if need_open:
        open_container(stack, phone, lg, name, email)
    stage("container open")
    if stop is not None and stop.is_set():
        return
    safari_open(stack, phone, lg, safari_community_url(community))
    stage("community open")
    blob, nodes = dump(stack, phone, "950_feed", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    if find_label_exact(nodes, CREATE_LABELS) is None:
        raise PhoneError("Not on the community with Create. See 950_feed.")
    if stop is not None and stop.is_set():
        return
    nodes = tap_create(stack, phone, lg, nodes)
    stage("composer open")
    nodes = tap_image_type(stack, phone, lg, nodes)
    nodes = tap_recents_photo(stack, phone, lg, nodes)
    stage("image attached")
    nodes = type_title(stack, phone, lg, nodes, title)
    stage("title typed")
    blob, _n = dump(stack, phone, "957_flair_check", lg, shot=False)
    if "add flair" in (blob or "").lower():
        if not flair:
            raise PhoneError(
                "Composer demands flair and the Flair field is empty. "
                "Fill Flair on the Post tab. See 957_flair_check. Did not guess.")
        lg("Composer demands flair, applying %r" % flair)
    nodes = apply_flair(stack, phone, lg, nodes, flair)
    if flair:
        stage("flair applied")
    tap_submit(stack, phone, lg, nodes)
    stage("submitted")
    archive_used(image_path, name, community, lg)
    try:
        clear_local_photos(stack, lg)
    except PhoneError as e:
        lg("Wipe after Post failed: " + str(e))
    stage("wiped+archived")
    lg(f"POST_OK {name} {community}")


def main() -> int:
    argv = [a for a in sys.argv[1:] if a]
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend. Open XXTouch Elite, Remote Service on.")
        if argv and argv[0] == "--enter-community":
            if len(argv) < 2:
                print("Usage: python misc/reddit_post.py --enter-community <name>")
                return 2
            enter_community(stack, phone, log, argv[1])
            return 0
        if argv and argv[0] == "--wipe-photos":
            clear_local_photos(stack, log)
            return 0
        if argv and argv[0] == "--to-album":
            if len(argv) < 2:
                print("Usage: python misc/reddit_post.py --to-album <image.jpg>")
                return 2
            import_to_camera_roll(stack, argv[1], log)
            return 0
        if argv and argv[0] == "--attach-latest":
            blob, nodes = dump(stack, phone, "952_now", log, shot=True)
            warn_siwa(nodes, log)
            refuse_join(nodes, log)
            nodes = tap_image_type(stack, phone, log, nodes)
            tap_recents_photo(stack, phone, log, nodes)
            log("ATTACH_LATEST")
            return 0
        if len(argv) < 4:
            print("Usage: python misc/reddit_post.py <container> <community> <title> <image.jpg> [flair]")
            return 2
        name, community, title, image = argv[0], argv[1], argv[2], argv[3]
        flair = argv[4] if len(argv) > 4 else ""
        run_post(phone, stack, log, name, community, title, image, flair=flair)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
