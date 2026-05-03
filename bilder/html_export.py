from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote

from . import db
from .media import image_dimensions


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


def export_html_conflicts(target: Path, output: Path | None = None) -> Path:
    output_path = output or (target / "name-conflicts.html")
    conn = db.connect(target)
    try:
        conflicts = conflict_groups(target, list(db.conflict_candidate_files(conn)))
    finally:
        conn.close()

    output_path.write_text(render_conflicts_html(conflicts), encoding="utf-8", newline="\n")
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


def conflict_groups(target: Path, rows: list) -> list[dict]:
    grouped: dict[tuple[str, str], list] = {}
    for row in rows:
        target_path = Path(str(row["target_path"]))
        key = (db.path_key(target_path.parent), str(row["original_filename"]))
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
                "items": [conflict_row_to_item(target, item) for item in items],
            }
        )
    conflicts.sort(key=lambda item: (item["targetDir"], item["originalFilename"]))
    return conflicts


def conflict_row_to_item(target: Path, row) -> dict[str, object]:
    target_path = Path(str(row["target_path"]))
    relative_path = relative_to_target(target, target_path)
    ext = target_path.suffix.lower().lstrip(".")
    dimensions = image_dimensions(target_path)
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
        "kind": "video" if ext in VIDEO_EXTENSIONS else "image",
    }


def relative_to_target(target: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(target.resolve())
    except ValueError:
        return Path(os.path.relpath(path, target))


def display_relative_path(target: Path, path: Path) -> str:
    return relative_to_target(target, path).as_posix()


def format_bytes(size: int) -> str:
    units = ("bytes", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "bytes":
                return f"{size} bytes"
            return f"{value:.1f} {unit}"
        value /= 1024


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
      min-width: 0;
      display: grid;
      place-items: center;
      background: #0e0e0e;
      border: 1px solid var(--border);
      border-radius: 8px;
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
      max-width: 100vw;
      max-height: calc(100vh - 10rem);
      width: 100%;
      height: 100%;
      object-fit: contain;
      object-position: center center;
      display: block;
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
        const link = document.createElement("a");
        link.className = "media-link";
        link.href = item.url;
        link.target = "_blank";
        const img = document.createElement("img");
        img.src = item.url;
        img.alt = item.name;
        link.append(img);
        viewer.replaceChildren(link);
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
    .position {{
      color: var(--accent);
      font-weight: 650;
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
        <span id="position" class="position"></span>
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
    const positionEl = document.getElementById("position");
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
      positionEl.textContent = `${{state.index + 1}}/${{conflicts.length}}`;
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
      }} else {{
        const link = document.createElement("a");
        link.className = "media-link";
        link.href = item.url;
        link.target = "_blank";
        const img = document.createElement("img");
        img.src = item.url;
        img.alt = item.storedFilename;
        link.append(img);
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
  </script>
</body>
</html>
"""
