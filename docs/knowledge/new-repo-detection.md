# Detecting "new repos" for the onset bump

## What it does

`recompute_daily_tiers` floors `overall_tier = "high"` for the (day,
category) cell where a project's earliest data-day lands. The intent:
starting a brand-new effort is itself significant, so a repo's first
activity day reads `high` even if day-one LoC/file counts are modest.

This used to be a `moderate` floor gated to days `>= first_scan_at`; it was
raised to `high` and un-gated (see "Onset scoping" below) when banding moved
to the causal expanding-window model — see
[causal-expanding-window-banding.md](causal-expanding-window-banding.md).

## Why we cache `projects.earliest_commit_date` instead of computing it from
the `commits` table

Two of the obvious approaches are wrong, and the wrongness only shows up
once you have real data.

### Wrong #1: `MIN(commits.author_date)`

The `commits` table is ingested with `--since=<7d>` (rolling 7-day window
from `last_scan_at`). It deliberately doesn't carry the full history of
each repo — that would balloon the table without contributing to the
heatmap (which only renders the last 365 days).

If you run `MIN(commits.author_date) FROM commits WHERE project_id = X`,
you get "the earliest commit *we've ingested*", not "the earliest commit
that exists." For a long-history repo we just added to our watch list
(e.g. meetscorer with 2014 commits, added 2026-05-09), the answer is
"today" — and the moderate floor fires falsely.

### Wrong #2: Earliest commit by *user-matching* author

Filtering `git log` by `--author kdmukai --author kdmukAI-bot -i` skips
commits authored under prior identities. meetscorer's 2014 history is
under "Keith Mukai <keith.mukai@essaytagger.com>" — neither name nor
email contains the substring `kdmukai`, so the filter would still report
the earliest match as a recent commit.

For "when did this repo's history begin?" the right signal is the absolute
earliest commit, regardless of who wrote it. That date is what anchors the
onset bump; the activity-required rule (see "Onset scoping") is what keeps a
repo you *joined* from false-bumping.

### What we do

`pd_dev_activity/sources/local_trees.py::find_earliest_commit_date()`
runs `git log --all --pretty=format:%at` (no `--since`, no `--author`),
finds the min timestamp in Python, and converts to a local-tz date.
The result is cached on `projects.earliest_commit_date` and refreshed
every scan (cheap — one git invocation per project).

`recompute_daily_tiers` reads that column. For projects with NO commits
at all (e.g. an empty `git init` repo), the column is NULL and the SQL
falls back to `MIN(file_touches.day)`. That covers the home-assistant-config
case: a repo created yesterday with staged-but-uncommitted files.

## Subtle git footgun: `git log --reverse -1` returns the *newest* commit

Don't write this:

```bash
git log --all --reverse -1 --pretty=format:%aI
```

Git applies `-1` BEFORE reversing the result set, so this returns the
single newest commit. To get the actual oldest commit you have to either:

- read the full timestamp stream and find the min yourself (what we do), OR
- use `git rev-list --max-parents=0 --all` (root commits) and pick the
  earliest if there are multiple roots — works but has its own edge cases
  (octopus merges, repos with multiple unrelated histories).

The "stream all dates and min in Python" approach is simple and correct
for any reasonable repo size; we don't need to optimize for million-commit
repos in this tool.

## Onset scoping

There is **no date gate**, and we deliberately do **not** synthesize a bucket
for the onset day. The bump fires only where a real activity bucket already
exists (`recompute_daily_tiers` iterates `by_dc`, which is built from measured
commits / file_touches / lens / telegram). That is exactly the set of repos
the user *started* — they committed on day one — so all of them get bumped,
across all history and going forward.

Repos the user merely *joined* are excluded automatically: their
`earliest_commit_date` is an upstream author's commit on a day the user did
nothing (e.g. SeedSigner's 2020-12-13 first commit, embit's 2020-01-23), so no
bucket exists for that (day, category) and no spurious `high` cell is painted.
This replaces the old `>= first_scan_at` gate, which was a blunt proxy: it
also excluded ~28 genuinely-new 2026 efforts (the dual-platform port + the
personal-tooling buildout) just because they predated the first scan.

Trade-off worth knowing: a truly empty repo (`git init`, no commits, no
file_touches) no longer produces a placeholder cell — there's no activity to
floor. The previous `moderate` floor synthesized one; in practice that only
ever materialized the spurious joined-repo cells the new rule avoids.
