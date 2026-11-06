#!/usr/bin/env python3
"""job-radar entry point.

    python run.py --once            # fetch all sources, store
    python run.py --stats           # inspect the database

Design note: one failing source must not break others, but it must fail loudly (error row in runs + non-zero exit) —
never silently return an empty table; that would read as "no new jobs today".
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from adapters.base import AdapterError
from adapters.remoteok import RemoteOKAdapter
from adapters.remotive import RemotiveAdapter
from adapters.upwork_mail import UpworkMailAdapter
from core import db

REGISTRY = {
    UpworkMailAdapter.name: UpworkMailAdapter,
    RemoteOKAdapter.name: RemoteOKAdapter,
    RemotiveAdapter.name: RemotiveAdapter,
}

DEFAULT_DB = Path(__file__).parent / "data" / "jobs.db"
POLITE_DELAY = 3.0  # serial with a delay; no concurrency


def cmd_once(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)

    names = [n.strip() for n in args.source.split(",") if n.strip()]
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        print(f"[fatal] unknown source(s): {', '.join(unknown)}; known: {', '.join(REGISTRY)}")
        return 2

    failed = 0
    total_new = 0
    for idx, name in enumerate(names):
        if idx:
            time.sleep(POLITE_DELAY)
        adapter = REGISTRY[name]()
        run_id = db.start_run(conn, name)
        t0 = time.perf_counter()
        try:
            jobs = adapter.fetch()
        except (AdapterError, RuntimeError) as exc:
            elapsed = time.perf_counter() - t0
            db.finish_run(conn, run_id, error=str(exc)[:500])
            print(f"[{name:9}] FAILED in {elapsed:4.1f}s  {exc}")
            failed += 1
            continue
        try:
            inserted, updated = db.upsert_jobs(conn, jobs)
        except Exception as exc:  # any storage-phase error must also land in runs, otherwise you get a row that "looks successful"
            elapsed = time.perf_counter() - t0
            db.finish_run(conn, run_id, fetched=len(jobs), error=f"store: {exc}"[:500])
            print(f"[{name:9}] STORE FAILED  {type(exc).__name__}: {exc}")
            failed += 1
            continue
        elapsed = time.perf_counter() - t0
        db.finish_run(
            conn, run_id, fetched=len(jobs), inserted=inserted, updated=updated
        )
        total_new += inserted
        print(
            f"[{name:9}] fetched {len(jobs):4d}  new {inserted:4d}  "
            f"dup {updated:4d}  {elapsed:4.1f}s"
        )

    print("-" * 62)
    print(f"database: {Path(args.db).resolve()}")
    print(f"new rows this run: {total_new}")
    for row in db.counts_by_platform(conn):
        print(f"  {row['platform']:9} {row['n']:5d} rows   newest {row['newest'] or '-'}")
    conn.close()
    return 1 if failed and failed == len(names) else 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    print("== rows by platform ==")
    rows = db.counts_by_platform(conn)
    if not rows:
        print("  (empty)")
    for row in rows:
        print(f"  {row['platform']:9} {row['n']:5d} rows   newest {row['newest'] or '-'}")
    print("\n== recent runs ==")
    for r in db.recent_runs(conn, 12):
        status = "ERROR" if r["error"] else "ok"
        print(
            f"  #{r['id']:<4} {r['platform']:9} {status:5} "
            f"fetched={r['fetched']:<4} new={r['inserted']:<4} {r['started_at']}"
        )
        if r["error"]:
            print(f"        {r['error'][:120]}")
    conn.close()
    return 0


def _short(value, width: int) -> str:
    s = "" if value is None else str(value)
    s = s.replace("\n", " ").strip()
    return s if len(s) <= width else s[: width - 1] + "…"


def _fmt_posted(value: str | None) -> str:
    # 2026-11-03T08:00:12+00:00 -> 11-03 08:00
    if not value or len(value) < 16:
        return "-"
    return f"{value[5:10]} {value[11:16]}"


def cmd_list(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    rows = db.list_jobs(conn, limit=args.limit, platform=args.platform)
    if not rows:
        print("(empty) — run: python run.py --once")
        return 0
    print(f"{'POSTED':12} {'PLATFORM':10} {'TITLE':48} {'COMPANY':22} BUDGET")
    print("-" * 108)
    for r in rows:
        budget = "-"
        if r["budget_min"] is not None or r["budget_max"] is not None:
            budget = f"{r['currency'] or ''}{r['budget_min'] or ''}-{r['budget_max'] or ''}"
        print(
            f"{_fmt_posted(r['posted_at']):12} {r['platform']:10} "
            f"{_short(r['title'], 48):48} {_short(r['company'], 22):22} {budget}"
        )
    print("-" * 108)
    print(f"{len(rows)} shown  ·  use --export to open full URLs in a spreadsheet")
    conn.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    import csv

    conn = db.connect(args.db)
    db.init_db(conn)
    rows = db.all_jobs(conn)
    if not rows:
        print("(empty) — run: python run.py --once")
        return 0
    out = Path(args.export)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))
    print(f"wrote {len(rows)} rows -> {out.resolve()}")
    print("Open directly in Excel (UTF-8 BOM included, displays correctly)")
    conn.close()
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    from core.normalize import now_utc
    from core.score import evaluate, load_rules

    rules = load_rules()
    conn = db.connect(args.db)
    db.init_db(conn)
    conn.execute(
        "INSERT INTO rules_snapshot (taken_at, content) VALUES (?, ?)", (now_utc(), "rules.yaml")
    )
    conn.commit()

    rows = list(conn.execute(
        "SELECT id, title, description, description_source, skills, posted_at, "
        "budget_type, budget_min, budget_max, currency, proposals, required_connects, "
        "client_spent, client_hires, client_country, client_rating, payment_verified FROM jobs"
    ))
    tally = {"apply": [], "consider": [], "filtered": 0}
    veto_counts: dict[str, int] = {}
    for row in rows:
        job = dict(row)
        result = evaluate(job, rules)
        conn.execute(
            "UPDATE jobs SET score=?, score_hits=? WHERE id=?",
            (
                result["score"],
                json.dumps({"hits": result["hits"], "veto": result["veto"]}, ensure_ascii=False),
                row["id"],
            ),
        )
        if result["verdict"] == "filtered" and result["veto"]:
            veto_counts[result["veto"]] = veto_counts.get(result["veto"], 0) + 1
        if result["verdict"] == "apply":
            tally["apply"].append((result["score"], dict(row), result["hits"]))
        elif result["verdict"] == "consider":
            tally["consider"].append((result["score"], dict(row), result["hits"]))
        else:
            tally["filtered"] += 1
    conn.commit()

    print(f"scored {len(rows)} jobs   rules: {len(rules['hard_filters'])} veto / "
          f"{len(rules['bonuses'])} bonus / {len(rules['penalties'])} penalty")
    print("=" * 100)
    for verdict, label in (("apply", "APPLY"), ("consider", "consider")):
        items = sorted(tally[verdict], key=lambda x: -x[0])
        threshold = rules["thresholds"]["apply"] if verdict == "apply" else rules["thresholds"]["consider"]
        print(f"\n[{label}] {len(items)} items (score >= {threshold})")
        for score, row, hits in items[: args.limit]:
            budget = f"${row['budget_min'] or row['budget_max'] or '?'}"
            print(f"  [{score:3d}] {_short(row['title'], 44):44} {row['platform']:9} {budget:8} {' '.join(hits)}")
    print(f"\n[filtered] {tally['filtered']} items, hard-filter breakdown:")
    for veto, n in sorted(veto_counts.items(), key=lambda x: -x[1]):
        print(f"    {veto:20} {n:4d}")
    conn.close()
    return 0


def cmd_pull_mail(args: argparse.Namespace) -> int:
    """Dump raw Upwork alert emails to _samples/ as .eml, to inspect real structure before writing the parser."""
    from adapters.upwork_imap import MailAuthError, fetch_raw
    from core.config import upwork_imap

    try:
        host, user, pwd = upwork_imap()
    except RuntimeError as exc:
        print(f"[config] {exc}")
        return 2
    t0 = time.perf_counter()
    try:
        mails = fetch_raw(user, pwd, host=host, days=args.days, limit=args.limit)
    except MailAuthError as exc:
        print(f"[fatal] {exc}")
        return 2
    elapsed = time.perf_counter() - t0

    out_dir = Path(__file__).parent / "_samples"
    out_dir.mkdir(exist_ok=True)
    for m in mails:
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in m["subject"])[:60]
        (out_dir / f"mail_{m['uid']}_{safe}.eml").write_bytes(m["raw"])

    print(f"pulled {len(mails)} mails from {user} via {host} in {elapsed:.1f}s -> {out_dir}")
    if not mails:
        print("  0 emails: check that the Gmail filter forwarding upwork.com to this mailbox is active and the forwarding address is verified.")
        print("  You can also widen the window with --days 30 and retry.")
        return 0
    for m in mails[-10:]:
        print(f"  {m['date'][:31]:33} {m['subject'][:70]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Job Radar — one unified table for job postings")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--source", default=",".join(REGISTRY), help="comma separated")
    ap.add_argument("--limit", type=int, default=30, help="rows to show with --list")
    ap.add_argument("--platform", default=None, help="filter by platform with --list")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="fetch one round and store")
    g.add_argument("--stats", action="store_true", help="show what is in the db")
    g.add_argument("--list", dest="do_list", action="store_true", help="print rows as a table")
    g.add_argument("--score", action="store_true", help="score every job with rules.yaml")
    g.add_argument("--export", default=None, metavar="CSV", help="export rows to CSV (for Excel)")
    g.add_argument("--pull-mail", dest="pull_mail", action="store_true",
                   help="download Upwork alert emails to _samples/ (needs .env)")
    ap.add_argument("--days", type=int, default=14, help="how far back for --pull-mail")
    args = ap.parse_args()
    if args.once:
        return cmd_once(args)
    if args.stats:
        return cmd_stats(args)
    if args.do_list:
        return cmd_list(args)
    if args.score:
        return cmd_score(args)
    if args.pull_mail:
        return cmd_pull_mail(args)
    return cmd_export(args)


if __name__ == "__main__":
    sys.exit(main())
