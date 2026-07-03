from __future__ import annotations

import html
import json
import sqlite3
import sys
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from . import db
from .browser_dates import (
    browser_date_text as item_browser_date_text,
    manual_date_midpoint as shared_manual_date_midpoint,
    month_key_from_path as shared_month_key_from_path,
)
from .formatting import format_bytes
from .html_paths import display_relative_path, path_to_url, relative_to_target
from .media import image_dimensions, media_kind
from .media_cache import MediaMetadataCache
from .static_browser import static_browser_item


VIEW_ROTATION_CSS = """
    .view-rotation-container {
      position: relative;
      overflow: hidden;
    }
    img.view-rotated {
      position: absolute;
      left: 50%;
      top: 50%;
      max-width: none;
      max-height: none;
      transform-origin: center center;
    }
"""


VIEW_ROTATION_JAVASCRIPT = """
    function normalizedViewRotation(item) {
      const rotation = Number(item.viewRotation || 0);
      return [0, 90, 180, 270].includes(rotation) ? rotation : 0;
    }
    function applyImageViewRotation(img, item, fit) {
      const rotation = normalizedViewRotation(item);
      if (rotation === 0) return;
      const container = img.parentElement;
      if (!container) return;
      container.classList.add("view-rotation-container");
      img.classList.add("view-rotated");
      img.dataset.viewRotation = String(rotation);
      const resize = () => sizeRotatedImage(img, rotation, fit);
      img.addEventListener("load", resize);
      if (typeof ResizeObserver !== "undefined") {
        new ResizeObserver(resize).observe(container);
      }
      requestAnimationFrame(resize);
    }
    function sizeRotatedImage(img, rotation, fit) {
      const container = img.parentElement;
      const naturalWidth = img.naturalWidth;
      const naturalHeight = img.naturalHeight;
      if (!container || naturalWidth <= 0 || naturalHeight <= 0) return;
      const containerWidth = container.clientWidth;
      const containerHeight = container.clientHeight;
      if (containerWidth <= 0 || containerHeight <= 0) return;
      const quarterTurn = rotation === 90 || rotation === 270;
      const rotatedWidth = quarterTurn ? naturalHeight : naturalWidth;
      const rotatedHeight = quarterTurn ? naturalWidth : naturalHeight;
      const scaleX = containerWidth / rotatedWidth;
      const scaleY = containerHeight / rotatedHeight;
      const scale = fit === "cover" ? Math.max(scaleX, scaleY) : Math.min(scaleX, scaleY);
      img.style.width = `${naturalWidth * scale}px`;
      img.style.height = `${naturalHeight * scale}px`;
      img.style.transform = `translate(-50%, -50%) rotate(${rotation}deg)`;
    }
"""


STATIC_BROWSER_MONTH_NAMES = {
    "01": "Januar",
    "02": "Februar",
    "03": "Mars",
    "04": "April",
    "05": "Mai",
    "06": "Juni",
    "07": "Juli",
    "08": "August",
    "09": "September",
    "10": "Oktober",
    "11": "November",
    "12": "Desember",
}


