from __future__ import annotations

import re
import sqlite3
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from pathlib import PureWindowsPath

from . import db_core
from .db_core import (
    COLLECTION_ID_META_KEY,
    DB_FILENAME,
    db_path_for_target,
    ensure_column,
    execute_sql_statements,
    get_meta,
    log_command,
    path_key,
    set_collection_id,
    set_meta,
    table_columns,
    table_exists,
)
from .db_tags import (
    SYSTEM_TAG_NAMES,
    TAG_KIND_SYSTEM,
    normalize_tag_name,
    tag_kind_for_name,
    tag_name_key,
)

SCHEMA_VERSION = 14
GPS_ERROR_EXIFTOOL = "exiftool_error"
GPS_ERROR_FILE_MISSING = "file_missing"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".m4v", ".mpg", ".mpeg", ".mts", ".m2ts", ".3gp", ".wmv"}
DATE_GLOB_SQL = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
MANUAL_DATE_MIDPOINT_SQL = "date((julianday(manual_date_from) + julianday(manual_date_to)) / 2.0)"
BROWSER_DATE_ORDER_SQL = (
    f"CASE WHEN manual_date_from GLOB {DATE_GLOB_SQL} AND manual_date_to GLOB {DATE_GLOB_SQL} "
    f"THEN {MANUAL_DATE_MIDPOINT_SQL} "
    f"WHEN taken_date GLOB {DATE_GLOB_SQL} "
    "THEN taken_date ELSE '9999-99-99' END"
)
H3_FILE_COLUMNS = tuple(f"h3_res{resolution}" for resolution in range(12))
H3_FILE_COLUMN_SET = set(H3_FILE_COLUMNS)
H3_FILE_COLUMNS_SQL = ", ".join(H3_FILE_COLUMNS)
PERFORMANCE_INDEX_NAMES = (
    "idx_files_active_browser_order",
    "idx_files_active_date_source_order",
    "idx_files_active_target_path_key",
    "idx_files_sha256_active_unique",
    *(f"idx_files_{column}" for column in H3_FILE_COLUMNS),
    *(f"idx_files_{column}_browser_order" for column in H3_FILE_COLUMNS),
    "idx_files_gps",
    "idx_file_sources_source_id_id",
    "idx_file_sources_source_id_file_id",
    "idx_errors_unresolved_stage_id",
)


def h3_file_column_definitions_sql() -> str:
    return ",\n            ".join(f"{column} TEXT" for column in H3_FILE_COLUMNS)


def h3_file_index_sql() -> str:
    return "\n\n        ".join(
        f"""CREATE INDEX IF NOT EXISTS idx_files_{column}
        ON files({column})
        WHERE {column} IS NOT NULL AND deleted_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_files_{column}_browser_order
        ON files({column}, {BROWSER_DATE_ORDER_SQL}, target_path_key)
        WHERE {column} IS NOT NULL AND deleted_at IS NULL;"""
        for column in H3_FILE_COLUMNS
    )


class SchemaMigrationRequired(ValueError):
    pass


@dataclass(frozen=True)
class MigrationPlan:
    current_version: int
    target_version: int
    imported_files: int
    duplicate_findings: int
    creates_file_sources: bool
    backfills_h3_10_11: bool = False
    rebuilds_files_without_legacy_source_columns: bool = False
    drops_duplicate_findings: bool = False
    rebuilds_errors_without_source_fk: bool = False
    rebuilds_sources_without_kind: bool = False
    rebuilds_file_sources_without_kind: bool = False
    cleans_gps_errors: bool = False
    refreshes_performance_indexes: bool = False
    adds_camera_columns: bool = False
    creates_pending_file_deletes: bool = False
    creates_pending_file_moves: bool = False
    adds_metadata_datetime_column: bool = False
    removes_superseded_sources: bool = False
    internal_repairs: tuple[str, ...] = ()


def connect(target: Path, *, require_current: bool = True) -> sqlite3.Connection:
    return db_core.connect(
        target,
        require_current=require_current,
        schema_validator=require_current_schema,
    )


def prepare_database(target: Path) -> None:
    db_core.prepare_database(target, schema_validator=require_current_schema)


def schema_version(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "meta"):
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is not None:
        try:
            return int(row["value"])
        except ValueError as exc:
            raise ValueError(f"Ugyldig schema_version i databasen: {row['value']}") from exc
    if table_exists(conn, "files"):
        return 1
    return 0


def require_current_schema(conn: sqlite3.Connection, *, full: bool = True) -> None:
    version = schema_version(conn)
    if version == SCHEMA_VERSION:
        if full:
            try:
                validate_current_schema(conn)
            except ValueError as exc:
                raise SchemaMigrationRequired(
                    f"Databasen har schema_version={SCHEMA_VERSION}, men mangler forventet v{SCHEMA_VERSION}-struktur.\n"
                    f"{exc}\n"
                    "Kjør bildebank migrate."
                ) from exc
        return
    if version < SCHEMA_VERSION:
        raise SchemaMigrationRequired(
            f"Databasen bruker et eldre format (schema_version={version}).\n"
            f"Denne versjonen av bildebank krever schema_version={SCHEMA_VERSION}.\n"
            "Kjør:\n"
            "  bildebank migrate\n"
            "Ingen endringer er gjort."
        )
    raise ValueError(
        f"Databasen bruker et nyere format (schema_version={version}) enn programmet støtter "
        f"(schema_version={SCHEMA_VERSION})."
    )


