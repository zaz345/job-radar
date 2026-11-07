"""Push notification. One channel: WeCom group-bot webhook — reaches a phone, no extra app.

Design rules:
1. No webhook configured is NOT an error. Write the message to logs/notify.log and stay quiet;
   a broken notifier must never take down the scrape run.
2. The message must be readable in a phone lock screen in under 3 seconds: title, score, one line
   of money, one line of why. No tables, no JSON, no "click here to view all".
3. Every number in the message comes from the database. Nothing is estimated.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "notify.log"
# WeCom markdown caps at 4096 bytes; Chinese chars are 3 bytes in UTF-8, so stay well under.
MAX_CHARS = 1200


def _post(url: str, payload: dict) -> tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # network, DNS, TLS, 4xx — all reported, none raised
        return False, f"{type(exc).__name__}: {exc}"
    try:
        j = json.loads(body)
    except json.JSONDecodeError:
        return False, f"non-JSON response: {body[:160]}"
    if j.get("errcode") == 0:
        return True, "ok"
    return False, f"errcode={j.get('errcode')} {j.get('errmsg')}"


def send(text: str, webhook: str | None) -> tuple[bool, str]:
    """Returns (delivered, detail). delivered=False never raises — callers only log."""
    text = text[:MAX_CHARS]
    if not webhook:
        _log(text, reason="no webhook configured")
        return False, "no webhook"
    ok, detail = _post(webhook, {"msgtype": "markdown", "markdown": {"content": text}})
    _log(text, reason="sent" if ok else f"FAILED {detail}")
    return ok, detail


def _log(text: str, reason: str) -> None:
    from .normalize import now_utc

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {now_utc()} [{reason}] =====\n{text}\n")


def _money(row: dict) -> str:
    lo, hi = row.get("budget_min"), row.get("budget_max")
    if lo is None and hi is None:
        return "预算未写"
    if row.get("budget_type") == "hourly":
        return f"${lo:g}–{hi:g}/h" if hi and hi != lo else f"${lo or hi:g}/h"
    v = lo if lo is not None else hi
    return f"固定 ${v:,.0f}"


def _why(row: dict) -> str:
    """Turn rule ids into something a human reads. Unknown ids are shown raw, never silently dropped."""
    labels = {
        "match_deliverable": "技能对口",
        "budget_fit": "预算在甜区",
        "fresh": "刚发布",
        "low_competition": "投标人少",
        "client_quality": "客户付过钱",
        "remote_friendly": "明确全球远程",
    }
    try:
        hits = json.loads(row.get("score_hits") or "{}").get("hits") or []
    except json.JSONDecodeError:
        hits = []
    out = [labels.get(h, h) for h in hits if not h.startswith("-")]
    return "、".join(out) if out else "仅过红线"


def format_jobs(rows: list[dict], threshold: int) -> str:
    n = len(rows)
    head = f"**{n} 个岗位值得看**" if n > 1 else "**1 个岗位值得看**"
    lines = [head, ""]
    for i, row in enumerate(rows, 1):
        title = (row.get("title") or "").replace("\n", " ").strip()
        title = title if len(title) <= 46 else title[:45] + "…"
        country = row.get("client_country") or ""
        tail = f" · {country}" if country else ""
        lines.append(f"**{i}. {title}**  `{row.get('score')}分`")
        lines.append(f"{_money(row)}{tail}")
        lines.append(f"{_why(row)}")
        lines.append(f"[打开岗位]({row.get('url')})")
        lines.append("")
    lines.append(f"<font color=\"comment\">阈值 {threshold} 分 · 同一岗位只推一次</font>")
    return "\n".join(lines)


def format_heartbeat(scanned: int, best: int | None, threshold: int, days: int) -> str:
    best_s = f"最高 {best} 分" if best is not None else "库里还是空的"
    return (
        f"**雷达还活着，{days} 天没有达标岗位**\n"
        f"期间扫了 {scanned} 条，{best_s}，阈值 {threshold}。\n"
        f"<font color=\"comment\">这条是心跳，说明程序在跑；不想收就把 .env 里的 HEARTBEAT_HOURS 设成 0</font>"
    )
