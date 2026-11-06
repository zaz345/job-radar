"""Adapter base class.

One contract: fetch() returns list[dict] with the field names defined in the schema.
No database access and no scoring inside adapters — adding a platform means one new file, core/ untouched.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

USER_AGENT = "JobRadar/0.1 (personal research; contact via GitHub)"
DEFAULT_TIMEOUT = 30


class AdapterError(RuntimeError):
    """Fetch failure. Must propagate so runs logs the error — a silent empty return reads as "no new jobs today"."""


class Adapter:
    name: str = "base"

    def fetch(self) -> list[dict]:
        raise NotImplementedError

    # --- shared helpers ---
    def get_json(self, url: str, *, retries: int = 2, timeout: int = DEFAULT_TIMEOUT) -> Any:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status != 200:
                        raise AdapterError(f"{self.name}: HTTP {resp.status} from {url}")
                    return json.loads(resp.read().decode("utf-8", errors="replace"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last = exc
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
        raise AdapterError(f"{self.name}: failed after {retries + 1} attempts: {last}")
