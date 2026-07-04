from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import db
from .media import media_kind


RAW_SIDECAR_IDS_CACHE_MAX_SIZE = 8
SidecarCacheKey = tuple[str, tuple[object, ...]]
SIDECAR_SIGNATURE_CACHE: dict[tuple[str, int], tuple[object, ...]] = {}
RAW_SIDECAR_IDS_BY_IMAGE_ID_CACHE: dict[SidecarCacheKey, dict[int, int]] = {}
RAW_SIDECAR_IMAGE_IDS_BY_SIDECAR_ID_CACHE: dict[SidecarCacheKey, dict[int, int]] = {}
RAW_SIDECAR_EXTENSIONS = {".nef", ".psd"}
RAW_SIDECAR_SQL_EXTENSION_FILTER = """
              lower(files.original_filename) LIKE '%.nef'
              OR lower(files.original_filename) LIKE '%.psd'
"""
RAW_SIDECAR_IMAGE_EXTENSIONS = {".jpg", ".jpeg"}
RAW_SIDECAR_GROUP_EXTENSIONS = RAW_SIDECAR_EXTENSIONS | RAW_SIDECAR_IMAGE_EXTENSIONS
RAW_SIDECAR_STEM_FALLBACK_EXTENSIONS = {".psd"} | RAW_SIDECAR_IMAGE_EXTENSIONS
FILE_COLUMNS = (
    "id, target_path, target_path_key, original_filename, stored_filename, taken_date, date_source, "
    "metadata_datetime, "
    "manual_date_from, manual_date_to, manual_date_note, "
    "camera_make, camera_model, "
    "size_bytes, view_rotation_degrees, gps_lat, gps_lon, gps_source, "
    "media_width, media_height, media_orientation, media_metadata_mtime_ns, "
    f"{db.H3_FILE_COLUMNS_SQL}"
)
ITEM_DATE_ORDER_SQL = db.BROWSER_DATE_ORDER_SQL
ITEM_ORDER_SQL = f"{ITEM_DATE_ORDER_SQL}, target_path_key"


def clear_sidecar_data_caches() -> None:
    cached_motion_video_file_ids.cache_clear()
    cached_raw_sidecar_ids_by_image_id.cache_clear()
    SIDECAR_SIGNATURE_CACHE.clear()
    RAW_SIDECAR_IDS_BY_IMAGE_ID_CACHE.clear()
    RAW_SIDECAR_IMAGE_IDS_BY_SIDECAR_ID_CACHE.clear()


def is_image_item(item: Any) -> bool:
    target_path = Path(str(item["target_path"]))
    return media_kind(target_path) == "image"


def hidden_sidecar_file_ids_for_day(conn: sqlite3.Connection, day_key: str) -> set[int]:
    return query_motion_video_file_ids_for_day(conn, day_key) | query_raw_sidecar_file_ids_for_day(conn, day_key)


def sidecar_cache_key(target: Path, *, conn: sqlite3.Connection | None = None) -> SidecarCacheKey:
    return (str(target.resolve()), sidecar_cache_signature(target, conn=conn))


