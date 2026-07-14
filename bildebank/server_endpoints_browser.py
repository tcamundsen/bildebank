from __future__ import annotations

import time
import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING

from . import db
from .config import FaceRecognitionConfig
from .geo import H3_COLUMNS, h3_resolution
from .server_browser_queries import (
    adjacent_items_from_id_order,
    adjacent_source_items,
    browser_date_for_item,
    imported_source_by_id,
    item_by_id,
    month_key_for_item,
    month_navigation_for_keys,
    source_item_by_id,
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
    parse_source_path,
    source_has_sql_filter,
    tag_browser_source,
)
from .server_filter import text_filter_browser_source
from .server_geo import DEFAULT_GEO_LIMIT, DEFAULT_GEO_MIN_COUNT, DEFAULT_GEO_RESOLUTION, geo_place_by_slug
from .server_pages import (
    filter_start_html,
    geo_area_page_html,
    geo_index_page_html,
    geo_map_page_html,
    source_item_page_html,
    source_month_page_html,
    source_year_months_page_html,
    source_years_page_html,
    year_months_page_html,
    years_page_html,
)
from .server_request import first_param, nonnegative_int_param, parse_file_id, positive_int_param
from .server_response import add_csrf_to_html, read_only_html

if TYPE_CHECKING:
    from .server_handler import BildebankRequestHandler


def respond_browser_root(handler: BildebankRequestHandler) -> None:
    respond_years(handler)


def respond_item(handler: BildebankRequestHandler, raw_file_id: str) -> None:
    start = time.perf_counter()
    file_id = parse_file_id(raw_file_id)
    handler.record_server_timing("parse", start)

    start = time.perf_counter()
    source = all_browser_source()
    browser_db_connection = getattr(handler, "browser_db_connection", None)
    conn, close_conn = (
        browser_db_connection() if browser_db_connection is not None else (db.connect(handler.server.target), True)
    )
    handler.record_server_timing("db_connect", start)
    try:
        start = time.perf_counter()
        item_ids, item_positions = handler.server.browser_item_order(
            hide_out_of_focus=handler.server.hide_out_of_focus
        )
        handler.record_server_timing("browser_item_order", start)

        start = time.perf_counter()
        item = item_by_id(handler.server.target, file_id, conn=conn) if file_id in item_positions else None
        handler.record_server_timing("item_by_id", start)
        if item is None:
            handler.respond_text("Filen finnes ikke i bildesamlingen.", status=HTTPStatus.NOT_FOUND)
            return
        if source == all_browser_source():
            start = time.perf_counter()
            previous_item, next_item = adjacent_items_from_id_order(
                item_ids,
                int(item["id"]),
                item_positions,
            )
            handler.record_server_timing("adjacent", start)

            start = time.perf_counter()
            month_nav = month_navigation_for_keys(
                handler.server.browser_month_keys(hide_out_of_focus=handler.server.hide_out_of_focus),
                month_key_for_item(handler.server.target, item),
            )
            handler.record_server_timing("month_nav", start)
        else:
            start = time.perf_counter()
            previous_item, next_item = adjacent_source_items(
                handler.server.target,
                source,
                item,
                hide_out_of_focus=handler.server.hide_out_of_focus,
                conn=conn,
            )
            handler.record_server_timing("adjacent", start)

            start = time.perf_counter()
            month_nav = source_month_navigation(
                handler.server.target,
                source,
                item,
                hide_out_of_focus=handler.server.hide_out_of_focus,
                conn=conn,
            )
            handler.record_server_timing("month_nav", start)

        start = time.perf_counter()
        first_day_item_id = handler.server.browser_first_day_item_id(
            browser_date_for_item(item),
            hide_out_of_focus=handler.server.hide_out_of_focus,
        )
        handler.record_server_timing("first_day_item", start)

        start = time.perf_counter()
        html = source_item_page_html(
            handler.server.target,
            source,
            item,
            previous_item,
            next_item,
            month_nav,
            face_enabled=handler.server.face_enabled,
            openclip_enabled=handler.server.openclip_enabled,
            face_config=handler.server.config.face_recognition,
            manual_person_controls_enabled=handler.server.config.browser.manual_person_controls_enabled,
            person_reference_links_enabled=handler.server.config.browser.person_reference_links_enabled,
            hotkey_hints_enabled=handler.server.config.browser.hotkey_hints_enabled,
            hotkeys=handler.server.config.browser.hotkeys,
            hide_out_of_focus=handler.server.hide_out_of_focus,
            conn=conn,
            first_day_item_id=first_day_item_id,
            timing_callback=handler.record_server_timing,
            read_only=getattr(handler.server, "read_only", False),
        )
        handler.record_server_timing("source_item_page_html", start)

        start = time.perf_counter()
        if getattr(handler.server, "read_only", False):
            html = read_only_html(html)
        encoded = add_csrf_to_html(html, handler.server.csrf_token).encode("utf-8")
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.record_server_timing("encode/respond_before_write", start)
        handler.respond_timing_headers()
        handler.end_headers()
        handler.wfile.write(encoded)
    finally:
        if close_conn:
            conn.close()


