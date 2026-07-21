from __future__ import annotations

import sqlite3
import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import db
from .browser_dates import (
    browser_date_from_item,
    manual_date_midpoint_for_item as shared_manual_date_midpoint_for_item,
    month_key_for_item as shared_month_key_for_item,
    month_key_from_stored_path as shared_month_key_from_stored_path,
    next_month_key as shared_next_month_key,
    parse_iso_date as shared_parse_iso_date,
    valid_day_key as shared_valid_day_key,
    valid_month_key as shared_valid_month_key,
)
from .config import FaceRecognitionConfig
from .media import IMAGE_EXTENSIONS, media_kind
from .server_browser_sidecars import (
    hidden_sidecar_file_ids_for_day,
    motion_video_file_ids,
    raw_sidecar_file_ids,
)
from .server_browser_sources import (
    BrowserSource,
    all_browser_source,
    is_filtered_source,
    person_browser_source,
    source_has_sql_filter,
    source_includes_deleted,
    source_sql_filter,
)


FILE_COLUMNS = (
    "id, target_path, target_path_key, original_filename, stored_filename, sha256, taken_date, date_source, "
    "metadata_datetime, "
    "comment, "
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


def first_source_day_item(
    target: Path,
    source: BrowserSource,
    day_key: str,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> Any | None:
    if not valid_day_key(day_key):
        return None
    if source_has_sql_filter(source):
        return first_sql_filtered_source_day_item(
            target,
            source,
            day_key,
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
        month_key = day_key[:7]
        return next(
            (
                item
                for item in source_month_items(
                    target,
                    source,
                    month_key,
                    face_config,
                    hide_out_of_focus=hide_out_of_focus,
                )
                if browser_date_for_item(item) == day_key
            ),
            None,
        )
    return first_unfiltered_source_day_item(target, day_key, hide_out_of_focus=hide_out_of_focus, conn=conn)


def first_sql_filtered_source_day_item(
    target: Path,
    source: BrowserSource,
    day_key: str,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> Any | None:
    where_sql, params = source_sql_filter(source)
    owned_conn = conn is None
    conn = conn or db.connect(target)
    where_sql, params = with_out_of_focus_filter(source, where_sql, params, hide_out_of_focus)
    if not source_shows_motion_videos(source):
        hidden_ids = sorted(hidden_sidecar_file_ids_for_day(conn, day_key))
        if hidden_ids:
            placeholders = ",".join("?" for _ in hidden_ids)
            where_sql = f"({where_sql}) AND id NOT IN ({placeholders})"
            params = (*params, *hidden_ids)
    deleted_sql = "1 = 1" if source_includes_deleted(source) else "deleted_at IS NULL"
    try:
        attach_source_sql_filter_databases(conn, target, source, face_config)
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE {deleted_sql}
              AND ({where_sql})
              AND {db.BROWSER_DATE_ORDER_SQL} = ?
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            (*params, day_key),
        ).fetchone()
    finally:
        if owned_conn:
            conn.close()


def first_unfiltered_source_day_item(
    target: Path,
    day_key: str,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> Any | None:
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        where_sql = "deleted_at IS NULL"
        params: tuple[object, ...] = ()
        if hide_out_of_focus:
            where_sql = f"{where_sql} AND {OUT_OF_FOCUS_FILTER_SQL}"
            params = (*params, *OUT_OF_FOCUS_FILTER_PARAMS)
        hidden_ids = sorted(hidden_sidecar_file_ids_for_day(conn, day_key))
        if hidden_ids:
            placeholders = ",".join("?" for _ in hidden_ids)
            where_sql = f"({where_sql}) AND id NOT IN ({placeholders})"
            params = (*params, *hidden_ids)
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE {where_sql}
              AND {db.BROWSER_DATE_ORDER_SQL} = ?
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            (*params, day_key),
        ).fetchone()
    finally:
        if owned_conn:
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


def active_item_by_id_including_hidden(
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
              AND deleted_at IS NULL
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
    return browser_date_from_item(item)


def manual_date_midpoint_for_item(item: Any) -> dt.date | None:
    return shared_manual_date_midpoint_for_item(item)


def parse_iso_date(value: str) -> dt.date | None:
    return shared_parse_iso_date(value)


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
    base_sql, params = hidden_sidecar_id_filter_sql(target, "deleted_at IS NULL", (), conn=conn)
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
    return hidden_sidecar_id_filter_sql(target, f"({where_sql})", params, conn=conn)


def hidden_sidecar_id_filter_sql(
    target: Path,
    where_sql: str,
    params: tuple[object, ...],
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[str, tuple[object, ...]]:
    hidden_ids = sorted(motion_video_file_ids(target, conn=conn) | raw_sidecar_file_ids(target, conn=conn))
    if not hidden_ids:
        return where_sql, params
    placeholders = ",".join("?" for _ in hidden_ids)
    return f"({where_sql}) AND id NOT IN ({placeholders})", (*params, *hidden_ids)


def source_shows_motion_videos(source: BrowserSource) -> bool:
    if source.text_filter is None:
        return False
    from .server_filter import text_filter_shows_motion_videos, text_filter_shows_sidecar_files

    return text_filter_shows_motion_videos(source.text_filter) or text_filter_shows_sidecar_files(source.text_filter)


def filter_out_of_focus_items(target: Path, source: BrowserSource, items: list[Any], hide_out_of_focus: bool) -> list[Any]:
    if not should_filter_out_of_focus(source, hide_out_of_focus) or not items:
        return items
    hidden_ids = out_of_focus_file_ids(target)
    return [item for item in items if int(item["id"]) not in hidden_ids]


def filter_motion_video_items(target: Path, items: list[Any], *, include_motion: bool = False) -> list[Any]:
    if include_motion or not items:
        return items
    hidden_ids = motion_video_file_ids(target) | raw_sidecar_file_ids(target)
    return [item for item in items if int(item["id"]) not in hidden_ids]




def out_of_focus_file_ids(target: Path) -> set[int]:
    conn = db.connect(target)
    try:
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
        or source.missing_face_suggestions
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
    return shared_valid_month_key(value)


def next_month_key(month_key: str) -> str | None:
    return shared_next_month_key(month_key)


def valid_day_key(value: str) -> bool:
    return shared_valid_day_key(value)


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


def image_extension_sql(column: str) -> str:
    clauses = [f"lower({column}) LIKE '%{extension}'" for extension in sorted(IMAGE_EXTENSIONS)]
    return "(" + " OR ".join(clauses) + ")"


def browser_year_summaries(target: Path, *, hide_out_of_focus: bool = False) -> list[dict[str, int | str]]:
    conn = db.connect(target)
    try:
        where_sql, params = all_source_where(target, hide_out_of_focus=hide_out_of_focus)
        month_rows = conn.execute(
            f"""
            WITH visible AS (
                SELECT
                    {db.BROWSER_DATE_ORDER_SQL} AS browser_date
                FROM files
                WHERE {where_sql}
            )
            SELECT
                substr(browser_date, 1, 7) AS month_key,
                COUNT(*) AS item_count
            FROM visible
            WHERE browser_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            GROUP BY month_key
            ORDER BY month_key
            """,
            params,
        )
        summaries_by_year: dict[str, dict[str, int | str]] = {}
        for row in month_rows:
            month_key = str(row["month_key"])
            if not valid_month_key(month_key):
                continue
            year = month_key[:4]
            if not valid_year_key(year):
                continue
            summary = summaries_by_year.setdefault(
                year,
                {"year": year, "month_count": 0, "item_count": 0, "first_month": month_key},
            )
            summary["month_count"] = int(summary["month_count"]) + 1
            summary["item_count"] = int(summary["item_count"]) + int(row["item_count"])

        summaries: list[dict[str, int | str]] = []
        for year in sorted(summaries_by_year):
            summary = summaries_by_year[year]
            first_month = str(summary["first_month"])
            item_id = first_year_thumbnail_item_id(conn, where_sql, params, first_month)
            if item_id is None:
                continue
            summary["item_id"] = item_id
            summaries.append(summary)
        return summaries
    finally:
        conn.close()


def first_year_thumbnail_item_id(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple[object, ...],
    first_month: str,
) -> int | None:
    next_month = next_month_key(first_month)
    if next_month is None:
        return None
    row = conn.execute(
        f"""
        SELECT id
        FROM files
        WHERE {where_sql}
          AND {db.BROWSER_DATE_ORDER_SQL} >= ?
          AND {db.BROWSER_DATE_ORDER_SQL} < ?
        ORDER BY
          CASE WHEN {image_extension_sql("target_path")} THEN 0 ELSE 1 END,
          {ITEM_ORDER_SQL}
        LIMIT 1
        """,
        (*params, f"{first_month}-01", f"{next_month}-01"),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def browser_year_cards(target: Path, *, hide_out_of_focus: bool = False) -> list[dict[str, Any]]:
    summaries = browser_year_summaries(target, hide_out_of_focus=hide_out_of_focus)
    items = {
        int(item["id"]): item
        for item in items_by_file_ids(
            target,
            [int(summary["item_id"]) for summary in summaries],
            hide_out_of_focus=hide_out_of_focus,
        )
    }
    cards: list[dict[str, Any]] = []
    for summary in summaries:
        year = str(summary["year"])
        item = items.get(int(summary["item_id"]))
        if item is None:
            continue
        cards.append(
            {
                "year": year,
                "month_count": int(summary["month_count"]),
                "item_count": int(summary["item_count"]),
                "first_month": str(summary["first_month"]),
                "item": item,
            }
        )
    return cards


def source_year_cards(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
) -> list[dict[str, Any]]:
    if source_has_sql_filter(source):
        return sql_filtered_source_year_cards(
            target,
            source,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
        )

    summaries_by_year: dict[str, dict[str, Any]] = {}
    for month_key in source_month_keys(target, source, face_config, hide_out_of_focus=hide_out_of_focus):
        year = month_key[:4]
        if not valid_year_key(year):
            continue
        items = source_month_items(
            target,
            source,
            month_key,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
        )
        if not items:
            continue
        summary = summaries_by_year.setdefault(
            year,
            {
                "year": year,
                "month_count": 0,
                "item_count": 0,
                "first_month": month_key,
                "item": representative_image_item(items),
            },
        )
        summary["month_count"] = int(summary["month_count"]) + 1
        summary["item_count"] = int(summary["item_count"]) + len(items)

    return [summaries_by_year[year] for year in sorted(summaries_by_year)]


def sql_filtered_source_year_cards(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
) -> list[dict[str, Any]]:
    where_sql, params = source_sql_filter(source)
    conn = db.connect(target)
    try:
        where_sql, params = with_motion_video_filter(
            target,
            where_sql,
            params,
            include_motion=source_shows_motion_videos(source),
            conn=conn,
        )
        where_sql, params = with_out_of_focus_filter(
            source,
            where_sql,
            params,
            hide_out_of_focus,
        )
        deleted_sql = "1 = 1" if source_includes_deleted(source) else "deleted_at IS NULL"
        attach_source_sql_filter_databases(conn, target, source, face_config)
        rows = conn.execute(
            f"""
            SELECT
                id,
                target_path,
                {db.BROWSER_DATE_ORDER_SQL} AS browser_date
            FROM files
            WHERE {deleted_sql}
              AND ({where_sql})
              AND {db.BROWSER_DATE_ORDER_SQL} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            ORDER BY {ITEM_ORDER_SQL}
            """,
            params,
        )

        summaries_by_year: dict[str, dict[str, Any]] = {}
        for row in rows:
            month_key = str(row["browser_date"])[:7]
            if not valid_month_key(month_key):
                continue
            year = month_key[:4]
            if not valid_year_key(year):
                continue
            summary = summaries_by_year.get(year)
            if summary is None:
                summaries_by_year[year] = {
                    "year": year,
                    "month_count": 1,
                    "item_count": 1,
                    "first_month": month_key,
                    "item_id": int(row["id"]),
                    "item_is_image": is_image_item(row),
                    "last_month": month_key,
                }
                continue

            summary["item_count"] = int(summary["item_count"]) + 1
            if month_key != summary["last_month"]:
                summary["month_count"] = int(summary["month_count"]) + 1
                summary["last_month"] = month_key
            if (
                month_key == summary["first_month"]
                and not bool(summary["item_is_image"])
                and is_image_item(row)
            ):
                summary["item_id"] = int(row["id"])
                summary["item_is_image"] = True

        summaries = [summaries_by_year[year] for year in sorted(summaries_by_year)]
        item_ids = [int(summary["item_id"]) for summary in summaries]
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
        items = {
            int(item["id"]): item
            for item in conn.execute(
                f"SELECT {FILE_COLUMNS} FROM files WHERE id IN ({placeholders})",
                item_ids,
            )
        }
        return [
            {
                "year": str(summary["year"]),
                "month_count": int(summary["month_count"]),
                "item_count": int(summary["item_count"]),
                "first_month": str(summary["first_month"]),
                "item": items[int(summary["item_id"])],
            }
            for summary in summaries
            if int(summary["item_id"]) in items
        ]
    finally:
        conn.close()


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
        or source.missing_face_suggestions
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
                    files.comment,
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
                    COUNT(file_sources.id) AS source_file_count,
                    COUNT(CASE WHEN files.deleted_at IS NULL THEN 1 END) AS active_file_count,
                    COUNT(DISTINCT CASE
                        WHEN files.deleted_at IS NULL
                         AND NOT EXISTS (
                            SELECT 1
                            FROM file_sources AS other_sources
                            WHERE other_sources.file_id = file_sources.file_id
                              AND other_sources.source_id != sources.id
                         )
                        THEN file_sources.file_id
                    END) AS exclusive_active_file_count
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
    return shared_month_key_for_item(target, item)


def month_key_from_stored_path(path: str) -> str | None:
    return shared_month_key_from_stored_path(path)


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
    if source.reference_suggestions_person_name is not None:
        attach_face_database(conn, target, face_config)
    if source.missing_face_suggestions:
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
    from .face import face_db_path
    from .server_faces import current_face_db_path

    current_face_db_path(target, face_config)
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


def last_month_before_year(keys: list[str], year: str) -> str | None:
    return next((key for key in reversed(keys) if key[:4] < year), None)


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
