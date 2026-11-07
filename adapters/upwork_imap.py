"""IMAP fetch layer: pulls raw bytes of Upwork notification emails, nothing else.

Parsing lives one layer up — writing a parser before seeing real mail is guessing. This layer uses `--pull-mail`
to dump samples locally; field extraction came after inspecting the real HTML.

Why any-host support: Gmail forces app-specific passwords, which require 2FA first,
and Google's risk controls block that flow when accessed through proxies from China ("couldn't verify it's you", no fix).
The working path: Gmail auto-forward → a domestic mailbox (163/QQ) → IMAP read here.

Why no server-side SEARCH: 163's IMAP returns OK but empty results for FROM/SUBJECT/HEADER searches
(no error, just nothing). So: fetch headers by date → filter sender locally → fetch full body only for hits.
That makes server behavior differences irrelevant.
"""
from __future__ import annotations

import email
import imaplib
from datetime import date, timedelta
from email.header import decode_header, make_header

DEFAULT_HOST = "imap.gmail.com"

# folders with these semantics are skipped: drafts/sent/trash
SKIP_FLAGS = ("\\Sent", "\\Drafts", "\\Trash", "\\All")


class MailAuthError(RuntimeError):
    """Login refused. Most common cause: using the mailbox login password instead of the auth code / app-specific password."""


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _send_client_id(conn: imaplib.IMAP4_SSL) -> None:
    """QQ/163/126 require the client to identify itself first (RFC 2971 ID), otherwise they reply "Unsafe Login"."""
    try:
        conn.xatom(
            "ID",
            '("name" "job-radar" "version" "0.1" "vendor" "python-imaplib" "contact" "local")',
        )
    except Exception:
        pass  # Gmail neither supports it nor cares; ignore


def _folders_to_scan(conn: imaplib.IMAP4_SSL) -> list[str]:
    """Pick folders to scan: INBOX first, then spam; skip sent/drafts/trash.

    Folder names may be modified UTF-7 (163's spam folder is &V4NXPpCuTvY-), so parse them
    from the LIST response instead of hardcoding any name.
    """
    typ, data = conn.list()
    if typ != "OK" or not data:
        return ["INBOX"]
    inbox, junk, other = [], [], []
    for raw in data:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        flags = line[line.find("(") + 1 : line.find(")")] if "(" in line else ""
        if any(f in flags for f in SKIP_FLAGS):
            continue
        # name is the last quoted segment
        name = line.rsplit('"', 2)[-2] if line.count('"') >= 2 else line.split()[-1]
        if name.upper() == "INBOX":
            inbox.append(name)
        elif "\\Junk" in flags:
            junk.append(name)
        else:
            other.append(name)
    # Filing alerts into a folder is the natural response to "too much mail", and filing must
    # not silently starve the scraper. So: scan every custom folder, preferring ones named
    # after Upwork, capped so a mailbox with 40 folders does not cost 40 round trips per tick.
    ranked = sorted(other, key=lambda n: 0 if "upwork" in n.lower() else 1)
    return inbox + junk + ranked[:5]


def fetch_raw(
    user: str,
    password: str,
    host: str = DEFAULT_HOST,
    days: int = 14,
    sender: str = "upwork.com",
    limit: int = 300,
) -> list[dict]:
    """Returns [{uid, subject, date, raw}] in server order (oldest first)."""
    conn = imaplib.IMAP4_SSL(host)
    try:
        try:
            conn.login(user, password)
        except imaplib.IMAP4.error as exc:
            raise MailAuthError(
                f"{host} login refused: " + str(exc) + "\n"
                "  Check: (1) the password is the auth code, not the mailbox password; "
                "(2) IMAP is actually enabled in mailbox settings; (3) the auth code was copied in full."
            ) from exc

        _send_client_id(conn)

        since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
        out: list[dict] = []
        seen: set[str] = set()
        for folder in _folders_to_scan(conn):
            typ, _ = conn.select(folder, readonly=True)
            if typ != "OK":
                continue
            typ, data = conn.search(None, "SINCE", since)
            if typ != "OK":
                raise RuntimeError(f"SEARCH failed for folder {folder}: {typ} {data}")
            ids = (data[0] or b"").split()[-limit:]

            # headers only first, filter sender locally — server-side search is unreliable on 163
            keep: list[bytes] = []
            for msg_id in ids:
                typ, head = conn.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
                if typ != "OK" or not head or not isinstance(head[0], tuple):
                    continue
                hdr = head[0][1].decode("utf-8", errors="replace").lower()
                if sender.lower() in hdr:
                    keep.append(msg_id)

            for msg_id in keep:
                if msg_id in seen:
                    continue
                typ, payload = conn.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                seen.add(msg_id)
                raw = payload[0][1]
                msg = email.message_from_bytes(raw)
                out.append(
                    {
                        "uid": msg_id.decode(),
                        "subject": _decode(msg.get("Subject")),
                        "date": msg.get("Date", ""),
                        "folder": folder,
                        "raw": raw,
                    }
                )
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()
