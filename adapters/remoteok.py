"""RemoteOK public API.

API terms require linking back and attributing Remote OK — noted in the README.
The first element of the response is a legal notice (no id) and must be skipped.
"""
from __future__ import annotations

from adapters.base import Adapter
from core.normalize import clean_text, fingerprint, to_iso_utc

API = "https://remoteok.com/api"


class RemoteOKAdapter(Adapter):
    name = "remoteok"

    def fetch(self) -> list[dict]:
        data = self.get_json(API)
        if not isinstance(data, list):
            raise RuntimeError("remoteok: unexpected payload (not a list)")
        out: list[dict] = []
        for item in data:
            if not isinstance(item, dict) or not item.get("id"):
                continue  # the legal-notice entry
            jid = str(item["id"])
            out.append(
                {
                    "id": fingerprint(self.name, jid),
                    "platform": self.name,
                    "platform_id": jid,
                    "title": clean_text(item.get("position"), 300) or "(untitled)",
                    "company": clean_text(item.get("company"), 200) or None,
                    "url": item.get("url") or item.get("apply_url") or "",
                    "description": clean_text(item.get("description")),
                    "posted_at": to_iso_utc(item.get("date")),
                    # ★ deliberately NOT mapping salary_min/salary_max:
                    #   these are annual salaries, not project budgets. Putting them into
                    #   budget_min would wreck the budget_fit rule ($100-500 sweet spot). Raw values stay in raw.
                    "budget_type": None,
                    "budget_min": None,
                    "budget_max": None,
                    "currency": None,
                    "skills": item.get("tags") or [],
                    "client_country": clean_text(item.get("location"), 80) or None,
                    "raw": {k: v for k, v in item.items() if k != "description"},
                }
            )
        return out
