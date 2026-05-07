"""Local working-tree scanner.

Walks each entry in scan_roots recursively, stopping descent once a directory
containing .git/ is found (that directory is recorded as a repo and not
descended into, so submodules / vendored repos don't double-count). For each
discovered repo:
  - run `git log --all --numstat` filtered to git_author_substrings.
  - run `git status --porcelain` + `git diff HEAD --numstat` to capture WIP.

Honors the strict-commit rule: a commit only counts on its author-date if at
least one of its files in the working tree still has mtime on that date.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .. import storage

logger = logging.getLogger(__name__)


@dataclass
class CommitRecord:
    sha: str
    author_iso: str
    author_date: str  # local-tz YYYY-MM-DD
    author_name: str
    author_email: str
    message: str
    files: list[tuple[int, int, str]]  # (insertions, deletions, path); -1 for binary

    @property
    def files_changed(self) -> int:
        return len(self.files)

    @property
    def lines_added(self) -> int:
        return sum(max(i, 0) for i, _d, _p in self.files)

    @property
    def lines_deleted(self) -> int:
        return sum(max(d, 0) for _i, d, _p in self.files)

    @property
    def loc_effort(self) -> int:
        total = 0
        for i, d, _p in self.files:
            if i < 0 or d < 0:
                # Binary: contribute 0.
                continue
            total += max(i, d)
        return total

    @property
    def file_paths(self) -> list[str]:
        return [p for _i, _d, p in self.files]


_SKIP_DIR_NAMES = {
    ".venv", "venv", "env",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    "dist", "build", "target", ".tox", ".cache",
    ".idea", ".vscode",
}

_MAX_DEPTH = 4

# Suffixes for files whose changes are machine-generated noise, not human work.
# Matched case-insensitively against the basename. SQLite/Chroma DBs and their
# write-ahead/journal sidecars; common binary data formats; lockfiles for
# pkg/sidecar artifacts that get rewritten by tools rather than authored.
_EXCLUDED_FILE_SUFFIXES = {
    # SQLite + sidecars (covers chromadb internals like chroma.sqlite3)
    ".db", ".sqlite", ".sqlite3",
    ".db-journal", ".sqlite-journal",
    ".db-shm", ".sqlite-shm",
    ".db-wal", ".sqlite-wal",
    # Columnar / binary data dumps
    ".parquet", ".arrow", ".feather",
    ".pkl", ".pickle",
    ".npy", ".npz",
    ".bin",
}


def _is_excluded_file(path: str) -> bool:
    lower = path.lower()
    for suffix in _EXCLUDED_FILE_SUFFIXES:
        if lower.endswith(suffix):
            return True
    return False


def _path_in_excluded_dir(path: str) -> bool:
    """True if any directory component of `path` is in `_SKIP_DIR_NAMES`.

    Why: a project's own `.gitignore` may forget to list `.venv`, `node_modules`,
    `build/`, etc. — so `git status --porcelain` will then report thousands of
    machine-managed files as uncommitted work. We apply the same skip-list used
    for repo discovery as a safety net so missing-gitignore entries don't
    pollute the activity timeline.
    """
    norm = path.replace("\\", "/")
    # Drop the basename — these names are matched against directory components.
    segments = norm.split("/")[:-1]
    return any(seg in _SKIP_DIR_NAMES for seg in segments)


def discover_repos(scan_roots: Iterable[str]) -> list[Path]:
    """Recursively find directories containing .git/, stopping descent at each repo.

    Skips common noise dirs (.venv, node_modules, etc.) and caps recursion at
    _MAX_DEPTH levels below each scan root.
    """
    repos: list[Path] = []
    seen: set[Path] = set()

    def walk(directory: Path, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        try:
            children = sorted(directory.iterdir())
        except (PermissionError, OSError) as e:
            logger.debug("cannot list %s: %s", directory, e)
            return
        # If this directory itself is a repo, record it and stop descending.
        if (directory / ".git").exists():
            real = directory.resolve()
            if real not in seen:
                seen.add(real)
                repos.append(directory)
            return
        for child in children:
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                # Skip hidden dirs except we already handled .git above.
                continue
            if child.name in _SKIP_DIR_NAMES:
                continue
            walk(child, depth + 1)

    for root_str in scan_roots:
        root = Path(os.path.expanduser(root_str))
        if not root.is_dir():
            logger.warning("scan_root %s does not exist; skipping", root)
            continue
        walk(root, 0)
    return repos


def repo_remote_url(repo_path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        return None
    return None


def _author_matches(name: str, email: str, substrs: Iterable[str]) -> bool:
    haystack = (name or "") + "\n" + (email or "")
    haystack_lower = haystack.lower()
    return any(s.lower() in haystack_lower for s in substrs)


def _parse_numstat_value(token: str) -> int:
    """Parse a --numstat insertions/deletions cell. Returns -1 for binary ('-')."""
    if token == "-":
        return -1
    try:
        return int(token)
    except ValueError:
        return 0


def parse_git_log(
    raw: str,
    *,
    author_substrings: Iterable[str],
) -> list[CommitRecord]:
    """Parse `git log --pretty=format:'%H|%aI|%an|%ae|%s' --numstat` output.

    Lines starting with a hex sha are commit headers; the next blank-separated
    block of `<i>\t<d>\t<path>` lines is the numstat.
    """
    commits: list[CommitRecord] = []
    cur: CommitRecord | None = None

    for line in raw.splitlines():
        if not line.strip():
            continue
        if "|" in line and len(line.split("|", 4)) == 5 and len(line.split("|", 1)[0]) >= 7 and all(
            c in "0123456789abcdef" for c in line.split("|", 1)[0]
        ):
            # Commit header
            sha, author_iso, author_name, author_email, message = line.split("|", 4)
            if cur is not None:
                if _author_matches(cur.author_name, cur.author_email, author_substrings):
                    commits.append(cur)
            try:
                # author_iso looks like 2024-01-02T15:04:05-05:00
                dt = datetime.fromisoformat(author_iso)
            except ValueError:
                # Fallback: trust the date prefix
                dt = None
            if dt is not None:
                # Convert to local-tz date string.
                local_dt = dt.astimezone()
                author_date = local_dt.date().isoformat()
            else:
                author_date = author_iso[:10]
            cur = CommitRecord(
                sha=sha,
                author_iso=author_iso,
                author_date=author_date,
                author_name=author_name,
                author_email=author_email,
                message=message,
                files=[],
            )
        else:
            if cur is None:
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                path = "\t".join(parts[2:])
                if _is_excluded_file(path) or _path_in_excluded_dir(path):
                    continue
                ins = _parse_numstat_value(parts[0])
                dele = _parse_numstat_value(parts[1])
                cur.files.append((ins, dele, path))

    if cur is not None and _author_matches(cur.author_name, cur.author_email, author_substrings):
        commits.append(cur)

    # Drop commits whose entire file list was excluded noise — they shouldn't
    # contribute to the commits dimension either.
    return [c for c in commits if c.files]


def run_git_log(
    repo_path: Path,
    *,
    since: str | None,
) -> str:
    cmd = [
        "git", "-C", str(repo_path), "log", "--all",
        "--pretty=format:%H|%aI|%an|%ae|%s",
        "--numstat",
    ]
    if since:
        cmd.append(f"--since={since}")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        logger.warning("git log timed out for %s", repo_path)
        return ""
    if proc.returncode != 0:
        logger.warning("git log failed for %s: %s", repo_path, proc.stderr.strip()[:200])
        return ""
    return proc.stdout


def compute_active_on_commit_day(repo_path: Path, commit: CommitRecord) -> int:
    """Set active=1 if any file in the commit exists in the working tree
    AND its mtime falls on the commit's author-date (local tz)."""
    target_date = commit.author_date
    for rel in commit.file_paths:
        full = repo_path / rel
        try:
            st = full.stat()
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
        try:
            mtime_dt = datetime.fromtimestamp(st.st_mtime)
        except (OSError, ValueError):
            continue
        if mtime_dt.date().isoformat() == target_date:
            return 1
    return 0


