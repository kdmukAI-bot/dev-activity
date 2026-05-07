"""SQLite storage layer for the dev-activity module.

Mirrors the WAL connection pattern used by other personal-dashboard modules.
Schema and queries here are shared by both the analyzer (read path) and the
scanner (write path).
"""

from __future__ import annotations

import os
import sqlite3
import statistics
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY,
    path         TEXT UNIQUE NOT NULL,
    name         TEXT NOT NULL,
    remote_url   TEXT,
    category     TEXT NOT NULL,
    source       TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commits (
    sha                   TEXT NOT NULL,
    project_id            INTEGER NOT NULL REFERENCES projects(id),
    author_date           TEXT NOT NULL,
    author_iso            TEXT NOT NULL,
    author_name           TEXT NOT NULL,
    author_email          TEXT NOT NULL,
    message               TEXT NOT NULL,
    files_changed         INTEGER NOT NULL,
    lines_added           INTEGER NOT NULL,
    lines_deleted         INTEGER NOT NULL,
    loc_effort            INTEGER NOT NULL,
    active_on_commit_day  INTEGER NOT NULL,
    PRIMARY KEY (sha, project_id)
);
CREATE INDEX IF NOT EXISTS commits_by_day ON commits(author_date, active_on_commit_day);
CREATE INDEX IF NOT EXISTS commits_by_project ON commits(project_id);

CREATE TABLE IF NOT EXISTS file_touches (
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    day          TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    mtime_iso    TEXT NOT NULL,
    loc_effort   INTEGER NOT NULL,
    PRIMARY KEY (project_id, day, file_path)
);
CREATE INDEX IF NOT EXISTS file_touches_by_day ON file_touches(day);

CREATE TABLE IF NOT EXISTS lens_events (
    id           INTEGER PRIMARY KEY,
    day          TEXT NOT NULL,
    kind         TEXT NOT NULL,
    repo         TEXT NOT NULL,
    number       INTEGER NOT NULL,
    title        TEXT,
    actor_login  TEXT NOT NULL,
    ts_iso       TEXT NOT NULL,
    UNIQUE(kind, repo, number, ts_iso, actor_login)
);
CREATE INDEX IF NOT EXISTS lens_by_day ON lens_events(day);

CREATE TABLE IF NOT EXISTS telegram_activity (
    day       TEXT NOT NULL,
    user_id   BIGINT NOT NULL,
    msg_count INTEGER NOT NULL,
    PRIMARY KEY (day, user_id)
);
CREATE INDEX IF NOT EXISTS telegram_by_day ON telegram_activity(day);

CREATE TABLE IF NOT EXISTS daily_tiers (
    day                          TEXT NOT NULL,
    category                     TEXT NOT NULL,
    -- For 'seedsigner', commits are split by author identity. personal_fork
    -- commits (kdmukai user) represent deliberate work pushed to the user's
    -- public forks; bot/local-tree commits (kdmukAI-bot) represent the bulk
    -- of routine autonomous work. Each gets its own banding strategy. For
    -- 'tools' and 'other', personal-fork pushes don't apply, so n_commits
    -- (= personal + bot, in practice all bot) is what feeds the overall
    -- tier and the per_personal/per_bot dimensions are populated but unused.
    n_commits                    INTEGER NOT NULL,
    n_commits_personal           INTEGER NOT NULL,
    n_commits_bot                INTEGER NOT NULL,
    n_committed_files            INTEGER NOT NULL,
    n_committed_loc_effort       INTEGER NOT NULL,
    n_uncommitted_files          INTEGER NOT NULL,
    n_uncommitted_loc_effort     INTEGER NOT NULL,
    n_lens_review                INTEGER NOT NULL,
    n_lens_discussion            INTEGER NOT NULL,
    n_telegram_msgs              INTEGER NOT NULL,
    tier_commits                 TEXT NOT NULL,
    tier_commits_personal        TEXT NOT NULL,
    tier_commits_bot             TEXT NOT NULL,
    tier_committed_files         TEXT NOT NULL,
    tier_committed_loc_effort    TEXT NOT NULL,
    tier_uncommitted_files       TEXT NOT NULL,
    tier_uncommitted_loc_effort  TEXT NOT NULL,
    tier_lens_review             TEXT NOT NULL,
    tier_lens_discussion         TEXT NOT NULL,
    tier_telegram_msgs           TEXT NOT NULL,
    overall_tier                 TEXT NOT NULL,
    PRIMARY KEY (day, category)
);
CREATE INDEX IF NOT EXISTS daily_tiers_by_day ON daily_tiers(day);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# Tier ordering, lowest to highest. The overall daily tier is max(per-dim tiers).
# 'trace' sits below 'low' for sub-low signals (currently telegram chatter): it
# registers presence on the heatmap without competing visually with real
# code-work days.
TIER_ORDER = ("none", "trace", "low", "moderate", "high")
TIER_RANK = {t: i for i, t in enumerate(TIER_ORDER)}


