"""Config reader: `.env` file first, then process environment on top.

Two reasons the environment wins:
- CI (GitHub Actions) has no `.env` — secrets arrive as env vars, and that is the only channel.
- A one-off override (`NOTIFY_MIN_SCORE=60 python run.py --watch`) should not require editing a file.

Rules: KEY=value, lines starting with # are comments, surrounding quotes are stripped.
Missing file is not an error — it just means "no file config".
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Every key the code can read. Listing them explicitly is what makes the os.environ
# override safe: an unrelated variable called PATH or TEMP can never leak into a config dict.
KNOWN_KEYS = (
    "UPWORK_IMAP_HOST",
    "UPWORK_IMAP_USER",
    "UPWORK_IMAP_PASS",
    "UPWORK_MAIL_DAYS",
    "WECOM_WEBHOOK",
    "NOTIFY_MIN_SCORE",
    "NOTIFY_MAX_PER_PUSH",
    "NOTIFY_PLATFORM",
    "NOTIFY_EMAIL_TO",
    "NOTIFY_EMAIL_FROM",
    "HEARTBEAT_HOURS",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_PASS",
)


def load_env(path: Path | None = None) -> dict[str, str]:
    p = Path(path) if path else ENV_PATH
    out: dict[str, str] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip()] = v
    for key in KNOWN_KEYS:
        value = os.environ.get(key)
        if value:
            out[key] = value
    return out


def upwork_imap(path: Path | None = None) -> tuple[str, str, str]:
    """Get IMAP credentials; on missing entries, say exactly which one and where to generate it."""
    env = load_env(path)
    host = env.get("UPWORK_IMAP_HOST", "").strip() or "imap.gmail.com"
    user = env.get("UPWORK_IMAP_USER", "").strip()
    pwd = env.get("UPWORK_IMAP_PASS", "").replace(" ", "").strip()
    if not user or not pwd:
        missing = " / ".join(
            n for n, v in (("UPWORK_IMAP_USER", user), ("UPWORK_IMAP_PASS", pwd)) if not v
        )
        raise RuntimeError(
            f".env is missing {missing}.\n"
            f"  File location: {ENV_PATH}\n"
            "  UPWORK_IMAP_PASS is the mailbox's auth code / app-specific password, not the login password."
        )
    return host, user, pwd


def notify_settings(path: Path | None = None) -> dict:
    """Notification config with defaults that work without any setup.

    min_score default 45 is not arbitrary: a mail-sourced Upwork job tops out at
    30 (match_deliverable) + 10 (client_quality) + 15 (fresh) + 10 (remote_friendly) = 65,
    and typically lands at 40-55. The 70 "apply" bar is unreachable from email alone.
    """
    env = load_env(path)
    return {
        "webhook": env.get("WECOM_WEBHOOK", "").strip() or None,
        "min_score": _int(env, "NOTIFY_MIN_SCORE", 45),
        "heartbeat_hours": _int(env, "HEARTBEAT_HOURS", 24),
        "max_per_push": _int(env, "NOTIFY_MAX_PER_PUSH", 3),
        "platform": env.get("NOTIFY_PLATFORM", "upwork").strip() or "upwork",
        "smtp": smtp_settings(env),
    }


def smtp_settings(env: dict | None = None) -> dict | None:
    """Email channel. Returns None when unusable, so the caller can fall back quietly.

    163's auth code covers IMAP and SMTP both — the password already in .env for reading
    mail works for sending it. That is the whole reason this channel exists: the user
    already has everything it needs.
    """
    env = load_env() if env is None else env
    user = env.get("NOTIFY_EMAIL_FROM", "").strip() or env.get("UPWORK_IMAP_USER", "").strip()
    pwd = env.get("SMTP_PASS", "").replace(" ", "").strip() or env.get("UPWORK_IMAP_PASS", "").replace(" ", "").strip()
    to = env.get("NOTIFY_EMAIL_TO", "").strip() or user
    host = env.get("SMTP_HOST", "").strip() or "smtp.163.com"
    if not (user and pwd and to):
        return None
    return {"host": host, "port": _int(env, "SMTP_PORT", 465), "user": user, "pass": pwd, "to": to}


def _int(env: dict, key: str, default: int) -> int:
    try:
        return int(env.get(key, default))
    except (TypeError, ValueError):
        return default
