"""Personal-fork bare-clone scanner.

For each `owner/repo` in personal_forks, maintain a bare clone in
forks_cache_dir, fetch nightly, then run the same git-log parser as
local_trees but filtered by github_logins.

Commits returned from a bare clone are by definition pushed to the remote.
We mark them all `active_on_commit_day=1` (no working tree to check).
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from .. import storage
from .local_trees import find_earliest_commit_date, parse_git_log, run_git_log

logger = logging.getLogger(__name__)


def _cache_path_for(forks_cache_dir: Path, owner_repo: str) -> Path:
    owner, repo = owner_repo.split("/", 1)
    return forks_cache_dir / f"{owner}__{repo}.git"


_BARE_FETCH_REFSPEC = "+refs/heads/*:refs/heads/*"


def _ensure_fetch_refspec(cache_path: Path) -> None:
    """Ensure the bare clone has a branch-tracking fetch refspec.

    `git clone --bare` does NOT set `remote.origin.fetch` (only `--mirror`
    does). Without a refspec, `git fetch` updates only FETCH_HEAD and never
    advances `refs/heads/*`, so branch tips silently freeze at clone time.
    We avoid `--mirror` because it would also pull `refs/pull/*` etc. — for
    a personal fork we only care about branches.
    """
    existing = subprocess.run(
        ["git", "-C", str(cache_path), "config", "--get-all", "remote.origin.fetch"],
        capture_output=True, text=True, check=False, timeout=10,
    )
    if existing.returncode == 0 and _BARE_FETCH_REFSPEC in existing.stdout.split():
        return
    subprocess.run(
        ["git", "-C", str(cache_path), "config", "remote.origin.fetch", _BARE_FETCH_REFSPEC],
        capture_output=True, check=False, timeout=10,
    )


def ensure_bare_clone(forks_cache_dir: Path, owner_repo: str) -> Path | None:
    """Clone if missing, fetch if present. Returns the bare-clone path or None on failure."""
    forks_cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_path_for(forks_cache_dir, owner_repo)

    if not cache_path.exists():
        url = f"https://github.com/{owner_repo}.git"
        logger.info("cloning %s -> %s", url, cache_path)
        try:
            proc = subprocess.run(
                ["git", "clone", "--bare", "--quiet", url, str(cache_path)],
                capture_output=True, text=True, check=False,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            logger.warning("clone of %s timed out", owner_repo)
            return None
        if proc.returncode != 0:
            logger.warning(
                "clone of %s failed (exit %d): %s",
                owner_repo, proc.returncode, proc.stderr.strip()[:200],
            )
            # Clean up half-clone if present
            if cache_path.exists():
                try:
                    import shutil
                    shutil.rmtree(cache_path)
                except OSError:
                    pass
            return None
        _ensure_fetch_refspec(cache_path)
    else:
        _ensure_fetch_refspec(cache_path)
        try:
            proc = subprocess.run(
                ["git", "-C", str(cache_path), "fetch", "origin", "--prune", "--quiet"],
                capture_output=True, text=True, check=False,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            logger.warning("fetch of %s timed out", owner_repo)
            return cache_path  # Use stale clone rather than failing
        if proc.returncode != 0:
            logger.warning(
                "fetch of %s failed (exit %d): %s",
                owner_repo, proc.returncode, proc.stderr.strip()[:200],
            )
            return cache_path  # Use stale clone

    return cache_path


def scan_personal_fork(
    conn,
    *,
    owner_repo: str,
    forks_cache_dir: Path,
    seedsigner_repos: set[str],
    github_logins: list[str],
    since: str | None,
    now_iso: str,
) -> None:
    cache_path = ensure_bare_clone(forks_cache_dir, owner_repo)
    if cache_path is None:
        return

    repo_basename = owner_repo.split("/", 1)[1]
    category = "seedsigner" if repo_basename in seedsigner_repos else "tools"
    remote_url = f"https://github.com/{owner_repo}.git"
    earliest_commit = find_earliest_commit_date(cache_path)
    project_id = storage.upsert_project(
        conn,
        path=str(cache_path),
        name=repo_basename,
        remote_url=remote_url,
        category=category,
        source="personal_fork",
        last_seen_at=now_iso,
        earliest_commit_date=earliest_commit,
    )

    raw = run_git_log(cache_path, since=since)
    # In a bare clone, repo_path is the .git itself; run_git_log uses -C which
    # accepts either a working tree or a bare repo path.
    commits = parse_git_log(raw, author_substrings=github_logins)
    for commit in commits:
        storage.upsert_commit(
            conn,
            sha=commit.sha,
            project_id=project_id,
            author_date=commit.author_date,
            author_iso=commit.author_iso,
            author_name=commit.author_name,
            author_email=commit.author_email,
            message=commit.message,
            files_changed=commit.files_changed,
            lines_added=commit.lines_added,
            lines_deleted=commit.lines_deleted,
            loc_effort=commit.loc_effort,
            active_on_commit_day=1,  # bare clones: unconditional
        )
