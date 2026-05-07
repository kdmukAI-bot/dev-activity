// Renders the dev-activity heatmap into an SVG skeleton.
//
// Both the home widget and the detail page call window.renderHeatmap(opts).
// The function is idempotent on a given (cellsId, monthsId) pair — it clears
// the target groups before rendering, so htmx swaps that re-fire it work.

(function () {
  const TIER_RANK = { none: 0, trace: 1, low: 2, moderate: 3, high: 4 };

  // Selection outline: position a single overlay <rect> over the day-cell
  // whose data-day matches `day`. Stored as a dataset attr on the outline
  // element so a heatmap re-render (htmx swap, refetch) can restore it.
  function applySelection(cellsId, outlineId, day) {
    const outline = document.getElementById(outlineId);
    if (!outline) return;
    const cellsG = document.getElementById(cellsId);
    if (!cellsG || !day) {
      outline.style.display = "none";
      return;
    }
    // For both solo cells and split cells, the element carrying data-day is a
    // full-square rect (the solo cell rect or the overlay rect on top of a
    // split). Reading geometry off it directly always wraps the whole day.
    const target = cellsG.querySelector(`[data-day="${day}"]`);
    if (!target) {
      outline.style.display = "none";
      return;
    }
    const x = parseFloat(target.getAttribute("x"));
    const y = parseFloat(target.getAttribute("y"));
    const w = parseFloat(target.getAttribute("width"));
    const h = parseFloat(target.getAttribute("height"));
    outline.setAttribute("x", x - 1);
    outline.setAttribute("y", y - 1);
    outline.setAttribute("width", w + 2);
    outline.setAttribute("height", h + 2);
    outline.style.display = "";
  }

  window.setHeatmapSelected = function (cellsId, outlineId, day) {
    const outline = document.getElementById(outlineId);
    if (outline) outline.dataset.day = day || "";
    applySelection(cellsId, outlineId, day);
  };

  function mkRect(x, y, w, h, cls, day, tooltip) {
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", x);
    rect.setAttribute("y", y);
    rect.setAttribute("width", w);
    rect.setAttribute("height", h);
    rect.setAttribute("rx", 1);
    rect.setAttribute("class", cls);
    rect.setAttribute("data-day", day);
    const t = document.createElementNS("http://www.w3.org/2000/svg", "title");
    t.textContent = tooltip;
    rect.appendChild(t);
    return rect;
  }

  function fmtSide(label, row, opts) {
    if (!row) return `${label}: —`;
    const split = opts && opts.splitCommits;
    const commitFrag = split
      ? `${row.n_commits_personal ?? 0} fork-commits / ${row.n_commits_bot ?? 0} bot-commits`
      : `${row.n_commits ?? (row.n_commits_personal ?? 0) + (row.n_commits_bot ?? 0)} commits`;
    return `${label}: ${commitFrag} / ${row.n_committed_files} files / ${row.n_committed_loc_effort} LoC committed / ${row.n_uncommitted_files} WIP files (${row.n_uncommitted_loc_effort} LoC) / ${row.n_lens_review ?? 0} review / ${row.n_lens_discussion ?? 0} disc / ${row.n_telegram_msgs ?? 0} tg → ${row.overall_tier}`;
  }

  function buildTooltip(iso, ss, tl, ot) {
    return `${iso}\n${fmtSide("SeedSigner", ss, { splitCommits: true })}\n${fmtSide("Tools", tl)}\n${fmtSide("Other", ot)}`;
  }

  window.renderHeatmap = function (opts) {
    const cellsG = document.getElementById(opts.cellsId);
    if (!cellsG) return;
    const monthsG = opts.monthsId ? document.getElementById(opts.monthsId) : null;

    // Clear in case of re-render (htmx swap).
    while (cellsG.firstChild) cellsG.removeChild(cellsG.firstChild);
    if (monthsG) {
      while (monthsG.firstChild) monthsG.removeChild(monthsG.firstChild);
    }

    const SQ = opts.sq ?? 12;
    const GAP = opts.gap ?? 2;
    const COLS = opts.cols ?? 52;
    const ROWS = opts.rows ?? 7;
    const PAD_LEFT = opts.padLeft ?? 0;
    const PAD_TOP = opts.padTop ?? 0;
    const onClickDay = opts.onClickDay; // optional

    const SVG_W = PAD_LEFT + COLS * (SQ + GAP);
    // Approximate width of a 3-letter month abbreviation at the .pd-devact-axis
    // font-size, with a few px of slack for anti-aliasing past the SVG's viewBox.
    const LABEL_W_EST = 30;

    fetch("/modules/dev-activity/heatmap.json")
      .then((r) => r.json())
      .then((rows) => {
        const byDay = {};
        for (const r of rows) {
          if (!byDay[r.day]) byDay[r.day] = {};
          byDay[r.day][r.category] = r;
        }

        const today = new Date();
        today.setHours(0, 0, 0, 0);
        const todayDow = today.getDay(); // 0=Sun
        const start = new Date(today);
        start.setDate(start.getDate() - ((COLS - 1) * 7 + todayDow));

        let lastMonth = -1;
        for (let col = 0; col < COLS; col++) {
          for (let row = 0; row < ROWS; row++) {
            const d = new Date(start);
            d.setDate(d.getDate() + col * 7 + row);
            if (d > today) continue;
            const iso = d.toISOString().slice(0, 10);
            const x = PAD_LEFT + col * (SQ + GAP);
            const y = PAD_TOP + row * (SQ + GAP);
            const cellInfo = byDay[iso] || {};
            const ss = cellInfo["seedsigner"];
            const tl = cellInfo["tools"];
            const ot = cellInfo["other"];
            const ssTier = ss ? ss.overall_tier : "none";
            const tlTier = tl ? tl.overall_tier : "none";
            const otTier = ot ? ot.overall_tier : "none";
            const tooltip = buildTooltip(iso, ss, tl, ot);

            const nonzeroCount =
              (ssTier !== "none" ? 1 : 0) +
              (tlTier !== "none" ? 1 : 0) +
              (otTier !== "none" ? 1 : 0);

            if (nonzeroCount === 0) {
              const rect = mkRect(
                x, y, SQ, SQ,
                "pd-devact-cell pd-devact-ss-none pd-devact-tl-none pd-devact-ot-none",
                iso, tooltip,
              );
              cellsG.appendChild(rect);
            } else if (nonzeroCount === 1) {
              // Solo-category day: paint the whole cell with that category's
              // tier color. The other two prefixes get -none to keep the
              // class set self-describing.
              const cls =
                `pd-devact-cell pd-devact-ss-${ssTier} pd-devact-tl-${tlTier} pd-devact-ot-${otTier}`;
              cellsG.appendChild(mkRect(x, y, SQ, SQ, cls, iso, tooltip));
            } else {
              // Multi-category day: render up to three vertical stripes
              // (ss | tl | ot), widths weighted by tier intensity. Stripes
              // for 'none' categories get zero width and aren't rendered, so
              // a 2-category day still produces two stripes (just at the L
              // and R positions appropriate to which categories are active).
              // Server stamps {ss,tl,ot}_share on each row; fall back to a
              // local TIER_RANK ratio if missing.
              const findShare = (key) => {
                for (const r of [ss, tl, ot]) {
                  if (r && typeof r[key] === "number") return r[key];
                }
                return null;
              };
              let ssShare = findShare("ss_share");
              let tlShare = findShare("tl_share");
              let otShare = findShare("ot_share");
              if (ssShare == null || tlShare == null || otShare == null) {
                const rs = TIER_RANK[ssTier] || 0;
                const rt = TIER_RANK[tlTier] || 0;
                const ro = TIER_RANK[otTier] || 0;
                const total = rs + rt + ro;
                if (total > 0) {
                  ssShare = rs / total;
                  tlShare = rt / total;
                  otShare = ro / total;
                } else {
                  ssShare = tlShare = otShare = 1 / 3;
                }
              }

              // Convert shares to integer pixel widths that sum to SQ.
              // Floor each then distribute the remainder to the largest
              // shares to avoid a sub-pixel gap on the right edge.
              const wSS = Math.floor(SQ * ssShare);
              const wTL = Math.floor(SQ * tlShare);
              let wOT = SQ - wSS - wTL;
              if (wOT < 0) wOT = 0;

              const stripes = [
                { w: wSS, x: x,                pos: "l", prefix: "ss", tier: ssTier },
                { w: wTL, x: x + wSS,          pos: "m", prefix: "tl", tier: tlTier },
                { w: wOT, x: x + wSS + wTL,    pos: "r", prefix: "ot", tier: otTier },
              ];
              const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
              for (const s of stripes) {
                if (s.w <= 0 || s.tier === "none") continue;
                const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
                rect.setAttribute("x", s.x);
                rect.setAttribute("y", y);
                rect.setAttribute("width", s.w);
                rect.setAttribute("height", SQ);
                rect.setAttribute("rx", 1);
                rect.setAttribute(
                  "class",
                  `pd-devact-cell pd-devact-cell-third-${s.pos} pd-devact-${s.prefix}-${s.tier}`,
                );
                g.appendChild(rect);
              }
              const overlay = mkRect(
                x, y, SQ, SQ,
                "pd-devact-cell pd-devact-cell-overlay",
                iso, tooltip,
              );
              g.appendChild(overlay);
              cellsG.appendChild(g);
            }

            if (monthsG && row === 0 && d.getMonth() !== lastMonth && d.getDate() <= 7) {
              const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
              // Nudge label left if it would overflow the SVG's right edge,
              // so the rightmost month (e.g. "May" early in the month) stays
              // fully visible. Slight visual offset relative to the column,
              // but readable.
              const labelX = Math.min(x, SVG_W - LABEL_W_EST);
              text.setAttribute("x", labelX);
              text.setAttribute("y", PAD_TOP - 6);
              text.setAttribute("class", "pd-devact-axis");
              text.textContent = d.toLocaleString("en", { month: "short" });
              monthsG.appendChild(text);
              lastMonth = d.getMonth();
            }
          }
        }

        if (onClickDay) {
          cellsG.addEventListener("click", function (e) {
            let target = e.target;
            let day = null;
            while (target && target !== cellsG) {
              if (target.dataset && target.dataset.day) {
                day = target.dataset.day;
                break;
              }
              target = target.parentNode;
            }
            if (day) onClickDay(day);
          });
        }

        // Restore selection outline if one was previously set (e.g. user
        // clicked a day, then htmx re-rendered the widget and rebuilt cells).
        if (opts.outlineId) {
          const outline = document.getElementById(opts.outlineId);
          const day = outline && outline.dataset.day;
          if (day) applySelection(opts.cellsId, opts.outlineId, day);
        }
      })
      .catch((err) => console.error("heatmap fetch failed", err));
  };
})();
