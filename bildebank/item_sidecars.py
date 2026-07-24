from __future__ import annotations

import sqlite3
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
