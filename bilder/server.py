from __future__ import annotations

import html
import importlib.util
import json
import mimetypes
import re
import shutil
import sqlite3
import threading
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import __version__, db
from .config import OpenClipConfig, load_config
from .face import add_face_to_person, create_person, normalize_person_name, remove_face_from_person
from .html_export import (
    FACE_DB_FILENAME,
    browser_face_items,
    display_relative_path,
    face_tables_exist,
    format_bytes,
    month_key_from_path,
)
from .geo import H3_COLUMNS, h3_column_for_resolution, h3_resolution, h3_resolution_label, h3_area_label
from .media import camera_info
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
    show_faces: bool = True


class OpenClipSearchCache:
    def __init__(self, config: OpenClipConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._model: Any | None = None
        self._tokenizer: Any | None = None

    def text_vector(self, query: str) -> list[float]:
        with self._lock:
            if self._model is None or self._tokenizer is None:
                self._model, self._tokenizer = load_text_model(self.config)
            return text_embedding(self._model, self._tokenizer, query)

    @property
    def loaded(self) -> bool:
        return self._model is not None and self._tokenizer is not None


class BildebankServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], target: Path, config: OpenClipConfig) -> None:
        super().__init__(address, BildebankRequestHandler)
        self.target = target
        self.config = config
        self.search_cache = OpenClipSearchCache(config)


