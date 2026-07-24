from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .config import FaceRecognitionConfig
from .face import (
    LEGACY_FACE_DB_FILENAME,
    ensure_face_schema_path,
    face_database_dir,
)
from .openclip import ensure_openclip_schema_path, openclip_db_path


def attach_existing_item_databases(
    conn: sqlite3.Connection,
    target: Path,
    face_config: FaceRecognitionConfig | None = None,
) -> None:
    """Validate and attach existing databases with data tied to file IDs."""
    database_rows = list(conn.execute("PRAGMA database_list"))
    attached_names = {str(row["name"]) for row in database_rows}
    attached_paths = {
        Path(str(row["file"])).resolve()
        for row in database_rows
        if str(row["file"])
    }

    face_index = 0
    for path in existing_face_database_paths(target, face_config):
        if path.resolve() in attached_paths:
            continue
        ensure_face_schema_path(path)
        while f"face_db_{face_index}" in attached_names:
            face_index += 1
        alias = f"face_db_{face_index}"
        conn.execute(
            f"ATTACH DATABASE ? AS {alias}",
            (str(path),),
        )
        attached_names.add(alias)
        attached_paths.add(path.resolve())
        face_index += 1

    openclip_path = openclip_db_path(target)
    if openclip_path.is_file() and openclip_path.resolve() not in attached_paths:
        ensure_openclip_schema_path(openclip_path)
        if "openclip_db" in attached_names:
            raise ValueError(
                "Kan ikke koble til OpenCLIP-databasen: "
                "databasenavnet openclip_db er allerede i bruk."
            )
        conn.execute("ATTACH DATABASE ? AS openclip_db", (str(openclip_path),))


def existing_face_database_paths(
    target: Path,
    face_config: FaceRecognitionConfig | None = None,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    legacy_path = target / LEGACY_FACE_DB_FILENAME
    if legacy_path.is_file():
        paths.append(legacy_path)

    database_dir = face_database_dir(target, face_config)
    if database_dir.is_dir():
        paths.extend(
            path
            for path in sorted(database_dir.glob("*.sqlite3"))
            if path.is_file()
        )

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
    source_conn = sqlite3.connect(path)
    try:
        backup_conn = sqlite3.connect(backup_path)
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
