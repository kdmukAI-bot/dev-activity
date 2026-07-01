# Excluding machine-generated config (sdkconfig) from LOC activity

## Problem
The MicroPython-builder project's activity showed **3254 lines** of "work" on a
single day, all from one file: `apps/qr_overlay_test/sdkconfig`. That file is an
**untracked**, ESP-IDF-generated config whose own header reads
`# Automatically generated file. DO NOT EDIT.` It was picked up by
`scan_uncommitted()` (untracked files get their full newline count as
`loc_effort`), inflating the day's totals with zero human effort.

## Why the existing filter missed it
`local_trees.py` already had noise-filtering, but only two mechanisms:
- `_EXCLUDED_FILE_SUFFIXES` — `path.lower().endswith(suffix)` (e.g. `.db`, `.png`).
- `_path_in_excluded_dir` / `_SKIP_DIR_NAMES` — directory-component match
  (`build/`, `node_modules/`, ...).

A generated `sdkconfig` defeats **both**:
- It has **no extension**, so suffix matching can't target it.
- It lives in a normal source dir (`apps/<x>/sdkconfig`), not under `build/`.
  (The copies under `build/.../` *were* already excluded via the `build` dir
  rule — which is why only the app-dir copy leaked through.)

## The subtlety: don't clobber the hand-authored siblings
You cannot just match `startswith("sdkconfig")`. The ESP-IDF file family splits
cleanly into machine output vs. human input:

| Basename | Origin | Count it? |
|---|---|---|
| `sdkconfig` | generated (expanded from defaults) | **no** |
| `sdkconfig.old` | menuconfig backup of previous generated config | **no** |
| `sdkconfig.combined` | builder-merged generated config | **no** |
| `sdkconfig.defaults`, `sdkconfig.defaults.*` | hand-authored input | **yes** |
| `sdkconfig.board` | hand-authored board config | **yes** |
| `sdkconfig.ci`, `sdkconfig.landscape` | hand-authored variants | **yes** |
| `sdkconfig.combined.in` | hand-authored template | **yes** |
| `sdkconfig.rename*` | ESP-IDF upstream (author-filtered anyway) | n/a |

So the fix is a third mechanism: `_EXCLUDED_FILE_BASENAMES`, matched against the
**exact** lowercased basename. Exact-match keeps every `sdkconfig.<suffix>`
variant (all hand-authored) counting while dropping the bare generated files.
Both the committed path (`parse_git_log`) and the WIP path (`scan_uncommitted`)
route through `_is_excluded_file`, so one edit covers both.

## Cleanup was automatic
`scan_local_tree()` wipes and rebuilds **today's** `file_touches` for each repo
on every run, so re-running the scan dropped the row immediately. An audit of
all prior days (across all 33 local repos and 6 fork bare-clones) found **no**
other generated-`sdkconfig` rows in `file_touches` and **no** commit in any
history that ever touched a bare `sdkconfig` — the only tracked variants are the
hand-authored `.board`/`.ci`/`.defaults` files. Nothing was baked into the
`commits` aggregates, so no historical backfill was needed.

## Same category: dependency lockfiles
The identical reasoning was extended to package-manager lockfiles (the day's WIP
also carried a 338-line ESP-IDF `dependencies.lock`). Lockfiles are regenerated
by tooling, never hand-authored, and split across both matching mechanisms:
- **Suffix** (`_EXCLUDED_FILE_SUFFIXES`): `.lock` (Cargo/poetry/uv/yarn/composer/
  Gemfile/Pipfile/flake/dependencies) and `.lockb` (bun, binary).
- **Exact basename** (`_EXCLUDED_FILE_BASENAMES`): `package-lock.json`,
  `npm-shrinkwrap.json`, `packages.lock.json`, `pnpm-lock.yaml` — these carry a
  `.json`/`.yaml` data extension, so a `.lock` suffix rule can't target them
  without also excluding real source.

## Takeaway
Generated artifacts come in three shapes for the filter: by-extension, by-dir,
and **extensionless-or-data-extension by-exact-name**. When a build tool emits a
config/lockfile with no telltale extension (`sdkconfig`) — or one that reuses a
data extension (`package-lock.json`) — only the exact-basename set can catch it,
and you must enumerate those basenames precisely so you don't sweep up the
hand-authored input files that share the same prefix (`sdkconfig.defaults`, a
real `config.json`, etc.).