def respond_month(handler: BildebankRequestHandler, raw_month: str) -> None:
    month_key = urllib.parse.unquote(raw_month).strip()
    if not valid_month_key(month_key):
        handler.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
        return
    source = all_browser_source()
    items = source_month_items(
        handler.server.target,
        source,
        month_key,
        hide_out_of_focus=handler.server.hide_out_of_focus,
    )
    handler.respond_html(
        source_month_page_html(
            handler.server.target,
            source,
            month_key,
            items,
            face_enabled=handler.server.face_enabled,
            openclip_enabled=handler.server.openclip_enabled,
            face_config=handler.server.config.face_recognition,
            hide_out_of_focus=handler.server.hide_out_of_focus,
        )
    )


def respond_years(handler: BildebankRequestHandler) -> None:
    handler.respond_html(
        years_page_html(
            handler.server.target,
            face_enabled=handler.server.face_enabled,
            openclip_enabled=handler.server.openclip_enabled,
            hide_out_of_focus=handler.server.hide_out_of_focus,
        )
    )


def respond_year(handler: BildebankRequestHandler, raw_year: str) -> None:
    year = urllib.parse.unquote(raw_year).strip().strip("/")
    if not valid_year_key(year):
        handler.respond_text("Ugyldig år.", status=HTTPStatus.BAD_REQUEST)
        return
    handler.respond_html(
        year_months_page_html(
            handler.server.target,
            year,
            face_enabled=handler.server.face_enabled,
            openclip_enabled=handler.server.openclip_enabled,
            hide_out_of_focus=handler.server.hide_out_of_focus,
        )
    )


def respond_filter(handler: BildebankRequestHandler, query: str) -> None:
    params = urllib.parse.parse_qs(query)
    raw_query = first_param(params, "q").strip()
    if not raw_query:
        handler.respond_html(filter_start_html(handler.server))
        return
    try:
        url = text_filter_browser_source(raw_query, handler.server.target).root_url
    except ValueError as exc:
        handler.respond_html(filter_start_html(handler.server, query=raw_query, message=str(exc)))
        return
    handler.redirect(url)


def respond_filter_source(handler: BildebankRequestHandler, raw_path: str) -> None:
    raw_query, page_mode, raw_value = parse_source_path(raw_path)
    query = urllib.parse.unquote(raw_query).strip()
    try:
        source = text_filter_browser_source(query, handler.server.target)
    except ValueError as exc:
        handler.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
        return
    respond_browser_source(
        handler,
        source,
        page_mode,
        raw_value,
        hide_out_of_focus=handler.server.hide_out_of_focus,
        item_not_found_message="Filen finnes ikke for dette filtersøket.",
        invalid_page_message="Ugyldig filtersøkside.",
    )


def respond_imported_source(handler: BildebankRequestHandler, raw_path: str) -> None:
    raw_source_id, page_mode, raw_value = parse_source_path(raw_path)
    try:
        source_id = int(urllib.parse.unquote(raw_source_id).strip())
    except ValueError:
        handler.respond_text("Ugyldig kilde.", status=HTTPStatus.BAD_REQUEST)
        return
    source_row = imported_source_by_id(handler.server.target, source_id)
    if source_row is None:
        handler.respond_text("Fant ikke kilde.", status=HTTPStatus.NOT_FOUND)
        return
    source = imported_source_browser_source(source_row)
    respond_browser_source(
        handler,
        source,
        page_mode,
        raw_value,
        hide_out_of_focus=handler.server.hide_out_of_focus,
        item_not_found_message="Filen finnes ikke for denne kilden.",
        invalid_page_message="Ugyldig kildeside.",
    )


