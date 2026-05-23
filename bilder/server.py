from __future__ import annotations

import html
import importlib.util
import json
import math
import mimetypes
import re
import shutil
import sqlite3
import threading
import urllib.parse
from dataclasses import dataclass, replace
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import __version__, db
from .config import AppConfig, FaceRecognitionConfig, set_face_recognition_enabled, set_face_recognition_model_name
from .face import (
    add_face_to_person,
    create_person,
    delete_person,
    face_db_path,
    normalize_person_name,
    remove_face_from_person,
    rename_person,
)
from .html_export import (
    browser_face_items_from_metadata,
    display_relative_path,
    face_tables_exist,
    format_bytes,
    month_key_from_path,
)
from .geo import (
    H3_COLUMNS,
    PREDEFINED_GEO_PLACES,
    PredefinedGeoPlace,
    h3_area_label,
    h3_column_for_resolution,
    h3_resolution,
    h3_resolution_label,
    predefined_geo_place,
)
from .media import ImageDimensions, camera_info, image_dimensions, image_orientation
from .media_cache import cached_image_dimensions
from .openclip import (
    ImageSearchResult,
    connect_openclip_db,
    cosine_similarity,
    create_search_run,
    embedding_from_blob,
    load_text_model,
    relative_to_target,
    text_embedding,
)
from .target_lock import TargetLock
from .thumbnails import existing_thumbnail_url


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_SEARCH_LIMIT = 100
DEFAULT_GEO_RESOLUTION = 7
DEFAULT_GEO_MIN_COUNT = 2
DEFAULT_GEO_LIMIT = 100


def current_face_db_path(target: Path, face_config: FaceRecognitionConfig | None = None) -> Path:
    if face_config is None:
        face_config = FaceRecognitionConfig()
    return face_db_path(target, face_config)


@dataclass(frozen=True)
class ServerSearchStats:
    query: str
    results: tuple[ImageSearchResult, ...]


@dataclass(frozen=True)
class BrowserSource:
    title: str
    root_url: str
    person_name: str | None = None
    include_suggestions: bool = True
    date_source: str | None = None
    source_id: int | None = None
    show_faces: bool = True
    geo_place_slug: str | None = None
    geo_place_cells: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeoMapCell:
    h3_cell: str
    count: int
    name: str | None
    x: float
    y: float


class OpenClipSearchCache:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._model: Any | None = None
        self._tokenizer: Any | None = None

    def text_vector(self, query: str) -> list[float]:
        with self._lock:
            if self._model is None or self._tokenizer is None:
                self._model, self._tokenizer = load_text_model(self.config.openclip)
            return text_embedding(self._model, self._tokenizer, query)

    @property
    def loaded(self) -> bool:
        return self._model is not None and self._tokenizer is not None


class BildebankServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], target: Path, config: AppConfig) -> None:
        super().__init__(address, BildebankRequestHandler)
        self.target = target
        self.config = config
        self.search_cache = OpenClipSearchCache(config)

    @property
    def face_enabled(self) -> bool:
        return self.config.face_recognition.enabled

    @property
    def openclip_enabled(self) -> bool:
        return self.config.openclip.enabled


