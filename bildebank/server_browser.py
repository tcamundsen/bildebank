from __future__ import annotations

import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import db
from .geo import PredefinedGeoPlace
from .server_geo import geo_place_cells_by_column


FILE_COLUMNS = (
    "id, target_path, target_path_key, stored_filename, taken_date, date_source, "
    "size_bytes, view_rotation_degrees, gps_lat, gps_lon, "
    "media_width, media_height, media_orientation, media_metadata_mtime_ns, "
    f"{db.H3_FILE_COLUMNS_SQL}"
)
ITEM_DATE_ORDER_SQL = db.BROWSER_DATE_ORDER_SQL
ITEM_ORDER_SQL = f"{ITEM_DATE_ORDER_SQL}, target_path_key"


@dataclass(frozen=True)
class BrowserSource:
    title: str
    root_url: str
    person_name: str | None = None
    include_suggestions: bool = True
    date_source: str | None = None
    source_id: int | None = None
    show_faces: bool = True
    geo_place_slug: str | None = None
    geo_place_cells: tuple[str, ...] = ()


def person_url(person_name: str, *, show_faces: bool = True) -> str:
    url = "/person/" + urllib.parse.quote(person_name, safe="")
    return url if show_faces else f"{url}/no-faces"


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


def date_source_browser_source(date_source: str) -> BrowserSource:
    labels = {
        "filename": "Dato fra filnavn",
        "mtime": "Dato fra mtime",
    }
    return BrowserSource(labels[date_source], f"/date-source/{date_source}", date_source=date_source)


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


def valid_browser_date_source(date_source: str) -> bool:
    return date_source in {"filename", "mtime"}


def is_filtered_source(source: BrowserSource) -> bool:
    return (
        source.person_name is not None
        or source.date_source is not None
        or source.source_id is not None
        or source.geo_place_slug is not None
    )


def source_has_sql_filter(source: BrowserSource) -> bool:
    return source.date_source is not None or source.geo_place_slug is not None


def source_sql_filter(source: BrowserSource) -> tuple[str, tuple[object, ...]]:
    if source.date_source is not None:
        if not valid_browser_date_source(source.date_source):
            raise ValueError("Ugyldig datokilde.")
        return "date_source = ?", (source.date_source,)
    if source.geo_place_slug is not None:
        place = PredefinedGeoPlace(source.geo_place_slug, source.title, source.geo_place_cells)
        return db.geo_place_where_clause(geo_place_cells_by_column(place))
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


def first_sql_filtered_source_item(target: Path, source: BrowserSource) -> Any | None:
    where_sql, params = source_sql_filter(source)
    conn = db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({where_sql})
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            params,
        ).fetchone()
    finally:
        conn.close()


def first_unfiltered_source_item(target: Path) -> Any | None:
    conn = db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()


def sql_filtered_source_item_by_id(target: Path, source: BrowserSource, file_id: int) -> Any | None:
    where_sql, params = source_sql_filter(source)
    conn = db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND id = ?
              AND ({where_sql})
            """,
            (file_id, *params),
        ).fetchone()
    finally:
        conn.close()


def unfiltered_source_item_by_id(target: Path, file_id: int) -> Any | None:
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


def adjacent_unfiltered_source_items(target: Path, item: Any) -> tuple[Any | None, Any | None]:
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


def adjacent_sql_filtered_source_items(target: Path, source: BrowserSource, item: Any) -> tuple[Any | None, Any | None]:
    where_sql, params = source_sql_filter(source)
    order_key = item_order_key(item)
    conn = db.connect(target)
    try:
        previous_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({where_sql})
              AND ({ITEM_DATE_ORDER_SQL}, target_path_key) < (?, ?)
            ORDER BY {ITEM_DATE_ORDER_SQL} DESC, target_path_key DESC
            LIMIT 1
            """,
            (*params, *order_key),
        ).fetchone()
        next_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({where_sql})
              AND ({ITEM_DATE_ORDER_SQL}, target_path_key) > (?, ?)
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            (*params, *order_key),
        ).fetchone()
        return previous_item, next_item
    finally:
        conn.close()


def valid_month_key(value: str) -> bool:
    if len(value) != 7 or value[4] != "-":
        return False
    year, month = value.split("-", 1)
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= 12


@lru_cache(maxsize=8)
def cached_browser_month_keys(target_path: str, db_mtime_ns: int) -> tuple[str, ...]:
    target = Path(target_path)
    conn = db.connect(target)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT substr(target_path, 1, 4) || '-' || substr(target_path, 6, 2) AS month_key
            FROM files
            WHERE deleted_at IS NULL
              AND target_path GLOB '[0-9][0-9][0-9][0-9]/[0-9][0-9]/*'
            ORDER BY month_key
            """
        )
        return tuple(str(row["month_key"]) for row in rows if valid_month_key(str(row["month_key"])))
    finally:
        conn.close()


def sql_filtered_source_month_keys(target: Path, source: BrowserSource) -> list[str]:
    where_sql, params = source_sql_filter(source)
    conn = db.connect(target)
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT substr(target_path, 1, 4) || '-' || substr(target_path, 6, 2) AS month_key
            FROM files
            WHERE deleted_at IS NULL
              AND ({where_sql})
              AND target_path GLOB '[0-9][0-9][0-9][0-9]/[0-9][0-9]/*'
            ORDER BY month_key
            """,
            params,
        )
        return [str(row["month_key"]) for row in rows if valid_month_key(str(row["month_key"]))]
    finally:
        conn.close()


def date_source_items(target: Path, date_source: str) -> list[Any]:
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
                (date_source,),
            )
        )
    finally:
        conn.close()


def imported_source_items(target: Path, source_id: int) -> list[Any]:
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT
                    files.id,
                    files.target_path,
                    files.target_path_key,
                    files.stored_filename,
                    files.taken_date,
                    files.date_source,
                    files.size_bytes,
                    files.view_rotation_degrees,
                    files.gps_lat,
                    files.gps_lon,
                    files.media_width,
                    files.media_height,
                    files.media_orientation,
                    files.media_metadata_mtime_ns,
                    {db.H3_FILE_COLUMNS_SQL}
                FROM files
                JOIN file_sources ON file_sources.file_id = files.id
                WHERE files.deleted_at IS NULL
                  AND file_sources.source_id = ?
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (source_id,),
            )
        )
    finally:
        conn.close()


def all_source_items(target: Path) -> list[Any]:
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
