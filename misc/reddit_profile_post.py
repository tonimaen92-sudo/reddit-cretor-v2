# UTF-8
"""Profile image post, single or multi-image. Dump-first. One Crane container.

Locked live path (proven on a0001 / u/Far-Strength9711):
  open container -> avatar (right of search Q) -> Profile row ->
  bottom-bar Create. The composer auto-targets u/<username>.
No community search. No Safari OPEN.

Multi-image: ONE wipe + batch import, then picker multi-select (circles,
row-major), badge-count verify, Next (or Add on the edit screen), title,
Post with retry on the "something went wrong" toast. Posted sources are
MOVED to repo-root used/<container>/<username>/.

Locked gotchas:
- Keyboard MUST be down before the toolbar image tap (drag to dismiss).
  OCR toolbar boxes are wrong with keyboard up; correct at the bottom (~y1270).
- Tapping the image icon with 1 already attached toasts
  "Posts can only include one attachment". Select everything in ONE picker
  pass instead.

  python misc/reddit_profile_post.py a0001 "a title" img1.jpg [img2.jpg ...]
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.request
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import XXT_LOCAL_PORT  # noqa: E402
from reddit_open import close_reddit, open_container, warn_siwa  # noqa: E402
from reddit_post import (  # noqa: E402
    archive_used,
    clear_local_photos,
    dump,
    log,
    read_image_bytes,
    refuse_join,
    type_title,
    used_dest_path,
)
from slots import get_slot, slot_reddit_ready  # noqa: E402
from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack, collapsed_text, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

LogFn = Callable[[str], None]

# Picker grid geometry (750x1334, 3 columns). Verified live.
PICKER_COL_W = 250
PICKER_GRID_TOP = 128
PICKER_ROW_H = 260
PICKER_CIRCLE_DX = 38
PICKER_CIRCLE_DY = 38


def tap_avatar(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    """Top-right avatar, anchored off the search icon.

    OCR often misreads the magnifier (Q, or a replacement char), so anchor
    off ANY small node in the search-icon zone, not exact text.
    """
    cands = [
        n
        for n in nodes
        if n.ntype != "ocr-line"
        and n.on_screen()
        and 40 <= n.y <= 140
        and n.x > 400
        and n.w < 200
    ]
    if not cands:
        raise PhoneError("Search icon zone empty on feed. See pf_feed.")
    q = min(cands, key=lambda c: c.x)
    lg(f"Search anchor {collapsed_text(q.labels())!r} ({q.x},{q.y})")
    ax = min(q.x + q.w + 90, 700)
    ay = q.center()[1]
    lg(f"TAP avatar ({ax},{ay}) anchored off search")
    stack.tap_xy(ax, ay)
    time.sleep(1.4)
    blob, nodes = dump(stack, phone, "pf_drawer", lg, shot=True)
    warn_siwa(nodes, lg)
    return nodes


def confirm_drawer_user(nodes, username: str, lg: LogFn) -> None:
    """Reuse mode only: the open container must be the expected user."""
    blob = " ".join(
        collapsed_text(n.labels())
        for n in nodes
        if n.ntype != "ocr-line" and n.on_screen()
    )
    if username.casefold() not in blob.casefold():
        raise PhoneError(
            f"Drawer is not u/{username}. Wrong container open? "
            "Run without reuse."
        )
    lg(f"Drawer confirms u/{username}.")


def tap_profile_row(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    p = find_label_exact(nodes, ("Profile",))
    if p is None:
        raise PhoneError("Profile row not in drawer. See pf_drawer.")
    lg("TAP Profile row")
    stack.tap_xy(*p.center())
    time.sleep(1.6)
    blob, nodes = dump(stack, phone, "pf_page", lg, shot=True)
    warn_siwa(nodes, lg)
    return nodes


def tap_create_bottom(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    cands = [
        n
        for n in nodes
        if n.ntype != "ocr-line"
        and n.on_screen()
        and collapsed_text(n.labels()) == "create"
    ]
    if not cands:
        raise PhoneError("Create not on profile page. See pf_page.")
    c = max(cands, key=lambda n: n.y)
    lg("TAP bottom Create")
    stack.tap_xy(*c.center())
    time.sleep(1.6)
    blob, nodes = dump(stack, phone, "pf_composer", lg, shot=True)
    warn_siwa(nodes, lg)
    refuse_join(nodes, lg)
    return nodes


def confirm_profile_composer(nodes, username: str, lg: LogFn) -> None:
    blob = " ".join(
        collapsed_text(n.labels())
        for n in nodes
        if n.ntype != "ocr-line" and n.on_screen()
    )
    if "choose a community" in blob.casefold() or "search for a community" in blob.casefold():
        raise PhoneError("Composer shows a community picker, not the profile. See dump.")
    if username.casefold() not in blob.casefold():
        raise PhoneError(f"Composer is not targeting u/{username}. See pf_composer.")
    lg(f"Composer targets u/{username}.")


def import_many(stack: TapStack, image_paths: list[str], lg: LogFn) -> int:
    """One wipe, then batch-import every file. Retry each file 3x."""
    files = [os.path.abspath(p) for p in image_paths]
    for f in files:
        if not os.path.isfile(f):
            raise PhoneError("Image file not on this PC: " + f)
    clear_local_photos(stack, lg)
    ok = 0
    for fp in files:
        raw, ext = read_image_bytes(fp)
        mime = "image/jpeg" if ext == "JPG" else "image/png"
        for attempt in (1, 2, 3):
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{XXT_LOCAL_PORT}/image_to_album",
                    data=raw,
                    headers={"Content-Type": mime},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as r:
                    body = r.read().decode("utf-8", errors="replace")
                code = int((json.loads(body) or {}).get("code", -1))
            except Exception as e:
                lg(f"IMPORT retry {attempt} {os.path.basename(fp)}: {e}")
                time.sleep(2)
                continue
            if code == 0:
                lg(f"IMPORT OK {os.path.basename(fp)}")
                ok += 1
                break
            lg(f"IMPORT code={code} {os.path.basename(fp)} attempt {attempt}")
            time.sleep(2)
        else:
            raise PhoneError(f"Could not import {fp} after 3 tries.")
    lg(f"IMPORTED {ok}/{len(files)}")
    return ok


def dismiss_keyboard(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    """Drag down on empty composer space. Toolbar drops to the bottom."""
    lg("Dismiss keyboard (drag down)")
    stack.interp_flick(375, 650, 375, 1000, steps=16, ms=22)
    time.sleep(1.0)
    blob, nodes = dump(stack, phone, "pf_composer_kbdown", lg, shot=True)
    return nodes


def keyboard_is_down(nodes) -> bool:
    letters = 0
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if len(t) == 1 and t in "qwertyasdfghjklzxcvbnm" and n.y > 900:
            letters += 1
    return letters < 8


def tap_composer_image_icon(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    """Toolbar image icon = 2nd icon (x nearest 144). Bottom layout only."""
    cands = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if not (700 <= n.y <= 1310) or not (80 <= n.x <= 200):
            continue
        cands.append(n)
    if cands:
        n = min(cands, key=lambda nd: abs(nd.x - 144))
        lg("TAP composer image icon")
        stack.tap_xy(*n.center())
        time.sleep(1.6)
        blob, nodes = dump(stack, phone, "pf_picker", lg, shot=True)
        warn_siwa(nodes, lg)
        return nodes
    if keyboard_is_down(nodes) and find_label_exact(nodes, ("Post",)) is not None:
        lg("GEOMETRY_FALLBACK image icon (172,1300), keyboard confirmed down")
        stack.tap_xy(172, 1300)
        time.sleep(1.6)
        blob, nodes = dump(stack, phone, "pf_picker", lg, shot=True)
        warn_siwa(nodes, lg)
        return nodes
    raise PhoneError("Composer image icon not in dump. See pf_composer_kbdown.")


def picker_grid_pos(i: int) -> tuple[int, int]:
    """Selection-circle center for photo i (0-based, row-major past Camera)."""
    r, c = (0, i + 1) if i < 2 else (1 + (i - 2) // 3, (i - 2) % 3)
    x = min((c + 1) * PICKER_COL_W - PICKER_CIRCLE_DX, 720)
    y = PICKER_GRID_TOP + r * PICKER_ROW_H + PICKER_CIRCLE_DY
    return (x, y)


def picker_badge_set(nodes) -> set[str]:
    """Blue number badges on selected thumbs (1, 2, ...). Excludes stray 0."""
    out: set[str] = set()
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen():
            continue
        if node.y > 1100:
            continue
        t = collapsed_text(node.labels())
        if re.fullmatch(r"[1-9]\d?", t):
            out.add(t)
    return out


def picker_badge_count(nodes) -> int:
    return len(picker_badge_set(nodes))


def picker_select_all(
    stack: TapStack, phone: Phone, lg: LogFn, nodes, count: int
):
    """Select every thumbnail, verifying EACH badge before moving on.

    Thumbnails load slowly; the per-photo retry loop doubles as the load
    wait. Tapping an already-selected photo would DESELECT it, so never
    blindly re-tap — each step proves its own badge first.
    """
    cam = find_label_exact(nodes, ("Camera",))
    if cam is None:
        raise PhoneError("Camera cell not in picker. See pf_picker.")
    if not (200 <= cam.y <= 360):
        lg(f"WARNING Camera at unexpected y={cam.y}. See pf_picker.")
    if find_label_exact(nodes, ("Next",)) is None:
        raise PhoneError("Next not on picker. See pf_picker.")
    if find_label_exact(nodes, ("Enable Access",)) is not None:
        raise PhoneError("Photo permission sheet is up. Grant access first.")
    for i in range(count):
        x, y = picker_grid_pos(i)
        want = str(i + 1)
        for attempt in (1, 2, 3):
            lg(f"SELECT photo {i + 1}/{count} ({x},{y}) try {attempt}")
            stack.tap_xy(x, y)
            time.sleep(1.2)
            blob, nodes = dump(
                stack, phone, f"pf_sel_{i + 1}_{attempt}", lg, shot=False
            )
            if want in picker_badge_set(nodes):
                break
            lg(f"badge {want} missing; waiting for grid")
            time.sleep(2.0)
        else:
            raise PhoneError(
                f"Photo {i + 1}/{count} would not select after 3 tries. "
                "See pf_sel dumps."
            )
    time.sleep(0.6)
    blob, nodes = dump(stack, phone, "pf_selected", lg, shot=True)
    badges = picker_badge_set(nodes)
    lg(f"SELECTED_BADGES {sorted(badges)} need 1..{count}")
    missing = [str(k) for k in range(1, count + 1) if str(k) not in badges]
    if missing:
        raise PhoneError(f"Missing selections {missing}. See pf_selected.")
    return nodes


def tap_next_or_add(stack: TapStack, phone: Phone, lg: LogFn, nodes):
    nxt = find_label_exact(nodes, ("Next",))
    if nxt is None:
        raise PhoneError("Next not on picker. See pf_selected.")
    lg("TAP Next")
    stack.tap_xy(*nxt.center())
    time.sleep(2.0)
    blob, nodes = dump(stack, phone, "pf_ready", lg, shot=True)
    warn_siwa(nodes, lg)
    if find_label_exact(nodes, ("Add",)) is not None and find_label_exact(
        nodes, ("Filters",)
    ) is not None:
        add = find_label_exact(nodes, ("Add",))
        lg("TAP Add (edit screen)")
        stack.tap_xy(*add.center())
        time.sleep(2.0)
        blob, nodes = dump(stack, phone, "pf_ready_add", lg, shot=True)
        warn_siwa(nodes, lg)
    return nodes


def confirm_posted(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    nodes,
    username: str,
    wait_s: int = 20,
):
    """20s settle, then prove the post landed. Raises with app left open."""
    lg(f"Settling {wait_s}s to confirm the post...")
    time.sleep(wait_s)
    blob, nodes = dump(stack, phone, "pf_confirm", lg, shot=True)
    low = (blob or "").lower()
    if "something went wrong" in low or "double-check your post" in low:
        raise PhoneError("Post rejected (toast after settle). Composer left open.")
    post_btn = find_label_exact(nodes, ("Post",))
    composer_up = (
        (post_btn is not None and post_btn.y < 160)
        or find_label_exact(nodes, ("Add tags (optional)",)) is not None
        or find_label_exact(nodes, ("Title",)) is not None
    )
    if composer_up:
        raise PhoneError("Composer still up after settle. NOT confirmed. Left open.")
    if username.casefold() not in low:
        raise PhoneError(f"u/{username} not on screen. NOT confirmed. Left open.")
    lg("POST confirmed on profile.")
    return nodes


def post_with_retry(
    stack: TapStack, phone: Phone, lg: LogFn, nodes, tries: int = 3
):
    """Tap Post. The app sometimes rejects the first tap with a toast."""
    for attempt in range(1, tries + 1):
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
            post = find_label_exact(nodes, ("Post",))
        if post is None:
            raise PhoneError("Post button not on composer. See pf_ready.")
        lg(f"TAP Post (attempt {attempt}/{tries})")
        stack.tap_xy(*post.center())
        time.sleep(3.0)
        blob, nodes = dump(stack, phone, f"pf_posted_{attempt}", lg, shot=True)
        warn_siwa(nodes, lg)
        low = (blob or "").lower()
        if "something went wrong" in low or "double-check your post" in low:
            lg(f"Post attempt {attempt} rejected by toast; retrying.")
            continue
        still = find_label_exact(nodes, ("Post",))
        if still is not None and still.y < 160:
            lg(f"Post attempt {attempt}: composer still up; retrying.")
            continue
        lg("POST accepted.")
        return nodes
    raise PhoneError("Post rejected 3x. See pf_posted_3.")


def run_profile_post(
    phone: Phone,
    stack: TapStack,
    lg: LogFn,
    name: str,
    title: str,
    image_paths: list[str] | str,
    stop: threading.Event | None = None,
    reuse_open: bool = False,
) -> None:
    if isinstance(image_paths, str):
        image_paths = [image_paths]
    name = (name or "").strip()
    title = (title or "").strip()
    image_paths = [p for p in image_paths if str(p or "").strip()]
    if not name:
        raise PhoneError("Container name is empty.")
    if not title:
        raise PhoneError("Title is empty.")
    if not image_paths:
        raise PhoneError("No images given.")
    slot = get_slot(name)
    if not slot or not slot.get("ghost"):
        raise PhoneError(f"{name} has no Ghost/Crane container.")
    if not slot.get("email"):
        raise PhoneError(f"{name} has no Hide My Email.")
    if not slot_reddit_ready(slot):
        raise PhoneError(f"{name} does not have a completed Reddit account.")
    username = str(slot.get("username") or "").strip()
    if not username:
        raise PhoneError(f"{name} has no Reddit username on file.")
    email = str(slot.get("email") or "")
    lg(f"PROFILE POST container={name} user={username} images={len(image_paths)}")
    if stop is not None and stop.is_set():
        return

    import_many(stack, image_paths, lg)
    if stop is not None and stop.is_set():
        return

    if reuse_open:
        # Already on the container's feed (live continue). No close/open.
        # Identity is verified via the drawer before anything else.
        blob, nodes = dump(stack, phone, "pf_feed", lg, shot=True)
        warn_siwa(nodes, lg)
        refuse_join(nodes, lg)
        if find_label_exact(nodes, ("Select Container",)):
            raise PhoneError("Crane picker is up; reuse impossible. Run fresh.")
        low = (blob or "").lower()
        if "verify your email" in low or "verification code" in low:
            raise PhoneError("Reddit is on Verify. Will not post. Will not close.")
        nodes = tap_avatar(stack, phone, lg, nodes)
        confirm_drawer_user(nodes, username, lg)
    else:
        close_reddit(stack, phone, lg)
        if stop is not None and stop.is_set():
            return
        open_container(stack, phone, lg, name, email)
        if stop is not None and stop.is_set():
            return

        blob, nodes = dump(stack, phone, "pf_feed", lg, shot=True)
        warn_siwa(nodes, lg)
        refuse_join(nodes, lg)
        low = (blob or "").lower()
        if "verify your email" in low or "verification code" in low:
            raise PhoneError("Reddit is on Verify. Will not post. Will not close.")

        nodes = tap_avatar(stack, phone, lg, nodes)
    nodes = tap_profile_row(stack, phone, lg, nodes)
    nodes = tap_create_bottom(stack, phone, lg, nodes)
    confirm_profile_composer(nodes, username, lg)
    if stop is not None and stop.is_set():
        return

    nodes = type_title(stack, phone, lg, nodes, title)
    nodes = dismiss_keyboard(stack, phone, lg, nodes)
    nodes = tap_composer_image_icon(stack, phone, lg, nodes)
    nodes = picker_select_all(stack, phone, lg, nodes, len(image_paths))
    nodes = tap_next_or_add(stack, phone, lg, nodes)
    post_with_retry(stack, phone, lg, nodes)
    confirm_posted(stack, phone, lg, nodes, username)

    archive_used(image_paths, name, username, lg)
    try:
        clear_local_photos(stack, lg)
    except PhoneError as e:
        lg("Wipe after Post failed: " + str(e))
    try:
        close_reddit(stack, phone, lg)
    except PhoneError as e:
        lg("Close after Post failed: " + str(e))
    lg(f"PROFILE_POST_OK {name} {len(image_paths)} images")


def main() -> int:
    argv = [a for a in sys.argv[1:] if a]
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend. Open XXTouch Elite, Remote Service on.")
        if len(argv) < 3:
            print(
                'Usage: python misc/reddit_profile_post.py [--reuse-open] '
                '<container> "title" <img1> [img2 ...]'
            )
            return 2
        reuse = False
        if argv[0] == "--reuse-open":
            reuse = True
            argv = argv[1:]
        if len(argv) < 3:
            print(
                'Usage: python misc/reddit_profile_post.py [--reuse-open] '
                '<container> "title" <img1> [img2 ...]'
            )
            return 2
        run_profile_post(phone, stack, log, argv[0], argv[1], argv[2:], reuse_open=reuse)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
