from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import warnings
from collections.abc import Callable
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import db
from .config import DEFAULT_FACE_MODEL_NAME, FaceRecognitionConfig
from .html_export import render_html
from .media import IMAGE_EXTENSIONS
from .media_cache import MediaMetadataCache
from .thumbnails import existing_thumbnail_url


LEGACY_FACE_DB_FILENAME = ".bilder-faces.sqlite3"
LEGACY_FACE_DB_MODEL_NAME = "buffalo_l"
FACE_DB_DIRNAME = ".bildebank-faces"
FACE_SCHEMA_VERSION = 3
FACE_MODEL_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
FACE_SUGGEST_BATCH_SIZE = 5000


@dataclass
class FaceScanStats:
    total: int = 0
    checked: int = 0
    skipped: int = 0
    scanned: int = 0
    faces: int = 0
    errors: int = 0
    last_error_path: Path | None = None
    last_error_message: str | None = None


FaceScanProgress = Callable[[str, int, int, FaceScanStats, Path | None], None]

@dataclass(frozen=True)
class FaceReport:
    database_exists: bool
    scanned_files: int = 0
    total_faces: int = 0
    files_with_zero_faces: int = 0
    files_with_one_face: int = 0
    files_with_multiple_faces: int = 0
    scan_errors: int = 0
    persons: int = 0
    confirmed_face_links: int = 0
    suggestions: int = 0
    files_with_confirmed_person: int = 0
    files_with_faces_no_confirmed_person: int = 0
    files_with_confirmed_and_unknown_faces: int = 0
    top_files: tuple[sqlite3.Row, ...] = ()
    errors: tuple[sqlite3.Row, ...] = ()


@dataclass(frozen=True)
class AddFaceToPersonResult:
    person_name: str
    face_id: int
    added: bool


@dataclass(frozen=True)
class RemoveFaceFromPersonResult:
    person_name: str
    face_id: int
    removed: bool


@dataclass(frozen=True)
class DeletePersonResult:
    person_name: str
    removed_faces: int
    removed_suggestions: int


@dataclass(frozen=True)
class RenamePersonResult:
    old_name: str
    new_name: str


@dataclass(frozen=True)
class FaceResetResult:
    mode: str
    removed_persons: int
    removed_person_faces: int
    removed_suggestions: int


@dataclass(frozen=True)
class PeopleBrowserResult:
    index_path: Path
    person_pages: tuple[Path, ...]


@dataclass(frozen=True)
class FaceSuggestStats:
    persons: int
    unknown_faces: int
    suggestions: int
    threshold: float


@dataclass
class FaceSuggestProgressStats:
    persons: int = 0
    known_faces: int = 0
    unknown_faces: int = 0
    suggestions: int = 0
    threshold: float = 0.0


FaceSuggestProgress = Callable[[str, int, int, FaceSuggestProgressStats, Path | None], None]


def face_database_dir(target: Path, config: FaceRecognitionConfig | None = None) -> Path:
    database_dir = config.database_dir if config is not None else Path(FACE_DB_DIRNAME)
    if database_dir.is_absolute():
        return database_dir
    return target / database_dir


def face_model_db_filename(model_name: str) -> str:
    if not FACE_MODEL_FILENAME_RE.fullmatch(model_name):
        raise ValueError(
            "Ugyldig InsightFace-modellnavn for databasefil: "
            f"{model_name!r}. Bruk bare bokstaver, tall, punktum, bindestrek og understrek."
        )
    return f"{model_name}.sqlite3"


