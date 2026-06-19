from __future__ import annotations

import html
import re
import sqlite3
import urllib.parse
import datetime as dt
import time
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import BrowserHotkeyConfig, FaceRecognitionConfig, HOTKEY_KEYS
from .formatting import format_bytes
from .geo import H3_COLUMNS, h3_area_label
from .html_export import display_relative_path, month_key_from_path
from .media import media_kind
from .media_cache import cached_image_dimensions
from .openclip import relative_to_target
from .server_browser_sources import (
    BrowserSource,
    all_browser_source,
    imported_source_browser_source,
    is_filtered_source,
    person_browser_source,
    source_has_sql_filter,
    source_includes_deleted,
    source_item_url,
    source_month_url,
    source_sql_filter,
    source_year_url,
)
from .thumbnails import existing_thumbnail_url


ShellPageRenderer = Callable[..., str]
PageRenderer = Callable[[str, str], str]
Breadcrumb = tuple[str, str | None] | tuple[str, str | None, str | None]
MONTH_PATH_RE = re.compile(r"(?:^|[\\/])(?P<year>\d{4})[\\/](?P<month>\d{2})(?:[\\/]|$)")
MONTH_NAMES = {
    "01": "Januar",
    "02": "Februar",
    "03": "Mars",
    "04": "April",
    "05": "Mai",
    "06": "Juni",
    "07": "Juli",
    "08": "August",
    "09": "September",
    "10": "Oktober",
    "11": "November",
    "12": "Desember",
}
FILE_COLUMNS = (
    "id, target_path, target_path_key, original_filename, stored_filename, taken_date, date_source, "
    "manual_date_from, manual_date_to, manual_date_note, "
    "camera_make, camera_model, "
    "size_bytes, view_rotation_degrees, gps_lat, gps_lon, gps_source, "
    "media_width, media_height, media_orientation, media_metadata_mtime_ns, "
    f"{db.H3_FILE_COLUMNS_SQL}"
)
ITEM_DATE_ORDER_SQL = db.BROWSER_DATE_ORDER_SQL
ITEM_ORDER_SQL = f"{ITEM_DATE_ORDER_SQL}, target_path_key"
OUT_OF_FOCUS_FILTER_SQL = """
NOT EXISTS (
    SELECT 1
    FROM file_tags hidden_file_tags
    JOIN tags hidden_tags ON hidden_tags.id = hidden_file_tags.tag_id
    WHERE hidden_file_tags.file_id = files.id
      AND hidden_tags.name_key = ?
)
"""
OUT_OF_FOCUS_FILTER_PARAMS = (db.tag_name_key(db.SYSTEM_TAG_OUT_OF_FOCUS),)
def is_image_item(item: Any) -> bool:
    target_path = Path(str(item["target_path"]))
    return media_kind(target_path) == "image"


def item_view_rotation(item: Any) -> int:
    try:
        return db.normalize_view_rotation(item["view_rotation_degrees"])
    except (KeyError, IndexError):
        return 0


def rotation_style_attr(item: Any, target: Path | None = None) -> str:
    rotation = item_view_rotation(item)
    if rotation == 0:
        return ""
    style = f"transform: rotate({rotation}deg);"
    if rotation in {90, 270}:
        ratio = item_media_width_height_ratio(item, target)
        if ratio is not None:
            style += f" --quarter-turn-width: {ratio * 100:.6f}%;"
    return f' style="{style}" data-view-rotation="{rotation}"'


def item_media_width_height_ratio(item: Any, target: Path | None = None) -> float | None:
    try:
        width = int(item["media_width"])
        height = int(item["media_height"])
    except (KeyError, IndexError, TypeError, ValueError):
        width = 0
        height = 0
    if (width <= 0 or height <= 0) and target is not None:
        dimensions = cached_image_dimensions(target, db.absolute_target_path(target, Path(str(item["target_path"]))))
        if dimensions is not None:
            width = dimensions.width
            height = dimensions.height
    if width <= 0 or height <= 0:
        return None
    return width / height


def media_link_class_attr(item: Any) -> str:
    rotation = item_view_rotation(item)
    css_class = "media-link quarter-turn" if rotation in {90, 270} else "media-link"
    return f' class="{css_class}"'


def first_browser_item(target: Path, *, hide_out_of_focus: bool = False) -> Any | None:
    return first_source_item(target, all_browser_source(), hide_out_of_focus=hide_out_of_focus)


def first_source_item(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
) -> Any | None:
    if source_has_sql_filter(source):
        return first_sql_filtered_source_item(target, source, face_config, hide_out_of_focus=hide_out_of_focus)
    if source.person_name is not None or source.source_id is not None or source.tag_name is not None:
        items = source_items(target, source, face_config, hide_out_of_focus=hide_out_of_focus)
        return items[0] if items else None
    if not is_filtered_source(source):
        return first_unfiltered_source_item(target, hide_out_of_focus=hide_out_of_focus)
    items = source_items(target, source, face_config, hide_out_of_focus=hide_out_of_focus)
    return items[0] if items else None


def first_sql_filtered_source_item(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
) -> Any | None:
    where_sql, params = source_sql_filter(source)
    where_sql, params = with_motion_video_filter(
        target,
        where_sql,
        params,
        include_motion=source_shows_motion_videos(source),
    )
    where_sql, params = with_out_of_focus_filter(source, where_sql, params, hide_out_of_focus)
    deleted_sql = "1 = 1" if source_includes_deleted(source) else "deleted_at IS NULL"
    conn = db.connect(target)
    try:
        attach_source_sql_filter_databases(conn, target, source, face_config)
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE {deleted_sql}
              AND ({where_sql})
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            params,
        ).fetchone()
    finally:
        conn.close()


def first_unfiltered_source_item(target: Path, *, hide_out_of_focus: bool = False) -> Any | None:
    conn = db.connect(target)
    try:
        where_sql, params = all_source_where(target, hide_out_of_focus=hide_out_of_focus, conn=conn)
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE {where_sql}
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            params,
        ).fetchone()
    finally:
        conn.close()


def sql_filtered_source_item_by_id(
    target: Path,
    source: BrowserSource,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> Any | None:
    where_sql, params = source_sql_filter(source)
    owned_conn = conn is None
    conn = conn or db.connect(target)
    where_sql, params = with_motion_video_filter(
        target,
        where_sql,
        params,
        include_motion=source_shows_motion_videos(source),
        conn=conn,
    )
    where_sql, params = with_out_of_focus_filter(source, where_sql, params, hide_out_of_focus)
    deleted_sql = "1 = 1" if source_includes_deleted(source) else "deleted_at IS NULL"
    try:
        attach_source_sql_filter_databases(conn, target, source, face_config)
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE {deleted_sql}
              AND id = ?
              AND ({where_sql})
            """,
            (file_id, *params),
        ).fetchone()
    finally:
        if owned_conn:
            conn.close()


def unfiltered_source_item_by_id(
    target: Path,
    file_id: int,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> Any | None:
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        where_sql, params = all_source_where(target, hide_out_of_focus=hide_out_of_focus, conn=conn)
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE {where_sql} AND id = ?
            """,
            (*params, file_id),
        ).fetchone()
    finally:
        if owned_conn:
            conn.close()


def item_by_id(
    target: Path,
    file_id: int,
    *,
    conn: sqlite3.Connection | None = None,
) -> Any | None:
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE id = ?
            """,
            (file_id,),
        ).fetchone()
    finally:
        if owned_conn:
            conn.close()


def browser_item_by_id(target: Path, file_id: int, *, hide_out_of_focus: bool = False) -> Any | None:
    return source_item_by_id(target, all_browser_source(), file_id, hide_out_of_focus=hide_out_of_focus)


def source_item_by_id(
    target: Path,
    source: BrowserSource,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> Any | None:
    if source_has_sql_filter(source):
        return sql_filtered_source_item_by_id(
            target,
            source,
            file_id,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
            conn=conn,
        )
    if source.person_name is not None or source.source_id is not None or source.tag_name is not None:
        return next(
            (
                item
                for item in source_items(target, source, face_config, hide_out_of_focus=hide_out_of_focus)
                if int(item["id"]) == file_id
            ),
            None,
        )
    return unfiltered_source_item_by_id(target, file_id, hide_out_of_focus=hide_out_of_focus, conn=conn)


def sql_filtered_source_item_count(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> int:
    where_sql, params = source_sql_filter(source)
    owned_conn = conn is None
    conn = conn or db.connect(target)
    where_sql, params = with_motion_video_filter(
        target,
        where_sql,
        params,
        include_motion=source_shows_motion_videos(source),
        conn=conn,
    )
    where_sql, params = with_out_of_focus_filter(source, where_sql, params, hide_out_of_focus)
    deleted_sql = "1 = 1" if source_includes_deleted(source) else "deleted_at IS NULL"
    try:
        attach_source_sql_filter_databases(conn, target, source, face_config)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS item_count
            FROM files
            WHERE {deleted_sql}
              AND ({where_sql})
            """,
            params,
        ).fetchone()
        return int(row["item_count"] if row is not None else 0)
    finally:
        if owned_conn:
            conn.close()