# Per-dimension tier discount applied AFTER std-dev banding and BEFORE the
# max-wins composition. The discounted tier is what gets stored and what
# participates in overall_tier. Use to dial down a low-quality signal so it
# only contributes when it's exceptionally strong.
#
# Telegram group chatter is low-quality vs committed code work, so a "high"
# Telegram day is treated as merely "moderate" toward the day's overall
# rating, "moderate" becomes "low", "low" becomes "none". Tweak this dict
# (or move to config.toml) as the data shapes settle in.
DIMENSION_DISCOUNTS: dict[str, int] = {
    # Empty by default. Keys are dimension column names (e.g. "n_lens_events");
    # values are tier levels to subtract after natural banding. Telegram used
    # to live here (-1) but now uses its own custom banding (see
    # _band_assigner_telegram) — a 3-tier tertile split that maps to
    # 'trace' / 'low' / 'moderate' and natively caps at 'moderate', so a
    # post-hoc discount isn't needed.
}


# Telegram messages get their own statistical banding instead of the generic
# std-dev one. Three tiers above zero (trace / low / moderate; no "high"),
# split at tertiles of the non-zero distribution. Telegram is never strong
# enough to dominate as "high".
#
# We deliberately keep no noise floor — a single message still registers, but
# lands in 'trace' (a sub-low band rendered as a barely-visible tint) so
# pure-chatter days don't read as real activity on the heatmap.


def _apply_discount(tier: str, dim: str) -> str:
    discount = DIMENSION_DISCOUNTS.get(dim, 0)
    if discount <= 0:
        return tier
    return TIER_ORDER[max(0, TIER_RANK[tier] - discount)]


def default_db_path() -> Path:
    """Per the plan, the canonical on-disk location of the module DB.

    The personal-dashboard `module_data_dir(name)` helper returns
    `~/.local/share/personal-dashboard/modules/<name>/`, but the plan
    asks the scanner CLI to hardcode the same path so it can run without
    the dashboard core. We honor the dashboard's actual XDG-data location
    so analyzer + scanner write/read the same DB.
    """
    return (
        Path.home()
        / ".local"
        / "share"
        / "personal-dashboard"
        / "modules"
        / "dev-activity"
        / "data.db"
    )


@contextmanager
def connect(db_path: os.PathLike | str | None = None) -> Iterator[sqlite3.Connection]:
    """Open the module DB with WAL + foreign keys + sane timeouts.

    Creates the parent directory and applies the schema on first open.
    """
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        # Migrations: drop tables when their shape has changed under us so
        # they get rebuilt fresh by the scanner.
        # - daily_tiers: rebuilt every recompute, safe to drop.
        # - lens_events: when the kind taxonomy changes (e.g. splitting
        #   pr_comment into pr_review_comment + pr_comment), old rows would
        #   sit alongside the new emissions and double-count. Wipe and re-scan.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(daily_tiers)")}
        schema_outdated = existing and (
            "n_telegram_msgs" not in existing
            or "n_lens_review" not in existing
            or "n_commits_personal" not in existing
            or "n_commits" not in existing
        )
        if schema_outdated:
            conn.execute("DROP TABLE daily_tiers")
            # If the lens_events table exists, wipe its rows so the next
            # lens scan repopulates with the current kind taxonomy. The
            # scanner uses INSERT OR IGNORE so dupes wouldn't add but old
            # rows wouldn't be reclassified either.
            try:
                conn.execute("DELETE FROM lens_events")
            except sqlite3.OperationalError:
                pass
        conn.executescript(SCHEMA)
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- meta helpers -----------------------------------------------------------

