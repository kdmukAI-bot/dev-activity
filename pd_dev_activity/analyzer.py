"""Dashboard-side analyzer: read-only over the SQLite DB written by the scanner."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from personal_dashboard.core.protocol import RouteSpec
from personal_dashboard.core.result import ModuleResult, Status

from . import storage
from .scanner import load_config, run_scan
from .storage import TIER_RANK

logger = logging.getLogger(__name__)


# Stale-data threshold. Cron is supposed to run every 24h; warn if no scan in 36h.
_STALE_AFTER = timedelta(hours=36)


def _today_iso() -> str:
    return date.today().isoformat()


def _yesterday_iso() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _seedsigner_share(ss_tier: str, ot_tier: str) -> float:
    # Width fraction allocated to the seedsigner half on a split-day cell.
    # Uses TIER_RANK so matched tiers (low+low, moderate+moderate) stay 50/50,
    # while a heavy/light pairing (high vs trace → 4/(4+1)=0.8) leans visibly
    # toward the louder side. Falls back to 0.5 if both sides are 'none'
    # (the split-cell branch shouldn't fire in that case anyway).
    rs = TIER_RANK.get(ss_tier, 0)
    ro = TIER_RANK.get(ot_tier, 0)
    if rs == 0 and ro == 0:
        return 0.5
    return rs / (rs + ro)


def _max_tier(*tiers: str | None) -> str:
    # Pick the highest tier among args using TIER_RANK; ignore None / unknown.
    best = "none"
    best_rank = TIER_RANK.get(best, 0)
    for t in tiers:
        if not t:
            continue
        r = TIER_RANK.get(t, 0)
        if r > best_rank:
            best, best_rank = t, r
    return best


def _enrich_with_share(rows: list[dict]) -> None:
    """In place: stamp `seedsigner_share` on both rows of any split day."""
    by_day: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_day.setdefault(r["day"], {})[r["category"]] = r
    for day_rows in by_day.values():
        ss = day_rows.get("seedsigner")
        ot = day_rows.get("other")
        if ss and ot and ss.get("overall_tier") != "none" and ot.get("overall_tier") != "none":
            share = _seedsigner_share(ss["overall_tier"], ot["overall_tier"])
            ss["seedsigner_share"] = share
            ot["seedsigner_share"] = share


class Analyzer:
    display_name = "Dev Activity"

    def __init__(self, config: dict) -> None:
        self._config = config
        self._latest: ModuleResult | None = None
        self._lock = asyncio.Lock()
        self._db_path: Path = storage.default_db_path()
        self._min_nonzero_days = int(config.get("tiering_min_nonzero_days", 14))

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def update(self) -> ModuleResult:
        async with self._lock:
            now = datetime.now()
            payload = await asyncio.to_thread(self._read_db)
            last_scan_iso = payload.get("last_scan_at")
            last_dt = None
            if last_scan_iso:
                try:
                    last_dt = datetime.fromisoformat(last_scan_iso)
                except ValueError:
                    last_dt = None

            stale = (last_dt is None) or (now - last_dt > _STALE_AFTER)

            today = _today_iso()
            today_rows = {
                row["category"]: row
                for row in payload["recent_days"]
                if row["day"] == today
            }
            ss = today_rows.get("seedsigner")
            ot = today_rows.get("other")
            ss_tier = ss["overall_tier"] if ss else "none"
            ot_tier = ot["overall_tier"] if ot else "none"
            summary = f"today: SeedSigner {ss_tier} / other {ot_tier}"
            detail = None
            status = Status.WARNING if stale else Status.OK
            if stale:
                if last_dt is None:
                    detail = "no scan recorded yet — run pd-dev-activity-scan"
                else:
                    age = now - last_dt
                    detail = f"scanner last ran {int(age.total_seconds() // 3600)}h ago"

            result = ModuleResult(
                status=status,
                summary_text=summary,
                detail_text=detail,
                click_url="/modules/dev-activity/",
                data=payload,
                occurred_at=now,
            )
            self._latest = result
            return result

    async def get_data(self) -> dict:
        if self._latest is None:
            await self.update()
        assert self._latest is not None
        return {
            "status": self._latest.status.value,
            "summary_text": self._latest.summary_text,
            "detail_text": self._latest.detail_text,
            "click_url": self._latest.click_url,
            "occurred_at": self._latest.occurred_at.isoformat()
            if self._latest.occurred_at
            else None,
            **self._latest.data,
        }

    # ------------------------------------------------------------------
    # DB read helpers (sync; called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _read_db(self) -> dict:
        if not self._db_path.is_file():
            return {
                "last_scan_at": None,
                "recent_days": [],
                "year_grid": [],
                "today": _today_iso(),
            }
        with storage.connect(self._db_path) as conn:
            last_scan_at = storage.get_meta(conn, "last_scan_at")

            today = date.today()
            cutoff_recent = (today - timedelta(days=14)).isoformat()
            recent_days = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT day, category, overall_tier,
                           n_commits_personal, n_commits_bot, n_committed_files, n_committed_loc_effort,
                           n_uncommitted_files, n_uncommitted_loc_effort,
                           n_lens_review, n_lens_discussion, n_telegram_msgs,
                           tier_commits_personal, tier_commits_bot, tier_committed_files, tier_committed_loc_effort,
                           tier_uncommitted_files, tier_uncommitted_loc_effort,
                           tier_lens_review, tier_lens_discussion, tier_telegram_msgs
                    FROM daily_tiers
                    WHERE day >= ?
                    ORDER BY day DESC, category
                    """,
                    (cutoff_recent,),
                )
            ]

            cutoff_year = (today - timedelta(days=365)).isoformat()
            year_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT day, category, overall_tier,
                           n_commits_personal, n_commits_bot, n_committed_files, n_committed_loc_effort,
                           n_uncommitted_files, n_uncommitted_loc_effort,
                           n_lens_review, n_lens_discussion, n_telegram_msgs
                    FROM daily_tiers
                    WHERE day >= ?
                    ORDER BY day, category
                    """,
                    (cutoff_year,),
                )
            ]
            _enrich_with_share(year_rows)
            _enrich_with_share(recent_days)
            # Pivot to per-day {seedsigner, other} dicts.
            grid: dict[str, dict[str, dict]] = {}
            for row in year_rows:
                grid.setdefault(row["day"], {})[row["category"]] = row

            return {
                "last_scan_at": last_scan_at,
                "recent_days": recent_days,
                "year_grid": grid,
                "today": today.isoformat(),
            }

    def _read_day_breakdown(self, day: str) -> dict:
        if not self._db_path.is_file():
            return {
                "day": day, "commits": [], "file_touches": [],
                "lens_events": [], "telegram_msgs": 0,
            }
        with storage.connect(self._db_path) as conn:
            # Dedup by LOGICAL commit identity — catches both same-SHA dupes
            # (fast-forward) and different-SHA dupes (rebase/cherry-pick).
            # Prefer the LOCAL-TREE (bot) row when both exist; the personal-
            # fork row only attributes when there's no matching bot commit
            # (i.e. true hands-on manual work that hasn't gone via the bot).
            commits = [
                dict(r)
                for r in conn.execute(
                    """
                    WITH ranked AS (
                        SELECT
                            c.*, p.name AS project_name, p.category,
                            p.source, p.remote_url,
                            ROW_NUMBER() OVER (
                                PARTITION BY c.author_iso, c.message, c.author_name,
                                             c.files_changed, c.loc_effort
                                ORDER BY CASE p.source WHEN 'local_tree' THEN 0 ELSE 1 END,
                                         p.id
                            ) AS rn
                        FROM commits c
                        JOIN projects p ON p.id = c.project_id
                        WHERE c.author_date = ?
                    ),
                    others AS (
                        SELECT author_iso, message, author_name,
                               GROUP_CONCAT(DISTINCT project_name) AS also_in
                        FROM ranked WHERE rn > 1
                        GROUP BY author_iso, message, author_name
                    )
                    SELECT
                        r.sha, r.author_iso, r.author_name, r.message,
                        r.files_changed, r.lines_added, r.lines_deleted,
                        r.loc_effort, r.active_on_commit_day,
                        r.project_name, r.category, r.source, r.remote_url,
                        o.also_in
                    FROM ranked r
                    LEFT JOIN others o
                      ON o.author_iso = r.author_iso
                     AND o.message = r.message
                     AND o.author_name = r.author_name
                    WHERE r.rn = 1
                    ORDER BY CASE r.category WHEN 'seedsigner' THEN 0 ELSE 1 END,
                             r.project_name, r.author_iso DESC
                    """,
                    (day,),
                )
            ]
            file_touches = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT ft.file_path, ft.mtime_iso, ft.loc_effort,
                           p.name AS project_name, p.category
                    FROM file_touches ft
                    JOIN projects p ON p.id = ft.project_id
                    WHERE ft.day = ?
                    ORDER BY CASE p.category WHEN 'seedsigner' THEN 0 ELSE 1 END,
                             p.name, ft.file_path
                    """,
                    (day,),
                )
            ]
            lens_events = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT repo, number, title,
                           COUNT(*) AS total_events,
                           SUM(CASE WHEN kind='pr_review_comment' THEN 1 ELSE 0 END)
                               AS n_review_comments,
                           SUM(CASE WHEN kind='pr_comment' THEN 1 ELSE 0 END)
                               AS n_pr_comments,
                           SUM(CASE WHEN kind='issue_comment' THEN 1 ELSE 0 END)
                               AS n_issue_comments,
                           SUM(CASE WHEN kind='pr_open' THEN 1 ELSE 0 END)
                               AS n_pr_opens,
                           SUM(CASE WHEN kind='issue_open' THEN 1 ELSE 0 END)
                               AS n_issue_opens,
                           MAX(ts_iso) AS latest_ts
                    FROM lens_events
                    WHERE day = ?
                    GROUP BY repo, number
                    ORDER BY latest_ts DESC
                    """,
                    (day,),
                )
            ]
            telegram_total = conn.execute(
                "SELECT COALESCE(SUM(msg_count), 0) AS n FROM telegram_activity WHERE day = ?",
                (day,),
            ).fetchone()
            telegram_msgs = int(telegram_total["n"]) if telegram_total else 0

            # Per-section tiers per category. The day-detail template renders
            # these next to each section heading (commits, uncommitted touches,
            # lens activity, telegram). Each section uses the max-rank across
            # the dimensions that contribute to it, so a section's label
            # reflects however that section's loudest signal landed in banding.
            ss_tier = "none"
            ot_tier = "none"
            section_tiers = {
                "commits":     {"seedsigner": "none", "other": "none"},
                "uncommitted": {"seedsigner": "none", "other": "none"},
                "lens":        {"seedsigner": "none", "other": "none"},
                "telegram":    {"seedsigner": "none"},
            }
            for r in conn.execute(
                """
                SELECT category, overall_tier,
                       tier_commits_personal, tier_commits_bot,
                       tier_committed_files, tier_committed_loc_effort,
                       tier_uncommitted_files, tier_uncommitted_loc_effort,
                       tier_lens_review, tier_lens_discussion, tier_telegram_msgs
                FROM daily_tiers WHERE day = ?
                """,
                (day,),
            ):
                cat = r["category"]
                if cat == "seedsigner":
                    ss_tier = r["overall_tier"]
                elif cat == "other":
                    ot_tier = r["overall_tier"]
                else:
                    continue
                section_tiers["commits"][cat] = _max_tier(
                    r["tier_commits_personal"], r["tier_commits_bot"],
                    r["tier_committed_files"], r["tier_committed_loc_effort"],
                )
                section_tiers["uncommitted"][cat] = _max_tier(
                    r["tier_uncommitted_files"], r["tier_uncommitted_loc_effort"],
                )
                section_tiers["lens"][cat] = _max_tier(
                    r["tier_lens_review"], r["tier_lens_discussion"],
                )
                if cat == "seedsigner":
                    section_tiers["telegram"]["seedsigner"] = r["tier_telegram_msgs"] or "none"
            seedsigner_share = _seedsigner_share(ss_tier, ot_tier)
            return {
                "day": day,
                "commits": commits,
                "file_touches": file_touches,
                "lens_events": lens_events,
                "telegram_msgs": telegram_msgs,
                "ss_tier": ss_tier,
                "ot_tier": ot_tier,
                "seedsigner_share": seedsigner_share,
                "section_tiers": section_tiers,
            }

    def _read_year_for_json(self) -> list[dict]:
        if not self._db_path.is_file():
            return []
        today = date.today()
        cutoff = (today - timedelta(days=365)).isoformat()
        with storage.connect(self._db_path) as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT day, category, overall_tier,
                           n_commits_personal, n_commits_bot, n_committed_files, n_committed_loc_effort,
                           n_uncommitted_files, n_uncommitted_loc_effort,
                           n_lens_review, n_lens_discussion, n_telegram_msgs
                    FROM daily_tiers
                    WHERE day >= ?
                    ORDER BY day, category
                    """,
                    (cutoff,),
                )
            ]
        _enrich_with_share(rows)
        return rows

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @property
    def routes(self) -> list[RouteSpec]:
        analyzer = self

        async def run_now_handler(request: Request) -> JSONResponse:
            def _do() -> dict:
                return run_scan(analyzer._config, db_path=analyzer._db_path)

            stats = await asyncio.to_thread(_do)
            result = await analyzer.update()
            core = getattr(request.app.state, "core", None)
            if core is not None:
                await core.publish_module_result("dev-activity", result)
            return JSONResponse({
                "status": result.status.value,
                "summary_text": result.summary_text,
                "scan_stats": stats,
            })

        async def day_handler(request: Request) -> HTMLResponse:
            day = request.path_params.get("date") or ""
            try:
                # Validate that it's an ISO date.
                date.fromisoformat(day)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid date")
            payload = await asyncio.to_thread(analyzer._read_day_breakdown, day)
            core = getattr(request.app.state, "core", None)
            if core is None:
                # Fallback: render without templates
                return HTMLResponse(_render_day_html_fallback(payload))
            templates = core.get_templates()
            return templates.TemplateResponse(
                request,
                "dev-activity/day.html",
                {"data": payload, "module_name": "dev-activity"},
            )

        async def heatmap_json_handler(request: Request) -> JSONResponse:
            rows = await asyncio.to_thread(analyzer._read_year_for_json)
            return JSONResponse(rows)

        return [
            RouteSpec(path="/run-now", handler=run_now_handler, method="POST", auth="bearer"),
            RouteSpec(path="/day/{date}", handler=day_handler, method="GET"),
            RouteSpec(path="/heatmap.json", handler=heatmap_json_handler, method="GET"),
        ]