def scan_uncommitted(
    repo_path: Path,
) -> list[tuple[str, str, str, int]]:
    """Return list of (file_path, day, mtime_iso, loc_effort) for current WIP.

    Honors .gitignore via `git status --porcelain`. Tracked-modified files use
    diff-vs-HEAD insertions/deletions; untracked files get full line count.
    """
    out: list[tuple[str, str, str, int]] = []

    try:
        status = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain", "-uall"],
            capture_output=True, text=True, check=False, timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.warning("git status timed out for %s", repo_path)
        return out
    if status.returncode != 0:
        return out

    # In a fresh repo with no commits yet there is no HEAD, so `git diff HEAD`
    # errors out and we'd otherwise drop every staged file as loc_effort=0.
    # Treat that case like everything is untracked (count full line counts).
    has_head = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", "HEAD"],
        capture_output=True, check=False, timeout=10,
    ).returncode == 0

    # Build numstat map for tracked-modified files vs HEAD.
    diff_map: dict[str, tuple[int, int]] = {}
    if has_head:
        try:
            diff = subprocess.run(
                ["git", "-C", str(repo_path), "diff", "HEAD", "--numstat"],
                capture_output=True, text=True, check=False, timeout=120,
            )
        except subprocess.TimeoutExpired:
            diff = None
        if diff is not None and diff.returncode == 0:
            for line in diff.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    ins = _parse_numstat_value(parts[0])
                    dele = _parse_numstat_value(parts[1])
                    path = "\t".join(parts[2:])
                    diff_map[path] = (ins, dele)

    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        # XY space then path. For renames, a space-arrow-space appears in the
        # path; we keep things simple and skip renames.
        xy = line[:2]
        rest = line[3:]
        if " -> " in rest:
            # renamed
            path = rest.split(" -> ", 1)[1]
        else:
            path = rest
        # Strip surrounding quotes (porcelain quotes paths with special chars)
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1].encode("utf-8", "surrogateescape").decode(
                "unicode_escape", errors="replace"
            )
        full = repo_path / path
        try:
            st = full.stat()
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
        try:
            mtime_dt = datetime.fromtimestamp(st.st_mtime)
        except (OSError, ValueError):
            continue
        day = mtime_dt.date().isoformat()
        mtime_iso = mtime_dt.isoformat()

        if _is_excluded_file(path) or _path_in_excluded_dir(path):
            continue

        is_untracked = xy.startswith("??") or not has_head
        if is_untracked:
            try:
                # Quick line count; binary safe-ish — count newlines.
                with open(full, "rb") as f:
                    loc_effort = sum(1 for _ in f)
            except OSError:
                loc_effort = 0
        else:
            ins, dele = diff_map.get(path, (0, 0))
            if ins < 0 or dele < 0:
                # Binary changed file
                loc_effort = 0
            else:
                loc_effort = max(ins, dele)

        # Skip empty / no-effective-change files (e.g. brand-new __init__.py
        # with 0 lines, binary files, whitespace-only edits that net to 0).
        # They show up in `git status` but represent no actual work.
        if loc_effort == 0:
            continue

        out.append((path, day, mtime_iso, loc_effort))

    return out


