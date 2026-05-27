from __future__ import annotations

import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import db
from .config import FaceRecognitionConfig
from .geo import PredefinedGeoPlace
from .html_export import month_key_from_path
from .openclip import relative_to_target
from .server_geo import geo_place_cells_by_column


MONTH_PATH_RE = re.compile(r"(?:^|[\\/])(?P<year>\d{4})[\\/](?P<month>\d{2})(?:[\\/]|$)")
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


def first_browser_item(target: Path) -> Any | None:
    return first_source_item(target, all_browser_source())


def first_source_item(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
) -> Any | None:
    if source_has_sql_filter(source):
        return first_sql_filtered_source_item(target, source)
    if source.person_name is not None or source.source_id is not None:
        items = source_items(target, source, face_config)
        return items[0] if items else None
    if not is_filtered_source(source):
        return first_unfiltered_source_item(target)
    items = source_items(target, source, face_config)
    return items[0] if items else None


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


def browser_item_by_id(target: Path, file_id: int) -> Any | None:
    return source_item_by_id(target, all_browser_source(), file_id)


def source_item_by_id(
    target: Path,
    source: BrowserSource,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> Any | None:
    if source_has_sql_filter(source):
        return sql_filtered_source_item_by_id(target, source, file_id)
    if source.person_name is not None or source.source_id is not None:
        return next((item for item in source_items(target, source, face_config) if int(item["id"]) == file_id), None)
    return unfiltered_source_item_by_id(target, file_id)


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


def adjacent_browser_items(target: Path, item: Any) -> tuple[Any | None, Any | None]:
    return adjacent_source_items(target, all_browser_source(), item)


def adjacent_source_items(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> tuple[Any | None, Any | None]:
    if source_has_sql_filter(source):
        return adjacent_sql_filtered_source_items(target, source, item)
    if source.person_name is not None or source.source_id is not None:
        return adjacent_items_from_list(source_items(target, source, face_config), item)
    return adjacent_unfiltered_source_items(target, item)


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


def browser_month_keys(target: Path) -> list[str]:
    return source_month_keys(target, all_browser_source())


def source_month_keys(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
) -> list[str]:
    if source_has_sql_filter(source):
        return sql_filtered_source_month_keys(target, source)
    if source.person_name is not None or source.source_id is not None:
        keys = {month_key_for_item(target, item) for item in source_items(target, source, face_config)}
        return sorted(key for key in keys if valid_month_key(key))
    db_path = db.db_path_for_target(target)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return list(cached_browser_month_keys(str(target.resolve()), mtime_ns))


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


def browser_month_navigation(target: Path, item: Any) -> dict[str, str | None]:
    current_key = month_key_for_item(target, item)
    return browser_month_navigation_for_key(target, current_key)


def month_key_for_item(target: Path, item: Any) -> str:
    stored_key = month_key_from_stored_path(str(item["target_path"]))
    if stored_key is not None:
        return stored_key
    return month_key_from_path(relative_to_target(target, Path(str(item["target_path"]))))


def month_key_from_stored_path(path: str) -> str | None:
    match = MONTH_PATH_RE.search(path.replace("\\\\", "\\"))
    if match is None:
        return None
    month_key = f"{match.group('year')}-{match.group('month')}"
    return month_key if valid_month_key(month_key) else None


def source_items(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
) -> list[Any]:
    if source.person_name is not None:
        from .server_faces import person_items

        return person_items(
            target,
            source.person_name,
            include_suggestions=source.include_suggestions,
            face_config=face_config,
        )
    if source.geo_place_slug is not None:
        from .server_geo import geo_place_items

        return geo_place_items(target, source.geo_place_slug)
    if source.date_source is not None:
        return date_source_items(target, source.date_source)
    if source.source_id is not None:
        return imported_source_items(target, source.source_id)
    return all_source_items(target)


def person_item_by_id(target: Path, person_name: str, file_id: int) -> Any | None:
    return source_item_by_id(target, person_browser_source(person_name, include_suggestions=True), file_id)


def adjacent_person_items(target: Path, person_name: str, item: Any) -> tuple[Any | None, Any | None]:
    return adjacent_source_items(target, person_browser_source(person_name, include_suggestions=True), item)


def person_month_navigation(target: Path, person_name: str, item: Any) -> dict[str, str | None]:
    return source_month_navigation(target, person_browser_source(person_name, include_suggestions=True), item)


def source_month_navigation(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> dict[str, str | None]:
    return source_month_navigation_for_key(target, source, month_key_for_item(target, item), face_config)


def source_month_navigation_for_key(
    target: Path,
    source: BrowserSource,
    current_key: str,
    face_config: FaceRecognitionConfig | None = None,
) -> dict[str, str | None]:
    if not valid_month_key(current_key):
        return {"previous_year": None, "next_year": None, "previous_month": None, "next_month": None}
    keys = source_month_keys(target, source, face_config)
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
    return source_month_items(target, person_browser_source(person_name, include_suggestions=True), month_key)


def source_month_items(
    target: Path,
    source: BrowserSource,
    month_key: str,
    face_config: FaceRecognitionConfig | None = None,
) -> list[Any]:
    if source_has_sql_filter(source):
        return sql_filtered_source_month_items(target, source, month_key)
    if source.person_name is not None or source.source_id is not None:
        return [
            item
            for item in source_items(target, source, face_config)
            if month_key_for_item(target, item) == month_key
        ]
    return browser_month_items(target, month_key)


def sql_filtered_source_month_items(target: Path, source: BrowserSource, month_key: str) -> list[Any]:
    if not valid_month_key(month_key):
        return []
    where_sql, params = source_sql_filter(source)
    year, month = month_key.split("-", 1)
    path_glob = f"{year}/{month}/*"
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                  AND ({where_sql})
                  AND target_path GLOB ?
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (*params, path_glob),
            )
        )
    finally:
        conn.close()


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
    prefix = db.relative_path_key(Path(year) / month) + "/"
    conn = db.connect(target)
    try:
        rows = list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                  AND target_path_key LIKE ?
                ORDER BY {ITEM_ORDER_SQL}
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
                ORDER BY {ITEM_ORDER_SQL}
                """
            )
            if month_key_from_stored_path(str(row["target_path"])) == month_key
        ]
    finally:
        conn.close()
