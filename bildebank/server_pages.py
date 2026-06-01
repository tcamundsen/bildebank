from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import AppConfig, FaceRecognitionConfig
from . import server_app
from . import server_browser
from .server_browser import adjacent_browser_items, browser_month_navigation, first_browser_item
from .server_browser_sources import BrowserSource
from . import server_faces
from . import server_geo
from .server_geo import DEFAULT_GEO_LIMIT, DEFAULT_GEO_MIN_COUNT, DEFAULT_GEO_RESOLUTION
from . import server_markdown
from . import server_search
from .server_search import ServerSearchStats
from .server_assets import page_html
from . import server_shell


def index_html(server: Any, *, message: str = "") -> str:
    if message:
        return search_start_html(server, message=message)
    item = first_browser_item(server.target, hide_out_of_focus=server.config.browser.hide_out_of_focus)
    if item is None:
        return empty_browser_html(face_enabled=server.face_enabled, openclip_enabled=server.openclip_enabled)
    previous_item, next_item = adjacent_browser_items(
        server.target,
        item,
        hide_out_of_focus=server.config.browser.hide_out_of_focus,
    )
    month_nav = browser_month_navigation(
        server.target,
        item,
        hide_out_of_focus=server.config.browser.hide_out_of_focus,
    )
    return item_page_html(
        server.target,
        item,
        previous_item,
        next_item,
        month_nav,
        face_enabled=server.face_enabled,
        openclip_enabled=server.openclip_enabled,
        manual_h3_cell=server.config.browser.manual_h3_cell,
    )


def search_start_html(server: Any, *, message: str = "") -> str:
    return server_search.search_start_html(
        server.config.openclip,
        shell_page_html=shell_page_html,
        model_loaded=server.search_cache.loaded,
        message=message,
        face_enabled=server.face_enabled,
        openclip_enabled=server.openclip_enabled,
    )


def search_html(server: Any, stats: ServerSearchStats, limit: int) -> str:
    return server_search.search_html(
        server.target,
        stats,
        limit,
        shell_page_html=shell_page_html,
        model_loaded=server.search_cache.loaded,
        face_enabled=server.face_enabled,
        openclip_enabled=server.openclip_enabled,
    )


