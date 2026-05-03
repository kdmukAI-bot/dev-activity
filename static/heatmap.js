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
    const target = cellsG.querySelector(`[data-day="${day}"]`);
    if (!target) {
      outline.style.display = "none";
      return;
    }
    let x, y, w, h;
    if (target.tagName.toLowerCase() === "g") {
      // Split cell: two half-rects. Span both.
      const left = target.querySelector("rect");
      const right = left.nextElementSibling;
      x = parseFloat(left.getAttribute("x"));
      y = parseFloat(left.getAttribute("y"));
      h = parseFloat(left.getAttribute("height"));
      w = parseFloat(left.getAttribute("width")) +
          parseFloat(right.getAttribute("width"));
    } else {
      x = parseFloat(target.getAttribute("x"));
      y = parseFloat(target.getAttribute("y"));
      w = parseFloat(target.getAttribute("width"));
      h = parseFloat(target.getAttribute("height"));
    }
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

  function fmtSide(label, row) {
    if (!row) return `${label}: —`;
    return `${label}: ${row.n_commits_personal ?? 0} fork-commits / ${row.n_commits_bot ?? 0} bot-commits / ${row.n_committed_files} files / ${row.n_committed_loc_effort} LoC committed / ${row.n_uncommitted_files} WIP files (${row.n_uncommitted_loc_effort} LoC) / ${row.n_lens_review ?? 0} review / ${row.n_lens_discussion ?? 0} disc / ${row.n_telegram_msgs ?? 0} tg → ${row.overall_tier}`;
  }

  function buildTooltip(iso, ss, ot) {
    return `${iso}\n${fmtSide("SeedSigner", ss)}\n${fmtSide("Other", ot)}`;
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
            const ot = cellInfo["other"];
            const ssTier = ss ? ss.overall_tier : "none";
            const otTier = ot ? ot.overall_tier : "none";
            const tooltip = buildTooltip(iso, ss, ot);

            if (ssTier === "none" && otTier === "none") {
              const rect = mkRect(
                x, y, SQ, SQ,
                "pd-devact-cell pd-devact-ss-none pd-devact-ot-none",
                iso, tooltip,
              );
              cellsG.appendChild(rect);
            } else if (otTier === "none") {
              cellsG.appendChild(
                mkRect(x, y, SQ, SQ, `pd-devact-cell pd-devact-ss-${ssTier} pd-devact-ot-none`, iso, tooltip),
              );
            } else if (ssTier === "none") {
              cellsG.appendChild(
                mkRect(x, y, SQ, SQ, `pd-devact-cell pd-devact-ss-none pd-devact-ot-${otTier}`, iso, tooltip),
              );
            } else {
              const left = mkRect(x, y, SQ / 2, SQ, `pd-devact-cell pd-devact-cell-half-l pd-devact-ss-${ssTier}`, iso, tooltip);
              const right = mkRect(x + SQ / 2, y, SQ / 2, SQ, `pd-devact-cell pd-devact-cell-half-r pd-devact-ot-${otTier}`, iso, tooltip);
              const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
              g.setAttribute("data-day", iso);
              g.appendChild(left);
              g.appendChild(right);
              const t = document.createElementNS("http://www.w3.org/2000/svg", "title");
              t.textContent = tooltip;
              g.appendChild(t);
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
