"""Telegram message activity source.

Reads the project-lens-seedsigner-config telegram.db (a separate SQLite
file from project-lens.db that holds raw Telegram messages), counts
per-day messages by the configured user_ids, and upserts into the local
telegram_activity table. The recompute_daily_tiers step then folds those
counts into a `n_telegram_msgs` dimension on the daily_tiers grid.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from .. import storage

logger = logging.getLogger(__name__)


def scan_telegram_db(
    conn: sqlite3.Connection,
    *,
    telegram_db_path: str,
    user_ids: list[int],
) -> int:
    """Pull per-day message counts for the configured user_ids and upsert
    into the local telegram_activity table. Returns rows upserted."""
    if not telegram_db_path or not user_ids:
        return 0
    db_path = os.path.expanduser(telegram_db_path)
    if not Path(db_path).is_file():
        logger.warning("telegram_db_path %s does not exist; skipping", db_path)
        return 0

    placeholders = ",".join("?" * len(user_ids))
    query = (
        f"SELECT date(date) AS day, from_user_id, COUNT(*) AS msgs "
        f"FROM telegram_msgs "
        f"WHERE from_user_id IN ({placeholders}) AND date IS NOT NULL "
        f"GROUP BY day, from_user_id"
    )

    uri = f"file:{db_path}?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=30.0)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute(query, list(user_ids)).fetchall()
    finally:
        src.close()

    n = 0
    for r in rows:
        storage.upsert_telegram_activity(
            conn,
            day=r["day"],
            user_id=int(r["from_user_id"]),
            msg_count=int(r["msgs"]),
        )
        n += 1
    return n