class BildebankRequestHandler(BaseHTTPRequestHandler):
    server: BildebankServer

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/":
                self.respond_browser_root()
                return
            if parsed.path == "/people":
                self.respond_html(people_page_html(self.server.target))
                return
            if parsed.path == "/app":
                self.respond_html(app_status_page_html(self.server.target))
                return
            if parsed.path in {"/app/removed", "/app/removed/"}:
                self.respond_html(removed_files_page_html(self.server.target))
                return
            if parsed.path == "/geo":
                self.respond_geo(parsed.query)
                return
            if parsed.path == "/geo/stats":
                self.respond_html(geo_stats_page_html(self.server.target))
                return
            if parsed.path == "/geo/missing":
                self.respond_geo_missing(parsed.query)
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
            if parsed.path.startswith("/person/"):
                self.respond_person(parsed.path.removeprefix("/person/"))
                return
            if parsed.path == "/search":
                params = urllib.parse.parse_qs(parsed.query)
                query = first_param(params, "q").strip()
                limit = positive_int_param(params, "limit", DEFAULT_SEARCH_LIMIT)
                if not query:
                    self.respond_html(index_html(self.server, message="Skriv inn et søk."))
                    return
                stats = search_server_images(self.server, query=query, limit=limit)
                self.respond_html(search_html(self.server, stats, limit))
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
            if parsed.path == "/api/face-person-add-face":
                self.respond_add_face_to_person()
                return
            if parsed.path == "/api/face-person-remove-face":
                self.respond_remove_face_from_person()
                return
            if parsed.path == "/api/face-person-create-and-add-face":
                self.respond_create_person_and_add_face()
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
            self.respond_html(empty_browser_html())
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
        self.respond_html(source_item_page_html(self.server.target, source, item, previous_item, next_item, month_nav))

    def respond_month(self, raw_month: str) -> None:
        month_key = urllib.parse.unquote(raw_month).strip()
        if not valid_month_key(month_key):
            self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
            return
        source = all_browser_source()
        items = source_month_items(self.server.target, source, month_key)
        self.respond_html(source_month_page_html(self.server.target, source, month_key, items))

    def respond_person(self, raw_path: str) -> None:
        raw_name, person_mode, show_faces, page_mode, raw_value = parse_person_path(raw_path)
        person_name = urllib.parse.unquote(raw_name).strip()
        if not person_name:
            self.respond_text("Personnavn mangler.", status=HTTPStatus.BAD_REQUEST)
            return
        person = person_by_name(self.server.target, person_name)
        if person is None:
            self.respond_html(person_not_found_html(person_name), status=HTTPStatus.NOT_FOUND)
            return
        canonical_name = str(person["name"])
        source = person_browser_source(canonical_name, include_suggestions=person_mode != "confirmed", show_faces=show_faces)
        if page_mode is None:
            item = first_source_item(self.server.target, source)
            if item is None:
                self.respond_html(empty_person_browser_html(source))
                return
            self.redirect(source_item_url(source, int(item["id"])))
            return
        if page_mode == "item":
            file_id = parse_file_id(raw_value)
            item = source_item_by_id(self.server.target, source, file_id)
            if item is None:
                self.respond_text("Filen finnes ikke for denne personen.", status=HTTPStatus.NOT_FOUND)
                return
            previous_item, next_item = adjacent_source_items(self.server.target, source, item)
            month_nav = source_month_navigation(self.server.target, source, item)
            self.respond_html(source_item_page_html(self.server.target, source, item, previous_item, next_item, month_nav))
            return
        if page_mode == "month":
            month_key = urllib.parse.unquote(raw_value).strip()
            if not valid_month_key(month_key):
                self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
                return
            items = source_month_items(self.server.target, source, month_key)
            self.respond_html(source_month_page_html(self.server.target, source, month_key, items))
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
                self.respond_html(empty_source_html(source))
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
            self.respond_html(source_item_page_html(self.server.target, source, item, previous_item, next_item, month_nav))
            return
        if page_mode == "month":
            month_key = urllib.parse.unquote(raw_value).strip()
            if not valid_month_key(month_key):
                self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
                return
            items = source_month_items(self.server.target, source, month_key)
            self.respond_html(source_month_page_html(self.server.target, source, month_key, items))
            return
        self.respond_text("Ugyldig datokildeside.", status=HTTPStatus.NOT_FOUND)

    def respond_geo(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        resolution = positive_int_param(params, "resolution", DEFAULT_GEO_RESOLUTION)
        min_count = positive_int_param(params, "min_count", DEFAULT_GEO_MIN_COUNT)
        limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
        if resolution not in H3_COLUMNS:
            self.respond_text("H3-oppløsning må være 5, 6, 7, 8 eller 9.", status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_html(geo_index_page_html(self.server.target, resolution=resolution, min_count=min_count, limit=limit))

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

    def respond_add_face_to_person(self) -> None:
        payload = BildebankRequestHandler.read_face_person_payload(self)
        if isinstance(payload[0], dict):
            self.respond_json(payload[0], status=payload[1])
            return
        person_name, face_id = payload
        try:
            result = add_face_to_person(self.server.target, person_name, face_id)
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
            result = remove_face_from_person(self.server.target, person_name, face_id)
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
            create_person(self.server.target, person_name)
            result = add_face_to_person(self.server.target, person_name, face_id)
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
                "added": result.added,
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
    "size_bytes, view_rotation_degrees, h3_res5, h3_res6, h3_res7, h3_res8, h3_res9"
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


def valid_browser_date_source(date_source: str) -> bool:
    return date_source in {"filename", "mtime"}


def source_item_url(source: BrowserSource, file_id: int) -> str:
    if source.person_name is not None or source.date_source is not None:
        return f"{source.root_url}/item/{file_id}"
    return f"/item/{file_id}"


def source_month_url(source: BrowserSource, month_key: str) -> str:
    quoted = urllib.parse.quote(month_key)
    if source.person_name is not None or source.date_source is not None:
        return f"{source.root_url}/month/{quoted}"
    return f"/month/{quoted}"


def first_browser_item(target: Path) -> Any | None:
    return first_source_item(target, all_browser_source())


def first_source_item(target: Path, source: BrowserSource) -> Any | None:
    items = source_items(target, source)
    return items[0] if items else None


def browser_item_by_id(target: Path, file_id: int) -> Any | None:
    return source_item_by_id(target, all_browser_source(), file_id)


def source_item_by_id(target: Path, source: BrowserSource, file_id: int) -> Any | None:
    if source.person_name is not None or source.date_source is not None:
        return next((item for item in source_items(target, source) if int(item["id"]) == file_id), None)
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


def adjacent_source_items(target: Path, source: BrowserSource, item: Any) -> tuple[Any | None, Any | None]:
    if source.person_name is not None or source.date_source is not None:
        return adjacent_items_from_list(source_items(target, source), item)
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


def source_month_keys(target: Path, source: BrowserSource) -> list[str]:
    if source.person_name is not None or source.date_source is not None:
        keys = {month_key_for_item(target, item) for item in source_items(target, source)}
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
            SELECT target_path
            FROM files
            WHERE deleted_at IS NULL
            ORDER BY target_path
            """
        )
        keys = {month_key_from_stored_path(str(row["target_path"])) for row in rows}
        return tuple(sorted(key for key in keys if key is not None))
    finally:
        conn.close()


def confirmed_people_for_file(target: Path, file_id: int) -> list[dict[str, str]]:
    face_db_path = target / FACE_DB_FILENAME
    try:
        mtime_ns = face_db_path.stat().st_mtime_ns
    except OSError:
        return []
    return [
        {"name": name, "url": person_url(name)}
        for name in cached_confirmed_people_for_file(str(target.resolve()), mtime_ns, file_id)
    ]


@lru_cache(maxsize=512)
def cached_confirmed_people_for_file(target_path: str, face_db_mtime_ns: int, file_id: int) -> tuple[str, ...]:
    conn = sqlite3.connect(Path(target_path) / FACE_DB_FILENAME)
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
        return tuple(sorted(people))
    except sqlite3.Error:
        return ()
    finally:
        conn.close()


def clear_face_caches() -> None:
    cached_confirmed_people_for_file.cache_clear()
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


def registered_people(target: Path) -> list[dict[str, str]]:
    face_db_path = target / FACE_DB_FILENAME
    try:
        mtime_ns = face_db_path.stat().st_mtime_ns
    except OSError:
        return []
    return [
        {"name": name, "url": person_url(name)}
        for name in cached_registered_people(str(target.resolve()), mtime_ns)
    ]


@lru_cache(maxsize=8)
def cached_registered_people(target_path: str, face_db_mtime_ns: int) -> tuple[str, ...]:
    conn = sqlite3.connect(Path(target_path) / FACE_DB_FILENAME)
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


def registered_people_rows(target: Path) -> list[dict[str, object]]:
    face_db_path = target / FACE_DB_FILENAME
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


def unconfirmed_faces_for_item(target: Path, item: Any) -> list[dict[str, object]]:
    face_db_path = target / FACE_DB_FILENAME
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
    return browser_face_items(db.absolute_target_path(target, Path(str(item["target_path"]))), faces)


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


def person_by_name(target: Path, person_name: str) -> sqlite3.Row | None:
    face_db_path = target / FACE_DB_FILENAME
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


def person_file_ids(target: Path, person_name: str, *, include_suggestions: bool = True) -> list[int]:
    person = person_by_name(target, person_name)
    if person is None:
        return []
    conn = sqlite3.connect(target / FACE_DB_FILENAME)
    conn.row_factory = sqlite3.Row
    try:
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
        return [int(row["file_id"]) for row in rows]
    finally:
        conn.close()


def person_items(target: Path, person_name: str, *, include_suggestions: bool = True) -> list[Any]:
    file_ids = person_file_ids(target, person_name, include_suggestions=include_suggestions)
    return items_by_file_ids(target, file_ids)


def source_items(target: Path, source: BrowserSource) -> list[Any]:
    if source.person_name is not None:
        return person_items(target, source.person_name, include_suggestions=source.include_suggestions)
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


def source_month_navigation(target: Path, source: BrowserSource, item: Any) -> dict[str, str | None]:
    return source_month_navigation_for_key(target, source, month_key_for_item(target, item))


def source_month_navigation_for_key(target: Path, source: BrowserSource, current_key: str) -> dict[str, str | None]:
    if not valid_month_key(current_key):
        return {"previous_year": None, "next_year": None, "previous_month": None, "next_month": None}
    keys = source_month_keys(target, source)
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


def source_month_items(target: Path, source: BrowserSource, month_key: str) -> list[Any]:
    if source.person_name is not None:
        return [item for item in source_items(target, source) if month_key_for_item(target, item) == month_key]
    return browser_month_items(target, month_key)


def person_faces_for_item(
    target: Path,
    person_name: str,
    item: Any,
    *,
    include_suggestions: bool = True,
) -> list[dict[str, object]]:
    person = person_by_name(target, person_name)
    if person is None:
        return []
    conn = sqlite3.connect(target / FACE_DB_FILENAME)
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
    rendered = browser_face_items(db.absolute_target_path(target, Path(str(item["target_path"]))), faces)
    for face in rendered:
        meta = face_meta.get(int(face["faceId"]))
        if meta is not None:
            face["status"] = meta["status"]
            face["similarity"] = meta["similarity"]
    return rendered


def person_url(person_name: str) -> str:
    return "/person/" + urllib.parse.quote(person_name, safe="")


def person_item_url(person_name: str, file_id: int) -> str:
    return f"{person_url(person_name)}/item/{file_id}"


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
    conn = connect_openclip_db(server.target)
    try:
        rows = list(
            conn.execute(
                """
                SELECT file_id, target_path, target_path_key, embedding
                FROM image_embeddings
                WHERE model_name = ? AND pretrained = ?
                """,
                (server.config.model_name, server.config.pretrained),
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
        run_id = create_search_run(conn, clean_query, server.config, limit)
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
        return empty_browser_html()
    previous_item, next_item = adjacent_browser_items(server.target, item)
    month_nav = browser_month_navigation(server.target, item)
    return item_page_html(server.target, item, previous_item, next_item, month_nav)


def search_start_html(server: BildebankServer, *, message: str = "") -> str:
    return page_html(
        "Bildesøk",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a></p>
          <h1>Bildesøk</h1>
          <p class="meta">OpenCLIP {html.escape(server.config.model_name)} ({html.escape(server.config.pretrained)})</p>
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
          <p><a href="/">Til bildebrowser</a> · <a href="/geo/stats">Geo-statistikk</a> · <a href="/geo/missing">Bilder uten GPS</a></p>
          <h1>Steder</h1>
          {geo_stats_summary_html(stats)}
          <form action="/geo" method="get" class="geo-filter">
            <label>H3-oppløsning <input name="resolution" value="{resolution}" inputmode="numeric"></label>
            <label>Minst antall <input name="min_count" value="{min_count}" inputmode="numeric"></label>
            <label>Maks steder <input name="limit" value="{limit}" inputmode="numeric"></label>
            <button type="submit">Vis</button>
          </form>
          <p class="meta">Viser H3-{h3_resolution_label(resolution)}. Lavere tall gir større områder.</p>
          {content}
        </main>
        """,
    )


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
          <p class="meta">H3-celle {html.escape(h3_cell)}, {h3_resolution_label(resolution)}. Viser opptil {limit} bilder.</p>
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
          <h1>Feil</h1>
          <p class="error">{html.escape(str(exc))}</p>
          {search_form("")}
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


def empty_browser_html() -> str:
    return page_html(
        "Bildebrowser",
        """
        <main class="shell">
          <h1>Bildebrowser</h1>
          <p class="meta">Ingen filer i bildesamlingen.</p>
          <p><a href="/search">Bildesøk</a></p>
        </main>
        """,
    )


def item_page_html(
    target: Path,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
) -> str:
    return source_item_page_html(target, all_browser_source(), item, previous_item, next_item, month_nav)


def source_item_page_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
) -> str:
    target_path = Path(str(item["target_path"]))
    relative = display_relative_path(target, target_path)
    media = source_item_media_html(target, source, item)
    controls = source_controls_html(
        source,
        month_nav,
        previous_item,
        next_item,
        include_info_button=True,
        rotation_buttons=rotation_buttons_html(source, item),
        unconfirm_buttons=unconfirm_face_buttons_html(target, source, item),
        delete_button=delete_button_html(source, item, previous_item, next_item),
    )
    people = people_links_html(confirmed_people_for_file(target, int(item["id"])))
    unconfirmed_faces = unconfirmed_faces_for_item(target, item)
    all_people = registered_people(target)
    faces_button = faces_button_html(unconfirmed_faces) if source.person_name is None else ""
    faces_overlay = faces_overlay_html(item, unconfirmed_faces, all_people) if source.person_name is None else ""
    action_links = source_action_links_html(source, item)
    info_overlay = image_info_overlay_html(target, item)
    duplicate_warning = source_duplicate_confirmed_faces_warning_html(target, source, item)
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


