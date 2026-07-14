from __future__ import annotations

import hmac
import ipaddress
import secrets
import sys
import time
import urllib.parse
from io import BytesIO
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import (
    AppConfig,
)
from . import server_app
from . import server_handler

from .server_browser_item_html import clear_tag_control_rows_cache
from .server_browser_queries import (
    browser_item_ids,
    browser_month_keys,
    source_item_count as browser_source_item_count,
    source_item_ids,
    source_month_keys,
    valid_day_key,
)
from .server_browser_sidecars import clear_sidecar_data_caches
from .server_browser_sources import (
    BrowserSource,
)
from .server_faces import (
    current_face_db_path,
)
from . import server_markdown
from .server_search import (
    OpenClipSearchCache,
)
from .server_request import first_param


BildebankRequestHandler = server_handler.BildebankRequestHandler


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PREVIEW_MAX_SIZE = 1600
BROWSER_NAVIGATION_CACHE_CHECK_INTERVAL_SECONDS = 1.0


def clear_sidecar_caches() -> None:
    clear_sidecar_data_caches()
    clear_tag_control_rows_cache()


def is_local_bind_host(host: str) -> bool:
    if not host:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_bind_host(host: str, *, allow_remote: bool) -> None:
    if is_local_bind_host(host) or allow_remote:
        return
    raise ValueError(
        f"Kan ikke starte Bildebank-serveren på {host!r} uten --allow-remote. "
        "Denne adressen kan gjøre Bildebank tilgjengelig fra andre maskiner på nettverket. "
        "Angi --allow-remote hvis du vil gjøre dette bevisst."
    )


def client_disconnected_error(exc: OSError) -> bool:
    return isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError))


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


def first_day_item_ids_for_order(target: Path, item_ids: list[int]) -> dict[str, int]:
    if not item_ids:
        return {}
    dates_by_id: dict[int, str] = {}
    conn = db.connect(target)
    try:
        for index in range(0, len(item_ids), 900):
            chunk = item_ids[index : index + 900]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"""
                SELECT id, {db.BROWSER_DATE_ORDER_SQL} AS browser_date
                FROM files
                WHERE id IN ({placeholders})
                """,
                chunk,
            ):
                dates_by_id[int(row["id"])] = str(row["browser_date"])
    finally:
        conn.close()
    first_by_day: dict[str, int] = {}
    for file_id in item_ids:
        day_key = dates_by_id.get(file_id, "")
        if valid_day_key(day_key) and day_key not in first_by_day:
            first_by_day[day_key] = file_id
    return first_by_day


