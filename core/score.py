"""Rule engine: reads rules.yaml and scores a single job.

Design constraints:
1. Rules are fully data-driven — calibration = editing the yaml, not this file.
2. Missing fields (RemoteOK has no budget/proposals) silence their rules instead of erroring:
   every source has different fields; the engine must tolerate sparse ones.
3. Output must be explainable: beyond the score, hit rule names and veto reasons come along.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

RULES_PATH = Path(__file__).resolve().parent.parent / "rules.yaml"

_DELIVERABLE_FALLBACK = ("pdf to excel", "n8n", "api integration", "python script",
                         "web scraping", "data cleaning", "automate report",
                         "data extraction", "google sheets")


def load_rules(path: str | Path = RULES_PATH) -> dict:
    rules = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rules, dict) or "hard_filters" not in rules:
        raise ValueError("rules.yaml missing hard_filters — wrong format")
    return rules


def _job_text(job: dict) -> str:
    skills = job.get("skills") or []
    if isinstance(skills, str):
        try:
            skills = json.loads(skills)
        except (json.JSONDecodeError, TypeError):
            skills = [skills]
    parts = [job.get("title") or "", job.get("description") or "", " ".join(map(str, skills))]
    return " ".join(p for p in parts if p).lower()


def _age_hours(posted_at: str | None) -> float | None:
    if not posted_at:
        return None
    try:
        dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def _kw_hit(text: str, kw: str) -> bool:
    """Keyword hit: word boundaries + negation exemption.

    - word boundaries: 'ghl' must not match 'highly';
    - negation: "not a full-time position" is not full-time.
    """
    pat = re.compile(rf"(?<!\w){re.escape(kw.lower())}(?!\w)")
    start = 0
    while True:
        m = pat.search(text, start)
        if not m:
            return False
        lookback = text[max(0, m.start() - 24): m.start()]
        if not re.search(r"\b(not|no|without|avoid|exclude)\s+\w*\s*$", lookback):
            return True
        start = m.end() + 1


def _nearby(text: str, kw: str, verbs: list[str], window: int = 90) -> bool:
    """Does a harvesting verb appear near the keyword hit — separates "wants you to harvest these" from "mentions in passing"."""
    pat = re.compile(rf"(?<!\w){re.escape(kw.lower())}(?!\w)")
    lowered_verbs = [v.lower() for v in verbs]
    for m in pat.finditer(text):
        seg = text[max(0, m.start() - window): m.end() + window]
        if any(v in seg for v in lowered_verbs):
            return True
    return False


def _budget(job: dict) -> float | None:
    if job.get("budget_type") != "fixed":
        return None
    for k in ("budget_min", "budget_max"):
        v = job.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _check_filter(rule: dict, job: dict, text: str, title: str) -> bool:
    kind = rule["kind"]
    if kind == "keywords":
        kws = [k.lower() for k in rule["keywords"]]
        for k in kws:
            if not _kw_hit(text, k):
                continue
            # require_nearby: a harvesting verb must appear near the hit (avoids killing recruiter self-descriptions)
            if rule.get("require_nearby") and not _nearby(text, k, rule["require_nearby"]):
                continue
            # also_anywhere: the whole text must also contain some phrase (design intent: LinkedIn automation + "your account")
            if rule.get("also_anywhere") and not any(
                _kw_hit(text, p.lower()) for p in rule["also_anywhere"]
            ):
                continue
            return True
        return False
    if kind == "tool_locked":
        for tool in rule["tools"]:
            t = tool.lower()
            if _kw_hit(title, t):
                return True
            if _kw_hit(text, t) and any(p.lower() in text for p in rule["require_phrases"]):
                return True
        return False
    if kind == "budget_micro":
        b = _budget(job)
        return b is not None and b < rule["min_fixed_budget"]
    if kind == "unpaid_trial":
        if any(_kw_hit(text, k.lower()) for k in rule["keywords"]):
            return True
        has_pay = _budget(job) is not None or "$" in text
        return (not has_pay) and any(_kw_hit(text, k.lower()) for k in rule["test_keywords"])
    if kind == "bid_pool":
        tiers = job.get("bid_tiers") or []
        return len(tiers) >= rule["same_tier_count"] and len(set(tiers)) == 1
    return False


def _check_bonus(rule: dict, job: dict, text: str) -> bool:
    kind = rule["kind"]
    if kind == "keywords":
        return any(_kw_hit(text, k.lower()) for k in rule["keywords"])
    if kind == "budget_range":
        b = _budget(job)
        return b is not None and rule["min"] <= b <= rule["max"]
    if kind == "fresh":
        age = _age_hours(job.get("posted_at"))
        return age is not None and age <= rule["max_age_hours"]
    if kind == "max_value":
        v = job.get(rule["field"])
        return isinstance(v, (int, float)) and v <= rule["max"]
    if kind == "min_value":
        v = job.get(rule["field"])
        return isinstance(v, (int, float)) and v >= rule["min"]
    if kind == "client_quality":
        spent = job.get("client_spent")
        hires = job.get("client_hires")
        return (isinstance(spent, (int, float)) and spent > 0) or (
            isinstance(hires, (int, float)) and hires > 0)
    return False


def _check_penalty(rule: dict, job: dict, text: str) -> bool:
    kind = rule["kind"]
    if kind in ("keywords",):
        return any(_kw_hit(text, k.lower()) for k in rule["keywords"])
    if kind == "min_value":
        v = job.get(rule["field"])
        return isinstance(v, (int, float)) and v >= rule["min"]
    if kind == "vague_scope":
        # bodies from email alerts are truncated snippets (ending with a "more" link), not full specs.
        # scoring length off snippets would flag every mail-sourced job as "unclear spec" — exempt them.
        if job.get("description_source") == "snippet":
            return False
        desc = job.get("description") or ""
        if len(desc) >= rule["min_description_chars"]:
            return False
        return not any(_kw_hit(text, k) for k in _DELIVERABLE_FALLBACK)
    if kind == "repeat_poster":
        v = job.get("client_jobs_30d")
        return isinstance(v, (int, float)) and v >= rule["max_jobs_30d"]
    return False


def evaluate(job: dict, rules: dict) -> dict:
    text = _job_text(job)
    title = (job.get("title") or "").lower()

    veto = None
    for f in rules.get("hard_filters", []):
        if not f.get("enabled", True):
            continue
        if _check_filter(f, job, text, title):
            veto = f["id"]
            break

    hits: list[str] = []
    score = 0
    if veto is None:
        for b in rules.get("bonuses", []):
            if _check_bonus(b, job, text):
                score += b["weight"]
                hits.append(b["id"])
        for p in rules.get("penalties", []):
            if not p.get("enabled", True):
                continue
            if _check_penalty(p, job, text):
                score += p["weight"]
                hits.append(f"-{p['id']}")
        score = max(0, min(100, score))

    th = rules.get("thresholds", {})
    if veto is not None:
        verdict = "filtered"
    elif score >= th.get("apply", 70):
        verdict = "apply"
    elif score >= th.get("consider", 50):
        verdict = "consider"
    else:
        verdict = "filtered"
    return {"score": score, "hits": hits, "veto": veto, "verdict": verdict}
