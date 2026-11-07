"""Upwork alert email → normalized job record.

Mail arrives via Gmail forwarding; one job per email (multi-job digests exist too).
Parsing uses the **text/plain part**, not HTML: in practice the plain-text version is
already a clean one-field-per-line structure, while HTML is nested tables for no benefit.

Fields available (verified against real mail, 2026-11-06):
    title (may be truncated by Upwork to ~45 chars) / budget / description snippet / skills /
    client payment verification / rating / total spent / country / job link and id
Not available: proposal count, required Connects — only on the job detail page.
"""
from __future__ import annotations

import email
import hashlib
import html as ihtml
import json
import re
from datetime import timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from .base import Adapter, AdapterError

PLATFORM = "upwork"
JOB_URL = re.compile(r"https://www\.upwork\.com/jobs/(?P<pid>~[A-Za-z0-9]+)")
TITLE_LINE = re.compile(
    r"^(?P<title>.+?):\s*https://www\.upwork\.com/jobs/(?P<pid>~[A-Za-z0-9]+)"
)
MORE_URL = re.compile(r"\s*more:\s*https?://\S+\s*$", re.I)
TRAILING_DOTS = re.compile(r"(\.{3}|…)+\s*$")
MONEY = r"\$([\d,]+(?:\.\d+)?)"
SPENT = re.compile(r"\$([\d,.]+)\s*([KMB])?\s*spent", re.I)
RATING = re.compile(r"([\d.]+)\s*stars?", re.I)
HIRE_COUNT = re.compile(r"([\d,]+)\s*hire", re.I)

# --- daily digest ("New jobs that match your saved search") ---
DIGEST_SUBJECT = re.compile(r"saved search", re.I)
DIGEST_POSTED = re.compile(
    r"^Posted\s+(?:(\d+)\s*(minute|hour|day)s?\s+ago|just now|yesterday)\b", re.I
)
DIGEST_BUDGET = re.compile(r"^(fixed[- ]price|hourly)\b", re.I)
DIGEST_CLIENT = re.compile(r"^payment\s+(not\s+)?verified", re.I)
DIGEST_MORE = re.compile(r"(?:\.{3}|…)\s*more\s*$", re.I)
# The last posting runs straight into the mail footer; without a stop mark the street address
# and the copyright line end up inside the client-country field.
DIGEST_FOOTER = re.compile(
    r"^(view all alerts|mobile app|follow us|privacy policy|©|\d+\s+lytton|"
    r"as a freelancer plus|this alert was sent)", re.I
)


def _text_of(msg: email.message.Message) -> str:
    """Prefer text/plain; fall back to crudely stripped HTML text."""
    plain, markup = None, None
    for part in msg.walk():
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ct == "text/plain" and plain is None:
            plain = body
        elif ct == "text/html" and markup is None:
            markup = body
    if plain:
        return plain
    if markup:
        text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", markup, flags=re.I)
        text = re.sub(r"<[^>]+>", "\n", text)
        return ihtml.unescape(text)
    return ""


