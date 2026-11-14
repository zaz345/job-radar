#!/usr/bin/env python3
"""job-radar entry point.

    python run.py --once            # fetch all sources, store
    python run.py --stats           # inspect the database

Design note: one failing source must not break others, but it must fail loudly (error row in runs + non-zero exit) —
never silently return an empty table; that would read as "no new jobs today".
"""
from __future__ import annotations

import argparse
import contextlib
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
    import os

    conn = db.connect(args.db)
    db.init_db(conn)
    rows = db.all_jobs(conn)
    if not rows:
        print("(empty) — run: python run.py --once")
        return 0
    out = Path(args.export)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling temp file, then swap. If the target is open in Excel the swap
    # fails on Windows with PermissionError; keep the temp copy and say so, instead of
    # dying with a traceback and losing the export.
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))
    try:
        os.replace(tmp, out)
    except PermissionError:
        keep = out.with_name(f"{out.stem}_{time.strftime('%H%M')}{out.suffix}")
        os.replace(tmp, keep)
        print(f"{out.name} is open in another program (probably Excel) — cannot overwrite it.")
        print(f"Close it and run again, or open this copy instead:")
        print(f"  {keep.resolve()}")
        conn.close()
        return 3
    print(f"wrote {len(rows)} rows -> {out.resolve()}")
    print("Open directly in Excel (UTF-8 BOM included, displays correctly)")
    conn.close()
    return 0


def _score_all(conn, rules: dict) -> dict:
    """Score every job in the db and persist score + hits. Shared by --score and --watch."""
    from core.score import evaluate as evaluate_job

    rows = list(conn.execute(
        "SELECT id, platform, title, description, description_source, skills, posted_at, "
        "budget_type, budget_min, budget_max, currency, proposals, required_connects, "
        "client_spent, client_hires, client_country, client_rating, payment_verified FROM jobs"
    ))
    tally: dict[str, list] = {"apply": [], "consider": []}
    filtered = 0
    veto_counts: dict[str, int] = {}
    closest: list[tuple[int, dict, list[str]]] = []
    for row in rows:
        job = dict(row)
        result = evaluate_job(job, rules)
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
            filtered += 1
            if result["veto"] is None and result["score"] > 0:
                closest.append((result["score"], dict(row), result["missed"]))
    conn.commit()
    return {
        "total": len(rows),
        "apply": tally["apply"],
        "consider": tally["consider"],
        "filtered": filtered,
        "veto_counts": veto_counts,
        "closest": closest,
    }


def cmd_score(args: argparse.Namespace) -> int:
    from core.normalize import now_utc
    from core.score import load_rules

    rules = load_rules()
    conn = db.connect(args.db)
    db.init_db(conn)
    conn.execute(
        "INSERT INTO rules_snapshot (taken_at, content) VALUES (?, ?)", (now_utc(), "rules.yaml")
    )
    conn.commit()

    stats = _score_all(conn, rules)
    tally = {"apply": stats["apply"], "consider": stats["consider"], "filtered": stats["filtered"]}
    veto_counts = stats["veto_counts"]
    closest = stats["closest"]

    print(f"scored {stats['total']} jobs   rules: {len(rules['hard_filters'])} veto / "
          f"{len(rules['bonuses'])} bonus / {len(rules['penalties'])} penalty")
    print("=" * 100)
    for verdict, label in (("apply", "APPLY"), ("consider", "consider")):
        items = sorted(tally[verdict], key=lambda x: -x[0])
        threshold = rules["thresholds"]["apply"] if verdict == "apply" else rules["thresholds"]["consider"]
        print(f"\n[{label}] {len(items)} items (score >= {threshold})")
        for score, row, hits in items[: args.limit]:
            print(f"  [{score:3d}] {_short(row['title'], 44):44} {row['platform']:9} "
                  f"{_fmt_budget(row):16} {' '.join(hits)}")
    if not tally["apply"] and not tally["consider"] and closest:
        # "0 items" alone tells you nothing actionable; show what the near misses were missing
        print(f"\n[closest] nothing reached {rules['thresholds']['consider']}; "
              f"top {min(args.limit, 8)} that survived the hard filters:")
        for score, row, missed in sorted(closest, key=lambda x: -x[0])[: min(args.limit, 8)]:
            print(f"  [{score:3d}] {_short(row['title'], 42):42} {row['platform']:8} "
                  f"{_fmt_budget(row):16} missing: {' '.join(missed)}")
    print(f"\n[filtered] {tally['filtered']} items, hard-filter breakdown:")
    for veto, n in sorted(veto_counts.items(), key=lambda x: -x[1]):
        print(f"    {veto:20} {n:4d}")
    conn.close()
    return 0