STATIC_BROWSER_CSS = f"""
    :root {{
      color-scheme: dark;
      --bg: #171717;
      --panel: #242424;
      --border: #3a3a3a;
      --text: #f2f2f2;
      --muted: #b8b8b8;
      --accent: #7db7ff;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      width: 100%;
      height: 100%;
      overflow: hidden;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .app {{
      height: 100vh;
      height: 100dvh;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      overflow: hidden;
    }}
    header {{
      background: var(--panel);
      border-color: var(--border);
    }}
    header {{
      border-bottom: 1px solid var(--border);
      padding: 12px;
      display: grid;
      gap: 10px;
    }}
    .topline, .controls {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .title {{
      font-weight: 700;
      margin-right: 12px;
    }}
    .breadcrumb {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .breadcrumb a {{
      color: var(--text);
      text-decoration: none;
    }}
    .breadcrumb a:hover {{ text-decoration: underline; }}
    .breadcrumb .sep {{
      color: var(--muted);
      font-weight: 400;
    }}
    button {{
      border: 1px solid var(--border);
      background: #303030;
      color: var(--text);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      cursor: pointer;
      min-height: 38px;
    }}
    button:hover {{ background: #3a3a3a; }}
    button:disabled {{
      opacity: 0.45;
      cursor: default;
    }}
    .nav-button-pair {{
      display: inline-flex;
      align-items: stretch;
      gap: 0;
    }}
    .nav-button-pair button {{
      border-radius: 0;
      justify-content: center;
    }}
    .nav-button-pair button + button {{ margin-left: -1px; }}
    .nav-button-pair button:first-child {{
      border-top-left-radius: 6px;
      border-bottom-left-radius: 6px;
      padding-right: 0;
    }}
    .nav-button-pair button:last-child {{
      border-top-right-radius: 6px;
      border-bottom-right-radius: 6px;
      padding-left: 0;
    }}
    .status {{
      color: var(--muted);
      font-size: 14px;
    }}
    main {{
      min-height: 0;
      display: grid;
      place-items: center;
      overflow: hidden;
    }}
    .viewer {{
      width: 100%;
      height: 100%;
      min-height: 0;
      min-width: 0;
      display: grid;
      place-items: center;
      background: #0e0e0e;
      overflow: hidden;
    }}
    .media-link {{
      width: 100%;
      height: 100%;
      min-width: 0;
      min-height: 0;
      display: grid;
      place-items: center;
    }}
    .viewer img, .viewer video {{
      min-width: 0;
      min-height: 0;
      width: auto;
      height: auto;
      max-width: 100%;
      max-height: 100%;
      object-fit: contain;
      object-position: center center;
      display: block;
    }}
{VIEW_ROTATION_CSS}
    .month-grid {{
      width: 100%;
      height: 100%;
      min-width: 0;
      min-height: 0;
      overflow: auto;
      padding: 12px;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
      grid-auto-rows: 130px;
      gap: 8px;
      align-content: start;
    }}
    .thumb {{
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #181818;
      color: var(--muted);
      display: grid;
      place-items: center;
      min-width: 0;
      min-height: 0;
      overflow: hidden;
      padding: 0;
      font: inherit;
      text-align: center;
    }}
    .thumb:hover {{
      border-color: var(--accent);
      background: #242424;
    }}
    .thumb img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .thumb-video {{
      padding: 10px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .overview-thumb {{
      position: relative;
    }}
    .overview-label {{
      position: absolute;
      inset: auto 0 0;
      padding: 7px 8px;
      background: rgb(0 0 0 / 78%);
      color: var(--text);
      font-weight: 650;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .file-card {{
      padding: 16px;
      color: var(--muted);
      line-height: 1.35;
      text-align: center;
      overflow-wrap: anywhere;
    }}
    .empty {{
      color: var(--muted);
      text-align: center;
      max-width: 560px;
      line-height: 1.5;
      padding: 24px;
    }}
"""


@dataclass
class BrowserExportTiming:
    steps: list[tuple[str, float]] = field(default_factory=list)

    def measure(self, name: str, start: float) -> float:
        now = perf_counter()
        self.steps.append((name, now - start))
        return now


def export_html(
    target: Path,
    output: Path | None = None,
    *,
    month_preview_limit: int | None = None,
    hide_out_of_focus: bool = False,
    debug_timing: bool = False,
) -> Path:
    timing = BrowserExportTiming() if debug_timing else None
    start = perf_counter()
    output_path = output or (target / "index.html")
    if timing is not None:
        start = timing.measure("resolve_output", start)
    items = browser_items(
        target,
        hide_out_of_focus=hide_out_of_focus,
        timing=timing,
    )
    if timing is not None:
        start = timing.measure("browser_items", start)
    html_text = render_html(items, month_preview_limit=month_preview_limit)
    if timing is not None:
        start = timing.measure("render_html", start)
    output_path.write_text(
        html_text,
        encoding="utf-8",
        newline="\n",
    )
    if timing is not None:
        timing.measure("write_file", start)
        print_browser_export_timing(timing, item_count=len(items))
    return output_path


def browser_items(
    target: Path,
    *,
    hide_out_of_focus: bool = False,
    timing: BrowserExportTiming | None = None,
) -> list[dict[str, object]]:
    from .server_browser_queries import all_source_items

    start = perf_counter()
    items = [row_to_item(target, row) for row in all_source_items(target, hide_out_of_focus=hide_out_of_focus)]
    if timing is not None:
        timing.measure("rows_to_items", start)
    return items


def print_browser_export_timing(timing: BrowserExportTiming, *, item_count: int) -> None:
    print("make-browser timing:", file=sys.stderr)
    for name, seconds in timing.steps:
        print(f"  {name}: {seconds:.3f}s", file=sys.stderr)
    print(f"  items: {item_count}", file=sys.stderr)


def export_html_conflicts(
    target: Path,
    output: Path | None = None,
    *,
    target_locked: bool = False,
) -> Path:
    output_path = output or (target / "name-conflicts.html")
    conn = db.connect(target)
    try:
        with MediaMetadataCache(target, conn, target_locked=target_locked) as media_cache:
            conflicts = conflict_groups(target, list(db.conflict_candidate_files(conn)), media_cache=media_cache)
    finally:
        conn.close()

    output_path.write_text(render_conflicts_html(conflicts), encoding="utf-8", newline="\n")
    return output_path