def sidecar_cache_signature(target: Path, *, conn: sqlite3.Connection | None = None) -> tuple[object, ...]:
    db_path = db.db_path_for_target(target)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    cache_mtime_key = (str(target.resolve()), mtime_ns)
    cached = SIDECAR_SIGNATURE_CACHE.get(cache_mtime_key)
    if cached is not None:
        return cached
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        files_row = conn.execute(
            """
            SELECT
                COUNT(*) AS row_count,
                COALESCE(MAX(id), 0) AS max_id,
                COALESCE(MAX(target_path_key), '') AS max_target_path_key,
                COALESCE(MAX(original_filename), '') AS max_original_filename,
                COALESCE(MAX(stored_filename), '') AS max_stored_filename,
                COALESCE(MAX(date_source), '') AS max_date_source,
                COALESCE(MAX(metadata_datetime), '') AS max_metadata_datetime,
                COALESCE(MAX(deleted_at), '') AS max_deleted_at,
                COALESCE(MAX(media_metadata_mtime_ns), 0) AS max_media_metadata_mtime_ns
            FROM files
            """
        ).fetchone()
        sources_row = conn.execute(
            """
            SELECT
                COUNT(*) AS row_count,
                COALESCE(MAX(id), 0) AS max_id,
                COALESCE(MAX(file_id), 0) AS max_file_id,
                COALESCE(MAX(source_id), 0) AS max_source_id,
                COALESCE(MAX(source_path_key), '') AS max_source_path_key
            FROM file_sources
            """
        ).fetchone()
        signature = (
            int(files_row["row_count"]),
            int(files_row["max_id"]),
            str(files_row["max_target_path_key"]),
            str(files_row["max_original_filename"]),
            str(files_row["max_stored_filename"]),
            str(files_row["max_date_source"]),
            str(files_row["max_metadata_datetime"]),
            str(files_row["max_deleted_at"]),
            int(files_row["max_media_metadata_mtime_ns"]),
            int(sources_row["row_count"]),
            int(sources_row["max_id"]),
            int(sources_row["max_file_id"]),
            int(sources_row["max_source_id"]),
            str(sources_row["max_source_path_key"]),
        )
        if len(SIDECAR_SIGNATURE_CACHE) >= RAW_SIDECAR_IDS_CACHE_MAX_SIZE:
            SIDECAR_SIGNATURE_CACHE.pop(next(iter(SIDECAR_SIGNATURE_CACHE)))
        SIDECAR_SIGNATURE_CACHE[cache_mtime_key] = signature
        return signature
    finally:
        if owned_conn:
            conn.close()


def query_motion_video_file_ids_for_day(conn: sqlite3.Connection, day_key: str) -> set[int]:
    image_originals = {
        str(row["original_filename"]).casefold()
        for row in conn.execute(
            f"""
            SELECT original_filename
            FROM files
            WHERE deleted_at IS NULL
              AND {db.BROWSER_DATE_ORDER_SQL} = ?
              AND lower(original_filename) LIKE '%.mp.jpg'
            """,
            (day_key,),
        )
    }
    rows = conn.execute(
        f"""
        SELECT id, original_filename
        FROM files
        WHERE deleted_at IS NULL
          AND {db.BROWSER_DATE_ORDER_SQL} = ?
          AND lower(original_filename) LIKE '%.mp'
          AND lower(stored_filename) LIKE '%.mp4'
        """,
        (day_key,),
    )
    return {
        int(row["id"])
        for row in rows
        if f"{str(row['original_filename'])}.jpg".casefold() in image_originals
    }


def query_raw_sidecar_file_ids_for_day(conn: sqlite3.Connection, day_key: str) -> set[int]:
    groups: dict[tuple[int, str, str, str], tuple[set[int], set[int]]] = {}
    for row in conn.execute(
        f"""
        SELECT
            files.id,
            files.original_filename,
            files.date_source,
            files.metadata_datetime,
            file_sources.source_id,
            file_sources.source_path_key
        FROM files
        JOIN file_sources ON file_sources.file_id = files.id
        WHERE files.deleted_at IS NULL
          AND {db.BROWSER_DATE_ORDER_SQL} = ?
          AND (
{RAW_SIDECAR_SQL_EXTENSION_FILTER}
              OR lower(files.original_filename) LIKE '%.jpg'
              OR lower(files.original_filename) LIKE '%.jpeg'
          )
        """,
        (day_key,),
    ):
        original_filename = str(row["original_filename"])
        suffix = Path(original_filename).suffix.casefold()
        if suffix not in RAW_SIDECAR_GROUP_EXTENSIONS:
            continue
        for key in raw_sidecar_group_keys(row, original_filename, suffix):
            raw_ids, image_ids = groups.setdefault(key, (set(), set()))
            if suffix in RAW_SIDECAR_EXTENSIONS:
                raw_ids.add(int(row["id"]))
            else:
                image_ids.add(int(row["id"]))
    return {next(iter(raw_ids)) for raw_ids, image_ids in groups.values() if len(raw_ids) == 1 and len(image_ids) == 1}


