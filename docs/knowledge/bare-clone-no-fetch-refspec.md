# `git clone --bare` doesn't set a fetch refspec

## Symptom

A personal fork is listed in `personal_forks` and the bare clone exists at
`~/.cache/personal-dashboard/dev-activity/forks/<owner>__<repo>.git`. New
commits land on the GitHub remote (visible via `gh api repos/<owner>/<repo>/branches/<branch>`),
but the dev-activity report never picks them up. The bare clone's
`refs/heads/<branch>` stays frozen at whatever value it had when the clone
was first created.

`git fetch --all --prune` runs without error in the cache and reports
something like:

```
From https://github.com/<owner>/<repo>
 * branch            HEAD       -> FETCH_HEAD
```

…and that's it. No branch updates, even though branches have advanced upstream.

## Root cause

`git clone --bare <url>` creates a bare repo with `remote.origin.url` set
but **does not write a `remote.origin.fetch` refspec**. Compare:

| Command          | Sets `remote.origin.fetch`?            |
|------------------|----------------------------------------|
| `git clone`      | `+refs/heads/*:refs/remotes/origin/*`  |
| `git clone --bare`   | (nothing — empty)                  |
| `git clone --mirror` | `+refs/*:refs/*` (and `mirror=true`) |

Without a refspec, `git fetch` falls back to fetching the remote's HEAD
into `FETCH_HEAD` only. It never updates `refs/heads/*`, so branch tips
in the bare clone silently freeze at clone time. `--all` doesn't help —
it iterates remotes, but each remote still needs its own refspec.

This is documented behavior, but it's a footgun: the clone "works",
fetches don't error, and the freeze is invisible until you compare a ref
against the upstream.

## Fix

In `pd_dev_activity/sources/personal_forks.py::ensure_bare_clone` we set
the refspec explicitly after clone (and idempotently before each fetch
to migrate pre-existing caches):

```python
git -C <cache>.git config remote.origin.fetch '+refs/heads/*:refs/heads/*'
git -C <cache>.git fetch origin --prune
```

We use `+refs/heads/*:refs/heads/*` (branches only) instead of `--mirror`'s
`+refs/*:refs/*` because we don't want `refs/pull/*`, `refs/notes/*`, etc.
For a personal fork the dev-activity scanner only cares about branch tips.

The leading `+` allows non-fast-forward updates, which matters for forks
where the user occasionally rebases or force-pushes a branch.

## Recovery for existing caches

The fix in code handles new clones and migrates existing ones at next
fetch. If you need to recover immediately without waiting for the next
scheduled scan, run:

```bash
for cache in ~/.cache/personal-dashboard/dev-activity/forks/*.git; do
  git -C "$cache" config remote.origin.fetch '+refs/heads/*:refs/heads/*'
  git -C "$cache" fetch origin --prune
done
```

Then trigger a scan so the new commits get parsed into the DB.

## Why this surfaced when it did

The dev-activity scanner has been running for weeks against these caches
without anyone noticing. The bare clones were created at a point when the
relevant branches happened to already have their interesting commits, so
"frozen at clone time" looked the same as "up to date" — until a
genuinely new commit landed on a personal fork's branch (kdmukai/embit
bip47, 2026-05-08) and visibly failed to appear in the report.
