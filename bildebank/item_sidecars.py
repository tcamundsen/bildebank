from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from . import db
from .config import FaceRecognitionConfig
from .face import (
    LEGACY_FACE_DB_FILENAME,
    ensure_face_schema_path,
    face_database_dir,
)
from .openclip import ensure_openclip_schema_path, openclip_db_path
from .sidecar_paths import (
    create_new_database_file,
    regular_database_file_exists,
    regular_directory_exists_without_links,
    sqlite_read_write_uri,
    validate_regular_database_file,
)


def attach_existing_item_databases(
    conn: sqlite3.Connection,
    target: Path,
    face_config: FaceRecognitionConfig | None = None,
) -> None:
    """Validate and attach existing databases with data tied to file IDs."""
    attach_existing_face_databases(conn, target, face_config)
    database_rows = list(conn.execute("PRAGMA database_list"))
    attached_names = {str(row["name"]) for row in database_rows}
    attached_paths = {
        Path(str(row["file"])).resolve()
        for row in database_rows
        if str(row["file"])
    }

    openclip_path = openclip_db_path(target)
    if (
        regular_database_file_exists(openclip_path)
        and openclip_path.resolve() not in attached_paths
    ):
        ensure_openclip_schema_path(openclip_path)
        if "openclip_db" in attached_names:
            raise ValueError(
                "Kan ikke koble til OpenCLIP-databasen: "
                "databasenavnet openclip_db er allerede i bruk."
            )
        conn.execute("ATTACH DATABASE ? AS openclip_db", (str(openclip_path),))


def attach_existing_face_databases(
    conn: sqlite3.Connection,
    target: Path,
    face_config: FaceRecognitionConfig | None = None,
    *,
    prepare_schema: bool = True,
) -> tuple[tuple[str, Path], ...]:
    """Attach all existing per-model face databases and return alias/path."""
    database_rows = list(conn.execute("PRAGMA database_list"))
    attached_names = {str(row["name"]) for row in database_rows}
    attached_by_path = {
        Path(str(row["file"])).resolve(): str(row["name"])
        for row in database_rows
        if str(row["file"])
    }

    attached: list[tuple[str, Path]] = []
    face_index = 0
    for path in existing_face_database_paths(target, face_config):
        resolved = path.resolve()
        existing_alias = attached_by_path.get(resolved)
        if existing_alias is not None:
            if existing_alias.startswith("face_db_"):
                attached.append((existing_alias, path))
            continue
        if prepare_schema:
            ensure_face_schema_path(path)
        while f"face_db_{face_index}" in attached_names:
            face_index += 1
        alias = f"face_db_{face_index}"
        database_name = (
            str(path)
            if prepare_schema
            else sqlite_read_write_uri(path)
        )
        conn.execute(
            f"ATTACH DATABASE ? AS {alias}",
            (database_name,),
        )
        attached_names.add(alias)
        attached_by_path[resolved] = alias
        attached.append((alias, path))
        face_index += 1
    return tuple(attached)


def validate_attached_face_path_sync(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    sha256: str,
) -> None:
    """Reject path synchronization when a face database has lost identity."""
    for database in attached_face_database_aliases(conn):
        scanned_row = conn.execute(
            f"SELECT sha256 FROM {database}.scanned_files WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        face_count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {database}.faces WHERE file_id = ?",
                (file_id,),
            ).fetchone()[0]
        )
        if scanned_row is None:
            if face_count:
                raise ValueError(
                    f"{database} har faces-rader for file #{file_id}, men "
                    "mangler scanned_files-rad"
                )
            continue
        if str(scanned_row["sha256"]) != sha256:
            raise ValueError(
                f"{database} har SHA-256-avvik for file #{file_id}; "
                "InsightFace-stier oppdateres ikke"
            )


