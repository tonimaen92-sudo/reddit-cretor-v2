# UTF-8
"""Safari bookmark auto-push: every FIRE stages its own permalink first.

Spike lesson: bookmark rows must exist before the tap (Ben10 id14 was
hand-pushed; later posts aborted with "bookmark entry missing" for exactly
this). ensure_bookmark pulls the live trio read-only, inserts locally on
PC (sqlite3 lives here, not on the phone), checkpoints, and pushes back
only when the device WAL is byte-identical. Zero URL typing, zero sends.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import time

from ssh_device import PhoneError
from ui_tree import collapsed_text, dump_named

REMOTE_DB = "/var/mobile/Library/Safari/Bookmarks.db"
BAR_PARENT = 1
FARM_WORD = "farm"
FARM_PREFIX = "FARM "

_WORD = re.compile(r"[A-Za-z0-9']+")


def _recount(con) -> None:
    """num_children drives how many rows Safari renders. Raw SQL bypasses
    Safari's bookkeeping, so a stale count hides fresh rows (proven: count 1
    rendered only the first row). Recompute every folder from actual children.
    """
    con.execute("UPDATE bookmarks SET num_children = "
                "(SELECT COUNT(*) FROM bookmarks AS b WHERE b.parent = bookmarks.id)"
                " WHERE type = 1")
    con.commit()


def entry_words(title: str) -> list[str]:
    return _WORD.findall((title or "").lower())


def build_modified(db_path: str, title: str, url: str) -> tuple[int, bool]:
    """Stage one permalink as the ONLY Bar leaf (single pick every run).

    Inserts (dedup by URL), prunes every other parent=1 leaf, recounts.
    Returns (row id, True) when anything was written.
    """
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT id FROM bookmarks WHERE url = ?", (url,)).fetchone()
        if row:
            nid, inserted = int(row[0]), False
        else:
            top = con.execute(
                "SELECT COALESCE(MAX(order_index), -1) FROM bookmarks WHERE parent = ?",
                (BAR_PARENT,)).fetchone()[0]
            cur = con.execute(
                "INSERT INTO bookmarks (parent, type, title, url, order_index, syncable,"
                " locally_added, editable, deletable, hidden, hidden_ancestor_count)"
                " VALUES (1, 0, ?, ?, ?, 1, 0, 1, 1, 0, 0)",
                (title, url, int(top) + 1))
            nid, inserted = int(cur.lastrowid), True
            for i, w in enumerate(entry_words(title)):
                con.execute(
                    "INSERT INTO bookmark_title_words (bookmark_id, word, word_index)"
                    " VALUES (?, ?, ?)", (nid, w, i))
        others = [r[0] for r in con.execute(
            "SELECT id FROM bookmarks WHERE parent = ? AND type = 0 AND id != ?",
            (BAR_PARENT, nid))]
        for bid in others:
            con.execute("DELETE FROM bookmark_title_words WHERE bookmark_id = ?", (bid,))
        cur = con.execute(
            "DELETE FROM bookmarks WHERE parent = ? AND type = 0 AND id != ?",
            (BAR_PARENT, nid))
        pruned = int(cur.rowcount)
        con.commit()
        _recount(con)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.commit()
        return nid, (inserted or pruned > 0)
    finally:
        con.close()


def safari_card(nodes):
    """Safari recents card. y<600 only: the SpringBoard icon label (~y1074)
    must never match or we flick the home screen."""
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        if collapsed_text(n.labels()) == "safari" and 40 < n.y < 600:
            return n
    return None


def close_safari_app(stack, phone, log) -> None:
    """Fully close Safari via recents so the next open re-reads Bookmarks.db.

    A backgrounded Safari holds the old DB snapshot and will never render
    a freshly pushed row. Non-fatal: logs and continues either way.
    """
    try:
        stack.press_home()
        time.sleep(0.7)
        stack.double_home(280)
        time.sleep(0.8)
        for i in range(3):
            blob, nodes = dump_named(stack, phone, "96close_%d" % i, log, shot=False)
            card = safari_card(nodes)
            if card is None:
                log("Safari not in recents.")
                break
            stack.interp_flick(375, 780, 375, 60, steps=12, ms=18)
            time.sleep(0.8)
        stack.press_home()
    except Exception as e:
        log("Safari close skipped: %s" % str(e)[:100])


def wipe_leaves_sql(con) -> int:
    """Delete every leaf bookmark, keep folders (Root/Bar/ReadingList/Freq).

    Returns deleted leaf count. Cascades title_words manually (only DELETE
    triggers exist). Folders (type=1) and sync metadata untouched.
    """
    con.execute("DELETE FROM bookmark_title_words WHERE bookmark_id IN "
                "(SELECT id FROM bookmarks WHERE type = 0)")
    cur = con.execute("DELETE FROM bookmarks WHERE type = 0")
    con.commit()
    _recount(con)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.commit()
    return int(cur.rowcount)


def wipe_bookmarks(phone, log) -> int:
    """Backup trio, wipe all leaves, push back on unchanged WAL."""
    import datetime
    t0 = time.monotonic()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(tempfile.gettempdir(), "bm_wipe_" + stamp)
    os.makedirs(bdir, exist_ok=True)
    code, out = phone.exec_ok(
        "md5sum '%s' '%s-wal' 2>/dev/null; ls -l '%s'" % (REMOTE_DB, REMOTE_DB, REMOTE_DB),
        timeout=15)
    dev_db = _parse_md5(out, "Bookmarks.db")
    dev_wal = _parse_md5(out, "Bookmarks.db-wal")
    if not dev_db:
        raise PhoneError("bookmark DB unreadable on device")
    trio = {}
    for name in ("Bookmarks.db", "Bookmarks.db-wal", "Bookmarks.db-shm"):
        try:
            data = phone.sftp_read_bytes(REMOTE_DB + name[len("Bookmarks.db"):])
            trio[name] = data
            with open(os.path.join(bdir, name), "wb") as f:
                f.write(data)
        except Exception:
            pass
    log("WIPE backup %s (%s)" % (bdir, ", ".join(
        "%s=%dB" % (k, len(v)) for k, v in sorted(trio.items()))))
    local_db = os.path.join(tempfile.mkdtemp(prefix="bm_wipe_"), "Bookmarks.db")
    with open(local_db, "wb") as f:
        f.write(trio["Bookmarks.db"])
    if "Bookmarks.db-wal" in trio:
        with open(os.path.join(os.path.dirname(local_db), "Bookmarks.db-wal"), "wb") as f:
            f.write(trio["Bookmarks.db-wal"])
    con = sqlite3.connect(local_db)
    try:
        folders = con.execute("SELECT COUNT(*) FROM bookmarks WHERE type = 1").fetchone()[0]
        n = wipe_leaves_sql(con)
        left = con.execute("SELECT COUNT(*) FROM bookmarks WHERE type = 0").fetchone()[0]
        words = con.execute("SELECT COUNT(*) FROM bookmark_title_words").fetchone()[0]
    finally:
        con.close()
    if left != 0 or folders < 4:
        raise PhoneError("wipe unsafe result (leaves=%d folders=%d), nothing pushed" % (left, folders))
    code, out2 = phone.exec_ok("md5sum '%s' '%s-wal' 2>/dev/null" % (REMOTE_DB, REMOTE_DB),
                               timeout=15)
    if _parse_md5(out2, "Bookmarks.db") != dev_db or _parse_md5(out2, "Bookmarks.db-wal") != dev_wal:
        raise PhoneError("bookmark WAL changed mid-wipe, nothing written")
    with open(local_db, "rb") as f:
        raw = f.read()
    phone.sftp_write_bytes(REMOTE_DB, raw)
    code, out3 = phone.exec_ok(
        "chmod 644 '%s'; rm -f '%s-wal' '%s-shm'; md5sum '%s'" % (
            REMOTE_DB, REMOTE_DB, REMOTE_DB, REMOTE_DB), timeout=20)
    if hashlib.md5(raw).hexdigest() not in (out3 or ""):
        raise PhoneError("wipe push verify failed")
    log("WIPE pushed: %d leaves deleted, %d folders kept, title_words left=%d (T+%.1fs)"
        % (n, folders, words, time.monotonic() - t0))
    return n


def delete_url_sql(con, url: str) -> int:
    """Delete one bookmark by exact URL + its title words. Returns rows."""
    ids = [r[0] for r in con.execute("SELECT id FROM bookmarks WHERE url = ?", (url,))]
    for bid in ids:
        con.execute("DELETE FROM bookmark_title_words WHERE bookmark_id = ?", (bid,))
    cur = con.execute("DELETE FROM bookmarks WHERE url = ?", (url,))
    con.commit()
    _recount(con)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.commit()
    return int(cur.rowcount)


def delete_bookmark(phone, log, url: str) -> int:
    """Remove one permalink row (test cleanup). WAL-gated push like the rest."""
    t0 = time.monotonic()
    code, out = phone.exec_ok(
        "md5sum '%s' '%s-wal' 2>/dev/null" % (REMOTE_DB, REMOTE_DB), timeout=15)
    dev_db = _parse_md5(out, "Bookmarks.db")
    dev_wal = _parse_md5(out, "Bookmarks.db-wal")
    if not dev_db:
        raise PhoneError("bookmark DB unreadable on device")
    tmp = tempfile.mkdtemp(prefix="bm_del_")
    try:
        local_db = os.path.join(tmp, "Bookmarks.db")
        with open(local_db, "wb") as f:
            f.write(phone.sftp_read_bytes(REMOTE_DB))
        if dev_wal:
            with open(os.path.join(tmp, "Bookmarks.db-wal"), "wb") as f:
                f.write(phone.sftp_read_bytes(REMOTE_DB + "-wal"))
        con = sqlite3.connect(local_db)
        try:
            n = delete_url_sql(con, url)
        finally:
            con.close()
        if n == 0:
            log("DELETE bookmark absent already (zero writes)")
            return 0
        code, out2 = phone.exec_ok("md5sum '%s' '%s-wal' 2>/dev/null" % (REMOTE_DB, REMOTE_DB),
                                   timeout=15)
        if _parse_md5(out2, "Bookmarks.db") != dev_db or _parse_md5(out2, "Bookmarks.db-wal") != dev_wal:
            raise PhoneError("bookmark WAL changed mid-delete, nothing written")
        with open(local_db, "rb") as f:
            raw = f.read()
        phone.sftp_write_bytes(REMOTE_DB, raw)
        code, out3 = phone.exec_ok(
            "chmod 644 '%s'; rm -f '%s-wal' '%s-shm'; md5sum '%s'" % (
                REMOTE_DB, REMOTE_DB, REMOTE_DB, REMOTE_DB), timeout=20)
        if hashlib.md5(raw).hexdigest() not in (out3 or ""):
            raise PhoneError("bookmark delete verify failed")
        log("DELETE pushed (%d row, T+%.1fs)" % (n, time.monotonic() - t0))
        return n
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _parse_md5(out: str, suffix: str) -> str:
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].endswith(suffix) and len(parts[0]) == 32:
            return parts[0]
    return ""


def ensure_bookmark(phone, log, title: str, url: str) -> tuple[int, bool]:
    """Stage one permalink in BookmarksBar. Returns (row id, pushed?)."""
    row_title = title if title.startswith(FARM_PREFIX) else FARM_PREFIX + title
    t0 = time.monotonic()
    code, out = phone.exec_ok(
        "md5sum '%s' '%s-wal' 2>/dev/null; ls -l '%s'" % (REMOTE_DB, REMOTE_DB, REMOTE_DB),
        timeout=15)
    dev_db = _parse_md5(out, "Bookmarks.db")
    dev_wal = _parse_md5(out, "Bookmarks.db-wal")
    if not dev_db:
        raise PhoneError("bookmark DB unreadable on device")
    tmp = tempfile.mkdtemp(prefix="bm_push_")
    try:
        local_db = os.path.join(tmp, "Bookmarks.db")
        with open(local_db, "wb") as f:
            f.write(phone.sftp_read_bytes(REMOTE_DB))
        if dev_wal:
            with open(os.path.join(tmp, "Bookmarks.db-wal"), "wb") as f:
                f.write(phone.sftp_read_bytes(REMOTE_DB + "-wal"))
        nid, changed = build_modified(local_db, row_title, url)
        if not changed:
            con = sqlite3.connect(local_db)
            try:
                before = dict(con.execute(
                    "SELECT id, num_children FROM bookmarks WHERE type = 1").fetchall())
                _recount(con)
                after = dict(con.execute(
                    "SELECT id, num_children FROM bookmarks WHERE type = 1").fetchall())
                fixed = before != after
            finally:
                con.close()
            if not fixed:
                log("BOOKMARK staged id=%d (already present, zero writes)" % nid)
                return nid, False
            log("BOOKMARK staged id=%d (counts repaired, pushing)" % nid)
        code, out2 = phone.exec_ok("md5sum '%s' '%s-wal' 2>/dev/null" % (REMOTE_DB, REMOTE_DB),
                                   timeout=15)
        if _parse_md5(out2, "Bookmarks.db") != dev_db or _parse_md5(out2, "Bookmarks.db-wal") != dev_wal:
            raise PhoneError("bookmark WAL changed mid-push, nothing written")
        with open(local_db, "rb") as f:
            raw = f.read()
        phone.sftp_write_bytes(REMOTE_DB, raw)
        code, out3 = phone.exec_ok(
            "chmod 644 '%s'; rm -f '%s-wal' '%s-shm'; md5sum '%s'" % (
                REMOTE_DB, REMOTE_DB, REMOTE_DB, REMOTE_DB), timeout=20)
        if hashlib.md5(raw).hexdigest() not in (out3 or ""):
            raise PhoneError("bookmark push verify failed")
        log("BOOKMARK pushed id=%d (%dB, T+%.1fs)" % (nid, len(raw), time.monotonic() - t0))
        _verify_device_row(phone, log, url)
        return nid, True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _verify_device_row(phone, log, url: str) -> int:
    """Re-pull and prove the row is really on the device (push vs render).

    Also proves single-pick: exactly one Bar leaf, or the tap can hit stale.
    """
    tmp = tempfile.mkdtemp(prefix="bm_verify_")
    try:
        with open(os.path.join(tmp, "Bookmarks.db"), "wb") as f:
            f.write(phone.sftp_read_bytes(REMOTE_DB))
        try:
            with open(os.path.join(tmp, "Bookmarks.db-wal"), "wb") as f:
                f.write(phone.sftp_read_bytes(REMOTE_DB + "-wal"))
        except Exception:
            pass
        con = sqlite3.connect(os.path.join(tmp, "Bookmarks.db"))
        try:
            row = con.execute("SELECT id FROM bookmarks WHERE url = ?", (url,)).fetchone()
            n = con.execute("SELECT COUNT(*) FROM bookmarks WHERE parent = 1 AND type = 0").fetchone()[0]
        finally:
            con.close()
        if not row:
            raise PhoneError("bookmark push did not stick (row absent on re-pull)")
        if int(n) != 1:
            raise PhoneError("Bar holds %d rows, expected exactly 1" % int(n))
        log("DEVICE_ROW id=%d confirmed (single pick)" % int(row[0]))
        return int(row[0])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
