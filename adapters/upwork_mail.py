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
from datetime import timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

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


def _parse_client(line: str) -> dict:
    """`Payment verified · 4.98 stars · $78K spent · United States` — order varies, match by content."""
    out: dict = {"payment_verified": None, "client_rating": None,
                 "client_spent": None, "client_country": None}
    if re.search(r"payment\s+verified", line, re.I):
        out["payment_verified"] = 1
    elif re.search(r"payment\s+(not\s+verified|unverified)", line, re.I):
        out["payment_verified"] = 0
    if (m := RATING.search(line)):
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
    parts = [p.strip(" ·,\t") for p in re.split(r"[·|]", rest)]
    for p in parts:
        p = p.strip()
        if p and not re.fullmatch(r"[\d.,$%KMkm ]+", p) and len(p) < 40:
            out["client_country"] = p
    return out


def _parse_budget(line: str) -> dict:
    out = {"budget_type": None, "budget_min": None, "budget_max": None, "currency": "USD"}
    m = re.search(r"fixed[- ]price:?\s*" + MONEY, line, re.I)
    if m:
        out["budget_type"] = "fixed"
        out["budget_min"] = out["budget_max"] = _money(m.group(1))
        return out
    m = re.search(r"hourly:?\s*" + MONEY + r"\s*(?:-\s*" + MONEY + r")?", line, re.I)
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


def parse_email(raw: bytes) -> list[dict]:
    msg = email.message_from_bytes(raw)
    subject = _decoded(msg.get("Subject"))
    posted_at = None
    if msg.get("Date"):
        try:
            dt = parsedate_to_datetime(msg["Date"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            posted_at = dt.astimezone(timezone.utc).isoformat()
        except Exception:
            posted_at = None
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