def ensure_compatible_columns(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS command_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command TEXT NOT NULL,
            args_json TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    create_geo_place_names_schema(conn)
    create_geo_places_schema(conn)
    create_tags_schema(conn)
    create_pending_file_deletes_schema(conn)
    create_pending_file_moves_schema(conn)
    if table_exists(conn, "errors"):
        ensure_column(conn, "errors", "resolved_at", "TEXT")
    if table_exists(conn, "files"):
        ensure_column(conn, "files", "deleted_at", "TEXT")
        ensure_column(conn, "files", "deleted_original_target_path", "TEXT")
        ensure_column(conn, "files", "media_width", "INTEGER")
        ensure_column(conn, "files", "media_height", "INTEGER")
        ensure_column(conn, "files", "media_orientation", "INTEGER")
        ensure_column(conn, "files", "media_metadata_mtime_ns", "INTEGER")
        ensure_column(conn, "files", "view_rotation_degrees", "INTEGER")
        ensure_column(conn, "files", "camera_make", "TEXT")
        ensure_column(conn, "files", "camera_model", "TEXT")
        ensure_column(conn, "files", "metadata_datetime", "TEXT")
        ensure_column(conn, "files", "manual_date_from", "TEXT")
        ensure_column(conn, "files", "manual_date_to", "TEXT")
        ensure_column(conn, "files", "manual_date_note", "TEXT")
        ensure_column(conn, "files", "gps_lat", "REAL")
        ensure_column(conn, "files", "gps_lon", "REAL")
        ensure_column(conn, "files", "gps_alt", "REAL")
        for column in H3_FILE_COLUMNS:
            ensure_column(conn, "files", column, "TEXT")
        ensure_column(conn, "files", "gps_source", "TEXT")
        ensure_column(conn, "files", "gps_scanned_at", "TEXT")
        ensure_column(conn, "files", "gps_error", "TEXT")

def ensure_performance_indexes(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "files"):
        validate_no_duplicate_active_sha256(conn)
        execute_sql_statements(
            conn,
            f"""
        CREATE INDEX IF NOT EXISTS idx_files_active_browser_order
        ON files ({BROWSER_DATE_ORDER_SQL}, target_path_key)
        WHERE deleted_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_files_active_date_source_order
        ON files (date_source, {BROWSER_DATE_ORDER_SQL}, target_path_key)
        WHERE deleted_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_files_active_target_path_key
        ON files(target_path_key)
        WHERE deleted_at IS NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS idx_files_sha256_active_unique
        ON files(sha256)
        WHERE deleted_at IS NULL;

        {h3_file_index_sql()}

        CREATE INDEX IF NOT EXISTS idx_files_gps
        ON files(gps_lat, gps_lon)
        WHERE gps_lat IS NOT NULL AND gps_lon IS NOT NULL AND deleted_at IS NULL;
        """
        )
    if table_exists(conn, "file_sources"):
        execute_sql_statements(
            conn,
            """
        CREATE INDEX IF NOT EXISTS idx_file_sources_source_id_id
        ON file_sources(source_id, id);

        CREATE INDEX IF NOT EXISTS idx_file_sources_source_id_file_id
        ON file_sources(source_id, file_id);
        """
        )
    if table_exists(conn, "errors"):
        execute_sql_statements(
            conn,
            """
        CREATE INDEX IF NOT EXISTS idx_errors_unresolved_stage_id
        ON errors(stage, id DESC)
        WHERE resolved_at IS NULL;
        """
        )


def drop_performance_indexes(conn: sqlite3.Connection) -> None:
    for name in PERFORMANCE_INDEX_NAMES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")


def init_database(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path_for_target(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        apply_schema(conn)
        set_meta(conn, "target_path", str(target.resolve()))
        set_collection_id(conn)
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        conn.commit()
    finally:
        conn.close()


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS command_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command TEXT NOT NULL,
            args_json TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            path_key TEXT,
            name TEXT NOT NULL UNIQUE,
            added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            imported_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_path TEXT NOT NULL,
            target_path_key TEXT NOT NULL UNIQUE,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            taken_date TEXT,
            date_source TEXT NOT NULL,
            name_conflict INTEGER NOT NULL DEFAULT 0,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TEXT,
            deleted_original_target_path TEXT,
            media_width INTEGER,
            media_height INTEGER,
            media_orientation INTEGER,
            media_metadata_mtime_ns INTEGER,
            view_rotation_degrees INTEGER,
            camera_make TEXT,
            camera_model TEXT,
            metadata_datetime TEXT,
            manual_date_from TEXT,
            manual_date_to TEXT,
            manual_date_note TEXT,
            gps_lat REAL,
            gps_lon REAL,
            gps_alt REAL,
            {h3_file_column_definitions_sql()},
            gps_source TEXT,
            gps_scanned_at TEXT,
            gps_error TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_files_sha256_active_unique
        ON files(sha256)
        WHERE deleted_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_files_active_browser_order
        ON files ({BROWSER_DATE_ORDER_SQL}, target_path_key)
        WHERE deleted_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_files_active_date_source_order
        ON files (date_source, {BROWSER_DATE_ORDER_SQL}, target_path_key)
        WHERE deleted_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_files_active_target_path_key
        ON files(target_path_key)
        WHERE deleted_at IS NULL;

        {h3_file_index_sql()}

        CREATE INDEX IF NOT EXISTS idx_files_gps
        ON files(gps_lat, gps_lon)
        WHERE gps_lat IS NOT NULL AND gps_lon IS NOT NULL AND deleted_at IS NULL;

        CREATE TABLE IF NOT EXISTS geo_place_names (
            h3_cell TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS geo_places (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS geo_place_cells (
            slug TEXT NOT NULL REFERENCES geo_places(slug) ON DELETE CASCADE,
            h3_cell TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY(slug, h3_cell)
        );

        CREATE INDEX IF NOT EXISTS idx_geo_place_cells_slug_position
        ON geo_place_cells(slug, position);

        CREATE TABLE IF NOT EXISTS file_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL REFERENCES files(id),
            source_id INTEGER NOT NULL REFERENCES sources(id),
            source_path TEXT NOT NULL,
            source_path_key TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_id, source_path_key)
        );

        CREATE INDEX IF NOT EXISTS idx_file_sources_file_id ON file_sources(file_id);
        CREATE INDEX IF NOT EXISTS idx_file_sources_sha256 ON file_sources(sha256);
        CREATE INDEX IF NOT EXISTS idx_file_sources_source_id_id ON file_sources(source_id, id);
        CREATE INDEX IF NOT EXISTS idx_file_sources_source_id_file_id ON file_sources(source_id, file_id);

        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER,
            source_path TEXT,
            stage TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_errors_unresolved_stage_id
        ON errors(stage, id DESC)
        WHERE resolved_at IS NULL;

        CREATE TABLE IF NOT EXISTS pending_file_deletes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            source_id INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pending_file_moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            from_path TEXT NOT NULL,
            to_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            operation TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            last_error TEXT
        );
        """
    )
    ensure_compatible_columns(conn)


def create_geo_place_names_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geo_place_names (
            h3_cell TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def create_geo_places_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS geo_places (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS geo_place_cells (
            slug TEXT NOT NULL REFERENCES geo_places(slug) ON DELETE CASCADE,
            h3_cell TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY(slug, h3_cell)
        );

        CREATE INDEX IF NOT EXISTS idx_geo_place_cells_slug_position
        ON geo_place_cells(slug, position);
        """
    )


def create_tags_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS file_tags (
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(file_id, tag_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_file_tags_tag_id_file_id
        ON file_tags(tag_id, file_id)
        """
    )
    ensure_column(conn, "tags", "kind", "TEXT NOT NULL DEFAULT 'user'")
    seed_system_tags(conn)


def seed_system_tags(conn: sqlite3.Connection) -> None:
    for name in SYSTEM_TAG_NAMES:
        clean_name = normalize_tag_name(name)
        name_key = clean_name.casefold()
        conn.execute(
            """
            INSERT INTO tags(name, name_key, kind)
            VALUES(?, ?, ?)
            ON CONFLICT(name_key) DO UPDATE SET
                name = excluded.name,
                kind = ?
            """,
            (clean_name, name_key, TAG_KIND_SYSTEM, TAG_KIND_SYSTEM),
        )


def create_file_sources_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS file_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL REFERENCES files(id),
            source_id INTEGER NOT NULL REFERENCES sources(id),
            source_path TEXT NOT NULL,
            source_path_key TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_id, source_path_key)
        );

        CREATE INDEX IF NOT EXISTS idx_file_sources_file_id ON file_sources(file_id);
        CREATE INDEX IF NOT EXISTS idx_file_sources_sha256 ON file_sources(sha256);
        CREATE INDEX IF NOT EXISTS idx_file_sources_source_id_id ON file_sources(source_id, id);
        CREATE INDEX IF NOT EXISTS idx_file_sources_source_id_file_id ON file_sources(source_id, file_id);
        """
    )


def create_pending_file_deletes_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_file_deletes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            source_id INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def create_pending_file_moves_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_file_moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            from_path TEXT NOT NULL,
            to_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            operation TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            last_error TEXT
        )
        """
    )


def migration_plan(target: Path, *, validate: bool = True) -> MigrationPlan:
    conn = connect(target, require_current=False)
    try:
        version = schema_version(conn)
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"Databasen bruker et nyere format (schema_version={version}) enn programmet støtter."
            )
        if version == SCHEMA_VERSION:
            if validate:
                validate_current_schema(
                    conn,
                    require_performance_indexes=False,
                    require_pending_file_deletes=False,
                    require_pending_file_moves=False,
                    require_internal_structure=False,
                )
            internal_repairs = current_schema_internal_repairs(conn)
            return MigrationPlan(
                current_version=version,
                target_version=SCHEMA_VERSION,
                imported_files=count_rows(conn, "files"),
                duplicate_findings=count_rows(conn, "duplicate_findings"),
                creates_file_sources=False,
                refreshes_performance_indexes=bool(missing_performance_indexes(conn)),
                internal_repairs=internal_repairs,
            )
        if version in {5, 6, 7, 8, 9, 10, 11, 12, 13}:
            if validate:
                validate_current_schema(
                    conn,
                    require_performance_indexes=False,
                    require_manual_date_columns=version >= 9,
                    require_camera_columns=version >= 10,
                    require_pending_file_deletes=version >= 11,
                    require_pending_file_moves=version >= 12,
                    require_metadata_datetime_column=version >= 13,
                    require_internal_structure=False,
                    require_no_superseded_sources=False,
                )
            source_columns = table_columns(conn, "sources") if table_exists(conn, "sources") else set()
            return MigrationPlan(
                current_version=version,
                target_version=SCHEMA_VERSION,
                imported_files=count_rows(conn, "files"),
                duplicate_findings=count_rows(conn, "duplicate_findings"),
                creates_file_sources=False,
                cleans_gps_errors=has_legacy_gps_errors(conn),
                backfills_h3_10_11=needs_h3_10_11_backfill(conn),
                refreshes_performance_indexes=version == 8,
                adds_camera_columns=version < 10,
                creates_pending_file_deletes=version < 11,
                creates_pending_file_moves=version < 12,
                adds_metadata_datetime_column=version < 13,
                removes_superseded_sources=(
                    "superseded_by_source_id" in source_columns
                    or has_superseded_source_status(conn)
                ),
            )
        if validate:
            validate_pre_migration(conn, version)
        file_columns = table_columns(conn, "files") if table_exists(conn, "files") else set()
        source_columns = table_columns(conn, "sources") if table_exists(conn, "sources") else set()
        file_source_columns = table_columns(conn, "file_sources") if table_exists(conn, "file_sources") else set()
        return MigrationPlan(
            current_version=version,
            target_version=SCHEMA_VERSION,
            imported_files=count_rows(conn, "files"),
            duplicate_findings=count_rows(conn, "duplicate_findings"),
            creates_file_sources=not table_exists(conn, "file_sources"),
            rebuilds_files_without_legacy_source_columns=bool(
                {"source_id", "source_path", "source_path_key"} & file_columns
            ),
            drops_duplicate_findings=table_exists(conn, "duplicate_findings"),
            rebuilds_errors_without_source_fk=table_exists(conn, "errors")
            and bool(conn.execute("PRAGMA foreign_key_list(errors)").fetchall()),
            rebuilds_sources_without_kind=bool("kind" in source_columns)
            or not source_name_is_not_null(conn),
            rebuilds_file_sources_without_kind=bool("kind" in file_source_columns),
            cleans_gps_errors=has_legacy_gps_errors(conn),
            backfills_h3_10_11=needs_h3_10_11_backfill(conn),
            adds_camera_columns=True,
            creates_pending_file_deletes=True,
            creates_pending_file_moves=True,
            adds_metadata_datetime_column=True,
            removes_superseded_sources=(
                "superseded_by_source_id" in source_columns
                or has_superseded_source_status(conn)
            ),
        )
    finally:
        conn.close()


def backup_database(target: Path) -> Path:
    db_path = db_path_for_target(target)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = target / f"{DB_FILENAME}.backup-before-schema-{SCHEMA_VERSION}-{stamp}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def migrate_database(target: Path) -> MigrationPlan:
    conn = connect(target, require_current=False)
    try:
        version = schema_version(conn)
        if version == SCHEMA_VERSION:
            refreshes_performance_indexes = bool(missing_performance_indexes(conn))
            internal_repairs = current_schema_internal_repairs(conn)
            try:
                conn.execute("BEGIN IMMEDIATE")
                validate_current_schema(
                    conn,
                    require_performance_indexes=False,
                    require_pending_file_deletes=False,
                    require_pending_file_moves=False,
                    require_internal_structure=False,
                )
                repair_current_schema_internal_structure(conn)
                drop_performance_indexes(conn)
                ensure_performance_indexes(conn)
                validate_current_schema(conn)
                validate_database_health(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return MigrationPlan(
                current_version=version,
                target_version=SCHEMA_VERSION,
                imported_files=count_rows(conn, "files"),
                duplicate_findings=count_rows(conn, "duplicate_findings"),
                creates_file_sources=False,
                refreshes_performance_indexes=refreshes_performance_indexes,
                internal_repairs=internal_repairs,
            )
        if version in {5, 6, 7, 8, 9, 10, 11, 12, 13}:
            imported_files = count_rows(conn, "files")
            duplicate_findings = count_rows(conn, "duplicate_findings")
            cleans_gps_errors = has_legacy_gps_errors(conn)
            backfills_h3_10_11 = needs_h3_10_11_backfill(conn)
            removes_superseded_sources = (
                "superseded_by_source_id" in table_columns(conn, "sources")
                or has_superseded_source_status(conn)
            )
            try:
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute("BEGIN IMMEDIATE")
                ensure_compatible_columns(conn)
                set_collection_id(conn)
                rebuild_sources_without_superseded(conn)
                validate_current_schema(conn, require_performance_indexes=False)
                drop_performance_indexes(conn)
                ensure_performance_indexes(conn)
                cleanup_legacy_gps_errors(conn)
                backfill_h3_10_11(conn)
                set_meta(conn, "schema_version", str(SCHEMA_VERSION))
                log_command(conn, "migrate", {"from_schema_version": version, "to_schema_version": SCHEMA_VERSION})
                validate_current_schema(conn)
                set_collection_id(conn)
                validate_database_health(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
            return MigrationPlan(
                current_version=version,
                target_version=SCHEMA_VERSION,
                imported_files=imported_files,
                duplicate_findings=duplicate_findings,
                creates_file_sources=False,
                cleans_gps_errors=cleans_gps_errors,
                backfills_h3_10_11=backfills_h3_10_11,
                refreshes_performance_indexes=version == 8,
                adds_camera_columns=version < 10,
                creates_pending_file_deletes=version < 11,
                creates_pending_file_moves=version < 12,
                adds_metadata_datetime_column=version < 13,
                removes_superseded_sources=removes_superseded_sources,
            )
        validate_pre_migration(conn, version)
        imported_files = count_rows(conn, "files")
        duplicate_findings = count_rows(conn, "duplicate_findings")
        creates_file_sources = not table_exists(conn, "file_sources")
        file_columns = table_columns(conn, "files")
        source_columns = table_columns(conn, "sources")
        file_source_columns = table_columns(conn, "file_sources") if table_exists(conn, "file_sources") else set()
        rebuilds_files = bool({"source_id", "source_path", "source_path_key"} & file_columns)
        drops_duplicate_findings = table_exists(conn, "duplicate_findings")
        rebuilds_errors = table_exists(conn, "errors") and bool(
            conn.execute("PRAGMA foreign_key_list(errors)").fetchall()
        )
        rebuilds_sources = bool("kind" in source_columns) or not source_name_is_not_null(conn)
        rebuilds_file_sources = bool("kind" in file_source_columns)
        cleans_gps_errors = has_legacy_gps_errors(conn)
        backfills_h3_10_11 = needs_h3_10_11_backfill(conn)
        removes_superseded_sources = (
            "superseded_by_source_id" in source_columns
            or has_superseded_source_status(conn)
        )
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            ensure_compatible_columns(conn)
            set_collection_id(conn)
            create_file_sources_schema(conn)
            if "kind" in table_columns(conn, "file_sources"):
                validate_legacy_file_sources_schema(conn)
            else:
                validate_file_sources_schema(conn)
            if {"source_id", "source_path", "source_path_key"} <= table_columns(conn, "files"):
                backfill_file_sources(conn)
            rebuild_files_without_legacy_source_columns(conn)
            rebuild_errors_without_source_fk(conn)
            rebuild_sources_without_kind(conn)
            rebuild_sources_without_superseded(conn)
            rebuild_file_sources_without_kind(conn)
            if table_exists(conn, "duplicate_findings"):
                conn.execute("DROP TABLE duplicate_findings")
            ensure_compatible_columns(conn)
            drop_performance_indexes(conn)
            ensure_performance_indexes(conn)
            cleanup_legacy_gps_errors(conn)
            backfill_h3_10_11(conn)
            set_meta(conn, "schema_version", str(SCHEMA_VERSION))
            log_command(conn, "migrate", {"from_schema_version": version, "to_schema_version": SCHEMA_VERSION})
            validate_current_schema(conn)
            set_collection_id(conn)
            validate_database_health(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        return MigrationPlan(
            current_version=version,
            target_version=SCHEMA_VERSION,
            imported_files=imported_files,
            duplicate_findings=duplicate_findings,
            creates_file_sources=creates_file_sources,
            rebuilds_files_without_legacy_source_columns=rebuilds_files,
            drops_duplicate_findings=drops_duplicate_findings,
            rebuilds_errors_without_source_fk=rebuilds_errors,
            rebuilds_sources_without_kind=rebuilds_sources,
            rebuilds_file_sources_without_kind=rebuilds_file_sources,
            cleans_gps_errors=cleans_gps_errors,
            backfills_h3_10_11=backfills_h3_10_11,
            refreshes_performance_indexes=version == 8,
            adds_camera_columns=True,
            creates_pending_file_deletes=True,
            creates_pending_file_moves=True,
            adds_metadata_datetime_column=True,
            removes_superseded_sources=removes_superseded_sources,
        )
    finally:
        conn.close()


def validate_pre_migration(conn: sqlite3.Connection, version: int) -> None:
    if version not in {1, 2, 3}:
        raise ValueError(f"Kan ikke migrere database med schema_version={version}.")
    required_tables = {"meta", "sources", "files", "errors"}
    if version == 1:
        required_tables.add("duplicate_findings")
    missing = sorted(table for table in required_tables if not table_exists(conn, table))
    if missing:
        raise ValueError(f"Databasen mangler forventede tabeller: {', '.join(missing)}")
    if version == 1:
        validate_existing_file_source_references(conn)
    if table_exists(conn, "file_sources"):
        validate_legacy_file_sources_schema(conn)


def validate_existing_file_source_references(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT files.id, files.source_id
        FROM files
        LEFT JOIN sources ON sources.id = files.source_id
        WHERE sources.id IS NULL
        ORDER BY files.id
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise ValueError(
            f"files #{row['id']} peker på source #{row['source_id']}, men den kilden finnes ikke."
        )

    row = conn.execute(
        """
        SELECT duplicate_findings.id, duplicate_findings.source_id
        FROM duplicate_findings
        LEFT JOIN sources ON sources.id = duplicate_findings.source_id
        WHERE sources.id IS NULL
        ORDER BY duplicate_findings.id
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise ValueError(
            f"duplicate_findings #{row['id']} peker på source #{row['source_id']}, "
            "men den kilden finnes ikke."
        )

    row = conn.execute(
        """
        SELECT duplicate_findings.id, duplicate_findings.matched_file_id
        FROM duplicate_findings
        LEFT JOIN files ON files.id = duplicate_findings.matched_file_id
        WHERE files.id IS NULL
        ORDER BY duplicate_findings.id
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise ValueError(
            f"duplicate_findings #{row['id']} peker på files #{row['matched_file_id']}, "
            "men den filraden finnes ikke."
        )

    row = conn.execute(
        """
        SELECT duplicate_findings.id, duplicate_findings.sha256, files.sha256 AS file_sha256
        FROM duplicate_findings
        JOIN files ON files.id = duplicate_findings.matched_file_id
        WHERE duplicate_findings.sha256 != files.sha256
        ORDER BY duplicate_findings.id
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise ValueError(
            f"duplicate_findings #{row['id']} har sha256={row['sha256']}, "
            f"men matched_file har sha256={row['file_sha256']}."
        )


def source_name_is_not_null(conn: sqlite3.Connection) -> bool:
    if not table_exists(conn, "sources"):
        return False
    for row in conn.execute("PRAGMA table_info(sources)"):
        if row["name"] == "name":
            return bool(row["notnull"])
    return False


def validate_current_schema(
    conn: sqlite3.Connection,
    *,
    require_performance_indexes: bool = True,
    require_manual_date_columns: bool = True,
    require_camera_columns: bool = True,
    require_metadata_datetime_column: bool = True,
    require_pending_file_deletes: bool = True,
    require_pending_file_moves: bool = True,
    require_internal_structure: bool = True,
    require_no_superseded_sources: bool = True,
) -> None:
    if not table_exists(conn, "sources"):
        raise ValueError("Databasen mangler tabellen sources.")
    if table_exists(conn, "duplicate_findings"):
        raise ValueError("Databasen inneholder legacy-tabellen duplicate_findings. Kjør bildebank migrate.")
    source_columns = table_columns(conn, "sources")
    if "kind" in source_columns:
        raise ValueError("sources inneholder gammel kind-kolonne. Kjør bildebank migrate.")
    if require_no_superseded_sources and "superseded_by_source_id" in source_columns:
        raise ValueError("sources inneholder gammel superseded-kolonne. Kjør bildebank migrate.")
    if "name" not in source_columns or not source_name_is_not_null(conn):
        raise ValueError("sources.name er ikke påkrevd. Kjør bildebank migrate.")
    if require_no_superseded_sources and has_superseded_source_status(conn):
        raise ValueError("sources inneholder gammel superseded-status. Kjør bildebank migrate.")
    file_columns = table_columns(conn, "files") if table_exists(conn, "files") else set()
    legacy_file_columns = sorted({"source_id", "source_path", "source_path_key"} & file_columns)
    if legacy_file_columns:
        raise ValueError(
            "files inneholder gamle kildekolonner "
            f"({', '.join(legacy_file_columns)}). Kjør bildebank migrate."
        )
    if require_manual_date_columns:
        missing_manual_date_columns = sorted(
            {"manual_date_from", "manual_date_to", "manual_date_note"} - file_columns
        )
        if missing_manual_date_columns:
            raise ValueError(
                "files mangler manuelle datokolonner "
                f"({', '.join(missing_manual_date_columns)}). Kjør bildebank migrate."
            )
    if require_camera_columns:
        missing_camera_columns = sorted({"camera_make", "camera_model"} - file_columns)
        if missing_camera_columns:
            raise ValueError(
                "files mangler kamerakolonner "
                f"({', '.join(missing_camera_columns)}). Kjør bildebank migrate."
            )
    if require_metadata_datetime_column and "metadata_datetime" not in file_columns:
        raise ValueError("files mangler metadata_datetime. Kjør bildebank migrate.")
    if require_pending_file_deletes:
        validate_pending_file_deletes_schema(conn)
    if require_pending_file_moves:
        validate_pending_file_moves_schema(conn)
    if table_exists(conn, "errors") and conn.execute("PRAGMA foreign_key_list(errors)").fetchall():
        raise ValueError("errors har gammel foreign key til sources. Kjør bildebank migrate.")
    validate_file_sources_schema(conn)
    if schema_version(conn) >= 5:
        validate_relative_target_paths(conn)
    if require_internal_structure:
        validate_tags_schema(conn)
        validate_collection_id(conn)
    if require_performance_indexes:
        validate_performance_indexes(conn)


def validate_pending_file_deletes_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "pending_file_deletes"):
        raise ValueError("Databasen mangler tabellen pending_file_deletes.")
    expected_columns = {
        "id",
        "path",
        "reason",
        "source_id",
        "attempts",
        "last_error",
        "created_at",
        "updated_at",
    }
    missing = sorted(expected_columns - table_columns(conn, "pending_file_deletes"))
    if missing:
        raise ValueError(
            "pending_file_deletes mangler forventede kolonner: "
            f"{', '.join(missing)}"
        )
    indexes = list(conn.execute("PRAGMA index_list(pending_file_deletes)"))
    if not any(
        bool(index["unique"])
        and [row["name"] for row in conn.execute(f"PRAGMA index_info({index['name']})")]
        == ["path"]
        for index in indexes
    ):
        raise ValueError("pending_file_deletes mangler unik nøkkel for path.")
    if conn.execute("PRAGMA foreign_key_list(pending_file_deletes)").fetchall():
        raise ValueError(
            "pending_file_deletes har gammel foreign key for source_id."
        )


def validate_pending_file_moves_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "pending_file_moves"):
        raise ValueError("Databasen mangler tabellen pending_file_moves.")
    expected_columns = {
        "id",
        "file_id",
        "from_path",
        "to_path",
        "sha256",
        "operation",
        "state",
        "created_at",
        "updated_at",
        "completed_at",
        "last_error",
    }
    missing = sorted(expected_columns - table_columns(conn, "pending_file_moves"))
    if missing:
        raise ValueError(
            "pending_file_moves mangler forventede kolonner: "
            f"{', '.join(missing)}"
        )