def sync_attached_face_paths(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    sha256: str,
    target_root: Path,
    target_path: Path,
) -> tuple[int, int]:
    """Synchronize copied paths without changing face or person data."""
    validate_attached_face_path_sync(
        conn,
        file_id=file_id,
        sha256=sha256,
    )
    relative_path = db.target_relative_path(target_root, target_path).as_posix()
    target_path_key = db.target_relative_path_key(target_root, target_path)
    scanned_rows = 0
    face_rows = 0
    for database in attached_face_database_aliases(conn):
        cursor = conn.execute(
            f"""
            UPDATE {database}.scanned_files
            SET target_path = ?, target_path_key = ?
            WHERE file_id = ? AND sha256 = ?
              AND (target_path <> ? OR target_path_key <> ?)
            """,
            (
                relative_path,
                target_path_key,
                file_id,
                sha256,
                relative_path,
                target_path_key,
            ),
        )
        scanned_rows += max(cursor.rowcount, 0)
        cursor = conn.execute(
            f"""
            UPDATE {database}.faces
            SET target_path_key = ?
            WHERE file_id = ? AND target_path_key <> ?
            """,
            (target_path_key, file_id, target_path_key),
        )
        face_rows += max(cursor.rowcount, 0)
    return scanned_rows, face_rows


def attached_face_database_aliases(
    conn: sqlite3.Connection,
) -> tuple[str, ...]:
    return tuple(
        str(row["name"])
        for row in conn.execute("PRAGMA database_list")
        if str(row["name"]).startswith("face_db_")
    )