def respond_tag(handler: BildebankRequestHandler, raw_path: str) -> None:
    raw_tag_name, page_mode, raw_value = parse_source_path(raw_path)
    tag_name = urllib.parse.unquote(raw_tag_name).strip()
    try:
        source = tag_browser_source(tag_name)
    except ValueError as exc:
        handler.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
        return
    respond_browser_source(
        handler,
        source,
        page_mode,
        raw_value,
        hide_out_of_focus=handler.server.hide_out_of_focus,
        item_not_found_message="Filen finnes ikke for denne taggen.",
        invalid_page_message="Ugyldig taggside.",
    )


def respond_geo(handler: BildebankRequestHandler, query: str) -> None:
    params = urllib.parse.parse_qs(query)
    resolution = nonnegative_int_param(params, "resolution", DEFAULT_GEO_RESOLUTION)
    min_count = positive_int_param(params, "min_count", DEFAULT_GEO_MIN_COUNT)
    limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
    if resolution not in H3_COLUMNS:
        handler.respond_text("H3-oppløsning må være mellom 0 og 11.", status=HTTPStatus.BAD_REQUEST)
        return
    handler.respond_html(
        geo_index_page_html(
            handler.server.target,
            resolution=resolution,
            min_count=min_count,
            limit=limit,
            face_enabled=handler.server.face_enabled,
            openclip_enabled=handler.server.openclip_enabled,
        )
    )


def respond_geo_map(handler: BildebankRequestHandler, query: str) -> None:
    params = urllib.parse.parse_qs(query)
    resolution = nonnegative_int_param(params, "resolution", DEFAULT_GEO_RESOLUTION)
    min_count = positive_int_param(params, "min_count", DEFAULT_GEO_MIN_COUNT)
    limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
    if resolution not in H3_COLUMNS:
        handler.respond_text("H3-oppløsning må være mellom 0 og 11.", status=HTTPStatus.BAD_REQUEST)
        return
    handler.respond_html(
        geo_map_page_html(
            handler.server.target,
            resolution=resolution,
            min_count=min_count,
            limit=limit,
            face_enabled=handler.server.face_enabled,
            openclip_enabled=handler.server.openclip_enabled,
        )
    )


def respond_geo_area(handler: BildebankRequestHandler, raw_cell: str, query: str) -> None:
    h3_cell = urllib.parse.unquote(raw_cell).strip()
    params = urllib.parse.parse_qs(query)
    limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
    try:
        resolution = h3_resolution(h3_cell)
    except ValueError as exc:
        handler.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
        return
    handler.respond_html(
        geo_area_page_html(
            handler.server.target,
            h3_cell,
            resolution=resolution,
            limit=limit,
            face_enabled=handler.server.face_enabled,
            openclip_enabled=handler.server.openclip_enabled,
            hide_out_of_focus=handler.server.hide_out_of_focus,
        )
    )


def respond_geo_place(handler: BildebankRequestHandler, raw_path: str) -> None:
    raw_slug, page_mode, raw_value = parse_source_path(raw_path)
    slug = urllib.parse.unquote(raw_slug).strip()
    place = geo_place_by_slug(handler.server.target, slug)
    if place is None:
        handler.respond_text("Ukjent sted.", status=HTTPStatus.NOT_FOUND)
        return
    source = geo_place_browser_source(place)
    respond_browser_source(
        handler,
        source,
        page_mode,
        raw_value,
        hide_out_of_focus=handler.server.hide_out_of_focus,
        item_not_found_message="Filen finnes ikke for dette stedet.",
        invalid_page_message="Ugyldig stedsside.",
    )


