"""Project-lens DB scanner.

Reads `github_prs.reviews_json`, `github_prs.review_comments_json`,
`github_prs.issue_comments_json`, and `github_issues.comments_json` and
extracts events authored by the configured github_logins. Emits one row
per event into our `lens_events` table.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .. import storage

logger = logging.getLogger(__name__)


def _parse_iso_to_local_day(iso_ts: str | None) -> tuple[str, str] | None:
    if not iso_ts:
        return None
    try:
        if iso_ts.endswith("Z"):
            iso_ts = iso_ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    local_dt = dt.astimezone()
    return local_dt.date().isoformat(), local_dt.isoformat()


def _emit_event(
    conn,
    *,
    kind: str,
    repo: str,
    number: int,
    title: str | None,
    actor_login: str,
    ts_iso: str,
) -> None:
    parsed = _parse_iso_to_local_day(ts_iso)
    if parsed is None:
        return
    day, local_iso = parsed
    storage.insert_lens_event(
        conn,
        day=day,
        kind=kind,
        repo=repo,
        number=number,
        title=title,
        actor_login=actor_login,
        ts_iso=local_iso,
    )


def _iter_blob(blob: str | None):
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return data


def scan_lens_db(
    conn,
    *,
    lens_db_path: str | Path,
    github_logins: list[str],
) -> int:
    """Read the lens DB read-only and write events into our DB. Returns
    the count of events emitted (best-effort)."""
    path = Path(lens_db_path).expanduser()
    if not path.is_file():
        logger.warning("lens DB not found at %s; skipping", path)
        return 0

    logins_lower = {x.lower() for x in github_logins}
    emitted = 0

    uri = f"file:{path}?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=30.0)
    src.row_factory = sqlite3.Row
    try:
        # Cleanup obsolete kinds. We used to emit `pr_review` for each entry
        # in reviews_json, but in the user's workflow every such event is an
        # auto-generated empty wrapper around an inline comment (the user
        # never uses GitHub's "Submit Review" feature). They were double-
        # counting the same activity already captured by pr_review_comment.
        conn.execute(
            "DELETE FROM lens_events WHERE kind NOT IN "
            "('pr_review_comment', 'pr_comment', 'issue_comment')"
        )

        # PRs: review_comments / issue_comments. We deliberately skip
        # reviews_json — see cleanup comment above.
        cur = src.execute(
            "SELECT repo, number, title, review_comments_json, "
            "issue_comments_json FROM github_prs"
        )
        for row in cur:
            repo = row["repo"]
            number = int(row["number"])
            title = row["title"]

            for comment in _iter_blob(row["review_comments_json"]):
                user = (comment or {}).get("user") or {}
                login = (user.get("login") or "").lower()
                if login not in logins_lower:
                    continue
                ts = comment.get("created_at")
                # Inline line-by-line review comments. Distinct from PR
                # conversation comments (issue_comments_json on PRs) so the
                # day-tier banding can treat code-review activity (volume-heavy
                # on contentious PRs) separately from substantive discussion.
                _emit_event(
                    conn, kind="pr_review_comment", repo=repo, number=number,
                    title=title, actor_login=user.get("login") or "", ts_iso=ts,
                )
                emitted += 1

            for comment in _iter_blob(row["issue_comments_json"]):
                user = (comment or {}).get("user") or {}
                login = (user.get("login") or "").lower()
                if login not in logins_lower:
                    continue
                ts = comment.get("created_at")
                _emit_event(
                    conn, kind="pr_comment", repo=repo, number=number,
                    title=title, actor_login=user.get("login") or "", ts_iso=ts,
                )
                emitted += 1

        # Issues: comments
        cur = src.execute(
            "SELECT repo, number, title, comments_json FROM github_issues"
        )
        for row in cur:
            repo = row["repo"]
            number = int(row["number"])
            title = row["title"]

            for comment in _iter_blob(row["comments_json"]):
                user = (comment or {}).get("user") or {}
                login = (user.get("login") or "").lower()
                if login not in logins_lower:
                    continue
                ts = comment.get("created_at")
                _emit_event(
                    conn, kind="issue_comment", repo=repo, number=number,
                    title=title, actor_login=user.get("login") or "", ts_iso=ts,
                )
                emitted += 1
    finally:
        src.close()

    return emitted
