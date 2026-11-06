"""Remotive public API.

publication_date has no timezone; treated as UTC (the API's convention, not a guess).
salary is free text ("$20k -$35k") and again an annual figure — also excluded from budget_*.
"""
from __future__ import annotations

from adapters.base import Adapter
from core.normalize import clean_text, fingerprint, parse_salary_text, to_iso_utc

API = "https://remotive.com/api/remote-jobs"


class RemotiveAdapter(Adapter):
    name = "remotive"

    def fetch(self) -> list[dict]:
        data = self.get_json(API)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise RuntimeError("remotive: unexpected payload (no 'jobs' list)")
        out: list[dict] = []
        for item in jobs:
            jid = str(item.get("id") or "").strip()
            if not jid:
                continue
            lo, hi, cur = parse_salary_text(item.get("salary"))
            out.append(
                {
                    "id": fingerprint(self.name, jid),
                    "platform": self.name,
                    "platform_id": jid,
                    "title": clean_text(item.get("title"), 300) or "(untitled)",
                    "company": clean_text(item.get("company_name"), 200) or None,
                    "url": item.get("url") or "",
                    "description": clean_text(item.get("description")),
                    "posted_at": to_iso_utc(item.get("publication_date")),
                    "budget_type": None,
                    "budget_min": None,
                    "budget_max": None,
                    "currency": None,
                    "skills": item.get("tags") or [],
                    "client_country": clean_text(item.get("candidate_required_location"), 80) or None,
                    "raw": {
                        "salary_text": item.get("salary"),
                        "salary_min": lo,
                        "salary_max": hi,
                        "salary_currency": cur,
                        "job_type": item.get("job_type"),
                        "category": item.get("category"),
                    },
                }
            )
        return out
