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
| Upwork adapter — reads job-alert emails over IMAP | done (single-job alerts *and* daily digests) |
| `tests/test_upwork_mail.py` — 14 unit tests on both real email layouts | done |
| Push notification (WeCom group bot → phone) | done |
| `tests/test_notify.py` — 17 unit tests, incl. "pushed once, never twice" | done |
| Scheduling — Windows Task Scheduler, every 30 min | done (`JobRadar-Watch`) |
| Credentials | `.env` → `UPWORK_IMAP_*` (auth code) + `WECOM_WEBHOOK` + `NOTIFY_*` (gitignored) |

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
python run.py --watch             # fetch + score + push only what clears the bar
python run.py --notify-test       # prove the push channel works before trusting it
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

## Push notification — the only part you look at

`--watch` is what Task Scheduler runs (task name `JobRadar-Watch`, every 30 minutes). It
fetches, scores, and pushes **only** the jobs worth opening. If nothing clears the bar it
sends nothing at all — an empty chat means the filter worked, not that the program died.

The bar is `NOTIFY_MIN_SCORE=45`, and that number is derived, not chosen:

| Bonus | Weight | Reachable from email? |
|---|---|---|
| `match_deliverable` | 30 | yes — title + snippet |
| `budget_fit` | 20 | only on fixed-price postings |
| `fresh` | 15 | only if posted < 6 h ago |
| `low_competition` | 15 | no — email carries no proposal count |
| `client_quality` | 10 | yes — digest footer |
| `remote_friendly` | 10 | rarely |

So a mail-sourced Upwork job tops out around 50–65. The 70 `apply` threshold is
**unreachable from email alone**; gating a notification on it produces a system that is
silent forever and looks broken. 45 means "skills match plus one more positive signal".

Three further rules, all in `core/db.py`:

- `platform = upwork` — RemoteOK and Remotive are hiring boards. They never hire
  freelancers, so pushing them would be noise by construction. They still get stored.
- `notified_at` — a job is pushed once, ever. Delivery failure leaves it NULL on purpose,
  so a network blip retries instead of silently swallowing the one job that mattered.
- `first_seen` within 2 days — a backlog must not flood the channel the first time a
  webhook is configured.

If nothing clears the bar for `HEARTBEAT_HOURS` (default 24) **and** new rows did arrive,
one line goes out: "saw N, best M, none passed". Zero new rows produces zero messages —
otherwise the heartbeat becomes the spam it was meant to prevent.

Set up — two channels, pick one, neither costs anything:

| Channel | Setup | Arrives in |
|---|---|---|
| **Email to yourself** (default) | nothing. It reuses `UPWORK_IMAP_USER` / `UPWORK_IMAP_PASS`: 163's auth code covers SMTP as well as IMAP, so the mailbox that reads alerts also sends them | seconds |
| WeCom group bot | WeCom → create a group with only yourself → group settings → group bot → add → paste the URL into `WECOM_WEBHOOK=` | instant |

`NOTIFY_EMAIL_TO` empty means "send to the same mailbox". Point it at a QQ address if that
one has a phone app you actually read. Then run `--notify-test` — it reports which channel
it picked and why it failed, if it did.

Webhook beats email when both are set, but a failing webhook falls through to email rather
than dropping the job.

## Running with the laptop off (GitHub Actions)

`.github/workflows/radar.yml` runs `--watch` on GitHub's runners every 30 minutes. The repo
is public, so Actions minutes are free and unlimited — there is nothing to rent.

| Option | Cost | What you take on |
|---|---|---|
| **GitHub Actions** | $0 | nothing |
| Oracle Cloud Always Free ARM | $0 | signup needs a card; instances get reclaimed when idle |
| Cheapest VPS (Vultr / 阿里云轻量) | ~¥25-60/month | OS updates, key management, "is the box up?" |

For a job that uses **under 10 minutes of CPU per day and 100 MB of RAM**, paying a monthly
fee buys you a machine to look after, not reliability.

Three things make a stateless runner work:

- **`--state state.json`** — every tick starts in a fresh container, so "already pushed"
  lives in a file committed back to the repo. Without it the same job is mailed on every
  tick and the heartbeat fires 48 times a day.
- **`UPWORK_MAIL_DAYS=2`** — a cold start cannot reuse a database, so parsing a week of
  mail 48 times a day is pure waste.
- **`_State.keepalive()`** — GitHub disables scheduled workflows after 60 days without a
  commit. A radar that finds nothing for two months would go quiet forever; it touches
  `state.json` every ~20 h so that cannot happen.

Secrets to add (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `UPWORK_IMAP_HOST` | same as `.env` |
| `UPWORK_IMAP_USER` | same as `.env` |
| `UPWORK_IMAP_PASS` | same as `.env` (the auth code) |
| `NOTIFY_EMAIL_TO` | optional; empty = send to `UPWORK_IMAP_USER` |
| `SMTP_HOST` | optional; default `smtp.163.com` |

`config.load_env()` reads `.env` first and then lets environment variables win, which is how
secrets reach the process — `.env` does not exist on the runner and must never be committed.

**Use one scheduler, not two.** The local task (`JobRadar-Watch`) tracks pushes in the
SQLite `notified_at` column; the workflow tracks them in `state.json`. They do not know about
each other, so running both mails you every job twice. Pick one: delete the local task with
`schtasks /delete /tn JobRadar-Watch /f` once the workflow is verified.

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
- Daily digests carry **no job id**. Every link is an encrypted `link.t.upwork.com/ls/click?upn=`
  redirect (verified 2026-11-07: the payload is 948 bytes of ciphertext, not base64-able into a URL).
  So those rows are keyed by a `title+budget` fingerprint and their `url` points at a Upwork
  search for the title. Re-postings of the same title at the same price would collapse into one row.
- Daily digests are parsed from **HTML**, not plain text: the text/plain part of those mails is
  a marketing shell whose only links are the same encrypted redirects, while the HTML has the
  real field layout. Single-job alerts go the other way round (plain text is clean).
- **Notifications only fire while the machine is on and you are logged in.** The scheduled
  task runs in the current-user context (`pythonw.exe`, no console window). Ticks missed
  while the machine was off are not replayed; the next tick catches up, because candidates
  are selected from the database, not from the current fetch.
- The scheduled task fetches **Upwork only** (every 30 min). The boards change once a day, so
  `scheduled_boards.bat` handles them separately — polling them every 30 minutes would be
  pure noise on their servers.
- A job pushed to the wrong address is not lost: `--reset-notified` clears the marks so the
  next `--watch` sends them again.

## License

MIT
