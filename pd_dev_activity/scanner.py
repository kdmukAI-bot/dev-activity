"""`pd-dev-activity-scan` console script.

Standalone: does not require the personal-dashboard core. Reads its own
config.toml from the package directory, opens the module DB at the
hardcoded location matching `module_data_dir("dev-activity")`, runs the
scan steps, and updates `meta.last_scan_at`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from . import storage
from .sources import lens, local_trees, personal_forks, telegram


logger = logging.getLogger("pd_dev_activity.scan")


def load_config(config_path: Path | None = None) -> dict:
    if config_path is None:
        # The package directory is `pd_dev_activity/`; config sits next to it.
        pkg_dir = Path(__file__).resolve().parent
        config_path = pkg_dir.parent / "config.toml"
    if not config_path.is_file():
        raise SystemExit(f"config.toml not found at {config_path}")
    with config_path.open("rb") as f:
        return tomllib.load(f)


def _ensure_log_dir() -> None:
    log_dir = Path.home() / ".cache" / "personal-dashboard" / "dev-activity"
    log_dir.mkdir(parents=True, exist_ok=True)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )


def run_scan(config: dict, db_path: Path | None = None) -> dict:
    """Execute the full scan flow. Returns a small stats dict."""
    started_at = datetime.now()
    _ensure_log_dir()

    seedsigner_repos = set(config.get("seedsigner_repos") or [])
    other_dirs = list(config.get("other_dirs") or [])
    git_author_substrings = list(config.get("git_author_substrings") or [])
    github_logins = list(config.get("github_logins") or [])
    scan_roots = list(config.get("scan_roots") or [])
    personal_fork_list = list(config.get("personal_forks") or [])
    forks_cache_dir = Path(
        os.path.expanduser(
            config.get("forks_cache_dir")
            or "~/.cache/personal-dashboard/dev-activity/forks"
        )
    )
    lens_db_path = config.get("lens_db_path") or ""
    telegram_db_path = config.get("telegram_db_path") or ""
    telegram_user_ids = [int(x) for x in (config.get("telegram_user_ids") or [])]
    min_nonzero_days = int(config.get("tiering_min_nonzero_days", 14))

    stats = {
        "started_at": started_at.isoformat(),
        "local_repos_scanned": 0,
        "personal_forks_scanned": 0,
        "lens_events_emitted": 0,
        "telegram_rows_upserted": 0,
    }

    with storage.connect(db_path) as conn:
        last_scan = storage.get_meta(conn, "last_scan_at")
        if last_scan:
            try:
                last_dt = datetime.fromisoformat(last_scan)
            except ValueError:
                last_dt = None
        else:
            last_dt = None

        if last_dt is not None:
            since = (last_dt - timedelta(days=7)).date().isoformat()
        else:
            since = None

        now_iso = started_at.isoformat()

        # Persist the first-ever scan timestamp. Used by recompute to treat
        # pre-scanning commits as active-on-author-date regardless of the
        # working-tree mtime check (no WIP file_touches data exists for that
        # era, so the strict rule would silently drop the commits entirely).
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('first_scan_at', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (now_iso,),
        )

        # 1+2+3: local working trees
        repos = local_trees.discover_repos(scan_roots)
        for repo_path in repos:
            try:
                local_trees.scan_local_tree(
                    conn,
                    repo_path,
                    seedsigner_repos=seedsigner_repos,
                    other_dirs=other_dirs,
                    git_author_substrings=git_author_substrings,
                    since=since,
                    now_iso=now_iso,
                )
                stats["local_repos_scanned"] += 1
            except Exception:
                logger.exception("scan failed for local tree %s", repo_path)

        # 4: personal forks
        for owner_repo in personal_fork_list:
            try:
                personal_forks.scan_personal_fork(
                    conn,
                    owner_repo=owner_repo,
                    forks_cache_dir=forks_cache_dir,
                    seedsigner_repos=seedsigner_repos,
                    github_logins=github_logins,
                    since=since,
                    now_iso=now_iso,
                )
                stats["personal_forks_scanned"] += 1
            except Exception:
                logger.exception("scan failed for personal fork %s", owner_repo)

        # 5: lens DB
        try:
            stats["lens_events_emitted"] = lens.scan_lens_db(
                conn,
                lens_db_path=lens_db_path,
                github_logins=github_logins,
            )
        except Exception:
            logger.exception("lens scan failed")

        # 5b: telegram raw messages
        try:
            stats["telegram_rows_upserted"] = telegram.scan_telegram_db(
                conn,
                telegram_db_path=telegram_db_path,
                user_ids=telegram_user_ids,
            )
        except Exception:
            logger.exception("telegram scan failed")

        # 6: recompute daily_tiers
        storage.recompute_daily_tiers(
            conn, min_nonzero_days, seedsigner_repos=seedsigner_repos
        )

        # 7: meta
        finished_at = datetime.now()
        storage.set_meta(conn, "last_scan_at", finished_at.isoformat())

    stats["finished_at"] = finished_at.isoformat()
    stats["duration_seconds"] = (finished_at - started_at).total_seconds()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pd-dev-activity-scan")
    parser.add_argument("--config", type=Path, default=None,
                        help="Path to config.toml (default: package config.toml)")
    parser.add_argument("--db", type=Path, default=None,
                        help="Override DB path (default: standard module data dir)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    config = load_config(args.config)
    stats = run_scan(config, db_path=args.db)
    logger.info("scan complete: %s", stats)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
