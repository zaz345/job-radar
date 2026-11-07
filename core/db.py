"""SQLite access layer. Single-file database: back up by copying, zero deployment."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .normalize import now_utc

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# only overwrite when the platform later filled in fields; never let NULL clobber existing values
_UPSERT = """
INSERT INTO jobs (id, platform, platform_id, title, company, url, description,
                  description_source, posted_at, budget_type, budget_min, budget_max,
                  currency, skills, client_country, client_spent, client_rating,
                  payment_verified, raw, first_seen, last_seen)
VALUES (:id, :platform, :platform_id, :title, :company, :url, :description,
        :description_source, :posted_at, :budget_type, :budget_min, :budget_max,
        :currency, :skills, :client_country, :client_spent, :client_rating,
        :payment_verified, :raw, :now, :now)
ON CONFLICT(id) DO UPDATE SET
  title          = excluded.title,
  company        = COALESCE(excluded.company, jobs.company),
  url            = excluded.url,
  description    = CASE WHEN length(COALESCE(excluded.description,'')) >
                             length(COALESCE(jobs.description,''))
                        THEN excluded.description ELSE jobs.description END,
  description_source = COALESCE(excluded.description_source, jobs.description_source),
  posted_at      = COALESCE(excluded.posted_at, jobs.posted_at),
  budget_type    = COALESCE(excluded.budget_type, jobs.budget_type),
  budget_min     = COALESCE(excluded.budget_min, jobs.budget_min),
  budget_max     = COALESCE(excluded.budget_max, jobs.budget_max),
  currency       = COALESCE(excluded.currency, jobs.currency),
  skills         = COALESCE(excluded.skills, jobs.skills),
  client_country = COALESCE(excluded.client_country, jobs.client_country),
  client_spent   = COALESCE(excluded.client_spent, jobs.client_spent),
  client_rating  = COALESCE(excluded.client_rating, jobs.client_rating),
  payment_verified = COALESCE(excluded.payment_verified, jobs.payment_verified),
  raw            = excluded.raw,
  last_seen      = excluded.last_seen
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # legacy migration: add scoring fields to an existing jobs table (idempotent)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for name, typ in (
        ("proposals", "INTEGER"),
        ("required_connects", "INTEGER"),
        ("client_spent", "REAL"),
        ("client_hires", "INTEGER"),
        ("client_rating", "REAL"),
        ("payment_verified", "INTEGER"),
        ("description_source", "TEXT"),
        ("notified_at", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {typ}")
    conn.commit()


def existing_ids(conn: sqlite3.Connection, platform: str) -> set[str]:
    rows = conn.execute("SELECT id FROM jobs WHERE platform = ?", (platform,))
    return {r["id"] for r in rows}


def upsert_jobs(conn: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    """Returns (inserted, updated). Dedup by primary key; reruns produce no duplicate rows."""
    if not jobs:
        return 0, 0
    stamp = now_utc()
    before = {j["id"] for j in jobs} & existing_ids(conn, jobs[0]["platform"])
    payload = []
    for j in jobs:
        row = dict(j)
        # Sparse sources legitimately omit columns (RemoteOK has no client_* at all).
        # Default them here, not in each adapter, so a new adapter missing one key
        # cannot crash the whole batch with a sqlite binding error.
        for key in (
            "company", "description", "description_source", "posted_at", "budget_type",
            "budget_min", "budget_max", "currency", "skills", "client_country",
            "client_spent", "client_hires", "client_rating", "payment_verified",
            "proposals", "required_connects", "raw",
        ):
            row.setdefault(key, None)
        row["now"] = stamp
        row["skills"] = json.dumps(row.get("skills") or [], ensure_ascii=False)
        row["raw"] = json.dumps(row.get("raw") or {}, ensure_ascii=False)[:200_000]
        payload.append(row)
    conn.executemany(_UPSERT, payload)
    conn.commit()
    return len(jobs) - len(before), len(before)


def start_run(conn: sqlite3.Connection, platform: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, platform) VALUES (?, ?)", (now_utc(), platform)
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    fetched: int = 0,
    inserted: int = 0,
    updated: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, fetched=?, inserted=?, updated=?, error=? WHERE id=?",
        (now_utc(), fetched, inserted, updated, error, run_id),
    )
    conn.commit()


def counts_by_platform(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT platform, COUNT(*) AS n, MAX(posted_at) AS newest "
            "FROM jobs GROUP BY platform ORDER BY n DESC"
        )
    )


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
    )


LIST_COLUMNS = (
    "id, platform, posted_at, title, company, budget_type, budget_min, "
    "budget_max, currency, url, status"
)


def list_jobs(
    conn: sqlite3.Connection, limit: int = 30, platform: str | None = None
) -> list[sqlite3.Row]:
    sql = f"SELECT {LIST_COLUMNS} FROM jobs"
    params: list = []
    if platform:
        sql += " WHERE platform = ?"
        params.append(platform)
    # NULL posted_at sorts last — keep them off the top rows
    sql += " ORDER BY posted_at IS NULL, posted_at DESC LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


def all_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """For export. Skips raw and description — a single 200KB column kills Excel."""
    return list(
        conn.execute(
            "SELECT id, platform, platform_id, title, company, url, posted_at, "
            "budget_type, budget_min, budget_max, currency, skills, client_country, "
            "first_seen, last_seen, score, score_hits, status "
            "FROM jobs ORDER BY posted_at IS NULL, posted_at DESC"
        )
    )


# ---------------------------------------------------------------- notifications
# Rule: a job is pushed at most once, ever. notified_at is the whole state machine —
# no separate outbox table, because a second source of truth would drift.

_NOTIFY_COLUMNS = (
    "id, platform, title, url, score, score_hits, budget_type, budget_min, "
    "budget_max, currency, client_country, posted_at, first_seen"
)


def notify_candidates(
    conn: sqlite3.Connection,
    min_score: int,
    platform: str = "upwork",
    limit: int = 3,
    max_age_days: int = 2,
) -> list[dict]:
    """Un-notified jobs at or above the bar, best first.

    platform defaults to upwork: RemoteOK/Remotive are hiring boards that never hire
    freelancers, so pushing them would be noise by construction.
    max_age_days stops a backlog: after a few days a posting is dead anyway (first-mover
    advantage is the whole reason this exists), so old rows must not flood the channel
    the first time a webhook is configured.
    """
    sql = (
        f"SELECT {_NOTIFY_COLUMNS} FROM jobs "
        "WHERE notified_at IS NULL AND score IS NOT NULL AND score >= ? "
        "AND COALESCE(status, 'new') = 'new' AND platform = ? "
        "AND julianday(first_seen) >= julianday('now', ?) "
        "ORDER BY score DESC, posted_at DESC LIMIT ?"
    )
    return [
        dict(r)
        for r in conn.execute(sql, (min_score, platform, f"-{max_age_days} days", limit))
    ]


def mark_notified(conn: sqlite3.Connection, ids: list[str]) -> int:
    if not ids:
        return 0
    stamp = now_utc()
    conn.executemany(
        "UPDATE jobs SET notified_at = ? WHERE id = ?", [(stamp, i) for i in ids]
    )
    conn.commit()
    return len(ids)


def last_notified(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(notified_at) AS t FROM jobs").fetchone()
    return row["t"] if row and row["t"] else None


def scanned_since(conn: sqlite3.Connection, iso_ts: str) -> tuple[int, int | None]:
    """(rows first seen after iso_ts, best score among them) — feeds the heartbeat."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(score) AS best FROM jobs WHERE first_seen >= ?",
        (iso_ts,),
    ).fetchone()
    return (row["n"] or 0, row["best"])