def respond_browser_source(
    handler: BildebankRequestHandler,
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
        handler.respond_html(
            source_years_page_html(
                handler.server.target,
                source,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
                face_config=handler.server.config.face_recognition,
                hide_out_of_focus=hide_out_of_focus,
            )
        )
        return
    if page_mode == "item":
        start = time.perf_counter()
        file_id = parse_file_id(raw_value)
        handler.record_server_timing("parse", start)
        start = time.perf_counter()
        browser_db_connection = getattr(handler, "browser_db_connection", None)
        conn, close_conn = (
            browser_db_connection() if browser_db_connection is not None else (db.connect(handler.server.target), True)
        )
        handler.record_server_timing("db_connect", start)
        try:
            if source_has_sql_filter(source):
                start = time.perf_counter()
                item_ids, item_positions = handler.server.source_item_order(
                    source,
                    hide_out_of_focus=hide_out_of_focus,
                )
                handler.record_server_timing("source_item_order", start)
                start = time.perf_counter()
                item = item_by_id(handler.server.target, file_id, conn=conn) if file_id in item_positions else None
                handler.record_server_timing("item_by_id", start)
            else:
                start = time.perf_counter()
                item = source_item_by_id(
                    handler.server.target,
                    source,
                    file_id,
                    face_config,
                    hide_out_of_focus=hide_out_of_focus,
                    conn=conn,
                )
                handler.record_server_timing("item_by_id", start)
            if item is None:
                handler.respond_text(item_not_found_message, status=HTTPStatus.NOT_FOUND)
                return
            if source_has_sql_filter(source):
                start = time.perf_counter()
                previous_item, next_item = adjacent_items_from_id_order(item_ids, int(item["id"]), item_positions)
                handler.record_server_timing("adjacent", start)
                start = time.perf_counter()
                month_nav = month_navigation_for_keys(
                    handler.server.source_month_keys(source, hide_out_of_focus=hide_out_of_focus),
                    month_key_for_item(handler.server.target, item),
                )
                handler.record_server_timing("month_nav", start)
                start = time.perf_counter()
                first_day_item_id = handler.server.source_first_day_item_id(
                    source,
                    browser_date_for_item(item),
                    hide_out_of_focus=hide_out_of_focus,
                )
                handler.record_server_timing("first_day_item", start)
            else:
                start = time.perf_counter()
                previous_item, next_item = adjacent_source_items(
                    handler.server.target,
                    source,
                    item,
                    face_config,
                    hide_out_of_focus=hide_out_of_focus,
                    conn=conn,
                )
                handler.record_server_timing("adjacent", start)
                start = time.perf_counter()
                month_nav = source_month_navigation(
                    handler.server.target,
                    source,
                    item,
                    face_config,
                    hide_out_of_focus=hide_out_of_focus,
                    conn=conn,
                )
                handler.record_server_timing("month_nav", start)
                first_day_item_id = None
            start = time.perf_counter()
            handler.respond_html(
                source_item_page_html(
                    handler.server.target,
                    source,
                    item,
                    previous_item,
                    next_item,
                    month_nav,
                    face_enabled=handler.server.face_enabled,
                    openclip_enabled=handler.server.openclip_enabled,
                    face_config=handler.server.config.face_recognition,
                    manual_person_controls_enabled=handler.server.config.browser.manual_person_controls_enabled,
                    person_reference_links_enabled=handler.server.config.browser.person_reference_links_enabled,
                    hotkey_hints_enabled=handler.server.config.browser.hotkey_hints_enabled,
                    hotkeys=handler.server.config.browser.hotkeys,
                    hide_out_of_focus=hide_out_of_focus,
                    conn=conn,
                    source_item_count_value=(
                        handler.server.source_item_count(source, hide_out_of_focus=hide_out_of_focus)
                        if source.text_filter is not None
                        else None
                    ),
                    first_day_item_id=first_day_item_id,
                    timing_callback=handler.record_server_timing,
                    read_only=getattr(handler.server, "read_only", False),
                )
            )
            handler.record_server_timing("source_item_page_html", start)
        finally:
            if close_conn:
                conn.close()
        return
    if page_mode == "month":
        month_key = urllib.parse.unquote(raw_value).strip()
        if not valid_month_key(month_key):
            handler.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
            return
        items = source_month_items(
            handler.server.target,
            source,
            month_key,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
        )
        handler.respond_html(
            source_month_page_html(
                handler.server.target,
                source,
                month_key,
                items,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
                face_config=handler.server.config.face_recognition,
                hide_out_of_focus=hide_out_of_focus,
            )
        )
        return
    if page_mode == "year":
        year = urllib.parse.unquote(raw_value).strip()
        if not valid_year_key(year):
            handler.respond_text("Ugyldig år.", status=HTTPStatus.BAD_REQUEST)
            return
        handler.respond_html(
            source_year_months_page_html(
                handler.server.target,
                source,
                year,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
                face_config=handler.server.config.face_recognition,
                hide_out_of_focus=hide_out_of_focus,
            )
        )
        return
    handler.respond_text(invalid_page_message, status=HTTPStatus.NOT_FOUND)