def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# --- projects ---------------------------------------------------------------

def upsert_project(
    conn: sqlite3.Connection,
    *,
    path: str,
    name: str,
    remote_url: str | None,
    category: str,
    source: str,
    last_seen_at: str,
) -> int:
    conn.execute(
        """
        INSERT INTO projects(path, name, remote_url, category, source, last_seen_at)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            name=excluded.name,
            remote_url=excluded.remote_url,
            category=excluded.category,
            source=excluded.source,
            last_seen_at=excluded.last_seen_at
        """,
        (path, name, remote_url, category, source, last_seen_at),
    )
    row = conn.execute("SELECT id FROM projects WHERE path=?", (path,)).fetchone()
    return int(row["id"])


# --- commits ---------------------------------------------------------------

def upsert_commit(
    conn: sqlite3.Connection,
    *,
    sha: str,
    project_id: int,
    author_date: str,
    author_iso: str,
    author_name: str,
    author_email: str,
    message: str,
    files_changed: int,
    lines_added: int,
    lines_deleted: int,
    loc_effort: int,
    active_on_commit_day: int,
) -> None:
    conn.execute(
        """
        INSERT INTO commits(
            sha, project_id, author_date, author_iso, author_name, author_email,
            message, files_changed, lines_added, lines_deleted, loc_effort,
            active_on_commit_day
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(sha, project_id) DO UPDATE SET
            author_date=excluded.author_date,
            author_iso=excluded.author_iso,
            author_name=excluded.author_name,
            author_email=excluded.author_email,
            message=excluded.message,
            files_changed=excluded.files_changed,
            lines_added=excluded.lines_added,
            lines_deleted=excluded.lines_deleted,
            loc_effort=excluded.loc_effort,
            active_on_commit_day=excluded.active_on_commit_day
        """,
        (
            sha, project_id, author_date, author_iso, author_name, author_email,
            message, files_changed, lines_added, lines_deleted, loc_effort,
            active_on_commit_day,
        ),
    )


# --- file_touches ----------------------------------------------------------

def upsert_file_touch(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    day: str,
    file_path: str,
    mtime_iso: str,
    loc_effort: int,
) -> None:
    conn.execute(
        """
        INSERT INTO file_touches(project_id, day, file_path, mtime_iso, loc_effort)
        VALUES(?,?,?,?,?)
        ON CONFLICT(project_id, day, file_path) DO UPDATE SET
            mtime_iso=excluded.mtime_iso,
            loc_effort=excluded.loc_effort
        """,
        (project_id, day, file_path, mtime_iso, loc_effort),
    )


# --- lens_events -----------------------------------------------------------

def insert_lens_event(
    conn: sqlite3.Connection,
    *,
    day: str,
    kind: str,
    repo: str,
    number: int,
    title: str | None,
    actor_login: str,
    ts_iso: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO lens_events(day, kind, repo, number, title, actor_login, ts_iso)
        VALUES(?,?,?,?,?,?,?)
        """,
        (day, kind, repo, number, title, actor_login, ts_iso),
    )


# --- telegram_activity -----------------------------------------------------

def upsert_telegram_activity(
    conn: sqlite3.Connection,
    *,
    day: str,
    user_id: int,
    msg_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO telegram_activity(day, user_id, msg_count)
        VALUES(?,?,?)
        ON CONFLICT(day, user_id) DO UPDATE SET msg_count=excluded.msg_count
        """,
        (day, user_id, msg_count),
    )


# --- daily_tiers -----------------------------------------------------------