def scan_local_tree(
    conn,
    repo_path: Path,
    *,
    seedsigner_repos: set[str],
    other_dirs: list[str],
    git_author_substrings: list[str],
    since: str | None,
    now_iso: str,
) -> None:
    name = repo_path.name
    category = storage.classify_local_repo(
        repo_path,
        name,
        seedsigner_repos=seedsigner_repos,
        other_dirs=other_dirs,
    )
    remote_url = repo_remote_url(repo_path)
    project_id = storage.upsert_project(
        conn,
        path=str(repo_path),
        name=name,
        remote_url=remote_url,
        category=category,
        source="local_tree",
        last_seen_at=now_iso,
    )

    raw = run_git_log(repo_path, since=since)
    commits = parse_git_log(raw, author_substrings=git_author_substrings)
    for commit in commits:
        active = compute_active_on_commit_day(repo_path, commit)
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
            active_on_commit_day=active,
        )

    # Wipe today's file_touches for this repo before rebuilding from current
    # `git status`. Under sub-daily cadence, a file edited earlier today and
    # then committed earlier today would otherwise leave a stale "uncommitted"
    # row alongside the new commit row, double-counting D2 vs D4. Historical
    # days' rows are untouched.
    today_iso = datetime.now().date().isoformat()
    conn.execute(
        "DELETE FROM file_touches WHERE project_id = ? AND day = ?",
        (project_id, today_iso),
    )

    for path, day, mtime_iso, loc_effort in scan_uncommitted(repo_path):
        storage.upsert_file_touch(
            conn,
            project_id=project_id,
            day=day,
            file_path=path,
            mtime_iso=mtime_iso,
            loc_effort=loc_effort,
        )