def motion_video_file_ids(target: Path, *, conn: sqlite3.Connection | None = None) -> set[int]:
    cache_key = sidecar_cache_key(target, conn=conn)
    return set(cached_motion_video_file_ids(*cache_key))


@lru_cache(maxsize=8)
def cached_motion_video_file_ids(target_path: str, sidecar_signature: tuple[object, ...]) -> tuple[int, ...]:
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


def raw_sidecar_file_ids(target: Path, *, conn: sqlite3.Connection | None = None) -> set[int]:
    return set(raw_sidecar_ids_by_image_id(target, conn=conn).values())


def query_raw_sidecar_file_ids(conn: sqlite3.Connection) -> tuple[int, ...]:
    groups = raw_sidecar_groups(conn)
    return tuple(sorted(next(iter(raw_ids)) for raw_ids, image_ids in groups.values() if len(raw_ids) == 1 and len(image_ids) == 1))


def raw_sidecar_id_by_image_id(
    target: Path,
    image_id: int,
    *,
    conn: sqlite3.Connection | None = None,
) -> int | None:
    return raw_sidecar_ids_by_image_id(target, conn=conn).get(image_id)


def raw_sidecar_image_id_by_sidecar_id(
    target: Path,
    sidecar_id: int,
    *,
    conn: sqlite3.Connection | None = None,
) -> int | None:
    cache_key = sidecar_cache_key(target, conn=conn)
    cached = RAW_SIDECAR_IMAGE_IDS_BY_SIDECAR_ID_CACHE.get(cache_key)
    if cached is not None:
        return cached.get(sidecar_id)
    cached = {sidecar_id: image_id for image_id, sidecar_id in raw_sidecar_ids_by_image_id(target, conn=conn).items()}
    if len(RAW_SIDECAR_IMAGE_IDS_BY_SIDECAR_ID_CACHE) >= RAW_SIDECAR_IDS_CACHE_MAX_SIZE:
        RAW_SIDECAR_IMAGE_IDS_BY_SIDECAR_ID_CACHE.pop(next(iter(RAW_SIDECAR_IMAGE_IDS_BY_SIDECAR_ID_CACHE)))
    RAW_SIDECAR_IMAGE_IDS_BY_SIDECAR_ID_CACHE[cache_key] = cached
    return cached.get(sidecar_id)