def geo_index_page_html(
    target: Path,
    *,
    resolution: int = DEFAULT_GEO_RESOLUTION,
    min_count: int = DEFAULT_GEO_MIN_COUNT,
    limit: int = DEFAULT_GEO_LIMIT,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    return server_geo.geo_index_page_html(
        target,
        shell_page_html=shell_page_html,
        resolution=resolution,
        min_count=min_count,
        limit=limit,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def custom_geo_places_page_html(target: Path, *, face_enabled: bool = True, openclip_enabled: bool = True) -> str:
    return server_geo.custom_geo_places_page_html(
        target,
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def geo_map_page_html(
    target: Path,
    *,
    resolution: int = DEFAULT_GEO_RESOLUTION,
    min_count: int = DEFAULT_GEO_MIN_COUNT,
    limit: int = DEFAULT_GEO_LIMIT,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    return server_geo.geo_map_page_html(
        target,
        shell_page_html=shell_page_html,
        resolution=resolution,
        min_count=min_count,
        limit=limit,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def geo_stats_page_html(target: Path, *, face_enabled: bool = True, openclip_enabled: bool = True) -> str:
    return server_geo.geo_stats_page_html(
        target,
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def geo_area_page_html(
    target: Path,
    h3_cell: str,
    *,
    resolution: int,
    limit: int = DEFAULT_GEO_LIMIT,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    hide_out_of_focus: bool = False,
) -> str:
    return server_geo.geo_area_page_html(
        target,
        h3_cell,
        shell_page_html=shell_page_html,
        resolution=resolution,
        limit=limit,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        hide_out_of_focus=hide_out_of_focus,
    )


def geo_missing_page_html(
    target: Path,
    *,
    limit: int = DEFAULT_GEO_LIMIT,
    offset: int = 0,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    hide_out_of_focus: bool = False,
) -> str:
    return server_geo.geo_missing_page_html(
        target,
        shell_page_html=shell_page_html,
        limit=limit,
        offset=offset,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        hide_out_of_focus=hide_out_of_focus,
    )


def error_html(exc: Exception, *, face_enabled: bool = True, openclip_enabled: bool = True) -> str:
    return server_shell.error_html(
        exc,
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def markdown_doc_page_html(
    doc_path: Path,
    markdown: str,
    *,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    return server_markdown.markdown_doc_page_html(
        doc_path,
        markdown,
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def empty_browser_html(*, face_enabled: bool = True, openclip_enabled: bool = True) -> str:
    return server_browser.empty_browser_html(
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
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
    manual_h3_cell: str = "",
) -> str:
    return server_browser.item_page_html(
        target,
        item,
        previous_item,
        next_item,
        month_nav,
        page_html=page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        face_config=face_config,
        manual_h3_cell=manual_h3_cell,
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
    manual_h3_cell: str = "",
    hide_out_of_focus: bool = False,
) -> str:
    return server_browser.source_item_page_html(
        target,
        source,
        item,
        previous_item,
        next_item,
        month_nav,
        page_html=page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        face_config=face_config,
        manual_h3_cell=manual_h3_cell,
        hide_out_of_focus=hide_out_of_focus,
    )


def shell_page_html(
    title: str,
    content: str,
    *,
    main_class: str = "shell",
    source: BrowserSource | None = None,
    item: Any | None = None,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    title_html: str | None = None,
) -> str:
    return server_shell.shell_page_html(
        title,
        content,
        page_html=page_html,
        main_class=main_class,
        source=source,
        item=item,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        title_html=title_html,
    )


def app_status_page_html(target: Path, config: AppConfig | None = None) -> str:
    return server_app.app_status_page_html(
        target,
        config,
        shell_page_html=shell_page_html,
        module_available_func=server_app.module_available,
    )


def h3_cells_page_html(target: Path, *, face_enabled: bool = True, openclip_enabled: bool = True) -> str:
    return server_app.h3_cells_page_html(
        target,
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def removed_files_page_html(target: Path, *, face_enabled: bool = True, openclip_enabled: bool = True) -> str:
    return server_app.removed_files_page_html(
        target,
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def person_item_page_html(
    target: Path,
    person_name: str,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
) -> str:
    return server_browser.person_item_page_html(
        target,
        person_name,
        item,
        previous_item,
        next_item,
        month_nav,
        page_html=page_html,
    )


def month_page_html(target: Path, month_key: str, items: list[Any]) -> str:
    return server_browser.month_page_html(target, month_key, items, page_html=page_html)


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
    return server_browser.source_month_page_html(
        target,
        source,
        month_key,
        items,
        page_html=page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        face_config=face_config,
    )


def source_year_months_page_html(
    target: Path,
    source: BrowserSource,
    year: str,
    *,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
    hide_out_of_focus: bool = False,
) -> str:
    return server_browser.source_year_months_page_html(
        target,
        source,
        year,
        page_html=page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        face_config=face_config,
        hide_out_of_focus=hide_out_of_focus,
    )


def years_page_html(
    target: Path,
    *,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    hide_out_of_focus: bool = False,
) -> str:
    return server_browser.years_page_html(
        target,
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        hide_out_of_focus=hide_out_of_focus,
    )


def year_months_page_html(
    target: Path,
    year: str,
    *,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    hide_out_of_focus: bool = False,
) -> str:
    return server_browser.year_months_page_html(
        target,
        year,
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        hide_out_of_focus=hide_out_of_focus,
    )


def empty_person_browser_html(person: str | BrowserSource, *, openclip_enabled: bool = True) -> str:
    return server_browser.empty_person_browser_html(
        person,
        shell_page_html=shell_page_html,
        openclip_enabled=openclip_enabled,
    )


def empty_source_html(source: BrowserSource, *, face_enabled: bool = True, openclip_enabled: bool = True) -> str:
    return server_browser.empty_source_html(
        source,
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def sources_page_html(target: Path, *, face_enabled: bool = True, openclip_enabled: bool = True) -> str:
    return server_browser.sources_page_html(
        target,
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def tags_page_html(target: Path, *, face_enabled: bool = True, openclip_enabled: bool = True) -> str:
    return server_browser.tags_page_html(
        target,
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def person_not_found_html(
    person_name: str,
    *,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    return server_browser.person_not_found_html(
        person_name,
        shell_page_html=shell_page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def people_page_html(
    target: Path,
    face_config: FaceRecognitionConfig | None = None,
    *,
    openclip_enabled: bool = True,
) -> str:
    return server_faces.people_page_html(
        target,
        face_config,
        shell_page_html=shell_page_html,
        openclip_enabled=openclip_enabled,
    )


def person_month_page_html(target: Path, person_name: str, month_key: str, items: list[Any]) -> str:
    return server_browser.person_month_page_html(target, person_name, month_key, items, page_html=page_html)