def _fmt_budget(row: dict) -> str:
    lo, hi = row.get("budget_min"), row.get("budget_max")
    if lo is None and hi is None:
        return "-"
    if row.get("budget_type") == "hourly" and hi is not None and hi != lo:
        return f"${lo:g}/h-${hi:g}/h"
    v = lo if lo is not None else hi
    return f"${v:g}"


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


_LOG_DIR = Path(__file__).parent / "logs"


def _cooldown_ok(name: str, hours: float) -> bool:
    """State file per notification kind — SQLite is for jobs, not for 'did I already ping'."""
    p = _LOG_DIR / f".{name}"
    if not p.exists():
        return True
    try:
        last = float(p.read_text(encoding="utf-8").strip())
    except ValueError:
        return True
    return (time.time() - last) >= hours * 3600


def _stamp(name: str) -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    (_LOG_DIR / f".{name}").write_text(str(time.time()), encoding="utf-8")


class _State:
    """Notification bookkeeping that survives a cold CI runner.

    Locally, a dotfile in logs/ is enough. On GitHub Actions every tick is a fresh
    container, so "did I already send this" has to live in a file that gets committed
    back to the repo — otherwise the heartbeat fires 48 times a day and the same job is
    pushed on every tick.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.data = _load_state(path) if path else None

    def cool(self, name: str, hours: float) -> bool:
        if self.data is not None:
            ts = self.data.get(f"{name}_at")
            return True if not ts else _hours_since(ts) >= hours
        return _cooldown_ok(name, hours)

    def mark(self, name: str) -> None:
        if self.data is not None:
            self.data[f"{name}_at"] = _now_iso()
            self.flush()
        else:
            _stamp(name)

    def seen(self) -> set[str]:
        return set(self.data.get("pushed", [])) if self.data is not None else set()

    def remember(self, ids: list[str]) -> None:
        if self.data is None:
            return
        self.data["pushed"] = self.data.get("pushed", []) + list(ids)
        self.flush()

    def flush(self) -> None:
        if self.data is None or self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Cap the list: anything older than the fetch window can never come back, and an
        # unbounded list would turn the state file into a real commit every single tick.
        self.data["pushed"] = list(dict.fromkeys(self.data.get("pushed", [])))[-800:]
        self.data["updated_at"] = _now_iso()
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )

    def keepalive(self, hours: float = 20) -> None:
        """GitHub disables scheduled workflows after 60 days without a commit.

        A radar that finds nothing for two months would commit nothing and get switched
        off — the worst possible failure, because it is silent. Touch the file anyway.
        """
        if self.data is None:
            return
        last = self.data.get("updated_at")
        if last and _hours_since(last) < hours:
            return
        self.flush()


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"pushed": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"pushed": []}
    return data if isinstance(data, dict) else {"pushed": []}


def _now_iso() -> str:
    from core.normalize import now_utc

    return now_utc()


def _hours_since(iso: str) -> float:
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return 1e9  # unparseable timestamp must not block a notification forever
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def cmd_watch(args: argparse.Namespace) -> int:
    """Scheduler entry point. Wraps _watch in a tee so the log survives unattended runs.

    Under pythonw.exe there is no console and sys.stdout is None — every print() in the
    fetch path would raise AttributeError, so the tee must own the output, not the terminal.
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = (_LOG_DIR / "watch.log").open("a", encoding="utf-8")
    try:
        with contextlib.redirect_stdout(_Tee(sys.stdout, log_fh)):
            print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
            print(f"cmd: --watch --source {args.source}")
            return _watch(args)
    finally:
        log_fh.close()


class _Tee:
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, s: str) -> None:
        for st in self.streams:
            try:
                st.write(s)
            except Exception:  # a closed terminal must not kill the run
                pass

    def flush(self) -> None:
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


