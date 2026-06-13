from __future__ import annotations

import sqlite3
import urllib.parse
from dataclasses import dataclass
from typing import Any

from . import db
from .geo import PredefinedGeoPlace
from .server_geo import geo_place_cells_by_column


@dataclass(frozen=True)
class BrowserSource:
    title: str
    root_url: str
    person_name: str | None = None
    include_suggestions: bool = True
    source_id: int | None = None
    show_faces: bool = True
    geo_place_slug: str | None = None
    geo_place_cells: tuple[str, ...] = ()
    tag_name: str | None = None
    text_filter: Any | None = None


def person_url(person_name: str, *, show_faces: bool = True) -> str:
    url = "/person/" + urllib.parse.quote(person_name, safe="")
    return url if show_faces else f"{url}/no-faces"


def parse_source_path(raw_path: str) -> tuple[str, str | None, str]:
    source_part = raw_path.strip("/")
    page_mode = None
    raw_value = ""
    if "/item/" in source_part:
        source_part, raw_value = source_part.split("/item/", 1)
        page_mode = "item"
    elif "/year/" in source_part:
        source_part, raw_value = source_part.split("/year/", 1)
        page_mode = "year"
    elif "/month/" in source_part:
        source_part, raw_value = source_part.split("/month/", 1)
        page_mode = "month"
    return source_part.strip("/"), page_mode, raw_value


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


def person_item_url(person_name: str, file_id: int, *, show_faces: bool = True) -> str:
    return f"{person_url(person_name, show_faces=show_faces)}/item/{file_id}"


def all_browser_source() -> BrowserSource:
    return BrowserSource("Bildebrowser", "/")


def person_browser_source(person_name: str, *, include_suggestions: bool, show_faces: bool = True) -> BrowserSource:
    title = person_name if include_suggestions else f"{person_name} - bekreftet"
    root_url = person_url(person_name) if include_suggestions else f"{person_url(person_name)}/confirmed"
    if not show_faces:
        title = f"{title} - uten ansiktsmarkering"
        root_url = f"{root_url}/no-faces"
    return BrowserSource(title, root_url, person_name, include_suggestions, show_faces=show_faces)


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


def tag_browser_source(tag_name: str) -> BrowserSource:
    name = db.normalize_tag_name(tag_name)
    return BrowserSource(
        f"Tagg: {name}",
        "/tag/" + urllib.parse.quote(name, safe=""),
        tag_name=name,
    )


def is_filtered_source(source: BrowserSource) -> bool:
    return (
        source.person_name is not None
        or source.source_id is not None
        or source.geo_place_slug is not None
        or source.tag_name is not None
        or source.text_filter is not None
    )


def source_has_sql_filter(source: BrowserSource) -> bool:
    if source.text_filter is not None:
        from .server_filter import text_filter_has_runtime_filter

        return not text_filter_has_runtime_filter(source.text_filter)
    return source.geo_place_slug is not None or source.text_filter is not None


def source_includes_deleted(source: BrowserSource) -> bool:
    return bool(source.text_filter is not None and getattr(source.text_filter, "deleted", False))


def source_sql_filter(source: BrowserSource) -> tuple[str, tuple[object, ...]]:
    if source.geo_place_slug is not None:
        place = PredefinedGeoPlace(source.geo_place_slug, source.title, source.geo_place_cells)
        return db.geo_place_where_clause(geo_place_cells_by_column(place))
    if source.text_filter is not None:
        from .server_filter import text_filter_where_clause

        return text_filter_where_clause(source.text_filter)
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


def source_year_url(source: BrowserSource, year: str) -> str:
    quoted = urllib.parse.quote(year)
    if is_filtered_source(source):
        return f"{source.root_url}/year/{quoted}"
    return f"/years/{quoted}"