class BildebankRequestHandler(BaseHTTPRequestHandler):
    server: BildebankServer

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/":
                self.respond_browser_root()
                return
            if parsed.path == "/people":
                if not self.server.face_enabled:
                    self.respond_text("Ansiktsgjenkjenning er av.", status=HTTPStatus.NOT_FOUND)
                    return
                self.respond_html(people_page_html(self.server.target, self.server.config.face_recognition))
                return
            if parsed.path in {"/sources", "/sources/"}:
                self.respond_html(sources_page_html(self.server.target))
                return
            if parsed.path == "/settings":
                self.respond_html(app_status_page_html(self.server.target, self.server.config))
                return
            if parsed.path in {"/settings/removed", "/settings/removed/"}:
                self.respond_html(removed_files_page_html(self.server.target))
                return
            if parsed.path == "/static/server.css":
                self.respond_static_asset(SERVER_CSS, "text/css; charset=utf-8")
                return
            if parsed.path == "/static/server.js":
                self.respond_static_asset(SERVER_JS, "application/javascript; charset=utf-8")
                return
            if parsed.path.startswith("/help/"):
                self.respond_help(parsed.path.removeprefix("/help/"))
                return
            if parsed.path in {"/geo", "/geo/"}:
                self.respond_geo(parsed.query)
                return
            if parsed.path == "/geo/map":
                self.respond_geo_map(parsed.query)
                return
            if parsed.path == "/geo/stats":
                self.respond_html(geo_stats_page_html(self.server.target))
                return
            if parsed.path == "/geo/missing":
                self.respond_geo_missing(parsed.query)
                return
            if parsed.path == "/geo/custom-places":
                self.respond_html(custom_geo_places_page_html(self.server.target))
                return
            if parsed.path.startswith("/geo/place/"):
                self.respond_geo_place(parsed.path.removeprefix("/geo/place/"))
                return
            if parsed.path.startswith("/geo/area/"):
                self.respond_geo_area(parsed.path.removeprefix("/geo/area/"), parsed.query)
                return
            if parsed.path.startswith("/item/"):
                self.respond_item(parsed.path.removeprefix("/item/"))
                return
            if parsed.path.startswith("/month/"):
                self.respond_month(parsed.path.removeprefix("/month/"))
                return
            if parsed.path.startswith("/date-source/"):
                self.respond_date_source(parsed.path.removeprefix("/date-source/"))
                return
            if parsed.path.startswith("/source/"):
                self.respond_imported_source(parsed.path.removeprefix("/source/"))
                return
            if parsed.path.startswith("/person/"):
                if not self.server.face_enabled:
                    self.respond_text("Ansiktsgjenkjenning er av.", status=HTTPStatus.NOT_FOUND)
                    return
                self.respond_person(parsed.path.removeprefix("/person/"))
                return
            if parsed.path == "/search":
                if not self.server.openclip_enabled:
                    self.respond_text("Tekstbasert bildesøk er av.", status=HTTPStatus.NOT_FOUND)
                    return
                params = urllib.parse.parse_qs(parsed.query)
                query = first_param(params, "q").strip()
                limit = positive_int_param(params, "limit", DEFAULT_SEARCH_LIMIT)
                if not query:
                    self.respond_html(index_html(self.server, message="Skriv inn et søk."))
                    return
                stats = search_server_images(self.server, query=query, limit=limit)
                self.respond_html(search_html(self.server, stats, limit))
                return
            if parsed.path == "/api/item-info":
                self.respond_item_info(parsed.query)
                return
            if parsed.path == "/api/item-faces":
                self.respond_item_faces(parsed.query)
                return
            if parsed.path.startswith("/file/"):
                self.respond_file(parsed.path.removeprefix("/file/"))
                return
            self.respond_file(parsed.path.lstrip("/"))
        except Exception as exc:  # noqa: BLE001 - local server should show readable errors
            self.respond_html(error_html(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/geo/place-name":
                self.respond_set_geo_place_name()
                return
            if parsed.path == "/geo/custom-place":
                self.respond_set_custom_geo_place()
                return
            if parsed.path == "/geo/custom-place-delete":
                self.respond_delete_custom_geo_place()
                return
            if parsed.path == "/settings/face-config":
                self.respond_set_face_config()
                return
            if parsed.path == "/settings/face-model":
                self.respond_set_face_model()
                return
            if parsed.path == "/api/face-person-add-face":
                if not self.server.face_enabled:
                    self.respond_json({"ok": False, "error": "Ansiktsgjenkjenning er av."}, status=HTTPStatus.FORBIDDEN)
                    return
                self.respond_add_face_to_person()
                return
            if parsed.path == "/api/face-person-remove-face":
                if not self.server.face_enabled:
                    self.respond_json({"ok": False, "error": "Ansiktsgjenkjenning er av."}, status=HTTPStatus.FORBIDDEN)
                    return
                self.respond_remove_face_from_person()
                return
            if parsed.path == "/api/face-person-create-and-add-face":
                if not self.server.face_enabled:
                    self.respond_json({"ok": False, "error": "Ansiktsgjenkjenning er av."}, status=HTTPStatus.FORBIDDEN)
                    return
                self.respond_create_person_and_add_face()
                return
            if parsed.path == "/api/face-person-rename":
                if not self.server.face_enabled:
                    self.respond_json({"ok": False, "error": "Ansiktsgjenkjenning er av."}, status=HTTPStatus.FORBIDDEN)
                    return
                self.respond_rename_person()
                return
            if parsed.path == "/api/face-person-delete":
                if not self.server.face_enabled:
                    self.respond_json({"ok": False, "error": "Ansiktsgjenkjenning er av."}, status=HTTPStatus.FORBIDDEN)
                    return
                self.respond_delete_person()
                return
            if parsed.path == "/api/item-rotate":
                self.respond_rotate_item()
                return
            if parsed.path == "/api/item-delete":
                self.respond_delete_item()
                return
            if parsed.path == "/api/item-undelete":
                self.respond_undelete_item()
                return
            self.respond_json({"ok": False, "error": "Ukjent endepunkt."}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:  # noqa: BLE001 - local server should show readable errors
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.respond_bytes(content.encode("utf-8"), "text/html; charset=utf-8", status=status)

    def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.respond_bytes(content.encode("utf-8"), "text/plain; charset=utf-8", status=status)

    def respond_json(self, content: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.respond_bytes(json.dumps(content).encode("utf-8"), "application/json; charset=utf-8", status=status)

    def respond_static_asset(self, content: str, content_type: str) -> None:
        encoded = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def respond_bytes(self, content: bytes, content_type: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def respond_browser_root(self) -> None:
        source = all_browser_source()
        item = first_source_item(self.server.target, source)
        if item is None:
            self.respond_html(empty_browser_html(openclip_enabled=self.server.openclip_enabled))
            return
        self.redirect(source_item_url(source, int(item["id"])))

    def respond_item(self, raw_file_id: str) -> None:
        file_id = parse_file_id(raw_file_id)
        source = all_browser_source()
        item = source_item_by_id(self.server.target, source, file_id)
        if item is None:
            self.respond_text("Filen finnes ikke i bildesamlingen.", status=HTTPStatus.NOT_FOUND)
            return
        previous_item, next_item = adjacent_source_items(self.server.target, source, item)
        month_nav = source_month_navigation(self.server.target, source, item)
        self.respond_html(
            source_item_page_html(
                self.server.target,
                source,
                item,
                previous_item,
                next_item,
                month_nav,
                face_enabled=self.server.face_enabled,
                openclip_enabled=self.server.openclip_enabled,
                face_config=self.server.config.face_recognition,
            )
        )

    def respond_month(self, raw_month: str) -> None:
        month_key = urllib.parse.unquote(raw_month).strip()
        if not valid_month_key(month_key):
            self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
            return
        source = all_browser_source()
        items = source_month_items(self.server.target, source, month_key)
        self.respond_html(
            source_month_page_html(
                self.server.target,
                source,
                month_key,
                items,
                face_enabled=self.server.face_enabled,
                openclip_enabled=self.server.openclip_enabled,
                face_config=self.server.config.face_recognition,
            )
        )

    def respond_person(self, raw_path: str) -> None:
        raw_name, person_mode, show_faces, page_mode, raw_value = parse_person_path(raw_path)
        person_name = urllib.parse.unquote(raw_name).strip()
        if not person_name:
            self.respond_text("Personnavn mangler.", status=HTTPStatus.BAD_REQUEST)
            return
        person = person_by_name(self.server.target, person_name, self.server.config.face_recognition)
        if person is None:
            self.respond_html(person_not_found_html(person_name), status=HTTPStatus.NOT_FOUND)
            return
        canonical_name = str(person["name"])
        source = person_browser_source(canonical_name, include_suggestions=person_mode != "confirmed", show_faces=show_faces)
        if page_mode is None:
            item = first_source_item(self.server.target, source, self.server.config.face_recognition)
            if item is None:
                self.respond_html(empty_person_browser_html(source, openclip_enabled=self.server.openclip_enabled))
                return
            self.redirect(source_item_url(source, int(item["id"])))
            return
        if page_mode == "item":
            file_id = parse_file_id(raw_value)
            item = source_item_by_id(self.server.target, source, file_id, self.server.config.face_recognition)
            if item is None:
                self.respond_text("Filen finnes ikke for denne personen.", status=HTTPStatus.NOT_FOUND)
                return
            previous_item, next_item = adjacent_source_items(self.server.target, source, item, self.server.config.face_recognition)
            month_nav = source_month_navigation(self.server.target, source, item, self.server.config.face_recognition)
            self.respond_html(
                source_item_page_html(
                    self.server.target,
                    source,
                    item,
                    previous_item,
                    next_item,
                    month_nav,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                    face_config=self.server.config.face_recognition,
                )
            )
            return
        if page_mode == "month":
            month_key = urllib.parse.unquote(raw_value).strip()
            if not valid_month_key(month_key):
                self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
                return
            items = source_month_items(self.server.target, source, month_key, self.server.config.face_recognition)
            self.respond_html(
                source_month_page_html(
                    self.server.target,
                    source,
                    month_key,
                    items,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                    face_config=self.server.config.face_recognition,
                )
            )
            return
        self.respond_text("Ugyldig personside.", status=HTTPStatus.NOT_FOUND)

    def respond_date_source(self, raw_path: str) -> None:
        raw_date_source, page_mode, raw_value = parse_source_path(raw_path)
        date_source = urllib.parse.unquote(raw_date_source).strip()
        if not valid_browser_date_source(date_source):
            self.respond_text("Ugyldig datokilde.", status=HTTPStatus.BAD_REQUEST)
            return
        source = date_source_browser_source(date_source)
        if page_mode is None:
            item = first_source_item(self.server.target, source)
            if item is None:
                self.respond_html(
                    empty_source_html(
                        source,
                        face_enabled=self.server.face_enabled,
                        openclip_enabled=self.server.openclip_enabled,
                    )
                )
                return
            self.redirect(source_item_url(source, int(item["id"])))
            return
        if page_mode == "item":
            file_id = parse_file_id(raw_value)
            item = source_item_by_id(self.server.target, source, file_id)
            if item is None:
                self.respond_text("Filen finnes ikke for denne datokilden.", status=HTTPStatus.NOT_FOUND)
                return
            previous_item, next_item = adjacent_source_items(self.server.target, source, item)
            month_nav = source_month_navigation(self.server.target, source, item)
            self.respond_html(
                source_item_page_html(
                    self.server.target,
                    source,
                    item,
                    previous_item,
                    next_item,
                    month_nav,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                    face_config=self.server.config.face_recognition,
                )
            )
            return
        if page_mode == "month":
            month_key = urllib.parse.unquote(raw_value).strip()
            if not valid_month_key(month_key):
                self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
                return
            items = source_month_items(self.server.target, source, month_key)
            self.respond_html(
                source_month_page_html(
                    self.server.target,
                    source,
                    month_key,
                    items,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                    face_config=self.server.config.face_recognition,
                )
            )
            return
        self.respond_text("Ugyldig datokildeside.", status=HTTPStatus.NOT_FOUND)

    def respond_imported_source(self, raw_path: str) -> None:
        raw_source_id, page_mode, raw_value = parse_source_path(raw_path)
        try:
            source_id = int(urllib.parse.unquote(raw_source_id).strip())
        except ValueError:
            self.respond_text("Ugyldig kilde.", status=HTTPStatus.BAD_REQUEST)
            return
        source_row = imported_source_by_id(self.server.target, source_id)
        if source_row is None:
            self.respond_text("Fant ikke kilde.", status=HTTPStatus.NOT_FOUND)
            return
        source = imported_source_browser_source(source_row)
        if page_mode is None:
            item = first_source_item(self.server.target, source)
            if item is None:
                self.respond_html(
                    empty_source_html(
                        source,
                        face_enabled=self.server.face_enabled,
                        openclip_enabled=self.server.openclip_enabled,
                    )
                )
                return
            self.redirect(source_item_url(source, int(item["id"])))
            return
        if page_mode == "item":
            file_id = parse_file_id(raw_value)
            item = source_item_by_id(self.server.target, source, file_id)
            if item is None:
                self.respond_text("Filen finnes ikke for denne kilden.", status=HTTPStatus.NOT_FOUND)
                return
            previous_item, next_item = adjacent_source_items(self.server.target, source, item)
            month_nav = source_month_navigation(self.server.target, source, item)
            self.respond_html(
                source_item_page_html(
                    self.server.target,
                    source,
                    item,
                    previous_item,
                    next_item,
                    month_nav,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                    face_config=self.server.config.face_recognition,
                )
            )
            return
        if page_mode == "month":
            month_key = urllib.parse.unquote(raw_value).strip()
            if not valid_month_key(month_key):
                self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
                return
            items = source_month_items(self.server.target, source, month_key)
            self.respond_html(
                source_month_page_html(
                    self.server.target,
                    source,
                    month_key,
                    items,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                    face_config=self.server.config.face_recognition,
                )
            )
            return
        self.respond_text("Ugyldig kildeside.", status=HTTPStatus.NOT_FOUND)

    def respond_geo(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        resolution = nonnegative_int_param(params, "resolution", DEFAULT_GEO_RESOLUTION)
        min_count = positive_int_param(params, "min_count", DEFAULT_GEO_MIN_COUNT)
        limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
        if resolution not in H3_COLUMNS:
            self.respond_text("H3-oppløsning må være mellom 0 og 9.", status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_html(geo_index_page_html(self.server.target, resolution=resolution, min_count=min_count, limit=limit))

    def respond_geo_map(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        resolution = nonnegative_int_param(params, "resolution", DEFAULT_GEO_RESOLUTION)
        min_count = positive_int_param(params, "min_count", DEFAULT_GEO_MIN_COUNT)
        limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
        if resolution not in H3_COLUMNS:
            self.respond_text("H3-oppløsning må være mellom 0 og 9.", status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_html(geo_map_page_html(self.server.target, resolution=resolution, min_count=min_count, limit=limit))

    def respond_geo_area(self, raw_cell: str, query: str) -> None:
        h3_cell = urllib.parse.unquote(raw_cell).strip()
        params = urllib.parse.parse_qs(query)
        limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
        try:
            resolution = h3_resolution(h3_cell)
        except ValueError as exc:
            self.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_html(geo_area_page_html(self.server.target, h3_cell, resolution=resolution, limit=limit))

    def respond_geo_place(self, raw_path: str) -> None:
        raw_slug, page_mode, raw_value = parse_source_path(raw_path)
        slug = urllib.parse.unquote(raw_slug).strip()
        place = geo_place_by_slug(self.server.target, slug)
        if place is None:
            self.respond_text("Ukjent sted.", status=HTTPStatus.NOT_FOUND)
            return
        source = geo_place_browser_source(place)
        if page_mode is None:
            item = first_source_item(self.server.target, source)
            if item is None:
                self.respond_html(
                    empty_source_html(
                        source,
                        face_enabled=self.server.face_enabled,
                        openclip_enabled=self.server.openclip_enabled,
                    )
                )
                return
            self.redirect(source_item_url(source, int(item["id"])))
            return
        if page_mode == "item":
            file_id = parse_file_id(raw_value)
            item = source_item_by_id(self.server.target, source, file_id)
            if item is None:
                self.respond_text("Filen finnes ikke for dette stedet.", status=HTTPStatus.NOT_FOUND)
                return
            previous_item, next_item = adjacent_source_items(self.server.target, source, item)
            month_nav = source_month_navigation(self.server.target, source, item)
            self.respond_html(
                source_item_page_html(
                    self.server.target,
                    source,
                    item,
                    previous_item,
                    next_item,
                    month_nav,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                    face_config=self.server.config.face_recognition,
                )
            )
            return
        if page_mode == "month":
            month_key = urllib.parse.unquote(raw_value).strip()
            if not valid_month_key(month_key):
                self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
                return
            items = source_month_items(self.server.target, source, month_key)
            self.respond_html(
                source_month_page_html(
                    self.server.target,
                    source,
                    month_key,
                    items,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                    face_config=self.server.config.face_recognition,
                )
            )
            return
        self.respond_text("Ugyldig stedsside.", status=HTTPStatus.NOT_FOUND)

    def respond_geo_missing(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
        offset = nonnegative_int_param(params, "offset", 0)
        self.respond_html(geo_missing_page_html(self.server.target, limit=limit, offset=offset))

    def respond_set_geo_place_name(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        params = urllib.parse.parse_qs(raw)
        h3_cell = first_param(params, "h3_cell").strip()
        name = first_param(params, "name")
        limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
        try:
            h3_resolution(h3_cell)
        except ValueError as exc:
            self.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
            return
        conn = db.connect(self.server.target)
        try:
            db.set_geo_place_name(conn, h3_cell, name)
            conn.commit()
        finally:
            conn.close()
        url = "/geo/area/" + urllib.parse.quote(h3_cell, safe="") + f"?limit={limit}"
        self.redirect(url)

    def respond_set_custom_geo_place(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        params = urllib.parse.parse_qs(raw)
        try:
            slug = normalize_geo_place_slug(first_param(params, "slug"))
            if predefined_geo_place(slug) is not None:
                raise ValueError("Slug er reservert for et innebygd sted.")
            name = first_param(params, "name")
            h3_cells = parse_geo_place_cells(first_param(params, "h3_cells"))
            conn = db.connect(self.server.target)
            try:
                db.set_custom_geo_place(conn, slug=slug, name=name, h3_cells=h3_cells)
                conn.commit()
            finally:
                conn.close()
        except ValueError as exc:
            self.respond_html(error_html(exc), status=HTTPStatus.BAD_REQUEST)
            return
        self.redirect("/geo/place/" + urllib.parse.quote(slug, safe=""))

    def respond_delete_custom_geo_place(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        params = urllib.parse.parse_qs(raw)
        try:
            slug = normalize_geo_place_slug(first_param(params, "slug"))
            if predefined_geo_place(slug) is not None:
                raise ValueError("Innebygde steder kan ikke slettes.")
            conn = db.connect(self.server.target)
            try:
                db.delete_custom_geo_place(conn, slug)
                conn.commit()
            finally:
                conn.close()
        except ValueError as exc:
            self.respond_html(error_html(exc), status=HTTPStatus.BAD_REQUEST)
            return
        self.redirect("/geo")

    def respond_set_face_config(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        params = urllib.parse.parse_qs(raw)
        enabled = "true" in {value.strip().lower() for value in params.get("enabled", [])}
        set_face_recognition_enabled(server_program_repo_root(), enabled)
        self.server.config = replace(
            self.server.config,
            face_recognition=replace(self.server.config.face_recognition, enabled=enabled),
        )
        self.redirect("/settings")

    def respond_set_face_model(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        params = urllib.parse.parse_qs(raw)
        model_name = (params.get("model_name") or [""])[0].strip()
        config = self.server.config.face_recognition
        installed_models = installed_insightface_models(config)
        if model_name not in installed_models:
            raise ValueError(f"InsightFace-modellen er ikke installert: {model_name}")
        set_face_recognition_model_name(server_program_repo_root(), model_name)
        self.server.config = replace(
            self.server.config,
            face_recognition=replace(self.server.config.face_recognition, model_name=model_name),
        )
        self.redirect("/settings")

    def respond_file(self, encoded_relative_path: str) -> None:
        raw_path = urllib.parse.unquote(encoded_relative_path).strip("/")
        if raw_path.isdigit():
            row = browser_item_by_id(self.server.target, int(raw_path))
            if row is None:
                self.respond_text("Filen finnes ikke.", status=HTTPStatus.NOT_FOUND)
                return
            path = db.absolute_target_path(self.server.target, Path(str(row["target_path"])))
        else:
            relative = Path(raw_path)
            path = (self.server.target / relative).resolve()
            try:
                path.relative_to(self.server.target.resolve())
            except ValueError:
                self.respond_text("Ugyldig filsti.", status=HTTPStatus.FORBIDDEN)
                return
        if not path.is_file():
            self.respond_text("Filen finnes ikke.", status=HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            content = path.read_bytes()
        except OSError as exc:
            self.respond_text(str(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.respond_bytes(content, content_type)

    def respond_help(self, raw_help_path: str) -> None:
        doc_path = resolve_doc_path(raw_help_path)
        if doc_path is None:
            self.respond_text("Ugyldig hjelpeside.", status=HTTPStatus.FORBIDDEN)
            return
        if not doc_path.is_file():
            self.respond_text("Hjelpesiden finnes ikke.", status=HTTPStatus.NOT_FOUND)
            return
        try:
            markdown = doc_path.read_text(encoding="utf-8")
        except OSError as exc:
            self.respond_text(str(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.respond_html(markdown_doc_page_html(doc_path, markdown))

    def respond_item_info(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        try:
            file_id = parse_file_id(first_param(params, "file_id"))
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        item = browser_item_by_id(self.server.target, file_id)
        if item is None:
            self.respond_json({"ok": False, "error": "Filen finnes ikke."}, status=HTTPStatus.NOT_FOUND)
            return
        self.respond_json({"ok": True, "html": image_info_content_html(self.server.target, item)})

    def respond_item_faces(self, query: str) -> None:
        if not self.server.face_enabled:
            self.respond_json({"ok": False, "error": "Ansiktsgjenkjenning er av."}, status=HTTPStatus.FORBIDDEN)
            return
        params = urllib.parse.parse_qs(query)
        try:
            file_id = parse_file_id(first_param(params, "file_id"))
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        item = browser_item_by_id(self.server.target, file_id)
        if item is None:
            self.respond_json({"ok": False, "error": "Filen finnes ikke."}, status=HTTPStatus.NOT_FOUND)
            return
        self.respond_json({"ok": True, "html": face_overlay_content_html(self.server.target, item, self.server.config.face_recognition)})

    def respond_add_face_to_person(self) -> None:
        payload = BildebankRequestHandler.read_face_person_payload(self)
        if isinstance(payload[0], dict):
            self.respond_json(payload[0], status=payload[1])
            return
        person_name, face_id = payload
        try:
            config = self.server.config.face_recognition
            result = add_face_to_person(self.server.target, person_name, face_id, config)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        clear_face_caches()
        person_link_url = person_item_url_for_face(self.server.target, result.person_name, result.face_id, config)
        self.respond_json(
            {
                "ok": True,
                "person_name": result.person_name,
                "person_url": person_link_url,
                "confirmed": True,
                "face_id": result.face_id,
                "added": result.added,
            }
        )

    def respond_remove_face_from_person(self) -> None:
        payload = BildebankRequestHandler.read_face_person_payload(self)
        if isinstance(payload[0], dict):
            self.respond_json(payload[0], status=payload[1])
            return
        person_name, face_id = payload
        try:
            config = self.server.config.face_recognition
            result = remove_face_from_person(self.server.target, person_name, face_id, config)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        clear_face_caches()
        self.respond_json(
            {
                "ok": True,
                "person_name": result.person_name,
                "person_url": person_url(result.person_name),
                "face_id": result.face_id,
                "removed": result.removed,
            }
        )

    def respond_create_person_and_add_face(self) -> None:
        payload = BildebankRequestHandler.read_face_person_payload(self)
        if isinstance(payload[0], dict):
            self.respond_json(payload[0], status=payload[1])
            return
        person_name, face_id = payload
        try:
            config = self.server.config.face_recognition
            create_person(self.server.target, person_name, config)
            result = add_face_to_person(self.server.target, person_name, face_id, config)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        clear_face_caches()
        person_link_url = person_item_url_for_face(self.server.target, result.person_name, result.face_id, config)
        self.respond_json(
            {
                "ok": True,
                "person_name": result.person_name,
                "person_url": person_link_url,
                "confirmed": True,
                "face_id": result.face_id,
                "added": result.added,
            }
        )

    def respond_rename_person(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        old_name = str(payload.get("old_name") or "").strip()
        new_name = str(payload.get("new_name") or "").strip()
        if not old_name:
            self.respond_json({"ok": False, "error": "Gammelt personnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
            return
        if not new_name:
            self.respond_json({"ok": False, "error": "Nytt personnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            config = self.server.config.face_recognition
            result = rename_person(self.server.target, old_name, new_name, config)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        clear_face_caches()
        self.respond_json(
            {
                "ok": True,
                "old_name": result.old_name,
                "new_name": result.new_name,
                "person_url": f"{person_url(result.new_name)}/no-faces",
            }
        )

    def respond_delete_person(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        person_name = str(payload.get("person_name") or "").strip()
        if not person_name:
            self.respond_json({"ok": False, "error": "Personnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            config = self.server.config.face_recognition
            result = delete_person(self.server.target, person_name, config)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        clear_face_caches()
        self.respond_json(
            {
                "ok": True,
                "person_name": result.person_name,
                "removed_faces": result.removed_faces,
                "removed_suggestions": result.removed_suggestions,
            }
        )

    def respond_rotate_item(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        try:
            file_id = int(payload.get("file_id"))
        except (TypeError, ValueError):
            self.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        direction = str(payload.get("direction") or "")
        conn = db.connect(self.server.target)
        try:
            try:
                rotation = db.rotate_file_view(conn, file_id, direction)
            except ValueError as exc:
                self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            conn.commit()
        finally:
            conn.close()
        self.respond_json({"ok": True, "file_id": file_id, "rotation": rotation})

    def respond_delete_item(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        try:
            file_id = int(payload.get("file_id"))
        except (TypeError, ValueError):
            self.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            deleted_path = remove_file_from_browser(self.server.target, file_id)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_json({"ok": True, "file_id": file_id, "deleted_path": deleted_path.as_posix()})

    def respond_undelete_item(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        try:
            file_id = int(payload.get("file_id"))
        except (TypeError, ValueError):
            self.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            restored_path = undelete_file_from_browser(self.server.target, file_id)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_json({"ok": True, "file_id": file_id, "restored_path": restored_path.as_posix()})

    def read_json_payload(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        if not raw:
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Ugyldig JSON.")
        return payload

    def read_face_person_payload(self) -> tuple[str, int] | tuple[dict[str, object], HTTPStatus]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            payload = json.loads(raw or "{}")
            person_name = str(payload.get("person_name") or "").strip()
            face_id_raw = payload.get("face_id")
        else:
            params = urllib.parse.parse_qs(raw)
            person_name = first_param(params, "person_name").strip()
            face_id_raw = first_param(params, "face_id")
        try:
            face_id = int(face_id_raw)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Ugyldig face_id."}, HTTPStatus.BAD_REQUEST
        if not person_name:
            return {"ok": False, "error": "Personnavn mangler."}, HTTPStatus.BAD_REQUEST
        return person_name, face_id


def first_param(params: dict[str, list[str]], name: str) -> str:
    values = params.get(name, [])
    return values[0] if values else ""


def positive_int_param(params: dict[str, list[str]], name: str, default: int) -> int:
    raw = first_param(params, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def nonnegative_int_param(params: dict[str, list[str]], name: str, default: int) -> int:
    raw = first_param(params, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def parse_file_id(value: str) -> int:
    try:
        file_id = int(value)
    except ValueError as exc:
        raise ValueError("Ugyldig file_id.") from exc
    if file_id < 1:
        raise ValueError("Ugyldig file_id.")
    return file_id


def normalize_geo_place_slug(value: str) -> str:
    slug = re.sub(r"\s+", "_", value.strip().lower())
    if not slug:
        raise ValueError("Slug mangler.")
    if any(char in slug for char in "/?#"):
        raise ValueError("Slug kan ikke inneholde /, ? eller #.")
    if len(slug) > 120:
        raise ValueError("Slug er for lang.")
    return slug


def parse_geo_place_cells(value: str) -> list[str]:
    cells = [cell.strip() for cell in re.split(r"[\s,]+", value) if cell.strip()]
    clean_cells: list[str] = []
    seen: set[str] = set()
    for cell in cells:
        h3_resolution_any(cell)
        if cell not in seen:
            clean_cells.append(cell)
            seen.add(cell)
    if not clean_cells:
        raise ValueError("Stedet må ha minst én H3-celle.")
    return clean_cells


def resolve_doc_path(raw_doc_path: str) -> Path | None:
    raw_path = urllib.parse.unquote(raw_doc_path).strip("/")
    if not raw_path:
        return None
    relative = Path(raw_path)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        return None
    if relative.suffix != ".md":
        relative = relative.with_suffix(".md")
    docs_root = (server_program_repo_root() / "docs").resolve()
    candidate = (docs_root / relative).resolve()
    try:
        candidate.relative_to(docs_root)
    except ValueError:
        return None
    return candidate


def parse_person_path(raw_path: str) -> tuple[str, str, bool, str | None, str]:
    person_part, page_mode, raw_value = parse_source_path(raw_path)
    person_mode = "all"
    show_faces = True
    if person_part.endswith("/no-faces"):
        person_part = person_part.removesuffix("/no-faces")
        show_faces = False
    if person_part.endswith("/confirmed"):
        person_part = person_part.removesuffix("/confirmed")
        person_mode = "confirmed"
    elif person_part.endswith("/all"):
        person_part = person_part.removesuffix("/all")
        person_mode = "all"
    return person_part.strip("/"), person_mode, show_faces, page_mode, raw_value


def parse_source_path(raw_path: str) -> tuple[str, str | None, str]:
    source_part = raw_path.strip("/")
    page_mode = None
    raw_value = ""
    if "/item/" in source_part:
        source_part, raw_value = source_part.split("/item/", 1)
        page_mode = "item"
    elif "/month/" in source_part:
        source_part, raw_value = source_part.split("/month/", 1)
        page_mode = "month"
    return source_part.strip("/"), page_mode, raw_value


FILE_COLUMNS = (
    "id, target_path, target_path_key, stored_filename, taken_date, date_source, "
    "size_bytes, view_rotation_degrees, gps_lat, gps_lon, "
    "media_width, media_height, media_orientation, media_metadata_mtime_ns, "
    f"{db.H3_FILE_COLUMNS_SQL}"
)
ITEM_DATE_ORDER_SQL = db.BROWSER_DATE_ORDER_SQL
ITEM_ORDER_SQL = f"{ITEM_DATE_ORDER_SQL}, target_path_key"
MONTH_PATH_RE = re.compile(r"(?:^|[\\/])(?P<year>\d{4})[\\/](?P<month>\d{2})(?:[\\/]|$)")


def all_browser_source() -> BrowserSource:
    return BrowserSource("Bildebrowser", "/")


def person_browser_source(person_name: str, *, include_suggestions: bool, show_faces: bool = True) -> BrowserSource:
    title = person_name if include_suggestions else f"{person_name} - bekreftet"
    root_url = person_url(person_name) if include_suggestions else f"{person_url(person_name)}/confirmed"
    if not show_faces:
        title = f"{title} - uten ansiktsmarkering"
        root_url = f"{root_url}/no-faces"
    return BrowserSource(title, root_url, person_name, include_suggestions, show_faces=show_faces)


def date_source_browser_source(date_source: str) -> BrowserSource:
    labels = {
        "filename": "Dato fra filnavn",
        "mtime": "Dato fra mtime",
    }
    return BrowserSource(labels[date_source], f"/date-source/{date_source}", date_source=date_source)


def imported_source_browser_source(source: db.Source | sqlite3.Row) -> BrowserSource:
    source_id = int(source["id"] if isinstance(source, sqlite3.Row) else source.id)
    name = str(source["name"] if isinstance(source, sqlite3.Row) else source.name)
    return BrowserSource(f"Kilde: {name}", f"/source/{source_id}", source_id=source_id)


def geo_place_browser_source(place: PredefinedGeoPlace) -> BrowserSource:
    return BrowserSource(
        place.name,
        "/geo/place/" + urllib.parse.quote(place.slug, safe=""),
        geo_place_slug=place.slug,
        geo_place_cells=place.h3_cells,
    )


def valid_browser_date_source(date_source: str) -> bool:
    return date_source in {"filename", "mtime"}


def is_filtered_source(source: BrowserSource) -> bool:
    return (
        source.person_name is not None
        or source.date_source is not None
        or source.source_id is not None
        or source.geo_place_slug is not None
    )


def source_has_sql_filter(source: BrowserSource) -> bool:
    return source.date_source is not None or source.geo_place_slug is not None


def source_sql_filter(source: BrowserSource) -> tuple[str, tuple[object, ...]]:
    if source.date_source is not None:
        if not valid_browser_date_source(source.date_source):
            raise ValueError("Ugyldig datokilde.")
        return "date_source = ?", (source.date_source,)
    if source.geo_place_slug is not None:
        place = PredefinedGeoPlace(source.geo_place_slug, source.title, source.geo_place_cells)
        return db.geo_place_where_clause(geo_place_cells_by_column(place))
    raise ValueError("Kilden har ikke SQL-filter.")


def source_item_url(source: BrowserSource, file_id: int) -> str:
    if is_filtered_source(source):
        return f"{source.root_url}/item/{file_id}"
    return f"/item/{file_id}"


def source_month_url(source: BrowserSource, month_key: str) -> str:
    quoted = urllib.parse.quote(month_key)
    if is_filtered_source(source):
        return f"{source.root_url}/month/{quoted}"
    return f"/month/{quoted}"


def first_browser_item(target: Path) -> Any | None:
    return first_source_item(target, all_browser_source())


def first_source_item(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
) -> Any | None:
    if source_has_sql_filter(source):
        return first_sql_filtered_source_item(target, source)
    if source.person_name is not None or source.source_id is not None:
        items = source_items(target, source, face_config)
        return items[0] if items else None
    if not is_filtered_source(source):
        return first_unfiltered_source_item(target)
    items = source_items(target, source, face_config)
    return items[0] if items else None


def first_sql_filtered_source_item(target: Path, source: BrowserSource) -> Any | None:
    where_sql, params = source_sql_filter(source)
    conn = db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({where_sql})
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            params,
        ).fetchone()
    finally:
        conn.close()


def first_unfiltered_source_item(target: Path) -> Any | None:
    conn = db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()


def browser_item_by_id(target: Path, file_id: int) -> Any | None:
    return source_item_by_id(target, all_browser_source(), file_id)


def source_item_by_id(
    target: Path,
    source: BrowserSource,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> Any | None:
    if source_has_sql_filter(source):
        where_sql, params = source_sql_filter(source)
        conn = db.connect(target)
        try:
            return conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                  AND id = ?
                  AND ({where_sql})
                """,
                (file_id, *params),
            ).fetchone()
        finally:
            conn.close()
    if source.person_name is not None or source.source_id is not None:
        return next((item for item in source_items(target, source, face_config) if int(item["id"]) == file_id), None)
    conn = db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL AND id = ?
            """,
            (file_id,),
        ).fetchone()
    finally:
        conn.close()


def adjacent_browser_items(target: Path, item: Any) -> tuple[Any | None, Any | None]:
    return adjacent_source_items(target, all_browser_source(), item)


def adjacent_source_items(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> tuple[Any | None, Any | None]:
    if source_has_sql_filter(source):
        return adjacent_sql_filtered_source_items(target, source, item)
    if source.person_name is not None or source.source_id is not None:
        return adjacent_items_from_list(source_items(target, source, face_config), item)
    order_key = item_order_key(item)
    conn = db.connect(target)
    try:
        previous_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({ITEM_DATE_ORDER_SQL}, target_path_key) < (?, ?)
            ORDER BY {ITEM_DATE_ORDER_SQL} DESC, target_path_key DESC
            LIMIT 1
            """,
            order_key,
        ).fetchone()
        next_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({ITEM_DATE_ORDER_SQL}, target_path_key) > (?, ?)
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            order_key,
        ).fetchone()
        return previous_item, next_item
    finally:
        conn.close()


def adjacent_sql_filtered_source_items(target: Path, source: BrowserSource, item: Any) -> tuple[Any | None, Any | None]:
    where_sql, params = source_sql_filter(source)
    order_key = item_order_key(item)
    conn = db.connect(target)
    try:
        previous_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({where_sql})
              AND ({ITEM_DATE_ORDER_SQL}, target_path_key) < (?, ?)
            ORDER BY {ITEM_DATE_ORDER_SQL} DESC, target_path_key DESC
            LIMIT 1
            """,
            (*params, *order_key),
        ).fetchone()
        next_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({where_sql})
              AND ({ITEM_DATE_ORDER_SQL}, target_path_key) > (?, ?)
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            (*params, *order_key),
        ).fetchone()
        return previous_item, next_item
    finally:
        conn.close()


def item_order_key(item: Any) -> tuple[str, str]:
    taken_date = str(item["taken_date"] or "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}", taken_date):
        taken_date = "9999-99-99"
    return taken_date, str(item["target_path_key"])


def adjacent_items_from_list(items: list[Any], item: Any) -> tuple[Any | None, Any | None]:
    index = next((idx for idx, candidate in enumerate(items) if int(candidate["id"]) == int(item["id"])), -1)
    if index < 0:
        return None, None
    previous_item = items[index - 1] if index > 0 else None
    next_item = items[index + 1] if index < len(items) - 1 else None
    return previous_item, next_item


def browser_month_keys(target: Path) -> list[str]:
    return source_month_keys(target, all_browser_source())


def source_month_keys(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
) -> list[str]:
    if source_has_sql_filter(source):
        return sql_filtered_source_month_keys(target, source)
    if source.person_name is not None or source.source_id is not None:
        keys = {month_key_for_item(target, item) for item in source_items(target, source, face_config)}
        return sorted(key for key in keys if valid_month_key(key))
    db_path = db.db_path_for_target(target)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return list(cached_browser_month_keys(str(target.resolve()), mtime_ns))


@lru_cache(maxsize=8)
def cached_browser_month_keys(target_path: str, db_mtime_ns: int) -> tuple[str, ...]:
    target = Path(target_path)
    conn = db.connect(target)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT substr(target_path, 1, 4) || '-' || substr(target_path, 6, 2) AS month_key
            FROM files
            WHERE deleted_at IS NULL
              AND target_path GLOB '[0-9][0-9][0-9][0-9]/[0-9][0-9]/*'
            ORDER BY month_key
            """
        )
        return tuple(str(row["month_key"]) for row in rows if valid_month_key(str(row["month_key"])))
    finally:
        conn.close()


def sql_filtered_source_month_keys(target: Path, source: BrowserSource) -> list[str]:
    where_sql, params = source_sql_filter(source)
    conn = db.connect(target)
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT substr(target_path, 1, 4) || '-' || substr(target_path, 6, 2) AS month_key
            FROM files
            WHERE deleted_at IS NULL
              AND ({where_sql})
              AND target_path GLOB '[0-9][0-9][0-9][0-9]/[0-9][0-9]/*'
            ORDER BY month_key
            """,
            params,
        )
        return [str(row["month_key"]) for row in rows if valid_month_key(str(row["month_key"]))]
    finally:
        conn.close()


def confirmed_people_for_file(
    target: Path,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> list[dict[str, object]]:
    face_db_path = current_face_db_path(target, face_config)
    try:
        mtime_ns = face_db_path.stat().st_mtime_ns
    except OSError:
        return []
    return [
        {"name": name, "url": person_item_url(name, file_id, show_faces=False), "confirmed": priority == 0}
        for name, priority in cached_confirmed_people_for_file(str(face_db_path), mtime_ns, file_id)
    ]


@lru_cache(maxsize=512)
def cached_confirmed_people_for_file(face_db_path: str, face_db_mtime_ns: int, file_id: int) -> tuple[tuple[str, int], ...]:
    conn = sqlite3.connect(face_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return ()
        rows = conn.execute(
            """
            SELECT persons.name, 0 AS priority
            FROM person_faces
            JOIN persons ON persons.id = person_faces.person_id
            JOIN faces ON faces.id = person_faces.face_id
            WHERE faces.file_id = ?
            UNION ALL
            SELECT persons.name, 1 AS priority
            FROM face_suggestions
            JOIN persons ON persons.id = face_suggestions.person_id
            JOIN faces ON faces.id = face_suggestions.face_id
            WHERE faces.file_id = ?
            ORDER BY name, priority
            """,
            (file_id, file_id),
        )
        people: dict[str, int] = {}
        for row in rows:
            name = str(row["name"])
            priority = int(row["priority"])
            if name not in people or priority < people[name]:
                people[name] = priority
        return tuple(sorted(people.items()))
    except sqlite3.Error:
        return ()
    finally:
        conn.close()


def person_item_url_for_face(
    target: Path,
    person_name: str,
    face_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    face_db_path = current_face_db_path(target, face_config)
    try:
        conn = sqlite3.connect(face_db_path)
        try:
            row = conn.execute("SELECT file_id FROM faces WHERE id = ?", (face_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        row = None
    if row is None:
        return person_url(person_name, show_faces=False)
    return person_item_url(person_name, int(row[0]), show_faces=False)


def clear_face_caches() -> None:
    cached_confirmed_people_for_file.cache_clear()
    cached_person_file_ids.cache_clear()
    cached_registered_people.cache_clear()


def remove_file_from_browser(target: Path, file_id: int) -> Path:
    with TargetLock(target, command="remove"):
        conn = db.connect(target)
        try:
            row = conn.execute(
                """
                SELECT id, target_path, deleted_at
                FROM files
                WHERE id = ?
                """,
                (file_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Filen finnes ikke i importdatabasen.")
            if row["deleted_at"] is not None:
                raise ValueError("Filen er allerede markert som slettet.")

            original_path = db.absolute_target_path(target, Path(str(row["target_path"]))).resolve()
            if not original_path.exists():
                raise ValueError(f"Målfilen finnes ikke på disk: {original_path}")
            try:
                relative_path = original_path.relative_to(target.resolve())
            except ValueError as exc:
                raise ValueError(f"Filen ligger ikke i bildesamlingen: {original_path}") from exc
            if not relative_path.parts or relative_path.parts[0] == "deleted":
                raise ValueError(f"Kan ikke slette filer fra deleted/: {original_path}")

            deleted_path = target / "deleted" / relative_path
            if deleted_path.exists():
                raise ValueError(f"Slettemål finnes allerede: {deleted_path}")

            deleted_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(original_path), str(deleted_path))
            db.mark_file_deleted(
                conn,
                file_id=file_id,
                target_root=target,
                deleted_path=deleted_path,
                original_target_path=original_path,
            )
            conn.commit()
            return db.target_relative_path(target, deleted_path)
        finally:
            conn.close()


def undelete_file_from_browser(target: Path, file_id: int) -> Path:
    with TargetLock(target, command="undelete"):
        conn = db.connect(target)
        try:
            row = conn.execute(
                """
                SELECT id, target_path, deleted_at, deleted_original_target_path
                FROM files
                WHERE id = ?
                """,
                (file_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Filen finnes ikke i importdatabasen.")
            if row["deleted_at"] is None:
                raise ValueError("Filen er ikke markert som slettet.")
            if row["deleted_original_target_path"] is None:
                raise ValueError("Filen mangler opprinnelig målsti i databasen.")

            deleted_path = db.absolute_target_path(target, Path(str(row["target_path"]))).resolve()
            if not deleted_path.exists():
                raise ValueError(f"Slettet fil finnes ikke på disk: {deleted_path}")
            try:
                deleted_relative_path = deleted_path.relative_to(target.resolve())
            except ValueError as exc:
                raise ValueError(f"Filen ligger ikke i bildesamlingen: {deleted_path}") from exc
            if len(deleted_relative_path.parts) < 2 or deleted_relative_path.parts[0] != "deleted":
                raise ValueError(f"Slettet fil ligger ikke under deleted/: {deleted_path}")

            restored_path = target / Path(str(row["deleted_original_target_path"]))
            if restored_path.exists():
                raise ValueError(f"Målfilen finnes allerede: {restored_path}")

            restored_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(deleted_path), str(restored_path))
            db.mark_file_undeleted(
                conn,
                file_id=file_id,
                target_root=target,
                restored_path=restored_path,
            )
            conn.commit()
            return db.target_relative_path(target, restored_path)
        finally:
            conn.close()


def registered_people(target: Path, face_config: FaceRecognitionConfig | None = None) -> list[dict[str, str]]:
    face_db_path = current_face_db_path(target, face_config)
    try:
        mtime_ns = face_db_path.stat().st_mtime_ns
    except OSError:
        return []
    return [
        {"name": name, "url": person_url(name)}
        for name in cached_registered_people(str(face_db_path), mtime_ns)
    ]


@lru_cache(maxsize=8)
def cached_registered_people(face_db_path: str, face_db_mtime_ns: int) -> tuple[str, ...]:
    conn = sqlite3.connect(face_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return ()
        rows = conn.execute("SELECT name FROM persons ORDER BY name")
        return tuple(str(row["name"]) for row in rows)
    except sqlite3.Error:
        return ()
    finally:
        conn.close()


def registered_people_rows(target: Path, face_config: FaceRecognitionConfig | None = None) -> list[dict[str, object]]:
    face_db_path = current_face_db_path(target, face_config)
    if not face_db_path.exists():
        return []
    conn = sqlite3.connect(face_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return []
        rows = conn.execute(
            """
            SELECT
                persons.name,
                (
                    SELECT COUNT(DISTINCT faces.file_id)
                    FROM person_faces
                    JOIN faces ON faces.id = person_faces.face_id
                    WHERE person_faces.person_id = persons.id
                ) AS confirmed_file_count,
                (
                    SELECT COUNT(DISTINCT file_id)
                    FROM (
                        SELECT faces.file_id
                        FROM person_faces
                        JOIN faces ON faces.id = person_faces.face_id
                        WHERE person_faces.person_id = persons.id
                        UNION
                        SELECT faces.file_id
                        FROM face_suggestions
                        JOIN faces ON faces.id = face_suggestions.face_id
                        WHERE face_suggestions.person_id = persons.id
                    )
                ) AS all_file_count,
                (
                    SELECT COUNT(*)
                    FROM face_suggestions
                    WHERE face_suggestions.person_id = persons.id
                ) AS suggestion_count,
                (
                    SELECT COUNT(*)
                    FROM (
                        SELECT faces.file_id
                        FROM person_faces
                        JOIN faces ON faces.id = person_faces.face_id
                        WHERE person_faces.person_id = persons.id
                        GROUP BY faces.file_id
                        HAVING COUNT(*) > 1
                    )
                ) AS duplicate_confirmed_file_count,
                (
                    SELECT COALESCE(MAX(confirmed_face_count), 0)
                    FROM (
                        SELECT COUNT(*) AS confirmed_face_count
                        FROM person_faces
                        JOIN faces ON faces.id = person_faces.face_id
                        WHERE person_faces.person_id = persons.id
                        GROUP BY faces.file_id
                    )
                ) AS max_confirmed_faces_per_file
            FROM persons
            ORDER BY persons.name
            """
        )
        return [
            {
                "name": str(row["name"]),
                "confirmed_file_count": int(row["confirmed_file_count"]),
                "all_file_count": int(row["all_file_count"]),
                "suggestion_count": int(row["suggestion_count"]),
                "duplicate_confirmed_file_count": int(row["duplicate_confirmed_file_count"]),
                "max_confirmed_faces_per_file": int(row["max_confirmed_faces_per_file"]),
            }
            for row in rows
        ]
    finally:
        conn.close()


def unconfirmed_faces_for_item(
    target: Path,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> list[dict[str, object]]:
    face_db_path = current_face_db_path(target, face_config)
    if not face_db_path.exists():
        return []
    conn = sqlite3.connect(face_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return []
        rows = conn.execute(
            """
            SELECT
                faces.id,
                faces.bbox_x,
                faces.bbox_y,
                faces.bbox_width,
                faces.bbox_height,
                faces.detection_score
            FROM faces
            WHERE faces.file_id = ?
              AND NOT EXISTS (
                SELECT 1
                FROM person_faces
                WHERE person_faces.face_id = faces.id
              )
            ORDER BY faces.id
            """,
            (int(item["id"]),),
        )
        faces = [
            {
                "faceId": int(row["id"]),
                "score": float(row["detection_score"]),
                "x": float(row["bbox_x"]),
                "y": float(row["bbox_y"]),
                "width": float(row["bbox_width"]),
                "height": float(row["bbox_height"]),
            }
            for row in rows
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return cached_face_box_items_for_item(target, item, faces)


def unconfirmed_face_count_for_item(
    target: Path,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> int:
    face_db_path = current_face_db_path(target, face_config)
    if not face_db_path.exists():
        return 0
    conn = sqlite3.connect(face_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return 0
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM faces
            WHERE faces.file_id = ?
              AND NOT EXISTS (
                SELECT 1
                FROM person_faces
                WHERE person_faces.face_id = faces.id
              )
            """,
            (file_id,),
        ).fetchone()
        return int(row[0] or 0) if row is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def browser_month_navigation(target: Path, item: Any) -> dict[str, str | None]:
    current_key = month_key_for_item(target, item)
    return browser_month_navigation_for_key(target, current_key)


def month_key_for_item(target: Path, item: Any) -> str:
    stored_key = month_key_from_stored_path(str(item["target_path"]))
    if stored_key is not None:
        return stored_key
    return month_key_from_path(relative_to_target(target, Path(str(item["target_path"]))))


def month_key_from_stored_path(path: str) -> str | None:
    match = MONTH_PATH_RE.search(path.replace("\\\\", "\\"))
    if match is None:
        return None
    month_key = f"{match.group('year')}-{match.group('month')}"
    return month_key if valid_month_key(month_key) else None


def person_by_name(
    target: Path,
    person_name: str,
    face_config: FaceRecognitionConfig | None = None,
) -> sqlite3.Row | None:
    face_db_path = current_face_db_path(target, face_config)
    if not face_db_path.exists():
        return None
    clean_name = normalize_person_name(person_name)
    conn = sqlite3.connect(face_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return None
        return conn.execute("SELECT id, name FROM persons WHERE name = ?", (clean_name,)).fetchone()
    finally:
        conn.close()


def person_file_ids(
    target: Path,
    person_name: str,
    *,
    include_suggestions: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> list[int]:
    face_db_path = current_face_db_path(target, face_config)
    try:
        mtime_ns = face_db_path.stat().st_mtime_ns
    except OSError:
        return []
    clean_name = normalize_person_name(person_name)
    return list(cached_person_file_ids(str(face_db_path), mtime_ns, clean_name, include_suggestions))


@lru_cache(maxsize=256)
def cached_person_file_ids(
    face_db_path: str,
    face_db_mtime_ns: int,
    person_name: str,
    include_suggestions: bool,
) -> tuple[int, ...]:
    conn = sqlite3.connect(face_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return ()
        person = conn.execute("SELECT id FROM persons WHERE name = ?", (person_name,)).fetchone()
        if person is None:
            return ()
        if include_suggestions:
            rows = conn.execute(
                """
                SELECT DISTINCT faces.file_id
                FROM person_faces
                JOIN faces ON faces.id = person_faces.face_id
                WHERE person_faces.person_id = ?
                UNION
                SELECT DISTINCT faces.file_id
                FROM face_suggestions
                JOIN faces ON faces.id = face_suggestions.face_id
                WHERE face_suggestions.person_id = ?
                ORDER BY file_id
                """,
                (int(person["id"]), int(person["id"])),
            )
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT faces.file_id
                FROM person_faces
                JOIN faces ON faces.id = person_faces.face_id
                WHERE person_faces.person_id = ?
                ORDER BY faces.file_id
                """,
                (int(person["id"]),),
            )
        return tuple(int(row["file_id"]) for row in rows)
    finally:
        conn.close()


def person_items(
    target: Path,
    person_name: str,
    *,
    include_suggestions: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> list[Any]:
    file_ids = person_file_ids(
        target,
        person_name,
        include_suggestions=include_suggestions,
        face_config=face_config,
    )
    return items_by_file_ids(target, file_ids)


def source_items(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
) -> list[Any]:
    if source.person_name is not None:
        return person_items(
            target,
            source.person_name,
            include_suggestions=source.include_suggestions,
            face_config=face_config,
        )
    if source.geo_place_slug is not None:
        return geo_place_items(target, source.geo_place_slug)
    if source.date_source is not None:
        conn = db.connect(target)
        try:
            return list(
                conn.execute(
                    f"""
                    SELECT {FILE_COLUMNS}
                    FROM files
                    WHERE deleted_at IS NULL
                      AND date_source = ?
                    ORDER BY {ITEM_ORDER_SQL}
                    """,
                    (source.date_source,),
                )
            )
        finally:
            conn.close()
    if source.source_id is not None:
        conn = db.connect(target)
        try:
            return list(
                conn.execute(
                    f"""
                    SELECT
                        files.id,
                        files.target_path,
                        files.target_path_key,
                        files.stored_filename,
                        files.taken_date,
                        files.date_source,
                        files.size_bytes,
                        files.view_rotation_degrees,
                        files.gps_lat,
                        files.gps_lon,
                        files.media_width,
                        files.media_height,
                        files.media_orientation,
                        files.media_metadata_mtime_ns,
                        {db.H3_FILE_COLUMNS_SQL}
                    FROM files
                    JOIN file_sources ON file_sources.file_id = files.id
                    WHERE files.deleted_at IS NULL
                      AND file_sources.source_id = ?
                    ORDER BY {ITEM_ORDER_SQL}
                    """,
                    (source.source_id,),
                )
            )
        finally:
            conn.close()
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                ORDER BY {ITEM_ORDER_SQL}
                """
            )
        )
    finally:
        conn.close()


def items_by_file_ids(target: Path, file_ids: list[int]) -> list[Any]:
    if not file_ids:
        return []
    placeholders = ",".join("?" for _ in file_ids)
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                  AND id IN ({placeholders})
                ORDER BY {ITEM_ORDER_SQL}
                """,
                tuple(file_ids),
            )
        )
    finally:
        conn.close()


def geo_area_items(target: Path, *, h3_cell: str, resolution: int, limit: int) -> list[Any]:
    column = h3_column_for_resolution(resolution)
    conn = db.connect(target)
    try:
        return db.geo_area_files(conn, column=column, h3_cell=h3_cell, limit=limit)
    finally:
        conn.close()


def geo_place_items(target: Path, slug: str, *, limit: int | None = None) -> list[Any]:
    place = geo_place_by_slug(target, slug)
    if place is None:
        return []
    conn = db.connect(target)
    try:
        return db.geo_place_files(conn, cells_by_column=geo_place_cells_by_column(place), limit=limit)
    finally:
        conn.close()


def geo_place_by_slug(target: Path, slug: str) -> PredefinedGeoPlace | None:
    clean_slug = slug.strip().lower()
    place = predefined_geo_place(clean_slug)
    if place is not None:
        return place
    conn = db.connect(target)
    try:
        custom = db.custom_geo_place(conn, clean_slug)
    finally:
        conn.close()
    return geo_place_from_row(custom) if custom is not None else None


def geo_place_from_row(row: dict[str, object]) -> PredefinedGeoPlace:
    return PredefinedGeoPlace(
        slug=str(row["slug"]),
        name=str(row["name"]),
        h3_cells=tuple(str(cell) for cell in row["h3_cells"]),
    )


def custom_geo_places(conn: sqlite3.Connection) -> list[PredefinedGeoPlace]:
    return [geo_place_from_row(row) for row in db.custom_geo_places(conn)]


def geo_place_cells_by_column(place: PredefinedGeoPlace) -> list[tuple[str, str]]:
    import h3

    cells_by_column: list[tuple[str, str]] = []
    max_resolution = max(H3_COLUMNS)
    for cell in place.h3_cells:
        resolution = h3_resolution_any(cell)
        query_cell = h3.cell_to_parent(cell, max_resolution) if resolution > max_resolution else cell
        query_resolution = min(resolution, max_resolution)
        cells_by_column.append((h3_column_for_resolution(query_resolution), query_cell))
    return cells_by_column


def h3_resolution_any(h3_cell: str) -> int:
    import h3

    if hasattr(h3, "is_valid_cell") and not h3.is_valid_cell(h3_cell):
        raise ValueError(f"Ugyldig H3-celle: {h3_cell}")
    try:
        return int(h3.get_resolution(h3_cell))
    except Exception as exc:  # noqa: BLE001 - h3 raises library-specific exceptions
        raise ValueError(f"Ugyldig H3-celle: {h3_cell}") from exc


def geo_child_area_items(target: Path, *, h3_cell: str, resolution: int) -> list[Any]:
    if resolution >= max(H3_COLUMNS):
        return []
    parent_column = h3_column_for_resolution(resolution)
    child_column = h3_column_for_resolution(resolution + 1)
    conn = db.connect(target)
    try:
        return db.geo_child_areas(
            conn,
            parent_column=parent_column,
            parent_h3_cell=h3_cell,
            child_column=child_column,
        )
    finally:
        conn.close()


def geo_missing_items(target: Path, *, limit: int, offset: int) -> list[Any]:
    conn = db.connect(target)
    try:
        return db.geo_missing_files(conn, limit=limit, offset=offset)
    finally:
        conn.close()


def first_person_item(target: Path, person_name: str) -> Any | None:
    return first_source_item(target, person_browser_source(person_name, include_suggestions=True))


def person_item_by_id(target: Path, person_name: str, file_id: int) -> Any | None:
    return source_item_by_id(target, person_browser_source(person_name, include_suggestions=True), file_id)


def adjacent_person_items(target: Path, person_name: str, item: Any) -> tuple[Any | None, Any | None]:
    return adjacent_source_items(target, person_browser_source(person_name, include_suggestions=True), item)


def person_month_keys(target: Path, person_name: str) -> list[str]:
    return source_month_keys(target, person_browser_source(person_name, include_suggestions=True))


def person_month_navigation(target: Path, person_name: str, item: Any) -> dict[str, str | None]:
    return source_month_navigation(target, person_browser_source(person_name, include_suggestions=True), item)


def person_month_navigation_for_key(target: Path, person_name: str, current_key: str) -> dict[str, str | None]:
    return source_month_navigation_for_key(target, person_browser_source(person_name, include_suggestions=True), current_key)


def source_month_navigation(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> dict[str, str | None]:
    return source_month_navigation_for_key(target, source, month_key_for_item(target, item), face_config)


def source_month_navigation_for_key(
    target: Path,
    source: BrowserSource,
    current_key: str,
    face_config: FaceRecognitionConfig | None = None,
) -> dict[str, str | None]:
    if not valid_month_key(current_key):
        return {"previous_year": None, "next_year": None, "previous_month": None, "next_month": None}
    keys = source_month_keys(target, source, face_config)
    if not keys:
        return {"previous_year": None, "next_year": None, "previous_month": None, "next_month": None}
    years = sorted({key[:4] for key in keys})
    current_year = current_key[:4]
    current_year_index = years.index(current_year) if current_year in years else -1
    previous_year = years[current_year_index - 1] if current_year_index > 0 else None
    next_year = years[current_year_index + 1] if current_year_index < len(years) - 1 else None
    return {
        "previous_year": first_month_in_year(keys, previous_year),
        "next_year": first_month_in_year(keys, next_year),
        "previous_month": next((key for key in reversed(keys) if key < current_key), None),
        "next_month": next((key for key in keys if key > current_key), None),
    }


def person_month_items(target: Path, person_name: str, month_key: str) -> list[Any]:
    return source_month_items(target, person_browser_source(person_name, include_suggestions=True), month_key)


def source_month_items(
    target: Path,
    source: BrowserSource,
    month_key: str,
    face_config: FaceRecognitionConfig | None = None,
) -> list[Any]:
    if source_has_sql_filter(source):
        return sql_filtered_source_month_items(target, source, month_key)
    if source.person_name is not None or source.source_id is not None:
        return [
            item
            for item in source_items(target, source, face_config)
            if month_key_for_item(target, item) == month_key
        ]
    return browser_month_items(target, month_key)


def sql_filtered_source_month_items(target: Path, source: BrowserSource, month_key: str) -> list[Any]:
    if not valid_month_key(month_key):
        return []
    where_sql, params = source_sql_filter(source)
    year, month = month_key.split("-", 1)
    path_glob = f"{year}/{month}/*"
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                  AND ({where_sql})
                  AND target_path GLOB ?
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (*params, path_glob),
            )
        )
    finally:
        conn.close()


def person_faces_for_item(
    target: Path,
    person_name: str,
    item: Any,
    *,
    include_suggestions: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> list[dict[str, object]]:
    person = person_by_name(target, person_name, face_config)
    if person is None:
        return []
    conn = sqlite3.connect(current_face_db_path(target, face_config))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                'bekreftet' AS status,
                faces.id,
                1.0 AS similarity,
                faces.bbox_x,
                faces.bbox_y,
                faces.bbox_width,
                faces.bbox_height,
                faces.detection_score
            FROM person_faces
            JOIN faces ON faces.id = person_faces.face_id
            WHERE person_faces.person_id = ?
              AND faces.file_id = ?
            """ + (
                """
                UNION ALL
                SELECT
                    'forslag' AS status,
                    faces.id,
                    face_suggestions.similarity,
                    faces.bbox_x,
                    faces.bbox_y,
                    faces.bbox_width,
                    faces.bbox_height,
                    faces.detection_score
                FROM face_suggestions
                JOIN faces ON faces.id = face_suggestions.face_id
                WHERE face_suggestions.person_id = ?
                  AND faces.file_id = ?
                """
                if include_suggestions
                else ""
            ) + """
            ORDER BY status, id
            """,
            (
                (int(person["id"]), int(item["id"]), int(person["id"]), int(item["id"]))
                if include_suggestions
                else (int(person["id"]), int(item["id"]))
            ),
        )
        faces = [
            {
                "faceId": int(row["id"]),
                "status": str(row["status"]),
                "similarity": float(row["similarity"]),
                "score": float(row["detection_score"]),
                "x": float(row["bbox_x"]),
                "y": float(row["bbox_y"]),
                "width": float(row["bbox_width"]),
                "height": float(row["bbox_height"]),
            }
            for row in rows
        ]
    finally:
        conn.close()
    face_meta = {int(face["faceId"]): face for face in faces}
    rendered = cached_face_box_items_for_item(target, item, faces)
    for face in rendered:
        meta = face_meta.get(int(face["faceId"]))
        if meta is not None:
            face["status"] = meta["status"]
            face["similarity"] = meta["similarity"]
    return rendered


def cached_face_box_items_for_item(target: Path, item: Any, faces: list[dict[str, object]]) -> list[dict[str, object]]:
    dimensions, orientation = cached_face_box_media_metadata(target, item)
    return browser_face_items_from_metadata(faces, dimensions, orientation)


def cached_face_box_media_metadata(target: Path, item: Any) -> tuple[ImageDimensions | None, int]:
    target_path = db.absolute_target_path(target, Path(str(item["target_path"])))
    mtime_ns = file_mtime_ns(target_path)
    cached_dimensions, cached_orientation = face_box_media_metadata_from_item(item, mtime_ns)
    if cached_orientation is not None:
        return cached_dimensions, cached_orientation

    dimensions = image_dimensions(target_path)
    orientation = image_orientation(target_path)
    update_face_box_media_metadata(target, int(item["id"]), dimensions, orientation, mtime_ns)
    return dimensions, orientation


def face_box_media_metadata_from_item(item: Any, mtime_ns: int | None) -> tuple[ImageDimensions | None, int | None]:
    cached_mtime = item_field(item, "media_metadata_mtime_ns")
    if cached_mtime is None or mtime_ns is None or int(cached_mtime) != mtime_ns:
        return None, None
    orientation = item_field(item, "media_orientation")
    if orientation is None:
        return None, None
    width = item_field(item, "media_width")
    height = item_field(item, "media_height")
    dimensions = None
    if width is not None and height is not None and int(width) > 0 and int(height) > 0:
        dimensions = ImageDimensions(int(width), int(height))
    return dimensions, int(orientation)


def item_field(item: Any, key: str) -> Any | None:
    try:
        return item[key]
    except (KeyError, IndexError):
        return None


def file_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def update_face_box_media_metadata(
    target: Path,
    file_id: int,
    dimensions: ImageDimensions | None,
    orientation: int,
    mtime_ns: int | None,
) -> None:
    conn = db.connect(target)
    try:
        conn.execute(
            """
            UPDATE files
            SET media_width = ?,
                media_height = ?,
                media_orientation = ?,
                media_metadata_mtime_ns = ?
            WHERE id = ?
            """,
            (
                dimensions.width if dimensions is not None else None,
                dimensions.height if dimensions is not None else None,
                orientation,
                mtime_ns,
                file_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def person_url(person_name: str, *, show_faces: bool = True) -> str:
    url = "/person/" + urllib.parse.quote(person_name, safe="")
    return url if show_faces else f"{url}/no-faces"


def person_item_url(person_name: str, file_id: int, *, show_faces: bool = True) -> str:
    return f"{person_url(person_name, show_faces=show_faces)}/item/{file_id}"


def person_month_url(person_name: str, month_key: str) -> str:
    return f"{person_url(person_name)}/month/{urllib.parse.quote(month_key)}"


def browser_month_navigation_for_key(target: Path, current_key: str) -> dict[str, str | None]:
    if not valid_month_key(current_key):
        return {
            "previous_year": None,
            "next_year": None,
            "previous_month": None,
            "next_month": None,
        }
    keys = browser_month_keys(target)
    if not keys:
        return {
            "previous_year": None,
            "next_year": None,
            "previous_month": None,
            "next_month": None,
        }
    years = sorted({key[:4] for key in keys})
    current_year = current_key[:4]
    current_year_index = years.index(current_year) if current_year in years else -1
    previous_year = years[current_year_index - 1] if current_year_index > 0 else None
    next_year = years[current_year_index + 1] if current_year_index < len(years) - 1 else None
    previous_month = next((key for key in reversed(keys) if key < current_key), None)
    next_month = next((key for key in keys if key > current_key), None)
    return {
        "previous_year": first_month_in_year(keys, previous_year),
        "next_year": first_month_in_year(keys, next_year),
        "previous_month": previous_month,
        "next_month": next_month,
    }


def first_month_in_year(keys: list[str], year: str | None) -> str | None:
    if year is None:
        return None
    return next((key for key in keys if key.startswith(year)), None)


def browser_month_items(target: Path, month_key: str) -> list[Any]:
    year, month = month_key.split("-", 1)
    prefix = db.relative_path_key(Path(year) / month) + "/"
    conn = db.connect(target)
    try:
        rows = list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                  AND target_path_key LIKE ?
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (prefix + "%",),
            )
        )
        if rows:
            return rows
        return [
            row
            for row in conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                ORDER BY {ITEM_ORDER_SQL}
                """
            )
            if month_key_from_stored_path(str(row["target_path"])) == month_key
        ]
    finally:
        conn.close()


def valid_month_key(value: str) -> bool:
    if len(value) != 7 or value[4] != "-":
        return False
    year, month = value.split("-", 1)
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= 12


def search_server_images(server: BildebankServer, *, query: str, limit: int) -> ServerSearchStats:
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Søketekst kan ikke være tom.")
    openclip_config = server.config.openclip
    conn = connect_openclip_db(server.target)
    try:
        rows = list(
            conn.execute(
                """
                SELECT file_id, target_path, target_path_key, embedding
                FROM image_embeddings
                WHERE model_name = ? AND pretrained = ?
                """,
                (openclip_config.model_name, openclip_config.pretrained),
            )
        )
        if not rows:
            raise ValueError("Fant ingen bilde-embeddings. Kjør bildebank image-scan først.")
        text_vector = server.search_cache.text_vector(clean_query)
        scored = sorted(
            (
                (
                    cosine_similarity(text_vector, embedding_from_blob(bytes(row["embedding"]))),
                    int(row["file_id"]),
                    Path(str(row["target_path"])),
                    str(row["target_path_key"]),
                )
                for row in rows
            ),
            reverse=True,
            key=lambda item: item[0],
        )[:limit]
        run_id = create_search_run(conn, clean_query, openclip_config, limit)
        results: list[ImageSearchResult] = []
        for index, (score, file_id, target_path, target_path_key) in enumerate(scored, start=1):
            conn.execute(
                """
                INSERT INTO image_search_results(run_id, file_id, target_path, target_path_key, similarity, rank)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    file_id,
                    target_path.as_posix(),
                    target_path_key,
                    score,
                    index,
                ),
            )
            results.append(ImageSearchResult(index, file_id, target_path, score))
        conn.commit()
        return ServerSearchStats(clean_query, tuple(results))
    finally:
        conn.close()


def index_html(server: BildebankServer, *, message: str = "") -> str:
    if message:
        return search_start_html(server, message=message)
    item = first_browser_item(server.target)
    if item is None:
        return empty_browser_html(openclip_enabled=server.openclip_enabled)
    previous_item, next_item = adjacent_browser_items(server.target, item)
    month_nav = browser_month_navigation(server.target, item)
    return item_page_html(
        server.target,
        item,
        previous_item,
        next_item,
        month_nav,
        face_enabled=server.face_enabled,
        openclip_enabled=server.openclip_enabled,
    )


def search_start_html(server: BildebankServer, *, message: str = "") -> str:
    openclip_config = server.config.openclip
    return page_html(
        "Bildesøk",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a></p>
          <h1>Bildesøk</h1>
          <p class="meta">OpenCLIP {html.escape(openclip_config.model_name)} ({html.escape(openclip_config.pretrained)})</p>
          {message_html(message)}
          {search_form("")}
        </main>
        """,
    )


def search_html(server: BildebankServer, stats: ServerSearchStats, limit: int) -> str:
    items = "\n".join(result_html(server.target, result) for result in stats.results)
    return page_html(
        f"Bildesøk: {stats.query}",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a></p>
          <h1>Bildesøk</h1>
          {search_form(stats.query, limit)}
          <p class="meta">{len(stats.results)} treff. Sortert med beste match først. Modell lastet: {'ja' if server.search_cache.loaded else 'nei'}.</p>
          <div class="grid">
            {items}
          </div>
        </main>
        """,
    )


def geo_index_page_html(
    target: Path,
    *,
    resolution: int = DEFAULT_GEO_RESOLUTION,
    min_count: int = DEFAULT_GEO_MIN_COUNT,
    limit: int = DEFAULT_GEO_LIMIT,
) -> str:
    column = h3_column_for_resolution(resolution)
    conn = db.connect(target)
    try:
        stats = db.geo_stats(conn)
        areas = db.geo_areas(conn, column=column, min_count=min_count, limit=limit)
        geo_places = geo_place_rows(conn)
    finally:
        conn.close()
    area_links = "\n".join(geo_area_row_html(row, resolution=resolution) for row in areas)
    content = (
        f'<div class="geo-list">{area_links}</div>'
        if area_links
        else '<p class="meta">Ingen steder med nok bilder. Kjør bildebank geo-scan, eller senk min_count.</p>'
    )
    return page_html(
        "Steder",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a>
           · <a href="/geo/map?resolution={resolution}&min_count={min_count}&limit={limit}">Heksagonkart</a>
           · <a href="/geo/stats">Geo-statistikk</a>
           · <a href="/geo/missing">Bilder uten GPS</a>
           · <a href="/help/web/steder">Hjelp</a></p>
          <h2>Statistikk over bilder med GPS-posisjon</h2>
          {geo_stats_summary_html(stats)}
          <p class="meta">Geo-data leses fra databasen. Kjør bildebank geo-scan for å fylle inn GPS og H3-celler.</p>
          {geo_places_section_html(geo_places)}
          <h2>H3-heksagoner</h2>
          {geo_filter_form_html("/geo", resolution=resolution, min_count=min_count, limit=limit)}
          <p class="meta">Viser H3-{h3_resolution_label(resolution)}. Lavere tall gir større områder. {len(areas)} steder funnet.</p>
          {content}
        </main>
        """,
    )


def custom_geo_places_page_html(target: Path) -> str:
    conn = db.connect(target)
    try:
        places = custom_geo_places(conn)
    finally:
        conn.close()
    return page_html(
        "Egendefinerte steder",
        f"""
        <main class="shell">
          <p><a href="/geo">Til steder</a>
           · <a href="/">Til bildebrowser</a>
           · <a href="/help/web/egendefinerte-steder.md">Hjelp</a>
          </p>
          <h1>Egne steder</h1>
          {custom_geo_places_admin_html(places)}
        </main>
        """,
    )


def geo_place_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    return [
        {
            "slug": place.slug,
            "name": place.name,
            "kind": "system" if place in PREDEFINED_GEO_PLACES else "user",
            "count": db.geo_place_count(conn, cells_by_column=geo_place_cells_by_column(place)),
        }
        for place in sorted((*PREDEFINED_GEO_PLACES, *custom_geo_places(conn)), key=lambda item: (item.name, item.slug))
    ]


def geo_places_section_html(rows: list[dict[str, object]]) -> str:
    if not rows:
        return ""
    links = "\n".join(geo_place_row_html(row) for row in rows)
    return f"""
          <section class="geo-predefined">
            <h2>Definerte steder</h2>
            <a href="/geo/custom-places">Rediger egendefinerte steder</a>
            <div class="geo-list">{links}</div>
          </section>
    """


def geo_place_row_html(row: dict[str, object]) -> str:
    slug = str(row["slug"])
    name = str(row["name"])
    count = int(row["count"])
    url = "/geo/place/" + urllib.parse.quote(slug, safe="")
    if row["kind"] == 'system':
        icon = "system"
    else:
        icon = "user"
    return f"""
    <a class="geo-row" href="{html.escape(url)}">
      <span>{html.escape(name)}</span>
      <span class="status">{icon}</span>
      <strong>{count} bilder</strong>
    </a>
    """


def custom_geo_places_admin_html(places: list[PredefinedGeoPlace]) -> str:
    existing = "\n".join(custom_geo_place_edit_html(place) for place in places)
    existing_section = (
        f"""
            <h2>Lagrede steder</h2>
            <div class="custom-place-list">{existing}</div>
        """
        if existing
        else '<p class="meta">Ingen egne steder er lagret ennå.</p>'
    )
    return f"""
          <section class="custom-geo-places">
            <h2>Legg til sted</h2>
            {custom_geo_place_form_html()}
            {existing_section}
          </section>
    """


def custom_geo_place_form_html(place: PredefinedGeoPlace | None = None) -> str:
    slug = place.slug if place is not None else ""
    name = place.name if place is not None else ""
    cells = "\n".join(place.h3_cells) if place is not None else ""
    button_text = "Oppdater sted" if place is not None else "Legg til sted"
    delete_button = (
        '<button class="danger-button" type="submit" '
        'formaction="/geo/custom-place-delete" formmethod="post">Slett sted</button>'
        if place is not None
        else ""
    )
    return f"""
    <form action="/geo/custom-place" method="post" class="custom-place-form">
      <div class="custom-place-identity">
        <label>Slug <input name="slug" value="{html.escape(slug)}" autocomplete="off"></label>
        <label>Navn <input name="name" value="{html.escape(name)}" autocomplete="off"></label>
      </div>
      <label class="custom-place-cells">H3-celler <textarea name="h3_cells" rows="4">{html.escape(cells)}</textarea></label>
      <div class="custom-place-actions">
        <button type="submit">{button_text}</button>
        {delete_button}
      </div>
    </form>
    """


def custom_geo_place_edit_html(place: PredefinedGeoPlace) -> str:
    cell_count = len(place.h3_cells)
    cell_label = "1 H3-celle" if cell_count == 1 else f"{cell_count} H3-celler"
    return f"""
    <details class="custom-place-edit">
      <summary>
        <span class="custom-place-name">{html.escape(place.name)}</span>
        <span class="status">{html.escape(place.slug)}</span>
        <span class="status">{cell_label}</span>
      </summary>
      <div class="custom-place-edit-body">
        {custom_geo_place_form_html(place)}
      </div>
    </details>
    """


def geo_map_page_html(
    target: Path,
    *,
    resolution: int = DEFAULT_GEO_RESOLUTION,
    min_count: int = DEFAULT_GEO_MIN_COUNT,
    limit: int = DEFAULT_GEO_LIMIT,
) -> str:
    column = h3_column_for_resolution(resolution)
    conn = db.connect(target)
    try:
        areas = db.geo_areas(conn, column=column, min_count=min_count, limit=limit)
    finally:
        conn.close()
    cells = geo_map_layout(areas)
    content = geo_map_svg_html(cells) if cells else '<p class="meta">Ingen steder med nok bilder. Kjør bildebank geo-scan, eller senk min_count.</p>'
    return page_html(
        "Heksagonkart",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a> · <a href="/geo?resolution={resolution}&min_count={min_count}&limit={limit}">Steder</a></p>
          <h1>Heksagonkart</h1>
          {geo_filter_form_html("/geo/map", resolution=resolution, min_count=min_count, limit=limit)}
          <p class="meta">Viser H3-{h3_resolution_label(resolution)}. Heksagoner som er H3-naboer legges sammen i klynger. Hver klynge orienteres etter faktiske GPS-retninger, men klyngene er ikke plassert med geografisk avstand.</p>
          {content}
        </main>
        """,
    )


def geo_filter_form_html(action: str, *, resolution: int, min_count: int, limit: int) -> str:
    return f"""
          <form action="{html.escape(action)}" method="get" class="geo-filter">
            <label>H3-oppløsning {h3_resolution_select_html(resolution)}</label>
            <label>Minst antall <input name="min_count" value="{min_count}" inputmode="numeric"></label>
            <label>Maks steder <input name="limit" value="{limit}" inputmode="numeric"></label>
            <button type="submit">Vis</button>
          </form>
    """


def h3_resolution_select_html(selected_resolution: int) -> str:
    options = "\n".join(
        f'<option value="{resolution}"{" selected" if resolution == selected_resolution else ""}>H3-{resolution} ({html.escape(h3_area_label(resolution))})</option>'
        for resolution in sorted(H3_COLUMNS)
    )
    return f'<select name="resolution">{options}</select>'


def geo_map_layout(rows: list[Any]) -> list[GeoMapCell]:
    import h3

    row_by_cell = {str(row["h3_cell"]): row for row in rows}
    remaining = set(row_by_cell)
    components: list[list[str]] = []
    while remaining:
        start = min(remaining)
        remaining.remove(start)
        component = [start]
        stack = [start]
        while stack:
            cell = stack.pop()
            for neighbor in sorted(set(h3.grid_disk(cell, 1)) & remaining):
                remaining.remove(neighbor)
                stack.append(neighbor)
                component.append(neighbor)
        components.append(sorted(component))

    size = 28.0
    component_gap = 120.0
    max_row_width = 980.0
    placed: list[GeoMapCell] = []
    offset_x = 0.0
    offset_y = 0.0
    row_height = 0.0
    for component in sorted(components, key=lambda cells: (-len(cells), cells[0])):
        coords = geo_component_pixel_coordinates(component, size)
        min_x = min(x for x, _ in coords.values())
        min_y = min(y for _, y in coords.values())
        max_x = max(x for x, _ in coords.values())
        max_y = max(y for _, y in coords.values())
        width = max_x - min_x + size * 3
        height = max_y - min_y + size * 3
        if offset_x > 0 and offset_x + width > max_row_width:
            offset_x = 0.0
            offset_y += row_height + component_gap
            row_height = 0.0
        for cell in component:
            row = row_by_cell[cell]
            pixel_x, pixel_y = coords[cell]
            placed.append(
                GeoMapCell(
                    h3_cell=cell,
                    count=int(row["count"]),
                    name=str(row["name"]) if "name" in row.keys() and row["name"] else None,
                    x=offset_x + (pixel_x - min_x) + size * 1.5,
                    y=offset_y + (pixel_y - min_y) + size * 1.5,
                )
            )
        offset_x += width + component_gap
        row_height = max(row_height, height)
    return placed


def geo_component_pixel_coordinates(cells: list[str], size: float) -> dict[str, tuple[float, float]]:
    coords = geo_component_grid_coordinates(cells)
    pixels = {cell: (x * size * math.sqrt(3), y * size * 1.5) for cell, (x, y) in coords.items()}
    if len(cells) < 2:
        return pixels
    return geo_oriented_component_pixels(cells, pixels)


def geo_component_grid_coordinates(cells: list[str]) -> dict[str, tuple[float, float]]:
    import h3

    origin = cells[0]
    coords: dict[str, tuple[float, float]] = {}
    try:
        for cell in cells:
            ij = h3.cell_to_local_ij(origin, cell)
            i = float(ij[0] if isinstance(ij, tuple) else ij["i"])
            j = float(ij[1] if isinstance(ij, tuple) else ij["j"])
            coords[cell] = ((i + j) * 0.5, -i + j)
    except Exception:  # noqa: BLE001 - H3 can fail for cells without a shared local IJ space
        coords = geo_component_fallback_coordinates(cells)
    return coords


def geo_oriented_component_pixels(
    cells: list[str],
    pixels: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    import h3

    lat_lon = {cell: h3.cell_to_latlng(cell) for cell in cells}
    center_lat = sum(float(lat) for lat, _ in lat_lon.values()) / len(lat_lon)
    lon_scale = max(0.2, math.cos(math.radians(center_lat)))
    geo_points = {
        cell: (float(lon) * lon_scale, -float(lat))
        for cell, (lat, lon) in lat_lon.items()
    }
    candidates = []
    for reflect in (False, True):
        reflected = {
            cell: ((-x if reflect else x), y)
            for cell, (x, y) in pixels.items()
        }
        for step in range(6):
            angle = step * math.pi / 3
            candidates.append(geo_rotate_points(reflected, angle))
    return max(candidates, key=lambda candidate: geo_orientation_score(cells, candidate, geo_points))


def geo_rotate_points(points: dict[str, tuple[float, float]], angle: float) -> dict[str, tuple[float, float]]:
    cos_angle = math.cos(angle)
    sin_angle = math.sin(angle)
    return {
        cell: (x * cos_angle - y * sin_angle, x * sin_angle + y * cos_angle)
        for cell, (x, y) in points.items()
    }


def geo_orientation_score(
    cells: list[str],
    layout_points: dict[str, tuple[float, float]],
    geo_points: dict[str, tuple[float, float]],
) -> float:
    score = 0.0
    for index, first in enumerate(cells):
        for second in cells[index + 1 :]:
            layout_dx = layout_points[second][0] - layout_points[first][0]
            layout_dy = layout_points[second][1] - layout_points[first][1]
            geo_dx = geo_points[second][0] - geo_points[first][0]
            geo_dy = geo_points[second][1] - geo_points[first][1]
            layout_length = math.hypot(layout_dx, layout_dy)
            geo_length = math.hypot(geo_dx, geo_dy)
            if layout_length == 0 or geo_length == 0:
                continue
            score += (layout_dx / layout_length) * (geo_dx / geo_length)
            score += (layout_dy / layout_length) * (geo_dy / geo_length)
    return score


def geo_component_fallback_coordinates(cells: list[str]) -> dict[str, tuple[float, float]]:
    import h3

    directions = [(1.0, 0.0), (0.5, 1.0), (-0.5, 1.0), (-1.0, 0.0), (-0.5, -1.0), (0.5, -1.0)]
    cell_set = set(cells)
    coords = {cells[0]: (0.0, 0.0)}
    queue = [cells[0]]
    while queue:
        cell = queue.pop(0)
        x, y = coords[cell]
        for index, neighbor in enumerate(sorted(set(h3.grid_disk(cell, 1)) & cell_set)):
            if neighbor in coords or neighbor == cell:
                continue
            dx, dy = directions[index % len(directions)]
            coords[neighbor] = (x + dx, y + dy)
            queue.append(neighbor)
    for cell in cells:
        coords.setdefault(cell, (float(len(coords)), 0.0))
    return coords


def geo_map_svg_html(cells: list[GeoMapCell]) -> str:
    size = 28.0
    max_x = max(cell.x for cell in cells) + size * 2
    max_y = max(cell.y for cell in cells) + size * 2
    shapes = "\n".join(geo_map_cell_svg(cell, size=size) for cell in cells)
    return f"""
    <div class="geo-map-wrap">
      <svg class="geo-map" viewBox="0 0 {max_x:.0f} {max_y:.0f}" role="img" aria-label="H3-heksagonkart">
        {shapes}
      </svg>
    </div>
    """


def geo_map_cell_svg(cell: GeoMapCell, *, size: float) -> str:
    points = []
    for index in range(6):
        angle = math.pi / 6 + index * math.pi / 3
        points.append(f"{cell.x + size * math.cos(angle):.1f},{cell.y + size * math.sin(angle):.1f}")
    label = cell.name or cell.h3_cell
    url = "/geo/area/" + urllib.parse.quote(cell.h3_cell, safe="")
    return f"""
    <a class="geo-hex-link" href="{html.escape(url)}">
      <polygon class="geo-hex" points="{' '.join(points)}"></polygon>
      <text class="geo-hex-count" x="{cell.x:.1f}" y="{cell.y + 4:.1f}" text-anchor="middle">{cell.count}</text>
      <title>{html.escape(label)} - {cell.count} bilder</title>
    </a>
    """


def geo_stats_page_html(target: Path) -> str:
    conn = db.connect(target)
    try:
        stats = db.geo_stats(conn)
    finally:
        conn.close()
    return page_html(
        "Geo-statistikk",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a> · <a href="/geo">Steder</a> · <a href="/geo/missing">Bilder uten GPS</a></p>
          <h1>Geo-statistikk</h1>
          {geo_stats_summary_html(stats)}
          <p class="meta">Geo-data leses fra databasen. Kjør bildebank geo-scan for å fylle inn GPS og H3-celler.</p>
        </main>
        """,
    )


def geo_area_page_html(target: Path, h3_cell: str, *, resolution: int, limit: int = DEFAULT_GEO_LIMIT) -> str:
    conn = db.connect(target)
    try:
        place_name = db.geo_place_name(conn, h3_cell)
    finally:
        conn.close()
    items = geo_area_items(target, h3_cell=h3_cell, resolution=resolution, limit=limit)
    child_areas = geo_child_area_items(target, h3_cell=h3_cell, resolution=resolution)
    cards = "\n".join(source_month_item_html(target, all_browser_source(), item) for item in items)
    content = cards if cards else '<p class="meta">Ingen aktive bilder i dette området.</p>'
    maps_link = google_maps_link_html(items[0]) if items else ""
    maps_paragraph = f'<p class="meta">{maps_link}</p>' if maps_link else ""
    parent_link = geo_parent_area_link_html(target, h3_cell, resolution)
    quoted = urllib.parse.quote(h3_cell, safe="")
    title = place_name or "Sted"
    escaped_name = html.escape(place_name or "")
    child_area_section = geo_child_areas_section_html(
        child_areas,
        resolution=resolution + 1,
        inherited_name=place_name,
    )
    return page_html(
        f"{title} {h3_cell}",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a> · <a href="/geo">Steder</a></p>
          <h1>{html.escape(title)}</h1>
          <p class="meta">H3-celle {html.escape(h3_cell)}, {h3_resolution_label(resolution)}. Viser opptil {limit} bilder.{parent_link}</p>
          {maps_paragraph}
          <form action="/geo/place-name" method="post" class="geo-filter geo-name-form">
            <input type="hidden" name="h3_cell" value="{html.escape(h3_cell)}">
            <input type="hidden" name="limit" value="{limit}">
            <label>Stedsnavn <input name="name" value="{escaped_name}" autocomplete="off"></label>
            <button type="submit">Lagre navn</button>
          </form>
          {child_area_section}
          <form action="/geo/area/{html.escape(quoted)}" method="get" class="geo-filter">
            <label>Maks bilder <input name="limit" value="{limit}" inputmode="numeric"></label>
            <button type="submit">Vis</button>
          </form>
          <section class="month-grid-server">{content}</section>
        </main>
        """,
    )


def geo_parent_area_link_html(target: Path, h3_cell: str, resolution: int) -> str:
    if resolution <= min(H3_COLUMNS):
        return ""
    import h3

    parent_resolution = resolution - 1
    parent_cell = h3.cell_to_parent(h3_cell, parent_resolution)
    conn = db.connect(target)
    try:
        parent_name = db.geo_place_name(conn, parent_cell)
    finally:
        conn.close()
    url = "/geo/area/" + urllib.parse.quote(parent_cell, safe="")
    label = parent_name or parent_cell
    return (
        " "
        f'<a href="{html.escape(url)}">Større område: H3-{parent_resolution} '
        f"{html.escape(label)}</a>"
    )


def geo_child_areas_section_html(rows: list[Any], *, resolution: int, inherited_name: str | None = None) -> str:
    if not rows:
        return ""
    links = "\n".join(geo_area_row_html(row, resolution=resolution, inherited_name=inherited_name) for row in rows)
    return f"""
    <section class="geo-child-areas">
      <h2>Inneholder</h2>
      <p class="meta">Understeder på H3-{h3_resolution_label(resolution)}.</p>
      <div class="geo-list">{links}</div>
    </section>
    """


def geo_missing_page_html(target: Path, *, limit: int = DEFAULT_GEO_LIMIT, offset: int = 0) -> str:
    items = geo_missing_items(target, limit=limit, offset=offset)
    cards = "\n".join(source_month_item_html(target, all_browser_source(), item) for item in items)
    previous_offset = max(0, offset - limit)
    next_offset = offset + limit
    previous_link = (
        f'<a class="server-search-link" href="/geo/missing?limit={limit}&offset={previous_offset}">Forrige side</a>'
        if offset > 0
        else '<span class="nav-button disabled">Forrige side</span>'
    )
    next_link = (
        f'<a class="server-search-link" href="/geo/missing?limit={limit}&offset={next_offset}">Neste side</a>'
        if len(items) == limit
        else '<span class="nav-button disabled">Neste side</span>'
    )
    content = cards if cards else '<p class="meta">Ingen aktive bilder mangler GPS.</p>'
    return page_html(
        "Bilder uten GPS",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a> · <a href="/geo">Steder</a> · <a href="/geo/stats">Geo-statistikk</a></p>
          <h1>Bilder uten GPS</h1>
          <p class="meta">Viser {len(items)} bilder fra offset {offset}.</p>
          <nav class="controls">{previous_link}{next_link}</nav>
          <section class="month-grid-server">{content}</section>
        </main>
        """,
    )


def geo_stats_summary_html(stats: dict[str, int]) -> str:
    rows = "\n".join(
        f"<div><strong>{label}</strong><span>{stats[key]}</span></div>"
        for label, key in (
            ("Aktive bilder", "total"),
            ("Scannet", "scanned"),
            ("Med GPS", "with_gps"),
            ("Uten GPS", "without_gps"),
            ("Feil", "errors"),
        )
    )
    return f'<div class="geo-stats">{rows}</div>'


def geo_area_row_html(row: Any, *, resolution: int, inherited_name: str | None = None) -> str:
    h3_cell = str(row["h3_cell"])
    count = int(row["count"])
    name = row["name"] if "name" in row.keys() else None
    if name:
        label = str(name)
        detail = h3_cell
    elif inherited_name:
        label = f"{inherited_name} (arvet)"
        detail = h3_cell
    else:
        label = h3_cell
        detail = h3_resolution_label(resolution)
    url = "/geo/area/" + urllib.parse.quote(h3_cell, safe="")
    return f"""
    <a class="geo-row" href="{html.escape(url)}">
      <span>{html.escape(label)}</span>
      <span>{html.escape(detail)}</span>
      <strong>{count} bilder</strong>
    </a>
    """


def error_html(exc: Exception) -> str:
    return page_html(
        "Feil",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a> · <a href="/geo">Steder</a> · <a href="/settings">Innstillinger</a></p>
          <h1>Feil</h1>
          <p class="error">{html.escape(str(exc))}</p>
        </main>
        """,
    )


def search_form(query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> str:
    return f"""
    <form action="/search" method="get" class="search">
      <input name="q" value="{html.escape(query)}" placeholder="a photo of a beach" autofocus>
      <input name="limit" value="{limit}" inputmode="numeric" aria-label="Antall treff">
      <button type="submit">Søk</button>
    </form>
    """


def message_html(message: str) -> str:
    if not message:
        return ""
    return f'<p class="message">{html.escape(message)}</p>'


def markdown_doc_page_html(doc_path: Path, markdown: str) -> str:
    title = markdown_doc_title(markdown, doc_path)
    body = markdown_to_html(markdown)
    return page_html(
        title,
        f"""
        <main class="shell doc-page">
          <p><a href="/">Til bildebrowser</a></p>
          <article class="doc-content">
            {body}
          </article>
        </main>
        """,
    )


def markdown_doc_title(markdown: str, doc_path: Path) -> str:
    for line in markdown.splitlines():
        clean = line.strip()
        if clean.startswith("# "):
            return clean[2:].strip()
    return doc_path.stem.replace("-", " ").title()


def markdown_to_html(markdown: str) -> str:
    lines = strip_markdown_cli_help_markers(markdown).splitlines()
    html_lines: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    in_code = False
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            html_lines.append("<p>" + markdown_inline_html(" ".join(paragraph)) + "</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            html_lines.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    def flush_code() -> None:
        if code_lines:
            html_lines.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
            code_lines.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        if stripped.startswith("#"):
            flush_paragraph()
            flush_list()
            level = min(len(stripped) - len(stripped.lstrip("#")), 3)
            text = stripped[level:].strip()
            if text:
                html_lines.append(f"<h{level}>{markdown_inline_html(text)}</h{level}>")
            continue
        if stripped.startswith(("- ", "* ")):
            flush_paragraph()
            list_items.append(markdown_inline_html(stripped[2:].strip()))
            continue
        paragraph.append(stripped)

    flush_paragraph()
    flush_list()
    if in_code:
        flush_code()
    return "\n".join(html_lines)


def strip_markdown_cli_help_markers(markdown: str) -> str:
    lines: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped in {"<!-- CLI-HELP-START -->", "<!-- CLI-HELP-END -->"}:
            continue
        lines.append(line)
    return "\n".join(lines)


def markdown_inline_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", lambda match: f"<code>{match.group(1)}</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", lambda match: f"<strong>{match.group(1)}</strong>", escaped)
    return re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda match: markdown_link_html(match.group(1), match.group(2)),
        escaped,
    )


def markdown_link_html(label: str, url: str) -> str:
    if not safe_markdown_link(url):
        return html.escape(label)
    return f'<a href="{html.escape(url, quote=True)}">{label}</a>'


def safe_markdown_link(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in {"", "http", "https"} and not url.startswith("//")


def result_html(target: Path, result: ImageSearchResult) -> str:
    relative = relative_to_target(target, result.target_path)
    url = f"/file/{result.file_id}"
    path_text = str(relative).replace("\\", "/")
    return f"""
    <article class="item">
      <a href="{html.escape(url)}"><img src="{html.escape(url)}" alt=""></a>
      <div class="text">
        <div class="path">#{result.rank} {html.escape(path_text)}</div>
        <div class="score">score={result.similarity:.3f}</div>
      </div>
    </article>
    """


def empty_browser_html(*, openclip_enabled: bool = True) -> str:
    search_link = '<p><a href="/search">Bildesøk</a></p>' if openclip_enabled else ""
    return page_html(
        "Bildebrowser",
        f"""
        <main class="shell">
          <h1>Bildebrowser</h1>
          <p class="meta">Ingen filer i bildesamlingen.</p>
          {search_link}
        </main>
        """,
    )


def item_page_html(
    target: Path,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
    *,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    return source_item_page_html(
        target,
        all_browser_source(),
        item,
        previous_item,
        next_item,
        month_nav,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        face_config=face_config,
    )


def source_item_page_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
    *,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    target_path = Path(str(item["target_path"]))
    relative = display_relative_path(target, target_path)
    media = source_item_media_html(target, source, item, face_config)
    controls = source_controls_html(
        source,
        month_nav,
        previous_item,
        next_item,
        include_info_button=True,
        info_file_id=int(item["id"]),
        rotation_buttons=rotation_buttons_html(source, item),
        unconfirm_buttons=unconfirm_face_buttons_html(target, source, item, face_config) if face_enabled else "",
        delete_button=delete_button_html(source, item, previous_item, next_item),
    )
    people = people_links_html(confirmed_people_for_file(target, int(item["id"]), face_config)) if face_enabled else ""
    show_unconfirmed_faces = face_enabled and source.person_name is None
    unconfirmed_face_count = unconfirmed_face_count_for_item(target, int(item["id"]), face_config) if show_unconfirmed_faces else 0
    faces_button = faces_button_html(unconfirmed_face_count, int(item["id"])) if show_unconfirmed_faces else ""
    faces_overlay = faces_overlay_html(item) if unconfirmed_face_count > 0 else ""
    action_links = source_action_links_html(source, item, face_enabled=face_enabled, openclip_enabled=openclip_enabled)
    info_overlay = image_info_overlay_html()
    duplicate_warning = source_duplicate_confirmed_faces_warning_html(target, source, item, face_config) if face_enabled else ""
    return page_html(
        f"{source.title}: {target_path.name}",
        f"""
        <main class="server-browser">
          <header class="browser-header">
            <div class="topline">
              <div class="title">{html.escape(source.title)}</div>
              {people}
              {faces_button}
              {action_links}
            </div>
            {controls}
            {duplicate_warning}
          </header>
          <section class="stage">{media}</section>
          <footer class="browser-footer">
            <a class="filename" href="/file/{int(item["id"])}" target="_blank">{html.escape(relative)}</a>
          </footer>
        </main>
        {faces_overlay}
        {info_overlay}
        """,
    )


def source_duplicate_confirmed_faces_warning_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    if source.person_name is None or source.include_suggestions:
        return ""
    count = confirmed_person_face_count_for_item(target, source.person_name, int(item["id"]), face_config)
    if count < 2:
        return ""
    return (
        '<div class="warning">'
        f"NB: {count} bekreftede ansikter for {html.escape(source.person_name)} i dette bildet"
        "</div>"
    )


def confirmed_person_face_count_for_item(
    target: Path,
    person_name: str,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> int:
    person = person_by_name(target, person_name, face_config)
    if person is None:
        return 0
    conn = sqlite3.connect(current_face_db_path(target, face_config))
    try:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM person_faces
            JOIN faces ON faces.id = person_faces.face_id
            WHERE person_faces.person_id = ?
              AND faces.file_id = ?
            """,
            (int(person["id"]), file_id),
        ).fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def source_top_links_html(source: BrowserSource, item: Any | None = None, *, face_enabled: bool = True) -> str:
    links = [
        '<a class="server-search-link" href="/geo">Steder</a>',
        '<a class="server-search-link" href="/sources">Kilder</a>',
    ]
    if face_enabled:
        links.insert(0, '<a class="server-search-link" href="/people">Personer</a>')
    if source.date_source is not None or source.source_id is not None or source.geo_place_slug is not None:
        all_url = source_item_url(all_browser_source(), int(item["id"])) if item is not None else "/"
        links.insert(0, f'<a class="server-search-link" href="{html.escape(all_url)}">Alle bilder</a>')
    if source.person_name is not None and face_enabled:
        all_url = source_item_url(all_browser_source(), int(item["id"])) if item is not None else "/"
        links.insert(0, f'<a class="server-search-link" href="{html.escape(all_url)}">Alle bilder</a>')
        if source.show_faces:
            no_faces_source = person_browser_source(
                source.person_name,
                include_suggestions=source.include_suggestions,
                show_faces=False,
            )
            no_faces_url = source_item_url(no_faces_source, int(item["id"])) if item is not None else no_faces_source.root_url
            links.insert(
                1,
                f'<a class="server-search-link" href="{html.escape(no_faces_url)}">Uten ansiktsmarkering</a>',
            )
        else:
            faces_source = person_browser_source(
                source.person_name,
                include_suggestions=source.include_suggestions,
                show_faces=True,
            )
            faces_url = source_item_url(faces_source, int(item["id"])) if item is not None else faces_source.root_url
            links.insert(
                1,
                f'<a class="server-search-link" href="{html.escape(faces_url)}">Med ansiktsmarkering</a>',
            )
        if source.include_suggestions:
            links.insert(
                2,
                f'<a class="server-search-link" href="{html.escape(person_browser_source(source.person_name, include_suggestions=False).root_url)}">Bare bekreftede</a>',
            )
        else:
            links.insert(
                2,
                f'<a class="server-search-link" href="{html.escape(person_browser_source(source.person_name, include_suggestions=True).root_url)}">Med forslag</a>',
            )
    return "\n".join(links)


def source_action_links_html(
    source: BrowserSource,
    item: Any | None = None,
    *,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    search_link = '<a class="server-search-link" href="/search">Bildesøk</a>' if openclip_enabled else ""
    return f"""
    <div class="top-actions">
      {source_top_links_html(source, item, face_enabled=face_enabled)}
      {search_link}
      <a class="server-search-link" href="/help/web/bildebrowser">Hjelp</a>
      <a class="server-search-link" href="/settings">Innstillinger</a>
    </div>
    """


def app_status_page_html(target: Path, config: AppConfig | None = None) -> str:
    if config is None:
        config = AppConfig()
    insightface_installed = module_available("insightface")
    rows = "\n".join(
        (
            app_status_row_html("Bildesamling", str(target)),
            app_status_row_html("Bildebank-versjon", __version__),
            app_status_face_config_row_html(config.face_recognition.enabled, insightface_installed=insightface_installed),
            app_status_face_model_row_html(config.face_recognition),
            app_status_row_html("InsightFace installert", yes_no(insightface_installed)),
            app_status_row_html("OpenCLIP tilgjengelig", yes_no(module_available("open_clip"))),
            app_status_row_html("OpenCLIP aktivert", yes_no(config.openclip.enabled)),
            app_status_row_html("OpenCLIP-modell", config.openclip.model_name),
            app_status_row_html("OpenCLIP-pretrained", config.openclip.pretrained),
            app_status_row_html("OpenCLIP-device", config.openclip.device),
        )
    )
    return page_html(
        "Innstillinger",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a> · <a href="/settings/removed">Slettede bilder</a> · <a href="/date-source/filename">Dato fra filnavn</a> · <a href="/date-source/mtime">Dato fra mtime</a></p>
          <h1>Innstillinger</h1>
          <dl class="info-list app-status">
            {rows}
          </dl>
        </main>
        """,
    )


def removed_files_page_html(target: Path) -> str:
    conn = db.connect(target)
    try:
        rows = list(db.deleted_files(conn))
    finally:
        conn.close()
    items = "\n".join(removed_file_row_html(target, row) for row in rows)
    content = (
        f'<div class="removed-list">{items}</div>'
        if items
        else '<p class="meta">Ingen bilder er flyttet til deleted/.</p>'
    )
    return page_html(
        "Slettede bilder",
        f"""
        <main class="shell">
          <p><a href="/settings">Til innstillinger</a> · <a href="/">Til bildebrowser</a></p>
          <h1>Slettede bilder</h1>
          <p class="meta">{len(rows)} bilder flyttet til deleted/.</p>
          {content}
        </main>
        """,
    )


def removed_file_row_html(target: Path, row: Any) -> str:
    deleted_path = Path(str(row["target_path"]))
    original_path = row["deleted_original_target_path"] or row["target_path"]
    link = "/file/" + urllib.parse.quote(deleted_path.as_posix())
    exists = "finnes" if db.absolute_target_path(target, deleted_path).is_file() else "mangler"
    taken_date = str(row["taken_date"] or "ukjent dato")
    size = format_bytes(int(row["size_bytes"])) if row["size_bytes"] is not None else "ukjent størrelse"
    deleted_at = str(row["deleted_at"] or "")
    return f"""
    <div class="removed-row">
      <a href="{html.escape(link)}" target="_blank">{html.escape(str(original_path))}</a>
      <span>{html.escape(deleted_at)}</span>
      <span>{html.escape(taken_date)}</span>
      <span>{html.escape(size)}</span>
      <span>{exists}</span>
      <button class="nav-button" type="button" data-undelete-item="{int(row["id"])}" data-undelete-path="{html.escape(str(original_path))}">Undelete</button>
    </div>
    """


def app_status_row_html(label: str, value: str) -> str:
    return f"""
    <div class="info-row">
      <dt>{html.escape(label)}</dt>
      <dd>{html.escape(value)}</dd>
    </div>
    """


def app_status_face_config_row_html(enabled: bool, *, insightface_installed: bool = True) -> str:
    checked = " checked" if enabled else ""
    status = "På" if enabled else "Av"
    install_note = (
        ""
        if insightface_installed
        else '<span class="app-toggle-note"><a href="/help/insightface">InsightFace</a> må installeres for å scanne ansikter i nye bilder.</span>'
    )
    return f"""
    <div class="info-row">
      <dt>InsightFace aktivert</dt>
      <dd>
        <form action="/settings/face-config" method="post" class="app-toggle-form">
          <input type="hidden" name="enabled" value="false">
          <label class="app-toggle">
            <input type="checkbox" name="enabled" value="true"{checked} onchange="this.form.submit()">
            <span class="app-toggle-track" aria-hidden="true"><span></span></span>
            <span class="app-toggle-status">{status}</span>
          </label>
          {install_note}
        </form>
      </dd>
    </div>
    """


def app_status_face_model_row_html(config: FaceRecognitionConfig) -> str:
    installed_models = installed_insightface_models(config)
    if not installed_models:
        return app_status_row_html("InsightFace-modell", f"{config.model_name} (ingen installerte modeller funnet)")
    options = "\n".join(
        f'<option value="{html.escape(model)}"{selected_attr(model == config.model_name)}>{html.escape(model)}</option>'
        for model in installed_models
    )
    note = (
        ""
        if config.model_name in installed_models
        else f'<span class="app-toggle-note">Aktiv config er {html.escape(config.model_name)}, men modellen finnes ikke i modellmappen.</span>'
    )
    return f"""
    <div class="info-row">
      <dt>InsightFace-modell</dt>
      <dd>
        <form action="/settings/face-model" method="post" class="app-toggle-form">
          <select name="model_name" onchange="this.form.submit()">
            {options}
          </select>
          {note}
        </form>
      </dd>
    </div>
    """


def selected_attr(selected: bool) -> str:
    return " selected" if selected else ""


def installed_insightface_models(config: FaceRecognitionConfig) -> list[str]:
    models_dir = config.model_root / "models"
    try:
        children = list(models_dir.iterdir())
    except OSError:
        return []
    models: list[str] = []
    for child in children:
        if not child.is_dir():
            continue
        if list(child.glob("*.onnx")) or list((child / child.name).glob("*.onnx")):
            models.append(child.name)
    return sorted(models, key=str.lower)


def yes_no(value: bool) -> str:
    return "ja" if value else "nei"


def module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def server_program_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def source_item_media_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    if source.person_name is not None:
        if not source.show_faces:
            return item_media_html(item)
        faces = person_faces_for_item(
            target,
            source.person_name,
            item,
            include_suggestions=source.include_suggestions,
            face_config=face_config,
        )
        return person_item_media_html(item, faces)
    return item_media_html(item)


def item_media_html(item: Any) -> str:
    file_id = int(item["id"])
    target_path = Path(str(item["target_path"]))
    url = f"/file/{file_id}"
    name = html.escape(str(item["stored_filename"]))
    if target_path.suffix.lower().lstrip(".") in {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}:
        return f'<video src="{url}" controls></video>'
    return f'<a href="{url}" target="_blank"><img src="{url}" alt="{name}"{rotation_style_attr(item)}></a>'


def person_item_page_html(
    target: Path,
    person_name: str,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
) -> str:
    return source_item_page_html(
        target,
        person_browser_source(person_name, include_suggestions=True),
        item,
        previous_item,
        next_item,
        month_nav,
    )


def person_item_media_html(item: Any, faces: list[dict[str, object]]) -> str:
    file_id = int(item["id"])
    target_path = Path(str(item["target_path"]))
    url = f"/file/{file_id}"
    name = html.escape(str(item["stored_filename"]))
    if target_path.suffix.lower().lstrip(".") in {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}:
        return f'<video src="{url}" controls></video>'
    boxes = "\n".join(person_face_box_html(face) for face in faces)
    return f"""
    <div class="person-media"{rotation_style_attr(item)}>
      <a href="{url}" target="_blank"><img src="{url}" alt="{name}"></a>
      {boxes}
    </div>
    """


def person_face_box_html(face: dict[str, object]) -> str:
    if not {"left", "top", "boxWidth", "boxHeight"} <= face.keys():
        return ""
    css_class = "person-face-box suggested" if face.get("status") == "forslag" else "person-face-box"
    title = f'{face.get("status", "")} face-id {face["faceId"]} score {float(face.get("similarity", 0.0)):.3f}'
    label = f'face-id {face["faceId"]}'
    return (
        f'<div class="{css_class}" title="{html.escape(title)}" style="'
        f'left: {float(face["left"]):.4f}%; '
        f'top: {float(face["top"]):.4f}%; '
        f'width: {float(face["boxWidth"]):.4f}%; '
        f'height: {float(face["boxHeight"]):.4f}%;'
        f'"><span class="person-face-label">{html.escape(label)}</span></div>'
    )


def nav_link(item: Any | None, label: str, key_nav: str) -> str:
    if item is None:
        return nav_disabled(label)
    return nav_button(f"/item/{int(item['id'])}", label, key_nav)


def month_nav_link(month_key: str | None, label: str, key_nav: str) -> str:
    if month_key is None:
        return nav_disabled(label)
    return nav_button(f"/month/{html.escape(month_key)}", label, key_nav)


def nav_button(href: str, label: str, key_nav: str) -> str:
    return f'<a class="nav-button" href="{href}" data-key-nav="{html.escape(key_nav)}">{html.escape(label)}</a>'


def nav_disabled(label: str) -> str:
    return f'<span class="nav-button disabled">{html.escape(label)}</span>'


def browser_controls_html(
    month_nav: dict[str, str | None],
    previous_item: Any | None,
    next_item: Any | None,
) -> str:
    return source_controls_html(all_browser_source(), month_nav, previous_item, next_item)


def source_controls_html(
    source: BrowserSource,
    month_nav: dict[str, str | None],
    previous_item: Any | None,
    next_item: Any | None,
    *,
    include_info_button: bool = False,
    info_file_id: int | None = None,
    rotation_buttons: str = "",
    unconfirm_buttons: str = "",
    delete_button: str = "",
) -> str:
    info_button = image_info_button_html(info_file_id) if include_info_button else ""
    return f"""
    <nav class="controls" aria-label="Navigering">
      {source_month_nav_link(source, month_nav["previous_year"], "Forrige år", "previous-year")}
      {source_month_nav_link(source, month_nav["next_year"], "Neste år", "next-year")}
      {source_month_nav_link(source, month_nav["previous_month"], "Forrige måned", "previous-month")}
      {source_month_nav_link(source, month_nav["next_month"], "Neste måned", "next-month")}
      {source_nav_link(source, previous_item, "Forrige bilde", "previous")}
      {source_nav_link(source, next_item, "Neste bilde", "next")}
      {rotation_buttons}
      {info_button}
      {unconfirm_buttons}
      {delete_button}
    </nav>
    """


def unconfirm_face_buttons_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    if source.person_name is None or source.include_suggestions:
        return ""
    faces = person_faces_for_item(
        target,
        source.person_name,
        item,
        include_suggestions=False,
        face_config=face_config,
    )
    buttons = []
    for face in faces:
        face_id = int(face["faceId"])
        person_name = source.person_name
        buttons.append(
            '<button class="nav-button danger-button" type="button" '
            f'data-unconfirm-face="{face_id}" '
            f'data-unconfirm-person="{html.escape(person_name)}">'
            f"Avbekreft face-id {face_id}"
            "</button>"
        )
    return "\n".join(buttons)


def person_controls_html(
    person_name: str,
    month_nav: dict[str, str | None],
    previous_item: Any | None,
    next_item: Any | None,
) -> str:
    return f"""
    <nav class="controls" aria-label="Navigering">
      {person_month_nav_link(person_name, month_nav["previous_year"], "Forrige år", "previous-year")}
      {person_month_nav_link(person_name, month_nav["next_year"], "Neste år", "next-year")}
      {person_month_nav_link(person_name, month_nav["previous_month"], "Forrige måned", "previous-month")}
      {person_month_nav_link(person_name, month_nav["next_month"], "Neste måned", "next-month")}
      {person_nav_link(person_name, previous_item, "Forrige bilde", "previous")}
      {person_nav_link(person_name, next_item, "Neste bilde", "next")}
    </nav>
    """


def person_nav_link(person_name: str, item: Any | None, label: str, key_nav: str) -> str:
    if item is None:
        return nav_disabled(label)
    return nav_button(person_item_url(person_name, int(item["id"])), label, key_nav)


def person_month_nav_link(person_name: str, month_key: str | None, label: str, key_nav: str) -> str:
    if month_key is None:
        return nav_disabled(label)
    return nav_button(person_month_url(person_name, month_key), label, key_nav)


def source_nav_link(source: BrowserSource, item: Any | None, label: str, key_nav: str) -> str:
    if item is None:
        return nav_disabled(label)
    return nav_button(source_item_url(source, int(item["id"])), label, key_nav)


def source_month_nav_link(source: BrowserSource, month_key: str | None, label: str, key_nav: str) -> str:
    if month_key is None:
        return nav_disabled(label)
    return nav_button(source_month_url(source, month_key), label, key_nav)


def rotation_buttons_html(source: BrowserSource, item: Any) -> str:
    if not is_image_item(item):
        return ""
    file_id = int(item["id"])
    return f"""
      <button class="nav-button" type="button" data-rotate-item="{file_id}" data-rotate-direction="left">Roter venstre</button>
      <button class="nav-button" type="button" data-rotate-item="{file_id}" data-rotate-direction="right">Roter høyre</button>
    """


def delete_button_html(source: BrowserSource, item: Any, previous_item: Any | None, next_item: Any | None) -> str:
    redirect_url = source_item_url(source, int(next_item["id"])) if next_item is not None else ""
    if not redirect_url and previous_item is not None:
        redirect_url = source_item_url(source, int(previous_item["id"]))
    if not redirect_url:
        redirect_url = source.root_url
    relative = display_relative_path(Path("."), Path(str(item["target_path"])))
    return (
        f'<button class="nav-button danger-button" type="button" '
        f'data-delete-item="{int(item["id"])}" '
        f'data-delete-path="{html.escape(relative)}" '
        f'data-delete-redirect="{html.escape(redirect_url)}">Slett</button>'
    )


def is_image_item(item: Any) -> bool:
    target_path = Path(str(item["target_path"]))
    return target_path.suffix.lower().lstrip(".") not in {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}


def item_view_rotation(item: Any) -> int:
    try:
        return db.normalize_view_rotation(item["view_rotation_degrees"])
    except (KeyError, IndexError):
        return 0


def rotation_style_attr(item: Any) -> str:
    rotation = item_view_rotation(item)
    if rotation == 0:
        return ""
    return f' style="transform: rotate({rotation}deg);" data-view-rotation="{rotation}"'


def image_info_button_html(file_id: int | None) -> str:
    file_attr = f' data-info-item="{file_id}"' if file_id is not None else ""
    return f'<button class="nav-button" type="button" data-open-info{file_attr}>Bildeinfo</button>'


def people_links_html(people: list[dict[str, object]]) -> str:
    if not people:
        return ""
    links = "\n".join(people_link_html(person) for person in people)
    return f'<div class="people">{links}</div>'


def people_link_html(person: dict[str, object]) -> str:
    name = str(person["name"])
    badge = '<span class="confirmed-badge" title="Bekreftet" aria-label="Bekreftet"> ✅</span>' if person.get("confirmed") else ""
    return (
        f'<a class="person-link" href="{html.escape(str(person["url"]))}" '
        f'data-person-name="{html.escape(name)}">{html.escape(name)}{badge}</a>'
    )


def faces_button_html(face_count: int, file_id: int) -> str:
    if face_count <= 0:
        return ""
    return f'<button class="faces-button" type="button" data-open-faces data-faces-item="{file_id}">Ubekreftet ansikter i bildet ({face_count})</button>'


def faces_overlay_html(item: Any) -> str:
    target_path = Path(str(item["target_path"]))
    return f"""
    <div id="faceOverlay" class="face-overlay" hidden>
      <div class="lightbox-bar">
        <div class="lightbox-title">Ansikter - {html.escape(target_path.name)}</div>
        <button class="lightbox-close" type="button" data-close-faces>Lukk</button>
      </div>
      <div class="lightbox-stage">
        <div class="face-list" data-face-list></div>
      </div>
    </div>
    """


def face_overlay_content_html(
    target: Path,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    faces = unconfirmed_faces_for_item(target, item, face_config)
    if not faces:
        return '<p class="empty">Ingen ubekreftede ansikter i bildet.</p>'
    people = registered_people(target, face_config)
    image_url = f"/file/{int(item['id'])}"
    return "\n".join(face_overlay_item_html(item, image_url, face, people) for face in faces)


def image_info_overlay_html() -> str:
    return f"""
    <div id="infoOverlay" class="info-overlay" hidden>
      <div class="lightbox-bar">
        <div class="lightbox-title">Bildeinfo</div>
        <button class="lightbox-close" type="button" data-close-info>Lukk</button>
      </div>
      <div class="info-panel">
        <h2>Bildeinfo</h2>
        <dl class="info-list" data-info-list></dl>
      </div>
    </div>
    """


def image_info_content_html(target: Path, item: Any) -> str:
    return "\n".join(image_info_rows(target, item))


def image_info_rows(target: Path, item: Any) -> list[str]:
    target_path = Path(str(item["target_path"]))
    absolute_path = db.absolute_target_path(target, target_path)
    dimensions = cached_image_dimensions(target, absolute_path)
    camera = camera_info(absolute_path)
    rows = [
        info_row_html("Filnavn", display_relative_path(target, target_path)),
        info_row_html("Dato", image_date_text(item)),
        info_row_html("Filstørrelse", f"{format_bytes(int(item['size_bytes']))} ({int(item['size_bytes'])} bytes)"),
        info_row_html("Oppløsning", f"{dimensions.width} x {dimensions.height}" if dimensions else "-"),
        info_row_html("Kamera", camera_text(camera)),
    ]
    sources = image_source_rows(target, target_path)
    if sources:
        rows.append(info_row_html("Kilder", "\n\n".join(sources), multiline=True))
    else:
        rows.append(info_row_html("Kilder", "-"))
    maps_link = google_maps_link_html(item)
    if maps_link:
        rows.append(info_row_html("Kart", maps_link, raw_html=True))
    geo_links = image_geo_area_links_html(target, item)
    if geo_links:
        rows.append(info_row_html("Steder", geo_links, raw_html=True))
    return rows


def image_date_text(item: Any) -> str:
    taken_date = str(item["taken_date"] or "-")
    source = str(item["date_source"] or "")
    return f"{taken_date} ({date_source_text(source)})"


def date_source_text(source: str) -> str:
    labels = {
        "metadata": "fra metadata",
        "filename": "fra filnavn",
        "mtime": "fra mtime",
        "unknown": "ukjent datokilde",
    }
    return labels.get(source, source or "ukjent datokilde")


def google_maps_link_html(item: Any) -> str:
    lat = item["gps_lat"]
    lon = item["gps_lon"]
    if lat is None or lon is None:
        return ""
    latitude = float(lat)
    longitude = float(lon)
    query = urllib.parse.quote(f"{latitude:.7f},{longitude:.7f}", safe=",")
    url = f"https://www.google.com/maps/search/?api=1&query={query}"
    label = f"Åpne i Google Maps ({latitude:.7f}, {longitude:.7f})"
    return f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(label)}</a>'


def camera_text(camera: Any | None) -> str:
    if camera is None:
        return "-"
    parts = [part for part in (camera.make, camera.model) if part]
    return " ".join(parts) if parts else "-"


def image_source_rows(target: Path, target_path: Path) -> list[str]:
    conn = db.connect(target)
    try:
        rows = db.file_sources_by_target_path(conn, target, db.absolute_target_path(target, target_path))
    finally:
        conn.close()
    result = []
    for row in rows:
        source_name = str(row["source_name"] or row["source_root"] or f"Kilde #{row['source_id']}")
        result.append(f"{source_name}: {row['source_path']}")
    return result


def image_geo_area_links_html(target: Path, item: Any) -> str:
    conn = db.connect(target)
    try:
        links = []
        for resolution, column in H3_COLUMNS.items():
            h3_cell = item[column]
            if not h3_cell:
                continue
            place_name = db.geo_place_name(conn, str(h3_cell))
            label = f"H3-{resolution}: {h3_cell} ({h3_area_label(resolution)})"
            if place_name:
                label += f" {place_name}"
            url = "/geo/area/" + urllib.parse.quote(str(h3_cell), safe="")
            links.append(f'<a href="{html.escape(url)}">{html.escape(label)}</a>')
        return "<br>".join(links)
    finally:
        conn.close()

def info_row_html(label: str, value: str, *, multiline: bool = False, raw_html: bool = False) -> str:
    escaped_value = value if raw_html else html.escape(value)
    if multiline and not raw_html:
        escaped_value = "<br>".join(escaped_value.splitlines())
    return f"""
    <div class="info-row">
      <dt>{html.escape(label)}</dt>
      <dd>{escaped_value}</dd>
    </div>
    """


def face_overlay_item_html(item: Any, image_url: str, face: dict[str, object], people: list[dict[str, str]]) -> str:
    face_id = int(face["faceId"])
    people_buttons = person_assignment_buttons_html(face_id, people)
    box = ""
    if {"left", "top", "boxWidth", "boxHeight"} <= face.keys():
        box = (
            '<div class="face-box" style="'
            f'left: {float(face["left"]):.4f}%; '
            f'top: {float(face["top"]):.4f}%; '
            f'width: {float(face["boxWidth"]):.4f}%; '
            f'height: {float(face["boxHeight"]):.4f}%;'
            '"></div>'
        )
    return f"""
    <section class="face-detail" data-face-detail="{face_id}">
      <div class="face-detail-title">face-id {face_id}, deteksjon {float(face["score"]):.3f}</div>
      <div class="lightbox-media"{rotation_style_attr(item)}>
        <img src="{html.escape(image_url)}" alt="">
        {box}
      </div>
      <div class="assign-row">{people_buttons}</div>
      <form class="new-person-form" data-new-person-form>
        <input type="hidden" name="face_id" value="{face_id}">
        <label for="new-person-{face_id}">Ny person</label>
        <input id="new-person-{face_id}" name="person_name" autocomplete="off">
        <button type="submit">Identifiser</button>
      </form>
      <div class="assign-status" aria-live="polite"></div>
    </section>
    """


def person_assignment_buttons_html(face_id: int, people: list[dict[str, str]]) -> str:
    if not people:
        return '<p class="empty">Ingen personer registrert.</p>'
    return "\n".join(
        (
            f'<button class="assign-person-button" type="button" '
            f'data-face-id="{face_id}" data-person-name="{html.escape(person["name"])}">'
            f'{html.escape(person["name"])}</button>'
        )
        for person in people
    )


def month_page_html(target: Path, month_key: str, items: list[Any]) -> str:
    return source_month_page_html(target, all_browser_source(), month_key, items)


def source_month_page_html(
    target: Path,
    source: BrowserSource,
    month_key: str,
    items: list[Any],
    *,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    cards = "\n".join(source_month_item_html(target, source, item) for item in items)
    previous_item = items[-1] if items else None
    next_item = items[0] if items else None
    controls = source_controls_html(
        source,
        source_month_navigation_for_key(target, source, month_key, face_config),
        previous_item,
        next_item,
    )
    action_links = source_action_links_html(source, face_enabled=face_enabled, openclip_enabled=openclip_enabled)
    return page_html(
        f"{source.title}: {month_key}",
        f"""
        <main class="server-browser">
          <header class="browser-header">
            <div class="topline">
              <div class="title">{html.escape(source.title)}</div>
              <span class="status">Månedsoversikt: {html.escape(month_key)}</span>
              {action_links}
            </div>
            {controls}
          </header>
          <section class="month-grid-server">{cards}</section>
          <footer class="browser-footer">
            <span class="filename">Månedsoversikt: {html.escape(month_key)}</span>
          </footer>
        </main>
        """,
    )


def empty_person_browser_html(person: str | BrowserSource, *, openclip_enabled: bool = True) -> str:
    source = person if isinstance(person, BrowserSource) else person_browser_source(person, include_suggestions=True)
    return empty_source_html(source, openclip_enabled=openclip_enabled)


def empty_source_html(source: BrowserSource, *, face_enabled: bool = True, openclip_enabled: bool = True) -> str:
    links = ['<a href="/">Til bildebrowser</a>']
    if face_enabled:
        links.append('<a href="/people">Personer</a>')
    if openclip_enabled:
        links.append('<a href="/search">Bildesøk</a>')
    return page_html(
        source.title,
        f"""
        <main class="shell">
          <p>{" · ".join(links)}</p>
          <h1>{html.escape(source.title)}</h1>
          <p class="meta">{html.escape(empty_source_message(source))}</p>
        </main>
        """,
    )


def empty_source_message(source: BrowserSource) -> str:
    if source.person_name is None:
        if source.date_source == "filename":
            return "Ingen bilder med dato fra filnavn."
        if source.date_source == "mtime":
            return "Ingen bilder med dato fra mtime."
        if source.source_id is not None:
            return "Ingen aktive bilder for denne kilden."
        if source.geo_place_slug is not None:
            return "Ingen aktive bilder for dette stedet."
        return "Ingen filer i bildesamlingen."
    if source.include_suggestions:
        return "Ingen bekreftede ansikter eller forslag for denne personen ennå."
    return "Ingen bekreftede bilder for denne personen ennå."


def imported_source_by_id(target: Path, source_id: int) -> db.Source | None:
    conn = db.connect(target)
    try:
        try:
            return db.get_source(conn, source_id)
        except ValueError:
            return None
    finally:
        conn.close()


def source_summary_rows(target: Path) -> list[sqlite3.Row]:
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                """
                SELECT
                    sources.id,
                    sources.name,
                    sources.path,
                    sources.imported_at,
                    sources.status,
                    sources.superseded_by_source_id,
                    COUNT(file_sources.id) AS source_file_count,
                    COUNT(CASE WHEN files.deleted_at IS NULL THEN 1 END) AS active_file_count
                FROM sources
                LEFT JOIN file_sources ON file_sources.source_id = sources.id
                LEFT JOIN files ON files.id = file_sources.file_id
                GROUP BY sources.id
                ORDER BY sources.imported_at IS NULL, sources.imported_at, sources.id
                """
            )
        )
    finally:
        conn.close()


def sources_page_html(target: Path) -> str:
    sources = source_summary_rows(target)
    rows = "\n".join(source_row_html(source) for source in sources)
    content = (
        f'<div class="people-table">{rows}</div>'
        if rows
        else '<p class="meta">Ingen importerte kilder registrert.</p>'
    )
    return page_html(
        "Kilder",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a></p>
          <h1>Kilder</h1>
          {content}
        </main>
        """,
    )


def source_row_html(source: sqlite3.Row) -> str:
    source_id = int(source["id"])
    name = str(source["name"])
    status = str(source["status"])
    active_file_count = int(source["active_file_count"])
    source_file_count = int(source["source_file_count"])
    imported_at = str(source["imported_at"] or "-")
    superseded_by = source["superseded_by_source_id"]
    superseded = f", erstattet av #{int(superseded_by)}" if superseded_by is not None else ""
    source_browser = imported_source_browser_source(source)
    return f"""
    <div class="people-row">
      <div class="people-name">{html.escape(name)}</div>
      <a class="person-link" href="{html.escape(source_browser.root_url)}">Vis bilder ({active_file_count})</a>
      <span class="status">filer fra kilde: {source_file_count}</span>
      <span class="status">status: {html.escape(status)}{html.escape(superseded)}</span>
      <span class="status">importert: {html.escape(imported_at)}</span>
      <div class="detail">{html.escape(str(source["path"]))}</div>
    </div>
    """


def person_not_found_html(person_name: str) -> str:
    return page_html(
        "Fant ikke person",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a></p>
          <h1>Fant ikke person</h1>
          <p class="error">{html.escape(person_name)}</p>
        </main>
        """,
    )


def people_page_html(target: Path, face_config: FaceRecognitionConfig | None = None) -> str:
    people = registered_people_rows(target, face_config)
    rows = "\n".join(people_row_html(person) for person in people)
    content = (
        f'<div class="people-table">{rows}</div>'
        if rows
        else '<p class="meta">Ingen personer registrert.</p>'
    )
    return page_html(
        "Personer",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a></p>
          <h1>Personer</h1>
          {content}
        </main>
        {person_rename_dialog_html()}
        """,
    )


def people_row_html(person: dict[str, object]) -> str:
    name = str(person["name"])
    confirmed_count = int(person["confirmed_file_count"])
    all_count = int(person["all_file_count"])
    suggestion_count = int(person["suggestion_count"])
    duplicate_count = int(person["duplicate_confirmed_file_count"])
    max_confirmed_faces = int(person["max_confirmed_faces_per_file"])
    confirmed_source = person_browser_source(name, include_suggestions=False, show_faces=False)
    all_source = person_browser_source(name, include_suggestions=True, show_faces=False)
    duplicate_warning = ""
    if duplicate_count > 0:
        duplicate_warning = (
            '<span class="warning people-warning">'
            f"NB: {max_confirmed_faces} bekreftede ansikter i samme bilde"
            "</span>"
        )
    return f"""
    <div class="people-row">
      <div class="people-name">
        <span>{html.escape(name)}</span>
        <button class="rename-person-link" type="button" data-open-person-rename data-person-name="{html.escape(name)}">endre navn</button>
        <button class="rename-person-link delete-person-link" type="button" data-delete-person-name="{html.escape(name)}">slett person</button>
      </div>
      {duplicate_warning}
      <a class="person-link" href="{html.escape(confirmed_source.root_url)}">Bekreftede bilder ({confirmed_count})</a>
      <a class="person-link" href="{html.escape(all_source.root_url)}">Bekreftede og forslag ({all_count})</a>
      <span class="status">forslag: {suggestion_count}</span>
    </div>
    """


def person_rename_dialog_html() -> str:
    return """
    <div id="personRenameDialog" class="modal-overlay" hidden>
      <form class="modal-panel person-rename-form" data-person-rename-form>
        <h2>Endre navn</h2>
        <input type="hidden" name="old_name">
        <label for="personRenameName">Nytt navn</label>
        <input id="personRenameName" type="text" name="new_name" autocomplete="off" required>
        <p class="assign-status" data-person-rename-status></p>
        <div class="modal-actions">
          <button class="nav-button" type="submit">Lagre</button>
          <button class="nav-button" type="button" data-close-person-rename>Avbryt</button>
        </div>
      </form>
    </div>
    """


def person_month_page_html(target: Path, person_name: str, month_key: str, items: list[Any]) -> str:
    return source_month_page_html(target, person_browser_source(person_name, include_suggestions=True), month_key, items)


def person_month_item_html(target: Path, person_name: str, item: Any) -> str:
    return source_month_item_html(target, person_browser_source(person_name, include_suggestions=True), item)


def source_month_item_html(target: Path, source: BrowserSource, item: Any) -> str:
    target_path = Path(str(item["target_path"]))
    label = html.escape(display_relative_path(target, target_path))
    media = thumbnail_media_html(target, item)
    return f"""
    <article class="item">
      <a class="thumb-link" href="{source_item_url(source, int(item["id"]))}">{media}</a>
      <div class="text">
        <div class="path">{label}</div>
        <div class="score">{html.escape(format_bytes(int(item["size_bytes"])))}</div>
      </div>
    </article>
    """


def month_item_html(target: Path, item: Any) -> str:
    return source_month_item_html(target, all_browser_source(), item)


def thumbnail_media_html(target: Path, item: Any) -> str:
    file_id = int(item["id"])
    target_path = Path(str(item["target_path"]))
    url = f"/file/{file_id}"
    name = html.escape(str(item["stored_filename"]))
    if target_path.suffix.lower().lstrip(".") in {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}:
        return f'<div class="video-thumb">Video<br>{name}</div>'
    relative_path = db.target_relative_path(target, target_path)
    thumbnail_src = "/file/" + existing_thumbnail_url(target, relative_path)
    return f'<img src="{html.escape(thumbnail_src)}" alt="{name}" loading="lazy"{rotation_style_attr(item)}>'


SERVER_ASSET_VERSION = "3"
SERVER_CSS = r"""    :root {
      color-scheme: dark;
      --bg: #171717;
      --panel: #242424;
      --stage: #0e0e0e;
      --border: #3a3a3a;
      --text: #f2f2f2;
      --muted: #b8b8b8;
      --accent: #7db7ff;
      --danger: #ff8a80;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .shell { max-width: 1200px; margin: 0 auto; padding: 24px; }
    h1 { margin: 0 0 8px; font-size: 28px; }
    .meta { color: var(--muted); margin: 0 0 18px; }
    .search { display: grid; grid-template-columns: minmax(0, 1fr) 90px auto; gap: 8px; margin: 18px 0; }
    input, select, button {
      font: inherit;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #303030;
      color: var(--text);
    }
    button { cursor: pointer; }
    button:hover { background: #3a3a3a; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }
    .geo-filter { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin: 18px 0; }
    .geo-filter label { display: grid; gap: 4px; color: var(--muted); font-size: 13px; }
    .geo-filter input { width: 120px; }
    .geo-filter textarea, .custom-place-form textarea {
      width: min(520px, 78vw);
      min-height: 96px;
      resize: vertical;
      font: 13px ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #303030;
      color: var(--text);
    }
    .geo-name-form input[name="name"] { width: min(420px, 70vw); }
    .custom-geo-places { margin-top: 28px; }
    .custom-place-form {
      display: grid;
      grid-template-columns: minmax(240px, 360px) minmax(320px, 1fr) auto;
      gap: 12px;
      align-items: stretch;
      margin: 18px 0;
    }
    .custom-place-form label,
    .custom-place-identity {
      display: grid;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }
    .custom-place-form input, .custom-place-form textarea { width: 100%; }
    .custom-place-actions {
      display: grid;
      gap: 8px;
      align-content: end;
    }
    .custom-place-actions button { min-height: 40px; white-space: nowrap; }
    .custom-place-list { display: grid; gap: 10px; margin-top: 12px; }
    .custom-place-edit {
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
      overflow: hidden;
    }
    .custom-place-edit summary {
      display: grid;
      grid-template-columns: minmax(180px, 1fr) minmax(120px, auto) auto;
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
      cursor: pointer;
      list-style: none;
    }
    .custom-place-edit summary::-webkit-details-marker { display: none; }
    .custom-place-edit summary:hover { background: #2b2b2b; }
    .custom-place-name { font-weight: 700; }
    .custom-place-edit-body {
      border-top: 1px solid var(--border);
      padding: 12px 14px 14px;
    }
    .custom-place-edit .custom-place-form { margin: 0; }
    @media (max-width: 900px) {
      .custom-place-form,
      .custom-place-edit summary {
        grid-template-columns: 1fr;
      }
    }
    .doc-page { max-width: 860px; }
    .doc-content { line-height: 1.6; }
    .doc-content h1, .doc-content h2, .doc-content h3 { margin: 1.2em 0 0.45em; }
    .doc-content p, .doc-content ul, .doc-content pre { margin: 0 0 1em; }
    .doc-content code {
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      background: #303030;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 1px 4px;
    }
    .doc-content pre {
      overflow: auto;
      background: #101010;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 12px;
    }
    .doc-content pre code { background: transparent; border: 0; padding: 0; }
    .geo-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin: 18px 0; }
    .geo-stats div { display: grid; gap: 3px; padding: 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }
    .geo-stats span { color: var(--muted); }
    .geo-list { display: grid; gap: 8px; margin-top: 18px; }
    .geo-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 12px; align-items: center; padding: 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); color: var(--text); }
    .geo-map-wrap { width: 100%; overflow: auto; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }
    .geo-map { display: block; min-width: 760px; width: 100%; height: auto; }
    .geo-hex { fill: #2f6f73; stroke: #8fd8dd; stroke-width: 2; }
    .geo-hex-link:hover .geo-hex { fill: #3f858a; }
    .geo-hex-count { fill: var(--text); font-size: 13px; font-weight: 700; pointer-events: none; }
    .server-browser { min-height: 100vh; display: grid; grid-template-rows: auto minmax(0, 1fr) auto; }
    .browser-header {
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 8px 10px;
      display: grid;
      gap: 7px;
    }
    .topline, .controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .title { font-weight: 700; margin-right: 8px; line-height: 1.2; }
    .status { color: var(--muted); font-size: 13px; line-height: 1.2; }
    .warning { color: #ffd166; font-size: 13px; line-height: 1.2; font-weight: 700; }
    .people { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    .top-actions {
      margin-left: auto;
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .browser-header .topline {
       padding-bottom: 0px;
       padding-top: 0px;
    }
    .top-actions .server-search-link {
      border: 0;
      border-radius: 0;
      padding: 0;
      background: transparent;
      min-height: 0;
      color: var(--text);
    }
    .top-actions .server-search-link:hover {
      background: transparent;
      text-decoration: underline;
    }
    .people-table { display: grid; gap: 8px; margin-top: 18px; }
    .removed-list { display: grid; gap: 6px; margin-top: 18px; }
    .removed-row {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto auto auto auto auto;
      gap: 10px;
      align-items: center;
      padding: 8px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
      font-size: 14px;
    }
    .removed-row span { color: var(--muted); }
    .people-row {
      display: grid;
      grid-template-columns: minmax(160px, 1fr) auto auto auto auto;
      gap: 8px;
      align-items: center;
      padding: 8px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
    }
    .people-name { font-weight: 700; overflow-wrap: anywhere; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
    .rename-person-link {
      border: 0;
      padding: 0;
      background: transparent;
      color: var(--muted);
      font: inherit;
      font-size: 13px;
      font-weight: 400;
      cursor: pointer;
    }
    .rename-person-link:hover { color: var(--text); text-decoration: underline; }
    .people-warning { justify-self: start; }
    a, .disabled { color: var(--accent); }
    a { text-decoration: none; }
    a:hover { text-decoration: underline; }
    .nav-button, .server-search-link, .person-link, .faces-button {
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 7px;
      background: #303030;
      color: var(--text);
      min-height: 32px;
      display: inline-flex;
      align-items: center;
    }
    .person-link { color: var(--accent); }
    .confirmed-badge {
      margin-left: 6px;
      font-size: 11px;
      font-weight: 700;
      line-height: 1;
      color: var(--ok);
    }
    .faces-button { color: var(--accent); }
    .nav-button:hover, .server-search-link:hover, .person-link:hover, .faces-button:hover { background: #3a3a3a; text-decoration: none; }
    .danger-button { color: var(--danger); }
    .danger-button:hover { background: rgb(255 138 128 / 12%); }
    .disabled { color: #777; cursor: default; }
    .stage {
      min-height: 0;
      display: grid;
      place-items: center;
      background: var(--stage);
      border-top: 1px solid var(--border);
      overflow: hidden;
      padding: 14px;
    }
    .stage img, .stage video {
      max-width: min(100%, 92vw);
      max-height: calc(100vh - 10rem);
      object-fit: contain;
      display: block;
      transform-origin: center center;
    }
    .person-media {
      position: relative;
      display: inline-block;
      max-width: min(100%, 92vw);
      max-height: calc(100vh - 10rem);
      transform-origin: center center;
    }
    .person-media img {
      max-width: 100%;
      max-height: calc(100vh - 10rem);
      object-fit: contain;
      display: block;
    }
    .person-face-box {
      position: absolute;
      border: 2px solid #2fbf71;
      background: rgb(47 191 113 / 13%);
      pointer-events: none;
    }
    .person-face-label {
      position: absolute;
      left: -2px;
      top: -24px;
      padding: 3px 6px;
      border-radius: 4px;
      background: rgb(0 0 0 / 78%);
      color: #fff;
      font-size: 12px;
      line-height: 1;
      white-space: nowrap;
    }
    .person-face-box.suggested {
      border-color: #e19b2d;
      background: rgb(225 155 45 / 14%);
    }
    .month-grid-server {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 14px;
      align-content: start;
      padding: 12px;
      overflow: auto;
    }
    .thumb-link {
      display: grid;
      place-items: center;
      width: 100%;
      aspect-ratio: 4 / 3;
      overflow: hidden;
      color: inherit;
      text-decoration: none;
      background: #181818;
    }
    .thumb-link img, .video-thumb {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: grid;
      place-items: center;
      background: #181818;
      text-align: center;
    }
    .item { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
    .item img { width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block; background: #181818; transform-origin: center center; }
    .text { padding: 10px; font-size: 14px; }
    .path { overflow-wrap: anywhere; }
    .score { color: var(--muted); margin-top: 4px; }
    .error { color: var(--danger); }
    .message { color: var(--muted); }
    .browser-footer {
      background: var(--panel);
      border-top: 1px solid var(--border);
      padding: 8px 12px;
      font-size: 13px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      align-items: center;
      min-width: 0;
    }
    .filename {
      min-width: 0;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
      color: var(--muted);
    }
    .face-overlay {
      position: fixed;
      inset: 0;
      z-index: 10;
      background: rgb(0 0 0 / 86%);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 8px;
      padding: 12px;
    }
    .face-overlay[hidden] { display: none; }
    .info-overlay {
      position: fixed;
      inset: 0;
      z-index: 10;
      background: rgb(0 0 0 / 86%);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 8px;
      padding: 12px;
    }
    .info-overlay[hidden] { display: none; }
    .info-panel {
      align-self: start;
      justify-self: center;
      width: min(760px, 100%);
      max-height: 100%;
      overflow: auto;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 18px;
      color: var(--text);
    }
    .info-panel h2 { margin: 0 0 14px; font-size: 20px; }
    .modal-overlay {
      position: fixed;
      inset: 0;
      z-index: 10;
      display: grid;
      place-items: center;
      padding: 16px;
      background: rgb(0 0 0 / 72%);
    }
    .modal-overlay[hidden] { display: none; }
    .modal-panel {
      width: min(420px, 100%);
      display: grid;
      gap: 10px;
      padding: 18px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
    }
    .modal-panel h2 { margin: 0; font-size: 20px; }
    .modal-panel label { color: var(--muted); font-size: 13px; }
    .modal-panel input[type="text"] {
      width: 100%;
      box-sizing: border-box;
      min-height: 36px;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 8px;
      background: #181818;
      color: var(--text);
      font: inherit;
    }
    .modal-actions { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
    .info-list { display: grid; gap: 0; margin: 0; }
    .info-row {
      display: grid;
      grid-template-columns: minmax(120px, 180px) minmax(0, 1fr);
      gap: 12px;
      padding: 9px 0;
      border-top: 1px solid var(--border);
    }
    .info-row:first-child { border-top: 0; }
    .info-row dt { color: var(--muted); }
    .info-row dd { margin: 0; overflow-wrap: anywhere; }
    .app-toggle-form { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .app-toggle { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; }
    .app-toggle input { position: absolute; opacity: 0; pointer-events: none; }
    .app-toggle-track {
      width: 44px;
      height: 24px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: #303030;
      padding: 2px;
      transition: background 120ms ease, border-color 120ms ease;
    }
    .app-toggle-track span {
      display: block;
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: var(--muted);
      transition: transform 120ms ease, background 120ms ease;
    }
    .app-toggle input:checked + .app-toggle-track {
      border-color: #6fbf8f;
      background: #1f5c38;
    }
    .app-toggle input:checked + .app-toggle-track span {
      transform: translateX(20px);
      background: #d8ffe5;
    }
    .lightbox-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      color: #fff;
      font-size: 14px;
      min-width: 0;
    }
    .lightbox-title {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .lightbox-close {
      border-color: rgb(255 255 255 / 35%);
      background: rgb(255 255 255 / 10%);
      color: #fff;
      min-width: 42px;
    }
    .lightbox-stage {
      min-width: 0;
      min-height: 0;
      display: grid;
      place-items: center;
      overflow: auto;
    }
    .face-list {
      width: min(1200px, 100%);
      display: grid;
      grid-template-columns: 1fr;
      gap: 18px;
      align-items: start;
    }
    .face-detail {
      display: grid;
      gap: 8px;
      color: #fff;
    }
    .face-detail-title {
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .lightbox-media {
      position: relative;
      display: inline-block;
      width: fit-content;
      max-width: 100%;
      justify-self: start;
      transform-origin: center center;
    }
    .lightbox-media img {
      display: block;
      max-width: calc(100vw - 24px);
      width: auto;
      height: auto;
    }
    .face-box {
      position: absolute;
      border: 3px solid #ff1f1f;
      background: rgb(255 31 31 / 12%);
      pointer-events: none;
    }
    .assign-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .new-person-form {
      display: grid;
      grid-template-columns: auto minmax(160px, 280px) auto;
      gap: 8px;
      align-items: center;
      justify-content: start;
    }
    .new-person-form label {
      color: var(--muted);
      font-size: 13px;
    }
    .assign-person-button {
      border-color: rgb(255 255 255 / 22%);
      background: rgb(255 255 255 / 10%);
      color: #fff;
      min-height: 34px;
      padding: 6px 10px;
    }
    .assign-person-button:hover { background: rgb(255 255 255 / 18%); }
    .assign-person-button:disabled { opacity: 0.55; cursor: default; }
    .assign-status { color: var(--muted); font-size: 13px; min-height: 1.3em; }
    @media (max-width: 640px) {
      .shell { padding: 16px; }
      .search { grid-template-columns: 1fr; }
      .browser-header { align-items: stretch; }
      .nav-button, .server-search-link, .person-link, .faces-button { flex: 1 1 auto; justify-content: center; text-align: center; }
      .top-actions { margin-left: 0; width: 100%; justify-content: stretch; }
      .people-row { grid-template-columns: 1fr; align-items: stretch; }
      .removed-row { grid-template-columns: 1fr; align-items: stretch; }
      .geo-row { grid-template-columns: 1fr; }
      .new-person-form { grid-template-columns: 1fr; align-items: stretch; }
      .info-row { grid-template-columns: 1fr; gap: 4px; }
    }
"""
SERVER_JS = r"""  const faceOverlay = document.getElementById("faceOverlay");
  const infoOverlay = document.getElementById("infoOverlay");
  const openFacesButton = document.querySelector("[data-open-faces]");
  const closeFacesButton = document.querySelector("[data-close-faces]");
  const openInfoButton = document.querySelector("[data-open-info]");
  const closeInfoButton = document.querySelector("[data-close-info]");
  const faceList = faceOverlay?.querySelector("[data-face-list]");
  const infoList = infoOverlay?.querySelector("[data-info-list]");
  const personRenameDialog = document.getElementById("personRenameDialog");
  const personRenameForm = document.querySelector("[data-person-rename-form]");
  const personRenameStatus = document.querySelector("[data-person-rename-status]");
  const closePersonRenameButton = document.querySelector("[data-close-person-rename]");
  const personRenameNameInput = personRenameForm?.querySelector('input[name="new_name"]');
  const personRenameOldNameInput = personRenameForm?.querySelector('input[name="old_name"]');
  let facesLoaded = false;
  let infoLoaded = false;
  function faceStatusMessage(message) {
    const item = document.createElement("p");
    item.className = "empty";
    item.textContent = message;
    return item;
  }
  async function loadFacesOverlay() {
    if (!faceList || facesLoaded) return;
    const fileId = openFacesButton?.dataset.facesItem || "";
    if (!fileId) return;
    faceList.replaceChildren(faceStatusMessage("Laster..."));
    try {
      const response = await fetch(`/api/item-faces?file_id=${encodeURIComponent(fileId)}`);
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke laste ansikter.");
      faceList.innerHTML = payload.html || "";
      bindFaceAssignmentHandlers(faceList);
      facesLoaded = true;
    } catch (error) {
      faceList.replaceChildren(faceStatusMessage(error.message || "Kunne ikke laste ansikter."));
    }
  }
  async function openFacesOverlay() {
    if (!faceOverlay) return;
    faceOverlay.hidden = false;
    await loadFacesOverlay();
    closeFacesButton?.focus();
  }
  function closeFacesOverlay() {
    if (!faceOverlay) return;
    faceOverlay.hidden = true;
  }
  function infoStatusRow(message) {
    const row = document.createElement("div");
    row.className = "info-row";
    const label = document.createElement("dt");
    label.textContent = "Status";
    const value = document.createElement("dd");
    value.textContent = message;
    row.append(label, value);
    return row;
  }
  async function loadInfoOverlay() {
    if (!infoList || infoLoaded) return;
    const fileId = openInfoButton?.dataset.infoItem || "";
    if (!fileId) return;
    infoList.replaceChildren(infoStatusRow("Laster..."));
    try {
      const response = await fetch(`/api/item-info?file_id=${encodeURIComponent(fileId)}`);
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke laste bildeinfo.");
      infoList.innerHTML = payload.html || "";
      infoLoaded = true;
    } catch (error) {
      infoList.replaceChildren(infoStatusRow(error.message || "Kunne ikke laste bildeinfo."));
    }
  }
  async function openInfoOverlay() {
    if (!infoOverlay) return;
    infoOverlay.hidden = false;
    await loadInfoOverlay();
    closeInfoButton?.focus();
  }
  function closeInfoOverlay() {
    if (!infoOverlay) return;
    infoOverlay.hidden = true;
  }
  function openPersonRenameDialog(name) {
    if (!personRenameDialog || !personRenameForm || !personRenameNameInput || !personRenameOldNameInput) return;
    personRenameOldNameInput.value = name || "";
    personRenameNameInput.value = name || "";
    if (personRenameStatus) personRenameStatus.textContent = "";
    personRenameDialog.hidden = false;
    personRenameNameInput.focus();
    personRenameNameInput.select();
  }
  function closePersonRenameDialog() {
    if (!personRenameDialog) return;
    personRenameDialog.hidden = true;
  }
  function ensureTopPersonLink(name, url, confirmed = false) {
    if (!name || !url) return;
    let people = document.querySelector(".topline .people");
    if (!people) {
      people = document.createElement("div");
      people.className = "people";
      document.querySelector(".topline .title")?.after(people);
    }
    const exists = Array.from(people.querySelectorAll(".person-link")).some(link => link.dataset.personName === name);
    if (exists) return;
    const link = document.createElement("a");
    link.className = "person-link";
    link.href = url;
    link.dataset.personName = name;
    link.append(document.createTextNode(name));
    if (confirmed) {
      const badge = document.createElement("span");
      badge.className = "confirmed-badge";
      badge.title = "Bekreftet";
      badge.setAttribute("aria-label", "Bekreftet");
      badge.textContent = " ✅";
      link.append(badge);
    }
    people.append(link);
  }
  openFacesButton?.addEventListener("click", openFacesOverlay);
  closeFacesButton?.addEventListener("click", closeFacesOverlay);
  openInfoButton?.addEventListener("click", openInfoOverlay);
  closeInfoButton?.addEventListener("click", closeInfoOverlay);
  closePersonRenameButton?.addEventListener("click", closePersonRenameDialog);
  document.querySelectorAll("[data-open-person-rename]").forEach(button => {
    button.addEventListener("click", () => openPersonRenameDialog(button.dataset.personName || ""));
  });
  document.querySelectorAll("[data-delete-person-name]").forEach(button => {
    button.addEventListener("click", async () => {
      const personName = button.dataset.deletePersonName || "";
      if (!personName) return;
      const command = `bildebank face-person-delete "${personName}"`;
      if (!confirm(`Slette personen ${personName} fra ansiktsdatabasen?\n\nDette sletter bekreftede ansiktskoblinger og forslag for personen, men ingen bilder.\n\nTilsvarer:\n${command}`)) return;
      button.disabled = true;
      try {
        const response = await fetch("/api/face-person-delete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({person_name: personName}),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke slette person.");
        window.location.reload();
      } catch (error) {
        alert(error.message || "Kunne ikke slette person.");
        button.disabled = false;
      }
    });
  });
  personRenameDialog?.addEventListener("click", event => {
    if (event.target === personRenameDialog) closePersonRenameDialog();
  });
  personRenameForm?.addEventListener("submit", async event => {
    event.preventDefault();
    const oldName = personRenameOldNameInput?.value || "";
    const newName = personRenameNameInput?.value?.trim() || "";
    if (personRenameStatus) personRenameStatus.textContent = "Lagrer...";
    personRenameForm.querySelectorAll("button, input").forEach(item => item.disabled = true);
    try {
      const response = await fetch("/api/face-person-rename", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({old_name: oldName, new_name: newName}),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke endre navn.");
      window.location.reload();
    } catch (error) {
      if (personRenameStatus) personRenameStatus.textContent = error.message || "Kunne ikke endre navn.";
      personRenameForm.querySelectorAll("button, input").forEach(item => item.disabled = false);
      personRenameNameInput?.focus();
    }
  });
  document.querySelectorAll("[data-rotate-item]").forEach(button => {
    button.addEventListener("click", async () => {
      const fileId = Number(button.dataset.rotateItem);
      const direction = button.dataset.rotateDirection || "";
      button.disabled = true;
      try {
        const response = await fetch("/api/item-rotate", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId, direction}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke rotere.");
        window.location.reload();
      } catch (error) {
        alert(error.message || "Kunne ikke rotere.");
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll("[data-delete-item]").forEach(button => {
    button.addEventListener("click", async () => {
      const fileId = Number(button.dataset.deleteItem);
      const path = button.dataset.deletePath || "";
      const redirectUrl = button.dataset.deleteRedirect || "/";
      if (!confirm(`Flytte til deleted/?\n\n${path}`)) return;
      button.disabled = true;
      try {
        const response = await fetch("/api/item-delete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke slette.");
        window.location.href = redirectUrl;
      } catch (error) {
        alert(error.message || "Kunne ikke slette.");
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll("[data-undelete-item]").forEach(button => {
    button.addEventListener("click", async () => {
      const fileId = Number(button.dataset.undeleteItem);
      const path = button.dataset.undeletePath || "";
      if (!confirm(`Flytte tilbake til bildesamlingen?\n\n${path}`)) return;
      button.disabled = true;
      try {
        const response = await fetch("/api/item-undelete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke angre sletting.");
        button.closest(".removed-row")?.remove();
      } catch (error) {
        alert(error.message || "Kunne ikke angre sletting.");
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll("[data-unconfirm-face]").forEach(button => {
    button.addEventListener("click", async () => {
      const faceId = Number(button.dataset.unconfirmFace);
      const personName = button.dataset.unconfirmPerson || "";
      if (!faceId || !personName) return;
      const command = `bildebank face-person-remove-face "${personName}" ${faceId}`;
      if (!confirm(`Avbekrefte face-id ${faceId} fra ${personName}?\n\nTilsvarer:\n${command}`)) return;
      button.disabled = true;
      try {
        const response = await fetch("/api/face-person-remove-face", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({face_id: faceId, person_name: personName}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke avbekrefte.");
        window.location.reload();
      } catch (error) {
        alert(error.message || "Kunne ikke avbekrefte.");
        button.disabled = false;
      }
    });
  });
  faceOverlay?.addEventListener("click", event => {
    if (event.target === faceOverlay || event.target.classList?.contains("lightbox-stage")) closeFacesOverlay();
  });
  infoOverlay?.addEventListener("click", event => {
    if (event.target === infoOverlay) closeInfoOverlay();
  });
  async function assignFace(detail, status, endpoint, faceId, personName) {
    if (!detail || !status || !faceId || !personName) return;
    status.textContent = "Lagrer...";
    detail.querySelectorAll("button, input").forEach(item => item.disabled = true);
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({face_id: Number(faceId), person_name: personName}),
      });
      const payload = await response.json();
      if (!payload.ok) throw new Error(payload.error || "Kunne ikke lagre.");
      status.textContent = `Koblet til ${payload.person_name}.`;
      ensureTopPersonLink(payload.person_name, payload.person_url, payload.confirmed);
      detail.remove();
      if (!document.querySelector(".face-detail")) {
        closeFacesOverlay();
        window.location.reload();
      }
    } catch (error) {
      status.textContent = error.message || "Kunne ikke lagre.";
      detail.querySelectorAll("button, input").forEach(item => item.disabled = false);
    }
  }
  function bindFaceAssignmentHandlers(root = document) {
    root.querySelectorAll(".assign-person-button").forEach(button => {
      button.addEventListener("click", async () => {
        const faceId = button.dataset.faceId;
        const personName = button.dataset.personName;
        const detail = button.closest(".face-detail");
        const status = detail?.querySelector(".assign-status");
        await assignFace(detail, status, "/api/face-person-add-face", faceId, personName);
      });
    });
    root.querySelectorAll("[data-new-person-form]").forEach(form => {
      form.addEventListener("submit", async event => {
        event.preventDefault();
        const detail = form.closest(".face-detail");
        const status = detail?.querySelector(".assign-status");
        const faceId = form.querySelector('input[name="face_id"]')?.value;
        const personName = form.querySelector('input[name="person_name"]')?.value?.trim();
        await assignFace(detail, status, "/api/face-person-create-and-add-face", faceId, personName);
      });
    });
  }
  bindFaceAssignmentHandlers();
  document.addEventListener("keydown", event => {
    if (faceOverlay && !faceOverlay.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeFacesOverlay();
      }
      return;
    }
    if (infoOverlay && !infoOverlay.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeInfoOverlay();
      }
      return;
    }
    if (personRenameDialog && !personRenameDialog.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        closePersonRenameDialog();
      }
      return;
    }
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const target = event.target;
    if (
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement ||
      target instanceof HTMLButtonElement ||
      target?.isContentEditable
    ) return;
    const selector = {
      ArrowLeft: '[data-key-nav="previous"]',
      ArrowRight: '[data-key-nav="next"]',
      ArrowUp: '[data-key-nav="previous-month"]',
      ArrowDown: '[data-key-nav="next-month"]',
      PageUp: '[data-key-nav="previous-year"]',
      PageDown: '[data-key-nav="next-year"]',
    }[event.key] || "";
    if (!selector) return;
    const link = document.querySelector(selector);
    if (!(link instanceof HTMLAnchorElement)) return;
    event.preventDefault();
    window.location.href = link.href;
  });
"""


def page_html(title: str, body: str) -> str:
    asset_version = urllib.parse.quote(SERVER_ASSET_VERSION, safe="")
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="/static/server.css?v={asset_version}">
</head>
<body>
{body}
<script src="/static/server.js?v={asset_version}"></script>
</body>
</html>
"""


def run_server(
    target: Path,
    config: AppConfig,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    ready: Callable[[str], None] | None = None,
) -> None:
    db.prepare_database(target)
    server = BildebankServer((host, port), target, config)
    actual_host, actual_port = server.server_address
    url = f"http://{actual_host}:{actual_port}/"
    if ready is not None:
        ready(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