def existing_face_database_paths(
    target: Path,
    face_config: FaceRecognitionConfig | None = None,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    legacy_path = target / LEGACY_FACE_DB_FILENAME
    if regular_database_file_exists(legacy_path):
        paths.append(legacy_path)

    database_dir = face_database_dir(target, face_config)
    if regular_directory_exists_without_links(database_dir):
        for path in sorted(database_dir.iterdir()):
            if path.suffix != ".sqlite3":
                continue
            if regular_database_file_exists(path):
                paths.append(path)

    unique_paths: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_paths.append(path)
    return tuple(unique_paths)


def backup_existing_face_databases(
    target: Path,
    face_config: FaceRecognitionConfig | None,
    *,
    target_schema_version: int,
) -> tuple[Path, ...]:
    """Validate and back up every existing face database before cleanup."""
    backup_paths: list[Path] = []
    for path in existing_face_database_paths(target, face_config):
        ensure_face_schema_path(path)
        backup_paths.append(
            _backup_face_database_for_main_schema(
                path,
                target_schema_version=target_schema_version,
            )
        )
    return tuple(backup_paths)


def _backup_face_database_for_main_schema(
    path: Path,
    *,
    target_schema_version: int,
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(
        f"{path.name}.backup-before-main-schema-{target_schema_version}-"
        f"{stamp}-{uuid.uuid4().hex}"
    )
    validate_regular_database_file(path)
    source_conn = sqlite3.connect(sqlite_read_write_uri(path), uri=True)
    backup_created = False
    try:
        create_new_database_file(backup_path)
        backup_created = True
        backup_conn = sqlite3.connect(sqlite_read_write_uri(backup_path), uri=True)
        try:
            source_conn.backup(backup_conn)
            integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(
                    "Integritetskontroll av InsightFace-databasebackup "
                    f"feilet for {path}: {integrity}"
                )
        finally:
            backup_conn.close()
    except BaseException:
        if backup_created:
            validate_regular_database_file(backup_path)
            backup_path.unlink(missing_ok=True)
        raise
    finally:
        source_conn.close()
    return backup_path


def delete_attached_item_data(
    conn: sqlite3.Connection,
    file_ids: tuple[int, ...],
) -> None:
    """Delete data belonging to file IDs from attached sidecar databases."""
    if not file_ids:
        return
    placeholders = ",".join("?" for _ in file_ids)
    databases = [
        str(row["name"])
        for row in conn.execute("PRAGMA database_list")
    ]
    for database in databases:
        if database.startswith("face_db_"):
            face_ids_sql = (
                f"SELECT id FROM {database}.faces "
                f"WHERE file_id IN ({placeholders})"
            )
            conn.execute(
                f"DELETE FROM {database}.face_suggestions "
                f"WHERE face_id IN ({face_ids_sql}) "
                f"OR reference_face_id IN ({face_ids_sql})",
                (*file_ids, *file_ids),
            )
            conn.execute(
                f"DELETE FROM {database}.person_faces "
                f"WHERE face_id IN ({face_ids_sql})",
                file_ids,
            )
            conn.execute(
                f"DELETE FROM {database}.person_files "
                f"WHERE file_id IN ({placeholders})",
                file_ids,
            )
            conn.execute(
                f"DELETE FROM {database}.faces "
                f"WHERE file_id IN ({placeholders})",
                file_ids,
            )
            conn.execute(
                f"DELETE FROM {database}.scanned_files "
                f"WHERE file_id IN ({placeholders})",
                file_ids,
            )
        elif database == "openclip_db":
            conn.execute(
                f"""
                DELETE FROM openclip_db.image_search_runs
                WHERE id IN (
                    SELECT run_id
                    FROM openclip_db.image_search_results
                    WHERE file_id IN ({placeholders})
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM openclip_db.image_search_results
                    WHERE image_search_results.run_id = image_search_runs.id
                      AND file_id NOT IN ({placeholders})
                )
                """,
                (*file_ids, *file_ids),
            )
            conn.execute(
                f"DELETE FROM openclip_db.image_search_results "
                f"WHERE file_id IN ({placeholders})",
                file_ids,
            )
            conn.execute(
                f"DELETE FROM openclip_db.image_embeddings "
                f"WHERE file_id IN ({placeholders})",
                file_ids,
            )


def delete_attached_obsolete_item_data(conn: sqlite3.Connection) -> None:
    """Delete sidecar rows for deleted files and file IDs missing from main."""
    databases = [
        str(row["name"])
        for row in conn.execute("PRAGMA database_list")
    ]
    for database in databases:
        if database.startswith("face_db_"):
            obsolete_face_ids_sql = (
                f"SELECT candidate.id FROM {database}.faces AS candidate "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM main.files "
                "WHERE files.id = candidate.file_id "
                "AND files.deleted_at IS NULL"
                ")"
            )
            conn.execute(
                f"DELETE FROM {database}.face_suggestions "
                f"WHERE face_id IN ({obsolete_face_ids_sql}) "
                f"OR reference_face_id IN ({obsolete_face_ids_sql})"
            )
            conn.execute(
                f"DELETE FROM {database}.person_faces "
                f"WHERE face_id IN ({obsolete_face_ids_sql})"
            )
            conn.execute(
                f"DELETE FROM {database}.person_files "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM main.files "
                f"WHERE files.id = {database}.person_files.file_id "
                "AND files.deleted_at IS NULL"
                ")"
            )
            conn.execute(
                f"DELETE FROM {database}.faces "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM main.files "
                f"WHERE files.id = {database}.faces.file_id "
                "AND files.deleted_at IS NULL"
                ")"
            )
            conn.execute(
                f"DELETE FROM {database}.scanned_files "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM main.files "
                f"WHERE files.id = {database}.scanned_files.file_id "
                "AND files.deleted_at IS NULL"
                ")"
            )
        elif database == "openclip_db":
            conn.execute(
                """
                DELETE FROM openclip_db.image_search_runs
                WHERE EXISTS (
                    SELECT 1
                    FROM openclip_db.image_search_results
                    WHERE image_search_results.run_id = image_search_runs.id
                      AND NOT EXISTS (
                          SELECT 1
                          FROM main.files
                          WHERE files.id = image_search_results.file_id
                            AND files.deleted_at IS NULL
                      )
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM openclip_db.image_search_results
                    WHERE image_search_results.run_id = image_search_runs.id
                      AND EXISTS (
                          SELECT 1
                          FROM main.files
                          WHERE files.id = image_search_results.file_id
                            AND files.deleted_at IS NULL
                      )
                )
                """
            )
            conn.execute(
                """
                DELETE FROM openclip_db.image_search_results
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM main.files
                    WHERE files.id = image_search_results.file_id
                      AND files.deleted_at IS NULL
                )
                """
            )
            conn.execute(
                """
                DELETE FROM openclip_db.image_embeddings
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM main.files
                    WHERE files.id = image_embeddings.file_id
                      AND files.deleted_at IS NULL
                )
                """
            )


def validate_attached_item_databases_health(conn: sqlite3.Connection) -> None:
    for row in conn.execute("PRAGMA database_list"):
        database = str(row["name"])
        if not (database.startswith("face_db_") or database == "openclip_db"):
            continue
        foreign_key_errors = conn.execute(
            f"PRAGMA {database}.foreign_key_check"
        ).fetchall()
        if foreign_key_errors:
            raise ValueError(
                f"foreign_key_check feilet for {database}: {foreign_key_errors[0]}"
            )
        integrity = conn.execute(
            f"PRAGMA {database}.integrity_check"
        ).fetchone()[0]
        if integrity != "ok":
            raise ValueError(
                f"integrity_check feilet for {database}: {integrity}"
            )