def face_db_path(target: Path, config: FaceRecognitionConfig | None = None) -> Path:
    model_name = config.model_name if config is not None else DEFAULT_FACE_MODEL_NAME
    path = face_database_dir(target, config) / face_model_db_filename(model_name)
    move_legacy_face_db_if_needed(target, path, model_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def move_legacy_face_db_if_needed(target: Path, new_path: Path, model_name: str) -> None:
    if model_name != LEGACY_FACE_DB_MODEL_NAME:
        return
    legacy_path = target / LEGACY_FACE_DB_FILENAME
    if not legacy_path.exists() or new_path.exists():
        return
    new_path.parent.mkdir(parents=True, exist_ok=True)
    # KOMPATIBILITET: den gamle face-databasen ble laget før modellvalg og regnes som buffalo_l.
    shutil.move(str(legacy_path), str(new_path))


def connect_face_db(target: Path, config: FaceRecognitionConfig | None = None) -> sqlite3.Connection:
    model_name = config.model_name if config is not None else DEFAULT_FACE_MODEL_NAME
    conn = sqlite3.connect(face_db_path(target, config))
    conn.row_factory = sqlite3.Row
    apply_face_schema(conn)
    validate_face_database_model(conn, model_name)
    set_meta(conn, "target_path", str(target.resolve()))
    conn.commit()
    return conn


def validate_face_database_model(conn: sqlite3.Connection, model_name: str) -> None:
    stored_model = get_meta(conn, "model_name")
    if stored_model is None:
        set_meta(conn, "model_name", model_name)
        return
    if stored_model != model_name:
        raise ValueError(
            "Face-databasen tilhører en annen modell "
            f"({stored_model}) enn aktiv config ({model_name})."
        )


def apply_face_schema(conn: sqlite3.Connection) -> None:
    version = face_schema_version(conn)
    if version > FACE_SCHEMA_VERSION:
        raise ValueError(
            f"Face-databasen bruker et nyere format (schema_version={version}) "
            f"enn programmet støtter (schema_version={FACE_SCHEMA_VERSION})."
        )
    if version == 0:
        create_current_face_schema(conn)
        set_meta(conn, "schema_version", str(FACE_SCHEMA_VERSION))
        validate_current_face_schema(conn)
        return
    migrate_face_schema(conn, version)
    create_current_face_schema(conn)
    set_meta(conn, "schema_version", str(FACE_SCHEMA_VERSION))
    validate_current_face_schema(conn)


def face_schema_version(conn: sqlite3.Connection) -> int:
    if not db.table_exists(conn, "meta"):
        return 2 if db.table_exists(conn, "scanned_files") else 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return 2 if db.table_exists(conn, "scanned_files") else 0
    value = row[0]
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Ugyldig schema_version i face-databasen: {value}") from exc


def migrate_face_schema(conn: sqlite3.Connection, version: int) -> None:
    if version < 2:
        raise ValueError(f"Kan ikke migrere face-database med schema_version={version}.")
    if version == 2:
        migrate_face_schema_v2_to_v3(conn)
        version = 3
    if version != FACE_SCHEMA_VERSION:
        raise ValueError(f"Kan ikke migrere face-database med schema_version={version}.")


def migrate_face_schema_v2_to_v3(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS face_group_members")
    conn.execute("DROP TABLE IF EXISTS face_groups")
    conn.execute("DROP TABLE IF EXISTS face_group_runs")


def validate_current_face_schema(conn: sqlite3.Connection) -> None:
    legacy_tables = {
        "face_group_members",
        "face_groups",
        "face_group_runs",
    }
    existing_legacy_tables = sorted(table for table in legacy_tables if db.table_exists(conn, table))
    if existing_legacy_tables:
        raise ValueError(
            "Face-databasen har schema_version=3, men inneholder legacy-gruppetabeller "
            f"({', '.join(existing_legacy_tables)})."
        )
    validate_relative_face_paths(conn)


def validate_relative_face_paths(conn: sqlite3.Connection) -> None:
    if not db.table_exists(conn, "scanned_files"):
        return
    row = conn.execute(
        """
        SELECT file_id, target_path
        FROM scanned_files
        WHERE target_path LIKE '/%'
           OR target_path GLOB '[A-Za-z]:*'
        ORDER BY file_id
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise ValueError(
            "Face-databasen har absolutt target_path for "
            f"file_id={row['file_id']}: {row['target_path']}. "
            "Kjør bildebank face-scan på nytt."
        )


def create_current_face_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scanned_files (
            file_id INTEGER PRIMARY KEY,
            target_path TEXT NOT NULL,
            target_path_key TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL,
            error_message TEXT,
            face_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS faces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            target_path_key TEXT NOT NULL,
            bbox_x REAL NOT NULL,
            bbox_y REAL NOT NULL,
            bbox_width REAL NOT NULL,
            bbox_height REAL NOT NULL,
            detection_score REAL NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_faces_file_id ON faces(file_id);
        CREATE INDEX IF NOT EXISTS idx_faces_target_path_key ON faces(target_path_key);

        CREATE TABLE IF NOT EXISTS persons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS person_faces (
            person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
            face_id INTEGER NOT NULL,
            confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(person_id, face_id)
        );

        CREATE INDEX IF NOT EXISTS idx_person_faces_face_id ON person_faces(face_id);

        CREATE TABLE IF NOT EXISTS face_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
            face_id INTEGER NOT NULL,
            similarity REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(person_id, face_id)
        );

        CREATE INDEX IF NOT EXISTS idx_face_suggestions_face_id ON face_suggestions(face_id);
        """
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def face_db_summary(target: Path, config: FaceRecognitionConfig | None = None) -> tuple[bool, int, int]:
    path = face_db_path(target, config)
    if not path.exists():
        return False, 0, 0
    conn = sqlite3.connect(path)
    try:
        scanned = count_rows(conn, "scanned_files")
        faces = count_rows(conn, "faces")
        return True, scanned, faces
    finally:
        conn.close()


def create_person(target: Path, name: str, config: FaceRecognitionConfig | None = None) -> int:
    clean_name = normalize_person_name(name)
    conn = connect_face_db(target, config)
    try:
        row = conn.execute("SELECT id FROM persons WHERE name = ?", (clean_name,)).fetchone()
        if row is not None:
            return int(row["id"])
        person_id = int(
            conn.execute(
                "INSERT INTO persons(name) VALUES(?) RETURNING id",
                (clean_name,),
            ).fetchone()["id"]
        )
        conn.commit()
        return person_id
    finally:
        conn.close()


def add_face_to_person(
    target: Path,
    person_name: str,
    face_id: int,
    config: FaceRecognitionConfig | None = None,
) -> AddFaceToPersonResult:
    clean_name = normalize_person_name(person_name)
    conn = connect_face_db(target, config)
    try:
        require_face(conn, face_id)
        person_id = require_person(conn, clean_name)
        conn.execute(
            "DELETE FROM person_faces WHERE face_id = ? AND person_id != ?",
            (face_id, person_id),
        )
        cur = conn.execute(
            "INSERT OR IGNORE INTO person_faces(person_id, face_id) VALUES(?, ?)",
            (person_id, face_id),
        )
        conn.execute("DELETE FROM face_suggestions WHERE face_id = ?", (face_id,))
        conn.execute("UPDATE persons SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (person_id,))
        conn.commit()
        return AddFaceToPersonResult(clean_name, face_id, bool(cur.rowcount))
    finally:
        conn.close()


def remove_face_from_person(
    target: Path,
    person_name: str,
    face_id: int,
    config: FaceRecognitionConfig | None = None,
) -> RemoveFaceFromPersonResult:
    clean_name = normalize_person_name(person_name)
    conn = connect_face_db(target, config)
    try:
        require_face(conn, face_id)
        row = conn.execute("SELECT id FROM persons WHERE name = ?", (clean_name,)).fetchone()
        if row is None:
            raise ValueError(f"Fant ikke person: {clean_name}")
        person_id = int(row["id"])
        cur = conn.execute(
            "DELETE FROM person_faces WHERE person_id = ? AND face_id = ?",
            (person_id, face_id),
        )
        conn.execute("UPDATE persons SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (person_id,))
        conn.commit()
        return RemoveFaceFromPersonResult(clean_name, face_id, bool(cur.rowcount))
    finally:
        conn.close()


def delete_person(target: Path, person_name: str, config: FaceRecognitionConfig | None = None) -> DeletePersonResult:
    clean_name = normalize_person_name(person_name)
    conn = connect_face_db(target, config)
    try:
        person_id = require_person(conn, clean_name)
        removed_faces = int(
            conn.execute(
                "SELECT COUNT(*) FROM person_faces WHERE person_id = ?",
                (person_id,),
            ).fetchone()[0]
        )
        removed_suggestions = int(
            conn.execute(
                "SELECT COUNT(*) FROM face_suggestions WHERE person_id = ?",
                (person_id,),
            ).fetchone()[0]
        )
        conn.execute("DELETE FROM person_faces WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM face_suggestions WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))
        conn.commit()
        return DeletePersonResult(clean_name, removed_faces, removed_suggestions)
    finally:
        conn.close()


def rename_person(
    target: Path,
    old_name: str,
    new_name: str,
    config: FaceRecognitionConfig | None = None,
) -> RenamePersonResult:
    clean_old_name = normalize_person_name(old_name)
    clean_new_name = normalize_person_name(new_name)
    if clean_old_name == clean_new_name:
        return RenamePersonResult(clean_old_name, clean_new_name)
    conn = connect_face_db(target, config)
    try:
        person_id = require_person(conn, clean_old_name)
        row = conn.execute("SELECT id FROM persons WHERE name = ?", (clean_new_name,)).fetchone()
        if row is not None:
            raise ValueError(f"Person finnes allerede: {clean_new_name}")
        conn.execute(
            "UPDATE persons SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (clean_new_name, person_id),
        )
        conn.commit()
        return RenamePersonResult(clean_old_name, clean_new_name)
    finally:
        conn.close()


def reset_face_database(
    target: Path,
    *,
    mode: str,
    config: FaceRecognitionConfig | None = None,
) -> FaceResetResult:
    conn = connect_face_db(target, config)
    try:
        removed_persons = count_rows_if_table_exists(conn, "persons")
        removed_person_faces = count_rows_if_table_exists(conn, "person_faces")
        removed_suggestions = count_rows_if_table_exists(conn, "face_suggestions")
        if mode not in {"all", "keep-scan"}:
            raise ValueError(f"Ukjent face-reset-nivå: {mode}")
        conn.execute("DELETE FROM face_suggestions")
        conn.execute("DELETE FROM person_faces")
        conn.execute("DELETE FROM persons")
        conn.commit()
        return FaceResetResult(
            mode=mode,
            removed_persons=removed_persons,
            removed_person_faces=removed_person_faces,
            removed_suggestions=removed_suggestions,
        )
    finally:
        conn.close()


def suggest_faces(
    target: Path,
    *,
    threshold: float = 0.6,
    config: FaceRecognitionConfig | None = None,
    progress: FaceSuggestProgress | None = None,
) -> FaceSuggestStats:
    path = face_db_path(target, config)
    if not path.exists():
        raise ValueError("Face-database finnes ikke. Kjør bildebank face-scan først.")
    conn = connect_face_db(target, config)
    try:
        import numpy as np

        progress_stats = FaceSuggestProgressStats(threshold=threshold)
        person_vectors: dict[int, list[Any]] = {}
        known_total = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM persons
                JOIN person_faces ON person_faces.person_id = persons.id
                JOIN faces ON faces.id = person_faces.face_id
                """
            ).fetchone()[0]
        )
        if progress is not None:
            progress("load_known_start", 0, known_total, progress_stats, None)
        for index, row in enumerate(
            conn.execute(
                """
                SELECT persons.id AS person_id, faces.embedding
                FROM persons
                JOIN person_faces ON person_faces.person_id = persons.id
                JOIN faces ON faces.id = person_faces.face_id
                ORDER BY persons.id, faces.id
                """
            ),
            start=1,
        ):
            person_vectors.setdefault(int(row["person_id"]), []).append(
                embedding_array_from_blob(row["embedding"], np)
            )
            progress_stats.known_faces = index
            progress_stats.persons = len(person_vectors)
            if progress is not None:
                progress("load_known", index, known_total, progress_stats, None)
        person_count = sum(1 for vectors in person_vectors.values() if vectors)
        centroid_person_ids, centroid_matrix = normalized_centroid_matrix(person_vectors, np)
        progress_stats.persons = person_count
        unknown_total = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM faces
                WHERE id NOT IN (SELECT face_id FROM person_faces)
                """
            ).fetchone()[0]
        )
        if progress is not None:
            progress("load_unknown_start", 0, unknown_total, progress_stats, None)

        conn.execute("DELETE FROM face_suggestions")
        suggestions = 0
        if progress is not None:
            progress("compare_start", 0, unknown_total, progress_stats, None)
        if centroid_matrix.size == 0 or unknown_total == 0:
            progress_stats.unknown_faces = unknown_total
            if progress is not None:
                progress("compare", unknown_total, unknown_total, progress_stats, None)
        else:
            cursor = conn.execute(
                """
                SELECT id, embedding
                FROM faces
                WHERE id NOT IN (SELECT face_id FROM person_faces)
                ORDER BY id
                """
            )
            processed_unknown = 0
            while True:
                batch = cursor.fetchmany(FACE_SUGGEST_BATCH_SIZE)
                if not batch:
                    break
                processed_unknown += len(batch)
                progress_stats.unknown_faces = processed_unknown
                if progress is not None:
                    progress("load_unknown", processed_unknown, unknown_total, progress_stats, None)

                unknown_matrix = normalized_embedding_matrix((row["embedding"] for row in batch), np)
                scores = unknown_matrix @ centroid_matrix.T
                best_indexes = scores.argmax(axis=1)
                best_scores = scores[np.arange(scores.shape[0]), best_indexes]
                rows_to_insert = [
                    (centroid_person_ids[int(best_index)], int(face["id"]), float(best_score))
                    for face, best_index, best_score in zip(batch, best_indexes, best_scores)
                    if float(best_score) > 0.0 and float(best_score) >= threshold
                ]
                if rows_to_insert:
                    conn.executemany(
                        """
                        INSERT INTO face_suggestions(person_id, face_id, similarity)
                        VALUES(?, ?, ?)
                        """,
                        rows_to_insert,
                    )
                    suggestions += len(rows_to_insert)
                    progress_stats.suggestions = suggestions
                if progress is not None:
                    progress("compare", processed_unknown, unknown_total, progress_stats, None)
        progress_stats.suggestions = suggestions
        conn.commit()
        if progress is not None:
            progress("done", unknown_total, unknown_total, progress_stats, None)
        return FaceSuggestStats(
            persons=person_count,
            unknown_faces=unknown_total,
            suggestions=suggestions,
            threshold=threshold,
        )
    finally:
        conn.close()


def embedding_array_from_blob(blob: bytes | memoryview, np: Any) -> Any:
    return np.frombuffer(blob, dtype=np.float32)


def normalized_centroid_matrix(person_vectors: dict[int, list[Any]], np: Any) -> tuple[list[int], Any]:
    person_ids: list[int] = []
    centroids = []
    for person_id, vectors in person_vectors.items():
        if not vectors:
            continue
        centroid = np.mean(np.vstack(vectors), axis=0, dtype=np.float32)
        norm = np.linalg.norm(centroid)
        if norm == 0.0:
            continue
        person_ids.append(person_id)
        centroids.append(centroid / norm)
    if not centroids:
        return [], np.empty((0, 0), dtype=np.float32)
    return person_ids, np.vstack(centroids).astype(np.float32, copy=False)


def normalized_embedding_matrix(blobs, np: Any) -> Any:
    matrix = np.vstack([embedding_array_from_blob(blob, np) for blob in blobs]).astype(
        np.float32,
        copy=False,
    )
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return np.divide(
        matrix,
        norms,
        out=np.zeros_like(matrix, dtype=np.float32),
        where=norms != 0.0,
    )


def list_face_suggestions(target: Path, config: FaceRecognitionConfig | None = None) -> list[sqlite3.Row]:
    path = face_db_path(target, config)
    if not path.exists():
        return []
    conn = connect_face_db(target, config)
    try:
        return list(
            conn.execute(
                """
                SELECT
                    persons.name,
                    face_suggestions.face_id,
                    face_suggestions.similarity,
                    scanned_files.target_path
                FROM face_suggestions
                JOIN persons ON persons.id = face_suggestions.person_id
                JOIN faces ON faces.id = face_suggestions.face_id
                JOIN scanned_files ON scanned_files.file_id = faces.file_id
                ORDER BY persons.name, face_suggestions.similarity DESC, face_suggestions.face_id
                """
            )
        )
    finally:
        conn.close()


def list_persons(target: Path, config: FaceRecognitionConfig | None = None) -> list[sqlite3.Row]:
    path = face_db_path(target, config)
    if not path.exists():
        return []
    conn = connect_face_db(target, config)
    try:
        return list(
            conn.execute(
                """
                SELECT
                    persons.name,
                    persons.created_at,
                    persons.updated_at,
                    (
                        SELECT COUNT(*)
                        FROM person_faces
                        WHERE person_faces.person_id = persons.id
                    ) AS face_count,
                    (
                        SELECT COUNT(DISTINCT faces.file_id)
                        FROM person_faces
                        JOIN faces ON faces.id = person_faces.face_id
                        WHERE person_faces.person_id = persons.id
                    ) AS confirmed_file_count,
                    (
                        SELECT COUNT(*)
                        FROM face_suggestions
                        WHERE face_suggestions.person_id = persons.id
                    ) AS suggestion_count
                FROM persons
                ORDER BY persons.name
                """
            )
        )
    finally:
        conn.close()


def normalize_person_name(name: str) -> str:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Personnavn kan ikke være tomt.")
    return clean_name


def require_person(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM persons WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(
            f"Fant ikke person: {name}. Opprett personen først med bildebank face-person-create \"{name}\"."
        )
    return int(row["id"])


def require_face(conn: sqlite3.Connection, face_id: int) -> None:
    row = conn.execute("SELECT id FROM faces WHERE id = ?", (face_id,)).fetchone()
    if row is None:
        raise ValueError(f"Fant ikke ansikt-id {face_id}. Kjør make-face-browser for å se id-er.")


def face_report(
    target: Path,
    *,
    limit: int = 20,
    config: FaceRecognitionConfig | None = None,
) -> FaceReport:
    path = face_db_path(target, config)
    if not path.exists():
        return FaceReport(database_exists=False)
    conn = connect_face_db(target, config)
    try:
        return FaceReport(
            database_exists=True,
            scanned_files=count_rows(conn, "scanned_files"),
            total_faces=count_rows(conn, "faces"),
            files_with_zero_faces=count_scanned_files(conn, "status = 'ok' AND face_count = 0"),
            files_with_one_face=count_scanned_files(conn, "status = 'ok' AND face_count = 1"),
            files_with_multiple_faces=count_scanned_files(conn, "status = 'ok' AND face_count > 1"),
            scan_errors=count_scanned_files(conn, "status = 'error'"),
            persons=count_rows_if_table_exists(conn, "persons"),
            confirmed_face_links=count_rows_if_table_exists(conn, "person_faces"),
            suggestions=count_rows_if_table_exists(conn, "face_suggestions"),
            files_with_confirmed_person=count_files_with_confirmed_person(conn),
            files_with_faces_no_confirmed_person=count_files_with_faces_no_confirmed_person(conn),
            files_with_confirmed_and_unknown_faces=count_files_with_confirmed_and_unknown_faces(conn),
            top_files=tuple(
                conn.execute(
                    """
                    SELECT target_path, face_count
                    FROM scanned_files
                    WHERE status = 'ok' AND face_count > 0
                    ORDER BY face_count DESC, target_path
                    LIMIT ?
                    """,
                    (limit,),
                )
            ),
            errors=tuple(
                conn.execute(
                    """
                    SELECT target_path, error_message
                    FROM scanned_files
                    WHERE status = 'error'
                    ORDER BY scanned_at DESC, target_path
                    LIMIT ?
                    """,
                    (limit,),
                )
            ),
        )
    finally:
        conn.close()


def export_face_browser(
    target: Path,
    output: Path | None = None,
    *,
    limit: int | None = None,
    config: FaceRecognitionConfig | None = None,
) -> Path:
    output_path = output or (target / "faces.html")
    path = face_db_path(target, config)
    if not path.exists():
        raise ValueError("Face-database finnes ikke. Kjør bildebank face-scan først.")
    conn = connect_face_db(target, config)
    try:
        with MediaMetadataCache(target) as media_cache:
            items = face_browser_items(target, conn, limit=limit, media_cache=media_cache)
    finally:
        conn.close()
    output_path.write_text(render_face_browser_html(items), encoding="utf-8", newline="\n")
    return output_path


def export_person_browser(
    target: Path,
    person_name: str,
    output: Path | None = None,
    *,
    month_preview_limit: int | None = None,
    config: FaceRecognitionConfig | None = None,
) -> Path:
    clean_name = normalize_person_name(person_name)
    output_path = output or (target / f"person-{safe_filename(clean_name)}.html")
    path = face_db_path(target, config)
    if not path.exists():
        raise ValueError("Face-database finnes ikke. Kjør bildebank face-scan først.")
    conn = connect_face_db(target, config)
    try:
        person = conn.execute("SELECT id, name FROM persons WHERE name = ?", (clean_name,)).fetchone()
        if person is None:
            raise ValueError(f"Fant ikke person: {clean_name}")
        with MediaMetadataCache(target) as media_cache:
            items = person_browser_items(target, conn, person_id=int(person["id"]), media_cache=media_cache)
    finally:
        conn.close()
    output_path.write_text(
        render_person_browser_html(clean_name, items, month_preview_limit=month_preview_limit),
        encoding="utf-8",
        newline="\n",
    )
    return output_path


def export_people_browser(
    target: Path,
    *,
    month_preview_limit: int | None = None,
    config: FaceRecognitionConfig | None = None,
) -> PeopleBrowserResult:
    output_path = target / "personer.html"
    path = face_db_path(target, config)
    if not path.exists():
        raise ValueError("Face-database finnes ikke. Kjør bildebank face-scan først.")
    conn = connect_face_db(target, config)
    try:
        people = list(conn.execute("SELECT id, name FROM persons ORDER BY name"))
        index_people: list[dict[str, Any]] = []
        person_pages: list[Path] = []
        with MediaMetadataCache(target) as media_cache:
            for person in people:
                name = str(person["name"])
                page_path = target / f"person-{safe_filename(name)}.html"
                items = person_browser_items(target, conn, person_id=int(person["id"]), media_cache=media_cache)
                page_path.write_text(
                    render_person_browser_html(name, items, month_preview_limit=month_preview_limit),
                    encoding="utf-8",
                    newline="\n",
                )
                person_pages.append(page_path)
                index_people.append(people_index_item(target, name, page_path, items))
    finally:
        conn.close()
    output_path.write_text(render_people_index_html(index_people), encoding="utf-8", newline="\n")
    return PeopleBrowserResult(index_path=output_path, person_pages=tuple(person_pages))


def person_browser_items(
    target: Path,
    conn: sqlite3.Connection,
    *,
    person_id: int,
    media_cache: MediaMetadataCache | None = None,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            'bekreftet' AS status,
            faces.id AS face_id,
            1.0 AS similarity,
            scanned_files.target_path,
            scanned_files.face_count,
            faces.bbox_x,
            faces.bbox_y,
            faces.bbox_width,
            faces.bbox_height,
            faces.detection_score
        FROM person_faces
        JOIN faces ON faces.id = person_faces.face_id
        JOIN scanned_files ON scanned_files.file_id = faces.file_id
        WHERE person_faces.person_id = ?

        UNION ALL

        SELECT
            'forslag' AS status,
            faces.id AS face_id,
            face_suggestions.similarity,
            scanned_files.target_path,
            scanned_files.face_count,
            faces.bbox_x,
            faces.bbox_y,
            faces.bbox_width,
            faces.bbox_height,
            faces.detection_score
        FROM face_suggestions
        JOIN faces ON faces.id = face_suggestions.face_id
        JOIN scanned_files ON scanned_files.file_id = faces.file_id
        WHERE face_suggestions.person_id = ?

        ORDER BY target_path, status, face_id
        """,
        (person_id, person_id),
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    own_cache = media_cache is None
    if media_cache is None:
        media_cache = MediaMetadataCache(target)
    try:
        for row in rows:
            relative_path = relative_to_target(target, Path(str(row["target_path"])))
            target_path = db.absolute_target_path(target, relative_path)
            key = relative_path.as_posix()
            dimensions = media_cache.image_dimensions(target_path)
            orientation = media_cache.image_orientation(target_path)
            item = grouped.setdefault(
                key,
                {
                    "path": relative_path.as_posix(),
                    "url": path_to_url(relative_path),
                    "thumbnailSrc": existing_thumbnail_url(target, relative_path),
                    "name": relative_path.name,
                    "monthKey": person_month_key(target, relative_path),
                    "sizeText": format_person_file_size(target_path),
                    "faceCount": int(row["face_count"]),
                    "dimensions": dimensions,
                    "orientation": orientation,
                    "faces": [],
                },
            )
            face = {
                "faceId": int(row["face_id"]),
                "status": str(row["status"]),
                "similarity": float(row["similarity"]),
                "x": float(row["bbox_x"]),
                "y": float(row["bbox_y"]),
                "width": float(row["bbox_width"]),
                "height": float(row["bbox_height"]),
                "score": float(row["detection_score"]),
            }
            percent = face_box_percent(face, dimensions, orientation)
            if percent is not None:
                left, top, width, height = percent
                face["left"] = left
                face["top"] = top
                face["boxWidth"] = width
                face["boxHeight"] = height
            item["faces"].append(face)
    finally:
        if own_cache:
            media_cache.close()
    return list(grouped.values())


def face_browser_items(
    target: Path,
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    media_cache: MediaMetadataCache | None = None,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            scanned_files.file_id,
            scanned_files.target_path,
            scanned_files.face_count,
            faces.id AS face_id,
            faces.bbox_x,
            faces.bbox_y,
            faces.bbox_width,
            faces.bbox_height,
            faces.detection_score,
            faces.embedding_model
        FROM scanned_files
        JOIN faces ON faces.file_id = scanned_files.file_id
        WHERE scanned_files.status = 'ok'
        ORDER BY scanned_files.target_path, faces.id
        """
    )
    grouped: dict[str, dict[str, Any]] = {}
    own_cache = media_cache is None
    if media_cache is None:
        media_cache = MediaMetadataCache(target)
    try:
        for row in rows:
            relative_path = relative_to_target(target, Path(str(row["target_path"])))
            target_path = db.absolute_target_path(target, relative_path)
            key = relative_path.as_posix()
            dimensions = media_cache.image_dimensions(target_path)
            orientation = media_cache.image_orientation(target_path)
            item = grouped.setdefault(
                key,
                {
                    "path": relative_path.as_posix(),
                    "url": path_to_url(relative_path),
                    "faceCount": int(row["face_count"]),
                    "dimensions": dimensions,
                    "orientation": orientation,
                    "faces": [],
                },
            )
            item["faces"].append(
                {
                    "faceId": int(row["face_id"]),
                    "x": float(row["bbox_x"]),
                    "y": float(row["bbox_y"]),
                    "width": float(row["bbox_width"]),
                    "height": float(row["bbox_height"]),
                    "score": float(row["detection_score"]),
                    "model": str(row["embedding_model"]),
                }
            )
            if limit is not None and len(grouped) >= limit:
                break
    finally:
        if own_cache:
            media_cache.close()
    return list(grouped.values())


def relative_to_target(target: Path, path: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate
    try:
        return candidate.resolve().relative_to(target.resolve())
    except ValueError:
        return candidate


def display_relative_path(target: Path, path: Path) -> str:
    return relative_to_target(target, path).as_posix()


def path_to_url(path: Path) -> str:
    return "/".join(quote(part) for part in path.parts)


def render_face_browser_html(items: list[dict[str, Any]]) -> str:
    cards = "\n".join(render_face_card(item) for item in items)
    if not cards:
        cards = '<p class="empty">Ingen ansikter funnet ennå.</p>'
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ansikter</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f5;
      --text: #202020;
      --muted: #666;
      --border: #d8d8d2;
      --panel: #fff;
      --accent: #ff1f1f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      padding: 16px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
    }}
    main {{
      padding: 16px;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 16px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    .media {{
      position: relative;
      background: #eee;
    }}
    .media img {{
      display: block;
      width: 100%;
      height: auto;
    }}
    .box {{
      position: absolute;
      border: 3px solid var(--accent);
      background: rgb(255 31 31 / 12%);
      pointer-events: none;
    }}
    .meta {{
      padding: 10px;
      display: grid;
      gap: 4px;
      font-size: 13px;
    }}
    .path {{
      overflow-wrap: anywhere;
      font-weight: 600;
    }}
    .muted {{
      color: var(--muted);
    }}
    .empty {{
      grid-column: 1 / -1;
      margin: 0;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <header>
    <h1>Ansikter ({len(items)} bilder)</h1>
  </header>
  <main>
    {cards}
  </main>
</body>
</html>
"""


def render_person_browser_html(
    person_name: str,
    items: list[dict[str, Any]],
    *,
    month_preview_limit: int | None = None,
) -> str:
    return render_html(
        person_browser_json_items(items),
        title=person_name,
        month_preview_limit=month_preview_limit,
    )


def people_index_item(
    target: Path,
    name: str,
    page_path: Path,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    confirmed = 0
    suggested = 0
    for item in items:
        for face in item["faces"]:
            if face["status"] == "forslag":
                suggested += 1
            else:
                confirmed += 1
    thumbnail_url = items[0]["url"] if items else None
    return {
        "name": name,
        "pageUrl": path_to_url(relative_to_target(target, page_path)),
        "thumbnailUrl": thumbnail_url,
        "imageCount": len(items),
        "confirmed": confirmed,
        "suggested": suggested,
    }


def render_people_index_html(people: list[dict[str, Any]]) -> str:
    cards = "\n".join(render_people_index_card(person) for person in people)
    if not cards:
        cards = '<p class="empty">Ingen personer registrert ennå.</p>'
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Personer</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f5;
      --text: #202020;
      --muted: #666;
      --border: #d8d8d2;
      --panel: #fff;
      --accent: #2f6fbf;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      padding: 16px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{ margin: 0; font-size: 22px; }}
    main {{
      padding: 16px;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 14px;
      align-items: start;
    }}
    .person {{
      display: grid;
      gap: 8px;
      min-width: 0;
      color: inherit;
      text-decoration: none;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    .person:hover {{ border-color: var(--accent); }}
    .thumb {{
      aspect-ratio: 4 / 3;
      background: #e8e8e2;
      display: grid;
      place-items: center;
      color: var(--muted);
      font-size: 42px;
      font-weight: 700;
      overflow: hidden;
    }}
    .thumb img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .meta {{
      padding: 0 10px 10px;
      display: grid;
      gap: 3px;
      min-width: 0;
    }}
    .name {{
      font-weight: 700;
      overflow-wrap: anywhere;
    }}
    .detail {{
      color: var(--muted);
      font-size: 13px;
    }}
    .empty {{
      grid-column: 1 / -1;
      margin: 0;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <header>
    <h1>Personer ({len(people)})</h1>
  </header>
  <main>
    {cards}
  </main>
</body>
</html>
"""


def render_people_index_card(person: dict[str, Any]) -> str:
    thumbnail_url = person.get("thumbnailUrl")
    if thumbnail_url:
        thumbnail = f'<img src="{html_escape(thumbnail_url)}" alt="">'
    else:
        thumbnail = html_escape(str(person["name"])[:1].upper() or "?")
    return f"""<a class="person" href="{html_escape(person['pageUrl'])}">
  <div class="thumb">{thumbnail}</div>
  <div class="meta">
    <div class="name">{html_escape(person['name'])}</div>
    <div class="detail">{int(person['imageCount'])} bilder</div>
    <div class="detail">{int(person['confirmed'])} bekreftet, {int(person['suggested'])} forslag</div>
  </div>
</a>"""


def render_face_card(item: dict[str, Any]) -> str:
    orientation = int(item.get("orientation", 1))
    boxes = "\n".join(render_face_box(face, item["dimensions"], orientation) for face in item["faces"])
    face_count = int(item["faceCount"])
    face_ids = ", ".join(str(face["faceId"]) for face in item["faces"])
    return f"""<article class="card">
  <div class="media">
    <img src="{html_escape(item['url'])}" alt="">
    {boxes}
  </div>
  <div class="meta">
    <div class="path">{html_escape(item['path'])}</div>
    <div>{face_count} ansikt{'er' if face_count != 1 else ''}</div>
    <div class="muted">Ansikt-id: {html_escape(face_ids)}</div>
    <div class="muted">Beste score: {max(float(face['score']) for face in item['faces']):.3f}</div>
  </div>
</article>"""


def render_person_card(item: dict[str, Any]) -> str:
    orientation = int(item.get("orientation", 1))
    boxes = "\n".join(render_person_box(face, item["dimensions"], orientation) for face in item["faces"])
    confirmed = sum(1 for face in item["faces"] if face["status"] == "bekreftet")
    suggested = sum(1 for face in item["faces"] if face["status"] == "forslag")
    details = "<br>".join(
        f"{html_escape(face['status'])}: face-id {face['faceId']}, score {float(face['similarity']):.3f}"
        for face in item["faces"]
    )
    return f"""<article class="card">
  <div class="media">
    <img src="{html_escape(item['url'])}" alt="">
    {boxes}
  </div>
  <div class="meta">
    <div class="path">{html_escape(item['path'])}</div>
    <div>{confirmed} bekreftet, {suggested} forslag</div>
    <div class="muted">{details}</div>
  </div>
</article>"""


def render_face_box(face: dict[str, Any], dimensions, orientation: int = 1) -> str:
    percent = face_box_percent(face, dimensions, orientation)
    if percent is None:
        left = top = width = height = 0.0
    else:
        left, top, width, height = percent
    return (
        '<div class="box" '
        f'title="score {float(face["score"]):.3f}" '
        'style="'
        f'left: {left:.4f}%; '
        f'top: {top:.4f}%; '
        f'width: {width:.4f}%; '
        f'height: {height:.4f}%;'
        '"></div>'
    )


def render_person_box(face: dict[str, Any], dimensions, orientation: int = 1) -> str:
    percent = face_box_percent(face, dimensions, orientation)
    if percent is None:
        left = top = width = height = 0.0
    else:
        left, top, width, height = percent
    css_class = "box suggested" if face["status"] == "forslag" else "box"
    return (
        f'<div class="{css_class}" '
        f'title="{html_escape(face["status"])} face-id {face["faceId"]} score {float(face["similarity"]):.3f}" '
        'style="'
        f'left: {left:.4f}%; '
        f'top: {top:.4f}%; '
        f'width: {width:.4f}%; '
        f'height: {height:.4f}%;'
        '"></div>'
    )


def face_box_percent(
    face: dict[str, Any],
    dimensions,
    orientation: int = 1,
) -> tuple[float, float, float, float] | None:
    if dimensions is None or dimensions.width <= 0 or dimensions.height <= 0:
        return None
    x, y, width, height, box_width, box_height = orient_face_box(
        float(face["x"]),
        float(face["y"]),
        float(face["width"]),
        float(face["height"]),
        float(dimensions.width),
        float(dimensions.height),
        orientation,
    )
    return (
        100.0 * x / box_width,
        100.0 * y / box_height,
        100.0 * width / box_width,
        100.0 * height / box_height,
    )


def orient_face_box(
    x: float,
    y: float,
    width: float,
    height: float,
    image_width: float,
    image_height: float,
    orientation: int = 1,
) -> tuple[float, float, float, float, float, float]:
    if orientation == 2:
        return image_width - x - width, y, width, height, image_width, image_height
    if orientation == 3:
        return (
            image_width - x - width,
            image_height - y - height,
            width,
            height,
            image_width,
            image_height,
        )
    if orientation == 4:
        return x, image_height - y - height, width, height, image_width, image_height
    if orientation == 5:
        return y, x, height, width, image_height, image_width
    if orientation == 6:
        return image_height - y - height, x, height, width, image_height, image_width
    if orientation == 7:
        return (
            image_height - y - height,
            image_width - x - width,
            height,
            width,
            image_height,
            image_width,
        )
    if orientation == 8:
        return y, image_width - x - width, height, width, image_height, image_width
    return x, y, width, height, image_width, image_height


def html_escape(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def person_browser_json_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "path": item["path"],
            "url": item["url"],
            "thumbnailSrc": item.get("thumbnailSrc", ""),
            "kind": "image",
            "name": item["name"],
            "monthKey": item["monthKey"],
            "takenDate": "",
            "dateSource": "",
            "sizeText": item["sizeText"],
        }
        for item in items
    ]


def person_month_key(target: Path, path: Path) -> str:
    parts = relative_to_target(target, path).parts
    if len(parts) >= 3 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].isdigit():
        return f"{parts[0]}-{parts[1]}"
    if parts and parts[0] == "udatert":
        return "udatert"
    return "ukjent"


def format_person_file_size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "-"
    units = ("bytes", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "bytes":
                return f"{size} bytes"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


def safe_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in ("-", "_") else "-" for char in value.strip())
    safe = "-".join(part for part in safe.split("-") if part)
    return safe or "person"


def count_scanned_files(conn: sqlite3.Connection, where_sql: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM scanned_files WHERE {where_sql}").fetchone()[0])


def count_files_with_confirmed_person(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT faces.file_id)
            FROM faces
            JOIN person_faces ON person_faces.face_id = faces.id
            """
        ).fetchone()[0]
    )


def count_files_with_faces_no_confirmed_person(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM scanned_files
            WHERE status = 'ok'
              AND face_count > 0
              AND file_id NOT IN (
                  SELECT DISTINCT faces.file_id
                  FROM faces
                  JOIN person_faces ON person_faces.face_id = faces.id
              )
            """
        ).fetchone()[0]
    )


def count_files_with_confirmed_and_unknown_faces(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT
                    faces.file_id,
                    SUM(CASE WHEN person_faces.face_id IS NOT NULL THEN 1 ELSE 0 END) AS confirmed_count,
                    SUM(CASE WHEN person_faces.face_id IS NULL THEN 1 ELSE 0 END) AS unknown_count
                FROM faces
                LEFT JOIN person_faces ON person_faces.face_id = faces.id
                GROUP BY faces.file_id
                HAVING confirmed_count > 0 AND unknown_count > 0
            )
            """
        ).fetchone()[0]
    )


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if table not in {"scanned_files", "faces"}:
        raise ValueError(f"Ukjent face-tabell: {table}")
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def count_rows_if_table_exists(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def scan_faces(
    target: Path,
    config: FaceRecognitionConfig,
    *,
    limit: int | None = None,
    progress: FaceScanProgress | None = None,
    show_model_output: bool = False,
) -> FaceScanStats:
    stats = FaceScanStats()
    main_conn = db.connect(target)
    face_conn = connect_face_db(target, config)
    try:
        rows = active_image_files(main_conn, limit=limit)
        stats.total = len(rows)
        if progress is not None:
            progress("start", 0, stats.total, stats, None)

        rows_to_scan = []
        for row in rows:
            stats.checked += 1
            file_id = int(row["id"])
            sha256 = str(row["sha256"])
            target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
            if is_file_scanned(face_conn, file_id, sha256):
                stats.skipped += 1
                if progress is not None:
                    progress("check", stats.checked, stats.total, stats, target_path)
                continue
            rows_to_scan.append(row)
            if progress is not None:
                progress("check", stats.checked, stats.total, stats, target_path)
        if not rows_to_scan:
            if progress is not None:
                progress("done", stats.checked, stats.total, stats, None)
            return stats

        if progress is not None:
            progress("load_model", 0, len(rows_to_scan), stats, None)
            if not insightface_model_files_exist(config):
                progress("download_model", 0, len(rows_to_scan), stats, None)
        with suppress_model_output(enabled=not show_model_output):
            app = load_face_app(config)
        for scan_index, row in enumerate(rows_to_scan, start=1):
            file_id = int(row["id"])
            target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
            target_path_key = str(row["target_path_key"])
            sha256 = str(row["sha256"])
            try:
                image = read_image(target_path)
                if image is None:
                    raise ValueError(f"Kunne ikke lese bildefil: {target_path}")
                with suppress_model_output(enabled=not show_model_output):
                    faces = app.get(image)
                replace_file_faces(
                    face_conn,
                    file_id=file_id,
                    target_root=target,
                    target_path=target_path,
                    target_path_key=target_path_key,
                    sha256=sha256,
                    faces=faces,
                    embedding_model=config.model_name,
                )
                stats.scanned += 1
                stats.faces += len(faces)
            except Exception as exc:  # noqa: BLE001 - scan should continue and record failures
                stats.errors += 1
                stats.last_error_path = target_path
                stats.last_error_message = str(exc)
                mark_file_scan_error(
                    face_conn,
                    file_id=file_id,
                    target_root=target,
                    target_path=target_path,
                    target_path_key=target_path_key,
                    sha256=sha256,
                    message=str(exc),
                )
                if progress is not None:
                    progress("error", scan_index, len(rows_to_scan), stats, target_path)
            face_conn.commit()
            if progress is not None:
                progress("scan", scan_index, len(rows_to_scan), stats, target_path)
        if progress is not None:
            progress("done", stats.checked, stats.total, stats, None)
    finally:
        main_conn.close()
        face_conn.close()
    return stats


@contextmanager
def suppress_model_output(*, enabled: bool):
    if not enabled:
        yield
        return
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        with redirect_stdout(devnull), redirect_stderr(devnull), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield


def active_image_files(conn: sqlite3.Connection, *, limit: int | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT id, target_path, target_path_key, sha256, stored_filename
        FROM files
        WHERE deleted_at IS NULL
        ORDER BY imported_at, id
    """
    rows = []
    for row in conn.execute(sql):
        if Path(str(row["stored_filename"])).suffix.lower() in IMAGE_EXTENSIONS:
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
    return rows


def is_file_scanned(conn: sqlite3.Connection, file_id: int, sha256: str) -> bool:
    row = conn.execute(
        "SELECT sha256, status FROM scanned_files WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    return row is not None and row["sha256"] == sha256 and row["status"] == "ok"


def replace_file_faces(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_root: Path,
    target_path: Path,
    target_path_key: str,
    sha256: str,
    faces: list[Any],
    embedding_model: str,
) -> None:
    conn.execute("DELETE FROM faces WHERE file_id = ?", (file_id,))
    for face in faces:
        x1, y1, x2, y2 = face_bbox(face)
        conn.execute(
            """
            INSERT INTO faces(
                file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                detection_score, embedding_model, embedding
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                target_path_key,
                x1,
                y1,
                x2 - x1,
                y2 - y1,
                face_score(face),
                embedding_model,
                embedding_blob(face),
            ),
        )
    conn.execute(
        """
        INSERT INTO scanned_files(
            file_id, target_path, target_path_key, sha256, status, error_message, face_count
        ) VALUES(?, ?, ?, ?, 'ok', NULL, ?)
        ON CONFLICT(file_id) DO UPDATE SET
            target_path = excluded.target_path,
            target_path_key = excluded.target_path_key,
            sha256 = excluded.sha256,
            scanned_at = CURRENT_TIMESTAMP,
            status = 'ok',
            error_message = NULL,
            face_count = excluded.face_count
        """,
        (file_id, db.target_relative_path(target_root, target_path).as_posix(), target_path_key, sha256, len(faces)),
    )


def mark_file_scan_error(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_root: Path,
    target_path: Path,
    target_path_key: str,
    sha256: str,
    message: str,
) -> None:
    conn.execute("DELETE FROM faces WHERE file_id = ?", (file_id,))
    conn.execute(
        """
        INSERT INTO scanned_files(
            file_id, target_path, target_path_key, sha256, status, error_message, face_count
        ) VALUES(?, ?, ?, ?, 'error', ?, 0)
        ON CONFLICT(file_id) DO UPDATE SET
            target_path = excluded.target_path,
            target_path_key = excluded.target_path_key,
            sha256 = excluded.sha256,
            scanned_at = CURRENT_TIMESTAMP,
            status = 'error',
            error_message = excluded.error_message,
            face_count = 0
        """,
        (file_id, db.target_relative_path(target_root, target_path).as_posix(), target_path_key, sha256, message[:1000]),
    )


def load_face_app(config: FaceRecognitionConfig):
    try:
        from insightface.app import FaceAnalysis
    except ImportError as exc:
        raise ValueError(
            "InsightFace er ikke installert. Kjør install-insightface.ps1 fra programmappen."
        ) from exc
    providers = ["CPUExecutionProvider"] if config.provider == "cpu" else None
    normalize_insightface_model_layout(config)
    try:
        app = FaceAnalysis(name=config.model_name, root=str(config.model_root), providers=providers)
    except AssertionError as exc:
        if normalize_insightface_model_layout(config):
            app = FaceAnalysis(name=config.model_name, root=str(config.model_root), providers=providers)
        else:
            raise ValueError(
                "Kunne ikke laste InsightFace-modellen "
                f"{config.model_name!r}. Modellen mangler detection-del eller har feil mappestruktur."
            ) from exc
    app.prepare(ctx_id=-1 if config.provider == "cpu" else 0, det_size=(640, 640))
    remove_insightface_model_zip(config)
    return app


def insightface_model_files_exist(config: FaceRecognitionConfig) -> bool:
    model_dir = config.model_root / "models" / config.model_name
    return model_dir.is_dir() and any(model_dir.rglob("*.onnx"))


def remove_insightface_model_zip(config: FaceRecognitionConfig) -> bool:
    zip_path = config.model_root / "models" / f"{config.model_name}.zip"
    try:
        if not zip_path.is_file():
            return False
        zip_path.unlink()
        return True
    except OSError:
        return False


def normalize_insightface_model_layout(config: FaceRecognitionConfig) -> bool:
    model_dir = config.model_root / "models" / config.model_name
    nested_dir = model_dir / config.model_name
    if not nested_dir.is_dir():
        return False
    try:
        parent_onnx = list(model_dir.glob("*.onnx"))
        nested_onnx = list(nested_dir.glob("*.onnx"))
    except OSError:
        return False
    if parent_onnx or not nested_onnx:
        return False
    for source in nested_onnx:
        destination = model_dir / source.name
        if destination.exists():
            raise ValueError(f"Kan ikke normalisere modellmappe fordi filen finnes fra før: {destination}")
    for source in nested_onnx:
        shutil.move(str(source), str(model_dir / source.name))
    try:
        nested_dir.rmdir()
    except OSError:
        pass
    return True


def read_image(path: Path):
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise ValueError("OpenCV mangler. Installer InsightFace-komponenten på nytt.") from exc
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError as exc:
        raise ValueError(f"Kunne ikke lese bildefil: {path}") from exc
    if data.size == 0:
        return None
    ignore_orientation = getattr(cv2, "IMREAD_IGNORE_ORIENTATION", 0)
    return cv2.imdecode(data, cv2.IMREAD_COLOR | ignore_orientation)


def face_bbox(face: Any) -> tuple[float, float, float, float]:
    bbox = getattr(face, "bbox")
    return float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])


def face_score(face: Any) -> float:
    return float(getattr(face, "det_score", 0.0))


def embedding_blob(face: Any) -> bytes:
    embedding = getattr(face, "embedding")
    if hasattr(embedding, "astype"):
        return embedding.astype("float32").tobytes()
    import array

    values = array.array("f", [float(value) for value in embedding])
    return values.tobytes()


def embedding_from_blob(blob: bytes) -> list[float]:
    import array

    values = array.array("f")
    values.frombytes(blob)
    return list(values)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def average_embedding(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    length = len(vectors[0])
    same_length_vectors = [vector for vector in vectors if len(vector) == length]
    if not same_length_vectors:
        return []
    return [
        sum(vector[index] for vector in same_length_vectors) / len(same_length_vectors)
        for index in range(length)
    ]
