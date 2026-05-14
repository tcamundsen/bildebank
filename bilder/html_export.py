from __future__ import annotations

import html
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from urllib.parse import quote

from . import db
from .media import image_dimensions, image_orientation
from .media_cache import MediaMetadataCache


IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "heic", "heif"}
VIDEO_EXTENSIONS = {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}
FACE_DB_FILENAME = ".bilder-faces.sqlite3"


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
    media_filter: str = "all",
    date_source_filter: str = "all",
    month_preview_limit: int | None = None,
    debug_timing: bool = False,
) -> Path:
    timing = BrowserExportTiming() if debug_timing else None
    start = perf_counter()
    output_path = output or (target / "index.html")
    if timing is not None:
        start = timing.measure("resolve_output", start)
    items = browser_items(
        target,
        media_filter=media_filter,
        date_source_filter=date_source_filter,
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
    media_filter: str = "all",
    date_source_filter: str = "all",
    timing: BrowserExportTiming | None = None,
) -> list[dict[str, object]]:
    conn = db.connect(target)
    try:
        start = perf_counter()
        face_data_by_file_id = face_browser_data_by_file_id(target)
        if timing is not None:
            start = timing.measure("face_data", start)
        items = [
            item
            for item in (
                row_to_item(target, row, face_data_by_file_id=face_data_by_file_id, include_face_boxes=False)
                for row in db.browser_files(conn)
            )
            if item_matches_filters(item, media_filter, date_source_filter)
        ]
        if timing is not None:
            timing.measure("rows_to_items", start)
    finally:
        conn.close()
    return items


def print_browser_export_timing(timing: BrowserExportTiming, *, item_count: int) -> None:
    print("make-browser timing:", file=sys.stderr)
    for name, seconds in timing.steps:
        print(f"  {name}: {seconds:.3f}s", file=sys.stderr)
    print(f"  items: {item_count}", file=sys.stderr)


def export_html_conflicts(target: Path, output: Path | None = None) -> Path:
    output_path = output or (target / "name-conflicts.html")
    conn = db.connect(target)
    try:
        with MediaMetadataCache(target, conn) as media_cache:
            conflicts = conflict_groups(target, list(db.conflict_candidate_files(conn)), media_cache=media_cache)
    finally:
        conn.close()

    output_path.write_text(render_conflicts_html(conflicts), encoding="utf-8", newline="\n")
    return output_path


def row_to_item(
    target: Path,
    row,
    *,
    face_data_by_file_id: dict[int, dict[str, object]] | None = None,
    media_cache: MediaMetadataCache | None = None,
    include_face_boxes: bool = True,
) -> dict[str, object]:
    target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
    relative_path = relative_to_target(target, target_path)
    path = relative_path.as_posix()
    ext = target_path.suffix.lower().lstrip(".")
    kind = "video" if ext in VIDEO_EXTENSIONS else "image"
    month_key = month_key_from_path(relative_path)
    file_id = int(row["id"])
    face_data = (face_data_by_file_id or {}).get(file_id, {})
    return {
        "fileId": file_id,
        "path": path,
        "url": path_to_url(relative_path),
        "kind": kind,
        "monthKey": month_key,
        "takenDate": row["taken_date"] or "",
        "dateSource": row["date_source"],
        "name": row["stored_filename"],
        "sizeText": format_bytes(int(row["size_bytes"])),
        "people": face_data.get("people", []),
        "faces": browser_face_items(
            target_path,
            face_data.get("faces", []),
            media_cache=media_cache,
            include_boxes=include_face_boxes,
        ),
    }


def browser_face_items(
    target_path: Path,
    faces: object,
    *,
    media_cache: MediaMetadataCache | None = None,
    include_boxes: bool = True,
) -> list[dict[str, object]]:
    if not isinstance(faces, list) or not faces:
        return []
    dimensions = None
    orientation = 1
    if include_boxes:
        dimensions = media_cache.image_dimensions(target_path) if media_cache is not None else image_dimensions(target_path)
        orientation = media_cache.image_orientation(target_path) if media_cache is not None else image_orientation(target_path)
    items: list[dict[str, object]] = []
    for face in faces:
        if not isinstance(face, dict):
            continue
        item = {
            "faceId": face["faceId"],
            "score": face["score"],
        }
        if include_boxes and dimensions is not None:
            percent = face_box_percent(face, dimensions, orientation)
            if percent is not None:
                left, top, width, height = percent
                item["left"] = left
                item["top"] = top
                item["boxWidth"] = width
                item["boxHeight"] = height
        items.append(item)
    return items


