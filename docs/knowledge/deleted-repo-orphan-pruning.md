# Deleted/moved repos leave orphaned activity unless explicitly pruned

## Symptom

The dev-activity heatmap showed activity for a project directory that no longer
existed. In one concrete case a temporary working tree,
`~/dev/seedsigner-lvgl-screens-overlay`, was scanned during the day, then
deleted. The day's `tools` row stayed `high` afterward — driven entirely by one
uncommitted file (`build-test/test_runner_core`, 5228 LoC) that the overlay had
contributed before it was removed.

## Root cause

The scanner is **add/refresh-only**. `local_trees.discover_repos` walks the
configured `scan_roots` and records every directory containing `.git`. A repo
that's been deleted (or moved, or de-initialized) is simply never visited again
— but nothing ever removes its rows. So its `projects` row plus all of its
`commits` and `file_touches` persist, and `recompute_daily_tiers` reads from the
full set of `projects` every run. Result: a vanished repo keeps contributing to
the heatmap indefinitely.

This also silently accumulates over time. When the immediate fix ran, three
*other* stale orphans surfaced that had been lingering for ~6 weeks — top-level
dirs (`~/dev/claude-config`, `~/dev/hardware-kb`, `~/dev/meal-tracker`) that had
been relocated under `tools/` and `misc/` back in early May. Their old rows had
just sat there since.

## Why it wasn't obvious

- The scan is idempotent for *existing* repos (the whole design goal of the
  causal banding + upsert pattern), which creates a false sense that "the DB
  always reflects reality." It only reflects reality for repos still on disk.
- `last_seen_at` *looks* like it should handle this — it's updated to the scan's
  start timestamp only for repos found this run — but nothing acted on a stale
  `last_seen_at`. It was diagnostic, not enforced.
- Shared git history masks the commit side. The overlay's commits were all
  deduped (by logical-commit identity) against the real `seedsigner-lvgl-screens`
  repo, so removing the overlay changed *no* commit counts — only the
  uncommitted-file dimension, which is per-repo and not deduped, exposed it.

## Fix

`storage.prune_missing_local_trees(conn)` runs each scan (step "5c", before
`recompute_daily_tiers`). It drops any `source='local_tree'` project whose
`<path>/.git` no longer exists — the **same** "is this a repo?" test
`discover_repos` uses, so discovery and pruning agree on what counts as a live
repo. It cascades to the project's `commits` and `file_touches` (children first;
`foreign_keys=ON`) and returns the pruned `(id, path)` pairs for logging.

## Critical scoping detail

The prune is restricted to `source='local_tree'`. **Personal-fork projects must
be excluded**: their `path` points at a *bare* clone in the forks cache
(`~/.cache/.../forks/kdmukai__*.git`), which has no nested `.git`, so the
`<path>/.git` test would false-positive and wipe every personal fork on the
first run. Personal forks are governed by the `personal_forks` config list, not
by working-tree presence — a different lifecycle entirely.

## Verification notes

- Idempotent: a second `prune_missing_local_trees` on a clean DB returns `[]`.
- After pruning the overlay, the `seedsigner` row for the day was unchanged
  (commits deduped to the real repo) and the spurious `tools` row disappeared
  (the overlay was the only `tools` activity that day).