def _tier_for_value(value: int, mean: float, stdev: float) -> str:
    if value <= 0:
        return "none"
    if value <= mean:
        return "low"
    if value <= mean + stdev:
        return "moderate"
    return "high"


def _band_assigner(values: Iterable[int], min_nonzero_days: int):
    """Build a (value -> tier) function for one (category, dimension)."""
    nonzero = [v for v in values if v and v > 0]
    if len(nonzero) < min_nonzero_days:
        # Fallback: any non-zero -> "low".
        def assign(value: int) -> str:
            return "none" if value <= 0 else "low"
        return assign

    mean = statistics.fmean(nonzero)
    stdev = statistics.pstdev(nonzero) if len(nonzero) > 1 else 0.0

    def assign(value: int) -> str:
        return _tier_for_value(value, mean, stdev)

    return assign


def _band_assigner_telegram(values: Iterable[int]):
    """Custom 3-tier banding for telegram message counts.

    - 0 messages → 'none'.
    - Above 0: tertile split → 'trace' (≤ p33) / 'low' (≤ p67) / 'moderate' (> p67).
    - Never produces 'high' — telegram is a low-quality signal that should
      never dominate the day's overall tier under max-wins composition.

    With no noise floor, a single-message day still counts — but lands in
    'trace' rather than 'low', so pure-chatter days stay barely visible on
    the heatmap without overstating activity.
    """
    nonzero = sorted(v for v in values if v and v > 0)
    if len(nonzero) < 3:
        def assign(value: int) -> str:
            if value <= 0:
                return "none"
            return "trace"
        return assign

    p33_idx = max(0, int(len(nonzero) * 1 / 3) - 1)
    p67_idx = max(0, int(len(nonzero) * 2 / 3) - 1)
    p33 = nonzero[p33_idx]
    p67 = nonzero[p67_idx]

    def assign(value: int) -> str:
        if value <= 0:
            return "none"
        if value <= p33:
            return "trace"
        if value <= p67:
            return "low"
        return "moderate"

    return assign


def _band_assigner_two_band_q3(values: Iterable[int]):
    """2-tier banding (none/moderate/high) with the moderate→high split at
    Q3 (75th percentile) of the non-zero distribution. Used for n_commits.

    Median-based 2-band put ~50% of commit-active days into 'high', which
    washed out the heatmap. Q3 caps 'high' at ~25% of commit days while
    still guaranteeing every commit day is at least 'moderate'. (Easy swap
    to mean+stdev later if a less skew-resistant cut is preferred.)
    """
    nonzero = sorted(v for v in values if v and v > 0)
    if len(nonzero) < 4:
        def assign(value: int) -> str:
            return "none" if value <= 0 else "moderate"
        return assign

    q3_idx = max(0, int(len(nonzero) * 3 / 4) - 1)
    q3 = nonzero[q3_idx]

    def assign(value: int) -> str:
        if value <= 0:
            return "none"
        if value <= q3:
            return "moderate"
        return "high"

    return assign


def _band_assigner_lens(values: Iterable[int]):
    """Custom 2-tier banding for lens (PR/issue) engagement.

    PR review and issue/PR comment activity is always at least moderate
    effort, so we skip the 'low' bucket entirely. Median split decides
    moderate vs high.

    - 0 events                       → 'none'
    - 1 to median(nonzero days)      → 'moderate'
    - > median                       → 'high'
    """
    nonzero = sorted(v for v in values if v and v > 0)
    if len(nonzero) < 3:
        def assign(value: int) -> str:
            return "none" if value <= 0 else "moderate"
        return assign

    median = statistics.median(nonzero)

    def assign(value: int) -> str:
        if value <= 0:
            return "none"
        if value <= median:
            return "moderate"
        return "high"

    return assign