def validate_tags_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "tags"):
        raise ValueError("Databasen mangler tabellen tags.")
    if not table_exists(conn, "file_tags"):
        raise ValueError("Databasen mangler tabellen file_tags.")

    validate_tags_schema_structure(conn)

    for name in SYSTEM_TAG_NAMES:
        row = conn.execute(
            "SELECT name, kind FROM tags WHERE name_key = ?",
            (tag_name_key(name),),
        ).fetchone()
        if row is None or row["name"] != name or row["kind"] != TAG_KIND_SYSTEM:
            raise ValueError(f"Databasen mangler systemtaggen {name!r} med kind=system.")


def validate_tags_schema_structure(conn: sqlite3.Connection) -> None:
    expected_columns = {
        "tags": {"id", "name", "name_key", "kind", "created_at"},
        "file_tags": {"file_id", "tag_id", "created_at"},
    }
    for table, expected in expected_columns.items():
        missing = sorted(expected - table_columns(conn, table))
        if missing:
            raise ValueError(f"{table} mangler forventede kolonner: {', '.join(missing)}")

    tag_indexes = list(conn.execute("PRAGMA index_list(tags)"))
    if not any(
        bool(index["unique"])
        and [row["name"] for row in conn.execute(f"PRAGMA index_info({index['name']})")] == ["name_key"]
        for index in tag_indexes
    ):
        raise ValueError("tags mangler unik nøkkel for name_key.")

    file_tag_info = list(conn.execute("PRAGMA table_info(file_tags)"))
    primary_key = [
        row["name"]
        for row in sorted(file_tag_info, key=lambda row: int(row["pk"]))
        if int(row["pk"]) > 0
    ]
    if primary_key != ["file_id", "tag_id"]:
        raise ValueError("file_tags mangler primærnøkkel for (file_id, tag_id).")

    foreign_keys = {
        (str(row["from"]), str(row["table"]), str(row["to"]), str(row["on_delete"]).upper())
        for row in conn.execute("PRAGMA foreign_key_list(file_tags)")
    }
    expected_foreign_keys = {
        ("file_id", "files", "id", "CASCADE"),
        ("tag_id", "tags", "id", "CASCADE"),
    }
    if not expected_foreign_keys <= foreign_keys:
        raise ValueError("file_tags mangler forventede foreign keys.")

    index_columns = [
        row["name"]
        for row in conn.execute("PRAGMA index_info(idx_file_tags_tag_id_file_id)")
    ]
    if index_columns != ["tag_id", "file_id"]:
        raise ValueError("Databasen mangler indeksen idx_file_tags_tag_id_file_id.")