def source_item_count(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> int:
    if source_has_sql_filter(source):
        return sql_filtered_source_item_count(target, source, face_config, hide_out_of_focus=hide_out_of_focus, conn=conn)
    return len(source_items(target, source, face_config, hide_out_of_focus=hide_out_of_focus))


def item_order_key(item: Any) -> tuple[str, str]:
    return browser_date_for_item(item), str(item["target_path_key"])


def browser_date_for_item(item: Any) -> str:
    manual_date = manual_date_midpoint_for_item(item)
    if manual_date is not None:
        return manual_date.isoformat()
    taken_date = str(item["taken_date"] or "")
    if re.match(r"^\d{4}-\d{2}-\d{2}", taken_date):
        return taken_date[:10]
    return "9999-99-99"


def manual_date_midpoint_for_item(item: Any) -> dt.date | None:
    try:
        date_from = parse_iso_date(str(item["manual_date_from"] or ""))
        date_to = parse_iso_date(str(item["manual_date_to"] or ""))
    except (KeyError, IndexError):
        return None
    if date_from is None or date_to is None:
        return None
    return date_from + (date_to - date_from) // 2


def parse_iso_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def should_filter_out_of_focus(source: BrowserSource, hide_out_of_focus: bool) -> bool:
    if not hide_out_of_focus:
        return False
    if source.tag_name is None:
        return True
    return db.tag_name_key(source.tag_name) != db.tag_name_key(db.SYSTEM_TAG_OUT_OF_FOCUS)


def all_source_where(
    target: Path,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> tuple[str, tuple[object, ...]]:
    base_sql, params = motion_video_id_filter_sql(target, "deleted_at IS NULL", (), conn=conn)
    if not hide_out_of_focus:
        return base_sql, params
    return f"{base_sql} AND {OUT_OF_FOCUS_FILTER_SQL}", (*params, *OUT_OF_FOCUS_FILTER_PARAMS)


def with_out_of_focus_filter(
    source: BrowserSource,
    where_sql: str,
    params: tuple[object, ...],
    hide_out_of_focus: bool,
) -> tuple[str, tuple[object, ...]]:
    if not should_filter_out_of_focus(source, hide_out_of_focus):
        return where_sql, params
    return f"({where_sql}) AND {OUT_OF_FOCUS_FILTER_SQL}", (*params, *OUT_OF_FOCUS_FILTER_PARAMS)


def with_motion_video_filter(
    target: Path,
    where_sql: str,
    params: tuple[object, ...],
    *,
    include_motion: bool = False,
    conn: sqlite3.Connection | None = None,
) -> tuple[str, tuple[object, ...]]:
    if include_motion:
        return where_sql, params
    return motion_video_id_filter_sql(target, f"({where_sql})", params, conn=conn)


def motion_video_id_filter_sql(
    target: Path,
    where_sql: str,
    params: tuple[object, ...],
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[str, tuple[object, ...]]:
    hidden_ids = sorted(motion_video_file_ids(target, conn=conn))
    if not hidden_ids:
        return where_sql, params
    placeholders = ",".join("?" for _ in hidden_ids)
    return f"({where_sql}) AND id NOT IN ({placeholders})", (*params, *hidden_ids)


def source_shows_motion_videos(source: BrowserSource) -> bool:
    if source.text_filter is None:
        return False
    from .server_filter import text_filter_shows_motion_videos

    return text_filter_shows_motion_videos(source.text_filter)


def filter_out_of_focus_items(target: Path, source: BrowserSource, items: list[Any], hide_out_of_focus: bool) -> list[Any]:
    if not should_filter_out_of_focus(source, hide_out_of_focus) or not items:
        return items
    hidden_ids = out_of_focus_file_ids(target)
    return [item for item in items if int(item["id"]) not in hidden_ids]


def filter_motion_video_items(target: Path, items: list[Any], *, include_motion: bool = False) -> list[Any]:
    if include_motion or not items:
        return items
    hidden_ids = motion_video_file_ids(target)
    return [item for item in items if int(item["id"]) not in hidden_ids]


def motion_video_file_ids(target: Path, *, conn: sqlite3.Connection | None = None) -> set[int]:
    if conn is not None:
        return set(query_motion_video_file_ids(conn))
    db_path = db.db_path_for_target(target)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return set(cached_motion_video_file_ids(str(target.resolve()), mtime_ns))


@lru_cache(maxsize=8)
def cached_motion_video_file_ids(target_path: str, db_mtime_ns: int) -> tuple[int, ...]:
    target = Path(target_path)
    conn = db.connect(target)
    try:
        return query_motion_video_file_ids(conn)
    finally:
        conn.close()


def query_motion_video_file_ids(conn: sqlite3.Connection) -> tuple[int, ...]:
    image_originals = {
        str(row["original_filename"]).casefold()
        for row in conn.execute(
            """
            SELECT original_filename
            FROM files
            WHERE deleted_at IS NULL
              AND lower(original_filename) LIKE '%.mp.jpg'
            """
        )
    }
    rows = conn.execute(
        """
        SELECT id, original_filename
        FROM files
        WHERE deleted_at IS NULL
          AND lower(original_filename) LIKE '%.mp'
          AND lower(stored_filename) LIKE '%.mp4'
        """
    )
    return tuple(
        int(row["id"])
        for row in rows
        if f"{str(row['original_filename'])}.jpg".casefold() in image_originals
    )


def out_of_focus_file_ids(target: Path) -> set[int]:
    conn = db.connect(target)
    try:
        db.create_tags_schema(conn)
        rows = conn.execute(
            """
            SELECT file_tags.file_id
            FROM file_tags
            JOIN tags ON tags.id = file_tags.tag_id
            WHERE tags.name_key = ?
            """,
            OUT_OF_FOCUS_FILTER_PARAMS,
        )
        return {int(row["file_id"]) for row in rows}
    finally:
        conn.close()


def adjacent_items_from_list(items: list[Any], item: Any) -> tuple[Any | None, Any | None]:
    index = next((idx for idx, candidate in enumerate(items) if int(candidate["id"]) == int(item["id"])), -1)
    if index < 0:
        return None, None
    previous_item = items[index - 1] if index > 0 else None
    next_item = items[index + 1] if index < len(items) - 1 else None
    return previous_item, next_item


def adjacent_items_from_id_order(
    item_ids: list[int],
    file_id: int,
    positions: dict[int, int] | None = None,
) -> tuple[Any | None, Any | None]:
    if positions is not None:
        index = positions.get(file_id, -1)
    else:
        try:
            index = item_ids.index(file_id)
        except ValueError:
            index = -1
    if index < 0:
        return None, None
    previous_item = {"id": item_ids[index - 1]} if index > 0 else None
    next_item = {"id": item_ids[index + 1]} if index < len(item_ids) - 1 else None
    return previous_item, next_item


def adjacent_unfiltered_source_items(
    target: Path,
    item: Any,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> tuple[Any | None, Any | None]:
    order_key = item_order_key(item)
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        where_sql, params = all_source_where(target, hide_out_of_focus=hide_out_of_focus, conn=conn)
        previous_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE {where_sql}
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
            WHERE {where_sql}
              AND ({ITEM_DATE_ORDER_SQL}, target_path_key) > (?, ?)
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            (*params, *order_key),
        ).fetchone()
        return previous_item, next_item
    finally:
        if owned_conn:
            conn.close()


def adjacent_sql_filtered_source_items(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> tuple[Any | None, Any | None]:
    where_sql, params = source_sql_filter(source)
    owned_conn = conn is None
    conn = conn or db.connect(target)
    where_sql, params = with_motion_video_filter(
        target,
        where_sql,
        params,
        include_motion=source_shows_motion_videos(source),
        conn=conn,
    )
    where_sql, params = with_out_of_focus_filter(source, where_sql, params, hide_out_of_focus)
    order_key = item_order_key(item)
    deleted_sql = "1 = 1" if source_includes_deleted(source) else "deleted_at IS NULL"
    try:
        attach_source_sql_filter_databases(conn, target, source, face_config)
        previous_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE {deleted_sql}
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
            WHERE {deleted_sql}
              AND ({where_sql})
              AND ({ITEM_DATE_ORDER_SQL}, target_path_key) > (?, ?)
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            (*params, *order_key),
        ).fetchone()
        return previous_item, next_item
    finally:
        if owned_conn:
            conn.close()


def adjacent_browser_items(
    target: Path,
    item: Any,
    *,
    hide_out_of_focus: bool = False,
) -> tuple[Any | None, Any | None]:
    return adjacent_source_items(target, all_browser_source(), item, hide_out_of_focus=hide_out_of_focus)


def browser_item_ids(
    target: Path,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[int]:
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        where_sql, params = all_source_where(target, hide_out_of_focus=hide_out_of_focus, conn=conn)
        return [
            int(row["id"])
            for row in conn.execute(
                f"""
                SELECT id
                FROM files
                WHERE {where_sql}
                ORDER BY {ITEM_ORDER_SQL}
                """,
                params,
            )
        ]
    finally:
        if owned_conn:
            conn.close()


def source_item_ids(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[int]:
    if source_has_sql_filter(source):
        return sql_filtered_source_item_ids(
            target,
            source,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
            conn=conn,
        )
    if source == all_browser_source():
        return browser_item_ids(target, hide_out_of_focus=hide_out_of_focus, conn=conn)
    return [int(item["id"]) for item in source_items(target, source, face_config, hide_out_of_focus=hide_out_of_focus)]


def sql_filtered_source_item_ids(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[int]:
    where_sql, params = source_sql_filter(source)
    owned_conn = conn is None
    conn = conn or db.connect(target)
    where_sql, params = with_motion_video_filter(
        target,
        where_sql,
        params,
        include_motion=source_shows_motion_videos(source),
        conn=conn,
    )
    where_sql, params = with_out_of_focus_filter(source, where_sql, params, hide_out_of_focus)
    deleted_sql = "1 = 1" if source_includes_deleted(source) else "deleted_at IS NULL"
    try:
        attach_source_sql_filter_databases(conn, target, source, face_config)
        return [
            int(row["id"])
            for row in conn.execute(
                f"""
                SELECT id
                FROM files
                WHERE {deleted_sql}
                  AND ({where_sql})
                ORDER BY {ITEM_ORDER_SQL}
                """,
                params,
            )
        ]
    finally:
        if owned_conn:
            conn.close()


def adjacent_source_items(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> tuple[Any | None, Any | None]:
    if source_has_sql_filter(source):
        return adjacent_sql_filtered_source_items(
            target,
            source,
            item,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
            conn=conn,
        )
    if (
        source.person_name is not None
        or source.source_id is not None
        or source.tag_name is not None
        or source.text_filter is not None
    ):
        return adjacent_items_from_list(
            source_items(target, source, face_config, hide_out_of_focus=hide_out_of_focus),
            item,
        )
    return adjacent_unfiltered_source_items(target, item, hide_out_of_focus=hide_out_of_focus, conn=conn)


def valid_month_key(value: str) -> bool:
    if len(value) != 7 or value[4] != "-":
        return False
    year, month = value.split("-", 1)
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= 12


@lru_cache(maxsize=8)
def cached_browser_month_keys(target_path: str, db_mtime_ns: int, hide_out_of_focus: bool) -> tuple[str, ...]:
    target = Path(target_path)
    where_sql, params = all_source_where(target, hide_out_of_focus=hide_out_of_focus)
    conn = db.connect(target)
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT substr({db.BROWSER_DATE_ORDER_SQL}, 1, 7) AS month_key
            FROM files
            WHERE {where_sql}
              AND {db.BROWSER_DATE_ORDER_SQL} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            ORDER BY month_key
            """,
            params,
        )
        return tuple(str(row["month_key"]) for row in rows if valid_month_key(str(row["month_key"])))
    finally:
        conn.close()


def sql_filtered_source_month_keys(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    where_sql, params = source_sql_filter(source)
    owned_conn = conn is None
    conn = conn or db.connect(target)
    where_sql, params = with_motion_video_filter(
        target,
        where_sql,
        params,
        include_motion=source_shows_motion_videos(source),
        conn=conn,
    )
    where_sql, params = with_out_of_focus_filter(source, where_sql, params, hide_out_of_focus)
    deleted_sql = "1 = 1" if source_includes_deleted(source) else "deleted_at IS NULL"
    try:
        attach_source_sql_filter_databases(conn, target, source, face_config)
        rows = conn.execute(
            f"""
            SELECT DISTINCT substr({db.BROWSER_DATE_ORDER_SQL}, 1, 7) AS month_key
            FROM files
            WHERE {deleted_sql}
              AND ({where_sql})
              AND {db.BROWSER_DATE_ORDER_SQL} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            ORDER BY month_key
            """,
            params,
        )
        return [str(row["month_key"]) for row in rows if valid_month_key(str(row["month_key"]))]
    finally:
        if owned_conn:
            conn.close()


def browser_month_keys(target: Path, *, hide_out_of_focus: bool = False) -> list[str]:
    return source_month_keys(target, all_browser_source(), hide_out_of_focus=hide_out_of_focus)


def valid_year_key(value: str) -> bool:
    return len(value) == 4 and value.isdigit()


def browser_year_cards(target: Path, *, hide_out_of_focus: bool = False) -> list[dict[str, Any]]:
    month_keys = browser_month_keys(target, hide_out_of_focus=hide_out_of_focus)
    years = sorted({key[:4] for key in month_keys})
    cards: list[dict[str, Any]] = []
    for year in years:
        year_months = [key for key in month_keys if key.startswith(year)]
        if not year_months:
            continue
        first_items = browser_month_items(target, year_months[0], hide_out_of_focus=hide_out_of_focus)
        if not first_items:
            continue
        cards.append({"year": year, "month_count": len(year_months), "item": representative_image_item(first_items)})
    return cards


def browser_year_month_cards(target: Path, year: str, *, hide_out_of_focus: bool = False) -> list[dict[str, Any]]:
    if not valid_year_key(year):
        return []
    month_keys = [
        key
        for key in browser_month_keys(target, hide_out_of_focus=hide_out_of_focus)
        if key.startswith(year)
    ]
    cards: list[dict[str, Any]] = []
    for month_key in month_keys:
        items = browser_month_items(target, month_key, hide_out_of_focus=hide_out_of_focus)
        if not items:
            continue
        cards.append({"month_key": month_key, "item_count": len(items), "item": representative_image_item(items)})
    return cards


def source_year_month_cards(
    target: Path,
    source: BrowserSource,
    year: str,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
) -> list[dict[str, Any]]:
    if not valid_year_key(year):
        return []
    month_keys = [
        key
        for key in source_month_keys(target, source, face_config, hide_out_of_focus=hide_out_of_focus)
        if key.startswith(year)
    ]
    cards: list[dict[str, Any]] = []
    for month_key in month_keys:
        items = source_month_items(
            target,
            source,
            month_key,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
        )
        if not items:
            continue
        cards.append({"month_key": month_key, "item_count": len(items), "item": representative_image_item(items)})
    return cards


def representative_image_item(items: list[Any]) -> Any:
    return next((item for item in items if is_image_item(item)), items[0])


def source_month_keys(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    if source_has_sql_filter(source):
        return sql_filtered_source_month_keys(target, source, face_config, hide_out_of_focus=hide_out_of_focus, conn=conn)
    if (
        source.person_name is not None
        or source.source_id is not None
        or source.tag_name is not None
        or source.text_filter is not None
    ):
        keys = {
            month_key_for_item(target, item)
            for item in source_items(target, source, face_config, hide_out_of_focus=hide_out_of_focus)
        }
        return sorted(key for key in keys if valid_month_key(key))
    db_path = db.db_path_for_target(target)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return list(cached_browser_month_keys(str(target.resolve()), mtime_ns, hide_out_of_focus))


def imported_source_items(target: Path, source_id: int, *, hide_out_of_focus: bool = False) -> list[Any]:
    hidden_ids = sorted(motion_video_file_ids(target))
    if hidden_ids:
        placeholders = ",".join("?" for _ in hidden_ids)
        filter_sql = f"AND files.id NOT IN ({placeholders})"
        filter_params: tuple[object, ...] = tuple(hidden_ids)
    else:
        filter_sql = ""
        filter_params = ()
    if hide_out_of_focus:
        filter_sql += f" AND {OUT_OF_FOCUS_FILTER_SQL}"
        filter_params = (*filter_params, *OUT_OF_FOCUS_FILTER_PARAMS)
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT
                    files.id,
                    files.target_path,
                    files.target_path_key,
                    files.original_filename,
                    files.stored_filename,
                    files.taken_date,
                    files.date_source,
                    files.camera_make,
                    files.camera_model,
                    files.size_bytes,
                    files.view_rotation_degrees,
                    files.gps_lat,
                    files.gps_lon,
                    files.gps_source,
                    files.media_width,
                    files.media_height,
                    files.media_orientation,
                    files.media_metadata_mtime_ns,
                    {db.H3_FILE_COLUMNS_SQL}
                FROM files
                JOIN file_sources ON file_sources.file_id = files.id
                WHERE files.deleted_at IS NULL
                  AND file_sources.source_id = ?
                  {filter_sql}
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (source_id, *filter_params),
            )
        )
    finally:
        conn.close()


def tagged_items(target: Path, tag_name: str) -> list[Any]:
    conn = db.connect(target)
    try:
        return db.tagged_files(conn, tag_name)
    finally:
        conn.close()


def imported_source_by_id(target: Path, source_id: int) -> db.Source | None:
    conn = db.connect(target)
    try:
        try:
            return db.get_source(conn, source_id)
        except ValueError:
            return None
    finally:
        conn.close()


def source_summary_rows(target: Path) -> list[sqlite3.Row]:
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                """
                SELECT
                    sources.id,
                    sources.name,
                    sources.path,
                    sources.imported_at,
                    sources.status,
                    sources.superseded_by_source_id,
                    COUNT(file_sources.id) AS source_file_count,
                    COUNT(CASE WHEN files.deleted_at IS NULL THEN 1 END) AS active_file_count
                FROM sources
                LEFT JOIN file_sources ON file_sources.source_id = sources.id
                LEFT JOIN files ON files.id = file_sources.file_id
                GROUP BY sources.id
                ORDER BY sources.imported_at IS NULL, sources.imported_at, sources.id
                """
            )
        )
    finally:
        conn.close()


def sources_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    sources = source_summary_rows(target)
    rows = "\n".join(source_row_html(source) for source in sources)
    content = (
        f'<div class="people-table">{rows}</div>'
        if rows
        else '<p class="meta">Ingen importerte kilder registrert.</p>'
    )
    return shell_page_html(
        "Importerte mapper",
        f"""
        <h1>Importerte mapper</h1>
        <p>Denne siden viser alle importerte mapper, dvs samme info som du får fra å kjøre
        <code>bildebank list-sources</code></p>
        {content}
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def tags_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    conn = db.connect(target)
    try:
        rows = list(db.tags(conn))
    finally:
        conn.close()
    items = "\n".join(tag_row_html(row) for row in rows)
    content = f'<div class="people-table">{items}</div>' if items else '<p class="meta">Ingen tagger registrert.</p>'
    return shell_page_html(
        "Tagger",
        f"""
        <h1>Tagger</h1>
        <form action="/tags/create" method="post" class="new-person-form">
          <label>Ny tagg <input name="name" autocomplete="off"></label>
          <button type="submit">Legg til</button>
        </form>
        {content}
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def tag_row_html(row: sqlite3.Row) -> str:
    tag_id = int(row["id"])
    name = str(row["name"])
    kind = str(row["kind"])
    kind_label = "systemtagg" if kind == db.TAG_KIND_SYSTEM else "brukertagg"
    url = "/tag/" + urllib.parse.quote(name, safe="")
    actions = tag_row_actions_html(tag_id, name, kind)
    return f"""
    <div class="people-row">
      <div class="people-name">{html.escape(name)}</div>
      <a class="person-link" href="{html.escape(url)}">Vis bilder ({int(row["file_count"])})</a>
      <span class="status">{html.escape(kind_label)}</span>
      <span class="status">opprettet: {html.escape(str(row["created_at"]))}</span>
      {actions}
    </div>
    """


def tag_row_actions_html(tag_id: int, name: str, kind: str) -> str:
    if kind == db.TAG_KIND_SYSTEM:
        return '<span class="status">systemtagg kan ikke endres</span>'
    escaped_name = html.escape(name)
    return f"""
      <div class="tag-actions">
        <form action="/tags/rename" method="post" class="inline-edit-form">
          <input type="hidden" name="tag_id" value="{tag_id}">
          <input name="name" value="{escaped_name}" autocomplete="off" aria-label="Nytt taggnavn">
          <button type="submit">Endre navn</button>
        </form>
        <form action="/tags/delete" method="post">
          <input type="hidden" name="tag_id" value="{tag_id}">
          <button type="submit" class="danger-button" data-confirm-submit="Slette taggen {escaped_name} fra alle bilder?">Slett</button>
        </form>
      </div>
    """


def source_row_html(source: sqlite3.Row) -> str:
    name = str(source["name"])
    status = str(source["status"])
    active_file_count = int(source["active_file_count"])
    source_file_count = int(source["source_file_count"])
    imported_at = str(source["imported_at"] or "-")
    superseded_by = source["superseded_by_source_id"]
    superseded = f", erstattet av #{int(superseded_by)}" if superseded_by is not None else ""
    source_browser = imported_source_browser_source(source)
    return f"""
    <div class="people-row">
      <div class="people-name">{html.escape(name)}</div>
      <a class="person-link" href="{html.escape(source_browser.root_url)}">Vis bilder ({active_file_count})</a>
      <span class="status">filer fra kilde: {source_file_count}</span>
      <span class="status">status: {html.escape(status)}{html.escape(superseded)}</span>
      <span class="status">importert: {html.escape(imported_at)}</span>
      <div class="detail">{html.escape(str(source["path"]))}</div>
    </div>
    """


def empty_browser_html(
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    search_link = '<p><a href="/search">Bildesøk</a></p>' if openclip_enabled else ""
    return shell_page_html(
        "Bildebrowser",
        f"""
        <h1>Bildebrowser</h1>
        <p class="meta">Ingen filer i bildesamlingen.</p>
        {search_link}
        """,
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
    page_html: PageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
    manual_person_controls_enabled: bool = True,
    hotkey_hints_enabled: bool = False,
    hotkeys: Mapping[str, BrowserHotkeyConfig] | None = None,
) -> str:
    return source_item_page_html(
        target,
        all_browser_source(),
        item,
        previous_item,
        next_item,
        month_nav,
        page_html=page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        face_config=face_config,
        manual_person_controls_enabled=manual_person_controls_enabled,
        hotkey_hints_enabled=hotkey_hints_enabled,
        hotkeys=hotkeys,
    )


def _source_item_face_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None,
    *,
    face_enabled: bool,
    manual_person_controls_enabled: bool,
    timing_callback: Callable[[str, float], None] | None = None,
) -> tuple[str, str, str, bool]:
    if not face_enabled:
        return "", "", "", False

    from .server_faces import (
        confirmed_face_people_text_html,
        faces_button_html,
        faces_overlay_html,
        manual_person_file_controls_html,
        people_for_file,
        people_links_html,
        source_duplicate_confirmed_faces_warning_html,
        unconfirmed_face_count_for_item,
    )

    start = time.perf_counter()
    people_data, confirmed_face_people_data = people_for_file(target, int(item["id"]), face_config)
    person_has_confirmed_face = False
    if source.person_name is not None:
        person_has_confirmed_face = any(
            str(person.get("name")) == source.person_name
            for person in confirmed_face_people_data
        )
    if timing_callback is not None:
        timing_callback("html_people_for_file", start)

    manual_person_controls = ""
    if manual_person_controls_enabled:
        start = time.perf_counter()
        manual_person_controls = manual_person_file_controls_html(target, item, people_data, face_config)
        if timing_callback is not None:
            timing_callback("html_manual_person_controls", start)

    start = time.perf_counter()
    face_rail_html = people_links_html(
        people_data,
        "Personer i bildet",
        manual_person_controls=manual_person_controls,
        file_id=int(item["id"]),
        manual_remove_enabled=manual_person_controls_enabled,
    )
    show_unconfirmed_faces = source.person_name is None
    unconfirmed_face_count = unconfirmed_face_count_for_item(target, int(item["id"]), face_config) if show_unconfirmed_faces else 0
    face_rail_html += faces_button_html(unconfirmed_face_count, int(item["id"])) if show_unconfirmed_faces else ""
    face_rail_html += confirmed_face_people_text_html(confirmed_face_people_data)
    faces_overlay = faces_overlay_html(item) if unconfirmed_face_count > 0 else ""
    duplicate_warning = source_duplicate_confirmed_faces_warning_html(target, source, item, face_config)
    if timing_callback is not None:
        timing_callback("html_face_rail", start)
    return face_rail_html, faces_overlay, duplicate_warning, person_has_confirmed_face


def _source_item_controls_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
    face_config: FaceRecognitionConfig | None,
    *,
    face_enabled: bool,
    person_has_confirmed_face: bool,
    hide_out_of_focus: bool,
    conn: sqlite3.Connection | None,
    timing_callback: Callable[[str, float], None] | None = None,
) -> str:
    from .server_shell import (
        face_suggest_button_html,
        face_toggle_button_html,
        source_controls_html,
        suggestion_toggle_button_html,
    )

    start = time.perf_counter()
    controls = source_controls_html(
        source,
        month_nav,
        previous_item,
        next_item,
        rotation_buttons=rotation_buttons_html(source, item),
        manual_date_button=manual_date_button_html(item),
        face_toggle_button=face_toggle_button_html(source, item, face_enabled=face_enabled),
        face_suggest_button=face_suggest_button_html(face_enabled=face_enabled),
        suggestion_toggle_button=suggestion_toggle_button_html(
            source,
            item,
            face_enabled=face_enabled,
            href=suggestion_toggle_href(
                target,
                source,
                item,
                face_config,
                person_has_confirmed_face=person_has_confirmed_face,
                hide_out_of_focus=hide_out_of_focus,
                conn=conn,
            ),
        ),
        unconfirm_buttons="",
        delete_button=delete_button_html(source, item, previous_item, next_item),
    )
    if timing_callback is not None:
        timing_callback("html_controls", start)
    return controls


def _source_item_tag_controls_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    *,
    hide_out_of_focus: bool,
    face_rail_html: str,
    suffix_html: str,
    conn: sqlite3.Connection | None,
    timing_start: float,
    timing_callback: Callable[[str, float], None] | None = None,
) -> str:
    out_of_focus_redirect_url = hidden_after_out_of_focus_tag_redirect_url(
        source,
        previous_item,
        next_item,
        hide_out_of_focus=hide_out_of_focus,
    )
    tag_controls = tag_controls_html(
        target,
        item,
        out_of_focus_redirect_url=out_of_focus_redirect_url,
        extra_html=face_rail_html,
        suffix_html=suffix_html,
        conn=conn,
    )
    if timing_callback is not None:
        timing_callback("html_tag_controls", timing_start)
    return tag_controls


def _source_item_header_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    controls: str,
    duplicate_warning: str,
    face_config: FaceRecognitionConfig | None,
    *,
    face_enabled: bool,
    openclip_enabled: bool,
    all_items_url: str | None,
    all_items_label: str,
    hide_out_of_focus: bool,
    conn: sqlite3.Connection | None,
    source_item_count_value: int | None,
    timing_callback: Callable[[str, float], None] | None = None,
) -> str:
    from .server_shell import app_header_html

    start = time.perf_counter()
    title_html = source_item_breadcrumb_html(
        target,
        source,
        item,
        face_config=face_config,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
        source_item_count_value=source_item_count_value,
    )
    if timing_callback is not None:
        timing_callback("html_breadcrumb", start)

    start = time.perf_counter()
    header_html = app_header_html(
        source.title,
        source=source,
        item=item,
        title_html=title_html,
        controls=controls,
        message_html=duplicate_warning,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        all_items_url=all_items_url,
        all_items_label=all_items_label,
    )
    if timing_callback is not None:
        timing_callback("html_app_header", start)
    return header_html


def source_item_page_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
    *,
    page_html: PageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
    manual_person_controls_enabled: bool = True,
    hotkey_hints_enabled: bool = False,
    hotkeys: Mapping[str, BrowserHotkeyConfig] | None = None,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
    source_item_count_value: int | None = None,
    timing_callback: Callable[[str, float], None] | None = None,
) -> str:
    target_path = Path(str(item["target_path"]))
    relative = display_relative_path(target, target_path)
    start = time.perf_counter()
    media = source_item_media_html(target, source, item, face_config)
    if timing_callback is not None:
        timing_callback("html_media", start)
    face_rail_html, faces_overlay, duplicate_warning, person_has_confirmed_face = _source_item_face_html(
        target,
        source,
        item,
        face_config,
        face_enabled=face_enabled,
        manual_person_controls_enabled=manual_person_controls_enabled,
        timing_callback=timing_callback,
    )
    start = time.perf_counter()
    hotkey_hints_html = (
        hotkey_hints_panel_html(target, hotkeys or {}, conn=conn)
        if hotkey_hints_enabled
        else ""
    )
    if timing_callback is not None:
        timing_callback("html_hotkey_hints", start)
    controls = _source_item_controls_html(
        target,
        source,
        item,
        previous_item,
        next_item,
        month_nav,
        face_config,
        face_enabled=face_enabled,
        person_has_confirmed_face=person_has_confirmed_face,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
        timing_callback=timing_callback,
    )
    start = time.perf_counter()
    info_overlay = image_info_overlay_html()
    manual_date_overlay = manual_date_overlay_html()
    face_suggest_dialog = ""
    if face_enabled:
        from .server_faces import face_suggest_dialog_html

        threshold = face_config.suggest_threshold if face_config is not None else 0.6
        face_suggest_dialog = face_suggest_dialog_html(
            threshold,
            return_url=source_item_url(source, int(item["id"])),
        )
    tag_controls = _source_item_tag_controls_html(
        target,
        source,
        item,
        previous_item,
        next_item,
        hide_out_of_focus=hide_out_of_focus,
        face_rail_html=face_rail_html,
        suffix_html=hotkey_hints_html,
        conn=conn,
        timing_start=start,
        timing_callback=timing_callback,
    )
    start = time.perf_counter()
    all_items_url = all_browser_item_link_url(target, source, item, hide_out_of_focus=hide_out_of_focus, conn=conn)
    if timing_callback is not None:
        timing_callback("html_all_items_link", start)
    all_items_label = "Synlige bilder" if hide_out_of_focus else "Alle bilder"
    start = time.perf_counter()
    motion_video = motion_video_for_image(target, item, conn=conn)
    motion_video_link = motion_video_link_html(motion_video) if motion_video is not None else ""
    if timing_callback is not None:
        timing_callback("html_motion_video", start)
    source_url_attr = ""
    if source.text_filter is not None:
        source_url_attr = f' data-browser-source-url="{html.escape(source.root_url)}"'
    hotkeys_enabled_attr = ' data-browser-hotkeys-enabled="true"' if hotkey_hints_enabled else ""
    header_html = _source_item_header_html(
        target,
        source,
        item,
        controls,
        duplicate_warning,
        face_config=face_config,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        all_items_url=all_items_url,
        all_items_label=all_items_label,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
        source_item_count_value=source_item_count_value,
        timing_callback=timing_callback,
    )
    start = time.perf_counter()
    result = page_html(
        f"{source.title}: {target_path.name}",
        f"""
        <main class="server-browser" data-browser-item-id="{int(item["id"])}"{source_url_attr}{hotkeys_enabled_attr}>
          {header_html}
          <div class="stage-shell">
            {tag_controls}
            <section class="stage">
              {media}
            </section>
          </div>
          <footer class="browser-footer">
            <a class="filename" href="/file/{int(item["id"])}" target="_blank">{html.escape(relative)}</a>
            {motion_video_link}
          </footer>
        </main>
        {faces_overlay}
        {info_overlay}
        {manual_date_overlay}
        {face_suggest_dialog}
        """,
    )
    if timing_callback is not None:
        timing_callback("html_page", start)
    return result


def suggestion_toggle_href(
    target: Path,
    source: BrowserSource,
    item: Any | None,
    face_config: FaceRecognitionConfig | None = None,
    *,
    person_has_confirmed_face: bool | None = None,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    if source.person_name is None:
        return None
    target_source = person_browser_source(
        source.person_name,
        include_suggestions=not source.include_suggestions,
        show_faces=source.show_faces,
    )
    if item is None:
        return target_source.root_url
    file_id = int(item["id"])
    if source.person_name is not None and source.include_suggestions and not target_source.include_suggestions:
        return source_item_url(target_source, file_id) if person_has_confirmed_face else target_source.root_url
    target_item = source_item_by_id(
        target,
        target_source,
        file_id,
        face_config,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
    )
    if target_item is None:
        return target_source.root_url
    return source_item_url(target_source, file_id)


def motion_video_for_image(target: Path, item: Any, *, conn: sqlite3.Connection | None = None) -> Any | None:
    if not is_image_item(item):
        return None
    original_filename = original_filename_for_item(target, item, conn=conn)
    if original_filename is None:
        return None
    if not original_filename.casefold().endswith(".mp.jpg"):
        return None
    motion_original_filename = original_filename[:-4]
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND lower(original_filename) = lower(?)
              AND lower(original_filename) LIKE '%.mp'
              AND lower(stored_filename) LIKE '%.mp4'
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            (motion_original_filename,),
        ).fetchone()
    finally:
        if owned_conn:
            conn.close()


def original_filename_for_item(target: Path, item: Any, *, conn: sqlite3.Connection | None = None) -> str | None:
    try:
        return str(item["original_filename"])
    except (KeyError, IndexError):
        pass
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        row = conn.execute("SELECT original_filename FROM files WHERE id = ?", (int(item["id"]),)).fetchone()
        return str(row["original_filename"]) if row is not None else None
    finally:
        if owned_conn:
            conn.close()


def motion_video_link_html(motion_video: Any) -> str:
    filename = str(motion_video["stored_filename"])
    url = "/filter/" + urllib.parse.quote(f"filename:{filename}", safe="") + f"/item/{int(motion_video['id'])}"
    return f'<a class="filename" href="{html.escape(url)}">Motion-video: {html.escape(filename)}</a>'


def source_item_breadcrumb_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
    source_item_count_value: int | None = None,
) -> str:
    month_key = month_key_for_item(target, item)
    filename = html.escape(str(item["stored_filename"]))
    file_id = int(item["id"])
    filename_link = (
        f'<a href="#" data-open-info data-info-item="{file_id}" '
        f'title="Vis detaljer om bildet" '
        f'aria-label="Åpne bildeinfo for {filename}">{filename}</a>'
    )
    source_label, source_title = source_breadcrumb_label(
        target,
        source,
        face_config,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
        source_item_count_value=source_item_count_value,
    )
    if not valid_month_key(month_key):
        return breadcrumb_html([(source_label, source.root_url, source_title)], filename_link)
    year, month = month_key.split("-", 1)
    month_name = MONTH_NAMES.get(month, month_key)
    crumbs: list[Breadcrumb]
    if source == all_browser_source():
        crumbs = [
            ("År", "/years"),
            (year, source_year_url(source, year)),
            (month_name, source_month_url(source, month_key)),
        ]
    else:
        crumbs = [
            (source_label, source.root_url, source_title),
            (year, source_year_url(source, year)),
            (month_name, source_month_url(source, month_key)),
        ]
    return breadcrumb_html(crumbs, filename_link)


def source_breadcrumb_label(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
    source_item_count_value: int | None = None,
) -> tuple[str, str | None]:
    label = source.person_name if source.person_name is not None else source.title
    if source.text_filter is None:
        return label, None
    count = (
        source_item_count_value
        if source_item_count_value is not None
        else source_item_count(target, source, face_config, hide_out_of_focus=hide_out_of_focus, conn=conn)
    )
    match_text = "1 treff" if count == 1 else f"{count} treff"
    return f"{label} ({match_text})", f"{match_text} i filtersøket"


def breadcrumb_html(
    crumbs: Sequence[Breadcrumb],
    final_html: str,
) -> str:
    parts = []
    for crumb in crumbs:
        label, url = crumb[0], crumb[1]
        title = crumb[2] if len(crumb) > 2 else None
        title_attr = f' title="{html.escape(title)}"' if title else ""
        parts.append(
            f'<a href="{html.escape(url)}"{title_attr}>{html.escape(label)}</a>'
            if url is not None
            else html.escape(label)
        )
    parts.append(final_html)
    return '<nav class="breadcrumb" aria-label="Plassering">' + '<span class="sep">/</span>'.join(parts) + "</nav>"


def all_browser_item_link_url(
    target: Path,
    source: BrowserSource,
    item: Any,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    if source == all_browser_source() or not hide_out_of_focus:
        return None
    if should_filter_out_of_focus(source, hide_out_of_focus) and not source_includes_deleted(source):
        return source_item_url(all_browser_source(), int(item["id"]))
    visible_item = source_item_by_id(
        target,
        all_browser_source(),
        int(item["id"]),
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
    )
    if visible_item is None:
        return "/"
    return source_item_url(all_browser_source(), int(item["id"]))


def hidden_after_out_of_focus_tag_redirect_url(
    source: BrowserSource,
    previous_item: Any | None,
    next_item: Any | None,
    *,
    hide_out_of_focus: bool = False,
) -> str:
    if not should_filter_out_of_focus(source, hide_out_of_focus):
        return ""
    if next_item is not None:
        return source_item_url(source, int(next_item["id"]))
    if previous_item is not None:
        return source_item_url(source, int(previous_item["id"]))
    return source.root_url


def tag_controls_html(
    target: Path,
    item: Any,
    *,
    out_of_focus_redirect_url: str = "",
    extra_html: str = "",
    suffix_html: str = "",
    conn: sqlite3.Connection | None = None,
) -> str:
    file_id = int(item["id"])
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        defined_tags = tag_control_rows(conn)
        active_names = active_tag_name_keys_for_file(conn, file_id)
        manual_h3_name = manual_h3_place_name(conn, item)
        manual_h3_cell_value = manual_h3_cell(item)
        location_controls = remove_manual_location_button_html(item)
    finally:
        if owned_conn:
            conn.close()
    buttons = []
    for tag in defined_tags:
        tag_name = str(tag["name"])
        tag_name_key = str(tag["name_key"])
        active = tag_name_key in active_names
        pressed = "true" if active else "false"
        active_class = " active" if active else ""
        redirect_attr = ""
        if tag_name == db.SYSTEM_TAG_OUT_OF_FOCUS and out_of_focus_redirect_url:
            redirect_attr = f' data-tag-hide-redirect="{html.escape(out_of_focus_redirect_url)}"'
        buttons.append(
            f'<button class="tag-toggle{active_class}" type="button" '
            f'title="Klikk for å legge til eller fjerne taggen fra bildet" '
            f'data-tag-toggle="{file_id}" data-tag-name="{html.escape(tag_name)}" '
            f'aria-pressed="{pressed}"{redirect_attr}>{html.escape(tag_name)}</button>'
        )
    location_status = (
        manual_h3_badge_html(manual_h3_name, manual_h3_cell_value, extra_html=location_controls)
        if gps_source_is_manual_h3(item)
        else gps_location_badge_html(item, extra_html=location_controls)
    )
    date_status = date_status_badge_html(item)
    return f'<aside class="tag-rail" aria-label="Tagger">{extra_html}{"".join(buttons)}{date_status}{location_status}{suffix_html}</aside>'


def hotkey_hints_panel_html(
    target: Path,
    hotkeys: Mapping[str, BrowserHotkeyConfig],
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    rows = []
    for key in HOTKEY_KEYS:
        label = hotkey_hint_label(target, hotkeys.get(key, BrowserHotkeyConfig()), conn=conn)
        if label:
            rows.append(f'<div class="hotkey-hint"><span>{html.escape(key)}:</span> {html.escape(label)}</div>')
    if not rows:
        return ""
    heading = '<div class="hotkey-hints-heading">Hurtigtaster aktivert:</div>'
    return '<section class="hotkey-hints" aria-label="Hurtigtaster">' + heading + "".join(rows) + "</section>"


def hotkey_hint_label(
    target: Path,
    hotkey: BrowserHotkeyConfig,
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    if hotkey.action == "h3" and hotkey.h3_cell:
        name = manual_h3_cell_name(target, hotkey.h3_cell, conn=conn) or hotkey.h3_cell
        return f"Sett H3 til {name}"
    if hotkey.action == "person" and hotkey.person_name:
        return f"Legg til {hotkey.person_name}"
    if hotkey.action == "tag" and hotkey.tag_name:
        return f"Sett tagg {db.normalize_tag_name(hotkey.tag_name)}"
    if hotkey.action == "manual_date":
        text = hotkey_date_hint_text(hotkey)
        return f"Sett dato til {text}" if text else ""
    return ""


def hotkey_date_hint_text(hotkey: BrowserHotkeyConfig) -> str:
    if hotkey.mode == "exact":
        return display_short_date(hotkey.date)
    if hotkey.mode == "uncertain":
        date_text = display_short_date(hotkey.date)
        return f"{date_text} ±{hotkey.uncertainty}" if date_text and hotkey.uncertainty else date_text
    if hotkey.mode == "between":
        start = display_short_date(hotkey.date_from)
        end = display_short_date(hotkey.date_to)
        if start and end:
            return f"{start}-{end}"
    return ""


def display_short_date(value: str) -> str:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%d.%m.%y")


def tag_control_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT id, name, name_key, kind, created_at
            FROM tags
            ORDER BY CASE kind WHEN 'system' THEN 0 ELSE 1 END, name_key
            """
        )
    )


def active_tag_name_keys_for_file(conn: sqlite3.Connection, file_id: int) -> set[str]:
    rows = conn.execute(
        """
        SELECT tags.name_key
        FROM tags
        JOIN file_tags ON file_tags.tag_id = tags.id
        WHERE file_tags.file_id = ?
        """,
        (file_id,),
    )
    return {str(row["name_key"]) for row in rows}


def date_status_badge_html(item: Any) -> str:
    date_text = image_date_text(item)
    manual_text = manual_date_text(item)
    original = ""
    if manual_text:
        original_date = str(item["taken_date"] or "-")
        original_source = date_source_text(str(item["date_source"] or ""))
        original = f'<span class="date-status-original">Opprinnelig: {html.escape(original_date)} ({html.escape(original_source)})</span>'
    return (
        '<div class="date-status-badge">'
        f'<span class="date-status-main">{html.escape(date_text)}</span>'
        f' {original}'
        '</div>'
    )


def source_item_media_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    if source.person_name is not None:
        if not source.show_faces:
            return item_media_html(target, item)
        from .server_faces import person_faces_for_item, person_item_media_html

        faces = person_faces_for_item(
            target,
            source.person_name,
            item,
            include_suggestions=source.include_suggestions,
            face_config=face_config,
        )
        return person_item_media_html(item, faces)
    return item_media_html(target, item)


def item_media_html(target: Path, item: Any) -> str:
    file_id = int(item["id"])
    target_path = Path(str(item["target_path"]))
    url = f"/file/{file_id}"
    name = html.escape(str(item["stored_filename"]))
    kind = media_kind(target_path)
    if kind == "video":
        return f'<video src="{url}" controls></video>'
    if kind != "image":
        return f'<a class="file-card" href="{url}" target="_blank">Fil<br>{name}</a>'
    return f'<a href="{url}" target="_blank"{media_link_class_attr(item)}><img src="{url}" alt="{name}"{rotation_style_attr(item, target)}></a>'


def person_item_page_html(
    target: Path,
    person_name: str,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
    *,
    page_html: PageRenderer,
) -> str:
    return source_item_page_html(
        target,
        person_browser_source(person_name, include_suggestions=True),
        item,
        previous_item,
        next_item,
        month_nav,
        page_html=page_html,
    )


def rotation_buttons_html(source: BrowserSource, item: Any) -> str:
    if not is_image_item(item):
        return ""
    file_id = int(item["id"])
    return f"""
      <button class="nav-button" type="button" title="Roter bildet til venstre" data-rotate-item="{file_id}" data-rotate-direction="left">↺</button>
      <button class="nav-button" type="button" title="Roter bildet til høyre" data-rotate-item="{file_id}" data-rotate-direction="right">↻</button>
    """


def manual_date_button_html(item: Any) -> str:
    file_id = int(item["id"])
    manual_from = item_string_value(item, "manual_date_from")
    manual_to = item_string_value(item, "manual_date_to")
    manual_note = item_string_value(item, "manual_date_note")
    title = "Endre manuell dato" if manual_date_text(item) else "Sett manuell dato"
    return (
        f'<button class="nav-button" type="button" '
        f'title="{title}" '
        f'data-open-manual-date '
        f'data-manual-date-item="{file_id}" '
        f'data-manual-date-from="{html.escape(manual_from)}" '
        f'data-manual-date-to="{html.escape(manual_to)}" '
        f'data-manual-date-note="{html.escape(manual_note)}">'
        f'📅</button>'
    )


def item_string_value(item: Any, key: str) -> str:
    try:
        return str(item[key] or "")
    except (KeyError, IndexError):
        return ""


def remove_manual_location_button_html(item: Any) -> str:
    if not gps_source_is_manual_h3(item):
        return ""
    file_id = int(item["id"])
    return (
        f'<span class="manual-location-remove">(<button class="inline-link danger-inline-link" type="button" '
        'title="Fjern manuell angitt sted bildet er tatt" '
        f'data-remove-manual-location-item="{file_id}">'
        f'fjern</button>)</span>'
    )


def manual_h3_cell_name(target: Path, h3_cell: str, *, conn: sqlite3.Connection | None = None) -> str | None:
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        return db.geo_place_name(conn, h3_cell)
    finally:
        if owned_conn:
            conn.close()


def delete_button_html(source: BrowserSource, item: Any, previous_item: Any | None, next_item: Any | None) -> str:
    redirect_url = source_item_url(source, int(next_item["id"])) if next_item is not None else ""
    if not redirect_url and previous_item is not None:
        redirect_url = source_item_url(source, int(previous_item["id"]))
    if not redirect_url:
        redirect_url = source.root_url
    relative = display_relative_path(Path("."), Path(str(item["target_path"])))
    return (
        f'<button class="nav-button danger-button delete-button" type="button" '
        f'title="Flytt bildet til papirkurven" '
        f'data-delete-item="{int(item["id"])}" '
        f'data-delete-path="{html.escape(relative)}" '
        f'data-delete-redirect="{html.escape(redirect_url)}">Slett</button>'
    )


def image_info_overlay_html() -> str:
    return """
    <div id="infoOverlay" class="info-overlay" hidden>
      <div class="lightbox-bar">
        <div class="lightbox-title">Bildeinfo</div>
        <button class="lightbox-close" type="button" data-close-info>Lukk</button>
      </div>
      <div class="info-panel">
        <h2>Bildeinfo</h2>
        <dl class="info-list" data-info-list></dl>
      </div>
    </div>
    """


def manual_date_overlay_html() -> str:
    return """
    <div id="manualDateOverlay" class="info-overlay" hidden>
      <div class="lightbox-bar">
        <div class="lightbox-title">Manuell dato</div>
        <button class="lightbox-close" type="button" data-close-manual-date>Lukk</button>
      </div>
      <form class="modal-panel manual-date-panel" data-manual-date-form>
        <h2>Manuell dato</h2>
        <fieldset class="manual-date-modes">
          <label><input type="radio" name="mode" value="exact" checked> Eksakt dato</label>
          <label><input type="radio" name="mode" value="uncertain"> Usikker dato</label>
          <label><input type="radio" name="mode" value="between"> Intervall</label>
        </fieldset>
        <label data-manual-date-field="date">Dato
          <input type="date" name="date">
        </label>
        <label data-manual-date-field="uncertainty">Usikkerhet (d=dag, w=uke, m=måned, y=år)
          <input type="text" name="uncertainty" placeholder="1m">
        </label>
        <label data-manual-date-field="date_from">Fra-dato
          <input type="date" name="date_from">
        </label>
        <label data-manual-date-field="date_to">Til-dato
          <input type="date" name="date_to">
        </label>
        <label>Notat
          <input type="text" name="note">
        </label>
        <p class="assign-status" data-manual-date-status></p>
        <div class="modal-actions">
          <button class="danger-button" type="button" data-clear-manual-date hidden>Fjern manuell dato</button>
          <button type="button" data-close-manual-date>Avbryt</button>
          <button type="submit">Lagre</button>
        </div>
      </form>
    </div>
    """


def image_info_content_html(target: Path, item: Any) -> str:
    return "\n".join(image_info_rows(target, item))


def image_info_rows(target: Path, item: Any) -> list[str]:
    target_path = Path(str(item["target_path"]))
    absolute_path = db.absolute_target_path(target, target_path)
    dimensions = cached_image_dimensions(target, absolute_path)
    rows = [
        info_row_html("Filnavn", display_relative_path(target, target_path)),
        info_row_html("Dato", image_date_text(item)),
        info_row_html("Filstørrelse", f"{format_bytes(int(item['size_bytes']))} ({int(item['size_bytes'])} bytes)"),
        info_row_html("Oppløsning", f"{dimensions.width} x {dimensions.height}" if dimensions else "-"),
        info_row_html("Kamera", camera_text_from_item(item)),
    ]
    if manual_date_text(item):
        rows.append(info_row_html("Opprinnelig dato", f"{item['taken_date'] or '-'} ({date_source_text(str(item['date_source'] or ''))})"))
        if item["manual_date_note"]:
            rows.append(info_row_html("Datonotat", str(item["manual_date_note"])))
    sources = image_source_rows(target, target_path)
    if sources:
        rows.append(info_row_html("Kilder", "\n\n".join(sources), multiline=True))
    else:
        rows.append(info_row_html("Kilder", "-"))
    tags = image_tag_links_html(target, int(item["id"]))
    if tags:
        rows.append(info_row_html("Tagger", tags, raw_html=True))
    manual_h3_label = manual_h3_label_html(target, item)
    if manual_h3_label:
        rows.append(info_row_html("", manual_h3_label, raw_html=True))
    maps_link = google_maps_link_html(item)
    if maps_link:
        rows.append(info_row_html("Kart", maps_link, raw_html=True))
    if gps_source_text(item) != "ukjent":
        rows.append(info_row_html("GPS-kilde", gps_source_text(item)))
    geo_links = image_geo_area_links_html(target, item)
    if geo_links:
        rows.append(info_row_html("Steder", geo_links, raw_html=True))
    return rows


def image_tag_links_html(target: Path, file_id: int) -> str:
    conn = db.connect(target)
    try:
        rows = db.tags_for_file(conn, file_id)
    finally:
        conn.close()
    links = []
    for row in rows:
        name = str(row["name"])
        suffix = " (system)" if row["kind"] == db.TAG_KIND_SYSTEM else ""
        url = "/tag/" + urllib.parse.quote(name, safe="")
        links.append(f'<a href="{html.escape(url)}">{html.escape(name)}</a>{html.escape(suffix)}')
    return ", ".join(links)


def manual_h3_label_html(target: Path, item: Any) -> str:
    if not gps_source_is_manual_h3(item):
        return ""
    conn = db.connect(target)
    try:
        place_name = manual_h3_place_name(conn, item)
        h3_cell = manual_h3_cell(item)
    finally:
        conn.close()
    return manual_h3_status_html(place_name, h3_cell)


def gps_source_is_manual_h3(item: Any) -> bool:
    try:
        return str(item["gps_source"] or "") == "manual-h3"
    except (KeyError, IndexError):
        return False


def manual_h3_place_name(conn: sqlite3.Connection, item: Any) -> str | None:
    if not gps_source_is_manual_h3(item):
        return None
    for resolution in sorted(H3_COLUMNS, reverse=True):
        column = H3_COLUMNS[resolution]
        try:
            h3_cell = item[column]
        except (KeyError, IndexError):
            continue
        if not h3_cell:
            continue
        place_name = db.geo_place_name(conn, str(h3_cell))
        if place_name:
            return place_name
    return None


def manual_h3_cell(item: Any) -> str | None:
    if not gps_source_is_manual_h3(item):
        return None
    for resolution in sorted(H3_COLUMNS, reverse=True):
        column = H3_COLUMNS[resolution]
        try:
            h3_cell = item[column]
        except (KeyError, IndexError):
            continue
        if h3_cell:
            return str(h3_cell)
    return None


def manual_h3_link_html(label: str, h3_cell: str | None) -> str:
    if not h3_cell:
        return html.escape(label)
    url = "https://h3geo.org/#hex=" + urllib.parse.quote_plus(h3_cell)
    return f'<a href="{html.escape(url)}" target="_blank" ' \
           'title="Vis plasseringen på kartet på https://h3geo.org/" ' \
           f'rel="noopener">{html.escape(label)}</a>'


def manual_h3_status_html(place_name: str | None, h3_cell: str | None = None) -> str:
    label = f"Manuell H3: {place_name}" if place_name else "Manuell H3"
    return f'<span class="status">{manual_h3_link_html(label, h3_cell)}</span>'


def manual_h3_badge_html(place_name: str | None, h3_cell: str | None = None, *, extra_html: str = "") -> str:
    label = f"Manuell H3: {place_name}" if place_name else "Manuell H3"
    return f'<div class="location-status-badge">{manual_h3_link_html(label, h3_cell)}{extra_html}</div>'


def gps_location_badge_html(item: Any, *, extra_html: str = "") -> str:
    coordinates = gps_coordinate_pair(item)
    if coordinates is None:
        return f'<div class="location-status-badge">GPS mangler{extra_html}</div>'
    latitude, longitude = coordinates
    url = google_maps_url(latitude, longitude)
    return (
        '<div class="location-status-badge">'
        f'<a href="{html.escape(url)}" target="_blank" rel="noopener">GPS-lokalisert</a>'
        f'{extra_html}'
        '</div>'
    )


def image_date_text(item: Any) -> str:
    manual_text = manual_date_text(item)
    if manual_text:
        return manual_text
    taken_date = str(item["taken_date"] or "-")
    source = str(item["date_source"] or "")
    return f"{taken_date} ({date_source_text(source)})"


def manual_date_text(item: Any) -> str:
    try:
        date_from = parse_iso_date(str(item["manual_date_from"] or ""))
        date_to = parse_iso_date(str(item["manual_date_to"] or ""))
    except (KeyError, IndexError):
        return ""
    if date_from is None or date_to is None:
        return ""
    if date_from == date_to:
        return f"{date_from.isoformat()} (manuell dato)"
    midpoint = date_from + (date_to - date_from) // 2
    uncertainty_days = max((date_to - date_from).days // 2, 1)
    return f"ca. {midpoint.isoformat()} ± {format_uncertainty_days(uncertainty_days)} (manuell dato)"


def format_uncertainty_days(days: int) -> str:
    if days % 365 == 0:
        years = days // 365
        return f"{years} år" if years != 1 else "1 år"
    if days % 30 == 0:
        months = days // 30
        return f"{months} måneder" if months != 1 else "1 måned"
    if days % 7 == 0:
        weeks = days // 7
        return f"{weeks} uker" if weeks != 1 else "1 uke"
    return f"{days} dager" if days != 1 else "1 dag"


def date_source_text(source: str) -> str:
    labels = {
        "metadata": "fra metadata",
        "filename": "fra filnavn",
        "mtime": "fra mtime",
        "unknown": "ukjent datokilde",
    }
    return labels.get(source, source or "ukjent datokilde")


def google_maps_link_html(item: Any) -> str:
    coordinates = gps_coordinate_pair(item)
    if coordinates is None:
        return ""
    latitude, longitude = coordinates
    label = f"Åpne i Google Maps ({latitude:.7f}, {longitude:.7f})"
    url = google_maps_url(latitude, longitude)
    return f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(label)}</a>'


def gps_coordinate_pair(item: Any) -> tuple[float, float] | None:
    try:
        lat = item["gps_lat"]
        lon = item["gps_lon"]
    except (KeyError, IndexError):
        return None
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def google_maps_url(latitude: float, longitude: float) -> str:
    query = urllib.parse.quote(f"{latitude:.7f},{longitude:.7f}", safe=",")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


def gps_source_text(item: Any) -> str:
    try:
        source = str(item["gps_source"] or "")
    except (KeyError, IndexError):
        source = ""
    labels = {
        "exiftool": "fra metadata",
        "manual-h3": "satt manuelt",
    }
    return labels.get(source, source or "ukjent")


def camera_text_from_item(item: Any) -> str:
    parts = [
        str(part)
        for part in (optional_item_value(item, "camera_make"), optional_item_value(item, "camera_model"))
        if part
    ]
    return " ".join(parts) if parts else "-"


def optional_item_value(item: Any, key: str) -> Any | None:
    try:
        return item[key]
    except (KeyError, IndexError):
        return None


def image_source_rows(target: Path, target_path: Path) -> list[str]:
    conn = db.connect(target)
    try:
        rows = db.file_sources_by_target_path(conn, target, db.absolute_target_path(target, target_path))
    finally:
        conn.close()
    result = []
    for row in rows:
        source_name = str(row["source_name"] or row["source_root"] or f"Kilde #{row['source_id']}")
        result.append(f"{source_name}: {row['source_path']}")
    return result


def image_geo_area_links_html(target: Path, item: Any) -> str:
    conn = db.connect(target)
    try:
        links = []
        for resolution, column in H3_COLUMNS.items():
            h3_cell = item[column]
            if not h3_cell:
                continue
            place_name = db.geo_place_name(conn, str(h3_cell))
            label = f"H3-{resolution}: {h3_cell} ({h3_area_label(resolution)})"
            if place_name:
                label += f" {place_name}"
            url = "/geo/area/" + urllib.parse.quote(str(h3_cell), safe="")
            links.append(f'<a href="{html.escape(url)}">{html.escape(label)}</a>')
        return "<br>".join(links)
    finally:
        conn.close()


def info_row_html(label: str, value: str, *, multiline: bool = False, raw_html: bool = False) -> str:
    escaped_value = value if raw_html else html.escape(value)
    if multiline and not raw_html:
        escaped_value = "<br>".join(escaped_value.splitlines())
    return f"""
    <div class="info-row">
      <dt>{html.escape(label)}</dt>
      <dd>{escaped_value}</dd>
    </div>
    """


def month_page_html(
    target: Path,
    month_key: str,
    items: list[Any],
    *,
    page_html: PageRenderer,
) -> str:
    return source_month_page_html(target, all_browser_source(), month_key, items, page_html=page_html)


def years_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    hide_out_of_focus: bool = False,
) -> str:
    cards = "\n".join(
        year_card_html(target, card)
        for card in browser_year_cards(target, hide_out_of_focus=hide_out_of_focus)
    )
    content = cards if cards else '<p class="meta">Ingen filer i bildesamlingen.</p>'
    return shell_page_html(
        "År",
        f"""
        <h1>År</h1>
        <section class="month-grid-server">{content}</section>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def year_months_page_html(
    target: Path,
    year: str,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    hide_out_of_focus: bool = False,
) -> str:
    cards = "\n".join(
        year_month_card_html(target, all_browser_source(), card)
        for card in browser_year_month_cards(target, year, hide_out_of_focus=hide_out_of_focus)
    )
    content = cards if cards else '<p class="meta">Ingen bilder dette året.</p>'
    escaped_year = html.escape(year)
    return shell_page_html(
        escaped_year,
        f"""
        <h1>{escaped_year}</h1>
        <section class="month-grid-server">{content}</section>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        title_html=breadcrumb_html([("År", "/years")], escaped_year),
    )


def source_year_months_page_html(
    target: Path,
    source: BrowserSource,
    year: str,
    *,
    page_html: PageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
    hide_out_of_focus: bool = False,
) -> str:
    from .server_shell import app_header_html

    cards = "\n".join(
        year_month_card_html(target, source, card)
        for card in source_year_month_cards(
            target,
            source,
            year,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
        )
    )
    content = cards if cards else '<p class="meta">Ingen bilder dette året.</p>'
    escaped_year = html.escape(year)
    return page_html(
        f"{source.title}: {escaped_year}",
        f"""
        <main class="server-browser">
          {app_header_html(
              source.title,
              source=source,
              title_html=source_year_breadcrumb_html(
                  target,
                  source,
                  year,
                  face_config,
                  hide_out_of_focus=hide_out_of_focus,
              ),
              face_enabled=face_enabled,
              openclip_enabled=openclip_enabled,
          )}
          <section class="month-grid-server">{content}</section>
          <footer class="browser-footer">
            <span class="filename">Årsoversikt: {escaped_year}</span>
          </footer>
        </main>
        """,
    )


def source_year_breadcrumb_html(
    target: Path,
    source: BrowserSource,
    year: str,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> str:
    if not valid_year_key(year):
        return html.escape(source.title)
    source_label, source_title = source_breadcrumb_label(
        target,
        source,
        face_config,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
    )
    return breadcrumb_html([(source_label, source.root_url, source_title)], html.escape(year))


def year_card_html(target: Path, card: dict[str, Any]) -> str:
    year = str(card["year"])
    month_count = int(card["month_count"])
    item = card["item"]
    month_label = "måned" if month_count == 1 else "måneder"
    media = thumbnail_media_html(target, item)
    return f"""
    <article class="item">
      <a class="thumb-link" href="/years/{html.escape(urllib.parse.quote(year))}">{media}</a>
      <div class="text">
        <div class="path">{html.escape(year)}</div>
        <div class="score">{month_count} {month_label}</div>
      </div>
    </article>
    """


def year_month_card_html(target: Path, source: BrowserSource, card: dict[str, Any]) -> str:
    month_key = str(card["month_key"])
    item_count = int(card["item_count"])
    item = card["item"]
    image_label = "bilde" if item_count == 1 else "bilder"
    media = thumbnail_media_html(target, item)
    return f"""
    <article class="item">
      <a class="thumb-link" href="{html.escape(source_month_url(source, month_key))}">{media}</a>
      <div class="text">
        <div class="path">{html.escape(month_key)}</div>
        <div class="score">{item_count} {image_label}</div>
      </div>
    </article>
    """


def source_month_page_html(
    target: Path,
    source: BrowserSource,
    month_key: str,
    items: list[Any],
    *,
    page_html: PageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
    hide_out_of_focus: bool = False,
) -> str:
    from .server_shell import app_header_html, source_controls_html, suggestion_toggle_button_html

    cards = "\n".join(source_month_item_html(target, source, item) for item in items)
    previous_item = items[-1] if items else None
    next_item = items[0] if items else None
    controls = source_controls_html(
        source,
        source_month_navigation_for_key(
            target,
            source,
            month_key,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
        ),
        previous_item,
        next_item,
        suggestion_toggle_button=suggestion_toggle_button_html(source, None, face_enabled=face_enabled),
    )
    return page_html(
        f"{source.title}: {month_key}",
        f"""
        <main class="server-browser month-browser">
          {app_header_html(
              source.title,
              source=source,
              title_html=source_month_breadcrumb_html(
                  target,
                  source,
                  month_key,
                  face_config,
                  hide_out_of_focus=hide_out_of_focus,
              ),
              controls=controls,
              face_enabled=face_enabled,
              openclip_enabled=openclip_enabled,
          )}
          <section class="month-grid-server">{cards}</section>
          <footer class="browser-footer">
            <span class="filename">Månedsoversikt: {html.escape(month_key)}</span>
          </footer>
        </main>
        """,
    )


def empty_person_browser_html(
    person: str | BrowserSource,
    *,
    shell_page_html: ShellPageRenderer,
    openclip_enabled: bool = True,
) -> str:
    source = person if isinstance(person, BrowserSource) else person_browser_source(person, include_suggestions=True)
    return empty_source_html(source, shell_page_html=shell_page_html, openclip_enabled=openclip_enabled)


def empty_source_html(
    source: BrowserSource,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    return shell_page_html(
        source.title,
        f"""
        <h1>{html.escape(source.title)}</h1>
        <p class="meta">{html.escape(empty_source_message(source))}</p>
        """,
        source=source,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def empty_source_message(source: BrowserSource) -> str:
    if source.person_name is None:
        if source.source_id is not None:
            return "Ingen aktive bilder for denne kilden."
        if source.geo_place_slug is not None:
            return "Ingen aktive bilder for dette stedet."
        if source.text_filter is not None:
            return "Ingen aktive bilder matcher filtersøket."
        return "Ingen filer i bildesamlingen."
    if source.include_suggestions:
        return "Ingen bekreftede ansikter eller forslag for denne personen ennå."
    return "Ingen bekreftede bilder for denne personen ennå."


def person_not_found_html(
    person_name: str,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    return shell_page_html(
        "Fant ikke person",
        f"""
        <h1>Fant ikke person</h1>
        <p class="error">{html.escape(person_name)}</p>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def person_month_page_html(
    target: Path,
    person_name: str,
    month_key: str,
    items: list[Any],
    *,
    page_html: PageRenderer,
) -> str:
    return source_month_page_html(
        target,
        person_browser_source(person_name, include_suggestions=True),
        month_key,
        items,
        page_html=page_html,
    )


def source_month_breadcrumb_html(
    target: Path,
    source: BrowserSource,
    month_key: str,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> str:
    if not valid_month_key(month_key):
        return html.escape(source.title)
    year, month = month_key.split("-", 1)
    month_name = MONTH_NAMES.get(month, month_key)
    crumbs: list[Breadcrumb]
    if source == all_browser_source():
        crumbs = [
            ("År", "/years"),
            (year, source_year_url(source, year)),
        ]
    else:
        source_label, source_title = source_breadcrumb_label(
            target,
            source,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
            conn=conn,
        )
        crumbs = [
            (source_label, source.root_url, source_title),
            (year, source_year_url(source, year)),
        ]
    return breadcrumb_html(crumbs, html.escape(month_name))


def source_month_item_html(target: Path, source: BrowserSource, item: Any) -> str:
    target_path = Path(str(item["target_path"]))
    label = html.escape(display_relative_path(target, target_path))
    media = thumbnail_media_html(target, item)
    return f"""
    <article class="item">
      <a class="thumb-link" href="{source_item_url(source, int(item["id"]))}">{media}</a>
      <div class="text">
        <div class="path">{label}</div>
        <div class="score">{html.escape(format_bytes(int(item["size_bytes"])))}</div>
      </div>
    </article>
    """


def thumbnail_media_html(target: Path, item: Any) -> str:
    target_path = Path(str(item["target_path"]))
    name = html.escape(str(item["stored_filename"]))
    kind = media_kind(target_path)
    if kind == "video":
        return f'<div class="video-thumb">Video<br>{name}</div>'
    if kind != "image":
        return f'<div class="video-thumb">Fil<br>{name}</div>'
    relative_path = db.target_relative_path(target, target_path)
    thumbnail_src = "/file/" + existing_thumbnail_url(target, relative_path)
    return f'<img src="{html.escape(thumbnail_src)}" alt="{name}" loading="lazy"{rotation_style_attr(item)}>'


def all_source_items(target: Path, *, hide_out_of_focus: bool = False) -> list[Any]:
    where_sql, params = all_source_where(target, hide_out_of_focus=hide_out_of_focus)
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE {where_sql}
                ORDER BY {ITEM_ORDER_SQL}
                """,
                params,
            )
        )
    finally:
        conn.close()


def items_by_file_ids(target: Path, file_ids: list[int], *, hide_out_of_focus: bool = False) -> list[Any]:
    if not file_ids:
        return []
    placeholders = ",".join("?" for _ in file_ids)
    where_sql, params = all_source_where(target, hide_out_of_focus=hide_out_of_focus)
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE {where_sql}
                  AND id IN ({placeholders})
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (*params, *file_ids),
            )
        )
    finally:
        conn.close()


def browser_month_navigation(target: Path, item: Any, *, hide_out_of_focus: bool = False) -> dict[str, str | None]:
    current_key = month_key_for_item(target, item)
    return browser_month_navigation_for_key(target, current_key, hide_out_of_focus=hide_out_of_focus)


def month_key_for_item(target: Path, item: Any) -> str:
    browser_date = browser_date_for_item(item)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", browser_date):
        return browser_date[:7]
    stored_key = month_key_from_stored_path(str(item["target_path"]))
    return stored_key or month_key_from_path(relative_to_target(target, Path(str(item["target_path"]))))


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
    *,
    hide_out_of_focus: bool = False,
) -> list[Any]:
    if source.person_name is not None:
        from .server_faces import person_items

        items = person_items(
            target,
            source.person_name,
            include_suggestions=source.include_suggestions,
            face_config=face_config,
        )
        items = filter_motion_video_items(target, items)
        return filter_out_of_focus_items(target, source, items, hide_out_of_focus)
    if source.geo_place_slug is not None:
        from .server_geo import geo_place_items

        items = filter_motion_video_items(target, geo_place_items(target, source.geo_place_slug))
        return filter_out_of_focus_items(target, source, items, hide_out_of_focus)
    if source.text_filter is not None:
        from .server_filter import text_filter_items

        return text_filter_items(target, source.text_filter, hide_out_of_focus=hide_out_of_focus)
    if source.source_id is not None:
        return imported_source_items(target, source.source_id, hide_out_of_focus=hide_out_of_focus)
    if source.tag_name is not None:
        items = filter_motion_video_items(target, tagged_items(target, source.tag_name))
        return filter_out_of_focus_items(target, source, items, hide_out_of_focus)
    return all_source_items(target, hide_out_of_focus=hide_out_of_focus)


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
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict[str, str | None]:
    return source_month_navigation_for_key(
        target,
        source,
        month_key_for_item(target, item),
        face_config,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
    )


def source_month_navigation_for_key(
    target: Path,
    source: BrowserSource,
    current_key: str,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict[str, str | None]:
    if not valid_month_key(current_key):
        return {"previous_year": None, "next_year": None, "previous_month": None, "next_month": None}
    keys = source_month_keys(target, source, face_config, hide_out_of_focus=hide_out_of_focus, conn=conn)
    return month_navigation_for_keys(keys, current_key)


def month_navigation_for_keys(keys: list[str], current_key: str) -> dict[str, str | None]:
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
    *,
    hide_out_of_focus: bool = False,
) -> list[Any]:
    if source_has_sql_filter(source):
        return sql_filtered_source_month_items(target, source, month_key, face_config, hide_out_of_focus=hide_out_of_focus)
    if (
        source.person_name is not None
        or source.source_id is not None
        or source.tag_name is not None
        or source.text_filter is not None
    ):
        return [
            item
            for item in source_items(target, source, face_config, hide_out_of_focus=hide_out_of_focus)
            if month_key_for_item(target, item) == month_key
        ]
    return browser_month_items(target, month_key, hide_out_of_focus=hide_out_of_focus)


def sql_filtered_source_month_items(
    target: Path,
    source: BrowserSource,
    month_key: str,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
) -> list[Any]:
    if not valid_month_key(month_key):
        return []
    where_sql, params = source_sql_filter(source)
    where_sql, params = with_motion_video_filter(
        target,
        where_sql,
        params,
        include_motion=source_shows_motion_videos(source),
    )
    where_sql, params = with_out_of_focus_filter(source, where_sql, params, hide_out_of_focus)
    deleted_sql = "1 = 1" if source_includes_deleted(source) else "deleted_at IS NULL"
    conn = db.connect(target)
    try:
        attach_source_sql_filter_databases(conn, target, source, face_config)
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE {deleted_sql}
                  AND ({where_sql})
                  AND substr({db.BROWSER_DATE_ORDER_SQL}, 1, 7) = ?
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (*params, month_key),
            )
        )
    finally:
        conn.close()


def attach_source_sql_filter_databases(
    conn: Any,
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
) -> None:
    if source.person_name is not None:
        attach_face_database(conn, target, face_config)
    if source.text_filter is None:
        return
    from .server_filter import attach_text_filter_databases

    attach_text_filter_databases(conn, target, source.text_filter)


def attach_face_database(
    conn: Any,
    target: Path,
    face_config: FaceRecognitionConfig | None = None,
) -> None:
    if any(str(row["name"]) == "face_db" for row in conn.execute("PRAGMA database_list")):
        return
    from .face import connect_face_db, face_db_path

    face_conn = connect_face_db(target, face_config)
    face_conn.close()
    conn.execute("ATTACH DATABASE ? AS face_db", (str(face_db_path(target, face_config)),))


def browser_month_navigation_for_key(
    target: Path,
    current_key: str,
    *,
    hide_out_of_focus: bool = False,
) -> dict[str, str | None]:
    if not valid_month_key(current_key):
        return {
            "previous_year": None,
            "next_year": None,
            "previous_month": None,
            "next_month": None,
        }
    keys = browser_month_keys(target, hide_out_of_focus=hide_out_of_focus)
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


def browser_month_items(target: Path, month_key: str, *, hide_out_of_focus: bool = False) -> list[Any]:
    if not valid_month_key(month_key):
        return []
    where_sql, params = all_source_where(target, hide_out_of_focus=hide_out_of_focus)
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE {where_sql}
                  AND substr({db.BROWSER_DATE_ORDER_SQL}, 1, 7) = ?
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (*params, month_key),
            )
        )
    finally:
        conn.close()
