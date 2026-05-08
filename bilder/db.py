from __future__ import annotations

import json
import os
import re
import sqlite3
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any, Iterable


DB_FILENAME = ".bilder.sqlite3"
SCHEMA_VERSION = 4
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".m4v", ".mpg", ".mpeg", ".mts", ".m2ts", ".3gp", ".wmv"}


def path_key(path: Path) -> str:
    resolved = path.resolve()
    value = os.path.normpath(str(resolved))
    if os.name == "nt":
        value = value.lower()
    return value


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
    rebuilds_files_without_legacy_source_columns: bool = False
    drops_duplicate_findings: bool = False
    rebuilds_errors_without_source_fk: bool = False
    rebuilds_sources_without_kind: bool = False
    rebuilds_file_sources_without_kind: bool = False


def connect(target: Path, *, require_current: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path_for_target(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if require_current:
        require_current_schema(conn)
    return conn


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


def require_current_schema(conn: sqlite3.Connection) -> None:
    version = schema_version(conn)
    if version == SCHEMA_VERSION:
        try:
            validate_current_schema(conn)
        except ValueError as exc:
            raise SchemaMigrationRequired(
                f"Databasen har schema_version={SCHEMA_VERSION}, men mangler forventet v4-struktur.\n"
                f"{exc}\n"
                "Kjør bildebank migrate før du gjør endringer."
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
    if table_exists(conn, "errors"):
        ensure_column(conn, "errors", "resolved_at", "TEXT")
    if table_exists(conn, "files"):
        ensure_column(conn, "files", "deleted_at", "TEXT")
        ensure_column(conn, "files", "deleted_original_target_path", "TEXT")
    if table_exists(conn, "sources"):
        ensure_column(conn, "sources", "superseded_by_source_id", "INTEGER REFERENCES sources(id)")


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
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        conn.commit()
    finally:
        conn.close()


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
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
            deleted_original_target_path TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);

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

        CREATE TABLE IF NOT EXISTS errors (
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
    ensure_compatible_columns(conn)


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
            validate_current_schema(conn)
            return MigrationPlan(
                current_version=version,
                target_version=SCHEMA_VERSION,
                imported_files=count_rows(conn, "files"),
                duplicate_findings=count_rows(conn, "duplicate_findings"),
                creates_file_sources=False,
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
            validate_current_schema(conn)
            return MigrationPlan(
                current_version=version,
                target_version=SCHEMA_VERSION,
                imported_files=count_rows(conn, "files"),
                duplicate_findings=count_rows(conn, "duplicate_findings"),
                creates_file_sources=False,
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
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            ensure_compatible_columns(conn)
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
            set_meta(conn, "schema_version", str(SCHEMA_VERSION))
            log_command(conn, "migrate", {"from_schema_version": version, "to_schema_version": SCHEMA_VERSION})
            validate_current_schema(conn)
            foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise ValueError(f"foreign_key_check feilet: {foreign_key_errors[0]}")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(f"integrity_check feilet: {integrity}")
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


def validate_current_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "sources"):
        raise ValueError("Databasen mangler tabellen sources.")
    source_columns = table_columns(conn, "sources")
    if "kind" in source_columns:
        raise ValueError("sources inneholder gammel kind-kolonne. Kjør bildebank migrate.")
    if "name" not in source_columns or not source_name_is_not_null(conn):
        raise ValueError("sources.name er ikke påkrevd. Kjør bildebank migrate.")
    validate_file_sources_schema(conn)


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
    named_rows: list[tuple[int, str, str | None, str, str | None, str, int | None]] = []
    for row in rows:
        existing_name = row["name"] if "name" in row.keys() else None
        base = str(existing_name).strip() if existing_name else default_source_name(str(row["path"]))
        name = unique_source_name(base, used)
        named_rows.append(
            (
                int(row["id"]),
                str(row["path"]),
                row["path_key"] if "path_key" in row.keys() else path_key(Path(str(row["path"]))),
                name,
                str(row["added_at"]),
                row["imported_at"],
                str(row["status"]),
                row["superseded_by_source_id"] if "superseded_by_source_id" in row.keys() else None,
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
    target_paths_to_delete: tuple[Path, ...]


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


def get_sources(conn: sqlite3.Connection, *, pending_only: bool = False) -> list[Source]:
    sql = "SELECT * FROM sources"
    if pending_only:
        sql += " WHERE imported_at IS NULL AND status != 'superseded'"
    sql += " ORDER BY id"
    return [row_to_source(row) for row in conn.execute(sql)]


def get_source(conn: sqlite3.Connection, source_id: int) -> Source:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"Fant ikke kilde #{source_id}")
    return row_to_source(row)


def find_sources_by_path(conn: sqlite3.Connection, path: Path) -> list[Source]:
    resolved = path.resolve()
    key = path_key(path)
    rows = conn.execute(
        """
        SELECT *
        FROM sources
        WHERE path_key = ?
           OR (path_key IS NULL AND path = ?)
        ORDER BY id
        """,
        (key, str(resolved)),
    ).fetchall()
    return [row_to_source(row) for row in rows]


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


def active_file_source_count(conn: sqlite3.Connection, source_id: int) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM file_sources
            WHERE file_sources.source_id = ?
            """,
            (source_id,),
        ).fetchone()[0]
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
    target_path: Path,
    original_filename: str,
    stored_filename: str,
    sha256: str,
    size_bytes: int,
    taken_date: str | None,
    date_source: str,
    name_conflict: bool,
) -> int:
    try:
        if "source_id" in table_columns(conn, "files"):
            cur = conn.execute(
                """
                INSERT INTO files(
                    source_id, source_path, source_path_key, target_path, target_path_key,
                    original_filename, stored_filename, sha256, size_bytes, taken_date,
                    date_source, name_conflict
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    source_id,
                    str(source_path.resolve()),
                    path_key(source_path),
                    str(target_path.resolve()),
                    path_key(target_path),
                    original_filename,
                    stored_filename,
                    sha256,
                    size_bytes,
                    taken_date,
                    date_source,
                    1 if name_conflict else 0,
                ),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO files(
                    target_path, target_path_key, original_filename, stored_filename, sha256,
                    size_bytes, taken_date, date_source, name_conflict
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    str(target_path.resolve()),
                    path_key(target_path),
                    original_filename,
                    stored_filename,
                    sha256,
                    size_bytes,
                    taken_date,
                    date_source,
                    1 if name_conflict else 0,
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


def build_unimport_plan(conn: sqlite3.Connection, source: Source) -> UnimportPlan:
    rows = source_file_sources(conn, source.id)
    source_counts: dict[int, int] = {}
    for row in rows:
        file_id = int(row["file_id"])
        source_counts[file_id] = source_counts.get(file_id, 0) + 1

    active_remove_file_ids: set[int] = set()
    active_keep_file_ids: set[int] = set()
    target_paths_to_delete: list[Path] = []
    seen_delete_file_ids: set[int] = set()

    for row in rows:
        if row["deleted_at"] is not None:
            continue
        file_id = int(row["file_id"])
        total_sources = int(
            conn.execute(
                "SELECT COUNT(*) FROM file_sources WHERE file_id = ?",
                (file_id,),
            ).fetchone()[0]
        )
        if total_sources == source_counts[file_id]:
            active_remove_file_ids.add(file_id)
            if file_id not in seen_delete_file_ids:
                target_paths_to_delete.append(Path(str(row["target_path"])))
                seen_delete_file_ids.add(file_id)
        else:
            active_keep_file_ids.add(file_id)

    return UnimportPlan(
        source=source,
        source_file_count=len(rows),
        active_remove_count=len(active_remove_file_ids),
        active_keep_count=len(active_keep_file_ids),
        target_paths_to_delete=tuple(target_paths_to_delete),
    )


def apply_unimport(conn: sqlite3.Connection, plan: UnimportPlan) -> None:
    source_id = plan.source.id
    file_ids_to_delete = [
        int(row["id"])
        for row in conn.execute(
            """
            SELECT files.id
            FROM files
            WHERE NOT EXISTS (
                SELECT 1
                FROM file_sources other_sources
                WHERE other_sources.file_id = files.id
                  AND other_sources.source_id != ?
            )
              AND EXISTS (
                SELECT 1
                FROM file_sources source_rows
                WHERE source_rows.file_id = files.id
                  AND source_rows.source_id = ?
            )
            """,
            (source_id, source_id),
        )
    ]

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
    conn.execute("DELETE FROM file_sources WHERE source_id = ?", (source_id,))
    if file_ids_to_delete:
        placeholders = ",".join("?" for _ in file_ids_to_delete)
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", file_ids_to_delete)
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
        WHERE name_conflict = 1
          AND deleted_at IS NULL
        ORDER BY imported_at, id
        """
    )


def file_by_target_path(conn: sqlite3.Connection, target_path: Path) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM files WHERE target_path_key = ?",
        (path_key(target_path),),
    ).fetchone()


def file_sources_by_target_path(conn: sqlite3.Connection, target_path: Path) -> list[sqlite3.Row]:
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
        (path_key(target_path),),
        )
    )


def file_source_by_target_path(conn: sqlite3.Connection, target_path: Path) -> sqlite3.Row | None:
    rows = file_sources_by_target_path(conn, target_path)
    return rows[0] if rows else None


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
            files.imported_at
        FROM files
        JOIN primary_sources ON primary_sources.file_id = files.id
            AND primary_sources.source_rank = 1
        WHERE deleted_at IS NULL
        ORDER BY files.original_filename, files.imported_at, files.id
        """
    )


def non_metadata_files(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
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
            files.taken_date,
            files.date_source,
            files.sha256,
            files.stored_filename
        FROM files
        JOIN primary_sources ON primary_sources.file_id = files.id
            AND primary_sources.source_rank = 1
        WHERE date_source != 'metadata'
          AND deleted_at IS NULL
        ORDER BY files.date_source, files.taken_date, files.target_path
        """
    )


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
        """
        SELECT target_path, stored_filename, taken_date, date_source, size_bytes
        FROM files
        WHERE deleted_at IS NULL
        ORDER BY taken_date, target_path
        """
    )


def mark_file_deleted(
    conn: sqlite3.Connection,
    *,
    file_id: int,
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
            str(deleted_path.resolve()),
            path_key(deleted_path),
            str(original_target_path.resolve()),
            file_id,
        ),
    )


def update_file_placement(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_path: Path,
    stored_filename: str,
    taken_date: str,
    date_source: str,
    name_conflict: bool,
) -> None:
    conn.execute(
        """
        UPDATE files
        SET target_path = ?,
            target_path_key = ?,
            stored_filename = ?,
            taken_date = ?,
            date_source = ?,
            name_conflict = ?
        WHERE id = ?
        """,
        (
            str(target_path.resolve()),
            path_key(target_path),
            stored_filename,
            taken_date,
            date_source,
            1 if name_conflict else 0,
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