def validate_collection_id(conn: sqlite3.Connection) -> str:
    value = get_meta(conn, COLLECTION_ID_META_KEY)
    if value is None:
        raise ValueError("Databasen mangler meta.collection_id.")
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"Ugyldig collection_id i databasen: {value}") from exc
    if normalized != value:
        raise ValueError(f"collection_id er ikke kanonisk UUID: {value}")
    return value


def current_schema_internal_repairs(conn: sqlite3.Connection) -> tuple[str, ...]:
    repairs: list[str] = []
    if not table_exists(conn, "pending_file_deletes"):
        repairs.append("Databasen mangler tabellen pending_file_deletes.")
    else:
        try:
            validate_pending_file_deletes_schema(conn)
        except ValueError as exc:
            repairs.append(str(exc))
    if not table_exists(conn, "pending_file_moves"):
        repairs.append("Databasen mangler tabellen pending_file_moves.")
    else:
        try:
            validate_pending_file_moves_schema(conn)
        except ValueError as exc:
            repairs.append(str(exc))
    tags_exists = table_exists(conn, "tags")
    file_tags_exists = table_exists(conn, "file_tags")
    if not tags_exists:
        repairs.append("Databasen mangler tabellen tags.")
    if not file_tags_exists:
        repairs.append("Databasen mangler tabellen file_tags.")
    if tags_exists and file_tags_exists:
        try:
            validate_tags_schema_structure(conn)
        except ValueError as exc:
            repairs.append(str(exc))
        if {"name", "name_key", "kind"} <= table_columns(conn, "tags"):
            for name in SYSTEM_TAG_NAMES:
                row = conn.execute(
                    "SELECT name, kind FROM tags WHERE name_key = ?",
                    (tag_name_key(name),),
                ).fetchone()
                if row is None or row["name"] != name or row["kind"] != TAG_KIND_SYSTEM:
                    repairs.append(f"Databasen mangler systemtaggen {name!r} med kind=system.")
    try:
        validate_collection_id(conn)
    except ValueError as exc:
        repairs.append(str(exc))
    return tuple(repairs)


