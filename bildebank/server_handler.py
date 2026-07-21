from __future__ import annotations

import hmac
import mimetypes
import time
import urllib.parse
from io import BytesIO
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from . import db
from . import server_app
from . import server_endpoints_admin
from . import server_endpoints_browser
from . import server_endpoints_faces
from . import server_endpoints_items
from .server_pages import (
    app_status_page_html,
    custom_geo_places_page_html,
    dashboard_page_html,
    error_html,
    h3_cells_page_html,
    index_html,
    markdown_doc_page_html,
    people_page_html,
    removed_files_page_html,
    search_html,
    sources_page_html,
    tags_page_html,
)
from .server_browser_info_html import image_info_content_html
from .server_browser_queries import (
    active_item_by_id_including_hidden,
    browser_item_by_id,
    item_by_id,
)
from .server_faces import (
    face_overlay_content_html,
    item_face_matches_content_html,
)
from . import server_markdown
from . import server_files
from . import server_slideshow
from .server_search import (
    DEFAULT_SEARCH_LIMIT,
    search_server_images,
)
from .target_lock import TargetLockError
from .server_response import ServerResponseMixin
from . import server_request
from .server_request import (
    first_param,
    nonnegative_int_param,
    parse_file_id,
    positive_int_param,
)
from .server_assets import SERVER_CSS, SERVER_JS


PREVIEW_MAX_SIZE = 1600


def client_disconnected_error(exc: OSError) -> bool:
    return isinstance(
        exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
    )


def validate_csrf_request(handler: Any) -> bool:
    expected = str(handler.server.csrf_token)
    length = int(handler.headers.get("Content-Length") or "0")
    body = handler.rfile.read(length) if length > 0 else b""
    handler.rfile = BytesIO(body)
    supplied = str(handler.headers.get("X-CSRF-Token") or "")
    if not supplied and body:
        content_type = str(handler.headers.get("Content-Type") or "")
        if "application/x-www-form-urlencoded" in content_type:
            params = urllib.parse.parse_qs(body.decode("utf-8"))
            supplied = first_param(params, "csrf_token")
    if supplied and hmac.compare_digest(expected, supplied):
        return True
    handler.respond_json(
        {"ok": False, "error": "Ugyldig eller manglende CSRF-token."},
        status=HTTPStatus.FORBIDDEN,
    )
    return False


def resolve_doc_path(raw_doc_path: str) -> Path | None:
    return server_markdown.resolve_doc_path(
        raw_doc_path, server_app.server_program_repo_root() / "docs"
    )


def resolve_doc_asset_path(raw_asset_path: str) -> Path | None:
    return server_markdown.resolve_doc_asset_path(
        raw_asset_path, server_app.server_program_repo_root() / "docs"
    )


