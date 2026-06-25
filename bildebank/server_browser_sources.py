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
    reference_suggestions_person_name: str | None = None
    reference_suggestions_file_id: int | None = None
    missing_face_suggestions: bool = False
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


def parse_person_reference_suggestions_path(raw_path: str) -> tuple[str, str, str | None, str]:
    source_part, page_mode, raw_value = parse_source_path(raw_path)
    raw_name, separator, raw_file_id = source_part.partition("/references/")
    if not separator:
        return source_part, "", page_mode, raw_value
    return raw_name.strip("/"), raw_file_id.strip("/"), page_mode, raw_value


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
    return BrowserSource(title, root_url, person_name=person_name, include_suggestions=include_suggestions, show_faces=show_faces)


def person_reference_suggestions_browser_source(person_name: str, reference_file_id: int) -> BrowserSource:
    root_url = (
        "/people/"
        + urllib.parse.quote(person_name, safe="")
        + f"/references/{reference_file_id}"
    )
    return BrowserSource(
        f"Forslag fra referansebilde: {person_name}",
        root_url,
        reference_suggestions_person_name=person_name,
        reference_suggestions_file_id=reference_file_id,
    )


def missing_face_suggestions_browser_source() -> BrowserSource:
    return BrowserSource(
        "Ansikter uten forslag",
        "/people/missing-suggestions",
        missing_face_suggestions=True,
    )


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
        or source.reference_suggestions_person_name is not None
        or source.missing_face_suggestions
        or source.source_id is not None
        or source.geo_place_slug is not None
        or source.tag_name is not None
        or source.text_filter is not None
    )


def source_has_sql_filter(source: BrowserSource) -> bool:
    if source.text_filter is not None:
        from .server_filter import text_filter_has_runtime_filter

        return not text_filter_has_runtime_filter(source.text_filter)
    return (
        source.person_name is not None
        or source.reference_suggestions_person_name is not None
        or source.missing_face_suggestions
        or source.source_id is not None
        or source.geo_place_slug is not None
        or source.text_filter is not None
    )


def source_includes_deleted(source: BrowserSource) -> bool:
    return bool(source.text_filter is not None and getattr(source.text_filter, "deleted", False))


def source_sql_filter(source: BrowserSource) -> tuple[str, tuple[object, ...]]:
    if source.reference_suggestions_person_name is not None:
        from .face import normalize_person_name

        if source.reference_suggestions_file_id is None:
            raise ValueError("Referansebilde mangler.")
        return (
            """
            files.id IN (
                SELECT suggested_faces.file_id
                FROM face_db.persons
                JOIN face_db.face_suggestions
                  ON face_db.face_suggestions.person_id = face_db.persons.id
                JOIN face_db.faces AS suggested_faces
                  ON suggested_faces.id = face_db.face_suggestions.face_id
                JOIN face_db.faces AS reference_faces
                  ON reference_faces.id = face_db.face_suggestions.reference_face_id
                JOIN face_db.person_faces
                  ON face_db.person_faces.person_id = face_db.persons.id
                 AND face_db.person_faces.face_id = reference_faces.id
                WHERE face_db.persons.name = ?
                  AND reference_faces.file_id = ?
            )
            """,
            (normalize_person_name(source.reference_suggestions_person_name), source.reference_suggestions_file_id),
        )
    if source.person_name is not None:
        from .face import normalize_person_name

        person_name = normalize_person_name(source.person_name)
        selectors = [
            """
            SELECT face_db.faces.file_id
            FROM face_db.persons
            JOIN face_db.person_faces ON face_db.person_faces.person_id = face_db.persons.id
            JOIN face_db.faces ON face_db.faces.id = face_db.person_faces.face_id
            WHERE face_db.persons.name = ?
            """,
        ]
        params: tuple[object, ...] = (person_name,)
        if source.include_suggestions:
            selectors.extend(
                [
                    """
                    SELECT face_db.person_files.file_id
                    FROM face_db.persons
                    JOIN face_db.person_files ON face_db.person_files.person_id = face_db.persons.id
                    WHERE face_db.persons.name = ?
                    """,
                    """
                    SELECT face_db.faces.file_id
                    FROM face_db.persons
                    JOIN face_db.face_suggestions ON face_db.face_suggestions.person_id = face_db.persons.id
                    JOIN face_db.faces ON face_db.faces.id = face_db.face_suggestions.face_id
                    WHERE face_db.persons.name = ?
                    """,
                ]
            )
            params = (person_name, person_name, person_name)
        return (
            "files.id IN ("
            + " UNION ".join(selectors)
            + ")",
            params,
        )
    if source.geo_place_slug is not None:
        place = PredefinedGeoPlace(source.geo_place_slug, source.title, source.geo_place_cells)
        return db.geo_place_where_clause(geo_place_cells_by_column(place))
    if source.missing_face_suggestions:
        return (
            """
            EXISTS (
                SELECT 1
                FROM face_db.faces
                WHERE face_db.faces.file_id = files.id
                  AND NOT EXISTS (
                    SELECT 1
                    FROM face_db.person_faces
                    WHERE face_db.person_faces.face_id = face_db.faces.id
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM face_db.face_suggestions
                    WHERE face_db.face_suggestions.face_id = face_db.faces.id
                  )
            )
            """,
            (),
        )
    if source.source_id is not None:
        return (
            """
            EXISTS (
                SELECT 1
                FROM file_sources
                WHERE file_sources.source_id = ?
                  AND file_sources.file_id = files.id
            )
            """,
            (source.source_id,),
        )
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