def raw_sidecar_ids_by_image_id(
    target: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[int, int]:
    cache_key = sidecar_cache_key(target, conn=conn)
    cached = RAW_SIDECAR_IDS_BY_IMAGE_ID_CACHE.get(cache_key)
    if cached is not None:
        return cached
    cached = cached_raw_sidecar_ids_by_image_id(*cache_key)
    if len(RAW_SIDECAR_IDS_BY_IMAGE_ID_CACHE) >= RAW_SIDECAR_IDS_CACHE_MAX_SIZE:
        RAW_SIDECAR_IDS_BY_IMAGE_ID_CACHE.pop(next(iter(RAW_SIDECAR_IDS_BY_IMAGE_ID_CACHE)))
    RAW_SIDECAR_IDS_BY_IMAGE_ID_CACHE[cache_key] = cached
    return cached


@lru_cache(maxsize=8)
def cached_raw_sidecar_ids_by_image_id(target_path: str, sidecar_signature: tuple[object, ...]) -> dict[int, int]:
    target = Path(target_path)
    conn = db.connect(target)
    try:
        return query_raw_sidecar_ids_by_image_id(conn)
    finally:
        conn.close()


def query_raw_sidecar_ids_by_image_id(conn: sqlite3.Connection) -> dict[int, int]:
    pairs: dict[int, int] = {}
    for raw_ids, image_ids in raw_sidecar_groups(conn).values():
        if len(raw_ids) != 1 or len(image_ids) != 1:
            continue
        image_id = next(iter(image_ids))
        if image_id not in pairs:
            pairs[image_id] = next(iter(raw_ids))
    return pairs


def raw_sidecar_groups(conn: sqlite3.Connection) -> dict[tuple[int, str, str, str], tuple[set[int], set[int]]]:
    groups: dict[tuple[int, str, str, str], tuple[set[int], set[int]]] = {}
    for row in conn.execute(
        f"""
        SELECT
            files.id,
            files.original_filename,
            files.date_source,
            files.metadata_datetime,
            file_sources.source_id,
            file_sources.source_path_key
        FROM files
        JOIN file_sources ON file_sources.file_id = files.id
        WHERE files.deleted_at IS NULL
          AND (
{RAW_SIDECAR_SQL_EXTENSION_FILTER}
              OR lower(files.original_filename) LIKE '%.jpg'
              OR lower(files.original_filename) LIKE '%.jpeg'
          )
        """
    ):
        original_filename = str(row["original_filename"])
        suffix = Path(original_filename).suffix.casefold()
        if suffix not in RAW_SIDECAR_GROUP_EXTENSIONS:
            continue
        for key in raw_sidecar_group_keys(row, original_filename, suffix):
            raw_ids, image_ids = groups.setdefault(key, (set(), set()))
            if suffix in RAW_SIDECAR_EXTENSIONS:
                raw_ids.add(int(row["id"]))
            else:
                image_ids.add(int(row["id"]))
    return groups


def raw_sidecar_group_keys(row: Any, original_filename: str, suffix: str) -> tuple[tuple[int, str, str, str], ...]:
    base_key = (
        int(row["source_id"]),
        source_parent_path_key(str(row["source_path_key"])),
        Path(original_filename).stem.casefold(),
    )
    keys: list[tuple[int, str, str, str]] = []
    metadata_datetime = row["metadata_datetime"]
    if row["date_source"] == "metadata" and metadata_datetime is not None:
        keys.append((*base_key, f"metadata:{metadata_datetime}"))
    if suffix in RAW_SIDECAR_STEM_FALLBACK_EXTENSIONS:
        keys.append((*base_key, "stem"))
    return tuple(keys)


def source_parent_path_key(source_path_key: str) -> str:
    normalized = source_path_key.replace("\\", "/").rstrip("/")
    if "/" not in normalized:
        return ""
    return normalized.rsplit("/", 1)[0].casefold()
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


def raw_sidecar_for_image(target: Path, item: Any, *, conn: sqlite3.Connection | None = None) -> Any | None:
    if not is_image_item(item):
        return None
    try:
        image_id = int(item["id"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        raw_id = raw_sidecar_id_by_image_id(target, image_id, conn=conn)
        if raw_id is None:
            return None
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND id = ?
            """,
            (raw_id,),
        ).fetchone()
    finally:
        if owned_conn:
            conn.close()


def motion_video_main_image(target: Path, item: Any, *, conn: sqlite3.Connection | None = None) -> Any | None:
    original_filename = original_filename_for_item(target, item, conn=conn)
    if original_filename is None:
        return None
    if not original_filename.casefold().endswith(".mp"):
        return None
    try:
        stored_filename = str(item["stored_filename"])
    except (KeyError, IndexError):
        stored_filename = ""
    if not stored_filename.casefold().endswith(".mp4"):
        return None
    image_original_filename = f"{original_filename}.jpg"
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND lower(original_filename) = lower(?)
              AND lower(original_filename) LIKE '%.mp.jpg'
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            (image_original_filename,),
        ).fetchone()
    finally:
        if owned_conn:
            conn.close()


def raw_sidecar_main_image(target: Path, item: Any, *, conn: sqlite3.Connection | None = None) -> Any | None:
    try:
        raw_id = int(item["id"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    try:
        original_filename = str(item["original_filename"])
    except (KeyError, IndexError):
        original_filename = original_filename_for_item(target, item, conn=conn) or ""
    if Path(original_filename).suffix.casefold() not in RAW_SIDECAR_EXTENSIONS:
        return None
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        image_id = raw_sidecar_image_id_by_sidecar_id(target, raw_id, conn=conn)
        if image_id is None:
            return None
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND id = ?
            """,
            (image_id,),
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
def hidden_sidecar_main_image(target: Path, item: Any, *, conn: sqlite3.Connection | None = None) -> Any | None:
    return motion_video_main_image(target, item, conn=conn) or raw_sidecar_main_image(target, item, conn=conn)