def _render_day_html_fallback(payload: dict) -> str:
    parts = [f"<h3>{payload['day']}</h3>"]
    if payload["commits"]:
        parts.append("<h4>Commits</h4><ul>")
        for c in payload["commits"]:
            parts.append(
                f"<li><strong>{c['project_name']}</strong> "
                f"({c['category']}/{c['source']}): {c['message']} "
                f"({c['files_changed']} files, {c['loc_effort']} LoC)</li>"
            )
        parts.append("</ul>")
    if payload["file_touches"]:
        parts.append(
            f"<h4>Uncommitted touches ({len(payload['file_touches'])})</h4><ul>"
        )
        for f in payload["file_touches"]:
            parts.append(
                f"<li>{f['project_name']}: {f['file_path']} "
                f"({f['loc_effort']} LoC)</li>"
            )
        parts.append("</ul>")
    if payload["lens_events"]:
        parts.append("<h4>Lens activity</h4><ul>")
        for e in payload["lens_events"]:
            parts.append(
                f"<li>{e['kind']} on {e['repo']}#{e['number']} ({e['title'] or ''})</li>"
            )
        parts.append("</ul>")
    if not (payload["commits"] or payload["file_touches"] or payload["lens_events"]):
        parts.append("<p><em>No recorded activity for this day.</em></p>")
    return "\n".join(parts)
