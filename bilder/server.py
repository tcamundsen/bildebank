from __future__ import annotations

import html
import json
import mimetypes
import re
import sqlite3
import threading
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import OpenClipConfig
from .face import add_face_to_person, normalize_person_name
from .html_export import (
    FACE_DB_FILENAME,
    browser_face_items,
    display_relative_path,
    face_tables_exist,
    format_bytes,
    month_key_from_path,
)
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


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_SEARCH_LIMIT = 100


@dataclass(frozen=True)
class ServerSearchStats:
    query: str
    results: tuple[ImageSearchResult, ...]


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
            if parsed.path.startswith("/item/"):
                self.respond_item(parsed.path.removeprefix("/item/"))
                return
            if parsed.path.startswith("/month/"):
                self.respond_month(parsed.path.removeprefix("/month/"))
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
            if parsed.path == "/api/face-person-add-face":
                self.respond_add_face_to_person()
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
        item = first_browser_item(self.server.target)
        if item is None:
            self.respond_html(empty_browser_html())
            return
        self.redirect(f"/item/{item['id']}")

    def respond_item(self, raw_file_id: str) -> None:
        file_id = parse_file_id(raw_file_id)
        item = browser_item_by_id(self.server.target, file_id)
        if item is None:
            self.respond_text("Filen finnes ikke i bildesamlingen.", status=HTTPStatus.NOT_FOUND)
            return
        previous_item, next_item = adjacent_browser_items(self.server.target, item)
        month_nav = browser_month_navigation(self.server.target, item)
        self.respond_html(item_page_html(self.server.target, item, previous_item, next_item, month_nav))

    def respond_month(self, raw_month: str) -> None:
        month_key = urllib.parse.unquote(raw_month).strip()
        if not valid_month_key(month_key):
            self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
            return
        items = browser_month_items(self.server.target, month_key)
        self.respond_html(month_page_html(self.server.target, month_key, items))

    def respond_person(self, raw_path: str) -> None:
        raw_name, mode, raw_value = parse_person_path(raw_path)
        person_name = urllib.parse.unquote(raw_name).strip()
        if not person_name:
            self.respond_text("Personnavn mangler.", status=HTTPStatus.BAD_REQUEST)
            return
        person = person_by_name(self.server.target, person_name)
        if person is None:
            self.respond_html(person_not_found_html(person_name), status=HTTPStatus.NOT_FOUND)
            return
        canonical_name = str(person["name"])
        if mode is None:
            item = first_person_item(self.server.target, canonical_name)
            if item is None:
                self.respond_html(empty_person_browser_html(canonical_name))
                return
            self.redirect(person_item_url(canonical_name, int(item["id"])))
            return
        if mode == "item":
            file_id = parse_file_id(raw_value)
            item = person_item_by_id(self.server.target, canonical_name, file_id)
            if item is None:
                self.respond_text("Filen finnes ikke for denne personen.", status=HTTPStatus.NOT_FOUND)
                return
            previous_item, next_item = adjacent_person_items(self.server.target, canonical_name, item)
            month_nav = person_month_navigation(self.server.target, canonical_name, item)
            self.respond_html(person_item_page_html(self.server.target, canonical_name, item, previous_item, next_item, month_nav))
            return
        if mode == "month":
            month_key = urllib.parse.unquote(raw_value).strip()
            if not valid_month_key(month_key):
                self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
                return
            items = person_month_items(self.server.target, canonical_name, month_key)
            self.respond_html(person_month_page_html(self.server.target, canonical_name, month_key, items))
            return
        self.respond_text("Ugyldig personside.", status=HTTPStatus.NOT_FOUND)

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
            self.respond_json({"ok": False, "error": "Ugyldig face_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        if not person_name:
            self.respond_json({"ok": False, "error": "Personnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
            return
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


def parse_file_id(value: str) -> int:
    try:
        file_id = int(value)
    except ValueError as exc:
        raise ValueError("Ugyldig file_id.") from exc
    if file_id < 1:
        raise ValueError("Ugyldig file_id.")
    return file_id


def parse_person_path(raw_path: str) -> tuple[str, str | None, str]:
    if "/item/" in raw_path:
        raw_name, raw_value = raw_path.split("/item/", 1)
        return raw_name, "item", raw_value
    if "/month/" in raw_path:
        raw_name, raw_value = raw_path.split("/month/", 1)
        return raw_name, "month", raw_value
    return raw_path.strip("/"), None, ""


FILE_COLUMNS = "id, target_path, target_path_key, stored_filename, taken_date, date_source, size_bytes"
MONTH_PATH_RE = re.compile(r"(?:^|[\\/])(?P<year>\d{4})[\\/](?P<month>\d{2})(?:[\\/]|$)")


def first_browser_item(target: Path) -> Any | None:
    conn = db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
            ORDER BY target_path_key
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()


def browser_item_by_id(target: Path, file_id: int) -> Any | None:
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
    key = item_order_key(item)
    conn = db.connect(target)
    try:
        previous_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND target_path_key < ?
            ORDER BY target_path_key DESC
            LIMIT 1
            """,
            (key,),
        ).fetchone()
        next_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND target_path_key > ?
            ORDER BY target_path_key
            LIMIT 1
            """,
            (key,),
        ).fetchone()
        return previous_item, next_item
    finally:
        conn.close()


def item_order_key(item: Any) -> str:
    return str(item["target_path_key"])


def browser_month_keys(target: Path) -> list[str]:
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
    return month_key_from_path(
        Path(display_relative_path(target, db.absolute_target_path(target, Path(str(item["target_path"])))))
    )


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


def person_file_ids(target: Path, person_name: str) -> list[int]:
    person = person_by_name(target, person_name)
    if person is None:
        return []
    conn = sqlite3.connect(target / FACE_DB_FILENAME)
    conn.row_factory = sqlite3.Row
    try:
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
        return [int(row["file_id"]) for row in rows]
    finally:
        conn.close()


def person_items(target: Path, person_name: str) -> list[Any]:
    file_ids = person_file_ids(target, person_name)
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
                ORDER BY target_path_key
                """,
                tuple(file_ids),
            )
        )
    finally:
        conn.close()


def first_person_item(target: Path, person_name: str) -> Any | None:
    items = person_items(target, person_name)
    return items[0] if items else None


def person_item_by_id(target: Path, person_name: str, file_id: int) -> Any | None:
    return next((item for item in person_items(target, person_name) if int(item["id"]) == file_id), None)


def adjacent_person_items(target: Path, person_name: str, item: Any) -> tuple[Any | None, Any | None]:
    items = person_items(target, person_name)
    index = next((idx for idx, candidate in enumerate(items) if int(candidate["id"]) == int(item["id"])), -1)
    if index < 0:
        return None, None
    previous_item = items[index - 1] if index > 0 else None
    next_item = items[index + 1] if index < len(items) - 1 else None
    return previous_item, next_item


def person_month_keys(target: Path, person_name: str) -> list[str]:
    keys = {month_key_for_item(target, item) for item in person_items(target, person_name)}
    return sorted(key for key in keys if valid_month_key(key))


def person_month_navigation(target: Path, person_name: str, item: Any) -> dict[str, str | None]:
    return person_month_navigation_for_key(target, person_name, month_key_for_item(target, item))


def person_month_navigation_for_key(target: Path, person_name: str, current_key: str) -> dict[str, str | None]:
    if not valid_month_key(current_key):
        return {"previous_year": None, "next_year": None, "previous_month": None, "next_month": None}
    keys = person_month_keys(target, person_name)
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
    return [item for item in person_items(target, person_name) if month_key_for_item(target, item) == month_key]


def person_faces_for_item(target: Path, person_name: str, item: Any) -> list[dict[str, object]]:
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
            ORDER BY status, id
            """,
            (int(person["id"]), int(item["id"]), int(person["id"]), int(item["id"])),
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
    prefix = db.path_key(target / year / month) + "/"
    conn = db.connect(target)
    try:
        rows = list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                  AND target_path_key LIKE ?
                ORDER BY target_path_key
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
                ORDER BY target_path_key
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
                    db.absolute_target_path(server.target, Path(str(row["target_path"]))),
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
                (run_id, file_id, str(target_path), target_path_key, score, index),
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
    target_path = Path(str(item["target_path"]))
    relative = display_relative_path(target, target_path)
    media = item_media_html(item)
    controls = browser_controls_html(month_nav, previous_item, next_item)
    people = people_links_html(confirmed_people_for_file(target, int(item["id"])))
    unconfirmed_faces = unconfirmed_faces_for_item(target, item)
    all_people = registered_people(target)
    faces_button = faces_button_html(unconfirmed_faces)
    faces_overlay = faces_overlay_html(item, unconfirmed_faces, all_people)
    return page_html(
        f"Bildebrowser: {target_path.name}",
        f"""
        <main class="server-browser">
          <header class="browser-header">
            <div class="topline">
              <div class="title">Bildebrowser</div>
              {people}
              {faces_button}
              <a class="server-search-link" href="/search">Bildesøk</a>
            </div>
            {controls}
          </header>
          <section class="stage">{media}</section>
          <footer class="browser-footer">
            <a class="filename" href="/file/{int(item["id"])}" target="_blank">{html.escape(relative)}</a>
          </footer>
        </main>
        {faces_overlay}
        """,
    )


def item_media_html(item: Any) -> str:
    file_id = int(item["id"])
    target_path = Path(str(item["target_path"]))
    url = f"/file/{file_id}"
    name = html.escape(str(item["stored_filename"]))
    if target_path.suffix.lower().lstrip(".") in {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}:
        return f'<video src="{url}" controls></video>'
    return f'<a href="{url}" target="_blank"><img src="{url}" alt="{name}"></a>'


def person_item_page_html(
    target: Path,
    person_name: str,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
) -> str:
    target_path = Path(str(item["target_path"]))
    relative = display_relative_path(target, target_path)
    media = person_item_media_html(item, person_faces_for_item(target, person_name, item))
    controls = person_controls_html(person_name, month_nav, previous_item, next_item)
    return page_html(
        f"{person_name}: {target_path.name}",
        f"""
        <main class="server-browser">
          <header class="browser-header">
            <div class="topline">
              <div class="title">{html.escape(person_name)}</div>
              <a class="server-search-link" href="/">Alle bilder</a>
              <a class="server-search-link" href="/search">Bildesøk</a>
            </div>
            {controls}
          </header>
          <section class="stage">{media}</section>
          <footer class="browser-footer">
            <a class="filename" href="/file/{int(item["id"])}" target="_blank">{html.escape(relative)}</a>
          </footer>
        </main>
        """,
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
    <div class="person-media">
      <a href="{url}" target="_blank"><img src="{url}" alt="{name}"></a>
      {boxes}
    </div>
    """


def person_face_box_html(face: dict[str, object]) -> str:
    if not {"left", "top", "boxWidth", "boxHeight"} <= face.keys():
        return ""
    css_class = "person-face-box suggested" if face.get("status") == "forslag" else "person-face-box"
    title = f'{face.get("status", "")} face-id {face["faceId"]} score {float(face.get("similarity", 0.0)):.3f}'
    return (
        f'<div class="{css_class}" title="{html.escape(title)}" style="'
        f'left: {float(face["left"]):.4f}%; '
        f'top: {float(face["top"]):.4f}%; '
        f'width: {float(face["boxWidth"]):.4f}%; '
        f'height: {float(face["boxHeight"]):.4f}%;'
        '"></div>'
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
    return f"""
    <nav class="controls" aria-label="Navigering">
      {month_nav_link(month_nav["previous_year"], "Forrige år", "previous-year")}
      {month_nav_link(month_nav["next_year"], "Neste år", "next-year")}
      {month_nav_link(month_nav["previous_month"], "Forrige måned", "previous-month")}
      {month_nav_link(month_nav["next_month"], "Neste måned", "next-month")}
      {nav_link(previous_item, "Forrige bilde", "previous")}
      {nav_link(next_item, "Neste bilde", "next")}
    </nav>
    """


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
    face_items = "\n".join(face_overlay_item_html(image_url, face, people) for face in faces)
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


def face_overlay_item_html(image_url: str, face: dict[str, object], people: list[dict[str, str]]) -> str:
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
      <div class="lightbox-media">
        <img src="{html.escape(image_url)}" alt="">
        {box}
      </div>
      <div class="assign-row">{people_buttons}</div>
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
    cards = "\n".join(month_item_html(target, item) for item in items)
    previous_item = items[-1] if items else None
    next_item = items[0] if items else None
    controls = browser_controls_html(browser_month_navigation_for_key(target, month_key), previous_item, next_item)
    return page_html(
        f"Månedsoversikt: {month_key}",
        f"""
        <main class="server-browser">
          <header class="browser-header">
            <div class="topline">
              <div class="title">Bildebrowser</div>
              <span class="status">Månedsoversikt: {html.escape(month_key)}</span>
              <a class="server-search-link" href="/search">Bildesøk</a>
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


def empty_person_browser_html(person_name: str) -> str:
    return page_html(
        person_name,
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a></p>
          <h1>{html.escape(person_name)}</h1>
          <p class="meta">Ingen bekreftede ansikter eller forslag for denne personen ennå.</p>
        </main>
        """,
    )


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


def person_month_page_html(target: Path, person_name: str, month_key: str, items: list[Any]) -> str:
    cards = "\n".join(person_month_item_html(target, person_name, item) for item in items)
    previous_item = items[-1] if items else None
    next_item = items[0] if items else None
    controls = person_controls_html(
        person_name,
        person_month_navigation_for_key(target, person_name, month_key),
        previous_item,
        next_item,
    )
    return page_html(
        f"{person_name}: {month_key}",
        f"""
        <main class="server-browser">
          <header class="browser-header">
            <div class="topline">
              <div class="title">{html.escape(person_name)}</div>
              <span class="status">Månedsoversikt: {html.escape(month_key)}</span>
              <a class="server-search-link" href="/">Alle bilder</a>
              <a class="server-search-link" href="/search">Bildesøk</a>
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


def person_month_item_html(target: Path, person_name: str, item: Any) -> str:
    target_path = Path(str(item["target_path"]))
    label = html.escape(display_relative_path(target, target_path))
    media = thumbnail_media_html(item)
    return f"""
    <article class="item">
      <a class="thumb-link" href="{person_item_url(person_name, int(item["id"]))}">{media}</a>
      <div class="text">
        <div class="path">{label}</div>
        <div class="score">{html.escape(format_bytes(int(item["size_bytes"])))}</div>
      </div>
    </article>
    """


def month_item_html(target: Path, item: Any) -> str:
    target_path = Path(str(item["target_path"]))
    label = html.escape(display_relative_path(target, target_path))
    media = thumbnail_media_html(item)
    return f"""
    <article class="item">
      <a class="thumb-link" href="/item/{int(item["id"])}">{media}</a>
      <div class="text">
        <div class="path">{label}</div>
        <div class="score">{html.escape(format_bytes(int(item["size_bytes"])))}</div>
      </div>
    </article>
    """


def thumbnail_media_html(item: Any) -> str:
    file_id = int(item["id"])
    target_path = Path(str(item["target_path"]))
    url = f"/file/{file_id}"
    name = html.escape(str(item["stored_filename"]))
    if target_path.suffix.lower().lstrip(".") in {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}:
        return f'<div class="video-thumb">Video<br>{name}</div>'
    return f'<img src="{url}" alt="{name}" loading="lazy">'


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
    .people {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
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
    }}
    .person-media {{
      position: relative;
      display: inline-block;
      max-width: min(100%, 92vw);
      max-height: calc(100vh - 10rem);
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
    .item img {{ width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block; background: #181818; }}
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
      max-width: 100%;
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
    }}
  </style>
</head>
<body>
{body}
<script>
  const faceOverlay = document.getElementById("faceOverlay");
  const openFacesButton = document.querySelector("[data-open-faces]");
  const closeFacesButton = document.querySelector("[data-close-faces]");
  function openFacesOverlay() {{
    if (!faceOverlay) return;
    faceOverlay.hidden = false;
    closeFacesButton?.focus();
  }}
  function closeFacesOverlay() {{
    if (!faceOverlay) return;
    faceOverlay.hidden = true;
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
  faceOverlay?.addEventListener("click", event => {{
    if (event.target === faceOverlay || event.target.classList?.contains("lightbox-stage")) closeFacesOverlay();
  }});
  document.querySelectorAll(".assign-person-button").forEach(button => {{
    button.addEventListener("click", async () => {{
      const faceId = button.dataset.faceId;
      const personName = button.dataset.personName;
      const detail = button.closest(".face-detail");
      const status = detail?.querySelector(".assign-status");
      if (!faceId || !personName || !status) return;
      status.textContent = "Lagrer...";
      detail.querySelectorAll(".assign-person-button").forEach(item => item.disabled = true);
      try {{
        const response = await fetch("/api/face-person-add-face", {{
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
        detail.querySelectorAll(".assign-person-button").forEach(item => item.disabled = false);
      }}
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