def repair_current_schema_internal_structure(conn: sqlite3.Connection) -> None:
    repair_tags_schema(conn)
    repair_pending_file_deletes_schema(conn)
    repair_pending_file_moves_schema(conn)
    value = get_meta(conn, COLLECTION_ID_META_KEY)
    if value is None:
        set_meta(conn, COLLECTION_ID_META_KEY, str(uuid.uuid4()))
    else:
        try:
            normalized = str(uuid.UUID(value))
        except ValueError:
            normalized = str(uuid.uuid4())
        if normalized != value:
            set_meta(conn, COLLECTION_ID_META_KEY, normalized)


def repair_pending_file_deletes_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "pending_file_deletes"):
        create_pending_file_deletes_schema(conn)
        return
    try:
        validate_pending_file_deletes_schema(conn)
        return
    except ValueError:
        pass
    required = {
        "id",
        "path",
        "reason",
        "source_id",
        "attempts",
        "last_error",
        "created_at",
        "updated_at",
    }
    missing = sorted(required - table_columns(conn, "pending_file_deletes"))
    if missing:
        raise ValueError(
            "Kan ikke reparere pending_file_deletes; mangler kolonner: "
            f"{', '.join(missing)}"
        )
    conn.execute("DROP TABLE IF EXISTS pending_file_deletes_v11_repair")
    conn.execute(
        """
        CREATE TABLE pending_file_deletes_v11_repair (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            source_id INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        INSERT INTO pending_file_deletes_v11_repair(
            id, path, reason, source_id, attempts, last_error, created_at, updated_at
        )
        SELECT id, path, reason, source_id, attempts, last_error, created_at, updated_at
        FROM pending_file_deletes
        ORDER BY id
        """
    )
    conn.execute("DROP TABLE pending_file_deletes")
    conn.execute(
        "ALTER TABLE pending_file_deletes_v11_repair RENAME TO pending_file_deletes"
    )


