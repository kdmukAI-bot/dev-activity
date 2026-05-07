# Fresh-repo (no HEAD) edge case in `scan_uncommitted`

## Symptom

A newly-`git init`'d repo with files staged for an initial commit but no commits yet shows up in `projects` (the scanner discovers it correctly) but contributes zero `commits` rows and zero `file_touches` rows — so it's invisible on the heatmap and day-detail view despite the user having staged real work.

## Root cause

`scan_uncommitted` in `pd_dev_activity/sources/local_trees.py` runs `git diff HEAD --numstat` to build a `diff_map: path → (insertions, deletions)` for tracked-modified files. In a fresh repo, **HEAD doesn't exist yet**, and that command exits non-zero with:

```
fatal: ambiguous argument 'HEAD': unknown revision or path not in the working tree.
```

The function caught the failure but left `diff_map` empty. Then in the porcelain loop:

- All staged files appear with code `A ` (added/staged), not `??` (untracked).
- The check `is_untracked = xy.startswith("??")` evaluates **False**, so the code falls through to the tracked-modified branch.
- `diff_map.get(path, (0, 0))` returns `(0, 0)` because the diff failed.
- `loc_effort = max(0, 0) = 0`.
- The `if loc_effort == 0: continue` guard drops the file.

Net result: every staged file in a fresh repo is silently dropped.

## Fix

Detect the no-HEAD case explicitly before the diff and treat porcelain entries as untracked-equivalent (count full file line counts, same as `??` entries). The HEAD check uses `git rev-parse --verify HEAD` and runs once per repo:

```python
has_head = subprocess.run(
    ["git", "-C", str(repo_path), "rev-parse", "--verify", "HEAD"],
    capture_output=True, check=False, timeout=10,
).returncode == 0
# ...
is_untracked = xy.startswith("??") or not has_head
```

In a fresh repo, every file is in fact "new" — there's nothing to diff against — so counting all of them as untracked-equivalent is semantically correct.

## Why this stayed hidden

The scanner already runs daily on the user's actual dev tree, but every long-lived repo there has a HEAD. The bug only surfaces when a brand-new repo is added before the user makes their first commit. With `home-assistant-config` (initial commit not yet made for ~2 hours), the data was actively being lost.

## Test it

```bash
# Create a fresh repo, stage a file, scan, verify it shows up.
mkdir /tmp/fresh-test && cd /tmp/fresh-test
git init -b main
echo "hello" > a.txt
git add a.txt

# Then run the scanner against a config that includes /tmp as a scan_root,
# and check `SELECT * FROM file_touches WHERE day=CURRENT_DATE;` —
# the staged file should appear with non-zero loc_effort.
```
