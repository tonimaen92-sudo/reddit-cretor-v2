# UTF-8
"""PC iCloud Mail viewer: Inbox / Junk / search. Passwords stay in gitignored secrets.json."""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from friend_poster.device.icloud_mail import connect, fetch_folder, list_folders, resolve_junk
from friend_poster.device.ssh_device import load_secrets, update_secrets

ICLOUD_MAIL_URL = "https://www.icloud.com/mail/"


class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("iCloud Mail (Hide My Email inbox)")
        self.root.geometry("980x640")
        self.rows = []
        self.busy = False

        sec = load_secrets()
        pad = {"padx": 8, "pady": 4}

        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True)

        ttk.Label(
            frm,
            text="Hide My Email forwards into this Apple ID inbox. Codes are From Reddit, not from the hide-my-email name. "
            "IMAP is imap.mail.me.com:993 with an app-specific password.",
            wraplength=940,
        ).pack(fill="x", **pad)

        row = ttk.Frame(frm)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="Apple ID:").pack(side="left")
        self.user = ttk.Entry(row, width=32)
        self.user.pack(side="left", padx=4)
        self.user.insert(0, str(sec.get("icloud_user") or ""))
        ttk.Label(row, text="App-specific password:").pack(side="left", padx=(12, 0))
        self.pw = ttk.Entry(row, show="*", width=22)
        self.pw.pack(side="left", padx=4)
        self.pw.insert(0, str(sec.get("icloud_app_password") or ""))
        ttk.Button(row, text="Save login", command=self.on_save).pack(side="left", padx=6)

        btns = ttk.Frame(frm)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="Open iCloud.com Mail", command=self.on_web).pack(side="left")
        ttk.Button(btns, text="Inbox", command=lambda: self.on_fetch("INBOX")).pack(
            side="left", padx=6
        )
        ttk.Button(btns, text="Junk", command=lambda: self.on_fetch("JUNK")).pack(side="left")
        ttk.Button(btns, text="Search reddit", command=self.on_search).pack(side="left", padx=6)
        self.status = tk.StringVar(value="Idle")
        ttk.Label(btns, textvariable=self.status).pack(side="left", padx=12)

        cols = ("date", "from", "subject", "codes")
        self.tree = ttk.Treeview(frm, columns=cols, show="headings", height=12)
        self.tree.heading("date", text="Date")
        self.tree.heading("from", text="From")
        self.tree.heading("subject", text="Subject")
        self.tree.heading("codes", text="6-digit")
        self.tree.column("date", width=130)
        self.tree.column("from", width=220)
        self.tree.column("subject", width=420)
        self.tree.column("codes", width=100)
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        self.body = tk.Text(frm, height=12, wrap="word")
        self.body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def on_web(self) -> None:
        webbrowser.open(ICLOUD_MAIL_URL)
        self.status.set("Opened iCloud.com Mail in your browser. Log in there. Junk is a folder on the left.")

    def on_save(self) -> None:
        update_secrets(
            icloud_user=self.user.get().strip(),
            icloud_app_password=self.pw.get().strip(),
        )
        self.status.set("Saved to secrets.json (gitignored). Not the Apple ID password.")

    def creds(self) -> tuple[str, str]:
        return self.user.get().strip(), self.pw.get().strip()

    def on_fetch(self, which: str) -> None:
        self._run(lambda: self._load(which, "ALL"))

    def on_search(self) -> None:
        self._run(lambda: self._load("INBOX", '(OR FROM "reddit" SUBJECT "reddit")'))

    def _run(self, fn) -> None:
        if self.busy:
            return
        user, pw = self.creds()
        if not user or not pw:
            messagebox.showinfo(
                "Need app-specific password",
                "IMAP cannot use your normal Apple ID password if 2FA is on.\n"
                "appleid.apple.com → Sign-In and Security → App-Specific Passwords.\n"
                "Or skip IMAP and click Open iCloud.com Mail.",
            )
            return
        self.busy = True
        self.status.set("Fetching…")

        def work() -> None:
            err = ""
            try:
                fn()
            except Exception as e:
                err = str(e)
            self.root.after(0, lambda: self._done(err))

        threading.Thread(target=work, daemon=True).start()

    def _load(self, which: str, query: str) -> None:
        user, pw = self.creds()
        mail = connect(user, pw)
        try:
            folders = list_folders(mail)
            folder = resolve_junk(folders) if which == "JUNK" else which
            self.rows = fetch_folder(mail, folder, limit=50, query=query)
            self._folder = folder
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _done(self, err: str) -> None:
        self.busy = False
        self.tree.delete(*self.tree.get_children())
        self.body.delete("1.0", "end")
        if err:
            self.status.set("IMAP failed. " + err[:220])
            return
        folder = getattr(self, "_folder", "")
        self.status.set(f"{folder}: {len(self.rows)} messages")
        for i, r in enumerate(self.rows):
            self.tree.insert(
                "",
                "end",
                iid=str(i),
                values=(r.date, r.frm[:48], r.subject[:80], " ".join(r.codes)),
            )

    def on_select(self, _evt=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        r = self.rows[int(sel[0])]
        self.body.delete("1.0", "end")
        codes = ("CODES: " + " ".join(r.codes) + "\n\n") if r.codes else ""
        self.body.insert(
            "1.0",
            f"{codes}Folder: {r.folder}\nFrom: {r.frm}\nTo: {r.to}\nSubject: {r.subject}\n\n{r.body}",
        )

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