def classify_local_repo(
    repo_path: Path,
    name: str,
    *,
    seedsigner_repos: Iterable[str],
    other_dirs: Iterable[str],
) -> str:
    """Classify a local-tree repo as 'seedsigner', 'other', or 'tools'.

    Order matters: seedsigner_repos by name takes precedence over directory
    placement (so e.g. ~/dev/tools/project-lens stays 'seedsigner'). Path
    matching uses directory-component containment via Path.is_relative_to,
    so '~/dev/misc' won't false-match '~/dev/misc-anything-else'.
    """
    if name in set(seedsigner_repos or []):
        return "seedsigner"
    try:
        repo_resolved = repo_path.resolve()
    except OSError:
        return "tools"
    for d in other_dirs or []:
        try:
            other_resolved = Path(d).expanduser().resolve()
        except OSError:
            continue
        try:
            if repo_resolved.is_relative_to(other_resolved):
                return "other"
        except ValueError:
            continue
    return "tools"


def lens_repo_category(repo: str, seedsigner_repos: Iterable[str]) -> str:
    """Classify a lens event's `owner/repo` string into 'seedsigner' or 'tools'.

    Two conditions promote a repo to 'seedsigner':
      - it lives under the SeedSigner GitHub org (any repo there is by
        definition SeedSigner work, even if not explicitly listed); OR
      - its basename appears in `seedsigner_repos` (catches dependencies the
        user treats as SeedSigner work — e.g. the upstream `embit` library
        under `diybitcoinhardware/embit`).

    Lens events come without local paths, so we can't apply directory-based
    'other' classification — anything non-seedsigner falls through to 'tools'.
    Project-lens is a SeedSigner-engagement DB so personal-project lens events
    aren't expected anyway.
    """
    if not repo:
        return "tools"
    if repo.startswith("SeedSigner/"):
        return "seedsigner"
    basename = repo.split("/", 1)[-1]
    if basename in set(seedsigner_repos or []):
        return "seedsigner"
    return "tools"