def source_duplicate_confirmed_faces_warning_html(target: Path, source: BrowserSource, item: Any) -> str:
    if source.person_name is None or source.include_suggestions:
        return ""
    count = confirmed_person_face_count_for_item(target, source.person_name, int(item["id"]))
    if count < 2:
        return ""
    return (
        '<div class="warning">'
        f"NB: {count} bekreftede ansikter for {html.escape(source.person_name)} i dette bildet"
        "</div>"
    )


def confirmed_person_face_count_for_item(target: Path, person_name: str, file_id: int) -> int:
    person = person_by_name(target, person_name)
    if person is None:
        return 0
    conn = sqlite3.connect(target / FACE_DB_FILENAME)
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


def source_top_links_html(source: BrowserSource, item: Any | None = None) -> str:
    links = [
        '<a class="server-search-link" href="/people">Personer</a>',
        '<a class="server-search-link" href="/geo">Steder</a>',
    ]
    if source.date_source is not None:
        all_url = source_item_url(all_browser_source(), int(item["id"])) if item is not None else "/"
        links.insert(0, f'<a class="server-search-link" href="{html.escape(all_url)}">Alle bilder</a>')
    if source.person_name is not None:
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


def source_action_links_html(source: BrowserSource, item: Any | None = None) -> str:
    return f"""
    <div class="top-actions">
      {source_top_links_html(source, item)}
      <a class="server-search-link" href="/search">Bildesøk</a>
      <a class="server-search-link" href="/app">App</a>
    </div>
    """


