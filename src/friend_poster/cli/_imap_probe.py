# UTF-8
"""One-shot: list Inbox + Junk via IMAP. Never print passwords."""

from __future__ import annotations

import os
import sys
from friend_poster.device.icloud_mail import connect, fetch_folder, list_folders, resolve_junk  # noqa: E402
from friend_poster.device.ssh_device import load_secrets  # noqa: E402


def main() -> int:
    sec = load_secrets()
    user = str(sec.get("icloud_user") or "").strip()
    pw = str(sec.get("icloud_app_password") or "").strip()
    if not user or not pw:
        print("ERROR Need icloud_user + icloud_app_password in secrets.json")
        return 1
    print("IMAP connecting…", flush=True)
    mail = connect(user, pw, timeout=25)
    try:
        folders = list_folders(mail)
        print("FOLDERS", ", ".join(folders[:20]), flush=True)
        junk = resolve_junk(folders)
        for name, query in (
            ("INBOX", "ALL"),
            (junk, "ALL"),
            ("INBOX", '(OR FROM "reddit" SUBJECT "reddit")'),
        ):
            try:
                rows = fetch_folder(mail, name, limit=15, query=query)
            except Exception as e:
                print(f"SKIP {name}: {e}", flush=True)
                continue
            print(f"--- {name} query={query!r} n={len(rows)} ---", flush=True)
            for r in rows[:12]:
                codes = (" | codes=" + ",".join(r.codes)) if r.codes else ""
                print(f"{r.date} | {r.frm[:50]} | {r.subject[:70]}{codes}", flush=True)
    finally:
        try:
            mail.logout()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
