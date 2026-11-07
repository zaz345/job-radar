"""Minimal .env reader (no python-dotenv dependency).

Rules: KEY=value, lines starting with # are comments, surrounding quotes are stripped. Missing file = no config.
"""
from __future__ import annotations

from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env(path: Path | None = None) -> dict[str, str]:
    p = Path(path) if path else ENV_PATH
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def upwork_imap(path: Path | None = None) -> tuple[str, str]:
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
    try:
        min_score = int(env.get("NOTIFY_MIN_SCORE", "45"))
    except ValueError:
        min_score = 45
    try:
        heartbeat_hours = int(env.get("HEARTBEAT_HOURS", "24"))
    except ValueError:
        heartbeat_hours = 24
    try:
        max_per_push = int(env.get("NOTIFY_MAX_PER_PUSH", "3"))
    except ValueError:
        max_per_push = 3
    return {
        "webhook": env.get("WECOM_WEBHOOK", "").strip() or None,
        "min_score": min_score,
        "heartbeat_hours": heartbeat_hours,
        "max_per_push": max_per_push,
        "platform": env.get("NOTIFY_PLATFORM", "upwork").strip() or "upwork",
    }
