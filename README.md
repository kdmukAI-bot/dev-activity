# dev-activity

A [personal-dashboard](https://github.com/kdmukAI-bot/personal-dashboard) plugin
that visualizes daily developer productivity as a GitHub-style heatmap, split
between SeedSigner-ecosystem work (orange) and everything else (teal).

An hourly cron job (`pd-dev-activity-scan`) walks local git working trees, bare
clones of personal GitHub forks, the project-lens SQLite DB, and a separate
Telegram-messages SQLite DB, then writes per-day activity counts into a
per-module SQLite database. The dashboard plugin reads from that DB to render
the home widget and the detail page.

## What's measured

Nine dimensions per (day, category). Distinct logical commits are deduped first
(by `(author_iso, message, author_name, files_changed, loc_effort)`, with
local-tree preferred over personal-fork when both exist for representative
attribution), then split by **commit author** — not by repo source. The bot
account `kdmukAI-bot` didn't exist until 2026-02, so historical local-tree
commits in those years were all human-authored and shouldn't be attributed to
the bot. The split is:

| Dim | Source | Banding |
|---|---|---|
| **D1a personal commits** | Commits authored by `kdmukai` (matched on `author_name` / `author_email`, excluding any bot match). True hands-on manual work — the user's deliberate, intentional commits regardless of which repo they live in. | **2-band, Q3 split** — manual work is intentional, so any such day is at least `moderate`; only the top quartile of personal-active days hits `high`. |
| **D1b bot commits** | Commits authored by `kdmukAI-bot`. The bulk of recent active development across SeedSigner, ESP32, and LVGL repos lives here. | **Standard 4-band** (`mean` / `mean+stdev` cuts) — bot work has the full natural spread, including genuine `low` days for one-off small commits. |
| **D2 committed files** | Sum of `files_changed` across active commits. | Standard 4-band (`mean` / `mean+stdev` cuts). |
| **D3 committed LoC effort** | Sum of per-file `max(insertions, deletions)` across active commits. Binary files contribute 0. | Standard 4-band. |
| **D4 uncommitted files** | Distinct file paths from `git status --porcelain` (per-day snapshot, rebuilt each scan to avoid double-counting committed work). | Standard 4-band. |
| **D5 uncommitted LoC effort** | Per-file `max(insertions, deletions)` from `git diff HEAD --numstat`; full file line count for untracked files. | Standard 4-band. |
| **D6 lens review** | Inline review comments (`pr_review_comment`) from project-lens — line-by-line PR feedback you wrote. | Standard 4-band. |
| **D7 lens discussion** | PR/issue creation by you + PR conversation comments + issue comments from project-lens — substantive engagement, including the act of opening a PR or issue. | 2-band median split — substantive PR/issue engagement is always at least `moderate`. |
| **D8 telegram messages** | Per-day count of your messages in the SeedSigner devs Telegram group (from a separate SQLite DB). All credited to the SeedSigner category. | 3-band tertile split (`trace` / `low` / `moderate`) with no noise floor — single-message days land in the sub-low `trace` band so pure chatter doesn't read as real activity, and the band caps at `moderate` so chatter can't dominate. |

The day's overall tier is the **max** across all nine dimensions (none < trace
< low < moderate < high). A high in any one dimension makes the day high.
`trace` is a sub-low band only emitted by D8 telegram, so it's effectively
"chatter without code work."

## Banding window

Tiers are assigned **causally**. `recompute_daily_tiers` replays history per
category in chronological order and bands each day against the distribution of
days **up to and including itself** — never the full history. So a day's tier
reflects how it compared to everything known *at that point in time* and is
**never degraded** by later, higher-volume days (e.g. the 2026 bot era). The
`daily_tiers` table is still fully rebuilt every scan because recent days' raw
counts keep settling inside the scan window, but a settled day has a fixed
window and therefore a stable, locked-in tier (the rebuild is idempotent for
those days).

Two adjustments keep this fair at the edges:

- **Warmup protection.** A dimension can't make real distinctions until it has
  accumulated enough non-zero days to leave its bander's cold-start fallback
  (the `_bander_min_nonzero` thresholds — 7 for the standard 4-band, 4 for
  personal-commit Q3, 3 for lens/telegram). Rather than pin a young
  category/dimension's earliest days to the flat fallback, those warmup days
  are scored with the **first window that reaches the threshold**: day *D* uses
  days `<= max(D, D0)`, where `D0` is the day the dimension first matures. This
  is a bounded one-time look-ahead limited to the warmup block; at and after
  `D0`, banding is purely causal.