def app_status_page_html(target: Path) -> str:
    config = load_config(server_program_repo_root())
    rows = "\n".join(
        app_status_row_html(label, value)
        for label, value in (
            ("Bildesamling", str(target)),
            ("Bildebank-versjon", __version__),
            ("InsightFace aktivert", yes_no(config.face_recognition.enabled)),
            ("InsightFace installert", yes_no(module_available("insightface"))),
            ("OpenCLIP tilgjengelig", yes_no(module_available("open_clip"))),
            ("OpenCLIP-modell", config.openclip.model_name),
            ("OpenCLIP-pretrained", config.openclip.pretrained),
            ("OpenCLIP-device", config.openclip.device),
        )
    )
    return page_html(
        "App",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a> · <a href="/app/removed">Slettede bilder</a> · <a href="/date-source/filename">Dato fra filnavn</a> · <a href="/date-source/mtime">Dato fra mtime</a></p>
          <h1>App</h1>
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
          <p><a href="/app">Til app</a> · <a href="/">Til bildebrowser</a></p>
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


def yes_no(value: bool) -> str:
    return "ja" if value else "nei"


def module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def server_program_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def source_item_media_html(target: Path, source: BrowserSource, item: Any) -> str:
    if source.person_name is not None:
        if not source.show_faces:
            return item_media_html(item)
        faces = person_faces_for_item(
            target,
            source.person_name,
            item,
            include_suggestions=source.include_suggestions,
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
    rotation_buttons: str = "",
    unconfirm_buttons: str = "",
    delete_button: str = "",
) -> str:
    info_button = image_info_button_html() if include_info_button else ""
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


def unconfirm_face_buttons_html(target: Path, source: BrowserSource, item: Any) -> str:
    if source.person_name is None or source.include_suggestions:
        return ""
    faces = person_faces_for_item(target, source.person_name, item, include_suggestions=False)
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


def image_info_button_html() -> str:
    return '<button class="nav-button" type="button" data-open-info>Bildeinfo</button>'


def people_links_html(people: list[dict[str, str]]) -> str:
    if not people:
        return ""
    links = "\n".join(
        f'<a class="person-link" href="{html.escape(person["url"])}">{html.escape(person["name"])}</a>'
        for person in people
    )
    return f'<div class="people">{links}</div>'


def faces_button_html(faces: list[dict[str, object]]) -> str:
    if not faces:
        return ""
    return f'<button class="faces-button" type="button" data-open-faces>Ansikter i bildet ({len(faces)})</button>'


def faces_overlay_html(item: Any, faces: list[dict[str, object]], people: list[dict[str, str]]) -> str:
    if not faces:
        return ""
    target_path = Path(str(item["target_path"]))
    file_id = int(item["id"])
    image_url = f"/file/{file_id}"
    face_items = "\n".join(face_overlay_item_html(item, image_url, face, people) for face in faces)
    return f"""
    <div id="faceOverlay" class="face-overlay" hidden>
      <div class="lightbox-bar">
        <div class="lightbox-title">Ansikter - {html.escape(target_path.name)}</div>
        <button class="lightbox-close" type="button" data-close-faces>Lukk</button>
      </div>
      <div class="lightbox-stage">
        <div class="face-list">{face_items}</div>
      </div>
    </div>
    """


def image_info_overlay_html(target: Path, item: Any) -> str:
    rows = "\n".join(image_info_rows(target, item))
    return f"""
    <div id="infoOverlay" class="info-overlay" hidden>
      <div class="lightbox-bar">
        <div class="lightbox-title">Bildeinfo</div>
        <button class="lightbox-close" type="button" data-close-info>Lukk</button>
      </div>
      <div class="info-panel">
        <h2>Bildeinfo</h2>
        <dl class="info-list">
          {rows}
        </dl>
      </div>
    </div>
    """


def image_info_rows(target: Path, item: Any) -> list[str]:
    target_path = Path(str(item["target_path"]))
    absolute_path = db.absolute_target_path(target, target_path)
    dimensions = cached_image_dimensions(target, absolute_path)
    camera = camera_info(absolute_path)
    rows = [
        info_row_html("Filnavn", display_relative_path(target, target_path)),
        info_row_html("Filstørrelse", f"{format_bytes(int(item['size_bytes']))} ({int(item['size_bytes'])} bytes)"),
        info_row_html("Oppløsning", f"{dimensions.width} x {dimensions.height}" if dimensions else "-"),
        info_row_html("Kamera", camera_text(camera)),
    ]
    sources = image_source_rows(target, target_path)
    if sources:
        rows.append(info_row_html("Kilder", "\n\n".join(sources), multiline=True))
    else:
        rows.append(info_row_html("Kilder", "-"))
    geo_links = image_geo_area_links_html(target, item)
    if geo_links:
        rows.append(info_row_html("Steder", geo_links, raw_html=True))
    return rows


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


def source_month_page_html(target: Path, source: BrowserSource, month_key: str, items: list[Any]) -> str:
    cards = "\n".join(source_month_item_html(target, source, item) for item in items)
    previous_item = items[-1] if items else None
    next_item = items[0] if items else None
    controls = source_controls_html(source, source_month_navigation_for_key(target, source, month_key), previous_item, next_item)
    action_links = source_action_links_html(source)
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


def empty_person_browser_html(person: str | BrowserSource) -> str:
    source = person if isinstance(person, BrowserSource) else person_browser_source(person, include_suggestions=True)
    return empty_source_html(source)


def empty_source_html(source: BrowserSource) -> str:
    return page_html(
        source.title,
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a> · <a href="/people">Personer</a></p>
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
        return "Ingen filer i bildesamlingen."
    if source.include_suggestions:
        return "Ingen bekreftede ansikter eller forslag for denne personen ennå."
    return "Ingen bekreftede bilder for denne personen ennå."


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


def people_page_html(target: Path) -> str:
    people = registered_people_rows(target)
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
        """,
    )


def people_row_html(person: dict[str, object]) -> str:
    name = str(person["name"])
    confirmed_count = int(person["confirmed_file_count"])
    all_count = int(person["all_file_count"])
    suggestion_count = int(person["suggestion_count"])
    duplicate_count = int(person["duplicate_confirmed_file_count"])
    max_confirmed_faces = int(person["max_confirmed_faces_per_file"])
    confirmed_source = person_browser_source(name, include_suggestions=False)
    all_source = person_browser_source(name, include_suggestions=True)
    duplicate_warning = ""
    if duplicate_count > 0:
        duplicate_warning = (
            '<span class="warning people-warning">'
            f"NB: {max_confirmed_faces} bekreftede ansikter i samme bilde"
            "</span>"
        )
    return f"""
    <div class="people-row">
      <div class="people-name">{html.escape(name)}</div>
      {duplicate_warning}
      <a class="person-link" href="{html.escape(confirmed_source.root_url)}">Bekreftede bilder ({confirmed_count})</a>
      <a class="person-link" href="{html.escape(all_source.root_url)}">Bekreftede og forslag ({all_count})</a>
      <span class="status">forslag: {suggestion_count}</span>
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


def page_html(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #171717;
      --panel: #242424;
      --stage: #0e0e0e;
      --border: #3a3a3a;
      --text: #f2f2f2;
      --muted: #b8b8b8;
      --accent: #7db7ff;
      --danger: #ff8a80;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    .shell {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    .meta {{ color: var(--muted); margin: 0 0 18px; }}
    .search {{ display: grid; grid-template-columns: minmax(0, 1fr) 90px auto; gap: 8px; margin: 18px 0; }}
    input, button {{
      font: inherit;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #303030;
      color: var(--text);
    }}
    button {{ cursor: pointer; }}
    button:hover {{ background: #3a3a3a; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }}
    .geo-filter {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin: 18px 0; }}
    .geo-filter label {{ display: grid; gap: 4px; color: var(--muted); font-size: 13px; }}
    .geo-filter input {{ width: 120px; }}
    .geo-name-form input[name="name"] {{ width: min(420px, 70vw); }}
    .geo-stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin: 18px 0; }}
    .geo-stats div {{ display: grid; gap: 3px; padding: 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }}
    .geo-stats span {{ color: var(--muted); }}
    .geo-list {{ display: grid; gap: 8px; margin-top: 18px; }}
    .geo-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 12px; align-items: center; padding: 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); color: var(--text); }}
    .server-browser {{ min-height: 100vh; display: grid; grid-template-rows: auto minmax(0, 1fr) auto; }}
    .browser-header {{
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 8px 10px;
      display: grid;
      gap: 7px;
    }}
    .topline, .controls {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    .title {{ font-weight: 700; margin-right: 8px; line-height: 1.2; }}
    .status {{ color: var(--muted); font-size: 13px; line-height: 1.2; }}
    .warning {{ color: #ffd166; font-size: 13px; line-height: 1.2; font-weight: 700; }}
    .people {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
    .top-actions {{
      margin-left: auto;
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .people-table {{ display: grid; gap: 8px; margin-top: 18px; }}
    .removed-list {{ display: grid; gap: 6px; margin-top: 18px; }}
    .removed-row {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto auto auto auto auto;
      gap: 10px;
      align-items: center;
      padding: 8px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
      font-size: 14px;
    }}
    .removed-row span {{ color: var(--muted); }}
    .people-row {{
      display: grid;
      grid-template-columns: minmax(160px, 1fr) auto auto auto auto;
      gap: 8px;
      align-items: center;
      padding: 8px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
    }}
    .people-name {{ font-weight: 700; overflow-wrap: anywhere; }}
    .people-warning {{ justify-self: start; }}
    a, .disabled {{ color: var(--accent); }}
    a {{ text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .nav-button, .server-search-link, .person-link, .faces-button {{
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 9px;
      background: #303030;
      color: var(--text);
      min-height: 32px;
      display: inline-flex;
      align-items: center;
    }}
    .person-link {{ color: var(--accent); }}
    .faces-button {{ color: var(--accent); }}
    .nav-button:hover, .server-search-link:hover, .person-link:hover, .faces-button:hover {{ background: #3a3a3a; text-decoration: none; }}
    .danger-button {{ color: var(--danger); }}
    .danger-button:hover {{ background: rgb(255 138 128 / 12%); }}
    .disabled {{ color: #777; cursor: default; }}
    .stage {{
      min-height: 0;
      display: grid;
      place-items: center;
      background: var(--stage);
      border-top: 1px solid var(--border);
      overflow: hidden;
      padding: 14px;
    }}
    .stage img, .stage video {{
      max-width: min(100%, 92vw);
      max-height: calc(100vh - 10rem);
      object-fit: contain;
      display: block;
      transform-origin: center center;
    }}
    .person-media {{
      position: relative;
      display: inline-block;
      max-width: min(100%, 92vw);
      max-height: calc(100vh - 10rem);
      transform-origin: center center;
    }}
    .person-media img {{
      max-width: 100%;
      max-height: calc(100vh - 10rem);
      object-fit: contain;
      display: block;
    }}
    .person-face-box {{
      position: absolute;
      border: 2px solid #2fbf71;
      background: rgb(47 191 113 / 13%);
      pointer-events: none;
    }}
    .person-face-label {{
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
    }}
    .person-face-box.suggested {{
      border-color: #e19b2d;
      background: rgb(225 155 45 / 14%);
    }}
    .month-grid-server {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 14px;
      align-content: start;
      padding: 12px;
      overflow: auto;
    }}
    .thumb-link {{ display: block; color: inherit; text-decoration: none; }}
    .thumb-link img, .video-thumb {{
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      display: grid;
      place-items: center;
      background: #181818;
      text-align: center;
    }}
    .item {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }}
    .item img {{ width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block; background: #181818; transform-origin: center center; }}
    .text {{ padding: 10px; font-size: 14px; }}
    .path {{ overflow-wrap: anywhere; }}
    .score {{ color: var(--muted); margin-top: 4px; }}
    .error {{ color: var(--danger); }}
    .message {{ color: var(--muted); }}
    .browser-footer {{
      background: var(--panel);
      border-top: 1px solid var(--border);
      padding: 8px 12px;
      font-size: 13px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      align-items: center;
      min-width: 0;
    }}
    .filename {{
      min-width: 0;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
      color: var(--muted);
    }}
    .face-overlay {{
      position: fixed;
      inset: 0;
      z-index: 10;
      background: rgb(0 0 0 / 86%);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 8px;
      padding: 12px;
    }}
    .face-overlay[hidden] {{ display: none; }}
    .info-overlay {{
      position: fixed;
      inset: 0;
      z-index: 10;
      background: rgb(0 0 0 / 86%);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 8px;
      padding: 12px;
    }}
    .info-overlay[hidden] {{ display: none; }}
    .info-panel {{
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
    }}
    .info-panel h2 {{ margin: 0 0 14px; font-size: 20px; }}
    .info-list {{ display: grid; gap: 0; margin: 0; }}
    .info-row {{
      display: grid;
      grid-template-columns: minmax(120px, 180px) minmax(0, 1fr);
      gap: 12px;
      padding: 9px 0;
      border-top: 1px solid var(--border);
    }}
    .info-row:first-child {{ border-top: 0; }}
    .info-row dt {{ color: var(--muted); }}
    .info-row dd {{ margin: 0; overflow-wrap: anywhere; }}
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
      gap: 8px;
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
      transform-origin: center center;
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
    .assign-row {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .new-person-form {{
      display: grid;
      grid-template-columns: auto minmax(160px, 280px) auto;
      gap: 8px;
      align-items: center;
      justify-content: start;
    }}
    .new-person-form label {{
      color: var(--muted);
      font-size: 13px;
    }}
    .assign-person-button {{
      border-color: rgb(255 255 255 / 22%);
      background: rgb(255 255 255 / 10%);
      color: #fff;
      min-height: 34px;
      padding: 6px 10px;
    }}
    .assign-person-button:hover {{ background: rgb(255 255 255 / 18%); }}
    .assign-person-button:disabled {{ opacity: 0.55; cursor: default; }}
    .assign-status {{ color: var(--muted); font-size: 13px; min-height: 1.3em; }}
    @media (max-width: 640px) {{
      .shell {{ padding: 16px; }}
      .search {{ grid-template-columns: 1fr; }}
      .browser-header {{ align-items: stretch; }}
      .nav-button, .server-search-link, .person-link, .faces-button {{ flex: 1 1 auto; justify-content: center; text-align: center; }}
      .top-actions {{ margin-left: 0; width: 100%; justify-content: stretch; }}
      .people-row {{ grid-template-columns: 1fr; align-items: stretch; }}
      .removed-row {{ grid-template-columns: 1fr; align-items: stretch; }}
      .geo-row {{ grid-template-columns: 1fr; }}
      .new-person-form {{ grid-template-columns: 1fr; align-items: stretch; }}
      .info-row {{ grid-template-columns: 1fr; gap: 4px; }}
    }}
  </style>
</head>
<body>
{body}
<script>
  const faceOverlay = document.getElementById("faceOverlay");
  const infoOverlay = document.getElementById("infoOverlay");
  const openFacesButton = document.querySelector("[data-open-faces]");
  const closeFacesButton = document.querySelector("[data-close-faces]");
  const openInfoButton = document.querySelector("[data-open-info]");
  const closeInfoButton = document.querySelector("[data-close-info]");
  function openFacesOverlay() {{
    if (!faceOverlay) return;
    faceOverlay.hidden = false;
    closeFacesButton?.focus();
  }}
  function closeFacesOverlay() {{
    if (!faceOverlay) return;
    faceOverlay.hidden = true;
  }}
  function openInfoOverlay() {{
    if (!infoOverlay) return;
    infoOverlay.hidden = false;
    closeInfoButton?.focus();
  }}
  function closeInfoOverlay() {{
    if (!infoOverlay) return;
    infoOverlay.hidden = true;
  }}
  function ensureTopPersonLink(name, url) {{
    if (!name || !url) return;
    let people = document.querySelector(".topline .people");
    if (!people) {{
      people = document.createElement("div");
      people.className = "people";
      document.querySelector(".topline .title")?.after(people);
    }}
    const exists = Array.from(people.querySelectorAll(".person-link")).some(link => link.textContent === name);
    if (exists) return;
    const link = document.createElement("a");
    link.className = "person-link";
    link.href = url;
    link.textContent = name;
    people.append(link);
  }}
  openFacesButton?.addEventListener("click", openFacesOverlay);
  closeFacesButton?.addEventListener("click", closeFacesOverlay);
  openInfoButton?.addEventListener("click", openInfoOverlay);
  closeInfoButton?.addEventListener("click", closeInfoOverlay);
  document.querySelectorAll("[data-rotate-item]").forEach(button => {{
    button.addEventListener("click", async () => {{
      const fileId = Number(button.dataset.rotateItem);
      const direction = button.dataset.rotateDirection || "";
      button.disabled = true;
      try {{
        const response = await fetch("/api/item-rotate", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{file_id: fileId, direction}}),
        }});
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke rotere.");
        window.location.reload();
      }} catch (error) {{
        alert(error.message || "Kunne ikke rotere.");
        button.disabled = false;
      }}
    }});
  }});
  document.querySelectorAll("[data-delete-item]").forEach(button => {{
    button.addEventListener("click", async () => {{
      const fileId = Number(button.dataset.deleteItem);
      const path = button.dataset.deletePath || "";
      const redirectUrl = button.dataset.deleteRedirect || "/";
      if (!confirm(`Flytte til deleted/?\\n\\n${{path}}`)) return;
      button.disabled = true;
      try {{
        const response = await fetch("/api/item-delete", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{file_id: fileId}}),
        }});
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke slette.");
        window.location.href = redirectUrl;
      }} catch (error) {{
        alert(error.message || "Kunne ikke slette.");
        button.disabled = false;
      }}
    }});
  }});
  document.querySelectorAll("[data-undelete-item]").forEach(button => {{
    button.addEventListener("click", async () => {{
      const fileId = Number(button.dataset.undeleteItem);
      const path = button.dataset.undeletePath || "";
      if (!confirm(`Flytte tilbake til bildesamlingen?\\n\\n${{path}}`)) return;
      button.disabled = true;
      try {{
        const response = await fetch("/api/item-undelete", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{file_id: fileId}}),
        }});
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke angre sletting.");
        button.closest(".removed-row")?.remove();
      }} catch (error) {{
        alert(error.message || "Kunne ikke angre sletting.");
        button.disabled = false;
      }}
    }});
  }});
  document.querySelectorAll("[data-unconfirm-face]").forEach(button => {{
    button.addEventListener("click", async () => {{
      const faceId = Number(button.dataset.unconfirmFace);
      const personName = button.dataset.unconfirmPerson || "";
      if (!faceId || !personName) return;
      const command = `bildebank face-person-remove-face "${{personName}}" ${{faceId}}`;
      if (!confirm(`Avbekrefte face-id ${{faceId}} fra ${{personName}}?\\n\\nTilsvarer:\\n${{command}}`)) return;
      button.disabled = true;
      try {{
        const response = await fetch("/api/face-person-remove-face", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{face_id: faceId, person_name: personName}}),
        }});
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke avbekrefte.");
        window.location.reload();
      }} catch (error) {{
        alert(error.message || "Kunne ikke avbekrefte.");
        button.disabled = false;
      }}
    }});
  }});
  faceOverlay?.addEventListener("click", event => {{
    if (event.target === faceOverlay || event.target.classList?.contains("lightbox-stage")) closeFacesOverlay();
  }});
  infoOverlay?.addEventListener("click", event => {{
    if (event.target === infoOverlay) closeInfoOverlay();
  }});
  async function assignFace(detail, status, endpoint, faceId, personName) {{
    if (!detail || !status || !faceId || !personName) return;
    status.textContent = "Lagrer...";
    detail.querySelectorAll("button, input").forEach(item => item.disabled = true);
    try {{
      const response = await fetch(endpoint, {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{face_id: Number(faceId), person_name: personName}}),
      }});
      const payload = await response.json();
      if (!payload.ok) throw new Error(payload.error || "Kunne ikke lagre.");
      status.textContent = `Koblet til ${{payload.person_name}}.`;
      ensureTopPersonLink(payload.person_name, payload.person_url);
      detail.remove();
      if (!document.querySelector(".face-detail")) {{
        closeFacesOverlay();
        window.location.reload();
      }}
    }} catch (error) {{
      status.textContent = error.message || "Kunne ikke lagre.";
      detail.querySelectorAll("button, input").forEach(item => item.disabled = false);
    }}
  }}
  document.querySelectorAll(".assign-person-button").forEach(button => {{
    button.addEventListener("click", async () => {{
      const faceId = button.dataset.faceId;
      const personName = button.dataset.personName;
      const detail = button.closest(".face-detail");
      const status = detail?.querySelector(".assign-status");
      await assignFace(detail, status, "/api/face-person-add-face", faceId, personName);
    }});
  }});
  document.querySelectorAll("[data-new-person-form]").forEach(form => {{
    form.addEventListener("submit", async event => {{
      event.preventDefault();
      const detail = form.closest(".face-detail");
      const status = detail?.querySelector(".assign-status");
      const faceId = form.querySelector('input[name="face_id"]')?.value;
      const personName = form.querySelector('input[name="person_name"]')?.value?.trim();
      await assignFace(detail, status, "/api/face-person-create-and-add-face", faceId, personName);
    }});
  }});
  document.addEventListener("keydown", event => {{
    if (faceOverlay && !faceOverlay.hidden) {{
      if (event.key === "Escape") {{
        event.preventDefault();
        closeFacesOverlay();
      }}
      return;
    }}
    if (infoOverlay && !infoOverlay.hidden) {{
      if (event.key === "Escape") {{
        event.preventDefault();
        closeInfoOverlay();
      }}
      return;
    }}
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const target = event.target;
    if (
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement ||
      target instanceof HTMLButtonElement ||
      target?.isContentEditable
    ) return;
    const selector = {{
      ArrowLeft: '[data-key-nav="previous"]',
      ArrowRight: '[data-key-nav="next"]',
      ArrowUp: '[data-key-nav="previous-month"]',
      ArrowDown: '[data-key-nav="next-month"]',
      PageUp: '[data-key-nav="previous-year"]',
      PageDown: '[data-key-nav="next-year"]',
    }}[event.key] || "";
    if (!selector) return;
    const link = document.querySelector(selector);
    if (!(link instanceof HTMLAnchorElement)) return;
    event.preventDefault();
    window.location.href = link.href;
  }});
</script>
</body>
</html>
"""


def run_server(
    target: Path,
    config: OpenClipConfig,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    ready: Callable[[str], None] | None = None,
) -> None:
    server = BildebankServer((host, port), target, config)
    actual_host, actual_port = server.server_address
    url = f"http://{actual_host}:{actual_port}/"
    if ready is not None:
        ready(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
