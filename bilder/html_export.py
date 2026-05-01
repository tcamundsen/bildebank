from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote

from . import db


IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "heic", "heif"}
VIDEO_EXTENSIONS = {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}


def export_html(target: Path, output: Path | None = None) -> Path:
    output_path = output or (target / "index.html")
    conn = db.connect(target)
    try:
        items = [row_to_item(target, row) for row in db.browser_files(conn)]
    finally:
        conn.close()

    output_path.write_text(render_html(items), encoding="utf-8", newline="\n")
    return output_path


def row_to_item(target: Path, row) -> dict[str, str]:
    target_path = Path(row["target_path"])
    try:
        relative_path = target_path.resolve().relative_to(target.resolve())
    except ValueError:
        relative_path = Path(os.path.relpath(target_path, target))
    path = relative_path.as_posix()
    ext = target_path.suffix.lower().lstrip(".")
    kind = "video" if ext in VIDEO_EXTENSIONS else "image"
    month_key = month_key_from_path(relative_path)
    return {
        "path": path,
        "url": path_to_url(relative_path),
        "kind": kind,
        "monthKey": month_key,
        "takenDate": row["taken_date"] or "",
        "dateSource": row["date_source"],
        "name": row["stored_filename"],
    }


def path_to_url(path: Path) -> str:
    return "/".join(quote(part) for part in path.parts)


def month_key_from_path(path: Path) -> str:
    parts = path.parts
    if len(parts) >= 3 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].isdigit():
        return f"{parts[0]}-{parts[1]}"
    if parts and parts[0] == "udatert":
        return "udatert"
    return "ukjent"


def render_html(items: list[dict[str, str]]) -> str:
    items_json = json.dumps(items, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bildebrowser</title>
  <style>
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
      grid-template-rows: auto minmax(0, 1fr) auto;
    }}
    header, footer {{
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
    .status {{
      color: var(--muted);
      font-size: 14px;
    }}
    .position {{
      color: var(--accent);
      font-weight: 650;
    }}
    main {{
      min-height: 0;
      display: grid;
      place-items: center;
      padding: 10px;
      overflow: hidden;
    }}
    .viewer {{
      width: 100%;
      height: 100%;
      min-height: 0;
      display: grid;
      place-items: center;
      background: #0e0e0e;
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    img, video {{
      max-width: 100%;
      max-height: 100%;
      width: auto;
      height: auto;
      object-fit: contain;
    }}
    .empty {{
      color: var(--muted);
      text-align: center;
      max-width: 560px;
      line-height: 1.5;
      padding: 24px;
    }}
    footer {{
      border-top: 1px solid var(--border);
      padding: 8px 12px;
      color: var(--muted);
      font-size: 13px;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }}
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div class="topline">
        <div class="title">Bildebrowser</div>
        <span id="status" class="status"></span>
      </div>
      <div class="controls">
        <button id="prevYear" type="button">Forrige år</button>
        <button id="nextYear" type="button">Neste år</button>
        <button id="prevMonth" type="button">Forrige måned</button>
        <button id="nextMonth" type="button">Neste måned</button>
        <button id="prevItem" type="button">Forrige bilde</button>
        <button id="nextItem" type="button">Neste bilde</button>
        <span id="position" class="position"></span>
      </div>
    </header>
    <main>
      <div id="viewer" class="viewer">
        <div class="empty">Ingen filer i indeksen.</div>
      </div>
    </main>
    <footer id="filename">Ingen fil valgt</footer>
  </div>
  <script>
    const embeddedItems = {items_json};
    const state = {{ months: [], monthIndex: 0, itemIndex: 0 }};
    const statusEl = document.getElementById("status");
    const positionEl = document.getElementById("position");
    const viewer = document.getElementById("viewer");
    const filenameEl = document.getElementById("filename");
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
      if (event.key === "ArrowLeft") moveItem(-1);
      if (event.key === "ArrowRight") moveItem(1);
      if (event.key === "ArrowUp") moveMonth(-1);
      if (event.key === "ArrowDown") moveMonth(1);
    }});
    init();
    function init() {{
      const items = embeddedItems.slice().sort(compareItems);
      state.months = buildMonths(items);
      state.monthIndex = 0;
      state.itemIndex = 0;
      statusEl.textContent = `${{items.length}} filer, ${{state.months.length}} måneder`;
      if (items.length === 0) {{
        setButtonsEnabled(false);
        return;
      }}
      setButtonsEnabled(true);
      render();
    }}
    function compareItems(a, b) {{
      return a.monthKey.localeCompare(b.monthKey, "nb") ||
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
    function currentMonth() {{ return state.months[state.monthIndex] || null; }}
    function currentItem() {{
      const month = currentMonth();
      return month ? month.items[state.itemIndex] : null;
    }}
    function moveYear(delta) {{
      const month = currentMonth();
      if (!month) return;
      const year = month.key.slice(0, 4);
      const years = Array.from(new Set(state.months.map(item => item.key.slice(0, 4))));
      const nextYear = years[years.indexOf(year) + delta];
      if (!nextYear) return;
      state.monthIndex = state.months.findIndex(item => item.key.startsWith(nextYear));
      state.itemIndex = 0;
      render();
    }}
    function moveMonth(delta) {{
      const next = state.monthIndex + delta;
      if (next < 0 || next >= state.months.length) return;
      state.monthIndex = next;
      state.itemIndex = 0;
      render();
    }}
    function moveItem(delta) {{
      const month = currentMonth();
      if (!month) return;
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
      const item = currentItem();
      if (!item) return;
      if (item.kind === "video") {{
        const video = document.createElement("video");
        video.src = item.url;
        video.controls = true;
        viewer.replaceChildren(video);
      }} else {{
        const img = document.createElement("img");
        img.src = item.url;
        img.alt = item.name;
        viewer.replaceChildren(img);
      }}
      const month = currentMonth();
      positionEl.textContent = `${{month.key}} ${{state.itemIndex + 1}}/${{month.items.length}}`;
      filenameEl.textContent = htmlDecode(item.path);
      updateButtons();
    }}
    function setButtonsEnabled(enabled) {{
      for (const button of Object.values(buttons)) button.disabled = !enabled;
    }}
    function updateButtons() {{
      const month = currentMonth();
      const years = Array.from(new Set(state.months.map(item => item.key.slice(0, 4))));
      const currentYear = month ? month.key.slice(0, 4) : "";
      const currentYearIndex = years.indexOf(currentYear);
      buttons.prevYear.disabled = currentYearIndex <= 0;
      buttons.nextYear.disabled = currentYearIndex < 0 || currentYearIndex >= years.length - 1;
      buttons.prevMonth.disabled = state.monthIndex <= 0;
      buttons.nextMonth.disabled = state.monthIndex >= state.months.length - 1;
      buttons.prevItem.disabled = state.monthIndex === 0 && state.itemIndex === 0;
      buttons.nextItem.disabled =
        state.monthIndex === state.months.length - 1 &&
        month &&
        state.itemIndex === month.items.length - 1;
    }}
    function htmlDecode(value) {{
      const textarea = document.createElement("textarea");
      textarea.innerHTML = value;
      return textarea.value;
    }}
  </script>
</body>
</html>
"""