def _decoded(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _money(raw: str) -> float:
    return float(raw.replace(",", ""))


def _spent_value(num: str, unit: str | None) -> float:
    mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get((unit or "").upper(), 1)
    return _money(num) * mult


def _clean_title(raw: str) -> str:
    return TRAILING_DOTS.sub("", raw.strip()).strip()


BARE_RATING = re.compile(r"(?<![\d.])([0-5]\.\d{1,2})(?![\d.])")


def _parse_client(line: str) -> dict:
    """Client facts, in two shapes:
       single-job alert : `Payment verified · 4.98 stars · $78K spent · United States`
       daily digest     : `Payment verified • 5.0 • $3k spent • Canada`  (no "stars" word)
    """
    out: dict = {"payment_verified": None, "client_rating": None,
                 "client_spent": None, "client_country": None}
    if re.search(r"payment\s+verified", line, re.I):
        out["payment_verified"] = 1
    elif re.search(r"payment\s+(not\s+verified|unverified)", line, re.I):
        out["payment_verified"] = 0
    if (m := RATING.search(line)):
        out["client_rating"] = float(m.group(1))
    elif (m := BARE_RATING.search(line)):
        out["client_rating"] = float(m.group(1))
    if (m := SPENT.search(line)):
        out["client_spent"] = _spent_value(m.group(1), m.group(2))
    if (m := HIRE_COUNT.search(line)):
        out["client_hires"] = int(m.group(1).replace(",", ""))
    # country = whatever token remains after removing known fragments
    rest = SPENT.sub("", line)
    rest = RATING.sub("", rest)
    rest = HIRE_COUNT.sub("", rest)
    rest = re.sub(r"payment\s+(verified|not\s+verified|unverified)", "", rest, flags=re.I)
    rest = BARE_RATING.sub("", rest) if out["client_rating"] is not None else rest
    parts = [p.strip(" ·•|,\t") for p in re.split(r"[·|•]", rest)]
    for p in parts:
        p = p.strip(" ·•")
        if p and not re.fullmatch(r"[\d.,$%KMkm ]+", p) and len(p) < 40:
            out["client_country"] = p
    return out


def _parse_budget(line: str) -> dict:
    """Two shapes in the wild:
       single-job alert : `Hourly: $25.00 - $47.00 · Est. time: ... · Posted Fri, Nov 06`
       daily digest     : `Fixed-price • $30`  /  `Hourly • $25.00 - $47.00`
    Separator between the label and the amount varies, so allow a few non-$ chars."""
    out = {"budget_type": None, "budget_min": None, "budget_max": None, "currency": "USD"}
    m = re.search(r"fixed[- ]price\b[^$]{0,12}\$([\d,]+(?:\.\d+)?)", line, re.I)
    if m:
        out["budget_type"] = "fixed"
        out["budget_min"] = out["budget_max"] = _money(m.group(1))
        return out
    m = re.search(r"hourly\b[^$]{0,12}\$([\d,]+(?:\.\d+)?)\s*(?:-\s*\$?([\d,]+(?:\.\d+)?))?", line, re.I)
    if m:
        out["budget_type"] = "hourly"
        out["budget_min"] = _money(m.group(1))
        out["budget_max"] = _money(m.group(2)) if m.group(2) else out["budget_min"]
    return out


def _skill_name(line: str) -> str:
    return line.split(":")[0].strip()


def _title_match(line: str):
    """Multiple links in one email point at the same job: the trailing `more:` in the
    description and the bottom `View job details`. Only the first is the real title line; the rest are noise."""
    m = TITLE_LINE.match(line)
    if not m:
        return None
    title = _clean_title(m.group("title"))
    low = title.lower()
    if low.startswith("view job details") or MORE_URL.search(line):
        return None
    if not 3 <= len(title) <= 120:
        return None
    return m


def parse_body(text: str, *, posted_at: str | None, subject: str) -> list[dict]:
    """Split one email body into job blocks and parse each. Multi-job digests use the same path."""
    lines = text.splitlines()
    starts = [(i, m) for i, ln in enumerate(lines) if (m := _title_match(ln.strip()))]

    jobs: list[dict] = []
    for n, (idx, match) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        block = lines[idx + 1 : end]
        job = {
            "platform": PLATFORM,
            "platform_id": match.group("pid"),
            "title": _clean_title(match.group("title")),
            "company": None,
            "url": f"https://www.upwork.com/jobs/{match.group('pid')}",
            "description": "",
            "description_source": "snippet",  # always a truncated snippet in email
            "posted_at": posted_at,
            "budget_type": None,
            "budget_min": None,
            "budget_max": None,
            "currency": "USD",
            "skills": [],
            "client_country": None,
            "client_rating": None,
            "client_spent": None,
            "client_hires": None,
            "payment_verified": None,
            "proposals": None,
            "required_connects": None,
            "subject": subject,
        }
        in_skills = False
        for raw in block:
            s = raw.strip()
            if not s:
                in_skills = False  # blank line ends the skills block
                continue
            low = s.lower()
            if low.startswith(("view job details", "you received this email", "----------")):
                continue
            if re.match(r"^(hourly|fixed[- ]price):", s, re.I):
                job.update(_parse_budget(s))
                continue
            # parse the client line before the skills lines: when the skills list ends without "+N more",
            # the client line follows immediately and would be swallowed as a skill name
            if re.search(r"payment\s+(verified|not\s+verified|unverified)", low) or SPENT.search(s):
                job.update(_parse_client(s))
                in_skills = False
                continue
            if low.startswith("skills:"):
                in_skills = True
                rest = s.split(":", 1)[1].strip()
                if rest:
                    job["skills"].append(_skill_name(rest))
                continue
            if in_skills:
                if s.startswith("+") or low == "more":
                    in_skills = False
                else:
                    name = _skill_name(s)
                    if name:
                        job["skills"].append(name)
                    continue
            if not job["description"]:
                desc = MORE_URL.sub("", s)
                desc = re.sub(r"\s*more\s*$", "", desc, flags=re.I)
                job["description"] = TRAILING_DOTS.sub("", desc).strip()
        jobs.append(job)
    return jobs


def _html_of(msg: email.message.Message) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def _html_lines(markup: str) -> list[str]:
    """HTML → one element per visual line, noise removed.

    The digest mail is table layout, so `</td>`/`</tr>` are the real line breaks.
    Upwork pads the message with U+034F / U+00AD filler for preview-text suppression;
    those lines are dropped, and a line that is just a bullet is glued onto the next one.
    """
    t = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", markup, flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(?:p|div|td|tr|th|h\d|li|table)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = ihtml.unescape(t)
    t = t.replace("\u034f", "").replace("\u00ad", "").replace("\u200b", "")
    keep: list[str] = []
    for ln in t.splitlines():
        ln = re.sub(r"[ \t\u00a0]+", " ", ln).strip()
        if not ln or not ln.strip("\u034f\u00ad\u200b ·"):
            continue
        if ln == "•":
            continue
        if ln.startswith("• "):
            # bullet glued to the previous line so `_parse_client` sees one string
            if keep:
                keep[-1] = keep[-1] + " • " + ln[2:].strip()
            else:
                keep.append(ln[2:].strip())
            continue
        keep.append(ln)
    return keep


def _ago_offset(line: str) -> float:
    """`Posted 6 hours ago` → 6.0 (hours). Used to turn mail arrival time into post time."""
    m = DIGEST_POSTED.match(line)
    if not m:
        return 0.0
    if m.group(1) is None:
        return 24.0 if re.search(r"yesterday", line, re.I) else 0.0
    n = float(m.group(1))
    unit = m.group(2).lower()
    return {"minute": n / 60.0, "hour": n, "day": n * 24.0}[unit]


def _join_desc(parts: list[str]) -> str:
    text = " ".join(parts)
    text = re.sub(r"\s*(?:\.{3}|…)\s*more\s*$", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_skill(line: str) -> bool:
    return len(line) <= 45 and not line.endswith((".", "?", "!"))


def _skills_start(body: list[str], i_desc: int, i_client: int) -> int:
    """Where the description stops.

    Normally the snippet ends with a `... more` link. Some postings come through complete
    (no truncation, no link), so fall back to the first position where *every* remaining
    line up to the client block looks like a skill name.
    """
    for i in range(i_desc, i_client):
        if DIGEST_MORE.search(body[i]):
            return i + 1
    for i in range(i_desc, i_client):
        rest = body[i:i_client]
        if rest and all(_looks_like_skill(s) for s in rest):
            return i
    return i_client


def _digest_block(block: list[str]) -> dict:
    """One posting inside a daily digest. Layout, top to bottom:

        <title>
        Fixed-price • $30            (or `Hourly • $25.00 - $47.00`)
        <description lines ... more>
        <skill>
        <skill>
        +N
        Payment verified • 5.0 • $3k spent • Canada
    """
    body = [s for s in block if s]
    if not body:
        return {}
    job: dict = {}
    job["title"] = _clean_title(body[0])

    i_budget = next((i for i, s in enumerate(body) if DIGEST_BUDGET.match(s)), None)
    i_client = next((i for i, s in enumerate(body) if DIGEST_CLIENT.match(s)), len(body))
    if i_budget is None:
        i_desc = 1
        budget: dict = {}
    else:
        i_desc = i_budget + 1
        budget = _parse_budget(body[i_budget])

    # description ends at the `... more` link, or at the first line that starts the skills list
    i_end = _skills_start(body, i_desc, i_client)
    desc = _join_desc(body[i_desc:i_end]) if i_desc < i_end else ""

    skills: list[str] = []
    for s in body[i_end:i_client]:
        if re.fullmatch(r"\+\d+", s.strip()):
            continue
        if len(s) <= 45:
            name = _skill_name(s)
            if name and name.lower() not in ("skills:", "skills"):
                skills.append(name)

    client_lines: list[str] = []
    for s in body[i_client:]:
        if DIGEST_FOOTER.match(s):
            break
        client_lines.append(s)
    client = _parse_client(" ".join(client_lines)) if client_lines else {}

    job.update(budget)
    job.setdefault("budget_type", None)
    job.setdefault("budget_min", None)
    job.setdefault("budget_max", None)
    job.setdefault("currency", "USD")
    job["description"] = desc
    job["description_source"] = "snippet"
    job["skills"] = skills
    job.update({k: client.get(k) for k in
                ("payment_verified", "client_rating", "client_spent",
                 "client_hires", "client_country")})
    job.setdefault("client_hires", None)
    return job


def parse_digest(markup: str, *, mail_dt, subject: str) -> list[dict]:
    """`New jobs that match your saved search` — several postings per email, HTML only.

    This variant has **no job id and no job URL**: every link is wrapped in an encrypted
    `link.t.upwork.com/ls/click?upn=...` redirect (verified: the payload is 948 bytes of
    ciphertext, not decodable). So the primary key is a fingerprint of title+budget, and
    `url` points at a Upwork search for the title instead of a page we cannot construct.
    """
    lines = _html_lines(markup)
    starts = [i for i, ln in enumerate(lines) if DIGEST_POSTED.match(ln)]
    jobs: list[dict] = []
    for n, idx in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        parsed = _digest_block(lines[idx + 1:end])
        if not parsed or not parsed.get("title"):
            continue
        hours = _ago_offset(lines[idx])
        posted = None
        if mail_dt is not None:
            posted = (mail_dt - timedelta(hours=hours)).astimezone(timezone.utc).isoformat()
        title = parsed["title"]
        fingerprint = f"{title}|{parsed.get('budget_type')}|{parsed.get('budget_min')}"
        pid = "digest-" + hashlib.sha1(fingerprint.encode()).hexdigest()[:12]
        jobs.append({
            "platform": PLATFORM,
            "platform_id": pid,
            "title": title,
            "company": None,
            "url": "https://www.upwork.com/nx/search/jobs/?q=" + quote(title),
            "description": parsed.get("description") or "",
            "description_source": "snippet",
            "posted_at": posted,
            "budget_type": parsed.get("budget_type"),
            "budget_min": parsed.get("budget_min"),
            "budget_max": parsed.get("budget_max"),
            "currency": parsed.get("currency", "USD"),
            "skills": parsed.get("skills") or [],
            "client_country": parsed.get("client_country"),
            "client_rating": parsed.get("client_rating"),
            "client_spent": parsed.get("client_spent"),
            "client_hires": parsed.get("client_hires"),
            "payment_verified": parsed.get("payment_verified"),
            "proposals": None,
            "required_connects": None,
            "subject": subject,
        })
    return jobs


def parse_email(raw: bytes) -> list[dict]:
    msg = email.message_from_bytes(raw)
    subject = _decoded(msg.get("Subject"))
    posted_at = None
    dt = None
    if msg.get("Date"):
        try:
            dt = parsedate_to_datetime(msg["Date"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            posted_at = dt.astimezone(timezone.utc).isoformat()
        except Exception:
            dt = None
            posted_at = None
    if DIGEST_SUBJECT.search(subject):
        markup = _html_of(msg)
        if not markup:
            raise AdapterError(f"digest email has no html part (subject={subject[:60]})")
        jobs = parse_digest(markup, mail_dt=dt, subject=subject)
    else:
        body = _text_of(msg)
        if not body:
            raise AdapterError(f"email has no body (subject={subject[:60]})")
        jobs = parse_body(body, posted_at=posted_at, subject=subject)
    for j in jobs:
        j["id"] = hashlib.sha1(f"{PLATFORM}:{j['platform_id']}".encode()).hexdigest()[:16]
        j["raw"] = {
            "subject": subject,
            "email_date": msg.get("Date"),
            "message_id": msg.get("Message-ID"),
        }
    return jobs


class UpworkMailAdapter(Adapter):
    """Pull jobs from Upwork alerts forwarded to a mailbox. Requires IMAP credentials in .env."""

    name = "upwork"

    def __init__(self, days: int = 7, limit: int = 300) -> None:
        self.days = days
        self.limit = limit

    def fetch(self) -> list[dict]:
        from core.config import upwork_imap

        from .upwork_imap import MailAuthError, fetch_raw

        try:
            host, user, pwd = upwork_imap()
        except RuntimeError as exc:
            raise AdapterError(str(exc)) from exc
        try:
            mails = fetch_raw(user, pwd, host=host, days=self.days, limit=self.limit)
        except MailAuthError as exc:
            raise AdapterError(str(exc)) from exc

        jobs: list[dict] = []
        seen: set[str] = set()
        for m in mails:
            for job in parse_email(m["raw"]):
                if job["id"] in seen:
                    continue
                seen.add(job["id"])
                jobs.append(job)
        return jobs
