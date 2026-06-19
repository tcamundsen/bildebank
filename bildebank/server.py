from __future__ import annotations

import time
import urllib.parse
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import (
    AppConfig,
    BrowserHotkeyConfig,
    FaceRecognitionConfig,
    HOTKEY_KEYS,
    set_face_suggest_threshold,
    validate_face_suggest_threshold,
)
from .face import (
    add_face_to_person,
    add_person_to_file,
    create_person,
    delete_person,
    remove_face_from_person,
    remove_person_from_file,
    rename_person,
    suggest_faces,
)
from .geo import (
    H3_COLUMNS,
    h3_resolution,
)
from . import server_app
from . import server_actions
from .server_pages import (
    app_status_page_html,
    custom_geo_places_page_html,
    empty_person_browser_html,
    empty_source_html,
    error_html,
    filter_start_html,
    geo_area_page_html,
    geo_index_page_html,
    h3_cells_page_html,
    geo_map_page_html,
    geo_missing_page_html,
    geo_stats_page_html,
    index_html,
    markdown_doc_page_html,
    people_page_html,
    person_not_found_html,
    removed_files_page_html,
    search_html,
    source_item_page_html,
    source_month_page_html,
    source_year_months_page_html,
    sources_page_html,
    tags_page_html,
    year_months_page_html,
    years_page_html,
)
from .server_browser import (
    adjacent_items_from_id_order,
    adjacent_source_items,
    browser_item_by_id,
    browser_item_ids,
    browser_month_keys,
    first_source_item,
    image_info_content_html,
    imported_source_by_id,
    item_by_id,
    month_key_for_item,
    month_navigation_for_keys,
    source_item_count as browser_source_item_count,
    source_item_ids,
    source_item_by_id,
    source_month_keys,
    source_month_items,
    source_month_navigation,
    valid_month_key,
    valid_year_key,
)
from .server_browser_sources import (
    BrowserSource,
    all_browser_source,
    geo_place_browser_source,
    imported_source_browser_source,
    parse_person_path,
    parse_source_path,
    person_browser_source,
    person_item_url,
    person_url,
    source_has_sql_filter,
    source_item_url,
    tag_browser_source,
)
from .server_faces import (
    clear_face_caches,
    current_face_db_path,
    face_overlay_content_html,
    person_by_name,
    person_item_url_for_face,
)
from . import server_geo
from .server_geo import DEFAULT_GEO_LIMIT, DEFAULT_GEO_MIN_COUNT, DEFAULT_GEO_RESOLUTION, geo_place_by_slug
from . import server_markdown
from . import server_files
from .server_search import (
    DEFAULT_SEARCH_LIMIT,
    OpenClipSearchCache,
    search_server_images,
)
from .server_filter import text_filter_browser_source
from .server_response import ServerResponseMixin
from . import server_request
from .server_request import first_param, nonnegative_int_param, parse_file_id, positive_int_param
from .server_assets import SERVER_CSS, SERVER_JS


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
BROWSER_NAVIGATION_CACHE_CHECK_INTERVAL_SECONDS = 1.0


def client_disconnected_error(exc: OSError) -> bool:
    return isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError))


def clear_browser_navigation_cache(server: Any) -> None:
    clear_cache = getattr(server, "clear_browser_navigation_cache", None)
    if clear_cache is not None:
        clear_cache()


def hotkey_filter_source_from_url(target: Path, source_url: object) -> BrowserSource | None:
    if not isinstance(source_url, str):
        return None
    parsed = urllib.parse.urlsplit(source_url.strip())
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    raw_path = parsed.path
    if not raw_path.startswith("/filter/"):
        return None
    raw_query, page_mode, raw_value = parse_source_path(raw_path.removeprefix("/filter/"))
    if page_mode is not None or raw_value:
        return None
    query = urllib.parse.unquote(raw_query).strip()
    if not query:
        return None
    try:
        source = text_filter_browser_source(query, target)
    except ValueError:
        return None
    return source if source.root_url == raw_path else None


class BildebankServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], target: Path, config: AppConfig) -> None:
        super().__init__(address, BildebankRequestHandler)
        self.target = target
        self.config = config
        self.search_cache = OpenClipSearchCache(config)
        self._browser_navigation_cache_version = 0
        self._browser_navigation_db_mtime_ns: int | None = None
        self._browser_navigation_face_db_mtime_ns: int | None = None
        self._browser_navigation_checked_at = 0.0
        self._browser_item_ids: dict[bool, tuple[int, list[int], dict[int, int]]] = {}
        self._browser_month_keys: dict[bool, tuple[int, list[str]]] = {}
        self._source_item_ids: dict[tuple[BrowserSource, bool], tuple[int, list[int], dict[int, int]]] = {}
        self._source_month_keys: dict[tuple[BrowserSource, bool], tuple[int, list[str]]] = {}
        self._source_item_counts: dict[tuple[BrowserSource, bool], tuple[int, int]] = {}

    @property
    def face_enabled(self) -> bool:
        return self.config.face_recognition.enabled

    @property
    def openclip_enabled(self) -> bool:
        return self.config.openclip.enabled

    @property
    def hide_out_of_focus(self) -> bool:
        return self.config.browser.hide_out_of_focus

    def browser_month_keys(self, *, hide_out_of_focus: bool = False) -> list[str]:
        version = self.browser_navigation_cache_version()
        cached = self._browser_month_keys.get(hide_out_of_focus)
        if cached is None or cached[0] != version:
            cached = (
                version,
                browser_month_keys(
                    self.target,
                    hide_out_of_focus=hide_out_of_focus,
                ),
            )
            self._browser_month_keys[hide_out_of_focus] = cached
        return cached[1]

    def browser_item_ids(self, *, hide_out_of_focus: bool = False) -> list[int]:
        return self.browser_item_order(hide_out_of_focus=hide_out_of_focus)[0]

    def browser_item_order(self, *, hide_out_of_focus: bool = False) -> tuple[list[int], dict[int, int]]:
        version = self.browser_navigation_cache_version()
        cached = self._browser_item_ids.get(hide_out_of_focus)
        if cached is None or cached[0] != version:
            item_ids = browser_item_ids(
                self.target,
                hide_out_of_focus=hide_out_of_focus,
            )
            cached = (
                version,
                item_ids,
                {file_id: index for index, file_id in enumerate(item_ids)},
            )
            self._browser_item_ids[hide_out_of_focus] = cached
        return cached[1], cached[2]

    def source_month_keys(self, source: BrowserSource, *, hide_out_of_focus: bool = False) -> list[str]:
        version = self.browser_navigation_cache_version()
        cache_key = (source, hide_out_of_focus)
        cached = self._source_month_keys.get(cache_key)
        if cached is None or cached[0] != version:
            cached = (
                version,
                source_month_keys(
                    self.target,
                    source,
                    self.config.face_recognition,
                    hide_out_of_focus=hide_out_of_focus,
                ),
            )
            self._source_month_keys[cache_key] = cached
        return cached[1]

    def source_item_order(self, source: BrowserSource, *, hide_out_of_focus: bool = False) -> tuple[list[int], dict[int, int]]:
        version = self.browser_navigation_cache_version()
        cache_key = (source, hide_out_of_focus)
        cached = self._source_item_ids.get(cache_key)
        if cached is None or cached[0] != version:
            item_ids = source_item_ids(
                self.target,
                source,
                self.config.face_recognition,
                hide_out_of_focus=hide_out_of_focus,
            )
            cached = (
                version,
                item_ids,
                {file_id: index for index, file_id in enumerate(item_ids)},
            )
            self._source_item_ids[cache_key] = cached
        return cached[1], cached[2]

    def source_item_count(self, source: BrowserSource, *, hide_out_of_focus: bool = False) -> int:
        version = self.browser_navigation_cache_version()
        cache_key = (source, hide_out_of_focus)
        cached = self._source_item_counts.get(cache_key)
        if cached is None or cached[0] != version:
            cached = (
                version,
                browser_source_item_count(
                    self.target,
                    source,
                    self.config.face_recognition,
                    hide_out_of_focus=hide_out_of_focus,
                ),
            )
            self._source_item_counts[cache_key] = cached
        return cached[1]

    def browser_navigation_cache_version(self) -> int:
        version = getattr(self, "_browser_navigation_cache_version", 0)
        now = time.monotonic()
        checked_at = getattr(self, "_browser_navigation_checked_at", 0.0)
        if now - checked_at < BROWSER_NAVIGATION_CACHE_CHECK_INTERVAL_SECONDS:
            return version
        self._browser_navigation_checked_at = now
        try:
            mtime_ns = db.db_path_for_target(self.target).stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        config = getattr(self, "config", None)
        face_config = config.face_recognition if config is not None else None
        try:
            face_db_mtime_ns = (
                current_face_db_path(self.target, face_config).stat().st_mtime_ns
                if face_config is not None and face_config.enabled
                else None
            )
        except OSError:
            face_db_mtime_ns = None
        previous_mtime_ns = getattr(self, "_browser_navigation_db_mtime_ns", None)
        previous_face_db_mtime_ns = getattr(self, "_browser_navigation_face_db_mtime_ns", None)
        self._browser_navigation_db_mtime_ns = mtime_ns
        self._browser_navigation_face_db_mtime_ns = face_db_mtime_ns
        if (
            (previous_mtime_ns is not None and mtime_ns != previous_mtime_ns)
            or (previous_face_db_mtime_ns is not None and face_db_mtime_ns != previous_face_db_mtime_ns)
        ):
            self._browser_item_ids.clear()
            self._browser_month_keys.clear()
            getattr(self, "_source_item_ids", {}).clear()
            getattr(self, "_source_month_keys", {}).clear()
            getattr(self, "_source_item_counts", {}).clear()
            version += 1
            self._browser_navigation_cache_version = version
        return version

    def clear_browser_navigation_cache(self) -> None:
        self._browser_item_ids.clear()
        self._browser_month_keys.clear()
        getattr(self, "_source_item_ids", {}).clear()
        getattr(self, "_source_month_keys", {}).clear()
        getattr(self, "_source_item_counts", {}).clear()
        self._browser_navigation_cache_version = getattr(self, "_browser_navigation_cache_version", 0) + 1
        try:
            self._browser_navigation_db_mtime_ns = db.db_path_for_target(self.target).stat().st_mtime_ns
        except OSError:
            self._browser_navigation_db_mtime_ns = None
        config = getattr(self, "config", None)
        face_config = config.face_recognition if config is not None else None
        try:
            self._browser_navigation_face_db_mtime_ns = (
                current_face_db_path(self.target, face_config).stat().st_mtime_ns
                if face_config is not None and face_config.enabled
                else None
            )
        except OSError:
            self._browser_navigation_face_db_mtime_ns = None
        self._browser_navigation_checked_at = time.monotonic()


