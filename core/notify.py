"""Push notification. Two channels, one message body.

Priority: WeCom group-bot webhook (instant, needs a WeCom account) -> SMTP email to
yourself (needs no signup at all: the 163 auth code already in .env covers sending as
well as reading) -> write to logs/notify.log and stay quiet.

Design rules:
1. No channel configured is NOT an error. Log and stay quiet; a broken notifier must
   never take down the scrape run.
2. The message must be readable on a phone lock screen in under 3 seconds: title, score,
   one line of money, one line of why. No tables, no JSON, no "click here to view all".
3. Every number in the message comes from the database. Nothing is estimated.
"""
from __future__ import annotations

import json
import re
import smtplib
import urllib.request
from email.message import EmailMessage
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "notify.log"
# WeCom markdown caps at 4096 bytes; Chinese chars are 3 bytes in UTF-8, so stay well under.
MAX_CHARS = 1200


def deliver(subject: str, text: str, cfg: dict) -> tuple[bool, str]:
    """Try every configured channel. Returns (delivered_at_least_once, detail).

    Email is the fallback so the thing still works for someone with no WeCom account;
    that is why the channel order lives here and not in run.py.
    """
    if cfg.get("webhook"):
        ok, detail = send(text, cfg["webhook"])
        if ok:
            return True, "wecom"
        if not cfg.get("smtp"):
            return False, detail
        # webhook configured but failing — fall through to email rather than lose the job
        ok2, detail2 = send_email(subject, text, cfg["smtp"])
        return ok2, f"wecom failed ({detail}); email {detail2}"
    if cfg.get("smtp"):
        return send_email(subject, text, cfg["smtp"])
    _log(text, reason="no channel configured")
    return False, "no channel configured"


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
    """Webhook channel. Returns (delivered, detail); never raises."""
    text = text[:MAX_CHARS]
    if not webhook:
        _log(text, reason="no webhook configured")
        return False, "no webhook"
    ok, detail = _post(webhook, {"msgtype": "markdown", "markdown": {"content": text}})
    _log(text, reason="sent(wecom)" if ok else f"FAILED {detail}")
    return ok, detail


def send_email(subject: str, text: str, smtp: dict) -> tuple[bool, str]:
    """SMTP channel. Same body, rendered as HTML so the links are tappable on a phone."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp["user"]
    msg["To"] = smtp["to"]
    msg.set_content(text)
    msg.add_alternative(_to_html(text), subtype="html")
    try:
        with smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=20) as s:
            s.login(smtp["user"], smtp["pass"])
            s.send_message(msg)
    except Exception as exc:  # auth, TLS, timeout, SMTPRecipientsRefused — report, never raise
        detail = f"{type(exc).__name__}: {exc}"
        _log(text, reason=f"FAILED email {detail}")
        return False, detail
    _log(text, reason=f"sent(email) -> {smtp['to']}")
    return True, f"email -> {smtp['to']}"


_LINK = re.compile(r"\[(.+?)\]\((.+?)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`(.+?)`")
_COMMENT = re.compile(r'<font color="comment">(.+?)</font>')


def _to_html(text: str) -> str:
    """Minimal markdown -> HTML. Deliberately not a parser: the only input is format_jobs()."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = _LINK.sub(r'<a href="\2" style="color:#2563eb">\1</a>', line)
        line = _BOLD.sub(r"<b>\1</b>", line)
        line = _CODE.sub(r'<span style="background:#f3f4f6;padding:1px 5px;border-radius:3px">\1</span>', line)
        line = _COMMENT.sub(r'<span style="color:#9ca3af;font-size:12px">\1</span>', line)
        out.append(f'<div style="margin:0 0 6px 0">{line}</div>')
    body = "\n".join(out)
    return (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.55;color:#111827;max-width:560px">'
        f"{body}</div>"
    )


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


def subject_for(rows: list[dict]) -> str:
    """Email only. Kept short so it is readable in a phone notification banner."""
    n = len(rows)
    if n == 1:
        title = (rows[0].get("title") or "").replace("\n", " ")
        return f"[雷达] {rows[0].get('score')}分 · {title[:40]}"
    return f"[雷达] {n} 个岗位值得看，最高 {max(r.get('score') or 0 for r in rows)} 分"


def format_heartbeat(scanned: int, best: int | None, threshold: int, days: int) -> str:
    best_s = f"最高 {best} 分" if best is not None else "库里还是空的"
    return (
        f"**雷达还活着，{days} 天没有达标岗位**\n"
        f"期间扫了 {scanned} 条，{best_s}，阈值 {threshold}。\n"
        f"<font color=\"comment\">这条是心跳，说明程序在跑；不想收就把 .env 里的 HEARTBEAT_HOURS 设成 0</font>"
    )
