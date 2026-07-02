from __future__ import annotations

# ruff: noqa: F401
# This module intentionally re-exports database helpers from the split db_* modules.

import sqlite3
from pathlib import Path
from typing import Iterable

from .db_core import (
    COLLECTION_ID_META_KEY,
    DB_FILENAME,
    absolute_target_path,
    db_path_for_target,
    ensure_column,
    execute_sql_statements,
    find_target,
    get_meta,
    log_command,
    path_key,
    relative_path,
    relative_path_key,
    set_collection_id,
    set_meta,
    table_columns,
    table_exists,
    target_relative_path,
    target_relative_path_key,
)
from .db_schema import (
    SCHEMA_VERSION,
    GPS_ERROR_EXIFTOOL,
    GPS_ERROR_FILE_MISSING,
    VIDEO_EXTENSIONS,
    DATE_GLOB_SQL,
    MANUAL_DATE_MIDPOINT_SQL,
    BROWSER_DATE_ORDER_SQL,
    H3_FILE_COLUMNS,
    H3_FILE_COLUMN_SET,
    H3_FILE_COLUMNS_SQL,
    PERFORMANCE_INDEX_NAMES,
    h3_file_column_definitions_sql,
    h3_file_index_sql,
    SchemaMigrationRequired,
    MigrationPlan,
    connect,
    prepare_database,
    schema_version,
    require_current_schema,
    ensure_compatible_columns,
    ensure_performance_indexes,
    drop_performance_indexes,
    init_database,
    apply_schema,
    create_geo_place_names_schema,
    create_geo_places_schema,
    create_tags_schema,
    seed_system_tags,
    create_file_sources_schema,
    create_pending_file_deletes_schema,
    create_pending_file_moves_schema,
    migration_plan,
    backup_database,
    migrate_database,
    validate_pre_migration,
    validate_existing_file_source_references,
    source_name_is_not_null,
    validate_current_schema,
    validate_pending_file_deletes_schema,
    validate_pending_file_moves_schema,
    validate_tags_schema,
    validate_tags_schema_structure,
    validate_collection_id,
    current_schema_internal_repairs,
    repair_current_schema_internal_structure,
    repair_pending_file_deletes_schema,
    repair_pending_file_moves_schema,
    repair_tags_schema,
    validate_database_health,
    validate_performance_indexes,
    validate_no_duplicate_active_sha256,
    missing_performance_indexes,
    validate_relative_target_paths,
    validate_legacy_file_sources_schema,
    validate_file_sources_schema,
    backfill_file_sources,
    rebuild_files_without_legacy_source_columns,
    rebuild_errors_without_source_fk,
    default_source_name,
    unique_source_name,
    rebuild_sources_without_kind,
    rebuild_file_sources_without_kind,
    count_rows,
    has_legacy_gps_errors,
    cleanup_legacy_gps_errors,
    needs_h3_10_11_backfill,
    backfill_h3_10_11,
)
from .db_tags import (
    SYSTEM_TAG_NAMES,
    SYSTEM_TAG_OUT_OF_FOCUS,
    TAG_KIND_SYSTEM,
    TAG_KIND_USER,
    create_user_tag,
    delete_user_tag,
    ensure_tag,
    is_system_tag_name,
    normalize_tag_name,
    rename_user_tag,
    set_file_tag,
    tag_file,
    tag_kind_for_name,
    tag_name_key,
    tagged_files,
    tags,
    tags_for_file,
    untag_file,
)
from .db_files import (
    UnimportPlan,
    active_file_integrity_rows,
    apply_unimport,
    build_unimport_plan,
    conflict_candidate_files,
    deleted_files,
    duplicate_active_sha256_files,
    duplicate_source_count,
    file_by_target_path,
    file_sources_by_target_path,
    file_target_path_keys,
    files_by_hash,
    files_by_original_filename,
    get_file_source_for_source_path,
    insert_duplicate,
    insert_imported_file,
    insert_or_validate_file_source,
    metadata_refresh_files,
    name_conflicts,
    non_metadata_files,
    source_file_sources,
    status_counts,
    unimport_total_sources_by_file_id,
)
from .db_geo import (
    custom_geo_place,
    custom_geo_place_cells,
    custom_geo_place_cells_by_slug,
    custom_geo_places,
    delete_custom_geo_place,
    geo_area_files,
    geo_areas,
    geo_child_areas,
    geo_missing_files,
    geo_place_count,
    geo_place_files,
    geo_place_name,
    geo_place_names,
    geo_place_where_clause,
    geo_scan_files,
    geo_stats,
    remove_file_manual_h3_location,
    rename_custom_geo_place,
    set_custom_geo_place,
    set_file_manual_h3_location,
    set_geo_place_name,
    update_file_gps,
    validate_h3_column,
)
from .db_lifecycle import (
    abort_pending_file_move,
    clean_manual_date_note,
    clear_manual_date,
    complete_pending_file_move,
    create_pending_file_move,
    fail_pending_file_move,
    mark_file_deleted,
    mark_file_undeleted,
    prepared_pending_file_moves,
    set_manual_date,
    update_file_placement,
)
from .db_sources import (
    Source,
    add_named_source,
    find_source_by_name,
    get_source,
    get_sources,
    mark_source_error,
    mark_source_imported,
    row_to_source,
)
from .value_parsing import optional_int, require_int


