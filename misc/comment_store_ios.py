# UTF-8
"""Per-container used-post lock for the iPhone farm (Crane slot).

Mark is per (container, sub, title): one container's send never blocks
another container. Global open-protection is separate: recently_taken()
reports any container's fresh mark (default 24h) so two containers never
pile onto the same post back-to-back. Pre-switch legacy rows (keyed
sub+title, no container prefix) keep binding their own container.

  python misc/comment_store_ios.py check fivenightsatfreddys "Arts and crafts with Sun!!" a0003
  python misc/comment_store_ios.py mark  fivenightsatfreddys "Arts and crafts with Sun!!" a0003 reply
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import LOG_DIR  # noqa: E402

USED_PATH = os.path.join(LOG_DIR, "commented_posts.json")
LOCK_PATH = USED_PATH + ".lock"
_THREAD = threading.Lock()

try:
    import msvcrt
except ImportError:
    msvcrt = None

_TITLE_STRIP = re.compile(r"[^\w\s]+", re.UNICODE)


def post_key(sub, title):
    sub_n = (sub or "").strip().lower()
    title_n = _TITLE_STRIP.sub("", re.sub(r"\s+", " ", (title or "").strip().lower())).strip()
    title_n = title_n[:80]
    if not sub_n or len(title_n) < 8:
        return ""
    return f"{sub_n}|{title_n}"


def slot_key(container, sub, title):
    """Per-container mark key. '' when any part is missing."""
    base = post_key(sub, title)
    cid = (container or "").strip().lower()
    if not base or not cid:
        return ""
    return f"{cid}|{base}"


def _valid_row(row) -> bool:
    return isinstance(row, dict) and str(row.get("status") or "") in ("commented", "claimed")


def _read():
    try:
        if os.path.isfile(USED_PATH):
            with open(USED_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("posts"), dict):
                return data
    except Exception:
        pass
    return {"posts": {}}


def _write(data):
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = USED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, USED_PATH)


def _with_lock(fn):
    with _THREAD:
        if msvcrt is None:
            return fn()
        lock_fd = None
        locked = False
        try:
            lock_fd = open(LOCK_PATH, "a+b")
            start = time.time()
            while time.time() - start < 8:
                try:
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.05)
            return fn()
        finally:
            if lock_fd is not None:
                if locked:
                    try:
                        lock_fd.seek(0)
                        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                try:
                    lock_fd.close()
                except Exception:
                    pass


def already_commented(sub, title, container=None):
    """Per-container send lock. container given: True only when THAT slot
    marked it (legacy rows bind their own recorded container). container
    None: legacy global check (any slot)."""
    key = post_key(sub, title)
    if not key:
        return True
    cid = (container or "").strip().lower() if container else ""

    def _check():
        posts = _read().get("posts") or {}
        if cid:
            if _valid_row(posts.get(f"{cid}|{key}")):
                return True
            legacy = posts.get(key)
            return bool(_valid_row(legacy)
                        and str(legacy.get("container") or "").strip().lower() == cid)
        return any(_valid_row(posts.get(k))
                   for k in posts if k == key or k.endswith("|" + key))

    return bool(_with_lock(_check))


def recently_taken(sub, title, within_hours: float = 24.0) -> bool:
    """Global open-protection: any slot's fresh mark inside the window.
    Keeps two containers off the same post back-to-back."""
    key = post_key(sub, title)
    if not key:
        return False
    try:
        cutoff = time.time() - float(within_hours) * 3600.0
    except (TypeError, ValueError):
        return False

    def _check():
        posts = _read().get("posts") or {}
        for k, row in posts.items():
            if not (k == key or k.endswith("|" + key)):
                continue
            if not _valid_row(row):
                continue
            try:
                if float(row.get("ts") or 0) >= cutoff:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    return bool(_with_lock(_check))


def mark_commented(sub, title, container="", kind="reply"):
    key = slot_key(container, sub, title) or post_key(sub, title)
    if not key:
        return ""

    def _mark():
        data = _read()
        posts = data.setdefault("posts", {})
        posts[key] = {
            "sub": (sub or "").strip(),
            "title": (title or "").strip()[:160],
            "kind": kind,
            "status": "commented",
            "container": str(container or ""),
            "ts": time.time(),
            "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _write(data)

    _with_lock(_mark)
    return key


def main() -> int:
    argv = [a for a in sys.argv[1:] if a]
    if len(argv) < 3 or argv[0] not in ("check", "mark"):
        print(__doc__)
        return 2
    action, sub, title = argv[0], argv[1], argv[2]
    if action == "check":
        slot = argv[3] if len(argv) > 3 else None
        print("USED" if already_commented(sub, title, slot) else "FREE")
        return 0
    container = argv[3] if len(argv) > 3 else ""
    kind = argv[4] if len(argv) > 4 else "reply"
    key = mark_commented(sub, title, container, kind)
    print("MARKED", key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
