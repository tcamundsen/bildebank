from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DB_FILENAME = ".bilder.sqlite3"
SCHEMA_VERSION = 1
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


def connect(target: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path_for_target(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn)
    return conn


def migrate_schema(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "errors"):
        ensure_column(conn, "errors", "resolved_at", "TEXT")
    if table_exists(conn, "files"):
        ensure_column(conn, "files", "deleted_at", "TEXT")
        ensure_column(conn, "files", "deleted_original_target_path", "TEXT")


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def init_database(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    conn = connect(target)
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
            kind TEXT NOT NULL CHECK (kind IN ('directory', 'removable')),
            path TEXT NOT NULL,
            path_key TEXT,
            name TEXT,
            added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            imported_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            superseded_by_source_id INTEGER REFERENCES sources(id),
            UNIQUE(kind, path_key),
            UNIQUE(kind, name)
        );

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES sources(id),
            source_path TEXT NOT NULL,
            source_path_key TEXT NOT NULL,
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
            UNIQUE(source_id, source_path_key)
        );

        CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);

        CREATE TABLE IF NOT EXISTS duplicate_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES sources(id),
            source_path TEXT NOT NULL,
            source_path_key TEXT NOT NULL,
            matched_file_id INTEGER NOT NULL REFERENCES files(id),
            sha256 TEXT NOT NULL,
            found_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_id, source_path_key)
        );

        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER REFERENCES sources(id),
            source_path TEXT,
            stage TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        );
        """
    )
    ensure_column(conn, "sources", "superseded_by_source_id", "INTEGER REFERENCES sources(id)")
    ensure_column(conn, "errors", "resolved_at", "TEXT")
    ensure_column(conn, "files", "deleted_at", "TEXT")
    ensure_column(conn, "files", "deleted_original_target_path", "TEXT")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
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
    kind: str
    path: Path
    path_key: str | None
    name: str | None
    imported_at: str | None
    status: str
    superseded_by_source_id: int | None


def row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=int(row["id"]),
        kind=str(row["kind"]),
        path=Path(str(row["path"])),
        path_key=row["path_key"],
        name=row["name"],
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


def add_directory_source(conn: sqlite3.Connection, path: Path) -> int:
    key = path_key(path)
    cur = conn.execute(
        """
        INSERT INTO sources(kind, path, path_key)
        VALUES('directory', ?, ?)
        ON CONFLICT(kind, path_key) DO UPDATE SET path = excluded.path
        RETURNING id
        """,
        (str(path.resolve()), key),
    )
    return int(cur.fetchone()["id"])


def add_removable_source(conn: sqlite3.Connection, path: Path, name: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO sources(kind, path, name)
        VALUES('removable', ?, ?)
        ON CONFLICT(kind, name) DO UPDATE SET path = excluded.path
        RETURNING id
        """,
        (str(path.resolve()), name),
    )
    return int(cur.fetchone()["id"])


def find_file_by_hash(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM files WHERE sha256 = ? ORDER BY id LIMIT 1",
        (sha256,),
    ).fetchone()


def get_file_for_source_path(
    conn: sqlite3.Connection, source_id: int, source_path_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM files WHERE source_id = ? AND source_path_key = ?",
        (source_id, source_path_key),
    ).fetchone()


def get_duplicate_for_source_path(
    conn: sqlite3.Connection, source_id: int, source_path_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM duplicate_findings WHERE source_id = ? AND source_path_key = ?",
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
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO files(
                source_id, source_path, source_path_key, target_path, target_path_key,
                original_filename, stored_filename, sha256, size_bytes, taken_date,
                date_source, name_conflict
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO duplicate_findings(
            source_id, source_path, source_path_key, matched_file_id, sha256
        ) VALUES(?, ?, ?, ?, ?)
        """,
        (source_id, str(source_path.resolve()), path_key(source_path), matched_file_id, sha256),
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


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if table not in {"sources", "files", "duplicate_findings", "errors"}:
        raise ValueError(f"Unsupported table: {table}")
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


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
        SELECT source_path, target_path, original_filename, stored_filename
        FROM files
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


def file_source_by_target_path(conn: sqlite3.Connection, target_path: Path) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            files.id AS file_id,
            files.source_id,
            files.source_path,
            files.target_path,
            files.original_filename,
            files.stored_filename,
            files.sha256,
            files.size_bytes,
            files.taken_date,
            files.date_source,
            files.name_conflict,
            files.imported_at AS file_imported_at,
            sources.kind AS source_kind,
            sources.path AS source_root,
            sources.name AS source_name,
            sources.status AS source_status,
            sources.imported_at AS source_imported_at
        FROM files
        JOIN sources ON sources.id = files.source_id
        WHERE files.target_path_key = ?
        """,
        (path_key(target_path),),
    ).fetchone()


def files_by_original_filename(
    conn: sqlite3.Connection, original_filename: str
) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, source_id, source_path, target_path, original_filename,
               stored_filename, sha256, size_bytes, taken_date, date_source,
               name_conflict, imported_at
        FROM files
        WHERE original_filename = ?
          AND deleted_at IS NULL
        ORDER BY imported_at, id
        """,
        (original_filename,),
    )


def conflict_candidate_files(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, source_id, source_path, target_path, original_filename,
               stored_filename, sha256, size_bytes, taken_date, date_source,
               name_conflict, imported_at
        FROM files
        WHERE deleted_at IS NULL
        ORDER BY original_filename, imported_at, id
        """
    )


def non_metadata_files(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, source_path, target_path, taken_date, date_source, sha256, stored_filename
        FROM files
        WHERE date_source != 'metadata'
          AND deleted_at IS NULL
        ORDER BY date_source, taken_date, target_path
        """
    )


def browser_files(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT target_path, stored_filename, taken_date, date_source
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
