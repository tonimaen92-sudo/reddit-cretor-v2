# UTF-8
"""Read iCloud Mail over IMAP. Hide My Email forwards here. Never print passwords.

iCloud with 2FA needs an app-specific password, not the Apple ID password:
https://appleid.apple.com → Sign-In and Security → App-Specific Passwords

Use Apple's documented IMAP host imap.mail.me.com:993 (not imap.mail.icloud.com,
which Vietnam drops). App-specific password, not the Apple ID password.
"""

from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass, field
from email.header import decode_header
from email.utils import parsedate_to_datetime

CODE_RE = re.compile(r"\b(\d{6})\b")
IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993
JUNK_NAMES = ("Junk", "Junk E-mail", "Spam", "Bulk Mail")


@dataclass
class MailRow:
    uid: str
    folder: str
    date: str
    frm: str
    to: str
    subject: str
    preview: str
    body: str
    codes: list[str] = field(default_factory=list)


def _decode(val: str | None) -> str:
    if not val:
        return ""
    parts = decode_header(val)
    out: list[str] = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return " ".join(out).strip()


def _body(msg: email.message.Message) -> str:
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                raw = part.get_payload(decode=True)
                if isinstance(raw, (bytes, bytearray)):
                    chunks.append(raw.decode("utf-8", errors="replace"))
        if not chunks:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    raw = part.get_payload(decode=True)
                    if isinstance(raw, (bytes, bytearray)):
                        html = raw.decode("utf-8", errors="replace")
                        chunks.append(re.sub(r"<[^>]+>", " ", html))
    else:
        raw = msg.get_payload(decode=True)
        if isinstance(raw, (bytes, bytearray)):
            chunks.append(raw.decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def _date(msg: email.message.Message) -> str:
    raw = msg.get("Date") or ""
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return raw[:22]


def _codes(text: str) -> list[str]:
    out: list[str] = []
    for m in CODE_RE.findall(text or ""):
        if m not in out and m not in ("000000", "111111"):
            out.append(m)
    return out


def connect(user: str, password: str, timeout: int = 20) -> imaplib.IMAP4_SSL:
    user = (user or "").strip()
    password = (password or "").strip()
    if not user or not password:
        raise RuntimeError("Need Apple ID and an app-specific password.")
    candidates = [user]
    if "@" in user:
        local = user.split("@", 1)[0]
        if local and local not in candidates:
            candidates.append(local)
    last = None
    for name in candidates:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=timeout)
        try:
            mail.login(name, password)
            return mail
        except imaplib.IMAP4.error as e:
            last = e
            try:
                mail.logout()
            except Exception:
                pass
    raise RuntimeError(f"IMAP login failed on {IMAP_HOST} ({last})")


def list_folders(mail: imaplib.IMAP4_SSL) -> list[str]:
    typ, data = mail.list()
    names: list[str] = []
    if typ != "OK" or not data:
        return ["INBOX", "Junk"]
    for raw in data:
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        # last quoted token is the folder name
        m = re.findall(r'"([^"]+)"', line)
        name = m[-1] if m else line.split()[-1]
        names.append(name)
    return names or ["INBOX", "Junk"]


def resolve_junk(folders: list[str]) -> str:
    lower = {n.casefold(): n for n in folders}
    for want in JUNK_NAMES:
        if want.casefold() in lower:
            return lower[want.casefold()]
    return "Junk"


def fetch_folder(
    mail: imaplib.IMAP4_SSL,
    folder: str,
    limit: int = 40,
    query: str = "ALL",
) -> list[MailRow]:
    typ, _ = mail.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        typ, _ = mail.select(folder, readonly=True)
    if typ != "OK":
        raise RuntimeError(f"Cannot open folder {folder!r}")
    typ, data = mail.search(None, query)
    if typ != "OK" or not data or not data[0]:
        return []
    uids = data[0].split()[-limit:]
    rows: list[MailRow] = []
    for uid in reversed(uids):
        typ, payload = mail.fetch(uid, "(RFC822)")
        if typ != "OK" or not payload or not payload[0]:
            continue
        raw = payload[0][1]
        if not isinstance(raw, (bytes, bytearray)):
            continue
        msg = email.message_from_bytes(raw)
        body = _body(msg)
        subj = _decode(msg.get("Subject"))
        frm = _decode(msg.get("From"))
        to = _decode(msg.get("To"))
        blob = f"{subj}\n{frm}\n{to}\n{body}"
        preview = re.sub(r"\s+", " ", body).strip()[:140]
        rows.append(
            MailRow(
                uid=uid.decode() if isinstance(uid, bytes) else str(uid),
                folder=folder,
                date=_date(msg),
                frm=frm,
                to=to,
                subject=subj,
                preview=preview,
                body=body,
                codes=_codes(blob),
            )
        )
    return rows
