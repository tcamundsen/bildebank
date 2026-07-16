from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import db
from .config import AppConfig
from .media import media_kind
from .server_browser_queries import source_item_ids
from .server_browser_sources import all_browser_source
from .server_filter import text_filter_browser_source

if TYPE_CHECKING:
    from .server_handler import BildebankRequestHandler


DEFAULT_SLIDESHOW_DELAY_SECONDS = 10


@dataclass(frozen=True)
class SlideshowItem:
    file_id: int
    view_rotation_degrees: int


@dataclass(frozen=True)
class Slideshow:
    items: tuple[SlideshowItem, ...]
    item_ids: frozenset[int]
    delay_seconds: int


def build_slideshow(
    target: Path,
    config: AppConfig,
    *,
    filter_query: str | None,
    delay_seconds: int,
) -> Slideshow:
    if delay_seconds < 1:
        raise ValueError("--delay må være minst 1 sekund.")
    source = (
        text_filter_browser_source(filter_query, target)
        if filter_query is not None
        else all_browser_source()
    )
    ordered_ids = source_item_ids(
        target,
        source,
        config.face_recognition,
        hide_out_of_focus=config.browser.hide_out_of_focus,
    )
    rows_by_id = slideshow_rows_by_id(target, ordered_ids)
    target_root = target.resolve()
    items: list[SlideshowItem] = []
    for file_id in ordered_ids:
        row = rows_by_id.get(file_id)
        if row is None or media_kind(Path(str(row["target_path"]))) != "image":
            continue
        path = db.absolute_target_path(target, str(row["target_path"])).resolve()
        try:
            path.relative_to(target_root)
        except ValueError as exc:
            raise ValueError(
                f"Ugyldig filsti i databasen for slideshow-bilde #{file_id}."
            ) from exc
        if not path.is_file():
            continue
        items.append(
            SlideshowItem(
                file_id=file_id,
                view_rotation_degrees=db.normalize_view_rotation(
                    row["view_rotation_degrees"]
                ),
            )
        )
    if not items:
        if filter_query is None:
            raise ValueError("Fant ingen aktive stillbilder som kan vises i slideshowet.")
        raise ValueError(
            f"Filtersøket {filter_query!r} ga ingen aktive stillbilder som kan vises i slideshowet."
        )
    return Slideshow(
        items=tuple(items),
        item_ids=frozenset(item.file_id for item in items),
        delay_seconds=delay_seconds,
    )


def slideshow_rows_by_id(target: Path, file_ids: list[int]) -> dict[int, Any]:
    if not file_ids:
        return {}
    rows: dict[int, Any] = {}
    conn = db.connect(target)
    try:
        for index in range(0, len(file_ids), 900):
            chunk = file_ids[index : index + 900]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"""
                SELECT id, target_path, view_rotation_degrees
                FROM files
                WHERE deleted_at IS NULL
                  AND id IN ({placeholders})
                """,
                chunk,
            ):
                rows[int(row["id"])] = row
    finally:
        conn.close()
    return rows


def slideshow_html(slideshow: Slideshow) -> str:
    slides_json = json.dumps(
        [
            {
                "url": f"/slideshow/media/{item.file_id}",
                "rotation": item.view_rotation_degrees,
            }
            for item in slideshow.items
        ],
        separators=(",", ":"),
    )
    delay_ms = slideshow.delay_seconds * 1000
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bildebank slideshow</title>
  <style>
    html, body {{ width: 100%; height: 100%; margin: 0; overflow: hidden; background: #000; cursor: none; }}
    #slideshow {{ position: fixed; left: 50%; top: 50%; object-fit: contain; transform-origin: center; }}
    #slideshow[data-quarter-turn="false"] {{ width: 100vw; height: 100vh; }}
    #slideshow[data-quarter-turn="true"] {{ width: 100vh; height: 100vw; }}
  </style>
</head>
<body>
  <img id="slideshow" alt="" data-quarter-turn="false">
  <script>
    const slides = {slides_json};
    const delayMs = {delay_ms};
    const image = document.getElementById("slideshow");
    let preloader = null;

    function show(index, failedCount = 0) {{
      const slide = slides[index];
      const loader = new Image();
      loader.onload = () => {{
        const quarterTurn = slide.rotation === 90 || slide.rotation === 270;
        image.dataset.quarterTurn = quarterTurn ? "true" : "false";
        image.style.transform = `translate(-50%, -50%) rotate(${{slide.rotation}}deg)`;
        image.src = slide.url;
        const nextIndex = (index + 1) % slides.length;
        preloader = new Image();
        preloader.src = slides[nextIndex].url;
        window.setTimeout(() => show(nextIndex), delayMs);
      }};
      loader.onerror = () => {{
        const nextIndex = (index + 1) % slides.length;
        if (failedCount + 1 >= slides.length) {{
          window.setTimeout(() => show(0), delayMs);
          return;
        }}
        show(nextIndex, failedCount + 1);
      }};
      loader.src = slide.url;
    }}

    show(0);
  </script>
</body>
</html>
"""


def respond_slideshow_get(
    handler: BildebankRequestHandler,
    path: str,
) -> None:
    slideshow = handler.server.slideshow
    if path == "/":
        handler.respond_html(slideshow_html(slideshow))
        return
    prefix = "/slideshow/media/"
    if not path.startswith(prefix):
        handler.respond_text("Siden finnes ikke.", status=HTTPStatus.NOT_FOUND)
        return
    raw_file_id = path.removeprefix(prefix)
    if not raw_file_id.isdigit():
        handler.respond_text("Filen finnes ikke.", status=HTTPStatus.NOT_FOUND)
        return
    file_id = int(raw_file_id)
    if file_id not in slideshow.item_ids:
        handler.respond_text("Filen finnes ikke.", status=HTTPStatus.NOT_FOUND)
        return
    if not handler.respond_preview_image(file_id, require_active=True):
        print(
            f"Slideshow: kunne ikke vise bilde #{file_id}.",
            file=sys.stderr,
        )