class BildebankRequestHandler(ServerResponseMixin, BaseHTTPRequestHandler):
    server: BildebankServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.request_started_at = time.perf_counter()
        self.server_timing_steps = {}
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/":
                self.respond_browser_root()
                return
            if parsed.path == "/people":
                if not self.server.face_enabled:
                    self.respond_text("Ansiktsgjenkjenning er av.", status=HTTPStatus.NOT_FOUND)
                    return
                self.respond_html(
                    people_page_html(
                        self.server.target,
                        self.server.config.face_recognition,
                        openclip_enabled=self.server.openclip_enabled,
                    )
                )
                return
            if parsed.path in {"/sources", "/sources/"}:
                self.respond_html(
                    sources_page_html(
                        self.server.target,
                        face_enabled=self.server.face_enabled,
                        openclip_enabled=self.server.openclip_enabled,
                    )
                )
                return
            if parsed.path in {"/tags", "/tags/"}:
                self.respond_html(
                    tags_page_html(
                        self.server.target,
                        face_enabled=self.server.face_enabled,
                        openclip_enabled=self.server.openclip_enabled,
                    )
                )
                return
            if parsed.path == "/settings":
                self.respond_html(app_status_page_html(self.server.target, self.server.config))
                return
            if parsed.path in {"/settings/h3-cells", "/settings/h3-cells/"}:
                self.respond_html(
                    h3_cells_page_html(
                        self.server.target,
                        face_enabled=self.server.face_enabled,
                        openclip_enabled=self.server.openclip_enabled,
                    )
                )
                return
            if parsed.path in {"/settings/removed", "/settings/removed/"}:
                self.respond_html(
                    removed_files_page_html(
                        self.server.target,
                        face_enabled=self.server.face_enabled,
                        openclip_enabled=self.server.openclip_enabled,
                    )
                )
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
                self.respond_html(
                    geo_stats_page_html(
                        self.server.target,
                        face_enabled=self.server.face_enabled,
                        openclip_enabled=self.server.openclip_enabled,
                    )
                )
                return
            if parsed.path == "/geo/missing":
                self.respond_geo_missing(parsed.query)
                return
            if parsed.path == "/geo/custom-places":
                self.respond_html(
                    custom_geo_places_page_html(
                        self.server.target,
                        face_enabled=self.server.face_enabled,
                        openclip_enabled=self.server.openclip_enabled,
                    )
                )
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
            if parsed.path in {"/years", "/years/"}:
                self.respond_years()
                return
            if parsed.path.startswith("/years/"):
                self.respond_year(parsed.path.removeprefix("/years/"))
                return
            if parsed.path == "/filter":
                self.respond_filter(parsed.query)
                return
            if parsed.path.startswith("/filter/"):
                self.respond_filter_source(parsed.path.removeprefix("/filter/"))
                return
            if parsed.path.startswith("/source/"):
                self.respond_imported_source(parsed.path.removeprefix("/source/"))
                return
            if parsed.path.startswith("/tag/"):
                self.respond_tag(parsed.path.removeprefix("/tag/"))
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
            self.respond_html(
                error_html(exc, face_enabled=self.server.face_enabled, openclip_enabled=self.server.openclip_enabled),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        self.request_started_at = time.perf_counter()
        self.server_timing_steps = {}
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/people/face-suggest":
                self.respond_face_suggest()
                return
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
            if parsed.path == "/settings/hide-out-of-focus":
                self.respond_set_hide_out_of_focus()
                return
            if parsed.path == "/settings/manual-person-controls":
                self.respond_set_manual_person_controls()
                return
            if parsed.path == "/settings/hotkey":
                self.respond_set_hotkey()
                return
            if parsed.path == "/settings/hotkey-hints":
                self.respond_set_hotkey_hints()
                return
            if parsed.path == "/settings/h3-cell":
                self.respond_set_h3_cell_name()
                return
            if parsed.path == "/settings/h3-cell-delete":
                self.respond_delete_h3_cell_name()
                return
            if parsed.path == "/settings/face-model":
                self.respond_set_face_model()
                return
            if parsed.path == "/tags/create":
                self.respond_create_tag()
                return
            if parsed.path == "/tags/rename":
                self.respond_rename_tag()
                return
            if parsed.path == "/tags/delete":
                self.respond_delete_tag()
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
            if parsed.path == "/api/face-person-add-file":
                if not self.server.face_enabled:
                    self.respond_json({"ok": False, "error": "Ansiktsgjenkjenning er av."}, status=HTTPStatus.FORBIDDEN)
                    return
                self.respond_add_person_to_file()
                return
            if parsed.path == "/api/face-person-remove-file":
                if not self.server.face_enabled:
                    self.respond_json({"ok": False, "error": "Ansiktsgjenkjenning er av."}, status=HTTPStatus.FORBIDDEN)
                    return
                self.respond_remove_person_from_file()
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
            if parsed.path == "/api/item-tag":
                self.respond_tag_item()
                return
            if parsed.path == "/api/item-manual-location-remove":
                self.respond_remove_manual_location_item()
                return
            if parsed.path == "/api/item-manual-date":
                self.respond_manual_date_item()
                return
            if parsed.path == "/api/item-manual-date-clear":
                self.respond_clear_manual_date_item()
                return
            if parsed.path == "/api/item-hotkey-action":
                self.respond_hotkey_action()
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

    def respond_face_suggest(self) -> None:
        if not self.server.face_enabled:
            self.respond_html(
                error_html(
                    ValueError("Ansiktsgjenkjenning er av."),
                    face_enabled=False,
                    openclip_enabled=self.server.openclip_enabled,
                ),
                status=HTTPStatus.FORBIDDEN,
            )
            return
        params = server_request.read_form_params(self.headers, self.rfile)
        try:
            threshold = validate_face_suggest_threshold(first_param(params, "threshold"))
        except ValueError as exc:
            self.respond_html(
                error_html(exc, face_enabled=True, openclip_enabled=self.server.openclip_enabled),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        set_face_suggest_threshold(server_app.server_program_repo_root(), threshold)
        face_config = replace(self.server.config.face_recognition, suggest_threshold=threshold)
        self.server.config = replace(self.server.config, face_recognition=face_config)
        try:
            stats = suggest_faces(self.server.target, threshold=threshold, config=face_config)
        except ValueError as exc:
            self.respond_html(
                error_html(exc, face_enabled=True, openclip_enabled=self.server.openclip_enabled),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        clear_face_caches()
        self.server.clear_browser_navigation_cache()
        message = (
            f"Face-suggest ferdig: personer={stats.persons}, ukjente ansikter={stats.unknown_faces}, "
            f"forslag={stats.suggestions}, threshold={stats.threshold:.3f}"
        )
        self.respond_html(
            people_page_html(
                self.server.target,
                face_config,
                openclip_enabled=self.server.openclip_enabled,
                message=message,
            )
        )

    def handle(self) -> None:
        try:
            super().handle()
        except OSError as exc:
            if client_disconnected_error(exc):
                self.close_connection = True
                return
            raise

    def log_message(self, format: str, *args: Any) -> None:
        return

    def record_server_timing(self, name: str, start: float) -> None:
        if not self.wants_benchmark_timing():
            return
        steps = getattr(self, "server_timing_steps", None)
        if steps is None:
            steps = {}
            self.server_timing_steps = steps
        steps[name] = (time.perf_counter() - start) * 1000.0

    def respond_browser_root(self) -> None:
        self.respond_years()

    def respond_item(self, raw_file_id: str) -> None:
        start = time.perf_counter()
        file_id = parse_file_id(raw_file_id)
        self.record_server_timing("parse", start)

        start = time.perf_counter()
        source = all_browser_source()
        conn = db.connect(self.server.target)
        self.record_server_timing("db_connect", start)
        try:
            start = time.perf_counter()
            item_ids, item_positions = self.server.browser_item_order(hide_out_of_focus=self.server.hide_out_of_focus)
            self.record_server_timing("browser_item_order", start)

            start = time.perf_counter()
            item = item_by_id(self.server.target, file_id, conn=conn) if file_id in item_positions else None
            self.record_server_timing("item_by_id", start)
            if item is None:
                self.respond_text("Filen finnes ikke i bildesamlingen.", status=HTTPStatus.NOT_FOUND)
                return
            if source == all_browser_source():
                start = time.perf_counter()
                previous_item, next_item = adjacent_items_from_id_order(
                    item_ids,
                    int(item["id"]),
                    item_positions,
                )
                self.record_server_timing("adjacent", start)

                start = time.perf_counter()
                month_nav = month_navigation_for_keys(
                    self.server.browser_month_keys(hide_out_of_focus=self.server.hide_out_of_focus),
                    month_key_for_item(self.server.target, item),
                )
                self.record_server_timing("month_nav", start)
            else:
                start = time.perf_counter()
                previous_item, next_item = adjacent_source_items(
                    self.server.target,
                    source,
                    item,
                    hide_out_of_focus=self.server.hide_out_of_focus,
                    conn=conn,
                )
                self.record_server_timing("adjacent", start)

                start = time.perf_counter()
                month_nav = source_month_navigation(
                    self.server.target,
                    source,
                    item,
                    hide_out_of_focus=self.server.hide_out_of_focus,
                    conn=conn,
                )
                self.record_server_timing("month_nav", start)

            start = time.perf_counter()
            html = source_item_page_html(
                self.server.target,
                source,
                item,
                previous_item,
                next_item,
                month_nav,
                face_enabled=self.server.face_enabled,
                openclip_enabled=self.server.openclip_enabled,
                face_config=self.server.config.face_recognition,
                manual_person_controls_enabled=self.server.config.browser.manual_person_controls_enabled,
                hotkey_hints_enabled=self.server.config.browser.hotkey_hints_enabled,
                hotkeys=self.server.config.browser.hotkeys,
                hide_out_of_focus=self.server.hide_out_of_focus,
                conn=conn,
                timing_callback=self.record_server_timing,
            )
            self.record_server_timing("source_item_page_html", start)

            start = time.perf_counter()
            encoded = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.record_server_timing("encode/respond_before_write", start)
            self.respond_timing_headers()
            self.end_headers()
            self.wfile.write(encoded)
        finally:
            conn.close()

    def respond_month(self, raw_month: str) -> None:
        month_key = urllib.parse.unquote(raw_month).strip()
        if not valid_month_key(month_key):
            self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
            return
        source = all_browser_source()
        items = source_month_items(self.server.target, source, month_key, hide_out_of_focus=self.server.hide_out_of_focus)
        self.respond_html(
            source_month_page_html(
                self.server.target,
                source,
                month_key,
                items,
                face_enabled=self.server.face_enabled,
                openclip_enabled=self.server.openclip_enabled,
                face_config=self.server.config.face_recognition,
                hide_out_of_focus=self.server.hide_out_of_focus,
            )
        )

    def respond_years(self) -> None:
        self.respond_html(
            years_page_html(
                self.server.target,
                face_enabled=self.server.face_enabled,
                openclip_enabled=self.server.openclip_enabled,
                hide_out_of_focus=self.server.hide_out_of_focus,
            )
        )

    def respond_year(self, raw_year: str) -> None:
        year = urllib.parse.unquote(raw_year).strip().strip("/")
        if not valid_year_key(year):
            self.respond_text("Ugyldig år.", status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_html(
            year_months_page_html(
                self.server.target,
                year,
                face_enabled=self.server.face_enabled,
                openclip_enabled=self.server.openclip_enabled,
                hide_out_of_focus=self.server.hide_out_of_focus,
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
            self.respond_html(
                person_not_found_html(
                    person_name,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                ),
                status=HTTPStatus.NOT_FOUND,
            )
            return
        canonical_name = str(person["name"])
        source = person_browser_source(canonical_name, include_suggestions=person_mode != "confirmed", show_faces=show_faces)
        self.respond_browser_source(
            source,
            page_mode,
            raw_value,
            face_config=self.server.config.face_recognition,
            hide_out_of_focus=self.server.hide_out_of_focus,
            item_not_found_message="Filen finnes ikke for denne personen.",
            invalid_page_message="Ugyldig personside.",
        )

    def respond_filter(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        raw_query = first_param(params, "q").strip()
        if not raw_query:
            self.respond_html(filter_start_html(self.server))
            return
        try:
            url = text_filter_browser_source(raw_query, self.server.target).root_url
        except ValueError as exc:
            self.respond_html(filter_start_html(self.server, query=raw_query, message=str(exc)))
            return
        self.redirect(url)

    def respond_filter_source(self, raw_path: str) -> None:
        raw_query, page_mode, raw_value = parse_source_path(raw_path)
        query = urllib.parse.unquote(raw_query).strip()
        try:
            source = text_filter_browser_source(query, self.server.target)
        except ValueError as exc:
            self.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_browser_source(
            source,
            page_mode,
            raw_value,
            hide_out_of_focus=self.server.hide_out_of_focus,
            item_not_found_message="Filen finnes ikke for dette filtersøket.",
            invalid_page_message="Ugyldig filtersøkside.",
        )

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
        self.respond_browser_source(
            source,
            page_mode,
            raw_value,
            hide_out_of_focus=self.server.hide_out_of_focus,
            item_not_found_message="Filen finnes ikke for denne kilden.",
            invalid_page_message="Ugyldig kildeside.",
        )

    def respond_tag(self, raw_path: str) -> None:
        raw_tag_name, page_mode, raw_value = parse_source_path(raw_path)
        tag_name = urllib.parse.unquote(raw_tag_name).strip()
        try:
            source = tag_browser_source(tag_name)
        except ValueError as exc:
            self.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_browser_source(
            source,
            page_mode,
            raw_value,
            hide_out_of_focus=self.server.hide_out_of_focus,
            item_not_found_message="Filen finnes ikke for denne taggen.",
            invalid_page_message="Ugyldig taggside.",
        )

    def respond_geo(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        resolution = nonnegative_int_param(params, "resolution", DEFAULT_GEO_RESOLUTION)
        min_count = positive_int_param(params, "min_count", DEFAULT_GEO_MIN_COUNT)
        limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
        if resolution not in H3_COLUMNS:
            self.respond_text("H3-oppløsning må være mellom 0 og 11.", status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_html(
            geo_index_page_html(
                self.server.target,
                resolution=resolution,
                min_count=min_count,
                limit=limit,
                face_enabled=self.server.face_enabled,
                openclip_enabled=self.server.openclip_enabled,
            )
        )

    def respond_geo_map(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        resolution = nonnegative_int_param(params, "resolution", DEFAULT_GEO_RESOLUTION)
        min_count = positive_int_param(params, "min_count", DEFAULT_GEO_MIN_COUNT)
        limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
        if resolution not in H3_COLUMNS:
            self.respond_text("H3-oppløsning må være mellom 0 og 11.", status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_html(
            geo_map_page_html(
                self.server.target,
                resolution=resolution,
                min_count=min_count,
                limit=limit,
                face_enabled=self.server.face_enabled,
                openclip_enabled=self.server.openclip_enabled,
            )
        )

    def respond_geo_area(self, raw_cell: str, query: str) -> None:
        h3_cell = urllib.parse.unquote(raw_cell).strip()
        params = urllib.parse.parse_qs(query)
        limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
        try:
            resolution = h3_resolution(h3_cell)
        except ValueError as exc:
            self.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_html(
            geo_area_page_html(
                self.server.target,
                h3_cell,
                resolution=resolution,
                limit=limit,
                face_enabled=self.server.face_enabled,
                openclip_enabled=self.server.openclip_enabled,
                hide_out_of_focus=self.server.hide_out_of_focus,
            )
        )

    def respond_geo_place(self, raw_path: str) -> None:
        raw_slug, page_mode, raw_value = parse_source_path(raw_path)
        slug = urllib.parse.unquote(raw_slug).strip()
        place = geo_place_by_slug(self.server.target, slug)
        if place is None:
            self.respond_text("Ukjent sted.", status=HTTPStatus.NOT_FOUND)
            return
        source = geo_place_browser_source(place)
        self.respond_browser_source(
            source,
            page_mode,
            raw_value,
            hide_out_of_focus=self.server.hide_out_of_focus,
            item_not_found_message="Filen finnes ikke for dette stedet.",
            invalid_page_message="Ugyldig stedsside.",
        )

    def respond_browser_source(
        self,
        source: BrowserSource,
        page_mode: str | None,
        raw_value: str,
        *,
        item_not_found_message: str,
        invalid_page_message: str,
        face_config: FaceRecognitionConfig | None = None,
        hide_out_of_focus: bool = False,
    ) -> None:
        if page_mode is None:
            item = first_source_item(
                self.server.target,
                source,
                face_config,
                hide_out_of_focus=hide_out_of_focus,
            )
            if item is None:
                if source.person_name is None:
                    self.respond_html(
                        empty_source_html(
                            source,
                            face_enabled=self.server.face_enabled,
                            openclip_enabled=self.server.openclip_enabled,
                        )
                    )
                else:
                    self.respond_html(empty_person_browser_html(source, openclip_enabled=self.server.openclip_enabled))
                return
            self.redirect(source_item_url(source, int(item["id"])))
            return
        if page_mode == "item":
            start = time.perf_counter()
            file_id = parse_file_id(raw_value)
            self.record_server_timing("parse", start)
            start = time.perf_counter()
            conn = db.connect(self.server.target)
            self.record_server_timing("db_connect", start)
            try:
                if source_has_sql_filter(source):
                    start = time.perf_counter()
                    item_ids, item_positions = self.server.source_item_order(source, hide_out_of_focus=hide_out_of_focus)
                    self.record_server_timing("source_item_order", start)
                    start = time.perf_counter()
                    item = item_by_id(self.server.target, file_id, conn=conn) if file_id in item_positions else None
                    self.record_server_timing("item_by_id", start)
                else:
                    start = time.perf_counter()
                    item = source_item_by_id(
                        self.server.target,
                        source,
                        file_id,
                        face_config,
                        hide_out_of_focus=hide_out_of_focus,
                        conn=conn,
                    )
                    self.record_server_timing("item_by_id", start)
                if item is None:
                    self.respond_text(item_not_found_message, status=HTTPStatus.NOT_FOUND)
                    return
                if source_has_sql_filter(source):
                    start = time.perf_counter()
                    previous_item, next_item = adjacent_items_from_id_order(item_ids, int(item["id"]), item_positions)
                    self.record_server_timing("adjacent", start)
                    start = time.perf_counter()
                    month_nav = month_navigation_for_keys(
                        self.server.source_month_keys(source, hide_out_of_focus=hide_out_of_focus),
                        month_key_for_item(self.server.target, item),
                    )
                    self.record_server_timing("month_nav", start)
                else:
                    start = time.perf_counter()
                    previous_item, next_item = adjacent_source_items(
                        self.server.target,
                        source,
                        item,
                        face_config,
                        hide_out_of_focus=hide_out_of_focus,
                        conn=conn,
                    )
                    self.record_server_timing("adjacent", start)
                    start = time.perf_counter()
                    month_nav = source_month_navigation(
                        self.server.target,
                        source,
                        item,
                        face_config,
                        hide_out_of_focus=hide_out_of_focus,
                        conn=conn,
                    )
                    self.record_server_timing("month_nav", start)
                start = time.perf_counter()
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
                        manual_person_controls_enabled=self.server.config.browser.manual_person_controls_enabled,
                        hotkey_hints_enabled=self.server.config.browser.hotkey_hints_enabled,
                        hotkeys=self.server.config.browser.hotkeys,
                        hide_out_of_focus=hide_out_of_focus,
                        conn=conn,
                        source_item_count_value=(
                            self.server.source_item_count(source, hide_out_of_focus=hide_out_of_focus)
                            if source.text_filter is not None
                            else None
                        ),
                        timing_callback=self.record_server_timing,
                    )
                )
                self.record_server_timing("source_item_page_html", start)
            finally:
                conn.close()
            return
        if page_mode == "month":
            month_key = urllib.parse.unquote(raw_value).strip()
            if not valid_month_key(month_key):
                self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
                return
            items = source_month_items(
                self.server.target,
                source,
                month_key,
                face_config,
                hide_out_of_focus=hide_out_of_focus,
            )
            self.respond_html(
                source_month_page_html(
                    self.server.target,
                    source,
                    month_key,
                    items,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                    face_config=self.server.config.face_recognition,
                    hide_out_of_focus=hide_out_of_focus,
                )
            )
            return
        if page_mode == "year":
            year = urllib.parse.unquote(raw_value).strip()
            if not valid_year_key(year):
                self.respond_text("Ugyldig år.", status=HTTPStatus.BAD_REQUEST)
                return
            self.respond_html(
                source_year_months_page_html(
                    self.server.target,
                    source,
                    year,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                    face_config=self.server.config.face_recognition,
                    hide_out_of_focus=hide_out_of_focus,
                )
            )
            return
        self.respond_text(invalid_page_message, status=HTTPStatus.NOT_FOUND)

    def respond_geo_missing(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
        offset = nonnegative_int_param(params, "offset", 0)
        self.respond_html(
            geo_missing_page_html(
                self.server.target,
                limit=limit,
                offset=offset,
                face_enabled=self.server.face_enabled,
                openclip_enabled=self.server.openclip_enabled,
                hide_out_of_focus=self.server.hide_out_of_focus,
            )
        )

    def respond_set_geo_place_name(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        h3_cell = first_param(params, "h3_cell").strip()
        name = first_param(params, "name")
        limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
        try:
            server_geo.set_geo_place_name(self.server.target, h3_cell, name)
        except ValueError as exc:
            self.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
            return
        url = "/geo/area/" + urllib.parse.quote(h3_cell, safe="") + f"?limit={limit}"
        self.redirect(url)

    def respond_set_custom_geo_place(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        try:
            server_geo.save_custom_geo_place(
                self.server.target,
                raw_original_slug=first_param(params, "original_slug"),
                raw_slug=first_param(params, "slug"),
                name=first_param(params, "name"),
                raw_h3_cells=first_param(params, "h3_cells"),
            )
        except ValueError as exc:
            self.respond_html(
                error_html(exc, face_enabled=self.server.face_enabled, openclip_enabled=self.server.openclip_enabled),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        self.redirect("/geo/custom-places")

    def respond_delete_custom_geo_place(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        try:
            server_geo.delete_custom_geo_place(
                self.server.target,
                first_param(params, "original_slug") or first_param(params, "slug"),
            )
        except ValueError as exc:
            self.respond_html(
                error_html(exc, face_enabled=self.server.face_enabled, openclip_enabled=self.server.openclip_enabled),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        self.redirect("/geo")

    def respond_set_face_config(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        enabled = "true" in {value.strip().lower() for value in params.get("enabled", [])}
        self.server.config = server_app.update_face_enabled_config(
            self.server.config,
            server_app.server_program_repo_root(),
            enabled,
        )
        self.redirect("/settings")

    def respond_set_hide_out_of_focus(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        enabled = "true" in {value.strip().lower() for value in params.get("enabled", [])}
        self.server.config = server_app.update_hide_out_of_focus_config(
            self.server.config,
            server_app.server_program_repo_root(),
            enabled,
        )
        clear_browser_navigation_cache(self.server)
        self.redirect("/settings")

    def respond_set_manual_person_controls(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        enabled = "true" in {value.strip().lower() for value in params.get("enabled", [])}
        self.server.config = server_app.update_manual_person_controls_config(
            self.server.config,
            server_app.server_program_repo_root(),
            enabled,
        )
        self.redirect("/settings")

    def respond_set_hotkey(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        key = first_param(params, "key").strip()
        action = first_param(params, "action").strip()
        if action == "h3":
            hotkey = BrowserHotkeyConfig(action=action, h3_cell=first_param(params, "h3_cell").strip())
        elif action == "person":
            hotkey = BrowserHotkeyConfig(action=action, person_name=first_param(params, "person_name").strip())
        elif action == "tag":
            hotkey = BrowserHotkeyConfig(action=action, tag_name=first_param(params, "tag_name").strip())
        elif action == "manual_date":
            hotkey = BrowserHotkeyConfig(
                action=action,
                mode=first_param(params, "mode").strip(),
                date=first_param(params, "date").strip(),
                uncertainty=first_param(params, "uncertainty").strip(),
                date_from=first_param(params, "date_from").strip(),
                date_to=first_param(params, "date_to").strip(),
                note=first_param(params, "note").strip(),
            )
        else:
            hotkey = BrowserHotkeyConfig(action=action)
        try:
            self.server.config = server_app.update_hotkey_config(
                self.server.config,
                server_app.server_program_repo_root(),
                key,
                hotkey,
            )
        except ValueError as exc:
            self.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
            return
        self.redirect("/settings")

    def respond_set_hotkey_hints(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        enabled = "true" in {value.strip().lower() for value in params.get("enabled", [])}
        self.server.config = server_app.update_hotkey_hints_config(
            self.server.config,
            server_app.server_program_repo_root(),
            enabled,
        )
        self.redirect("/settings")

    def respond_set_h3_cell_name(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        try:
            server_app.save_h3_cell_name(
                self.server.target,
                original_h3_cell=first_param(params, "original_h3_cell"),
                h3_cell=first_param(params, "h3_cell"),
                name=first_param(params, "name"),
            )
        except ValueError as exc:
            self.respond_html(
                error_html(exc, face_enabled=self.server.face_enabled, openclip_enabled=self.server.openclip_enabled),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        self.redirect("/settings/h3-cells")

    def respond_delete_h3_cell_name(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        try:
            server_app.delete_h3_cell_name(
                self.server.target,
                h3_cell=first_param(params, "original_h3_cell") or first_param(params, "h3_cell"),
            )
        except ValueError as exc:
            self.respond_html(
                error_html(exc, face_enabled=self.server.face_enabled, openclip_enabled=self.server.openclip_enabled),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        self.redirect("/settings/h3-cells")

    def respond_set_face_model(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        model_name = (params.get("model_name") or [""])[0].strip()
        self.server.config = server_app.update_face_model_config(
            self.server.config,
            server_app.server_program_repo_root(),
            model_name,
        )
        self.redirect("/settings")

    def respond_create_tag(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        try:
            conn = db.connect(self.server.target)
            try:
                db.create_user_tag(conn, first_param(params, "name"))
                conn.commit()
            finally:
                conn.close()
        except ValueError as exc:
            self.respond_html(
                error_html(exc, face_enabled=self.server.face_enabled, openclip_enabled=self.server.openclip_enabled),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        self.redirect("/tags")

    def respond_rename_tag(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        try:
            tag_id = int(first_param(params, "tag_id"))
        except ValueError:
            self.respond_text("Ugyldig tagg-id.", status=HTTPStatus.BAD_REQUEST)
            return
        try:
            conn = db.connect(self.server.target)
            try:
                db.rename_user_tag(conn, tag_id=tag_id, new_name=first_param(params, "name"))
                conn.commit()
            finally:
                conn.close()
        except ValueError as exc:
            self.respond_html(
                error_html(exc, face_enabled=self.server.face_enabled, openclip_enabled=self.server.openclip_enabled),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        self.redirect("/tags")

    def respond_delete_tag(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        try:
            tag_id = int(first_param(params, "tag_id"))
        except ValueError:
            self.respond_text("Ugyldig tagg-id.", status=HTTPStatus.BAD_REQUEST)
            return
        try:
            conn = db.connect(self.server.target)
            try:
                db.delete_user_tag(conn, tag_id=tag_id)
                conn.commit()
            finally:
                conn.close()
        except ValueError as exc:
            self.respond_html(
                error_html(exc, face_enabled=self.server.face_enabled, openclip_enabled=self.server.openclip_enabled),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        self.redirect("/tags")

    def respond_file(self, encoded_relative_path: str) -> None:
        try:
            served_file = server_files.read_server_file(self.server.target, encoded_relative_path)
        except PermissionError as exc:
            self.respond_text(str(exc), status=HTTPStatus.FORBIDDEN)
            return
        except FileNotFoundError as exc:
            self.respond_text(str(exc), status=HTTPStatus.NOT_FOUND)
            return
        except OSError as exc:
            self.respond_text(str(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.respond_bytes(served_file.content, served_file.content_type)

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
        self.respond_html(
            markdown_doc_page_html(
                doc_path,
                markdown,
                face_enabled=self.server.face_enabled,
                openclip_enabled=self.server.openclip_enabled,
            )
        )

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
                "file_id": result.file_id,
                "redirect_url": f"/item/{result.file_id}",
                "removed": result.removed,
            }
        )

    def respond_add_person_to_file(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        person_name = str(payload.get("person_name") or "").strip()
        if not person_name:
            self.respond_json({"ok": False, "error": "Personnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            file_id = int(payload.get("file_id"))
        except (TypeError, ValueError):
            self.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            config = self.server.config.face_recognition
            result = add_person_to_file(self.server.target, person_name, file_id, config)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        clear_face_caches()
        self.respond_json(
            {
                "ok": True,
                "person_name": result.person_name,
                "person_url": person_item_url(result.person_name, result.file_id, show_faces=False),
                "confirmed": True,
                "file_id": result.file_id,
                "added": result.added,
            }
        )

    def respond_remove_person_from_file(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        person_name = str(payload.get("person_name") or "").strip()
        if not person_name:
            self.respond_json({"ok": False, "error": "Personnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            file_id = int(payload.get("file_id"))
        except (TypeError, ValueError):
            self.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            config = self.server.config.face_recognition
            result = remove_person_from_file(self.server.target, person_name, file_id, config)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        clear_face_caches()
        self.respond_json(
            {
                "ok": True,
                "person_name": result.person_name,
                "person_url": person_url(result.person_name),
                "file_id": result.file_id,
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
                "removed_files": result.removed_files,
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
        try:
            rotation = server_actions.rotate_file_view(self.server.target, file_id, direction)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_json({"ok": True, "file_id": file_id, "rotation": rotation})

    def respond_tag_item(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        try:
            file_id = int(payload.get("file_id"))
        except (TypeError, ValueError):
            self.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        tag_name = str(payload.get("tag_name") or "").strip()
        tagged = bool(payload.get("tagged"))
        if not tag_name:
            self.respond_json({"ok": False, "error": "Taggnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            server_actions.set_tag_on_file(self.server.target, file_id, tag_name, tagged)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_json({"ok": True, "file_id": file_id, "tag_name": db.normalize_tag_name(tag_name), "tagged": tagged})

    def respond_remove_manual_location_item(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        try:
            file_id = int(payload.get("file_id"))
        except (TypeError, ValueError):
            self.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            server_actions.remove_manual_h3_location_from_file(self.server.target, file_id)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_json({"ok": True, "file_id": file_id, "gps_source": None})

    def respond_manual_date_item(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        try:
            file_id = int(payload.get("file_id"))
        except (TypeError, ValueError):
            self.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            date_from, date_to = server_actions.set_manual_date_on_file(
                self.server.target,
                file_id,
                mode=str(payload.get("mode") or ""),
                date=str(payload.get("date") or ""),
                uncertainty=str(payload.get("uncertainty") or ""),
                date_from=str(payload.get("date_from") or ""),
                date_to=str(payload.get("date_to") or ""),
                note=str(payload.get("note") or ""),
            )
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        clear_browser_navigation_cache(self.server)
        self.respond_json(
            {
                "ok": True,
                "file_id": file_id,
                "manual_date_from": date_from.isoformat(),
                "manual_date_to": date_to.isoformat(),
            }
        )

    def respond_clear_manual_date_item(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        try:
            file_id = int(payload.get("file_id"))
        except (TypeError, ValueError):
            self.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            server_actions.clear_manual_date_on_file(self.server.target, file_id)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        clear_browser_navigation_cache(self.server)
        self.respond_json({"ok": True, "file_id": file_id})

    def respond_hotkey_action(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        if not self.server.config.browser.hotkey_hints_enabled:
            self.respond_json(
                {"ok": False, "error": "Hurtigtaster er slått av."},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        try:
            file_id = int(payload.get("file_id"))
        except (TypeError, ValueError):
            self.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        key = str(payload.get("key") or "").strip()
        if key not in HOTKEY_KEYS:
            self.respond_json({"ok": False, "error": "Ugyldig hurtigtast."}, status=HTTPStatus.BAD_REQUEST)
            return
        hotkey = (self.server.config.browser.hotkeys or {}).get(key, BrowserHotkeyConfig())
        if hotkey.action == "person" and not self.server.face_enabled:
            self.respond_json({"ok": False, "error": "Ansiktsgjenkjenning er av."}, status=HTTPStatus.FORBIDDEN)
            return
        filter_source = hotkey_filter_source_from_url(self.server.target, payload.get("source_url"))
        previous_filter_item = None
        next_filter_item = None
        if filter_source is not None:
            conn = db.connect(self.server.target)
            try:
                filter_item = source_item_by_id(self.server.target, filter_source, file_id, conn=conn)
                if filter_item is not None:
                    previous_filter_item, next_filter_item = adjacent_source_items(
                        self.server.target,
                        filter_source,
                        filter_item,
                        conn=conn,
                    )
                else:
                    filter_source = None
            finally:
                conn.close()
        try:
            result = server_actions.apply_browser_hotkey_to_file(
                self.server.target,
                file_id,
                hotkey,
                face_config=self.server.config.face_recognition,
            )
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if hotkey.action == "manual_date":
            clear_browser_navigation_cache(self.server)
        if hotkey.action == "person":
            clear_face_caches()
            result["person_url"] = person_item_url(str(result["person_name"]), file_id, show_faces=False)
            result["confirmed"] = True
        if filter_source is not None:
            conn = db.connect(self.server.target)
            try:
                filter_item = source_item_by_id(self.server.target, filter_source, file_id, conn=conn)
            finally:
                conn.close()
            if filter_item is None:
                if next_filter_item is not None:
                    result["redirect_url"] = source_item_url(filter_source, int(next_filter_item["id"]))
                elif previous_filter_item is not None:
                    result["redirect_url"] = source_item_url(filter_source, int(previous_filter_item["id"]))
                else:
                    result["redirect_url"] = filter_source.root_url
        self.respond_json({"ok": True, "key": key, **result})

    def respond_delete_item(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        try:
            file_id = int(payload.get("file_id"))
        except (TypeError, ValueError):
            self.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            deleted_path = server_actions.remove_file_from_browser(self.server.target, file_id)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        clear_browser_navigation_cache(self.server)
        self.respond_json({"ok": True, "file_id": file_id, "deleted_path": deleted_path.as_posix()})

    def respond_undelete_item(self) -> None:
        payload = BildebankRequestHandler.read_json_payload(self)
        try:
            file_id = int(payload.get("file_id"))
        except (TypeError, ValueError):
            self.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            restored_path = server_actions.undelete_file_from_browser(self.server.target, file_id)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        clear_browser_navigation_cache(self.server)
        self.respond_json({"ok": True, "file_id": file_id, "restored_path": restored_path.as_posix()})

    def read_json_payload(self) -> dict[str, object]:
        return server_request.read_json_payload(self.headers, self.rfile)

    def read_face_person_payload(self) -> tuple[str, int] | tuple[dict[str, object], HTTPStatus]:
        return server_request.read_face_person_payload(self.headers, self.rfile)


def resolve_doc_path(raw_doc_path: str) -> Path | None:
    return server_markdown.resolve_doc_path(raw_doc_path, server_app.server_program_repo_root() / "docs")


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
