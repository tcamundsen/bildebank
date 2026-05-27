from __future__ import annotations

import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import AppConfig, FaceRecognitionConfig
from .face import (
    add_face_to_person,
    create_person,
    delete_person,
    remove_face_from_person,
    rename_person,
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
    empty_browser_html,
    empty_person_browser_html,
    empty_source_html,
    error_html,
    geo_area_page_html,
    geo_index_page_html,
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
    sources_page_html,
)
from .server_browser import (
    adjacent_source_items,
    browser_item_by_id,
    first_source_item,
    image_info_content_html,
    imported_source_by_id,
    source_item_by_id,
    source_month_items,
    source_month_navigation,
    valid_month_key,
)
from .server_browser_sources import (
    BrowserSource,
    all_browser_source,
    date_source_browser_source,
    geo_place_browser_source,
    imported_source_browser_source,
    parse_person_path,
    parse_source_path,
    person_browser_source,
    person_url,
    source_item_url,
    valid_browser_date_source,
)
from .server_faces import (
    clear_face_caches,
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
from .server_response import ServerResponseMixin
from . import server_request
from .server_request import first_param, nonnegative_int_param, parse_file_id, positive_int_param
from .server_assets import SERVER_CSS, SERVER_JS


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
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


class BildebankRequestHandler(ServerResponseMixin, BaseHTTPRequestHandler):
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
            if parsed.path == "/settings":
                self.respond_html(app_status_page_html(self.server.target, self.server.config))
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
            self.respond_html(
                error_html(exc, face_enabled=self.server.face_enabled, openclip_enabled=self.server.openclip_enabled),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

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

    def respond_browser_root(self) -> None:
        source = all_browser_source()
        item = first_source_item(self.server.target, source)
        if item is None:
            self.respond_html(
                empty_browser_html(
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                )
            )
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
            item_not_found_message="Filen finnes ikke for denne personen.",
            invalid_page_message="Ugyldig personside.",
        )

    def respond_date_source(self, raw_path: str) -> None:
        raw_date_source, page_mode, raw_value = parse_source_path(raw_path)
        date_source = urllib.parse.unquote(raw_date_source).strip()
        if not valid_browser_date_source(date_source):
            self.respond_text("Ugyldig datokilde.", status=HTTPStatus.BAD_REQUEST)
            return
        source = date_source_browser_source(date_source)
        self.respond_browser_source(
            source,
            page_mode,
            raw_value,
            item_not_found_message="Filen finnes ikke for denne datokilden.",
            invalid_page_message="Ugyldig datokildeside.",
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
            item_not_found_message="Filen finnes ikke for denne kilden.",
            invalid_page_message="Ugyldig kildeside.",
        )

    def respond_geo(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        resolution = nonnegative_int_param(params, "resolution", DEFAULT_GEO_RESOLUTION)
        min_count = positive_int_param(params, "min_count", DEFAULT_GEO_MIN_COUNT)
        limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
        if resolution not in H3_COLUMNS:
            self.respond_text("H3-oppløsning må være mellom 0 og 9.", status=HTTPStatus.BAD_REQUEST)
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
            self.respond_text("H3-oppløsning må være mellom 0 og 9.", status=HTTPStatus.BAD_REQUEST)
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
    ) -> None:
        if page_mode is None:
            item = first_source_item(self.server.target, source, face_config)
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
            file_id = parse_file_id(raw_value)
            item = source_item_by_id(self.server.target, source, file_id, face_config)
            if item is None:
                self.respond_text(item_not_found_message, status=HTTPStatus.NOT_FOUND)
                return
            previous_item, next_item = adjacent_source_items(self.server.target, source, item, face_config)
            month_nav = source_month_navigation(self.server.target, source, item, face_config)
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
            items = source_month_items(self.server.target, source, month_key, face_config)
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

    def respond_set_face_model(self) -> None:
        params = server_request.read_form_params(self.headers, self.rfile)
        model_name = (params.get("model_name") or [""])[0].strip()
        self.server.config = server_app.update_face_model_config(
            self.server.config,
            server_app.server_program_repo_root(),
            model_name,
        )
        self.redirect("/settings")

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
        try:
            rotation = server_actions.rotate_file_view(self.server.target, file_id, direction)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_json({"ok": True, "file_id": file_id, "rotation": rotation})

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