def insert_error(
    conn: sqlite3.Connection,
    *,
    source_id: int | None,
    source_path: Path | None,
    stage: str,
    message: str,
) -> None:
    conn.execute(
        "INSERT INTO errors(source_id, source_path, stage, message) VALUES(?, ?, ?, ?)",
        (source_id, str(source_path) if source_path else None, stage, message),
    )


def resolve_errors_for_path(conn: sqlite3.Connection, *, stage: str, source_path: Path) -> None:
    conn.execute(
        """
        UPDATE errors
        SET resolved_at = CURRENT_TIMESTAMP
        WHERE stage = ?
          AND source_path = ?
          AND resolved_at IS NULL
        """,
        (stage, str(source_path)),
    )


def error_count(conn: sqlite3.Connection, *, include_resolved: bool = False) -> int:
    sql = "SELECT COUNT(*) FROM errors"
    if not include_resolved:
        sql += " WHERE resolved_at IS NULL"
    return int(conn.execute(sql).fetchone()[0])


def update_file_camera(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    camera_make: str | None,
    camera_model: str | None,
) -> None:
    conn.execute(
        """
        UPDATE files
        SET camera_make = ?,
            camera_model = ?
        WHERE id = ?
        """,
        (camera_make, camera_model, file_id),
    )


def normalize_view_rotation(value: object) -> int:
    try:
        rotation = require_int(value or 0, "visningsrotasjon") % 360
    except ValueError:
        rotation = 0
    return rotation if rotation in {0, 90, 180, 270} else 0


def rotate_file_view(conn: sqlite3.Connection, file_id: int, direction: str) -> int:
    delta_by_direction = {"left": -90, "right": 90}
    if direction not in delta_by_direction:
        raise ValueError("Ugyldig rotasjonsretning.")
    row = conn.execute(
        """
        SELECT view_rotation_degrees
        FROM files
        WHERE id = ?
          AND deleted_at IS NULL
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Filen finnes ikke i bildesamlingen.")
    rotation = (normalize_view_rotation(row["view_rotation_degrees"]) + delta_by_direction[direction]) % 360
    conn.execute(
        """
        UPDATE files
        SET view_rotation_degrees = ?
        WHERE id = ?
        """,
        (rotation, file_id),
    )
    return rotation


def errors(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    stage: str | None = None,
    include_resolved: bool = False,
) -> Iterable[sqlite3.Row]:
    params: list[object] = []
    sql = "SELECT id, created_at, resolved_at, stage, source_path, message FROM errors"
    clauses: list[str] = []
    if stage is not None:
        clauses.append("stage = ?")
        params.append(stage)
    if not include_resolved:
        clauses.append("resolved_at IS NULL")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params)