def face_box_percent(face: dict[str, object], dimensions, orientation: int = 1) -> tuple[float, float, float, float] | None:
    if dimensions.width <= 0 or dimensions.height <= 0:
        return None
    x, y, width, height, box_width, box_height = orient_face_box(
        float(face["x"]),
        float(face["y"]),
        float(face["width"]),
        float(face["height"]),
        dimensions.width,
        dimensions.height,
        orientation,
    )
    if box_width <= 0 or box_height <= 0:
        return None
    return (
        100.0 * x / box_width,
        100.0 * y / box_height,
        100.0 * width / box_width,
        100.0 * height / box_height,
    )


def orient_face_box(
    x: float,
    y: float,
    width: float,
    height: float,
    image_width: int,
    image_height: int,
    orientation: int,
) -> tuple[float, float, float, float, int, int]:
    if orientation == 3:
        return image_width - x - width, image_height - y - height, width, height, image_width, image_height
    if orientation == 6:
        return image_height - y - height, x, height, width, image_height, image_width
    if orientation == 8:
        return y, image_width - x - width, height, width, image_height, image_width
    return x, y, width, height, image_width, image_height


def face_browser_data_by_file_id(target: Path) -> dict[int, dict[str, object]]:
    path = target / FACE_DB_FILENAME
    if not path.exists():
        return {}
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return {}
        people: dict[int, dict[str, str]] = {}
        for row in conn.execute(
            """
            SELECT faces.file_id, persons.name, 'bekreftet' AS status
            FROM person_faces
            JOIN persons ON persons.id = person_faces.person_id
            JOIN faces ON faces.id = person_faces.face_id
            UNION ALL
            SELECT faces.file_id, persons.name, 'forslag' AS status
            FROM face_suggestions
            JOIN persons ON persons.id = face_suggestions.person_id
            JOIN faces ON faces.id = face_suggestions.face_id
            ORDER BY name, status
            """
        ):
            file_id = int(row["file_id"])
            name = str(row["name"])
            key = f"{file_id}\0{name}"
            existing = people.get(key)
            status = str(row["status"])
            if existing is None or existing["status"] != "bekreftet":
                people[key] = {
                    "fileId": str(file_id),
                    "name": name,
                    "status": status,
                    "url": person_page_url(name),
                }
        grouped: dict[int, list[dict[str, str]]] = {}
        for person in sorted(people.values(), key=lambda item: (item["name"], item["status"])):
            file_id = int(person["fileId"])
            grouped.setdefault(file_id, []).append(
                {
                    "name": person["name"],
                    "status": person["status"],
                    "url": person["url"],
                }
            )
        data: dict[int, dict[str, object]] = {
            file_id: {"people": items, "faces": []}
            for file_id, items in grouped.items()
        }
        for row in conn.execute(
            """
            SELECT
                id,
                file_id,
                bbox_x,
                bbox_y,
                bbox_width,
                bbox_height,
                detection_score
            FROM faces
            ORDER BY file_id, id
            """
        ):
            file_id = int(row["file_id"])
            data.setdefault(file_id, {"people": [], "faces": []})
            faces = data[file_id]["faces"]
            if isinstance(faces, list):
                faces.append(
                    {
                        "faceId": int(row["id"]),
                        "x": float(row["bbox_x"]),
                        "y": float(row["bbox_y"]),
                        "width": float(row["bbox_width"]),
                        "height": float(row["bbox_height"]),
                        "score": float(row["detection_score"]),
                    }
                )
        return data
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def face_tables_exist(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN ('faces', 'persons', 'person_faces', 'face_suggestions')
        """
    )
    return {str(row["name"]) for row in rows} == {
        "faces",
        "persons",
        "person_faces",
        "face_suggestions",
    }


def person_page_url(name: str) -> str:
    return path_to_url(Path(f"person-{safe_filename(name)}.html"))


def safe_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in ("-", "_") else "-" for char in value.strip())
    safe = "-".join(part for part in safe.split("-") if part)
    return safe or "person"


def item_matches_filters(item: dict[str, str], media_filter: str, date_source_filter: str) -> bool:
    if media_filter != "all" and item["kind"] != media_filter:
        return False
    if date_source_filter != "all" and item["dateSource"] != date_source_filter:
        return False
    return True


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
    ext = target_path.suffix.lower().lstrip(".")
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
        "kind": "video" if ext in VIDEO_EXTENSIONS else "image",
    }


def relative_to_target(target: Path, path: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(target.resolve())
        except ValueError:
            return Path(os.path.relpath(candidate, target))
    return candidate


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


def render_html(
    items: list[dict[str, object]],
    *,
    month_preview_limit: int | None = None,
    search_url: str | None = None,
) -> str:
    items_json = json.dumps(items, ensure_ascii=False)
    month_preview_limit_json = json.dumps(month_preview_limit)
    search_link = (
        f'<a class="server-search-link" href="{html.escape(search_url)}">Bildesøk</a>'
        if search_url
        else ""
    )
    search_link_css = """
    .server-search-link {
      color: var(--accent);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 7px 10px;
      text-decoration: none;
      background: #303030;
    }
    .server-search-link:hover {
      background: #3a3a3a;
      text-decoration: none;
    }
""" if search_url else ""
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
{search_link_css}
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
      font-size: 13px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      align-items: center;
      min-width: 0;
    }}
    footer a {{
      color: var(--muted);
      text-decoration: none;
    }}
    footer a:hover {{
      color: var(--accent);
      text-decoration: underline;
    }}
    .filename {{
      min-width: 0;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }}
    .people {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      min-width: 0;
      color: var(--muted);
    }}
    .people a.person-link {{
      color: var(--accent);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 2px 7px;
      background: #303030;
      line-height: 1.5;
    }}
    .people a.person-link.suggested {{
      color: #e7c16a;
    }}
    .faces-button {{
      min-height: 28px;
      padding: 3px 8px;
      font-size: 13px;
    }}
    .lightbox {{
      position: fixed;
      inset: 0;
      z-index: 10;
      background: rgb(0 0 0 / 86%);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 8px;
      padding: 12px;
    }}
    .lightbox[hidden] {{ display: none; }}
    .lightbox-bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      color: #fff;
      font-size: 14px;
      min-width: 0;
    }}
    .lightbox-title {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .lightbox-close {{
      border-color: rgb(255 255 255 / 35%);
      background: rgb(255 255 255 / 10%);
      color: #fff;
      min-width: 42px;
    }}
    .lightbox-stage {{
      min-width: 0;
      min-height: 0;
      display: grid;
      place-items: center;
      overflow: auto;
    }}
    .face-list {{
      width: min(1200px, 100%);
      display: grid;
      grid-template-columns: 1fr;
      gap: 18px;
      align-items: start;
    }}
    .face-detail {{
      display: grid;
      gap: 6px;
      color: #fff;
    }}
    .face-detail-title {{
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .lightbox-media {{
      position: relative;
      display: inline-block;
      width: fit-content;
      max-width: 100%;
      justify-self: start;
    }}
    .lightbox-media img {{
      display: block;
      max-width: calc(100vw - 24px);
      width: auto;
      height: auto;
    }}
    .face-box {{
      position: absolute;
      border: 3px solid #ff1f1f;
      background: rgb(255 31 31 / 12%);
      pointer-events: none;
    }}
    .command-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: stretch;
    }}
    .face-command {{
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      font-size: 13px;
      color: #fff;
      background: rgb(255 255 255 / 10%);
      border: 1px solid rgb(255 255 255 / 22%);
      border-radius: 6px;
      padding: 7px;
      overflow-wrap: anywhere;
    }}
    .copy-command {{
      border-color: rgb(255 255 255 / 35%);
      background: rgb(255 255 255 / 10%);
      color: #fff;
      min-width: 70px;
      white-space: nowrap;
    }}
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div class="topline">
        <div class="title">Bildebrowser</div>
        <span id="status" class="status"></span>
        {search_link}
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
    <footer>
      <a id="filename" class="filename" href="#">Ingen fil valgt</a>
      <div id="people" class="people"></div>
    </footer>
  </div>
  <div id="lightbox" class="lightbox" hidden>
    <div class="lightbox-bar">
      <div id="lightboxTitle" class="lightbox-title"></div>
      <button id="lightboxClose" class="lightbox-close" type="button">Lukk</button>
    </div>
    <div id="lightboxStage" class="lightbox-stage"></div>
  </div>
  <script>
    const embeddedItems = {items_json};
    const MONTH_PREVIEW_LIMIT = {month_preview_limit_json};
    const state = {{ months: [], monthIndex: 0, itemIndex: 0, viewMode: "item" }};
    const statusEl = document.getElementById("status");
    const positionEl = document.getElementById("position");
    const viewer = document.getElementById("viewer");
    const filenameEl = document.getElementById("filename");
    const peopleEl = document.getElementById("people");
    const lightboxEl = document.getElementById("lightbox");
    const lightboxTitleEl = document.getElementById("lightboxTitle");
    const lightboxStageEl = document.getElementById("lightboxStage");
    const lightboxCloseButton = document.getElementById("lightboxClose");
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
    lightboxCloseButton.addEventListener("click", closeLightbox);
    lightboxEl.addEventListener("click", event => {{
      if (event.target === lightboxEl || event.target === lightboxStageEl) closeLightbox();
    }});
    document.addEventListener("keydown", event => {{
      if (!lightboxEl.hidden && event.key === "Escape") {{ event.preventDefault(); closeLightbox(); return; }}
      if (!lightboxEl.hidden) return;
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
      state.viewMode = "item";
      statusEl.textContent = `${{items.length}} filer, ${{state.months.length}} måneder`;
      if (items.length === 0) {{
        setButtonsEnabled(false);
        return;
      }}
      setButtonsEnabled(true);
      render();
    }}
    function compareItems(a, b) {{
      const aDate = (a.takenDate && /^\\d{{4}}-\\d{{2}}-\\d{{2}}/.test(a.takenDate)) ? a.takenDate : "9999-99-99";
      const bDate = (b.takenDate && /^\\d{{4}}-\\d{{2}}-\\d{{2}}/.test(b.takenDate)) ? b.takenDate : "9999-99-99";
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
      if (state.viewMode === "month") {{
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
      filenameEl.textContent = `${{htmlDecode(item.path)}} (${{item.sizeText}})`;
      filenameEl.href = item.url;
      filenameEl.target = "_blank";
      renderPeopleAndFaces(item);
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
        }} else {{
          const label = document.createElement("span");
          label.className = "thumb-video";
          label.textContent = `Video\\n${{item.name}}`;
          button.append(label);
        }}
        grid.append(button);
      }}
      viewer.replaceChildren(grid);
      positionEl.textContent = `${{month.key}} oversikt (${{month.items.length}} filer)`;
      filenameEl.textContent = `Månedsoversikt: ${{month.key}}`;
      filenameEl.removeAttribute("href");
      filenameEl.removeAttribute("target");
      peopleEl.replaceChildren();
      updateButtons();
    }}
    function renderPeopleAndFaces(item) {{
      peopleEl.replaceChildren();
      const people = item.people || [];
      const faces = item.faces || [];
      if (people.length) {{
        const label = document.createElement("span");
        label.textContent = "Personer:";
        peopleEl.append(label);
      }}
      for (const person of people) {{
        const link = document.createElement("a");
        link.className = person.status === "forslag" ? "person-link suggested" : "person-link";
        link.href = person.url;
        link.textContent = person.status === "forslag" ? `${{person.name}} (forslag)` : person.name;
        peopleEl.append(link);
      }}
      if (faces.length) {{
        const button = document.createElement("button");
        button.className = "faces-button";
        button.type = "button";
        button.textContent = `Ansikter i bildet (${{faces.length}})`;
        button.title = "Vis face-id og kopier kommando for hvert ansikt";
        button.addEventListener("click", () => openAllFaces(item));
        peopleEl.append(button);
      }}
    }}
    function openAllFaces(item) {{
      lightboxTitleEl.textContent = `Ansikter - ${{item.path}}`;
      const list = document.createElement("div");
      list.className = "face-list";
      for (const face of item.faces || []) {{
        const detail = document.createElement("div");
        detail.className = "face-detail";
        const title = document.createElement("div");
        title.className = "face-detail-title";
        title.textContent = `face-id ${{face.faceId}}, deteksjon ${{face.score.toFixed(3)}}`;
        detail.append(
          title,
          renderMarkedImage(item, face),
          renderCommand(`bildebank face-person-add-face "Navn" ${{face.faceId}}`)
        );
        list.append(detail);
      }}
      lightboxStageEl.replaceChildren(list);
      lightboxEl.hidden = false;
      lightboxCloseButton.focus();
    }}
    function renderMarkedImage(item, face) {{
      const media = document.createElement("div");
      media.className = "lightbox-media";
      const img = document.createElement("img");
      img.src = item.url;
      img.alt = "";
      media.append(img);
      if (face.left !== undefined) {{
        const box = document.createElement("div");
        box.className = "face-box";
        box.style.left = `${{face.left.toFixed(4)}}%`;
        box.style.top = `${{face.top.toFixed(4)}}%`;
        box.style.width = `${{face.boxWidth.toFixed(4)}}%`;
        box.style.height = `${{face.boxHeight.toFixed(4)}}%`;
        media.append(box);
      }}
      return media;
    }}
    function renderCommand(commandText) {{
      const row = document.createElement("div");
      row.className = "command-row";
      const command = document.createElement("div");
      command.className = "face-command";
      command.textContent = commandText;
      const button = document.createElement("button");
      button.className = "copy-command";
      button.type = "button";
      button.textContent = "Kopier";
      button.addEventListener("click", () => copyCommand(commandText, button));
      row.append(command, button);
      return row;
    }}
    async function copyCommand(text, button) {{
      if (!text) return;
      const originalText = button.textContent;
      try {{
        if (navigator.clipboard && window.isSecureContext) {{
          await navigator.clipboard.writeText(text);
        }} else {{
          fallbackCopyCommand(text);
        }}
        button.textContent = "Kopiert";
      }} catch (_error) {{
        button.textContent = "Kunne ikke kopiere";
      }}
      window.setTimeout(() => {{
        button.textContent = originalText;
      }}, 1600);
    }}
    function fallbackCopyCommand(text) {{
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      document.body.append(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
    }}
    function closeLightbox() {{
      lightboxEl.hidden = true;
      lightboxStageEl.replaceChildren();
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
