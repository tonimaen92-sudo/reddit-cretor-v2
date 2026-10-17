# UTF-8
"""Shared farm stats readers (commenter + poster). No GUI. No phone.

Day buckets are NYC (Today resets at NYC midnight), matching the
commenter Stats tab. Sources:
- logs/commented_posts.json (comment marks = sends)
- logs/poster_queue.json (post queue items, status posted = sends)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "logs")


def _nyc_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return timezone.utc


def nyc_today() -> str:
    try:
        return datetime.now(_nyc_tz()).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def day_of(ts) -> str:
    """NYC day bucket for epoch seconds or ISO string. '' when unparseable."""
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts), tz=_nyc_tz()
                                          ).strftime("%Y-%m-%d")
        s = str(ts or "").strip()
        if not s:
            return ""
        try:
            num = float(s)
            return datetime.fromtimestamp(num, tz=_nyc_tz()
                                          ).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_nyc_tz()).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def load_marks_posts() -> dict:
    try:
        d = json.load(open(os.path.join(LOG_DIR, "commented_posts.json"),
                           encoding="utf-8"))
        posts = (d or {}).get("posts") if isinstance(d, dict) else None
        return posts if isinstance(posts, dict) else {}
    except (OSError, ValueError):
        return {}


def load_poster_items() -> list:
    try:
        d = json.load(open(os.path.join(LOG_DIR, "poster_queue.json"),
                           encoding="utf-8"))
        items = (d or {}).get("items") if isinstance(d, dict) else []
        return [i for i in items if isinstance(i, dict)]
    except (OSError, ValueError):
        return []


def _bucket(rows: list) -> dict:
    """{container: {"total": n, "by_day": {day: n}, "last": row}}."""
    out: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("container") or "").strip() or "(none)"
        day = day_of(row.get("posted_ts", row.get("ts", row.get("when"))))
        st = out.setdefault(cid, {"total": 0, "by_day": {}, "last": None})
        st["total"] += 1
        if day:
            st["by_day"][day] = st["by_day"].get(day, 0) + 1
        st["last"] = row
    return out


def stats_from_marks(posts: dict) -> dict:
    rows = [r for r in (posts or {}).values() if isinstance(r, dict)]
    return _bucket(rows)


def stats_from_poster(items: list) -> dict:
    rows = [i for i in (items or [])
            if str(i.get("status") or "") == "posted"]
    return _bucket(rows)


def failed_poster_today(items: list) -> dict:
    """Failed post sends per slot for today (NYC): {slot: [notes]}."""
    today = nyc_today()
    out: dict = {}
    for i in items or []:
        if not isinstance(i, dict) or str(i.get("status") or "") != "failed":
            continue
        if day_of(i.get("failed_ts")) != today:
            continue
        sid = str(i.get("container") or "").strip().lower() or "(none)"
        out.setdefault(sid, []).append(str(i.get("note") or "failed"))
    return out


FAIL_SIGNALS = (
    "error", "timeout", "fail", "missing", "exception", "refused",
    "stuck", "unverified", "abort", "over budget", "mismatch", "absent",
    "stopped",
)


def failed_today(rows: list) -> dict:
    """Error failures per slot for today (NYC): {slot: [reasons]}.

    Counts run rows with abort verdicts, or skips whose reason carries an
    error signal. Benign skips (no candidate, logout skips) never count.
    Batch summary / alert rows never count. Pure."""
    today = nyc_today()
    out: dict = {}
    for r in rows or []:
        if not isinstance(r, dict) or "batch" in r or "alert" in r:
            continue
        verdict = str(r.get("verdict") or "")
        reason = str(r.get("reason") or "")
        low = reason.lower()
        if verdict == "abort" or (verdict == "skip"
                                  and any(s in low for s in FAIL_SIGNALS)):
            if day_of(r.get("ts")) != today:
                continue
            sid = str(r.get("slot") or "").strip().lower() or "(none)"
            out.setdefault(sid, []).append(reason or verdict)
    return out


def today_summary() -> dict:
    """Headline counts for today (NYC): comments + posts, with slot lists."""
    today = nyc_today()
    comments = stats_from_marks(load_marks_posts())
    posts = stats_from_poster(load_poster_items())
    c_slots = sorted(s for s, st in comments.items()
                     if st.get("by_day", {}).get(today, 0))
    p_slots = sorted(s for s, st in posts.items()
                     if st.get("by_day", {}).get(today, 0))
    return {
        "day": today,
        "comments": sum(comments[s]["by_day"][today] for s in c_slots),
        "comment_slots": c_slots,
        "posts": sum(posts[s]["by_day"][today] for s in p_slots),
        "post_slots": p_slots,
    }