def repair_pending_file_moves_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "pending_file_moves"):
        create_pending_file_moves_schema(conn)
        return
    validate_pending_file_moves_schema(conn)


def repair_tags_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "tags") or not table_exists(conn, "file_tags"):
        create_tags_schema(conn)
    if "kind" not in table_columns(conn, "tags"):
        ensure_column(conn, "tags", "kind", "TEXT NOT NULL DEFAULT 'user'")
    if {"file_id", "tag_id"} <= table_columns(conn, "file_tags"):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_file_tags_tag_id_file_id
            ON file_tags(tag_id, file_id)
            """
        )
    try:
        validate_tags_schema_structure(conn)
        seed_system_tags(conn)
        return
    except ValueError:
        pass

    tag_columns = table_columns(conn, "tags")
    if not {"id", "name"} <= tag_columns:
        raise ValueError("Kan ikke reparere tags uten kolonnene id og name.")
    file_tag_columns = table_columns(conn, "file_tags")
    if not {"file_id", "tag_id"} <= file_tag_columns:
        raise ValueError("Kan ikke reparere file_tags uten kolonnene file_id og tag_id.")

    tag_rows = list(conn.execute("SELECT * FROM tags ORDER BY id"))
    file_tag_rows = list(conn.execute("SELECT * FROM file_tags ORDER BY file_id, tag_id"))
    conn.execute("DROP TABLE IF EXISTS file_tags_v10_repair")
    conn.execute("DROP TABLE IF EXISTS tags_v10_repair")
    conn.execute(
        """
        CREATE TABLE tags_v10_repair (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE file_tags_v10_repair (
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags_v10_repair(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(file_id, tag_id)
        )
        """
    )
    for row in tag_rows:
        name = normalize_tag_name(str(row["name"]))
        name_key = str(row["name_key"]) if "name_key" in tag_columns else tag_name_key(name)
        kind = str(row["kind"]) if "kind" in tag_columns else tag_kind_for_name(name)
        created_at = (
            str(row["created_at"])
            if "created_at" in tag_columns
            else datetime.now().isoformat(timespec="seconds")
        )
        conn.execute(
            """
            INSERT INTO tags_v10_repair(id, name, name_key, kind, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (int(row["id"]), name, name_key, kind, created_at),
        )
    for row in file_tag_rows:
        created_at = (
            str(row["created_at"])
            if "created_at" in file_tag_columns
            else datetime.now().isoformat(timespec="seconds")
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO file_tags_v10_repair(file_id, tag_id, created_at)
            VALUES(?, ?, ?)
            """,
            (int(row["file_id"]), int(row["tag_id"]), created_at),
        )

    conn.execute("DROP TABLE file_tags")
    conn.execute("DROP TABLE tags")
    conn.execute("ALTER TABLE tags_v10_repair RENAME TO tags")
    conn.execute("ALTER TABLE file_tags_v10_repair RENAME TO file_tags")
    conn.execute(
        """
        CREATE INDEX idx_file_tags_tag_id_file_id
        ON file_tags(tag_id, file_id)
        """
    )
    seed_system_tags(conn)


def validate_database_health(conn: sqlite3.Connection) -> None:
    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise ValueError(f"foreign_key_check feilet: {foreign_key_errors[0]}")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError(f"integrity_check feilet: {integrity}")


def validate_performance_indexes(conn: sqlite3.Connection) -> None:
    missing = missing_performance_indexes(conn)
    if missing:
        raise ValueError(f"Databasen mangler ytelsesindeks: {missing[0]}. Kjør bildebank migrate.")


def validate_no_duplicate_active_sha256(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT sha256, COUNT(*) AS file_count
        FROM files
        WHERE deleted_at IS NULL
        GROUP BY sha256
        HAVING COUNT(*) > 1
        ORDER BY sha256
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return
    raise ValueError(
        "Kan ikke opprette unik indeks for aktive filer: "
        f"{int(row['file_count'])} aktive files-rader har samme sha256={row['sha256']}. "
        "Kjør bildebank doctor og rett duplikatene før du prøver migrate på nytt."
    )


def missing_performance_indexes(conn: sqlite3.Connection) -> list[str]:
    existing = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    return sorted(name for name in PERFORMANCE_INDEX_NAMES if name not in existing)


def validate_relative_target_paths(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "files"):
        return
    row = conn.execute(
        """
        SELECT id, target_path
        FROM files
        WHERE target_path LIKE '/%'
           OR target_path GLOB '[A-Za-z]:*'
        ORDER BY id
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise ValueError(
            f"files #{row['id']} har absolutt target_path i schema_version={schema_version(conn)}-database: {row['target_path']}. "
            "Kjør bildebank migrate."
        )
    row = conn.execute(
        """
        SELECT id, deleted_original_target_path
        FROM files
        WHERE deleted_original_target_path IS NOT NULL
          AND (
              deleted_original_target_path LIKE '/%'
              OR deleted_original_target_path GLOB '[A-Za-z]:*'
          )
        ORDER BY id
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise ValueError(
            "files #"
            f"{row['id']} har absolutt deleted_original_target_path i schema_version={schema_version(conn)}-database: "
            f"{row['deleted_original_target_path']}. Kjør bildebank migrate."
        )


def validate_legacy_file_sources_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "file_sources"):
        raise ValueError("Databasen mangler tabellen file_sources.")
    required_columns = {
        "id",
        "file_id",
        "source_id",
        "source_path",
        "source_path_key",
        "sha256",
        "size_bytes",
        "kind",
        "recorded_at",
    }
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(file_sources)")}
    missing = sorted(required_columns - columns)
    if missing:
        raise ValueError(f"file_sources mangler forventede kolonner: {', '.join(missing)}")


def validate_file_sources_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "file_sources"):
        raise ValueError("Databasen mangler tabellen file_sources.")
    required_columns = {
        "id",
        "file_id",
        "source_id",
        "source_path",
        "source_path_key",
        "sha256",
        "size_bytes",
        "recorded_at",
    }
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(file_sources)")}
    missing = sorted(required_columns - columns)
    if missing:
        raise ValueError(f"file_sources mangler forventede kolonner: {', '.join(missing)}")
    if "kind" in columns:
        raise ValueError("file_sources inneholder gammel kind-kolonne. Kjør bildebank migrate.")


def backfill_file_sources(conn: sqlite3.Connection) -> None:
    for row in conn.execute(
        """
        SELECT id AS file_id, source_id, source_path, source_path_key, sha256, size_bytes
        FROM files
        ORDER BY id
        """
    ):
        _insert_or_validate_file_source(
            conn,
            file_id=int(row["file_id"]),
            source_id=int(row["source_id"]),
            source_path=str(row["source_path"]),
            source_path_key=str(row["source_path_key"]),
            sha256=str(row["sha256"]),
            size_bytes=int(row["size_bytes"]),
        )

    if table_exists(conn, "duplicate_findings"):
        for row in conn.execute(
            """
            SELECT
                duplicate_findings.matched_file_id AS file_id,
                duplicate_findings.source_id,
                duplicate_findings.source_path,
                duplicate_findings.source_path_key,
                duplicate_findings.sha256,
                files.size_bytes
            FROM duplicate_findings
            JOIN files ON files.id = duplicate_findings.matched_file_id
            ORDER BY duplicate_findings.id
            """
        ):
            _insert_or_validate_file_source(
                conn,
                file_id=int(row["file_id"]),
                source_id=int(row["source_id"]),
                source_path=str(row["source_path"]),
                source_path_key=str(row["source_path_key"]),
                sha256=str(row["sha256"]),
                size_bytes=int(row["size_bytes"]),
            )


def _insert_or_validate_file_source(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    source_id: int,
    source_path: str,
    source_path_key: str,
    sha256: str,
    size_bytes: int,
) -> None:
    existing = conn.execute(
        """
        SELECT file_id, source_path, sha256, size_bytes
        FROM file_sources
        WHERE source_id = ? AND source_path_key = ?
        """,
        (source_id, source_path_key),
    ).fetchone()
    if existing is not None:
        mismatches = []
        expected = {
            "file_id": file_id,
            "source_path": source_path,
            "sha256": sha256,
            "size_bytes": size_bytes,
        }
        for key, value in expected.items():
            if existing[key] != value:
                mismatches.append(f"{key}: eksisterende={existing[key]!r}, forventet={value!r}")
        if mismatches:
            raise ValueError(
                "Konflikt i file_sources for "
                f"source_id={source_id}, source_path_key={source_path_key}: "
                + "; ".join(mismatches)
            )
        return

    conn.execute(
        """
        INSERT INTO file_sources(
            file_id, source_id, source_path, source_path_key, sha256, size_bytes
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (file_id, source_id, source_path, source_path_key, sha256, size_bytes),
    )


def rebuild_files_without_legacy_source_columns(conn: sqlite3.Connection) -> None:
    legacy_columns = {"source_id", "source_path", "source_path_key"}
    if not (legacy_columns & table_columns(conn, "files")):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256)")
        return
    conn.executescript(
        """
        CREATE TABLE files_v3 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_path TEXT NOT NULL,
            target_path_key TEXT NOT NULL UNIQUE,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            taken_date TEXT,
            date_source TEXT NOT NULL,
            name_conflict INTEGER NOT NULL DEFAULT 0,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TEXT,
            deleted_original_target_path TEXT
        );

        INSERT INTO files_v3(
            id, target_path, target_path_key, original_filename, stored_filename,
            sha256, size_bytes, taken_date, date_source, name_conflict, imported_at,
            deleted_at, deleted_original_target_path
        )
        SELECT
            id, target_path, target_path_key, original_filename, stored_filename,
            sha256, size_bytes, taken_date, date_source, name_conflict, imported_at,
            deleted_at, deleted_original_target_path
        FROM files;

        DROP TABLE files;
        ALTER TABLE files_v3 RENAME TO files;
        CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
        """
    )


def rebuild_errors_without_source_fk(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "errors"):
        conn.executescript(
            """
            CREATE TABLE errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                source_path TEXT,
                stage TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            );
            """
        )
        return
    if not conn.execute("PRAGMA foreign_key_list(errors)").fetchall():
        return
    conn.executescript(
        """
        CREATE TABLE errors_v3 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER,
            source_path TEXT,
            stage TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        );

        INSERT INTO errors_v3(
            id, source_id, source_path, stage, message, created_at, resolved_at
        )
        SELECT id, source_id, source_path, stage, message, created_at, resolved_at
        FROM errors;

        DROP TABLE errors;
        ALTER TABLE errors_v3 RENAME TO errors;
        """
    )


def default_source_name(path_value: str) -> str:
    raw = path_value.strip()
    drive = re.match(r"^([A-Za-z]):[\\/]*$", raw)
    if drive:
        return drive.group(1).upper()
    if "\\" in raw or re.match(r"^[A-Za-z]:", raw):
        candidate = PureWindowsPath(raw).name
        if candidate:
            return candidate
        if PureWindowsPath(raw).drive:
            return PureWindowsPath(raw).drive.rstrip(":\\/")
    candidate = Path(raw).name
    if candidate:
        return candidate
    stripped = raw.strip("\\/")
    return stripped or "kilde"


def unique_source_name(base: str, used: set[str]) -> str:
    name = base
    index = 1
    while name in used:
        name = f"{base}-{index}"
        index += 1
    used.add(name)
    return name


def rebuild_sources_without_kind(conn: sqlite3.Connection) -> None:
    columns = table_columns(conn, "sources")
    needs_rebuild = "kind" in columns or not source_name_is_not_null(conn)
    if not needs_rebuild:
        return
    rebuild_sources_table(conn)


def has_superseded_source_status(conn: sqlite3.Connection) -> bool:
    if not table_exists(conn, "sources") or "status" not in table_columns(conn, "sources"):
        return False
    return conn.execute(
        "SELECT 1 FROM sources WHERE status = 'superseded' LIMIT 1"
    ).fetchone() is not None


def rebuild_sources_without_superseded(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "sources"):
        return
    columns = table_columns(conn, "sources")
    if "superseded_by_source_id" not in columns and not has_superseded_source_status(conn):
        return
    rebuild_sources_table(conn)


def rebuild_sources_table(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
    used: set[str] = set()
    named_rows: list[tuple[int, str, str | None, str, str, str | None, str]] = []
    for row in rows:
        existing_name = row["name"] if "name" in row.keys() else None
        base = str(existing_name).strip() if existing_name else default_source_name(str(row["path"]))
        name = unique_source_name(base, used)
        raw_path_key = row["path_key"] if "path_key" in row.keys() else path_key(Path(str(row["path"])))
        source_path_key = str(raw_path_key) if raw_path_key is not None else None
        raw_imported_at = row["imported_at"]
        imported_at = str(raw_imported_at) if raw_imported_at is not None else None
        status = str(row["status"])
        if status == "superseded":
            status = "imported"
        named_rows.append(
            (
                int(row["id"]),
                str(row["path"]),
                source_path_key,
                name,
                str(row["added_at"]),
                imported_at,
                status,
            )
        )
    conn.executescript(
        """
        CREATE TABLE sources_v4 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            path_key TEXT,
            name TEXT NOT NULL UNIQUE,
            added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            imported_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO sources_v4(
            id, path, path_key, name, added_at, imported_at, status
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        named_rows,
    )
    conn.executescript(
        """
        DROP TABLE sources;
        ALTER TABLE sources_v4 RENAME TO sources;
        """
    )


def rebuild_file_sources_without_kind(conn: sqlite3.Connection) -> None:
    if "kind" not in table_columns(conn, "file_sources"):
        return
    conn.executescript(
        """
        CREATE TABLE file_sources_v4 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL REFERENCES files(id),
            source_id INTEGER NOT NULL REFERENCES sources(id),
            source_path TEXT NOT NULL,
            source_path_key TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_id, source_path_key)
        );

        INSERT INTO file_sources_v4(
            id, file_id, source_id, source_path, source_path_key, sha256, size_bytes, recorded_at
        )
        SELECT id, file_id, source_id, source_path, source_path_key, sha256, size_bytes, recorded_at
        FROM file_sources;

        DROP TABLE file_sources;
        ALTER TABLE file_sources_v4 RENAME TO file_sources;
        CREATE INDEX IF NOT EXISTS idx_file_sources_file_id ON file_sources(file_id);
        CREATE INDEX IF NOT EXISTS idx_file_sources_sha256 ON file_sources(sha256);
        CREATE INDEX IF NOT EXISTS idx_file_sources_source_id_id ON file_sources(source_id, id);
        CREATE INDEX IF NOT EXISTS idx_file_sources_source_id_file_id ON file_sources(source_id, file_id);
        """
    )

def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if table not in {"sources", "files", "duplicate_findings", "file_sources", "errors"}:
        raise ValueError(f"Unsupported table: {table}")
    if not table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def has_legacy_gps_errors(conn: sqlite3.Connection) -> bool:
    if not table_exists(conn, "files") or "gps_error" not in table_columns(conn, "files"):
        return False
    return (
        conn.execute(
            """
            SELECT 1
            FROM files
            WHERE gps_error IS NOT NULL
              AND gps_error NOT IN (?, ?)
            LIMIT 1
            """,
            (GPS_ERROR_EXIFTOOL, GPS_ERROR_FILE_MISSING),
        ).fetchone()
        is not None
    )


def cleanup_legacy_gps_errors(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "files") or "gps_error" not in table_columns(conn, "files"):
        return 0
    cursor = conn.execute(
        """
        UPDATE files
        SET gps_error = CASE
            WHEN gps_error LIKE 'Filen finnes ikke:%' THEN ?
            ELSE ?
        END
        WHERE gps_error IS NOT NULL
          AND gps_error NOT IN (?, ?)
        """,
        (GPS_ERROR_FILE_MISSING, GPS_ERROR_EXIFTOOL, GPS_ERROR_EXIFTOOL, GPS_ERROR_FILE_MISSING),
    )
    return int(cursor.rowcount or 0)


def needs_h3_10_11_backfill(conn: sqlite3.Connection) -> bool:
    if not table_exists(conn, "files"):
        return False
    columns = table_columns(conn, "files")
    if "gps_lat" not in columns or "gps_lon" not in columns:
        return False
    if {"h3_res10", "h3_res11"} - columns:
        row = conn.execute(
            """
            SELECT 1
            FROM files
            WHERE gps_lat IS NOT NULL
              AND gps_lon IS NOT NULL
            LIMIT 1
            """
        ).fetchone()
        return row is not None
    row = conn.execute(
        """
        SELECT 1
        FROM files
        WHERE gps_lat IS NOT NULL
          AND gps_lon IS NOT NULL
          AND (h3_res10 IS NULL OR h3_res11 IS NULL)
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def backfill_h3_10_11(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "files"):
        return 0
    columns = table_columns(conn, "files")
    required = {"gps_lat", "gps_lon", "h3_res10", "h3_res11"}
    if not required <= columns:
        return 0
    import h3

    rows = list(
        conn.execute(
            """
            SELECT id, gps_lat, gps_lon
            FROM files
            WHERE gps_lat IS NOT NULL
              AND gps_lon IS NOT NULL
              AND (h3_res10 IS NULL OR h3_res11 IS NULL)
            """
        )
    )
    for row in rows:
        lat = float(row["gps_lat"])
        lon = float(row["gps_lon"])
        conn.execute(
            """
            UPDATE files
            SET h3_res10 = ?,
                h3_res11 = ?
            WHERE id = ?
            """,
            (h3.latlng_to_cell(lat, lon, 10), h3.latlng_to_cell(lat, lon, 11), int(row["id"])),
        )
    return len(rows)
