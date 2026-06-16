# Causal expanding-window banding (tiers don't degrade as history grows)

## The problem

`recompute_daily_tiers` runs every scan and rewrites the whole `daily_tiers`
table. The original implementation built one statistical bander per
`(category, dimension)` from the **entire** per-category distribution, then
applied that single bander to **every** day. Because mean/stdev (and the
percentile cuts) are computed over all history *including future days relative
to any given past day*, a day's tier was a function of data that didn't exist
yet when that day happened.

Concrete symptom: the 2026 "bot era" (kdmukAI-bot driving high-volume daily
work across the SeedSigner/ESP32/LVGL repos) pushed the seedsigner
distribution's mean and stdev way up. That raised the `moderate`/`high`
thresholds for *all* days, so genuinely busy days from 2021–2023 silently
decayed from `high` to `moderate`. Measured on the real DB: **61** days that
were real high-effort days had been demoted purely by distribution growth.

This was a known, documented caveat ("tier ratings shift as the historical
distribution grows") — the fix removes it.

## The fix: replay history causally

Band each day against only the days **up to and including itself** (per
category). `recompute_daily_tiers` now sorts each category's days
chronologically and, for day *D*, builds the dimension's bander from the
prefix `days <= D` before scoring *D*. A day's tier therefore reflects how it
compared to everything known *at that point in time*, and **future activity
can never degrade a past day**.

### Why it's still a full rebuild every scan (and why that's fine)

We still `DELETE FROM daily_tiers` and recompute all rows each scan, because
recent days' raw counts keep settling inside the scan window (the `commits`
table ingests `--since=7d`, the strict-commit `active_on_commit_day` flag can
flip when files are edited again later, and `file_touches` are rebuilt). But:

- For a **settled** day, both its raw counts and its causal window (`days <= D`)
  are fixed, so its tier is stable — the rebuild is idempotent for it. This is
  the "lock-in" the feature promises, achieved without persisting a separate
  frozen column.
- A settled day's window **excludes** all later days, so a future burst of
  activity cannot touch it. (Verified: re-running `recompute` back-to-back
  yields byte-identical tiers.)
- Backfilling an *older* repo (adding history *before* existing days) can
  legitimately change those older days — that's new information about the past,
  not the future-degradation we were eliminating, and the causal windows fold
  it in correctly.

### Cost

O(N²) per category in the naive per-day bander rebuild, but N is days-per-
category (~920 for seedsigner) and the bander build is cheap, so the full
replay over ~980 day-rows runs in ~230 ms. No need for incremental
accumulators.

## Warmup protection (cold-start)

A bander can't make real distinctions until it has enough non-zero days to
leave its fallback (the `_bander_min_nonzero` thresholds: 7 for the standard
4-band, 4 for personal-commit Q3, 3 for lens/telegram). Under a *strict*
causal window the earliest days of any young category/dimension would be
judged on too little data and pinned to the flat fallback — e.g. the `tools`
and `other` categories (born March 2026) would spend their first ~7 active
days stuck at `low`, dropping currently-`high` days to `low`.

So warmup days borrow the **first mature window**: day *D* is scored against
`days <= max(D, D0)`, where `D0` is the day the dimension first reaches its
threshold. This is a **bounded, one-time look-ahead limited to the warmup
block**; at and after `D0`, banding is purely causal. It eliminates the
`high → low` cold-start cliff (measured: strict causal dropped 10 currently-
high days; warmup-protected drops only 3, all defensible `high → moderate`
cases where the opening day really was modest next to its launch week).

## Onset bump

A repo's earliest activity day is floored to `high` — see
[new-repo-detection.md](new-repo-detection.md) for why it's un-gated and fires
only on real activity days (so repos you *joined* don't paint spurious cells).

## Net effect on the real DB

Re-banding the live history (min_nonzero_days = 7): **81 days up, 13 down**
vs. the old global banding; **68** days restored/bumped to `high`; only **3**
currently-`high` days drop (all `high → moderate`); row set unchanged (no
spurious or lost cells); idempotent on re-run.

## If you ever need to tune

- The `_bander_min_nonzero` thresholds **must** stay in sync with the
  `len(nonzero) < N` guards inside the matching `_band_assigner*` functions —
  they encode the same warmup boundary from two angles.
- `_bander_for` is the single dispatch point for "which bander does this
  dimension use"; keep banding-strategy changes there so the causal replay and
  any future caller agree.
