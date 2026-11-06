"""Normalization utilities: dates, salary text, HTML cleaning, primary-key fingerprint.

Standard library only, deliberately. This runs twice a day over a few hundred records —
httpx/bs4 would buy nothing visible and add one more way for installation to fail.
"""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone

_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")

# $20k / $20,000 / 20k / €1.5k-2k
_MONEY = re.compile(r"(?P<cur>[€£$])?\s*(?P<num>\d+(?:[.,]\d+)?)\s*(?P<unit>[kKmM])?")
_CURRENCY_MAP = {"$": "USD", "€": "EUR", "£": "GBP"}


def fingerprint(platform: str, platform_id: str) -> str:
    return hashlib.sha1(f"{platform}:{platform_id}".encode("utf-8")).hexdigest()[:16]


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_iso_utc(value: str | None) -> str | None:
    """Convert assorted date formats to ISO-8601 UTC.

    Unparseable → None. No guessing, no defaulting to today — that would kill the "how fresh" signal.
    Timezone-naive values are treated as UTC (Remotive's publication_date, for instance).
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: str | None, limit: int = 8000) -> str:
    """Strip HTML tags, unescape entities, collapse whitespace. Truncate very long bodies — one 200KB post must not blow up the digest."""
    if not value:
        return ""
    s = _HTML_TAG.sub(" ", str(value))
    s = html.unescape(s)
    s = _WS.sub(" ", s)
    s = _BLANKS.sub("\n\n", s).strip()
    return s[:limit]


def parse_salary_text(value: str | None) -> tuple[float | None, float | None, str | None]:
    """Extract a range from free text like "$20k -$35k". Unparseable → all None.

    Only k/m suffixes and decimals are recognized; "competitive" and friends are given up on —
    a guessed number is worse than none: it poisons the budget_fit score downstream.
    """
    if not value or not isinstance(value, str):
        return None, None, None
    hits = [m for m in _MONEY.finditer(value) if m.group("num")]
    if not hits:
        return None, None, None
    currency = None
    for m in hits:
        if m.group("cur") in _CURRENCY_MAP:
            currency = _CURRENCY_MAP[m.group("cur")]
            break
    nums: list[float] = []
    for m in hits[:2]:
        n = float(m.group("num").replace(",", ""))
        unit = (m.group("unit") or "").lower()
        if unit == "k":
            n *= 1_000
        elif unit == "m":
            n *= 1_000_000
        nums.append(n)
    if not nums:
        return None, None, currency
    lo = min(nums)
    hi = max(nums)
    return lo, hi, currency