def row_to_item(
    target: Path,
    row,
    *,
    media_cache: MediaMetadataCache | None = None,
) -> dict[str, object]:
    stored_path = Path(str(row["target_path"]))
    relative_path = relative_to_target(target, stored_path)
    return static_browser_item(row, relative_path, target=target)


def browser_date_text(row) -> str:
    return item_browser_date_text(row)


def manual_date_midpoint(date_from: object, date_to: object) -> dt.date | None:
    return shared_manual_date_midpoint(date_from, date_to)


def face_tables_exist(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN ('faces', 'persons', 'person_faces', 'person_files', 'face_suggestions')
        """
    )
    return {str(row["name"]) for row in rows} == {
        "faces",
        "persons",
        "person_faces",
        "person_files",
        "face_suggestions",
    }

def conflict_groups(
    target: Path,
    rows: list,
    *,
    media_cache: MediaMetadataCache | None = None,
) -> list[dict]:
    grouped: dict[tuple[str, str], list] = {}
    for row in rows:
        target_path = Path(str(row["target_path"]))
        key = (db.relative_path_key(target_path.parent), str(row["original_filename"]))
        grouped.setdefault(key, []).append(row)

    conflicts = []
    for (_parent_key, original_filename), items in grouped.items():
        if len(items) < 2 or not any(item["name_conflict"] for item in items):
            continue
        first_target = Path(str(items[0]["target_path"]))
        conflicts.append(
            {
                "originalFilename": original_filename,
                "targetDir": display_relative_path(target, first_target.parent),
                "items": [conflict_row_to_item(target, item, media_cache=media_cache) for item in items],
            }
        )
    conflicts.sort(key=lambda item: (item["targetDir"], item["originalFilename"]))
    return conflicts


def conflict_row_to_item(
    target: Path,
    row,
    *,
    media_cache: MediaMetadataCache | None = None,
) -> dict[str, object]:
    target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
    relative_path = relative_to_target(target, target_path)
    dimensions = media_cache.image_dimensions(target_path) if media_cache is not None else image_dimensions(target_path)
    return {
        "storedFilename": row["stored_filename"],
        "originalFilename": row["original_filename"],
        "targetPath": display_relative_path(target, target_path),
        "sourcePath": str(row["source_path"]),
        "sourceId": row["source_id"],
        "takenDate": row["taken_date"] or "-",
        "dateSource": row["date_source"],
        "dimensions": f"{dimensions.width}x{dimensions.height}" if dimensions else "-",
        "sizeBytes": int(row["size_bytes"]),
        "sizeText": format_bytes(int(row["size_bytes"])),
        "sha256": row["sha256"],
        "sourceExists": Path(str(row["source_path"])).exists(),
        "url": path_to_url(relative_path),
        "kind": media_kind(target_path),
        "viewRotation": db.normalize_view_rotation(row["view_rotation_degrees"]),
    }


def month_key_from_path(path: Path) -> str:
    return shared_month_key_from_path(path)


def render_html(
    items: list[dict[str, object]],
    *,
    title: str = "Bildebrowser",
    month_preview_limit: int | None = None,
) -> str:
    items_json = (
        json.dumps(items, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    month_preview_limit_json = json.dumps(month_preview_limit)
    month_names_json = json.dumps(STATIC_BROWSER_MONTH_NAMES, ensure_ascii=False)
    escaped_title = html.escape(title)
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
{STATIC_BROWSER_CSS}
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div class="topline">
        <div id="title" class="title">
          <nav class="breadcrumb" aria-label="Plassering"><span>År</span></nav>
        </div>
        <span id="status" class="status"></span>
      </div>
      <nav class="controls" aria-label="Navigering">
        <span class="nav-button-pair" data-nav-button-pair="year">
          <button id="prevYear" type="button" title="Forrige år">◀ Å</button>
          <button id="nextYear" type="button" title="Neste år">r ▶</button>
        </span>
        <span class="nav-button-pair" data-nav-button-pair="month">
          <button id="prevMonth" type="button" title="Forrige måned">◀ Mån</button>
          <button id="nextMonth" type="button" title="Neste måned">ed ▶</button>
        </span>
        <span class="nav-button-pair" data-nav-button-pair="item">
          <button id="prevItem" type="button" title="Forrige bilde">◀ Bil</button>
          <button id="nextItem" type="button" title="Neste bilde">de ▶</button>
        </span>
      </nav>
    </header>
    <main>
      <div id="viewer" class="viewer">
        <div class="empty">Ingen filer i indeksen.</div>
      </div>
    </main>
  </div>
  <script>
    const embeddedItems = {items_json};
    const MONTH_PREVIEW_LIMIT = {month_preview_limit_json};
    const MONTH_NAMES = {month_names_json};
    const state = {{ months: [], years: [], monthIndex: 0, itemIndex: 0, viewMode: "item" }};
    const titleEl = document.getElementById("title");
    const statusEl = document.getElementById("status");
    const viewer = document.getElementById("viewer");
    const buttons = {{
      prevYear: document.getElementById("prevYear"),
      nextYear: document.getElementById("nextYear"),
      prevMonth: document.getElementById("prevMonth"),
      nextMonth: document.getElementById("nextMonth"),
      prevItem: document.getElementById("prevItem"),
      nextItem: document.getElementById("nextItem")
    }};
    buttons.prevYear.addEventListener("click", () => moveYear(-1));
    buttons.nextYear.addEventListener("click", () => moveYear(1));
    buttons.prevMonth.addEventListener("click", () => moveMonth(-1));
    buttons.nextMonth.addEventListener("click", () => moveMonth(1));
    buttons.prevItem.addEventListener("click", () => moveItem(-1));
    buttons.nextItem.addEventListener("click", () => moveItem(1));
    document.addEventListener("keydown", event => {{
      if (event.key === "ArrowLeft") {{
        event.preventDefault();
        moveItem(-1);
      }}
      if (event.key === "ArrowRight") {{
        event.preventDefault();
        moveItem(1);
      }}
      if (event.key === "ArrowUp") {{
        event.preventDefault();
        moveMonth(-1);
      }}
      if (event.key === "ArrowDown") {{
        event.preventDefault();
        moveMonth(1);
      }}
      if (event.key === "PageUp") {{
        event.preventDefault();
        moveYear(-1);
      }}
      if (event.key === "PageDown") {{
        event.preventDefault();
        moveYear(1);
      }}
    }});
    function attachSwipeNavigation(container, onSwipe) {{
      if (!container) return;
      const minDistance = 60;
      const maxTapDrift = 10;
      let start = null;
      let suppressNextClick = false;
      function startSwipe(x, y, pointerId = null) {{
        start = {{ x, y, pointerId }};
      }}
      function finishSwipe(x, y, pointerId = null) {{
        if (!start) return false;
        if (start.pointerId !== null && pointerId !== null && start.pointerId !== pointerId) return false;
        const dx = x - start.x;
        const dy = y - start.y;
        start = null;
        const absX = Math.abs(dx);
        const absY = Math.abs(dy);
        if (absX <= maxTapDrift && absY <= maxTapDrift) return false;
        if (absX < minDistance || absX <= absY * 1.4) return false;
        suppressNextClick = true;
        onSwipe(dx < 0 ? 1 : -1);
        return true;
      }}
      container.addEventListener("click", event => {{
        if (!suppressNextClick) return;
        suppressNextClick = false;
        event.preventDefault();
        event.stopPropagation();
      }}, true);
      if (window.PointerEvent) {{
        container.addEventListener("pointerdown", event => {{
          if (event.pointerType !== "touch" && event.pointerType !== "pen") return;
          startSwipe(event.clientX, event.clientY, event.pointerId);
        }});
        container.addEventListener("pointerup", event => {{
          if (event.pointerType !== "touch" && event.pointerType !== "pen") return;
          if (finishSwipe(event.clientX, event.clientY, event.pointerId)) event.preventDefault();
        }});
        container.addEventListener("pointercancel", () => {{
          start = null;
        }});
        return;
      }}
      container.addEventListener("touchstart", event => {{
        if (event.changedTouches.length !== 1) return;
        const touch = event.changedTouches[0];
        startSwipe(touch.clientX, touch.clientY);
      }}, {{ passive: true }});
      container.addEventListener("touchend", event => {{
        if (event.changedTouches.length !== 1) return;
        const touch = event.changedTouches[0];
        if (finishSwipe(touch.clientX, touch.clientY)) event.preventDefault();
      }}, {{ passive: false }});
      container.addEventListener("touchcancel", () => {{
        start = null;
      }}, {{ passive: true }});
    }}
    attachSwipeNavigation(viewer, direction => {{
      if (state.viewMode !== "item") return;
      moveItem(direction);
    }});
    init();
    function init() {{
      const items = embeddedItems.slice().sort(compareItems);
      state.months = buildMonths(items);
      state.years = buildYears(state.months);
      state.monthIndex = 0;
      state.itemIndex = 0;
      state.viewMode = "item";
      statusEl.textContent = `${{items.length}} filer, ${{state.months.length}} måneder`;
      if (items.length === 0) {{
        renderBreadcrumb();
        setButtonsEnabled(false);
        return;
      }}
      setButtonsEnabled(true);
      render();
    }}
    function compareItems(a, b) {{
      const aDate = a.browserDate || "9999-99-99";
      const bDate = b.browserDate || "9999-99-99";
      return a.monthKey.localeCompare(b.monthKey, "nb") ||
        aDate.localeCompare(bDate, "nb") ||
        a.path.localeCompare(b.path, "nb", {{ numeric: true }});
    }}
    function buildMonths(items) {{
      const map = new Map();
      for (const item of items) {{
        if (!map.has(item.monthKey)) map.set(item.monthKey, []);
        map.get(item.monthKey).push(item);
      }}
      return Array.from(map.entries()).map(([key, monthItems]) => ({{ key, items: monthItems }}));
    }}
    function buildYears(months) {{
      const map = new Map();
      for (const month of months) {{
        const key = yearKeyForMonth(month);
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(month);
      }}
      return Array.from(map.entries()).map(([key, yearMonths]) => ({{ key, months: yearMonths }}));
    }}
    function yearKeyForMonth(month) {{
      return /^\\d{{4}}-\\d{{2}}$/.test(month.key) ? month.key.slice(0, 4) : month.key;
    }}
    function yearLabel(key) {{
      if (key === "udatert") return "Udatert";
      if (key === "ukjent") return "Ukjent";
      return key;
    }}
    function monthLabel(key) {{
      if (key === "udatert") return "Udatert";
      if (key === "ukjent") return "Ukjent";
      return MONTH_NAMES[key.slice(5, 7)] || key;
    }}
    function currentMonth() {{ return state.months[state.monthIndex] || null; }}
    function currentYear() {{
      const month = currentMonth();
      if (!month) return null;
      return state.years.find(year => year.key === yearKeyForMonth(month)) || null;
    }}
    function currentItem() {{
      const month = currentMonth();
      return month ? month.items[state.itemIndex] : null;
    }}
    function moveYear(delta) {{
      const month = currentMonth();
      if (!month) return;
      const year = yearKeyForMonth(month);
      const years = state.years.map(item => item.key);
      const nextYear = years[years.indexOf(year) + delta];
      if (!nextYear) return;
      state.monthIndex = state.months.findIndex(item => yearKeyForMonth(item) === nextYear);
      state.itemIndex = 0;
      state.viewMode = "month";
      render();
    }}
    function moveMonth(delta) {{
      const next = state.monthIndex + delta;
      if (next < 0 || next >= state.months.length) return;
      state.monthIndex = next;
      state.itemIndex = 0;
      state.viewMode = "month";
      render();
    }}
    function moveItem(delta) {{
      const month = currentMonth();
      if (!month) return;
      if (state.viewMode !== "item") {{
        state.itemIndex = delta < 0 ? month.items.length - 1 : 0;
        state.viewMode = "item";
        render();
        return;
      }}
      const nextItem = state.itemIndex + delta;
      if (nextItem >= 0 && nextItem < month.items.length) {{
        state.itemIndex = nextItem;
        render();
        return;
      }}
      const nextMonth = state.monthIndex + (delta > 0 ? 1 : -1);
      if (nextMonth < 0 || nextMonth >= state.months.length) return;
      state.monthIndex = nextMonth;
      state.itemIndex = delta > 0 ? 0 : state.months[nextMonth].items.length - 1;
      render();
    }}
    function render() {{
      renderBreadcrumb();
      if (state.viewMode === "years") {{
        renderYears();
        return;
      }}
      if (state.viewMode === "year") {{
        renderYear();
        return;
      }}
      if (state.viewMode === "month") {{
        renderMonth();
        return;
      }}
      renderItem();
    }}
    function renderItem() {{
      const item = currentItem();
      if (!item) return;
      if (item.kind === "video") {{
        const video = document.createElement("video");
        video.src = item.url;
        video.controls = true;
        viewer.replaceChildren(video);
      }} else if (item.kind === "image") {{
        const link = document.createElement("a");
        link.className = "media-link";
        link.href = item.url;
        link.target = "_blank";
        const img = document.createElement("img");
        img.src = item.url;
        img.alt = item.name;
        link.append(img);
        applyImageViewRotation(img, item, "contain");
        viewer.replaceChildren(link);
      }} else {{
        const link = document.createElement("a");
        link.className = "media-link file-card";
        link.href = item.url;
        link.target = "_blank";
        link.textContent = `Fil\\n${{item.name}}`;
        viewer.replaceChildren(link);
      }}
      updateButtons();
    }}
    function renderYears() {{
      const grid = document.createElement("div");
      grid.className = "month-grid";
      for (const year of state.years) {{
        const itemCount = year.months.reduce((sum, month) => sum + month.items.length, 0);
        const button = overviewButton(
          year.months[0].items[0],
          `${{yearLabel(year.key)}} (${{itemCount}} filer)`,
          () => {{
            state.monthIndex = state.months.indexOf(year.months[0]);
            state.itemIndex = 0;
            state.viewMode = year.key === "udatert" || year.key === "ukjent" ? "month" : "year";
            render();
          }}
        );
        grid.append(button);
      }}
      viewer.replaceChildren(grid);
      updateButtons();
    }}
    function renderYear() {{
      const year = currentYear();
      if (!year) return;
      const grid = document.createElement("div");
      grid.className = "month-grid";
      for (const month of year.months) {{
        const button = overviewButton(
          month.items[0],
          `${{monthLabel(month.key)}} (${{month.items.length}} filer)`,
          () => {{
            state.monthIndex = state.months.indexOf(month);
            state.itemIndex = 0;
            state.viewMode = "month";
            render();
          }}
        );
        grid.append(button);
      }}
      viewer.replaceChildren(grid);
      updateButtons();
    }}
    function renderMonth() {{
      const month = currentMonth();
      if (!month) return;
      const grid = document.createElement("div");
      grid.className = "month-grid";
      for (const item of representativeItems(month.items, MONTH_PREVIEW_LIMIT)) {{
        const index = month.items.indexOf(item);
        const button = document.createElement("button");
        button.className = "thumb";
        button.type = "button";
        button.title = item.path;
        button.addEventListener("click", () => {{
          state.itemIndex = index;
          state.viewMode = "item";
          render();
        }});
        if (item.kind === "image") {{
          const img = document.createElement("img");
          img.src = item.thumbnailSrc || item.url;
          img.alt = item.name;
          img.loading = "lazy";
          button.append(img);
          applyImageViewRotation(img, item, "cover");
        }} else {{
          const label = document.createElement("span");
          label.className = "thumb-video";
          label.textContent = `${{item.kind === "video" ? "Video" : "Fil"}}\\n${{item.name}}`;
          button.append(label);
        }}
        grid.append(button);
      }}
      viewer.replaceChildren(grid);
      updateButtons();
    }}
    function overviewButton(item, labelText, onClick) {{
      const button = document.createElement("button");
      button.className = "thumb overview-thumb";
      button.type = "button";
      button.title = labelText;
      button.addEventListener("click", onClick);
      appendThumbnail(button, item);
      const label = document.createElement("span");
      label.className = "overview-label";
      label.textContent = labelText;
      button.append(label);
      return button;
    }}
    function appendThumbnail(container, item) {{
      if (item.kind === "image") {{
        const img = document.createElement("img");
        img.src = item.thumbnailSrc || item.url;
        img.alt = item.name;
        img.loading = "lazy";
        container.append(img);
        applyImageViewRotation(img, item, "cover");
      }} else {{
        const label = document.createElement("span");
        label.className = "thumb-video";
        label.textContent = `${{item.kind === "video" ? "Video" : "Fil"}}\\n${{item.name}}`;
        container.append(label);
      }}
    }}
    function showYears(event) {{
      if (event) event.preventDefault();
      state.viewMode = "years";
      render();
    }}
    function showYear(yearKey, event) {{
      if (event) event.preventDefault();
      const year = state.years.find(candidate => candidate.key === yearKey);
      if (!year) return;
      state.monthIndex = state.months.indexOf(year.months[0]);
      state.itemIndex = 0;
      state.viewMode = yearKey === "udatert" || yearKey === "ukjent" ? "month" : "year";
      render();
    }}
    function showMonth(monthKey, event) {{
      if (event) event.preventDefault();
      const index = state.months.findIndex(month => month.key === monthKey);
      if (index < 0) return;
      state.monthIndex = index;
      state.itemIndex = 0;
      state.viewMode = "month";
      render();
    }}
    function renderBreadcrumb() {{
      const nav = document.createElement("nav");
      nav.className = "breadcrumb";
      nav.setAttribute("aria-label", "Plassering");
      const month = currentMonth();
      const item = currentItem();
      const yearKey = month ? yearKeyForMonth(month) : "";
      const parts = [];
      if (state.viewMode === "years" || !month) {{
        parts.push({{ label: "År" }});
      }} else {{
        parts.push({{ label: "År", action: showYears }});
        if (yearKey === "udatert" || yearKey === "ukjent") {{
          parts.push({{
            label: yearLabel(yearKey),
            action: state.viewMode === "item" ? event => showMonth(month.key, event) : null
          }});
        }} else {{
          parts.push({{
            label: yearLabel(yearKey),
            action: state.viewMode === "year" ? null : event => showYear(yearKey, event)
          }});
          if (state.viewMode === "month" || state.viewMode === "item") {{
            parts.push({{
              label: monthLabel(month.key),
              action: state.viewMode === "item" ? event => showMonth(month.key, event) : null
            }});
          }}
        }}
        if (state.viewMode === "item" && item) parts.push({{ label: item.name }});
      }}
      parts.forEach((part, index) => {{
        if (index > 0) {{
          const separator = document.createElement("span");
          separator.className = "sep";
          separator.textContent = "/";
          nav.append(separator);
        }}
        if (part.action) {{
          const link = document.createElement("a");
          link.href = "#";
          link.textContent = part.label;
          link.addEventListener("click", part.action);
          nav.append(link);
        }} else {{
          const text = document.createElement("span");
          text.textContent = part.label;
          nav.append(text);
        }}
      }});
      titleEl.replaceChildren(nav);
    }}
    function representativeItems(items, limit) {{
      if (limit === null) return items;
      if (items.length <= limit) return items;
      if (limit === 1) return [items[0]];
      const selected = [];
      const last = items.length - 1;
      const selectedIndexes = new Set();
      for (let i = 0; i < limit; i += 1) {{
        selectedIndexes.add(Math.round((i * last) / (limit - 1)));
      }}
      for (const index of Array.from(selectedIndexes).sort((a, b) => a - b)) {{
        selected.push(items[index]);
      }}
      return selected;
    }}
{VIEW_ROTATION_JAVASCRIPT}
    function setButtonsEnabled(enabled) {{
      for (const button of Object.values(buttons)) button.disabled = !enabled;
    }}
    function updateButtons() {{
      const month = currentMonth();
      const years = state.years.map(item => item.key);
      const currentYear = month ? yearKeyForMonth(month) : "";
      const currentYearIndex = years.indexOf(currentYear);
      buttons.prevYear.disabled = currentYearIndex <= 0;
      buttons.nextYear.disabled = currentYearIndex < 0 || currentYearIndex >= years.length - 1;
      buttons.prevMonth.disabled = state.monthIndex <= 0;
      buttons.nextMonth.disabled = state.monthIndex >= state.months.length - 1;
      if (state.viewMode === "month") {{
        buttons.prevItem.disabled = false;
        buttons.nextItem.disabled = false;
        return;
      }}
      buttons.prevItem.disabled = state.monthIndex === 0 && state.itemIndex === 0;
      buttons.nextItem.disabled =
        state.monthIndex === state.months.length - 1 &&
        month &&
        state.itemIndex === month.items.length - 1;
    }}
  </script>
</body>
</html>
"""


def render_conflicts_html(conflicts: list[dict]) -> str:
    conflicts_json = json.dumps(conflicts, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Navnekollisjoner</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #181818;
      --panel: #242424;
      --panel-2: #2d2d2d;
      --border: #404040;
      --text: #f2f2f2;
      --muted: #b8b8b8;
      --accent: #7db7ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .app {{
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }}
    header {{
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 12px;
      display: grid;
      gap: 10px;
    }}
    .topline, .controls {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .title {{
      font-weight: 700;
      margin-right: 12px;
    }}
    .status {{
      color: var(--muted);
      font-size: 14px;
    }}
    button {{
      border: 1px solid var(--border);
      background: #303030;
      color: var(--text);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      cursor: pointer;
      min-height: 38px;
    }}
    button:hover {{ background: #3a3a3a; }}
    button:disabled {{
      opacity: 0.45;
      cursor: default;
    }}
    main {{
      min-height: 0;
      overflow: auto;
      padding: 14px;
    }}
    .heading {{
      margin: 0 0 12px;
      display: grid;
      gap: 4px;
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
      line-height: 1.25;
    }}
    .target-dir {{
      color: var(--muted);
      font-size: 14px;
      overflow-wrap: anywhere;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
      align-items: start;
    }}
    .item {{
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel);
      overflow: hidden;
    }}
    .media {{
      height: min(52vh, 520px);
      min-height: 220px;
      background: #0f0f0f;
      display: grid;
      place-items: center;
      border-top: 1px solid var(--border);
      overflow: hidden;
      min-width: 0;
      min-height: 0;
    }}
    .media-link {{
      width: 100%;
      height: 100%;
      min-width: 0;
      min-height: 0;
      display: grid;
      place-items: center;
    }}
    .media img, .media video {{
      width: 100%;
      height: 100%;
      max-width: 100%;
      max-height: 100%;
      object-fit: contain;
      object-position: center center;
      display: block;
    }}
{VIEW_ROTATION_CSS}
    .file-card {{
      width: 100%;
      height: 100%;
      display: grid;
      place-items: center;
      padding: 16px;
      color: var(--muted);
      line-height: 1.35;
      text-align: center;
      overflow-wrap: anywhere;
    }}
    .meta {{
      display: grid;
      gap: 7px;
      padding: 10px;
      font-size: 13px;
    }}
    .name {{
      font-size: 15px;
      font-weight: 650;
      overflow-wrap: anywhere;
    }}
    .row {{
      display: grid;
      grid-template-columns: 92px minmax(0, 1fr);
      gap: 8px;
      line-height: 1.35;
    }}
    .label {{
      color: var(--muted);
    }}
    .value {{
      overflow-wrap: anywhere;
    }}
    .empty {{
      color: var(--muted);
      text-align: center;
      padding: 48px 16px;
      line-height: 1.5;
    }}
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div class="topline">
        <div class="title">Navnekollisjoner</div>
        <span id="status" class="status"></span>
      </div>
      <div class="controls">
        <button id="prevConflict" type="button">Forrige konflikt</button>
        <button id="nextConflict" type="button">Neste konflikt</button>
      </div>
    </header>
    <main id="main">
      <div class="empty">Ingen navnekollisjoner i indeksen.</div>
    </main>
  </div>
  <script>
    const conflicts = {conflicts_json};
    const state = {{ index: 0 }};
    const main = document.getElementById("main");
    const statusEl = document.getElementById("status");
    const buttons = {{
      prev: document.getElementById("prevConflict"),
      next: document.getElementById("nextConflict")
    }};
    buttons.prev.addEventListener("click", () => move(-1));
    buttons.next.addEventListener("click", () => move(1));
    document.addEventListener("keydown", event => {{
      if (event.key === "ArrowLeft") move(-1);
      if (event.key === "ArrowRight") move(1);
    }});
    init();
    function init() {{
      statusEl.textContent = `${{conflicts.length}} konflikter`;
      if (conflicts.length === 0) {{
        buttons.prev.disabled = true;
        buttons.next.disabled = true;
        return;
      }}
      render();
    }}
    function move(delta) {{
      const next = state.index + delta;
      if (next < 0 || next >= conflicts.length) return;
      state.index = next;
      render();
    }}
    function render() {{
      const conflict = conflicts[state.index];
      buttons.prev.disabled = state.index <= 0;
      buttons.next.disabled = state.index >= conflicts.length - 1;

      const heading = document.createElement("section");
      heading.className = "heading";
      const title = document.createElement("h1");
      title.textContent = conflict.originalFilename;
      const targetDir = document.createElement("div");
      targetDir.className = "target-dir";
      targetDir.textContent = conflict.targetDir;
      heading.append(title, targetDir);

      const grid = document.createElement("section");
      grid.className = "grid";
      for (const item of conflict.items) {{
        grid.append(renderItem(item));
      }}
      main.replaceChildren(heading, grid);
    }}
    function renderItem(item) {{
      const article = document.createElement("article");
      article.className = "item";
      const media = document.createElement("div");
      media.className = "media";
      if (item.kind === "video") {{
        const video = document.createElement("video");
        video.src = item.url;
        video.controls = true;
        media.append(video);
      }} else if (item.kind === "image") {{
        const link = document.createElement("a");
        link.className = "media-link";
        link.href = item.url;
        link.target = "_blank";
        const img = document.createElement("img");
        img.src = item.url;
        img.alt = item.storedFilename;
        link.append(img);
        applyImageViewRotation(img, item, "contain");
        media.append(link);
      }} else {{
        const link = document.createElement("a");
        link.className = "media-link file-card";
        link.href = item.url;
        link.target = "_blank";
        link.textContent = `Fil\\n${{item.storedFilename}}`;
        media.append(link);
      }}
      const meta = document.createElement("div");
      meta.className = "meta";
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = item.storedFilename;
      meta.append(
        name,
        row("mål", item.targetPath),
        row("kilde", item.sourcePath),
        row("kilde-id", item.sourceId),
        row("dato", `${{item.takenDate}} (${{item.dateSource}})`),
        row("oppløsning", item.dimensions),
        row("filstørrelse", `${{item.sizeText}} (${{item.sizeBytes}} bytes)`),
        row("sha256", item.sha256),
        row("kildefil", item.sourceExists ? "finnes" : "finnes ikke")
      );
      article.append(meta, media);
      return article;
    }}
    function row(label, value) {{
      const div = document.createElement("div");
      div.className = "row";
      const labelEl = document.createElement("div");
      labelEl.className = "label";
      labelEl.textContent = label;
      const valueEl = document.createElement("div");
      valueEl.className = "value";
      valueEl.textContent = String(value);
      div.append(labelEl, valueEl);
      return div;
    }}
{VIEW_ROTATION_JAVASCRIPT}
  </script>
</body>
</html>
"""