class BildebankRequestHandler(ServerResponseMixin, BaseHTTPRequestHandler):
    server: Any
    server_timing_steps: dict[str, float]
    protocol_version = "HTTP/1.1"

    def browser_db_connection(self) -> tuple[Any, bool]:
        if not hasattr(self, "request"):
            return db.connect(self.server.target), True
        conn = getattr(self, "_browser_db_connection", None)
        if conn is None:
            conn = db.connect(self.server.target)
            self._browser_db_connection = conn
        return conn, False

    def close_browser_db_connection(self) -> None:
        conn: Any | None = getattr(self, "_browser_db_connection", None)
        if conn is None:
            return
        delattr(self, "_browser_db_connection")
        conn.close()

    def read_only_get_blocked(self, path: str) -> bool:
        return (
            path
            in {
                "/settings",
                "/sources",
                "/sources/",
                "/tags",
                "/tags/",
                "/search",
                "/api/search-preload",
            }
            or path.startswith("/settings/")
            or path.startswith("/people/missing-suggestions")
            or path.startswith("/geo/custom-places")
            or (
                path.startswith("/api/maintenance/")
                and path != "/api/maintenance/statuses"
            )
        )

    def respond_read_only_forbidden(self, path: str) -> None:
        message = "Serveren kjører i read-only-modus."
        if path.startswith("/api/"):
            self.respond_json(
                {"ok": False, "error": message}, status=HTTPStatus.FORBIDDEN
            )
            return
        self.respond_text(message, status=HTTPStatus.FORBIDDEN)

    def do_GET(self) -> None:
        self.request_started_at = time.perf_counter()
        self.server_timing_steps = {}
        parsed = urllib.parse.urlparse(self.path)
        try:
            if getattr(self.server, "slideshow", None) is not None:
                server_slideshow.respond_slideshow_get(self, parsed.path)
                return
            if getattr(self.server, "read_only", False) and self.read_only_get_blocked(
                parsed.path
            ):
                self.respond_read_only_forbidden(parsed.path)
                return
            if parsed.path == "/":
                server_endpoints_browser.respond_browser_root(self)
                return
            if parsed.path in {"/dashboard", "/dashboard/"}:
                self.respond_html(dashboard_page_html(self.server))
                return
            if parsed.path == "/people":
                if not self.server.face_enabled:
                    self.respond_text(
                        "Ansiktsgjenkjenning er av.", status=HTTPStatus.NOT_FOUND
                    )
                    return
                self.respond_html(
                    people_page_html(
                        self.server.target,
                        self.server.config.face_recognition,
                        openclip_enabled=self.server.openclip_enabled,
                        read_only=getattr(self.server, "read_only", False),
                    )
                )
                return
            if parsed.path == "/people/missing-suggestions" or parsed.path.startswith(
                "/people/missing-suggestions/"
            ):
                if not self.server.face_enabled:
                    self.respond_text(
                        "Ansiktsgjenkjenning er av.", status=HTTPStatus.NOT_FOUND
                    )
                    return
                server_endpoints_faces.respond_missing_face_suggestions(
                    self, parsed.path.removeprefix("/people/missing-suggestions")
                )
                return
            if parsed.path.startswith("/people/") and "/references/" in parsed.path:
                if not self.server.face_enabled:
                    self.respond_text(
                        "Ansiktsgjenkjenning er av.", status=HTTPStatus.NOT_FOUND
                    )
                    return
                server_endpoints_faces.respond_person_reference_suggestions(
                    self, parsed.path.removeprefix("/people/")
                )
                return
            if parsed.path.startswith("/people/") and parsed.path.endswith(
                "/references"
            ):
                if not self.server.face_enabled:
                    self.respond_text(
                        "Ansiktsgjenkjenning er av.", status=HTTPStatus.NOT_FOUND
                    )
                    return
                server_endpoints_faces.respond_person_references(
                    self,
                    parsed.path.removeprefix("/people/")
                    .removesuffix("/references")
                    .strip("/"),
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
                params = urllib.parse.parse_qs(parsed.query)
                self.respond_html(
                    app_status_page_html(
                        self.server.target,
                        self.server.config,
                        scroll_y=nonnegative_int_param(params, "scroll", 0),
                    )
                )
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
                self.respond_static_asset(
                    SERVER_JS, "application/javascript; charset=utf-8"
                )
                return
            if parsed.path.startswith("/help/"):
                self.respond_help(parsed.path.removeprefix("/help/"))
                return
            if parsed.path.startswith("/docs/"):
                self.respond_help(parsed.path.removeprefix("/docs/"))
                return
            if parsed.path == "/README.md":
                self.respond_readme()
                return
            if parsed.path in {"/geo", "/geo/"}:
                server_endpoints_browser.respond_geo(self, parsed.query)
                return
            if parsed.path == "/geo/map":
                server_endpoints_browser.respond_geo_map(self, parsed.query)
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
                server_endpoints_browser.respond_geo_place(
                    self, parsed.path.removeprefix("/geo/place/")
                )
                return
            if parsed.path.startswith("/geo/area/"):
                server_endpoints_browser.respond_geo_area(
                    self, parsed.path.removeprefix("/geo/area/"), parsed.query
                )
                return
            if parsed.path.startswith("/item/"):
                server_endpoints_browser.respond_item(
                    self, parsed.path.removeprefix("/item/")
                )
                return
            if parsed.path.startswith("/month/"):
                server_endpoints_browser.respond_month(
                    self, parsed.path.removeprefix("/month/")
                )
                return
            if parsed.path in {"/years", "/years/"}:
                server_endpoints_browser.respond_years(self)
                return
            if parsed.path.startswith("/years/"):
                server_endpoints_browser.respond_year(
                    self, parsed.path.removeprefix("/years/")
                )
                return
            if parsed.path == "/filter":
                server_endpoints_browser.respond_filter(self, parsed.query)
                return
            if parsed.path.startswith("/filter/"):
                server_endpoints_browser.respond_filter_source(
                    self, parsed.path.removeprefix("/filter/")
                )
                return
            if parsed.path.startswith("/source/"):
                server_endpoints_browser.respond_imported_source(
                    self, parsed.path.removeprefix("/source/")
                )
                return
            if parsed.path.startswith("/tag/"):
                server_endpoints_browser.respond_tag(
                    self, parsed.path.removeprefix("/tag/")
                )
                return
            if parsed.path.startswith("/person/"):
                if not self.server.face_enabled:
                    self.respond_text(
                        "Ansiktsgjenkjenning er av.", status=HTTPStatus.NOT_FOUND
                    )
                    return
                server_endpoints_faces.respond_person(
                    self, parsed.path.removeprefix("/person/")
                )
                return
            if parsed.path == "/search":
                if not self.server.openclip_enabled:
                    self.respond_text(
                        "Tekstbasert bildesøk er av.", status=HTTPStatus.NOT_FOUND
                    )
                    return
                params = urllib.parse.parse_qs(parsed.query)
                query = first_param(params, "q").strip()
                limit = positive_int_param(params, "limit", DEFAULT_SEARCH_LIMIT)
                if not query:
                    self.server.search_cache.preload_model_async()
                    self.respond_html(
                        index_html(self.server, message="Skriv inn et søk.")
                    )
                    return
                try:
                    stats = search_server_images(self.server, query=query, limit=limit)
                except TargetLockError as exc:
                    self.respond_html(
                        error_html(
                            exc,
                            face_enabled=self.server.face_enabled,
                            openclip_enabled=self.server.openclip_enabled,
                        ),
                        status=HTTPStatus.CONFLICT,
                    )
                    return
                self.respond_html(search_html(self.server, stats, limit))
                return
            if parsed.path == "/api/item-info":
                self.respond_item_info(parsed.query)
                return
            if parsed.path == "/api/item-faces":
                self.respond_item_faces(parsed.query)
                return
            if parsed.path == "/api/item-face-matches":
                self.respond_item_face_matches(parsed.query)
                return
            if parsed.path == "/api/search-preload":
                self.respond_search_preload()
                return
            if parsed.path == "/api/maintenance/thumbnails":
                self.respond_thumbnail_maintenance()
                return
            if parsed.path == "/api/maintenance/statuses":
                self.respond_maintenance_statuses()
                return
            if parsed.path.startswith("/display/"):
                self.respond_display(parsed.path.removeprefix("/display/"))
                return
            if parsed.path.startswith("/preview/"):
                self.respond_preview(parsed.path.removeprefix("/preview/"))
                return
            if parsed.path.startswith("/video-preview/"):
                self.respond_video_preview(parsed.path.removeprefix("/video-preview/"))
                return
            if parsed.path.startswith("/file/"):
                self.respond_file(parsed.path.removeprefix("/file/"))
                return
            self.respond_file(parsed.path.lstrip("/"))
        except TargetLockError as exc:
            if getattr(self.server, "slideshow", None) is not None:
                self.respond_text(str(exc), status=HTTPStatus.CONFLICT)
                return
            self.respond_html(
                error_html(
                    exc,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                ),
                status=HTTPStatus.CONFLICT,
            )
        except Exception as exc:  # noqa: BLE001 - local server should show readable errors
            if getattr(self.server, "slideshow", None) is not None:
                self.respond_text(
                    f"Kunne ikke vise slideshowet: {exc}",
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self.respond_html(
                error_html(
                    exc,
                    face_enabled=self.server.face_enabled,
                    openclip_enabled=self.server.openclip_enabled,
                ),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        self.request_started_at = time.perf_counter()
        self.server_timing_steps = {}
        parsed = urllib.parse.urlparse(self.path)
        if getattr(self.server, "slideshow", None) is not None:
            self.respond_text("Siden finnes ikke.", status=HTTPStatus.NOT_FOUND)
            return
        if getattr(self.server, "read_only", False):
            self.respond_read_only_forbidden(parsed.path)
            return
        if not validate_csrf_request(self):
            return
        try:
            if parsed.path == "/people/face-suggest":
                server_endpoints_faces.respond_face_suggest(self)
                return
            if parsed.path == "/geo/place-name":
                server_endpoints_admin.respond_set_geo_place_name(self)
                return
            if parsed.path == "/geo/custom-place":
                server_endpoints_admin.respond_set_custom_geo_place(self)
                return
            if parsed.path == "/geo/custom-place-delete":
                server_endpoints_admin.respond_delete_custom_geo_place(self)
                return
            if parsed.path == "/settings/face-config":
                server_endpoints_admin.respond_set_face_config(self)
                return
            if parsed.path == "/settings/image-search":
                server_endpoints_admin.respond_set_image_search(self)
                return
            if parsed.path == "/settings/hide-out-of-focus":
                server_endpoints_admin.respond_set_hide_out_of_focus(self)
                return
            if parsed.path == "/settings/manual-person-controls":
                server_endpoints_admin.respond_set_manual_person_controls(self)
                return
            if parsed.path == "/settings/person-reference-links":
                server_endpoints_admin.respond_set_person_reference_links(self)
                return
            if parsed.path == "/settings/hotkey":
                server_endpoints_admin.respond_set_hotkey(self)
                return
            if parsed.path == "/settings/hotkey-hints":
                server_endpoints_admin.respond_set_hotkey_hints(self)
                return
            if parsed.path == "/settings/h3-cell":
                server_endpoints_admin.respond_set_h3_cell_name(self)
                return
            if parsed.path == "/settings/h3-cell-delete":
                server_endpoints_admin.respond_delete_h3_cell_name(self)
                return
            if parsed.path == "/settings/face-model":
                server_endpoints_admin.respond_set_face_model(self)
                return
            if parsed.path == "/tags/create":
                server_endpoints_admin.respond_create_tag(self)
                return
            if parsed.path == "/tags/rename":
                server_endpoints_admin.respond_rename_tag(self)
                return
            if parsed.path == "/tags/delete":
                server_endpoints_admin.respond_delete_tag(self)
                return
            if parsed.path == "/api/face-person-add-face":
                if not self.server.face_enabled:
                    self.respond_json(
                        {"ok": False, "error": "Ansiktsgjenkjenning er av."},
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
                server_endpoints_faces.respond_add_face_to_person(self)
                return
            if parsed.path == "/api/face-person-remove-face":
                if not self.server.face_enabled:
                    self.respond_json(
                        {"ok": False, "error": "Ansiktsgjenkjenning er av."},
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
                server_endpoints_faces.respond_remove_face_from_person(self)
                return
            if parsed.path == "/api/face-person-add-file":
                if not self.server.face_enabled:
                    self.respond_json(
                        {"ok": False, "error": "Ansiktsgjenkjenning er av."},
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
                server_endpoints_faces.respond_add_person_to_file(self)
                return
            if parsed.path == "/api/face-person-remove-file":
                if not self.server.face_enabled:
                    self.respond_json(
                        {"ok": False, "error": "Ansiktsgjenkjenning er av."},
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
                server_endpoints_faces.respond_remove_person_from_file(self)
                return
            if parsed.path == "/api/face-person-create-and-add-face":
                if not self.server.face_enabled:
                    self.respond_json(
                        {"ok": False, "error": "Ansiktsgjenkjenning er av."},
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
                server_endpoints_faces.respond_create_person_and_add_face(self)
                return
            if parsed.path == "/api/face-person-rename":
                if not self.server.face_enabled:
                    self.respond_json(
                        {"ok": False, "error": "Ansiktsgjenkjenning er av."},
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
                server_endpoints_faces.respond_rename_person(self)
                return
            if parsed.path == "/api/face-person-delete":
                if not self.server.face_enabled:
                    self.respond_json(
                        {"ok": False, "error": "Ansiktsgjenkjenning er av."},
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
                server_endpoints_faces.respond_delete_person(self)
                return
            if parsed.path == "/api/item-rotate":
                server_endpoints_items.respond_rotate_item(self)
                return
            if parsed.path == "/api/item-comment":
                server_endpoints_items.respond_comment_item(self)
                return
            if parsed.path == "/api/item-tag":
                server_endpoints_items.respond_tag_item(self)
                return
            if parsed.path == "/api/item-manual-location":
                server_endpoints_items.respond_manual_location_item(self)
                return
            if parsed.path == "/api/item-manual-location-remove":
                server_endpoints_items.respond_remove_manual_location_item(self)
                return
            if parsed.path == "/api/item-manual-date":
                server_endpoints_items.respond_manual_date_item(self)
                return
            if parsed.path == "/api/item-manual-date-clear":
                server_endpoints_items.respond_clear_manual_date_item(self)
                return
            if parsed.path == "/api/item-hotkey-action":
                server_endpoints_items.respond_hotkey_action(self)
                return
            if parsed.path == "/api/item-delete":
                server_endpoints_items.respond_delete_item(self)
                return
            if parsed.path == "/api/item-undelete":
                server_endpoints_items.respond_undelete_item(self)
                return
            self.respond_json(
                {"ok": False, "error": "Ukjent endepunkt."}, status=HTTPStatus.NOT_FOUND
            )
        except Exception as exc:  # noqa: BLE001 - local server should show readable errors
            self.respond_json(
                {"ok": False, "error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def validate_csrf_request(self) -> bool:
        return validate_csrf_request(self)

    def respond_search_preload(self) -> None:
        if not self.server.openclip_enabled:
            self.respond_json(
                {"ok": False, "error": "Tekstbasert bildesøk er av."},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        status = self.server.search_cache.preload_model_async()
        self.respond_json(
            {"ok": True, "status": status, "loaded": self.server.search_cache.loaded}
        )

    def handle(self) -> None:
        try:
            super().handle()
        except OSError as exc:
            if client_disconnected_error(exc):
                self.close_connection = True
                return
            raise
        finally:
            self.close_browser_db_connection()

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

    def respond_display(self, raw_file_id: str) -> None:
        try:
            file_id = parse_file_id(raw_file_id)
        except ValueError as exc:
            self.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
            return
        self.respond_preview_image(
            file_id,
            apply_view_rotation=True,
            resize=self.server.preview_images,
        )

    def respond_preview(self, raw_file_id: str) -> None:
        try:
            file_id = parse_file_id(raw_file_id)
        except ValueError as exc:
            self.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
            return
        if not self.server.preview_images:
            self.respond_file(str(file_id))
            return
        self.respond_preview_image(file_id)

    def respond_preview_image(
        self,
        file_id: int,
        *,
        require_active: bool = False,
        apply_view_rotation: bool = False,
        resize: bool = True,
    ) -> bool:
        item = (
            active_item_by_id_including_hidden(self.server.target, file_id)
            if require_active
            else item_by_id(self.server.target, file_id)
        )
        if item is None:
            self.respond_text("Filen finnes ikke.", status=HTTPStatus.NOT_FOUND)
            return False
        try:
            absolute_path = server_files.server_file_path_by_id(
                self.server.target, file_id
            )
        except PermissionError as exc:
            self.respond_text(str(exc), status=HTTPStatus.FORBIDDEN)
            return False
        except (FileNotFoundError, OSError) as exc:
            self.respond_text(str(exc), status=HTTPStatus.NOT_FOUND)
            return False
        if not absolute_path.is_file():
            self.respond_text(
                "Bildefilen finnes ikke på disk.", status=HTTPStatus.NOT_FOUND
            )
            return False
        try:
            from PIL import Image, ImageOps, UnidentifiedImageError
        except ImportError as exc:
            self.respond_text(
                f"Pillow mangler, kan ikke lage preview-bilde: {exc}",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return False
        try:
            with Image.open(absolute_path) as image:
                preview = ImageOps.exif_transpose(image)
                if apply_view_rotation:
                    rotation = db.normalize_view_rotation(
                        item["view_rotation_degrees"]
                    )
                    if rotation:
                        preview = preview.rotate(-rotation, expand=True)
                if resize:
                    preview.thumbnail((PREVIEW_MAX_SIZE, PREVIEW_MAX_SIZE))
                if preview.mode != "RGB":
                    preview = preview.convert("RGB")
                output = BytesIO()
                preview.save(output, format="JPEG", quality=85)
        except UnidentifiedImageError:
            self.respond_text("Filen er ikke et bilde.", status=HTTPStatus.BAD_REQUEST)
            return False
        except OSError as exc:
            self.respond_text(str(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return False
        self.respond_bytes(output.getvalue(), "image/jpeg")
        return True

    def respond_file(self, encoded_relative_path: str) -> None:
        try:
            served_file = server_files.resolve_server_file(
                self.server.target, encoded_relative_path
            )
        except PermissionError as exc:
            self.respond_text(str(exc), status=HTTPStatus.FORBIDDEN)
            return
        except FileNotFoundError as exc:
            self.respond_text(str(exc), status=HTTPStatus.NOT_FOUND)
            return
        except OSError as exc:
            self.respond_text(str(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        # Keep the small handler doubles used by callers/tests working while the
        # real HTTP handler streams files and supports Range requests.
        if not hasattr(self, "respond_server_file"):
            self.respond_bytes(served_file.path.read_bytes(), served_file.content_type)
            return
        self.respond_server_file(served_file)

    def respond_video_preview(self, raw_file_id: str) -> None:
        try:
            served_file = server_files.resolve_video_preview_file(self.server.target, raw_file_id)
        except PermissionError as exc:
            self.respond_text(str(exc), status=HTTPStatus.FORBIDDEN)
            return
        except FileNotFoundError as exc:
            self.respond_text(str(exc), status=HTTPStatus.NOT_FOUND)
            return
        except OSError as exc:
            self.respond_text(str(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.respond_server_file(served_file)

    def respond_server_file(self, served_file: server_files.ServerFilePath) -> None:
        try:
            byte_range = server_files.parse_byte_range(
                self.headers.get("Range"),
                served_file.size,
            )
        except ValueError:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{served_file.size}")
            self.send_header("Content-Length", "0")
            self.send_header("Accept-Ranges", "bytes")
            self.respond_timing_headers()
            self.end_headers()
            return

        start = byte_range.start if byte_range is not None else 0
        end = byte_range.end if byte_range is not None else served_file.size - 1
        length = byte_range.length if byte_range is not None else served_file.size
        self.send_response(HTTPStatus.PARTIAL_CONTENT if byte_range is not None else HTTPStatus.OK)
        self.send_header("Content-Type", served_file.content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if byte_range is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{served_file.size}")
        self.respond_timing_headers()
        self.end_headers()
        remaining = length
        try:
            with served_file.path.open("rb") as stream:
                stream.seek(start)
                while remaining > 0:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError as exc:
            if not client_disconnected_error(exc):
                raise

    def respond_help(self, raw_help_path: str) -> None:
        doc_asset_path = resolve_doc_asset_path(raw_help_path)
        if doc_asset_path is not None:
            if not doc_asset_path.is_file():
                self.respond_text(
                    "Hjelpebildet finnes ikke.", status=HTTPStatus.NOT_FOUND
                )
                return
            content_type = (
                mimetypes.guess_type(doc_asset_path.name)[0]
                or "application/octet-stream"
            )
            try:
                content = doc_asset_path.read_bytes()
            except OSError as exc:
                self.respond_text(str(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.respond_bytes(content, content_type)
            return
        if server_markdown.doc_asset_path_has_image_suffix(raw_help_path):
            self.respond_text("Ugyldig hjelpebilde.", status=HTTPStatus.FORBIDDEN)
            return

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

    def respond_readme(self) -> None:
        readme_path = server_app.server_program_repo_root() / "README.md"
        if not readme_path.is_file():
            self.respond_text("README.md finnes ikke.", status=HTTPStatus.NOT_FOUND)
            return
        try:
            markdown = readme_path.read_text(encoding="utf-8")
        except OSError as exc:
            self.respond_text(str(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.respond_html(
            markdown_doc_page_html(
                readme_path,
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
            self.respond_json(
                {"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST
            )
            return
        item = active_item_by_id_including_hidden(self.server.target, file_id)
        if item is None:
            self.respond_json(
                {"ok": False, "error": "Filen finnes ikke."},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        self.respond_json(
            {
                "ok": True,
                "html": image_info_content_html(
                    self.server.target,
                    item,
                    read_only=getattr(self.server, "read_only", False),
                ),
            }
        )

    def respond_item_faces(self, query: str) -> None:
        if not self.server.face_enabled:
            self.respond_json(
                {"ok": False, "error": "Ansiktsgjenkjenning er av."},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        params = urllib.parse.parse_qs(query)
        try:
            file_id = parse_file_id(first_param(params, "file_id"))
        except ValueError as exc:
            self.respond_json(
                {"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST
            )
            return
        item = browser_item_by_id(self.server.target, file_id)
        if item is None:
            self.respond_json(
                {"ok": False, "error": "Filen finnes ikke."},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        self.respond_json(
            {
                "ok": True,
                "html": face_overlay_content_html(
                    self.server.target,
                    item,
                    self.server.config.face_recognition,
                    person_reference_links_enabled=self.server.config.browser.person_reference_links_enabled,
                    read_only=getattr(self.server, "read_only", False),
                ),
            }
        )

    def respond_item_face_matches(self, query: str) -> None:
        if not self.server.face_enabled:
            self.respond_json(
                {"ok": False, "error": "Ansiktsgjenkjenning er av."},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        params = urllib.parse.parse_qs(query)
        try:
            file_id = parse_file_id(first_param(params, "file_id"))
        except ValueError as exc:
            self.respond_json(
                {"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST
            )
            return
        item = browser_item_by_id(self.server.target, file_id)
        if item is None:
            self.respond_json(
                {"ok": False, "error": "Filen finnes ikke."},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        try:
            content = item_face_matches_content_html(
                self.server.target,
                item,
                self.server.config.face_recognition,
                read_only=getattr(self.server, "read_only", False),
            )
        except Exception as exc:  # noqa: BLE001 - API should return JSON errors
            self.respond_json(
                {"ok": False, "error": f"Kunne ikke beregne ansiktstreff: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self.respond_json({"ok": True, "html": content})

    def respond_thumbnail_maintenance(self) -> None:
        try:
            status = server_app.thumbnail_maintenance_status(self.server.target)
        except ValueError as exc:
            self.respond_json(
                {"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST
            )
            return
        except Exception as exc:  # noqa: BLE001 - API should return JSON errors
            self.respond_json(
                {"ok": False, "error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self.respond_json(
            {
                "ok": True,
                "name": status.name,
                "total": status.total,
                "current": status.scanned,
                "missing": status.missing,
            }
        )

    def respond_maintenance_statuses(self) -> None:
        try:
            statuses = server_app.maintenance_statuses(
                self.server.target, self.server.config
            )
        except ValueError as exc:
            self.respond_json(
                {"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST
            )
            return
        except Exception as exc:  # noqa: BLE001 - API should return JSON errors
            self.respond_json(
                {"ok": False, "error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self.respond_json(
            {
                "ok": True,
                "statuses": [
                    server_app.maintenance_status_to_json(status) for status in statuses
                ],
            }
        )

    def read_json_payload(self) -> dict[str, object]:
        return server_request.read_json_payload(self.headers, self.rfile)

    def read_face_person_payload(
        self,
    ) -> tuple[str, int] | tuple[dict[str, object], HTTPStatus]:
        return server_request.read_face_person_payload(self.headers, self.rfile)