class BildebankServer(ThreadingHTTPServer):
    server_address: tuple[str, int]

    def __init__(
        self,
        address: tuple[str, int],
        target: Path,
        config: AppConfig,
        preview_images: bool = False,
        read_only: bool = False,
    ) -> None:
        super().__init__(address, server_handler.BildebankRequestHandler)
        self.target = target
        self.config = config
        self.preview_images = preview_images
        self.read_only = read_only
        self.csrf_token = secrets.token_urlsafe(32)
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
        self._browser_first_day_item_ids: dict[bool, tuple[int, dict[str, int]]] = {}
        self._source_first_day_item_ids: dict[tuple[BrowserSource, bool], tuple[int, dict[str, int]]] = {}

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

    def browser_first_day_item_id(self, day_key: str, *, hide_out_of_focus: bool = False) -> int | None:
        version = self.browser_navigation_cache_version()
        if not hasattr(self, "_browser_first_day_item_ids"):
            self._browser_first_day_item_ids = {}
        cached = self._browser_first_day_item_ids.get(hide_out_of_focus)
        if cached is None or cached[0] != version:
            item_ids = self.browser_item_ids(hide_out_of_focus=hide_out_of_focus)
            cached = (
                version,
                first_day_item_ids_for_order(self.target, item_ids),
            )
            self._browser_first_day_item_ids[hide_out_of_focus] = cached
        return cached[1].get(day_key)

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

    def source_first_day_item_id(
        self,
        source: BrowserSource,
        day_key: str,
        *,
        hide_out_of_focus: bool = False,
    ) -> int | None:
        version = self.browser_navigation_cache_version()
        cache_key = (source, hide_out_of_focus)
        if not hasattr(self, "_source_first_day_item_ids"):
            self._source_first_day_item_ids = {}
        cached = self._source_first_day_item_ids.get(cache_key)
        if cached is None or cached[0] != version:
            item_ids = self.source_item_order(source, hide_out_of_focus=hide_out_of_focus)[0]
            cached = (
                version,
                first_day_item_ids_for_order(self.target, item_ids),
            )
            self._source_first_day_item_ids[cache_key] = cached
        return cached[1].get(day_key)

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

    def note_tag_navigation_change(self, tag_name: str) -> None:
        tag_key = db.tag_name_key(tag_name)
        if tag_key == db.tag_name_key(db.SYSTEM_TAG_OUT_OF_FOCUS) and self.hide_out_of_focus:
            self.clear_browser_navigation_cache()
            return
        self._clear_source_navigation_cache_for_tag(tag_key)
        self._refresh_browser_navigation_mtimes()

    def _clear_source_navigation_cache_for_tag(self, tag_key: str) -> None:
        def source_matches(source: BrowserSource) -> bool:
            if source.tag_name is not None and db.tag_name_key(source.tag_name) == tag_key:
                return True
            text_filter = source.text_filter
            return bool(text_filter is not None and db.tag_name_key(getattr(text_filter, "tag", "") or "") == tag_key)

        def prune(cache: dict[tuple[BrowserSource, bool], Any]) -> None:
            for cache_key in list(cache):
                source, _hide_out_of_focus = cache_key
                if source_matches(source):
                    del cache[cache_key]

        prune(getattr(self, "_source_item_ids", {}))
        prune(getattr(self, "_source_month_keys", {}))
        prune(getattr(self, "_source_item_counts", {}))
        prune(getattr(self, "_source_first_day_item_ids", {}))

    def _refresh_browser_navigation_mtimes(self) -> None:
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
            getattr(self, "_browser_first_day_item_ids", {}).clear()
            getattr(self, "_source_first_day_item_ids", {}).clear()
            version += 1
            self._browser_navigation_cache_version = version
        return version

    def clear_browser_navigation_cache(self) -> None:
        self._browser_item_ids.clear()
        self._browser_month_keys.clear()
        getattr(self, "_source_item_ids", {}).clear()
        getattr(self, "_source_month_keys", {}).clear()
        getattr(self, "_source_item_counts", {}).clear()
        getattr(self, "_browser_first_day_item_ids", {}).clear()
        getattr(self, "_source_first_day_item_ids", {}).clear()
        clear_sidecar_caches()
        self._browser_navigation_cache_version = getattr(self, "_browser_navigation_cache_version", 0) + 1
        self._refresh_browser_navigation_mtimes()


def resolve_doc_path(raw_doc_path: str) -> Path | None:
    return server_markdown.resolve_doc_path(raw_doc_path, server_app.server_program_repo_root() / "docs")


def resolve_doc_asset_path(raw_asset_path: str) -> Path | None:
    return server_markdown.resolve_doc_asset_path(raw_asset_path, server_app.server_program_repo_root() / "docs")


def run_server(
    target: Path,
    config: AppConfig,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    allow_remote: bool = False,
    preview_images: bool = False,
    read_only: bool = False,
    ready: Callable[[str], None] | None = None,
) -> None:
    validate_bind_host(host, allow_remote=allow_remote)
    if allow_remote and not is_local_bind_host(host):
        print(
            f"ADVARSEL: Bildebank-serveren bindes til {host!r} og kan bli tilgjengelig "
            "fra andre maskiner på nettverket.",
            file=sys.stderr,
        )
    db.prepare_database(target)
    server = BildebankServer((host, port), target, config, preview_images=preview_images, read_only=read_only)
    actual_host, actual_port = server.server_address
    url = f"http://{actual_host}:{actual_port}/"
    if ready is not None:
        ready(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