- **Onset bump.** A repo's earliest activity day is floored to `high` —
  starting a brand-new effort is itself significant. It fires only on days with
  real measured activity (no synthetic empty-day cells), which is exactly the
  set of repos you *started* (committed on day one). Repos you merely *joined* —
  where `earliest_commit_date` is an upstream author's commit on a day you did
  nothing (e.g. SeedSigner 2020, embit 2020) — have no bucket that day and so
  produce no spurious `high` cell.

## Custom file/data filtering

`local_trees.py` excludes machine-generated artifacts from the file/LoC counts:
SQLite/Chroma DB files (`.db`, `.sqlite*`, `*-journal/-shm/-wal`), columnar
data dumps (`.parquet`, `.arrow`, `.feather`, `.npy`, `.npz`), pickles, and
`.bin` blobs. Adjust `_EXCLUDED_FILE_SUFFIXES` if other patterns turn out to be
noisy.

WIP file_touches with `loc_effort = 0` are also dropped at scan time — empty
new files (e.g. brand-new `__init__.py`), binary edits, whitespace-only edits
that net to 0 lines. They show up in `git status` but represent no actual work.

## Post-banding adjustments

After natural banding, `DIMENSION_DISCOUNTS` (`storage.py`) can subtract levels
from a dimension's tier before the max-wins composition. Currently empty:
telegram used to be discounted -1 here but moved to its own custom bander
(which natively caps at `moderate` and emits `trace` below `low`); a fork-day
commit boost was tried then removed once D1 was split into per-author
dimensions (D1a is already 2-band Q3, so an explicit boost would be redundant).

The mechanism remains in code and can be re-enabled or extended for new
dimensions as needed.

## Install

Into the personal-dashboard venv:

```sh
cd /path/to/personal-dashboard
. .venv/bin/activate
pip install -e ../personal-dashboard-modules/dev-activity
```

This registers:
- The `dev-activity` plugin entry point (the dashboard module loader picks it
  up on next process start — restart the dashboard service).
- The `pd-dev-activity-scan` console script (used by cron).

## Configuration

Copy and edit:

```sh
cp config.toml.example config.toml
```

Key knobs:
- `scan_roots` — directories searched recursively (4 levels deep, stopping at
  any `.git/` directory) for git working trees. Common noise dirs (`.venv`,
  `node_modules`, etc.) are skipped.
- `git_author_substrings` — substrings to match against `git log`'s `%an` /
  `%ae` for crediting commits as your work.
- `github_logins` — used both to filter project-lens events and to filter
  commits in personal-fork bare clones.
- `seedsigner_repos` — repo basenames to classify under the orange (SeedSigner)
  category. Anything else is "other" (teal).
- `lens_db_path` — absolute path to the project-lens SQLite DB (read-only).
- `telegram_db_path` / `telegram_user_ids` — separate SQLite store for raw
  Telegram messages, plus the user_ids to credit.
- `personal_forks` — `owner/repo` strings; bare-cloned into `forks_cache_dir`.
  Forks that don't exist or fail to clone are logged and skipped.

## Crontab entry

Run hourly (at `:15` so each scan picks up the latest project-lens data, which
runs at the top of each hour):

```cron
# name: pd-dev-activity-scan
# log: /home/<user>/.cache/personal-dashboard/dev-activity/scan.log
# success_pattern: scan complete
# failure_pattern: ^(Traceback|ERROR)
15 * * * * /path/to/personal-dashboard/.venv/bin/pd-dev-activity-scan >> ~/.cache/personal-dashboard/dev-activity/scan.log 2>&1
```

The `# log:` annotation block makes the job legible to the cron-summary
plugin's status detection.

## Storage

Module DB lives at:

```
~/.local/share/personal-dashboard/modules/dev-activity/data.db
```

Matches what the dashboard's `module_data_dir("dev-activity")` returns. The
scanner CLI hardcodes the same path so it can run without the dashboard core.
WAL mode is enabled so the dashboard can read while the scanner writes.

## Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/modules/dev-activity/` | GET | Detail page with full-year heatmap. Accepts `?day=YYYY-MM-DD` to pre-load one day's breakdown. |
| `/modules/dev-activity/widget` | GET | Compact widget HTML (full-year heatmap, right-anchored and clipped to fit the card). |
| `/modules/dev-activity/data` | GET | Raw `get_data()` payload as JSON. |
| `/modules/dev-activity/run-now` | POST | Trigger a scan synchronously. Used by the detail-page "Rescan now" button. |
| `/modules/dev-activity/day/{YYYY-MM-DD}` | GET | HTML fragment with one day's breakdown (htmx). |
| `/modules/dev-activity/heatmap.json` | GET | Year of `[date, category, overall_tier, ...]` rows for the SVG client. |

Clicking a day in the detail-page heatmap pushes `?day=` into the URL (via
`history.pushState`), so a refresh keeps the same day loaded and the day is
linkable.

## Verifying

```sh
# 1. Run the scanner
pd-dev-activity-scan

# 2. Inspect the DB
sqlite3 ~/.local/share/personal-dashboard/modules/dev-activity/data.db \
  "SELECT category, source, COUNT(*) FROM commits c JOIN projects p ON p.id=c.project_id GROUP BY category, source;"

sqlite3 ~/.local/share/personal-dashboard/modules/dev-activity/data.db \
  "SELECT day, category, overall_tier,
          n_commits_personal, n_commits_bot,
          n_committed_files, n_committed_loc_effort,
          n_uncommitted_files, n_uncommitted_loc_effort,
          n_lens_review, n_lens_discussion, n_telegram_msgs
   FROM daily_tiers ORDER BY day DESC LIMIT 14;"

# 3. Idempotency: re-run and confirm row counts don't grow in raw tables
pd-dev-activity-scan
```

## Caveats

- **Tiers are locked-in causally, not rescaled.** Each day is banded against
  the per-category distribution of days *up to and including itself*, so a day
  rated `high` in a sparse early month keeps that rating even after later
  high-volume days thicken the distribution — future activity never degrades a
  past day. (See "Banding window" above.) Tiers for recent days can still move
  while their raw counts settle inside the scan window, but a settled day is
  stable. The day-detail panel always shows the raw counts behind the tier.
- **Strict commit rule** relies on working-tree mtime: a commit whose every
  file has been edited again later (so mtime no longer matches the commit
  date) shows `active_on_commit_day=0`. Day-detail labels these as "older
  work" and the section header splits the count (e.g. *"Commits (5, 2 older
  work)"*). For commits older than `meta.first_scan_at`, the strict rule
  falls back to "count on author-date regardless" — there's no compensating
  WIP file_touch record for that era, so dropping them would just lose the
  data outright. Post-scanning commits still respect the strict rule, with
  the original work captured via file_touches on the actual edit day.
- **Branch coverage**: `git log --all` captures commits on every local and
  remote-tracking branch. WIP file_touches only see the currently-checked-out
  branch's working tree.
- **Dedup**: a commit that exists in both a local-tree repo and a personal
  fork (same SHA, or different SHAs from rebase/cherry-pick that share the
  same `(author_iso, message, author_name, files_changed, loc_effort)`
  signature) is counted once. The local-tree row wins for representative
  attribution; classification into D1a/D1b is by `author_name`/`author_email`
  regardless.
- **GitHub PR reviews**: the `pr_review` event kind is intentionally NOT
  emitted by the lens scanner. In the user's workflow every such record is an
  auto-generated empty wrapper around an inline `pr_review_comment` (the user
  doesn't use GitHub's "Submit Review" feature). They were double-counting the
  same activity.
- **Deleted/moved working trees are reaped.** `discover_repos` only ever adds
  or refreshes repos it can still walk to, so a local working tree that's been
  deleted (or relocated, or had its `.git` removed) would otherwise leave its
  `projects`/`commits`/`file_touches` rows behind to feed `recompute` forever.
  Each scan calls `prune_missing_local_trees`, which drops any `local_tree`
  project whose `<path>/.git` is gone (same "is this a repo?" test discovery
  uses) and cascades to its commits and file_touches. Scoped to `local_tree`
  only — personal-fork projects store a *bare* clone path (no nested `.git`)
  and are governed by the `personal_forks` config list, not disk presence.
- Personal-fork bare clones use unauthenticated HTTPS — fine for public repos
  only. Switch to SSH URLs if any forks are private.
- Repo categorization (SeedSigner / other) is by basename — a single repo
  can't be split across categories.
