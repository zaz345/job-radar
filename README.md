# job-radar

One table for job postings from several boards, with a pluggable adapter per source.

Built for personal use first: the point is to replace 40–60 minutes of daily manual triage
with a short digest. The scoring layer and the digest itself are not written yet — see
**Status**.

## Status

| Piece | State |
|---|---|
| SQLite schema (unified `jobs` table + `runs` health log) | done |
| `core/normalize.py` — ISO-8601 UTC, salary-text parsing, HTML cleaning | done |
| Adapter base + `remoteok` + `remotive` | done |
| Idempotent upsert (same job twice → one row) | done |
| Rule-based scorer (`rules.yaml`, 7 hard filters / 6 bonuses / 4 penalties) | done |
| `tests/test_score.py` — 17 unit tests incl. regression tests for real false positives | done |
| Upwork adapter — reads job-alert emails over IMAP | done (single-job alerts; multi-job digests unverified) |
| `tests/test_upwork_mail.py` — 7 unit tests on the real email layout | done |
| Markdown digest + email delivery | **not yet** |
| Credentials | `.env` → `UPWORK_IMAP_HOST` / `UPWORK_IMAP_USER` / `UPWORK_IMAP_PASS` (mailbox auth code; gitignored) |
| Scheduling (Windows Task Scheduler, 2×/day) | **not yet** |

## How scoring works

```bash
python run.py --score    # score every stored job with rules.yaml
```

Rules live in `rules.yaml` (weights, keywords, thresholds) — calibrating means editing that
file, not code. Every rule carries a one-line rationale kept from the manual triage period
it was distilled from.

Two things the scorer deliberately does not pretend:

- **Missing fields never fire rules.** RemoteOK/Remotive rows have no budget and no
  proposal count, so `budget_fit` and `low_competition` stay silent instead of guessing.
- **Keyword vetoes need context.** "verified LinkedIn profiles" in a recruiter's
  self-description is not a PII request — PII rules require a harvesting verb nearby, and
  the LinkedIn-automation veto requires "your account" somewhere in the post. Three real
  false positives found in the first live run are locked in as regression tests.

## Quick start

No third-party dependencies — Python 3.11+ standard library only.

```bash
python run.py --once              # fetch all sources, store
python run.py --stats             # counts + health of every fetch
python run.py --list --limit 30   # print rows as a table
python run.py --export data/jobs.csv   # open in Excel
python run.py --pull-mail --days 14    # dump Upwork alert emails to _samples/ as .eml
```

`.db` is a binary SQLite file — double-clicking it in Explorer gets you a "Data Base File"
icon and nothing else. Use `--list` or `--export` instead.

```
[remoteok ] fetched   99  new   99  dup    0  41.7s
[remotive ] fetched   11  new   11  dup    0   3.0s
--------------------------------------------------------------
new rows this run: 110
  remoteok     99 rows   newest 2026-11-03T08:00:12+00:00
  remotive     11 rows   newest 2026-11-02T20:00:00+00:00
```

Running `--once` twice in a row yields `new 0` on the second pass.

## The Upwork source

Upwork has no public API and its job pages are behind login + bot checks, so the feed comes
in through email instead of scraping.

The original plan was Gmail IMAP with an app password. That is a dead end on this machine:
app passwords require 2-Step Verification, and turning 2SV on from a proxied connection
hits Google's "we can't verify it's you" wall — it wants a network you normally use, while
Google itself is only reachable *through* that proxy. So the route is:

```
Gmail (filter: from upwork.com)  →  forward to a domestic mailbox (163/QQ)  →  IMAP
```

`adapters/upwork_mail.py` parses the `text/plain` part, not the HTML: the plain-text version
is already one field per line, while the HTML is nested tables for the same data.

Fields recovered per posting: title, budget (hourly range or fixed price), description
snippet, skills, client payment verification, client rating, client spend, client country,
job id and URL. Not recovered: **proposal count** and **required Connects** — those exist
only on the job page.

Two known limits, both parsed-and-handled rather than papered over:

- **Titles are truncated by Upwork itself** at roughly 45 characters (the email shows
  `…`), so `title` is a prefix. The full title is not in the message.
- **Descriptions are snippets.** They are marked `description_source = "snippet"` in the
  database, and `vague_scope` (which penalises short descriptions) skips them — otherwise
  every email-sourced posting would be penalised for being short by construction.

Server-side `SEARCH` is not used for sender filtering: 163's IMAP answers `OK` with an
empty result for `FROM`/`SUBJECT`/`HEADER` queries. The adapter searches by date, then
filters the `From` header locally.

## Layout

```
adapters/     one file per source; fetch() -> list[dict], nothing else
core/db.py    connection, upsert, run logging
core/schema.sql
core/normalize.py
run.py        CLI entry point
```

Adding a source means adding one file under `adapters/` and one line in `REGISTRY`.
Nothing in `core/` should need to change.

## Design notes

**Standard library only.** This processes a few hundred records twice a day. `httpx` and
`BeautifulSoup` would not make anything visibly better, and every dependency is one more
thing that can fail at 08:00 on a machine nobody is watching.

**Salary on job boards is not project budget.** RemoteOK and Remotive report *annual
salary*. Those values are deliberately left out of `budget_min` / `budget_max` and kept in
`raw` instead: a scoring rule that looks for a $100–500 project budget would otherwise
match a $180k/year role. `budget_*` is reserved for real project budgets.

**Failures must be loud.** Each source gets a row in `runs`. A fetch error, or a failure
while storing, is written there and the exit code is non-zero. An adapter that silently
returns an empty list is worse than one that crashes — it looks like "no new postings
today".

## Data sources and attribution

- **Remote OK** — <https://remoteok.com/api>. Remote OK's API terms require attribution and
  a link back: *Job data from [Remote OK](https://remoteok.com).*
- **Remotive** — <https://remotive.com/api/remote-jobs>.

Both are polled at most twice a day, serially, with a delay between sources.

## Limitations

- Only public board APIs so far. Job-board postings are mostly full-time roles; the
  freelance/project sources that matter for the actual use case are not wired up yet.
- `posted_at` comes from the source. Where a source omits a timezone (Remotive), UTC is
  assumed — that assumption is documented in the adapter, not hidden.
- No cross-platform merging. The same role posted on two boards stays as two rows.
- Title truncation on the Upwork source (see above).
- Multi-job digest emails ("New jobs that match your saved search") are parsed with the same
  block splitter but have not been verified against a real sample yet.

## License

MIT