def recompute_daily_tiers(
    conn: sqlite3.Connection,
    min_nonzero_days: int,
    seedsigner_repos: Iterable[str] | None = None,
) -> None:
    """Truncate daily_tiers and rebuild from raw tables.

    Per the plan: tiers shift as the distribution grows; full rebuild every scan.
    """
    ss_repos_list = sorted(set(seedsigner_repos or []))
    # Aggregate per (day, category) from the three source tables.
    # We build a dict in Python because the joins+grouping are easier to read,
    # and it's all small enough that we don't need pure-SQL gymnastics.

    # commits aggregations — only "active" commits count
    by_dc: dict[tuple[str, str], dict[str, int]] = {}

    def _bucket(day: str, category: str) -> dict[str, int]:
        key = (day, category)
        bucket = by_dc.get(key)
        if bucket is None:
            bucket = {
                "n_commits": 0,
                "n_commits_personal": 0,
                "n_commits_bot": 0,
                "n_committed_files": 0,
                "n_committed_loc_effort": 0,
                "n_uncommitted_files": 0,
                "n_uncommitted_loc_effort": 0,
                "n_lens_review": 0,
                "n_lens_discussion": 0,
                "n_telegram_msgs": 0,
            }
            by_dc[key] = bucket
        return bucket

    # Dedup by LOGICAL commit identity (author_iso + message + author_name +
    # files_changed + loc_effort), not by SHA. This catches both:
    #   - same SHA in local_tree AND personal_fork (fast-forward merge), AND
    #   - DIFFERENT SHAs for the same logical commit (rebase / cherry-pick
    #     produces a new SHA upstream while the original SHA still lives in
    #     the personal fork).
    # Among matching rows, prefer the LOCAL-TREE (bot) row. The bot account
    # drives the bulk of active development across SeedSigner/ESP32/LVGL
    # repos, so a commit that exists in both is canonically a bot commit.
    # The personal_fork dimension is reserved for commits that exist ONLY
    # in the user's personal kdmukai fork — true hands-on manual work.
    # Classify by COMMIT AUTHOR, not by repo source. The bot account
    # (kdmukAI-bot) didn't exist until 2026-02, so historical local-tree
    # commits in those years were all human-authored — attributing them by
    # repo source would be wrong. We split by author identity:
    #   - n_bot      = commits whose author matches "kdmukAI-bot"
    #   - n_personal = commits whose author matches "kdmukai" but NOT bot
    #   - anything else (shouldn't occur given git_author_substrings filter)
    #     is neither, defensively dropped from both dimensions.
    #
    # The strict-commit rule (`active_on_commit_day=1`) requires a file in
    # the commit to still have an mtime on the author-date — the assumption
    # being that work attributed to the actual edit-day already lives in
    # file_touches. But we only started capturing file_touches when the
    # scanner first ran; pre-scanning commits have no compensating
    # file_touches record, so the strict rule would silently drop them.
    # We OR the active flag with `author_date < first_scan_at` so historical
    # commits count on their author-date.
    first_scan_at = get_meta(conn, "first_scan_at") or ""
    cur = conn.execute(
        """
        WITH ranked AS (
            SELECT
                c.author_date,
                p.category,
                c.author_name,
                c.author_email,
                c.files_changed,
                c.loc_effort,
                -- Take the MAX active flag across all rows for the same
                -- logical commit. Bot's local_tree row may have active=0
                -- (mtime moved past commit-day after later edits) while
                -- personal_fork rows are unconditionally active=1; we
                -- want the commit to count if ANY source has active=1.
                MAX(c.active_on_commit_day) OVER (
                    PARTITION BY c.author_iso, c.message, c.author_name,
                                 c.files_changed, c.loc_effort
                ) AS group_active,
                -- Pre-scanning commits get a free pass on the active check.
                CASE WHEN c.author_date < substr(?, 1, 10)
                     THEN 1 ELSE 0 END AS pre_scanning,
                ROW_NUMBER() OVER (
                    PARTITION BY c.author_iso, c.message, c.author_name,
                                 c.files_changed, c.loc_effort
                    ORDER BY CASE p.source WHEN 'local_tree' THEN 0 ELSE 1 END,
                             p.id
                ) AS rn
            FROM commits c
            JOIN projects p ON p.id = c.project_id
        )
        SELECT author_date AS day, category,
               SUM(CASE
                   WHEN (author_name LIKE '%kdmukai%' OR author_email LIKE '%kdmukai%')
                    AND author_name  NOT LIKE '%kdmukAI-bot%'
                    AND author_email NOT LIKE '%kdmukAI-bot%'
                   THEN 1 ELSE 0
               END) AS n_personal,
               SUM(CASE
                   WHEN author_name  LIKE '%kdmukAI-bot%'
                     OR author_email LIKE '%kdmukAI-bot%'
                   THEN 1 ELSE 0
               END) AS n_bot,
               COALESCE(SUM(files_changed), 0) AS n_files,
               COALESCE(SUM(loc_effort), 0)    AS n_loc
        FROM ranked
        WHERE rn = 1 AND (group_active = 1 OR pre_scanning = 1)
        GROUP BY author_date, category
        """,
        (first_scan_at,),
    )
    for row in cur:
        b = _bucket(row["day"], row["category"])
        b["n_commits_personal"] = int(row["n_personal"])
        b["n_commits_bot"] = int(row["n_bot"])
        b["n_commits"] = b["n_commits_personal"] + b["n_commits_bot"]
        b["n_committed_files"] = int(row["n_files"])
        b["n_committed_loc_effort"] = int(row["n_loc"])

    cur = conn.execute(
        """
        SELECT ft.day AS day, p.category AS category,
               COUNT(DISTINCT ft.file_path) AS n_files,
               COALESCE(SUM(ft.loc_effort), 0) AS n_loc
        FROM file_touches ft
        JOIN projects p ON p.id = ft.project_id
        GROUP BY ft.day, p.category
        """
    )
    for row in cur:
        b = _bucket(row["day"], row["category"])
        b["n_uncommitted_files"] = int(row["n_files"])
        b["n_uncommitted_loc_effort"] = int(row["n_loc"])

    # Split lens events into two dimensions:
    #   - lens_review: inline review comments (line-by-line code feedback —
    #     often volume-heavy on contentious PRs). The user doesn't use
    #     GitHub's "Submit Review" feature, so pr_review events are not
    #     emitted by the scanner.
    #   - lens_discussion: pr_comment + issue_comment + pr_open + issue_open
    #     (substantive engagement on PR conversation threads and issues,
    #     plus the act of opening one in the first place — at least as
    #     deliberate as commenting).
    # Lens repo classification: SeedSigner org always counts as 'seedsigner';
    # additionally, repos whose basename appears in seedsigner_repos count
    # (e.g. diybitcoinhardware/embit). Mirrors `lens_repo_category()`.
    if ss_repos_list:
        placeholders = ",".join("?" for _ in ss_repos_list)
        category_case = (
            "CASE "
            "WHEN le.repo LIKE 'SeedSigner/%' THEN 'seedsigner' "
            f"WHEN substr(le.repo, instr(le.repo, '/') + 1) IN ({placeholders}) "
            "THEN 'seedsigner' "
            "ELSE 'tools' END"
        )
        params: tuple = tuple(ss_repos_list)
    else:
        category_case = (
            "CASE WHEN le.repo LIKE 'SeedSigner/%' THEN 'seedsigner' ELSE 'tools' END"
        )
        params = ()
    cur = conn.execute(
        f"""
        SELECT le.day AS day,
               {category_case} AS category,
               SUM(CASE WHEN kind = 'pr_review_comment' THEN 1 ELSE 0 END) AS n_review,
               SUM(CASE
                     WHEN kind IN ('pr_comment','issue_comment','pr_open','issue_open')
                     THEN 1 ELSE 0
                   END) AS n_discussion
        FROM lens_events le
        GROUP BY le.day, category
        """,
        params,
    )
    for row in cur:
        b = _bucket(row["day"], row["category"])
        b["n_lens_review"] = int(row["n_review"])
        b["n_lens_discussion"] = int(row["n_discussion"])

    # Telegram messages — all credited to seedsigner (this is the SeedSigner
    # Telegram group). Sum across configured user_ids per day.
    cur = conn.execute(
        """
        SELECT day, SUM(msg_count) AS n
        FROM telegram_activity
        GROUP BY day
        """
    )
    for row in cur:
        b = _bucket(row["day"], "seedsigner")
        b["n_telegram_msgs"] = int(row["n"])

    # Build per (category, dimension) banders from the historical distribution.
    dim_keys = (
        "n_commits",
        "n_commits_personal",
        "n_commits_bot",
        "n_committed_files",
        "n_committed_loc_effort",
        "n_uncommitted_files",
        "n_uncommitted_loc_effort",
        "n_lens_review",
        "n_lens_discussion",
        "n_telegram_msgs",
    )

    banders: dict[tuple[str, str], any] = {}
    for category in ("seedsigner", "tools", "other"):
        for dim in dim_keys:
            values = [
                bucket[dim]
                for (_day, cat), bucket in by_dc.items()
                if cat == category
            ]
            if dim == "n_telegram_msgs":
                # Custom 3-tier banding (none/low/moderate, no high; noise floor).
                banders[(category, dim)] = _band_assigner_telegram(values)
            elif dim == "n_lens_discussion":
                # 2-tier (none/moderate/high), median split. Substantive
                # PR/issue conversation is always at least moderate effort.
                banders[(category, dim)] = _band_assigner_lens(values)
            elif dim == "n_commits_personal":
                # 2-tier (none/moderate/high), Q3 split. Any commit pushed to
                # the user's personal kdmukai forks is intentional public
                # work — at least moderate. Top quartile of fork-active days
                # hits 'high'.
                banders[(category, dim)] = _band_assigner_two_band_q3(values)
            else:
                # Standard 4-band for everything else. n_commits_bot tracks
                # routine kdmukAI-bot / local-tree work and benefits from
                # the full low / moderate / high spread (a 1-commit day is
                # genuinely a 'low' day for bot work, unlike fork pushes).
                banders[(category, dim)] = _band_assigner(values, min_nonzero_days)

    conn.execute("DELETE FROM daily_tiers")

    for (day, category), bucket in by_dc.items():
        tier_commits = banders[(category, "n_commits")](bucket["n_commits"])
        tier_commits_personal = banders[(category, "n_commits_personal")](
            bucket["n_commits_personal"]
        )
        tier_commits_bot = banders[(category, "n_commits_bot")](
            bucket["n_commits_bot"]
        )
        tier_committed_files = banders[(category, "n_committed_files")](
            bucket["n_committed_files"]
        )
        tier_committed_loc_effort = banders[(category, "n_committed_loc_effort")](
            bucket["n_committed_loc_effort"]
        )
        tier_uncommitted_files = banders[(category, "n_uncommitted_files")](
            bucket["n_uncommitted_files"]
        )
        tier_uncommitted_loc_effort = banders[(category, "n_uncommitted_loc_effort")](
            bucket["n_uncommitted_loc_effort"]
        )
        tier_lens_review = banders[(category, "n_lens_review")](
            bucket["n_lens_review"]
        )
        tier_lens_discussion = banders[(category, "n_lens_discussion")](
            bucket["n_lens_discussion"]
        )
        tier_telegram_msgs = banders[(category, "n_telegram_msgs")](
            bucket["n_telegram_msgs"]
        )
        # Post-banding adjustments. DIMENSION_DISCOUNTS subtracts levels from
        # a dimension's tier (telegram has its own custom bander now, so it's
        # not currently in here). The fork-day boost was removed once n_commits
        # moved to a 2-band Q3 split — Q3 already differentiates fork-heavy
        # days from light days without an extra promotion step.
        tier_telegram_msgs = _apply_discount(tier_telegram_msgs, "n_telegram_msgs")

        # Overall-tier composition differs by category. SeedSigner keeps the
        # personal/bot split — manual fork pushes vs autonomous bot work get
        # weighted differently. Tools and Other don't have personal-fork
        # commits in practice, so the split adds a perpetually-empty
        # dimension; instead they ride on the merged n_commits tier.
        if category == "seedsigner":
            per_dim_tiers = (
                tier_commits_personal, tier_commits_bot,
                tier_committed_files, tier_committed_loc_effort,
                tier_uncommitted_files, tier_uncommitted_loc_effort,
                tier_lens_review, tier_lens_discussion, tier_telegram_msgs,
            )
        else:
            per_dim_tiers = (
                tier_commits,
                tier_committed_files, tier_committed_loc_effort,
                tier_uncommitted_files, tier_uncommitted_loc_effort,
                tier_lens_review, tier_lens_discussion, tier_telegram_msgs,
            )
        overall_tier = max(per_dim_tiers, key=lambda t: TIER_RANK[t])

        conn.execute(
            """
            INSERT INTO daily_tiers(
                day, category,
                n_commits, n_commits_personal, n_commits_bot,
                n_committed_files, n_committed_loc_effort,
                n_uncommitted_files, n_uncommitted_loc_effort,
                n_lens_review, n_lens_discussion, n_telegram_msgs,
                tier_commits, tier_commits_personal, tier_commits_bot,
                tier_committed_files, tier_committed_loc_effort,
                tier_uncommitted_files, tier_uncommitted_loc_effort,
                tier_lens_review, tier_lens_discussion, tier_telegram_msgs,
                overall_tier
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                day, category,
                bucket["n_commits"], bucket["n_commits_personal"], bucket["n_commits_bot"],
                bucket["n_committed_files"], bucket["n_committed_loc_effort"],
                bucket["n_uncommitted_files"], bucket["n_uncommitted_loc_effort"],
                bucket["n_lens_review"], bucket["n_lens_discussion"],
                bucket["n_telegram_msgs"],
                tier_commits, tier_commits_personal, tier_commits_bot,
                tier_committed_files, tier_committed_loc_effort,
                tier_uncommitted_files, tier_uncommitted_loc_effort,
                tier_lens_review, tier_lens_discussion, tier_telegram_msgs,
                overall_tier,
            ),
        )
