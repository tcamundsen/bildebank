from __future__ import annotations

import json
import os
import re
import sqlite3
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any, Iterable

from .value_parsing import optional_int, require_int


DB_FILENAME = ".bilder.sqlite3"
SCHEMA_VERSION = 11
GPS_ERROR_EXIFTOOL = "exiftool_error"
GPS_ERROR_FILE_MISSING = "file_missing"
TAG_KIND_USER = "user"
TAG_KIND_SYSTEM = "system"
SYSTEM_TAG_OUT_OF_FOCUS = "Ute av fokus"
SYSTEM_TAG_NAMES = (SYSTEM_TAG_OUT_OF_FOCUS,)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".m4v", ".mpg", ".mpeg", ".mts", ".m2ts", ".3gp", ".wmv"}
COLLECTION_ID_META_KEY = "collection_id"
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
    *(f"idx_files_{column}" for column in H3_FILE_COLUMNS),
    *(f"idx_files_{column}_browser_order" for column in H3_FILE_COLUMNS),
    "idx_files_gps",
    "idx_file_sources_source_id_id",
    "idx_file_sources_source_id_file_id",
    "idx_errors_unresolved_stage_id",
)
_PREPARED_TARGETS: set[str] = set()


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


def path_key(path: Path) -> str:
    resolved = path.resolve()
    value = os.path.normpath(str(resolved))
    if os.name == "nt":
        value = value.lower()
    return value