def _watch(args: argparse.Namespace) -> int:
    """Fetch -> score -> push only what is worth opening.

    It must be silent when there is nothing to say: an empty chat is the product working,
    not the product being broken.
    """
    from core import notify
    from core.config import notify_settings
    from core.score import load_rules

    cfg = notify_settings()
    state = _State(Path(args.state).resolve() if getattr(args, "state", None) else None)
    conn = db.connect(args.db)
    db.init_db(conn)

    rc = cmd_once(args)
    if rc != 0:
        # total fetch failure is a real fault, not "no jobs today" — say so, at most once a day
        if state.cool("fetchfail", 24):
            ok, _detail = notify.deliver(
                "[雷达] 抓取失败",
                "**雷达抓取失败**\n所有源都没拉到数据，看一下 logs/watch.log。",
                cfg,
            )
            if ok:
                state.mark("fetchfail")
        conn.close()
        return rc

    rules = load_rules()
    stats = _score_all(conn, rules)
    print(
        f"[watch] scored {stats['total']}  apply {len(stats['apply'])}  "
        f"consider {len(stats['consider'])}  filtered {stats['filtered']}"
    )

    cands = db.notify_candidates(
        conn, cfg["min_score"], cfg["platform"], cfg["max_per_push"]
    )
    seen = state.seen()
    if seen:
        before = len(cands)
        cands = [c for c in cands if c["id"] not in seen]
        if before != len(cands):
            print(f"[watch] {before - len(cands)} candidate(s) already pushed, skipped")
    if cands:
        text = notify.format_jobs(cands, cfg["min_score"])
        ok, detail = notify.deliver(notify.subject_for(cands), text, cfg)
        print(f"[watch] {len(cands)} candidate(s) >= {cfg['min_score']} -> "
              f"{detail if ok else 'FAILED ' + detail}")
        if ok:
            db.mark_notified(conn, [c["id"] for c in cands])
            state.remember([c["id"] for c in cands])
        # On delivery failure, notified_at stays NULL on purpose: a transient network error
        # must not silently swallow the one job this thing exists to find. Retry next tick.
    else:
        print(f"[watch] nothing new >= {cfg['min_score']} on {cfg['platform']} — staying quiet")
        _maybe_heartbeat(conn, cfg, state)

    state.keepalive()
    conn.close()
    return 0


def _maybe_heartbeat(conn, cfg: dict, state: "_State | None" = None) -> None:
    """One line a day, only when there was something to say: 'saw N, best M, none passed'.

    Silence when zero new rows arrived — otherwise the heartbeat itself becomes the spam.
    """
    from core import notify

    hours = cfg["heartbeat_hours"]
    gate = state if state is not None else _State(None)
    if hours <= 0 or not gate.cool("heartbeat", hours):
        return
    n, best = db.scanned_since(conn, _hours_ago_iso(hours))
    if n == 0:
        return
    days = max(1, hours // 24)
    ok, _ = notify.deliver(
        f"[雷达] 心跳：{days} 天没有达标岗位",
        notify.format_heartbeat(n, best, cfg["min_score"], days),
        cfg,
    )
    if ok:
        gate.mark("heartbeat")


def _hours_ago_iso(hours: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def cmd_notify_test(args: argparse.Namespace) -> int:
    """Prove the channel works before trusting it with anything.

    Reports which channel it picked: webhook beats email, because it arrives faster.
    """
    from core import notify
    from core.config import notify_settings

    cfg = notify_settings()
    env_path = Path(__file__).parent / ".env"
    if not cfg["webhook"] and not cfg["smtp"]:
        print(f"[config] no channel configured in {env_path}")
        print("  Email (no signup, reuses the 163 auth code you already have):")
        print("      NOTIFY_EMAIL_TO=your-address@163.com")
        print("      (leave it empty to send to the mailbox that reads the alerts)")
        print("  WeCom webhook (instant): WECOM_WEBHOOK=https://qyapi.weixin.qq.com/...")
        print("  Until then messages only land in logs/notify.log")
        return 2

    channel = "wecom" if cfg["webhook"] else f"email -> {cfg['smtp']['to']}"
    ok, detail = notify.deliver(
        "[雷达] 连通性测试",
        "**雷达连通性测试**\n看到这条说明推送链路通了。"
        f"\n阈值 {cfg['min_score']} 分 · 只推 {cfg['platform']} · 每次最多 {cfg['max_per_push']} 条",
        cfg,
    )
    print(f"channel: {channel}")
    print(("sent" if ok else "FAILED") + f": {detail}")
    if not ok:
        print("  email 失败常见原因：SMTP 未在 163 设置里开启 / 授权码不对 / 收件地址写错")
    return 0 if ok else 1


def cmd_reset_notified(args: argparse.Namespace) -> int:
    """Un-mark pushed jobs. For 'I changed the address and those three never arrived'."""
    conn = db.connect(args.db)
    db.init_db(conn)
    n = conn.execute("UPDATE jobs SET notified_at = NULL").rowcount
    conn.commit()
    conn.close()
    print(f"cleared notified_at on {n} rows — next --watch will push them again")
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
    g.add_argument("--watch", action="store_true",
                   help="unattended tick: fetch + score + push only what clears the bar")
    g.add_argument("--notify-test", dest="notify_test", action="store_true",
                   help="send one test message to prove the push channel works")
    g.add_argument("--reset-notified", dest="reset_notified", action="store_true",
                   help="clear the pushed-marker so those jobs can be sent again")
    ap.add_argument("--days", type=int, default=14, help="how far back for --pull-mail")
    ap.add_argument("--state", default=None, metavar="JSON",
                    help="persist the pushed-list here (required for a stateless CI runner)")
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
    if args.watch:
        return cmd_watch(args)
    if args.notify_test:
        return cmd_notify_test(args)
    if args.reset_notified:
        return cmd_reset_notified(args)
    return cmd_export(args)


if __name__ == "__main__":
    sys.exit(main())