def relative_path(path: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    normalized = os.path.normpath(str(candidate))
    return Path(normalized)


def relative_path_key(path: Path) -> str:
    value = os.path.normpath(str(relative_path(path))).replace("\\", "/")
    if value in {".", ""}:
        return ""
    if os.name == "nt":
        value = value.lower()
    return value


def target_relative_path(target: Path, path: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(target.resolve())
        except ValueError as exc:
            raise ValueError(f"Filen ligger utenfor bildesamlingen: {path}") from exc
    return relative_path(candidate)


def target_relative_path_key(target: Path, path: Path) -> str:
    return relative_path_key(target_relative_path(target, path))


def absolute_target_path(target: Path, path: Path | str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else target / candidate


def db_path_for_target(target: Path) -> Path:
    return target / DB_FILENAME


def find_target(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if db_path_for_target(candidate).exists():
            return candidate
    return None


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
    internal_repairs: tuple[str, ...] = ()


def connect(target: Path, *, require_current: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path_for_target(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        if require_current:
            require_current_schema(conn, full=str(target.resolve()) not in _PREPARED_TARGETS)
        return conn
    except Exception:
        conn.close()
        raise


def prepare_database(target: Path) -> None:
    conn = connect(target, require_current=False)
    try:
        require_current_schema(conn, full=True)
        _PREPARED_TARGETS.add(str(target.resolve()))
    finally:
        conn.close()


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
                    f"Databasen har schema_version={SCHEMA_VERSION}, men mangler forventet v10-struktur.\n"
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
    if table_exists(conn, "sources"):
        ensure_column(conn, "sources", "superseded_by_source_id", "INTEGER REFERENCES sources(id)")


def ensure_performance_indexes(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "files"):
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


def execute_sql_statements(conn: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            conn.execute(statement)


def drop_performance_indexes(conn: sqlite3.Connection) -> None:
    for name in PERFORMANCE_INDEX_NAMES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


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
            status TEXT NOT NULL DEFAULT 'pending',
            superseded_by_source_id INTEGER REFERENCES sources(id)
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
        if version in {5, 6, 7, 8, 9, 10}:
            if validate:
                validate_current_schema(
                    conn,
                    require_performance_indexes=False,
                    require_manual_date_columns=version >= 9,
                    require_camera_columns=version >= 10,
                    require_pending_file_deletes=version >= 11,
                    require_internal_structure=False,
                )
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
                creates_pending_file_deletes=True,
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
        if version in {5, 6, 7, 8, 9, 10}:
            imported_files = count_rows(conn, "files")
            duplicate_findings = count_rows(conn, "duplicate_findings")
            cleans_gps_errors = has_legacy_gps_errors(conn)
            backfills_h3_10_11 = needs_h3_10_11_backfill(conn)
            try:
                conn.execute("BEGIN IMMEDIATE")
                ensure_compatible_columns(conn)
                set_collection_id(conn)
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
                creates_pending_file_deletes=True,
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
    require_pending_file_deletes: bool = True,
    require_internal_structure: bool = True,
) -> None:
    if not table_exists(conn, "sources"):
        raise ValueError("Databasen mangler tabellen sources.")
    if table_exists(conn, "duplicate_findings"):
        raise ValueError("Databasen inneholder legacy-tabellen duplicate_findings. Kjør bildebank migrate.")
    source_columns = table_columns(conn, "sources")
    if "kind" in source_columns:
        raise ValueError("sources inneholder gammel kind-kolonne. Kjør bildebank migrate.")
    if "name" not in source_columns or not source_name_is_not_null(conn):
        raise ValueError("sources.name er ikke påkrevd. Kjør bildebank migrate.")
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
    if require_pending_file_deletes:
        validate_pending_file_deletes_schema(conn)
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
        insert_or_validate_file_source(
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
            insert_or_validate_file_source(
                conn,
                file_id=int(row["file_id"]),
                source_id=int(row["source_id"]),
                source_path=str(row["source_path"]),
                source_path_key=str(row["source_path_key"]),
                sha256=str(row["sha256"]),
                size_bytes=int(row["size_bytes"]),
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
    rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
    used: set[str] = set()
    named_rows: list[tuple[int, str, str | None, str, str, str | None, str, int | None]] = []
    for row in rows:
        existing_name = row["name"] if "name" in row.keys() else None
        base = str(existing_name).strip() if existing_name else default_source_name(str(row["path"]))
        name = unique_source_name(base, used)
        raw_path_key = row["path_key"] if "path_key" in row.keys() else path_key(Path(str(row["path"])))
        source_path_key = str(raw_path_key) if raw_path_key is not None else None
        raw_imported_at = row["imported_at"]
        imported_at = str(raw_imported_at) if raw_imported_at is not None else None
        raw_superseded_by = row["superseded_by_source_id"] if "superseded_by_source_id" in row.keys() else None
        superseded_by_source_id = optional_int(raw_superseded_by, "superseded_by_source_id")
        named_rows.append(
            (
                int(row["id"]),
                str(row["path"]),
                source_path_key,
                name,
                str(row["added_at"]),
                imported_at,
                str(row["status"]),
                superseded_by_source_id,
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
            status TEXT NOT NULL DEFAULT 'pending',
            superseded_by_source_id INTEGER REFERENCES sources(id)
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO sources_v4(
            id, path, path_key, name, added_at, imported_at, status, superseded_by_source_id
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
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


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = table_columns(conn, table)
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_collection_id(conn: sqlite3.Connection) -> str:
    value = get_meta(conn, COLLECTION_ID_META_KEY)
    if value is None:
        value = str(uuid.uuid4())
        set_meta(conn, COLLECTION_ID_META_KEY, value)
        return value
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"Ugyldig collection_id i databasen: {value}") from exc
    if normalized != value:
        set_meta(conn, COLLECTION_ID_META_KEY, normalized)
    return normalized


def log_command(conn: sqlite3.Connection, command: str, args: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO command_log(command, args_json) VALUES(?, ?)",
        (command, json.dumps(args, ensure_ascii=False, sort_keys=True)),
    )


@dataclass(frozen=True)
class Source:
    id: int
    path: Path
    path_key: str | None
    name: str
    imported_at: str | None
    status: str
    superseded_by_source_id: int | None


@dataclass(frozen=True)
class UnimportPlan:
    source: Source
    source_file_count: int
    active_remove_count: int
    active_keep_count: int
    file_ids_to_delete: tuple[int, ...]
    target_paths_to_delete: tuple[Path, ...]
    target_paths_to_keep: tuple[Path, ...]


def row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=int(row["id"]),
        path=Path(str(row["path"])),
        path_key=row["path_key"],
        name=str(row["name"]),
        imported_at=row["imported_at"],
        status=row["status"],
        superseded_by_source_id=row["superseded_by_source_id"],
    )


def get_sources(conn: sqlite3.Connection) -> list[Source]:
    return [row_to_source(row) for row in conn.execute("SELECT * FROM sources ORDER BY id")]


def get_source(conn: sqlite3.Connection, source_id: int) -> Source:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"Fant ikke kilde #{source_id}")
    return row_to_source(row)


def find_source_by_name(conn: sqlite3.Connection, name: str) -> Source | None:
    row = conn.execute(
        "SELECT * FROM sources WHERE name = ?",
        (name,),
    ).fetchone()
    return row_to_source(row) if row is not None else None


def source_file_sources(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT
                file_sources.id,
                file_sources.file_id,
                file_sources.source_path,
                file_sources.source_path_key,
                file_sources.sha256,
                file_sources.size_bytes,
                files.target_path,
                files.deleted_at
            FROM file_sources
            JOIN files ON files.id = file_sources.file_id
            WHERE file_sources.source_id = ?
            ORDER BY file_sources.id
            """,
            (source_id,),
        )
    )


def add_named_source(conn: sqlite3.Connection, path: Path, name: str) -> int:
    existing = conn.execute(
        "SELECT id, path, imported_at FROM sources WHERE name = ?",
        (name,),
    ).fetchone()
    if existing is not None and existing["imported_at"] is not None:
        raise ValueError(
            f"Kilde med navn {name!r} er allerede importert som "
            f"{existing['path']}. Bruk et nytt --name hvis dette er en annen mappe/import."
        )
    cur = conn.execute(
        """
        INSERT INTO sources(path, path_key, name)
        VALUES(?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET path = excluded.path, path_key = excluded.path_key
        RETURNING id
        """,
        (str(path.resolve()), path_key(path), name),
    )
    return int(cur.fetchone()["id"])


def find_file_by_hash(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM files WHERE sha256 = ? ORDER BY id LIMIT 1",
        (sha256,),
    ).fetchone()


def files_by_hash(conn: sqlite3.Connection, sha256: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM files
            WHERE sha256 = ?
            ORDER BY deleted_at IS NOT NULL, id
            """,
            (sha256,),
        )
    )


def get_file_source_for_source_path(
    conn: sqlite3.Connection, source_id: int, source_path_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM file_sources WHERE source_id = ? AND source_path_key = ?",
        (source_id, source_path_key),
    ).fetchone()


def insert_imported_file(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    source_path: Path,
    target_root: Path,
    target_path: Path,
    original_filename: str,
    stored_filename: str,
    sha256: str,
    size_bytes: int,
    taken_date: str | None,
    date_source: str,
    name_conflict: bool,
    camera_make: str | None = None,
    camera_model: str | None = None,
) -> int:
    try:
        cur = conn.execute(
            """
            INSERT INTO files(
                target_path, target_path_key, original_filename, stored_filename, sha256,
                size_bytes, taken_date, date_source, name_conflict, camera_make, camera_model
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                target_relative_path(target_root, target_path).as_posix(),
                target_relative_path_key(target_root, target_path),
                original_filename,
                stored_filename,
                sha256,
                size_bytes,
                taken_date,
                date_source,
                1 if name_conflict else 0,
                camera_make,
                camera_model,
            ),
        )
        file_id = int(cur.fetchone()["id"])
        insert_or_validate_file_source(
            conn,
            file_id=file_id,
            source_id=source_id,
            source_path=str(source_path.resolve()),
            source_path_key=path_key(source_path),
            sha256=sha256,
            size_bytes=size_bytes,
        )
        return file_id
    except sqlite3.IntegrityError as exc:
        raise sqlite3.IntegrityError(
            f"Kunne ikke registrere importert fil i databasen: {exc}"
        ) from exc


def insert_duplicate(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    source_path: Path,
    matched_file_id: int,
    sha256: str,
    size_bytes: int,
) -> None:
    insert_or_validate_file_source(
        conn,
        file_id=matched_file_id,
        source_id=source_id,
        source_path=str(source_path.resolve()),
        source_path_key=path_key(source_path),
        sha256=sha256,
        size_bytes=size_bytes,
    )


def insert_or_validate_file_source(
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


def mark_source_imported(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute(
        "UPDATE sources SET imported_at = CURRENT_TIMESTAMP, status = 'imported' WHERE id = ?",
        (source_id,),
    )


def mark_source_error(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("UPDATE sources SET status = 'error' WHERE id = ?", (source_id,))


def mark_sources_superseded(
    conn: sqlite3.Connection, *, source_ids: Iterable[int], superseded_by_source_id: int
) -> None:
    ids = list(source_ids)
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"""
        UPDATE sources
        SET status = 'superseded',
            superseded_by_source_id = ?
        WHERE id IN ({placeholders})
        """,
        [superseded_by_source_id, *ids],
    )


def build_unimport_plan(conn: sqlite3.Connection, target: Path, source: Source) -> UnimportPlan:
    rows = source_file_sources(conn, source.id)
    source_counts: dict[int, int] = {}
    for row in rows:
        file_id = int(row["file_id"])
        source_counts[file_id] = source_counts.get(file_id, 0) + 1
    total_sources_by_file_id = unimport_total_sources_by_file_id(conn, source.id)

    active_remove_file_ids: set[int] = set()
    active_keep_file_ids: set[int] = set()
    target_paths_to_delete: list[Path] = []
    target_paths_to_keep: list[Path] = []
    seen_delete_file_ids: set[int] = set()
    seen_keep_file_ids: set[int] = set()

    for row in rows:
        file_id = int(row["file_id"])
        total_sources = total_sources_by_file_id[file_id]
        if total_sources == source_counts[file_id]:
            if row["deleted_at"] is None:
                active_remove_file_ids.add(file_id)
            if file_id not in seen_delete_file_ids:
                target_paths_to_delete.append(absolute_target_path(target, Path(str(row["target_path"]))))
                seen_delete_file_ids.add(file_id)
        else:
            if row["deleted_at"] is None:
                active_keep_file_ids.add(file_id)
            if file_id not in seen_keep_file_ids:
                target_paths_to_keep.append(absolute_target_path(target, Path(str(row["target_path"]))))
                seen_keep_file_ids.add(file_id)

    return UnimportPlan(
        source=source,
        source_file_count=len(rows),
        active_remove_count=len(active_remove_file_ids),
        active_keep_count=len(active_keep_file_ids),
        file_ids_to_delete=tuple(sorted(seen_delete_file_ids)),
        target_paths_to_delete=tuple(target_paths_to_delete),
        target_paths_to_keep=tuple(target_paths_to_keep),
    )


def unimport_total_sources_by_file_id(conn: sqlite3.Connection, source_id: int) -> dict[int, int]:
    return {
        int(row["file_id"]): int(row["total_sources"])
        for row in conn.execute(
            """
            SELECT file_sources.file_id, COUNT(all_sources.id) AS total_sources
            FROM file_sources
            JOIN file_sources all_sources ON all_sources.file_id = file_sources.file_id
            WHERE file_sources.source_id = ?
            GROUP BY file_sources.file_id
            """,
            (source_id,),
        )
    }


def apply_unimport(conn: sqlite3.Connection, plan: UnimportPlan) -> None:
    source_id = plan.source.id
    conn.execute(
        """
        UPDATE sources
        SET status = 'imported',
            superseded_by_source_id = NULL
        WHERE superseded_by_source_id = ?
          AND imported_at IS NOT NULL
        """,
        (source_id,),
    )
    conn.execute("DELETE FROM errors WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM file_sources WHERE source_id = ?", (source_id,))
    if plan.file_ids_to_delete:
        placeholders = ",".join("?" for _ in plan.file_ids_to_delete)
        conn.execute(
            f"DELETE FROM file_tags WHERE file_id IN ({placeholders})",
            plan.file_ids_to_delete,
        )
        conn.execute(
            f"DELETE FROM files WHERE id IN ({placeholders})",
            plan.file_ids_to_delete,
        )
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if table not in {"sources", "files", "duplicate_findings", "file_sources", "errors"}:
        raise ValueError(f"Unsupported table: {table}")
    if not table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def duplicate_source_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COALESCE(SUM(source_count - 1), 0)
            FROM (
                SELECT COUNT(*) AS source_count
                FROM file_sources
                GROUP BY file_id
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
    )


def error_count(conn: sqlite3.Connection, *, include_resolved: bool = False) -> int:
    sql = "SELECT COUNT(*) FROM errors"
    if not include_resolved:
        sql += " WHERE resolved_at IS NULL"
    return int(conn.execute(sql).fetchone()[0])


def status_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int] | int]:
    media = {"bilder": 0, "videoer": 0}
    for row in conn.execute("SELECT stored_filename FROM files WHERE deleted_at IS NULL"):
        suffix = Path(str(row["stored_filename"])).suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            media["videoer"] += 1
        else:
            media["bilder"] += 1

    date_sources = {
        str(row["date_source"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT date_source, COUNT(*) AS count
            FROM files
            WHERE deleted_at IS NULL
            GROUP BY date_source
            """
        )
    }
    total = sum(media.values())
    return {"total": total, "media": media, "date_sources": date_sources}


def name_conflicts(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        WITH primary_sources AS (
            SELECT
                file_sources.*,
                ROW_NUMBER() OVER (
                    PARTITION BY file_id
                    ORDER BY id
                ) AS source_rank
            FROM file_sources
        )
        SELECT
            primary_sources.source_path,
            files.target_path,
            files.original_filename,
            files.stored_filename
        FROM files
        JOIN primary_sources ON primary_sources.file_id = files.id
            AND primary_sources.source_rank = 1
        WHERE files.name_conflict = 1
          AND files.deleted_at IS NULL
        ORDER BY files.imported_at, files.id
        """
    )


def file_by_target_path(conn: sqlite3.Connection, target: Path, target_path: Path) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM files WHERE target_path_key = ?",
        (target_relative_path_key(target, target_path),),
    ).fetchone()


def file_sources_by_target_path(conn: sqlite3.Connection, target: Path, target_path: Path) -> list[sqlite3.Row]:
    return list(
        conn.execute(
        """
        SELECT
            files.id AS file_id,
            file_sources.source_id,
            file_sources.source_path,
            files.target_path,
            files.original_filename,
            files.stored_filename,
            files.sha256,
            files.size_bytes,
            files.taken_date,
            files.date_source,
            files.manual_date_from,
            files.manual_date_to,
            files.manual_date_note,
            files.name_conflict,
            files.imported_at AS file_imported_at,
            sources.path AS source_root,
            sources.name AS source_name,
            sources.status AS source_status,
            sources.imported_at AS source_imported_at
        FROM files
        JOIN file_sources ON file_sources.file_id = files.id
        JOIN sources ON sources.id = file_sources.source_id
        WHERE files.target_path_key = ?
        ORDER BY
            file_sources.id
        """,
        (target_relative_path_key(target, target_path),),
        )
    )


def files_by_original_filename(
    conn: sqlite3.Connection, original_filename: str
) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        WITH primary_sources AS (
            SELECT
                file_sources.*,
                ROW_NUMBER() OVER (
                    PARTITION BY file_id
                    ORDER BY id
                ) AS source_rank
            FROM file_sources
        )
        SELECT
            files.id,
            primary_sources.source_id,
            primary_sources.source_path,
            files.target_path,
            files.original_filename,
            files.stored_filename,
            files.sha256,
            files.size_bytes,
            files.taken_date,
            files.date_source,
            files.name_conflict,
            files.imported_at
        FROM files
        JOIN primary_sources ON primary_sources.file_id = files.id
            AND primary_sources.source_rank = 1
        WHERE original_filename = ?
          AND deleted_at IS NULL
        ORDER BY files.imported_at, files.id
        """,
        (original_filename,),
    )


def conflict_candidate_files(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        WITH primary_sources AS (
            SELECT
                file_sources.*,
                ROW_NUMBER() OVER (
                    PARTITION BY file_id
                    ORDER BY id
                ) AS source_rank
            FROM file_sources
        )
        SELECT
            files.id,
            primary_sources.source_id,
            primary_sources.source_path,
            files.target_path,
            files.original_filename,
            files.stored_filename,
            files.sha256,
            files.size_bytes,
            files.taken_date,
            files.date_source,
            files.name_conflict,
            files.view_rotation_degrees,
            files.imported_at
        FROM files
        JOIN primary_sources ON primary_sources.file_id = files.id
            AND primary_sources.source_rank = 1
        WHERE deleted_at IS NULL
        ORDER BY files.original_filename, files.imported_at, files.id
        """
    )


def metadata_refresh_files(conn: sqlite3.Connection, *, rescan: bool = False) -> Iterable[sqlite3.Row]:
    date_filter = "" if rescan else "AND date_source != 'metadata'"
    return conn.execute(
        f"""
        WITH primary_sources AS (
            SELECT
                file_sources.*,
                ROW_NUMBER() OVER (
                    PARTITION BY file_id
                    ORDER BY id
                ) AS source_rank
            FROM file_sources
        )
        SELECT
            files.id,
            primary_sources.source_path,
            files.target_path,
            files.taken_date,
            files.date_source,
            files.sha256,
            files.stored_filename
        FROM files
        JOIN primary_sources ON primary_sources.file_id = files.id
            AND primary_sources.source_rank = 1
        WHERE deleted_at IS NULL
          {date_filter}
        ORDER BY files.date_source, files.taken_date, files.target_path
        """
    )


def non_metadata_files(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return metadata_refresh_files(conn)


def deleted_files(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        WITH primary_sources AS (
            SELECT
                file_sources.*,
                ROW_NUMBER() OVER (
                    PARTITION BY file_id
                    ORDER BY id
                ) AS source_rank
            FROM file_sources
        )
        SELECT
            files.id,
            primary_sources.source_path,
            files.target_path,
            files.deleted_original_target_path,
            files.deleted_at,
            files.stored_filename,
            files.size_bytes,
            files.taken_date,
            files.date_source,
            files.sha256
        FROM files
        JOIN primary_sources ON primary_sources.file_id = files.id
            AND primary_sources.source_rank = 1
        WHERE deleted_at IS NOT NULL
        ORDER BY files.deleted_at, files.deleted_original_target_path, files.target_path
        """
    )


def browser_files(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT
            id, target_path, stored_filename, taken_date, date_source,
            manual_date_from, manual_date_to, manual_date_note,
            camera_make, camera_model,
            size_bytes, view_rotation_degrees
        FROM files
        WHERE deleted_at IS NULL
        ORDER BY {BROWSER_DATE_ORDER_SQL}, target_path
        """
    )


def normalize_tag_name(name: str) -> str:
    normalized = " ".join(name.strip().split())
    if not normalized:
        raise ValueError("Taggnavn kan ikke være tomt.")
    if len(normalized) > 80:
        raise ValueError("Taggnavn kan være maks 80 tegn.")
    return normalized


def tag_name_key(name: str) -> str:
    return normalize_tag_name(name).casefold()


def tag_kind_for_name(name: str) -> str:
    name_key = tag_name_key(name)
    if name_key in {tag_name_key(system_name) for system_name in SYSTEM_TAG_NAMES}:
        return TAG_KIND_SYSTEM
    return TAG_KIND_USER


def is_system_tag_name(name: str) -> bool:
    return tag_kind_for_name(name) == TAG_KIND_SYSTEM


def ensure_tag(conn: sqlite3.Connection, name: str) -> int:
    clean_name = normalize_tag_name(name)
    name_key = clean_name.casefold()
    kind = tag_kind_for_name(clean_name)
    row = conn.execute("SELECT id, kind FROM tags WHERE name_key = ?", (name_key,)).fetchone()
    if row is not None:
        if kind == TAG_KIND_SYSTEM and row["kind"] != TAG_KIND_SYSTEM:
            conn.execute("UPDATE tags SET name = ?, kind = ? WHERE id = ?", (clean_name, TAG_KIND_SYSTEM, int(row["id"])))
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO tags(name, name_key, kind) VALUES(?, ?, ?)",
        (clean_name, name_key, kind),
    )
    tag_id = optional_int(cursor.lastrowid, "tag-id")
    if tag_id is None:
        raise ValueError("Databasen returnerte ikke id for den nye taggen.")
    return tag_id


def create_user_tag(conn: sqlite3.Connection, name: str) -> int:
    clean_name = normalize_tag_name(name)
    if is_system_tag_name(clean_name):
        raise ValueError("Systemtagger kan ikke opprettes som brukertagger.")
    row = conn.execute("SELECT id FROM tags WHERE name_key = ?", (tag_name_key(clean_name),)).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO tags(name, name_key, kind) VALUES(?, ?, ?)",
        (clean_name, tag_name_key(clean_name), TAG_KIND_USER),
    )
    tag_id = optional_int(cursor.lastrowid, "tag-id")
    if tag_id is None:
        raise ValueError("Databasen returnerte ikke id for den nye taggen.")
    return tag_id


def rename_user_tag(conn: sqlite3.Connection, *, tag_id: int, new_name: str) -> str:
    clean_name = normalize_tag_name(new_name)
    if is_system_tag_name(clean_name):
        raise ValueError("Brukertagger kan ikke endres til systemtagg-navn.")
    row = conn.execute("SELECT id, kind FROM tags WHERE id = ?", (tag_id,)).fetchone()
    if row is None:
        raise ValueError("Taggen finnes ikke.")
    if row["kind"] == TAG_KIND_SYSTEM:
        raise ValueError("Systemtagger kan ikke endres.")
    name_key = tag_name_key(clean_name)
    conflict = conn.execute("SELECT id FROM tags WHERE name_key = ? AND id <> ?", (name_key, tag_id)).fetchone()
    if conflict is not None:
        raise ValueError("Det finnes allerede en tagg med dette navnet.")
    conn.execute("UPDATE tags SET name = ?, name_key = ? WHERE id = ?", (clean_name, name_key, tag_id))
    return clean_name


def delete_user_tag(conn: sqlite3.Connection, *, tag_id: int) -> str:
    row = conn.execute("SELECT name, kind FROM tags WHERE id = ?", (tag_id,)).fetchone()
    if row is None:
        raise ValueError("Taggen finnes ikke.")
    if row["kind"] == TAG_KIND_SYSTEM:
        raise ValueError("Systemtagger kan ikke slettes.")
    name = str(row["name"])
    conn.execute("DELETE FROM file_tags WHERE tag_id = ?", (tag_id,))
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    return name


def tag_file(conn: sqlite3.Connection, *, file_id: int, tag_name: str) -> bool:
    if conn.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone() is None:
        raise ValueError(f"Filen finnes ikke i importdatabasen: #{file_id}")
    tag_id = ensure_tag(conn, tag_name)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO file_tags(file_id, tag_id) VALUES(?, ?)",
        (file_id, tag_id),
    )
    return cursor.rowcount > 0


def set_file_tag(conn: sqlite3.Connection, *, file_id: int, tag_name: str, tagged: bool) -> bool:
    if tagged:
        return tag_file(conn, file_id=file_id, tag_name=tag_name)
    return untag_file(conn, file_id=file_id, tag_name=tag_name)


def untag_file(conn: sqlite3.Connection, *, file_id: int, tag_name: str) -> bool:
    row = conn.execute("SELECT id, kind FROM tags WHERE name_key = ?", (tag_name_key(tag_name),)).fetchone()
    if row is None:
        return False
    cursor = conn.execute(
        "DELETE FROM file_tags WHERE file_id = ? AND tag_id = ?",
        (file_id, int(row["id"])),
    )
    if row["kind"] != TAG_KIND_SYSTEM:
        conn.execute(
            """
            DELETE FROM tags
            WHERE id = ?
              AND NOT EXISTS (SELECT 1 FROM file_tags WHERE tag_id = ?)
            """,
            (int(row["id"]), int(row["id"])),
        )
    return cursor.rowcount > 0


def tags(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT tags.id, tags.name, tags.name_key, tags.kind, tags.created_at, COUNT(file_tags.file_id) AS file_count
        FROM tags
        LEFT JOIN file_tags ON file_tags.tag_id = tags.id
        GROUP BY tags.id
        ORDER BY CASE tags.kind WHEN 'system' THEN 0 ELSE 1 END, tags.name_key
        """
    )


def tags_for_file(conn: sqlite3.Connection, file_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT tags.id, tags.name, tags.name_key, tags.kind
            FROM tags
            JOIN file_tags ON file_tags.tag_id = tags.id
            WHERE file_tags.file_id = ?
            ORDER BY tags.name_key
            """,
            (file_id,),
        )
    )


def tagged_files(conn: sqlite3.Connection, tag_name: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            f"""
            SELECT files.id, files.target_path, files.target_path_key, files.stored_filename,
                   files.taken_date, files.date_source, files.size_bytes, files.view_rotation_degrees,
                   files.camera_make, files.camera_model,
                   files.gps_lat, files.gps_lon, files.gps_source, files.media_width, files.media_height,
                   files.media_orientation, files.media_metadata_mtime_ns, {H3_FILE_COLUMNS_SQL}
            FROM files
            JOIN file_tags ON file_tags.file_id = files.id
            JOIN tags ON tags.id = file_tags.tag_id
            WHERE files.deleted_at IS NULL
              AND tags.name_key = ?
            ORDER BY {BROWSER_DATE_ORDER_SQL}, files.target_path
            """,
            (tag_name_key(tag_name),),
        )
    )


def geo_scan_files(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    only_missing: bool = False,
    override_manual_h3: bool = False,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    where = ["deleted_at IS NULL"]
    if not override_manual_h3:
        where.append("(gps_source IS NULL OR gps_source != 'manual-h3')")
    if only_missing:
        where.append("gps_lat IS NULL")
        where.append("gps_lon IS NULL")
        where.append("gps_scanned_at IS NULL")
        where.append("gps_error IS NULL")
    elif not force:
        missing_or_unscanned = "(gps_scanned_at IS NULL OR gps_lat IS NULL OR gps_lon IS NULL)"
        if override_manual_h3:
            where.append(f"({missing_or_unscanned} OR gps_source = 'manual-h3')")
        else:
            where.append(missing_or_unscanned)
    sql = """
        SELECT id, target_path
        FROM files
        WHERE {where_sql}
        ORDER BY target_path
    """.format(where_sql=" AND ".join(where))
    params: list[int] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def update_file_gps(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    gps_lat: float | None,
    gps_lon: float | None,
    gps_alt: float | None,
    h3_cells: dict[str, str | None] | None,
    gps_source: str | None,
    gps_error: str | None,
) -> None:
    cells = h3_cells or {}
    assignments = ",\n            ".join(f"{column} = ?" for column in H3_FILE_COLUMNS)
    conn.execute(
        f"""
        UPDATE files
        SET gps_lat = ?,
            gps_lon = ?,
            gps_alt = ?,
            {assignments},
            gps_source = ?,
            gps_scanned_at = CURRENT_TIMESTAMP,
            gps_error = ?
        WHERE id = ?
        """,
        (
            gps_lat,
            gps_lon,
            gps_alt,
            *(cells.get(column) for column in H3_FILE_COLUMNS),
            gps_source,
            gps_error,
            file_id,
        ),
    )


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


def set_file_manual_h3_location(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    h3_cells: dict[str, str | None],
) -> bool:
    row = conn.execute(
        """
        SELECT id, deleted_at, gps_lat, gps_lon
        FROM files
        WHERE id = ?
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        return False
    if row["deleted_at"] is not None:
        raise ValueError("Filen er markert som slettet.")
    if row["gps_lat"] is not None or row["gps_lon"] is not None:
        raise ValueError("Filen har GPS-lokasjon og kan ikke få manuell H3-celle.")
    cells = h3_cells or {}
    assignments = ",\n            ".join(f"{column} = ?" for column in H3_FILE_COLUMNS)
    conn.execute(
        f"""
        UPDATE files
        SET gps_lat = NULL,
            gps_lon = NULL,
            gps_alt = NULL,
            {assignments},
            gps_source = 'manual-h3',
            gps_scanned_at = CURRENT_TIMESTAMP,
            gps_error = NULL
        WHERE id = ?
        """,
        (
            *(cells.get(column) for column in H3_FILE_COLUMNS),
            file_id,
        ),
    )
    return True


def remove_file_manual_h3_location(
    conn: sqlite3.Connection,
    *,
    file_id: int,
) -> bool:
    row = conn.execute(
        """
        SELECT id, deleted_at, gps_source
        FROM files
        WHERE id = ?
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        return False
    if row["deleted_at"] is not None:
        raise ValueError("Filen er markert som slettet.")
    if str(row["gps_source"] or "") != "manual-h3":
        raise ValueError("Filen har ikke manuell H3-lokasjon.")
    assignments = ",\n            ".join(f"{column} = NULL" for column in H3_FILE_COLUMNS)
    conn.execute(
        f"""
        UPDATE files
        SET gps_lat = NULL,
            gps_lon = NULL,
            gps_alt = NULL,
            {assignments},
            gps_source = NULL,
            gps_scanned_at = NULL,
            gps_error = NULL
        WHERE id = ?
        """,
        (file_id,),
    )
    return True


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


def geo_stats(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN gps_scanned_at IS NOT NULL THEN 1 ELSE 0 END) AS scanned,
            SUM(CASE WHEN gps_lat IS NOT NULL AND gps_lon IS NOT NULL THEN 1 ELSE 0 END) AS with_gps,
            SUM(CASE WHEN gps_scanned_at IS NOT NULL
                      AND gps_lat IS NULL
                      AND gps_lon IS NULL
                      AND gps_error IS NULL THEN 1 ELSE 0 END) AS without_gps,
            SUM(CASE WHEN gps_source = 'manual-h3' THEN 1 ELSE 0 END) AS manual_h3,
            SUM(CASE WHEN gps_error IS NOT NULL THEN 1 ELSE 0 END) AS errors
        FROM files
        WHERE deleted_at IS NULL
        """
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "scanned": int(row["scanned"] or 0),
        "with_gps": int(row["with_gps"] or 0),
        "without_gps": int(row["without_gps"] or 0),
        "manual_h3": int(row["manual_h3"] or 0),
        "errors": int(row["errors"] or 0),
    }


def geo_areas(
    conn: sqlite3.Connection,
    *,
    column: str,
    min_count: int,
    limit: int,
) -> list[sqlite3.Row]:
    validate_h3_column(column)
    return list(
        conn.execute(
            f"""
            SELECT files.{column} AS h3_cell, COUNT(*) AS count, geo_place_names.name AS name
            FROM files
            LEFT JOIN geo_place_names ON geo_place_names.h3_cell = files.{column}
            WHERE files.{column} IS NOT NULL
              AND files.deleted_at IS NULL
            GROUP BY files.{column}
            HAVING COUNT(*) >= ?
            ORDER BY count DESC, geo_place_names.name, files.{column}
            LIMIT ?
            """,
            (min_count, limit),
        )
    )


GEO_FILE_COLUMNS = (
    "id, target_path, target_path_key, stored_filename, taken_date, date_source, "
    "manual_date_from, manual_date_to, manual_date_note, "
    "camera_make, camera_model, "
    "size_bytes, view_rotation_degrees, gps_lat, gps_lon, gps_alt, gps_source, "
    f"{H3_FILE_COLUMNS_SQL}"
)


def validate_h3_column(column: str) -> None:
    if column not in H3_FILE_COLUMN_SET:
        raise ValueError(f"Ustøttet H3-kolonne: {column}")


def geo_area_files(
    conn: sqlite3.Connection,
    *,
    column: str,
    h3_cell: str,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    validate_h3_column(column)
    sql = f"""
        SELECT {GEO_FILE_COLUMNS}
        FROM files
        WHERE {column} = ?
          AND deleted_at IS NULL
        ORDER BY {BROWSER_DATE_ORDER_SQL}, target_path_key
    """
    params: list[str | int] = [h3_cell]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def geo_place_count(conn: sqlite3.Connection, *, cells_by_column: list[tuple[str, str]]) -> int:
    conditions, params = geo_place_where_clause(cells_by_column)
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT id) AS count
        FROM files
        WHERE deleted_at IS NULL
          AND ({conditions})
        """,
        params,
    ).fetchone()
    return int(row["count"] or 0)


def geo_place_files(
    conn: sqlite3.Connection,
    *,
    cells_by_column: list[tuple[str, str]],
    limit: int | None = None,
) -> list[sqlite3.Row]:
    conditions, params = geo_place_where_clause(cells_by_column)
    sql = f"""
        SELECT DISTINCT {GEO_FILE_COLUMNS}
        FROM files
        WHERE deleted_at IS NULL
          AND ({conditions})
        ORDER BY {BROWSER_DATE_ORDER_SQL}, target_path_key
    """
    query_params: list[str | int] = list(params)
    if limit is not None:
        sql += " LIMIT ?"
        query_params.append(limit)
    return list(conn.execute(sql, query_params))


def geo_place_where_clause(cells_by_column: list[tuple[str, str]]) -> tuple[str, tuple[str, ...]]:
    clean_cells: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for column, h3_cell in cells_by_column:
        validate_h3_column(column)
        clean_cell = h3_cell.strip()
        if not clean_cell:
            continue
        key = (column, clean_cell)
        if key not in seen:
            clean_cells.append(key)
            seen.add(key)
    if not clean_cells:
        raise ValueError("Geo-sted mangler H3-celler.")
    conditions = " OR ".join(f"{column} = ?" for column, _h3_cell in clean_cells)
    return conditions, tuple(h3_cell for _column, h3_cell in clean_cells)


def geo_child_areas(
    conn: sqlite3.Connection,
    *,
    parent_column: str,
    parent_h3_cell: str,
    child_column: str,
) -> list[sqlite3.Row]:
    validate_h3_column(parent_column)
    validate_h3_column(child_column)
    return list(
        conn.execute(
            f"""
            SELECT files.{child_column} AS h3_cell, COUNT(*) AS count, geo_place_names.name AS name
            FROM files
            LEFT JOIN geo_place_names ON geo_place_names.h3_cell = files.{child_column}
            WHERE files.{parent_column} = ?
              AND files.{child_column} IS NOT NULL
              AND files.deleted_at IS NULL
            GROUP BY files.{child_column}
            ORDER BY count DESC, COALESCE(geo_place_names.name, files.{child_column}), files.{child_column}
            """,
            (parent_h3_cell,),
        )
    )


def geo_place_name(conn: sqlite3.Connection, h3_cell: str) -> str | None:
    row = conn.execute("SELECT name FROM geo_place_names WHERE h3_cell = ?", (h3_cell,)).fetchone()
    return None if row is None else str(row["name"])


def geo_place_names(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT h3_cell, name
            FROM geo_place_names
            ORDER BY name COLLATE NOCASE, h3_cell
            """
        )
    )


def set_geo_place_name(conn: sqlite3.Connection, h3_cell: str, name: str) -> str | None:
    clean_name = name.strip()
    if not h3_cell.strip():
        raise ValueError("H3-celle mangler.")
    if not clean_name:
        conn.execute("DELETE FROM geo_place_names WHERE h3_cell = ?", (h3_cell,))
        return None
    conn.execute(
        """
        INSERT INTO geo_place_names(h3_cell, name, updated_at)
        VALUES(?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(h3_cell) DO UPDATE SET
            name = excluded.name,
            updated_at = CURRENT_TIMESTAMP
        """,
        (h3_cell, clean_name),
    )
    return clean_name


def custom_geo_places(conn: sqlite3.Connection) -> list[dict[str, object]]:
    places = list(conn.execute("SELECT slug, name FROM geo_places ORDER BY name, slug"))
    if not places:
        return []
    cells_by_slug = custom_geo_place_cells_by_slug(conn)
    return [
        {
            "slug": str(row["slug"]),
            "name": str(row["name"]),
            "h3_cells": tuple(cells_by_slug.get(str(row["slug"]), ())),
        }
        for row in places
    ]


def custom_geo_place(conn: sqlite3.Connection, slug: str) -> dict[str, object] | None:
    clean_slug = slug.strip().lower()
    row = conn.execute("SELECT slug, name FROM geo_places WHERE slug = ?", (clean_slug,)).fetchone()
    if row is None:
        return None
    cells = tuple(row["h3_cell"] for row in custom_geo_place_cells(conn, clean_slug))
    return {"slug": str(row["slug"]), "name": str(row["name"]), "h3_cells": cells}


def custom_geo_place_cells_by_slug(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute(
        """
        SELECT slug, h3_cell
        FROM geo_place_cells
        ORDER BY slug, position, h3_cell
        """
    )
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(str(row["slug"]), []).append(str(row["h3_cell"]))
    return result


def custom_geo_place_cells(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT h3_cell
            FROM geo_place_cells
            WHERE slug = ?
            ORDER BY position, h3_cell
            """,
            (slug,),
        )
    )


def set_custom_geo_place(conn: sqlite3.Connection, *, slug: str, name: str, h3_cells: list[str]) -> None:
    clean_slug = slug.strip().lower()
    clean_name = name.strip()
    if not clean_slug:
        raise ValueError("Slug mangler.")
    if not clean_name:
        raise ValueError("Navn mangler.")
    clean_cells: list[str] = []
    seen: set[str] = set()
    for cell in h3_cells:
        clean_cell = cell.strip()
        if not clean_cell or clean_cell in seen:
            continue
        clean_cells.append(clean_cell)
        seen.add(clean_cell)
    if not clean_cells:
        raise ValueError("Stedet må ha minst én H3-celle.")
    conn.execute(
        """
        INSERT INTO geo_places(slug, name, updated_at)
        VALUES(?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(slug) DO UPDATE SET
            name = excluded.name,
            updated_at = CURRENT_TIMESTAMP
        """,
        (clean_slug, clean_name),
    )
    conn.execute("DELETE FROM geo_place_cells WHERE slug = ?", (clean_slug,))
    conn.executemany(
        """
        INSERT INTO geo_place_cells(slug, h3_cell, position)
        VALUES(?, ?, ?)
        """,
        [(clean_slug, cell, index) for index, cell in enumerate(clean_cells)],
    )


def rename_custom_geo_place(
    conn: sqlite3.Connection,
    *,
    old_slug: str,
    slug: str,
    name: str,
    h3_cells: list[str],
) -> None:
    clean_old_slug = old_slug.strip().lower()
    clean_slug = slug.strip().lower()
    if not clean_old_slug:
        set_custom_geo_place(conn, slug=slug, name=name, h3_cells=h3_cells)
        return
    if clean_old_slug == clean_slug:
        set_custom_geo_place(conn, slug=slug, name=name, h3_cells=h3_cells)
        return
    if conn.execute("SELECT 1 FROM geo_places WHERE slug = ?", (clean_slug,)).fetchone() is not None:
        raise ValueError("Slug er allerede i bruk.")
    if conn.execute("SELECT 1 FROM geo_places WHERE slug = ?", (clean_old_slug,)).fetchone() is None:
        raise ValueError("Stedet som skulle oppdateres finnes ikke.")
    conn.execute("DELETE FROM geo_places WHERE slug = ?", (clean_old_slug,))
    set_custom_geo_place(conn, slug=slug, name=name, h3_cells=h3_cells)


def delete_custom_geo_place(conn: sqlite3.Connection, slug: str) -> None:
    conn.execute("DELETE FROM geo_places WHERE slug = ?", (slug.strip().lower(),))


def geo_missing_files(conn: sqlite3.Connection, *, limit: int, offset: int = 0) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            f"""
            SELECT {GEO_FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND (gps_lat IS NULL OR gps_lon IS NULL)
            ORDER BY {BROWSER_DATE_ORDER_SQL}, target_path_key
            LIMIT ? OFFSET ?
            """,
            (limit, max(0, offset)),
        )
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


def set_manual_date(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    date_from: str,
    date_to: str,
    note: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE files
        SET manual_date_from = ?,
            manual_date_to = ?,
            manual_date_note = ?
        WHERE id = ?
          AND deleted_at IS NULL
        """,
        (date_from, date_to, clean_manual_date_note(note), file_id),
    )


def clear_manual_date(conn: sqlite3.Connection, *, file_id: int) -> None:
    conn.execute(
        """
        UPDATE files
        SET manual_date_from = NULL,
            manual_date_to = NULL,
            manual_date_note = NULL
        WHERE id = ?
          AND deleted_at IS NULL
        """,
        (file_id,),
    )


def clean_manual_date_note(note: str | None) -> str | None:
    if note is None:
        return None
    clean = " ".join(note.strip().split())
    return clean or None


def mark_file_deleted(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_root: Path,
    deleted_path: Path,
    original_target_path: Path,
) -> None:
    conn.execute(
        """
        UPDATE files
        SET target_path = ?,
            target_path_key = ?,
            deleted_at = CURRENT_TIMESTAMP,
            deleted_original_target_path = ?
        WHERE id = ?
          AND deleted_at IS NULL
        """,
        (
            target_relative_path(target_root, deleted_path).as_posix(),
            target_relative_path_key(target_root, deleted_path),
            target_relative_path(target_root, original_target_path).as_posix(),
            file_id,
        ),
    )


def mark_file_undeleted(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_root: Path,
    restored_path: Path,
) -> None:
    conn.execute(
        """
        UPDATE files
        SET target_path = ?,
            target_path_key = ?,
            deleted_at = NULL,
            deleted_original_target_path = NULL
        WHERE id = ?
          AND deleted_at IS NOT NULL
        """,
        (
            target_relative_path(target_root, restored_path).as_posix(),
            target_relative_path_key(target_root, restored_path),
            file_id,
        ),
    )


def update_file_placement(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_root: Path,
    target_path: Path,
    stored_filename: str,
    taken_date: str,
    date_source: str,
    name_conflict: bool,
    camera_make: str | None = None,
    camera_model: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE files
        SET target_path = ?,
            target_path_key = ?,
            stored_filename = ?,
            taken_date = ?,
            date_source = ?,
            name_conflict = ?,
            camera_make = ?,
            camera_model = ?
        WHERE id = ?
        """,
        (
            target_relative_path(target_root, target_path).as_posix(),
            target_relative_path_key(target_root, target_path),
            stored_filename,
            taken_date,
            date_source,
            1 if name_conflict else 0,
            camera_make,
            camera_model,
            file_id,
        ),
    )


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
