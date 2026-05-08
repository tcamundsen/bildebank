from __future__ import annotations

import json
import math
import os
import sqlite3
import warnings
from collections.abc import Callable
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import db
from .config import FaceRecognitionConfig
from .media import IMAGE_EXTENSIONS, image_dimensions, image_orientation


FACE_DB_FILENAME = ".bilder-faces.sqlite3"
FACE_SCHEMA_VERSION = 2


@dataclass
class FaceScanStats:
    total: int = 0
    checked: int = 0
    skipped: int = 0
    scanned: int = 0
    faces: int = 0
    errors: int = 0


FaceScanProgress = Callable[[str, int, int, FaceScanStats, Path | None], None]
FaceGroupProgress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class FaceReport:
    database_exists: bool
    scanned_files: int = 0
    total_faces: int = 0
    files_with_zero_faces: int = 0
    files_with_one_face: int = 0
    files_with_multiple_faces: int = 0
    scan_errors: int = 0
    top_files: tuple[sqlite3.Row, ...] = ()
    errors: tuple[sqlite3.Row, ...] = ()


@dataclass(frozen=True)
class FaceGroupStats:
    faces: int
    groups: int
    grouped_faces: int
    threshold: float
    skipped_large_groups: int = 0
    skipped_large_faces: int = 0


@dataclass(frozen=True)
class AddGroupToPersonResult:
    person_name: str
    group_index: int
    added_faces: int
    already_linked_faces: int


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
class FaceResetResult:
    mode: str
    removed_persons: int
    removed_person_faces: int
    removed_suggestions: int
    removed_group_runs: int
    removed_groups: int
    removed_group_members: int


@dataclass(frozen=True)
class FaceSuggestStats:
    persons: int
    unknown_faces: int
    suggestions: int
    threshold: float


def face_db_path(target: Path) -> Path:
    return target / FACE_DB_FILENAME


def connect_face_db(target: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(face_db_path(target))
    conn.row_factory = sqlite3.Row
    apply_face_schema(conn)
    conn.commit()
    return conn


def apply_face_schema(conn: sqlite3.Connection) -> None:
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

        CREATE TABLE IF NOT EXISTS face_group_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            threshold REAL NOT NULL,
            method TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS face_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES face_group_runs(id) ON DELETE CASCADE,
            group_index INTEGER NOT NULL,
            member_count INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS face_group_members (
            group_id INTEGER NOT NULL REFERENCES face_groups(id) ON DELETE CASCADE,
            face_id INTEGER NOT NULL,
            similarity REAL NOT NULL,
            PRIMARY KEY(group_id, face_id)
        );

        CREATE INDEX IF NOT EXISTS idx_face_groups_run_id ON face_groups(run_id);
        CREATE INDEX IF NOT EXISTS idx_face_group_members_face_id ON face_group_members(face_id);

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
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(FACE_SCHEMA_VERSION),),
    )


def face_db_summary(target: Path) -> tuple[bool, int, int]:
    path = face_db_path(target)
    if not path.exists():
        return False, 0, 0
    conn = sqlite3.connect(path)
    try:
        scanned = count_rows(conn, "scanned_files")
        faces = count_rows(conn, "faces")
        return True, scanned, faces
    finally:
        conn.close()


def create_person(target: Path, name: str) -> int:
    clean_name = normalize_person_name(name)
    conn = connect_face_db(target)
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


def add_group_to_person(target: Path, person_name: str, group_index: int) -> AddGroupToPersonResult:
    clean_name = normalize_person_name(person_name)
    conn = connect_face_db(target)
    try:
        person_id = require_person(conn, clean_name)
        group = latest_group_by_index(conn, group_index)
        if group is None:
            raise ValueError(f"Fant ikke ansiktsgruppe {group_index}. Kjør bildebank face-group først.")
        face_ids = [
            int(row["face_id"])
            for row in conn.execute(
                "SELECT face_id FROM face_group_members WHERE group_id = ? ORDER BY face_id",
                (int(group["id"]),),
            )
        ]
        added = 0
        already = 0
        for face_id in face_ids:
            conn.execute(
                "DELETE FROM person_faces WHERE face_id = ? AND person_id != ?",
                (face_id, person_id),
            )
            cur = conn.execute(
                "INSERT OR IGNORE INTO person_faces(person_id, face_id) VALUES(?, ?)",
                (person_id, face_id),
            )
            if cur.rowcount:
                added += 1
            else:
                already += 1
            conn.execute("DELETE FROM face_suggestions WHERE face_id = ?", (face_id,))
        conn.execute("UPDATE persons SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (person_id,))
        conn.commit()
        return AddGroupToPersonResult(clean_name, group_index, added, already)
    finally:
        conn.close()


def add_face_to_person(target: Path, person_name: str, face_id: int) -> AddFaceToPersonResult:
    clean_name = normalize_person_name(person_name)
    conn = connect_face_db(target)
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


def remove_face_from_person(target: Path, person_name: str, face_id: int) -> RemoveFaceFromPersonResult:
    clean_name = normalize_person_name(person_name)
    conn = connect_face_db(target)
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


def delete_person(target: Path, person_name: str) -> DeletePersonResult:
    clean_name = normalize_person_name(person_name)
    conn = connect_face_db(target)
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


def reset_face_database(target: Path, *, mode: str) -> FaceResetResult:
    conn = connect_face_db(target)
    try:
        removed_persons = count_rows_if_table_exists(conn, "persons")
        removed_person_faces = count_rows_if_table_exists(conn, "person_faces")
        removed_suggestions = count_rows_if_table_exists(conn, "face_suggestions")
        removed_group_runs = 0
        removed_groups = 0
        removed_group_members = 0
        if mode == "keep-scan":
            removed_group_runs = count_rows_if_table_exists(conn, "face_group_runs")
            removed_groups = count_rows_if_table_exists(conn, "face_groups")
            removed_group_members = count_rows_if_table_exists(conn, "face_group_members")
            conn.execute("DELETE FROM face_group_members")
            conn.execute("DELETE FROM face_groups")
            conn.execute("DELETE FROM face_group_runs")
        elif mode != "keep-scan-and-groups":
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
            removed_group_runs=removed_group_runs,
            removed_groups=removed_groups,
            removed_group_members=removed_group_members,
        )
    finally:
        conn.close()


def suggest_faces(target: Path, *, threshold: float = 0.6) -> FaceSuggestStats:
    path = face_db_path(target)
    if not path.exists():
        raise ValueError("Face-database finnes ikke. Kjør bildebank face-scan først.")
    conn = connect_face_db(target)
    try:
        person_vectors: dict[int, list[list[float]]] = {}
        for row in conn.execute(
            """
            SELECT persons.id AS person_id, faces.embedding
            FROM persons
            JOIN person_faces ON person_faces.person_id = persons.id
            JOIN faces ON faces.id = person_faces.face_id
            ORDER BY persons.id, faces.id
            """
        ):
            person_vectors.setdefault(int(row["person_id"]), []).append(
                embedding_from_blob(bytes(row["embedding"]))
            )
        centroids = {
            person_id: average_embedding(vectors)
            for person_id, vectors in person_vectors.items()
            if vectors
        }
        unknown_faces = list(
            conn.execute(
                """
                SELECT id, embedding
                FROM faces
                WHERE id NOT IN (SELECT face_id FROM person_faces)
                ORDER BY id
                """
            )
        )

        conn.execute("DELETE FROM face_suggestions")
        suggestions = 0
        for face in unknown_faces:
            face_id = int(face["id"])
            vector = embedding_from_blob(bytes(face["embedding"]))
            best_person_id = None
            best_score = 0.0
            for person_id, centroid in centroids.items():
                score = cosine_similarity(vector, centroid)
                if score > best_score:
                    best_person_id = person_id
                    best_score = score
            if best_person_id is not None and best_score >= threshold:
                conn.execute(
                    """
                    INSERT INTO face_suggestions(person_id, face_id, similarity)
                    VALUES(?, ?, ?)
                    """,
                    (best_person_id, face_id, best_score),
                )
                suggestions += 1
        conn.commit()
        return FaceSuggestStats(
            persons=len(centroids),
            unknown_faces=len(unknown_faces),
            suggestions=suggestions,
            threshold=threshold,
        )
    finally:
        conn.close()


def list_face_suggestions(target: Path) -> list[sqlite3.Row]:
    path = face_db_path(target)
    if not path.exists():
        return []
    conn = connect_face_db(target)
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


def list_persons(target: Path) -> list[sqlite3.Row]:
    path = face_db_path(target)
    if not path.exists():
        return []
    conn = connect_face_db(target)
    try:
        return list(
            conn.execute(
                """
                SELECT
                    persons.name,
                    persons.created_at,
                    persons.updated_at,
                    COUNT(person_faces.face_id) AS face_count
                FROM persons
                LEFT JOIN person_faces ON person_faces.person_id = persons.id
                GROUP BY persons.id
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
        raise ValueError(f"Fant ikke ansikt-id {face_id}. Kjør make-face-browser eller make-face-groups-browser for å se id-er.")


def latest_group_by_index(conn: sqlite3.Connection, group_index: int) -> sqlite3.Row | None:
    run = conn.execute("SELECT id FROM face_group_runs ORDER BY id DESC LIMIT 1").fetchone()
    if run is None:
        return None
    return conn.execute(
        "SELECT * FROM face_groups WHERE run_id = ? AND group_index = ?",
        (int(run["id"]), group_index),
    ).fetchone()


def face_report(target: Path, *, limit: int = 20) -> FaceReport:
    path = face_db_path(target)
    if not path.exists():
        return FaceReport(database_exists=False)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return FaceReport(
            database_exists=True,
            scanned_files=count_rows(conn, "scanned_files"),
            total_faces=count_rows(conn, "faces"),
            files_with_zero_faces=count_scanned_files(conn, "status = 'ok' AND face_count = 0"),
            files_with_one_face=count_scanned_files(conn, "status = 'ok' AND face_count = 1"),
            files_with_multiple_faces=count_scanned_files(conn, "status = 'ok' AND face_count > 1"),
            scan_errors=count_scanned_files(conn, "status = 'error'"),
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


def export_face_browser(target: Path, output: Path | None = None) -> Path:
    output_path = output or (target / "faces.html")
    path = face_db_path(target)
    if not path.exists():
        raise ValueError("Face-database finnes ikke. Kjør bildebank face-scan først.")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        items = face_browser_items(target, conn)
    finally:
        conn.close()
    output_path.write_text(render_face_browser_html(items), encoding="utf-8", newline="\n")
    return output_path


def group_faces(
    target: Path,
    *,
    threshold: float = 0.6,
    max_size: int | None = None,
    progress: FaceGroupProgress | None = None,
) -> FaceGroupStats:
    path = face_db_path(target)
    if not path.exists():
        raise ValueError("Face-database finnes ikke. Kjør bildebank face-scan først.")
    conn = connect_face_db(target)
    try:
        face_rows = list(
            conn.execute(
                """
                SELECT id, embedding
                FROM faces
                ORDER BY id
                """
            )
        )
        vectors = [(int(row["id"]), embedding_from_blob(bytes(row["embedding"]))) for row in face_rows]
        total_pairs = len(vectors) * (len(vectors) - 1) // 2
        if progress is not None:
            progress("start", 0, total_pairs)
        parent, similarities = similar_face_pairs(vectors, threshold=threshold, progress=progress)
        if progress is not None:
            progress("build_groups", len(vectors), len(vectors))

        grouped: dict[int, list[int]] = {}
        for face_id, _vector in vectors:
            grouped.setdefault(find(parent, face_id), []).append(face_id)
        candidate_groups = [sorted(members) for members in grouped.values() if len(members) >= 2]
        skipped_large_groups = 0
        skipped_large_faces = 0
        if max_size is None:
            groups = candidate_groups
        else:
            groups = []
            for members in candidate_groups:
                if len(members) > max_size:
                    skipped_large_groups += 1
                    skipped_large_faces += len(members)
                else:
                    groups.append(members)
        groups.sort(key=lambda members: (-len(members), members[0]))

        if progress is not None:
            progress("write", 0, len(groups))
        conn.execute("DELETE FROM face_group_members")
        conn.execute("DELETE FROM face_groups")
        conn.execute("DELETE FROM face_group_runs")
        run_id = int(
            conn.execute(
                "INSERT INTO face_group_runs(threshold, method) VALUES(?, 'cosine-threshold') RETURNING id",
                (threshold,),
            ).fetchone()["id"]
        )
        grouped_faces = 0
        for group_index, members in enumerate(groups, start=1):
            grouped_faces += len(members)
            group_id = int(
                conn.execute(
                    """
                    INSERT INTO face_groups(run_id, group_index, member_count)
                    VALUES(?, ?, ?)
                    RETURNING id
                    """,
                    (run_id, group_index, len(members)),
                ).fetchone()["id"]
            )
            for face_id in members:
                conn.execute(
                    """
                    INSERT INTO face_group_members(group_id, face_id, similarity)
                    VALUES(?, ?, ?)
                    """,
                    (group_id, face_id, best_group_similarity(face_id, members, similarities)),
                )
            if progress is not None:
                progress("write", group_index, len(groups))
        conn.commit()
        if progress is not None:
            progress("done", total_pairs, total_pairs)
        return FaceGroupStats(
            faces=len(vectors),
            groups=len(groups),
            grouped_faces=grouped_faces,
            threshold=threshold,
            skipped_large_groups=skipped_large_groups,
            skipped_large_faces=skipped_large_faces,
        )
    finally:
        conn.close()


def export_face_groups_browser(target: Path, output: Path | None = None) -> Path:
    output_path = output or (target / "face-groups.html")
    path = face_db_path(target)
    if not path.exists():
        raise ValueError("Face-database finnes ikke. Kjør bildebank face-scan først.")
    conn = connect_face_db(target)
    try:
        items = face_group_browser_items(target, conn)
    finally:
        conn.close()
    output_path.write_text(render_face_groups_html(items), encoding="utf-8", newline="\n")
    return output_path


def export_person_browser(
    target: Path,
    person_name: str,
    output: Path | None = None,
    *,
    month_preview_limit: int | None = None,
) -> Path:
    clean_name = normalize_person_name(person_name)
    output_path = output or (target / f"person-{safe_filename(clean_name)}.html")
    path = face_db_path(target)
    if not path.exists():
        raise ValueError("Face-database finnes ikke. Kjør bildebank face-scan først.")
    conn = connect_face_db(target)
    try:
        person = conn.execute("SELECT id, name FROM persons WHERE name = ?", (clean_name,)).fetchone()
        if person is None:
            raise ValueError(f"Fant ikke person: {clean_name}")
        items = person_browser_items(target, conn, person_id=int(person["id"]))
    finally:
        conn.close()
    output_path.write_text(
        render_person_browser_html(clean_name, items, month_preview_limit=month_preview_limit),
        encoding="utf-8",
        newline="\n",
    )
    return output_path


def face_group_browser_items(target: Path, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    run = conn.execute("SELECT * FROM face_group_runs ORDER BY id DESC LIMIT 1").fetchone()
    if run is None:
        return []
    rows = conn.execute(
        """
        SELECT
            face_groups.group_index,
            face_groups.member_count,
            faces.id AS face_id,
            face_group_members.similarity,
            scanned_files.target_path,
            faces.bbox_x,
            faces.bbox_y,
            faces.bbox_width,
            faces.bbox_height,
            faces.detection_score
        FROM face_groups
        JOIN face_group_members ON face_group_members.group_id = face_groups.id
        JOIN faces ON faces.id = face_group_members.face_id
        JOIN scanned_files ON scanned_files.file_id = faces.file_id
        WHERE face_groups.run_id = ?
        ORDER BY face_groups.group_index, face_group_members.similarity DESC, faces.id
        """,
        (run["id"],),
    )
    groups: dict[int, dict[str, Any]] = {}
    for row in rows:
        group_index = int(row["group_index"])
        target_path = Path(str(row["target_path"]))
        dimensions = image_dimensions(target_path)
        orientation = image_orientation(target_path)
        face = {
            "faceId": int(row["face_id"]),
            "path": display_relative_path(target, target_path),
            "url": path_to_url(relative_to_target(target, target_path)),
            "x": float(row["bbox_x"]),
            "y": float(row["bbox_y"]),
            "width": float(row["bbox_width"]),
            "height": float(row["bbox_height"]),
            "score": float(row["detection_score"]),
            "similarity": float(row["similarity"]),
            "dimensions": dimensions,
            "orientation": orientation,
        }
        percent = face_box_percent(face, dimensions, orientation)
        if percent is not None:
            left, top, width, height = percent
            face["left"] = left
            face["top"] = top
            face["boxWidth"] = width
            face["boxHeight"] = height
        groups.setdefault(
            group_index,
            {
                "index": group_index,
                "memberCount": int(row["member_count"]),
                "personName": person_name_for_group(conn, group_index),
                "faces": [],
            },
        )["faces"].append(face)
    return list(groups.values())


def person_browser_items(target: Path, conn: sqlite3.Connection, *, person_id: int) -> list[dict[str, Any]]:
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
    for row in rows:
        target_path = Path(str(row["target_path"]))
        key = str(target_path)
        dimensions = image_dimensions(target_path)
        orientation = image_orientation(target_path)
        item = grouped.setdefault(
            key,
            {
                "path": display_relative_path(target, target_path),
                "url": path_to_url(relative_to_target(target, target_path)),
                "name": target_path.name,
                "monthKey": person_month_key(target, target_path),
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
    return list(grouped.values())


def person_name_for_group(conn: sqlite3.Connection, group_index: int) -> str | None:
    group = latest_group_by_index(conn, group_index)
    if group is None:
        return None
    row = conn.execute(
        """
        SELECT persons.name
        FROM face_group_members
        JOIN person_faces ON person_faces.face_id = face_group_members.face_id
        JOIN persons ON persons.id = person_faces.person_id
        WHERE face_group_members.group_id = ?
        GROUP BY persons.id
        ORDER BY COUNT(*) DESC, persons.name
        LIMIT 1
        """,
        (int(group["id"]),),
    ).fetchone()
    return str(row["name"]) if row is not None else None


def face_browser_items(target: Path, conn: sqlite3.Connection) -> list[dict[str, Any]]:
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
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        target_path = Path(str(row["target_path"]))
        key = str(target_path)
        dimensions = image_dimensions(target_path)
        orientation = image_orientation(target_path)
        item = grouped.setdefault(
            key,
            {
                "path": display_relative_path(target, target_path),
                "url": path_to_url(relative_to_target(target, target_path)),
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
    return list(grouped.values())


def relative_to_target(target: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(target.resolve())
    except ValueError:
        import os

        return Path(os.path.relpath(path, target))


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
      --accent: #d62f2f;
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
      border: 2px solid var(--accent);
      background: rgb(214 47 47 / 12%);
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


def render_face_groups_html(groups: list[dict[str, Any]]) -> str:
    groups_json = json.dumps(face_groups_json_items(groups), ensure_ascii=False)
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ansiktsgrupper</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f5;
      --text: #202020;
      --muted: #666;
      --border: #d8d8d2;
      --panel: #fff;
      --accent: #d62f2f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .app {{
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }}
    header {{
      padding: 12px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
      display: grid;
      gap: 10px;
    }}
    h1 {{ margin: 0; font-size: 20px; }}
    .topline, .controls {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    button {{
      border: 1px solid var(--border);
      background: #f0f0eb;
      color: var(--text);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      cursor: pointer;
      min-height: 38px;
    }}
    button:hover {{ background: #e6e6df; }}
    button:disabled {{ opacity: 0.45; cursor: default; }}
    .status {{ color: var(--muted); font-size: 14px; }}
    .command {{
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      background: #f0f0eb;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 7px;
      overflow-wrap: anywhere;
      font-size: 13px;
    }}
    main {{
      min-height: 0;
      padding: 16px;
      overflow: auto;
    }}
    .faces {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 14px;
      align-content: start;
    }}
    .face {{
      display: grid;
      gap: 6px;
      min-width: 0;
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
      border: 2px solid var(--accent);
      background: rgb(214 47 47 / 12%);
      pointer-events: none;
    }}
    .meta {{
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
      padding: 0 8px 8px;
    }}
    .empty {{ margin: 0; color: var(--muted); }}
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div class="topline">
        <h1>Ansiktsgrupper</h1>
        <span id="status" class="status"></span>
      </div>
      <div class="controls">
        <button id="prevGroup" type="button">Forrige gruppe</button>
        <button id="nextGroup" type="button">Neste gruppe</button>
        <span id="groupTitle" class="status"></span>
      </div>
      <div id="person" class="status"></div>
      <div id="command" class="command"></div>
    </header>
    <main id="main">
      <p class="empty">Ingen grupper beregnet ennå. Kjør bildebank face-group først.</p>
    </main>
  </div>
  <script>
    const groups = {groups_json};
    let groupIndex = 0;
    const statusEl = document.getElementById("status");
    const groupTitleEl = document.getElementById("groupTitle");
    const personEl = document.getElementById("person");
    const commandEl = document.getElementById("command");
    const mainEl = document.getElementById("main");
    const prevButton = document.getElementById("prevGroup");
    const nextButton = document.getElementById("nextGroup");
    prevButton.addEventListener("click", () => moveGroup(-1));
    nextButton.addEventListener("click", () => moveGroup(1));
    document.addEventListener("keydown", event => {{
      if (event.key === "ArrowLeft") {{ event.preventDefault(); moveGroup(-1); }}
      if (event.key === "ArrowRight") {{ event.preventDefault(); moveGroup(1); }}
    }});
    init();
    function init() {{
      statusEl.textContent = `${{groups.length}} grupper`;
      if (groups.length === 0) {{
        prevButton.disabled = true;
        nextButton.disabled = true;
        commandEl.textContent = "";
        return;
      }}
      renderGroup();
    }}
    function moveGroup(delta) {{
      const next = groupIndex + delta;
      if (next < 0 || next >= groups.length) return;
      groupIndex = next;
      renderGroup();
    }}
    function renderGroup() {{
      const group = groups[groupIndex];
      groupTitleEl.textContent = `Gruppe ${{group.index}} (${{group.memberCount}} ansikter), ${{groupIndex + 1}}/${{groups.length}}`;
      personEl.textContent = group.personName ? `Koblet til: ${{group.personName}}` : "Forslag. Ikke bekreftet person.";
      commandEl.textContent = `bildebank face-person-add-group "Navn" ${{group.index}}`;
      prevButton.disabled = groupIndex === 0;
      nextButton.disabled = groupIndex === groups.length - 1;
      const facesEl = document.createElement("div");
      facesEl.className = "faces";
      for (const face of group.faces) facesEl.append(renderFace(face));
      mainEl.replaceChildren(facesEl);
    }}
    function renderFace(face) {{
      const card = document.createElement("div");
      card.className = "face";
      const media = document.createElement("div");
      media.className = "media";
      const link = document.createElement("a");
      link.href = face.url;
      link.target = "_blank";
      link.title = "Åpne bildet";
      const img = document.createElement("img");
      img.src = face.url;
      img.alt = "";
      link.append(img);
      media.append(link);
      if (face.left !== undefined) {{
        const box = document.createElement("div");
        box.className = "box";
        box.style.left = `${{face.left.toFixed(4)}}%`;
        box.style.top = `${{face.top.toFixed(4)}}%`;
        box.style.width = `${{face.boxWidth.toFixed(4)}}%`;
        box.style.height = `${{face.boxHeight.toFixed(4)}}%`;
        media.append(box);
      }}
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `face-id ${{face.faceId}}, likhet ${{face.similarity.toFixed(3)}} - ${{face.path}}`;
      card.append(media, meta);
      return card;
    }}
  </script>
</body>
</html>
"""


def render_person_browser_html(
    person_name: str,
    items: list[dict[str, Any]],
    *,
    month_preview_limit: int | None = None,
) -> str:
    items_json = json.dumps(person_browser_json_items(items), ensure_ascii=False)
    month_preview_limit_json = json.dumps(month_preview_limit)
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(person_name)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f5;
      --text: #202020;
      --muted: #666;
      --border: #d8d8d2;
      --panel: #fff;
      --accent: #7db7ff;
      --confirmed: #2fbf71;
      --suggested: #e19b2d;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .app {{
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
    }}
    header {{
      padding: 12px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
      display: grid;
      gap: 10px;
    }}
    .topline, .controls {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    h1 {{ margin: 0; font-size: 20px; }}
    button {{
      border: 1px solid var(--border);
      background: #f0f0eb;
      color: var(--text);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      cursor: pointer;
      min-height: 38px;
    }}
    button:hover {{ background: #e6e6df; }}
    button:disabled {{
      background: #eeeeea;
      color: #9a9a92;
      border-color: #ddddd7;
      cursor: default;
    }}
    .status {{ color: var(--muted); font-size: 14px; }}
    .position {{ color: var(--accent); font-weight: 650; }}
    main {{
      min-height: 0;
      padding: 16px;
      display: grid;
      place-items: center;
      overflow: hidden;
    }}
    .viewer {{
      width: 100%;
      height: 100%;
      min-width: 0;
      min-height: 0;
      display: grid;
      place-items: center;
      background: #0e0e0e;
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    .media {{
      position: relative;
      width: 100%;
      height: 100%;
      display: grid;
      place-items: center;
    }}
    .media img {{
      max-width: 100vw;
      max-height: calc(100vh - 11rem);
      width: 100%;
      height: 100%;
      object-fit: contain;
      object-position: center center;
      display: block;
    }}
    .box {{
      position: absolute;
      border: 2px solid var(--confirmed);
      background: rgb(47 191 113 / 13%);
      pointer-events: none;
    }}
    .box.suggested {{
      border-color: var(--suggested);
      background: rgb(225 155 45 / 14%);
    }}
    .month-grid {{
      width: 100%;
      height: 100%;
      min-width: 0;
      min-height: 0;
      overflow: auto;
      padding: 12px;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
      grid-auto-rows: 130px;
      gap: 8px;
      align-content: start;
    }}
    .thumb {{
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #181818;
      color: var(--muted);
      display: grid;
      place-items: center;
      min-width: 0;
      min-height: 0;
      overflow: hidden;
      padding: 0;
    }}
    .thumb:hover {{ border-color: var(--accent); background: #242424; }}
    .thumb img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
    .empty {{
      margin: 0;
      color: var(--muted);
      text-align: center;
      max-width: 560px;
      line-height: 1.5;
      padding: 24px;
    }}
    footer {{
      background: var(--panel);
      border-top: 1px solid var(--border);
      padding: 8px 12px;
      font-size: 13px;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }}
    footer a {{ color: var(--muted); text-decoration: none; }}
    footer a:hover {{ color: var(--accent); text-decoration: underline; }}
    .detail {{ color: var(--muted); margin-left: 8px; }}
    .confirmed {{ color: var(--confirmed); }}
    .suggested-text {{ color: var(--suggested); }}
    .meta-line {{ overflow: hidden; text-overflow: ellipsis; }}
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div class="topline">
        <h1>{html_escape(person_name)}</h1>
        <span id="status" class="status"></span>
      </div>
      <div class="controls">
        <button id="prevYear" type="button">Forrige år</button>
        <button id="nextYear" type="button">Neste år</button>
        <button id="prevMonth" type="button">Forrige måned</button>
        <button id="nextMonth" type="button">Neste måned</button>
        <button id="prevItem" type="button">Forrige bilde</button>
        <button id="nextItem" type="button">Neste bilde</button>
        <span id="position" class="position"></span>
      </div>
    </header>
    <main>
      <div id="viewer" class="viewer">
        <div class="empty">Ingen bekreftede ansikter eller forslag for denne personen ennå.</div>
      </div>
    </main>
    <footer><a id="filename" href="#">Ingen fil valgt</a><span id="details" class="detail"></span></footer>
  </div>
  <script>
    const embeddedItems = {items_json};
    const MONTH_PREVIEW_LIMIT = {month_preview_limit_json};
    const state = {{ months: [], monthIndex: 0, itemIndex: 0, viewMode: "item" }};
    const statusEl = document.getElementById("status");
    const positionEl = document.getElementById("position");
    const viewer = document.getElementById("viewer");
    const filenameEl = document.getElementById("filename");
    const detailsEl = document.getElementById("details");
    const buttons = {{
      prevYear: document.getElementById("prevYear"),
      nextYear: document.getElementById("nextYear"),
      prevMonth: document.getElementById("prevMonth"),
      nextMonth: document.getElementById("nextMonth"),
      prevItem: document.getElementById("prevItem"),
      nextItem: document.getElementById("nextItem")
    }};
    buttons.prevYear.addEventListener("click", () => moveYear(-1));
    buttons.nextYear.addEventListener("click", () => moveYear(1));
    buttons.prevMonth.addEventListener("click", () => moveMonth(-1));
    buttons.nextMonth.addEventListener("click", () => moveMonth(1));
    buttons.prevItem.addEventListener("click", () => moveItem(-1));
    buttons.nextItem.addEventListener("click", () => moveItem(1));
    document.addEventListener("keydown", event => {{
      if (event.key === "ArrowLeft") {{ event.preventDefault(); moveItem(-1); }}
      if (event.key === "ArrowRight") {{ event.preventDefault(); moveItem(1); }}
      if (event.key === "ArrowUp") {{ event.preventDefault(); moveMonth(-1); }}
      if (event.key === "ArrowDown") {{ event.preventDefault(); moveMonth(1); }}
      if (event.key === "PageUp") {{ event.preventDefault(); moveYear(-1); }}
      if (event.key === "PageDown") {{ event.preventDefault(); moveYear(1); }}
    }});
    init();
    function init() {{
      const items = embeddedItems.slice().sort(compareItems);
      state.months = buildMonths(items);
      statusEl.textContent = `${{items.length}} bilder, ${{state.months.length}} måneder`;
      if (items.length === 0) {{
        setButtonsEnabled(false);
        return;
      }}
      setButtonsEnabled(true);
      render();
    }}
    function compareItems(a, b) {{
      return a.monthKey.localeCompare(b.monthKey, "nb") ||
        a.path.localeCompare(b.path, "nb", {{ numeric: true }});
    }}
    function buildMonths(items) {{
      const map = new Map();
      for (const item of items) {{
        if (!map.has(item.monthKey)) map.set(item.monthKey, []);
        map.get(item.monthKey).push(item);
      }}
      return Array.from(map.entries()).map(([key, monthItems]) => ({{ key, items: monthItems }}));
    }}
    function currentMonth() {{ return state.months[state.monthIndex] || null; }}
    function currentItem() {{
      const month = currentMonth();
      return month ? month.items[state.itemIndex] : null;
    }}
    function moveYear(delta) {{
      const month = currentMonth();
      if (!month) return;
      const year = month.key.slice(0, 4);
      const years = Array.from(new Set(state.months.map(item => item.key.slice(0, 4))));
      const nextYear = years[years.indexOf(year) + delta];
      if (!nextYear) return;
      state.monthIndex = state.months.findIndex(item => item.key.startsWith(nextYear));
      state.itemIndex = 0;
      state.viewMode = "month";
      render();
    }}
    function moveMonth(delta) {{
      const next = state.monthIndex + delta;
      if (next < 0 || next >= state.months.length) return;
      state.monthIndex = next;
      state.itemIndex = 0;
      state.viewMode = "month";
      render();
    }}
    function moveItem(delta) {{
      const month = currentMonth();
      if (!month) return;
      if (state.viewMode === "month") {{
        state.itemIndex = delta < 0 ? month.items.length - 1 : 0;
        state.viewMode = "item";
        render();
        return;
      }}
      const nextItem = state.itemIndex + delta;
      if (nextItem >= 0 && nextItem < month.items.length) {{
        state.itemIndex = nextItem;
        render();
        return;
      }}
      const nextMonth = state.monthIndex + (delta > 0 ? 1 : -1);
      if (nextMonth < 0 || nextMonth >= state.months.length) return;
      state.monthIndex = nextMonth;
      state.itemIndex = delta > 0 ? 0 : state.months[nextMonth].items.length - 1;
      render();
    }}
    function render() {{
      if (state.viewMode === "month") renderMonth();
      else renderItem();
    }}
    function renderItem() {{
      const item = currentItem();
      if (!item) return;
      const media = document.createElement("div");
      media.className = "media";
      const img = document.createElement("img");
      img.src = item.url;
      img.alt = item.name;
      img.addEventListener("load", () => positionBoxes(media, img, item.faces));
      media.append(img);
      for (const face of item.faces) {{
        const box = document.createElement("div");
        box.className = face.status === "forslag" ? "box suggested" : "box";
        box.title = `${{face.status}} face-id ${{face.faceId}} score ${{face.similarity.toFixed(3)}}`;
        box.dataset.faceId = String(face.faceId);
        media.append(box);
      }}
      viewer.replaceChildren(media);
      const month = currentMonth();
      positionEl.textContent = `${{month.key}} ${{state.itemIndex + 1}}/${{month.items.length}}`;
      filenameEl.textContent = `${{item.path}} (${{item.sizeText}})`;
      filenameEl.href = item.url;
      filenameEl.target = "_blank";
      detailsEl.innerHTML = item.faces.map(face => {{
        const cls = face.status === "forslag" ? "suggested-text" : "confirmed";
        return `<span class="${{cls}}">${{face.status}}: face-id ${{face.faceId}}, score ${{face.similarity.toFixed(3)}}<\\/span>`;
      }}).join(" ");
      updateButtons();
    }}
    function renderMonth() {{
      const month = currentMonth();
      if (!month) return;
      const grid = document.createElement("div");
      grid.className = "month-grid";
      for (const item of representativeItems(month.items, MONTH_PREVIEW_LIMIT)) {{
        const index = month.items.indexOf(item);
        const button = document.createElement("button");
        button.className = "thumb";
        button.type = "button";
        button.title = item.path;
        button.addEventListener("click", () => {{
          state.itemIndex = index;
          state.viewMode = "item";
          render();
        }});
        const img = document.createElement("img");
        img.src = item.url;
        img.alt = item.name;
        img.loading = "lazy";
        button.append(img);
        grid.append(button);
      }}
      viewer.replaceChildren(grid);
      positionEl.textContent = `${{month.key}} oversikt (${{month.items.length}} bilder)`;
      filenameEl.textContent = `Månedsoversikt: ${{month.key}}`;
      filenameEl.removeAttribute("href");
      filenameEl.removeAttribute("target");
      detailsEl.textContent = "";
      updateButtons();
    }}
    function positionBoxes(media, img, faces) {{
      const rendered = renderedImageRect(media, img);
      for (const face of faces) {{
        const box = media.querySelector(`[data-face-id="${{face.faceId}}"]`);
        if (!box || face.left === undefined) continue;
        box.style.left = `${{rendered.left + rendered.width * face.left / 100}}px`;
        box.style.top = `${{rendered.top + rendered.height * face.top / 100}}px`;
        box.style.width = `${{rendered.width * face.boxWidth / 100}}px`;
        box.style.height = `${{rendered.height * face.boxHeight / 100}}px`;
      }}
    }}
    function renderedImageRect(media, img) {{
      const mediaRect = media.getBoundingClientRect();
      const mediaRatio = mediaRect.width / mediaRect.height;
      const imageRatio = img.naturalWidth / img.naturalHeight;
      if (imageRatio > mediaRatio) {{
        const width = mediaRect.width;
        const height = width / imageRatio;
        return {{ left: 0, top: (mediaRect.height - height) / 2, width, height }};
      }}
      const height = mediaRect.height;
      const width = height * imageRatio;
      return {{ left: (mediaRect.width - width) / 2, top: 0, width, height }};
    }}
    function representativeItems(items, limit) {{
      if (limit === null) return items;
      if (items.length <= limit) return items;
      if (limit === 1) return [items[0]];
      const selected = [];
      const last = items.length - 1;
      const selectedIndexes = new Set();
      for (let i = 0; i < limit; i += 1) {{
        selectedIndexes.add(Math.round((i * last) / (limit - 1)));
      }}
      for (const index of Array.from(selectedIndexes).sort((a, b) => a - b)) {{
        selected.push(items[index]);
      }}
      return selected;
    }}
    function setButtonsEnabled(enabled) {{
      for (const button of Object.values(buttons)) button.disabled = !enabled;
    }}
    function updateButtons() {{
      const month = currentMonth();
      const years = Array.from(new Set(state.months.map(item => item.key.slice(0, 4))));
      const currentYear = month ? month.key.slice(0, 4) : "";
      const currentYearIndex = years.indexOf(currentYear);
      buttons.prevYear.disabled = currentYearIndex <= 0;
      buttons.nextYear.disabled = currentYearIndex < 0 || currentYearIndex >= years.length - 1;
      buttons.prevMonth.disabled = state.monthIndex <= 0;
      buttons.nextMonth.disabled = state.monthIndex >= state.months.length - 1;
      if (state.viewMode === "month") {{
        buttons.prevItem.disabled = false;
        buttons.nextItem.disabled = false;
        return;
      }}
      buttons.prevItem.disabled = state.monthIndex === 0 && state.itemIndex === 0;
      buttons.nextItem.disabled =
        state.monthIndex === state.months.length - 1 &&
        month &&
        state.itemIndex === month.items.length - 1;
    }}
    window.addEventListener("resize", () => {{
      if (state.viewMode !== "item") return;
      const item = currentItem();
      const media = viewer.querySelector(".media");
      const img = viewer.querySelector("img");
      if (item && media && img) positionBoxes(media, img, item.faces);
    }});
  </script>
</body>
</html>
"""


def render_face_group_section(group: dict[str, Any]) -> str:
    faces = "\n".join(render_group_face(face) for face in group["faces"])
    person = (
        f'<p class="person">Koblet til: {html_escape(group["personName"])}</p>'
        if group.get("personName")
        else '<p class="person">Forslag. Ikke bekreftet person.</p>'
    )
    return f"""<section>
  <h2>Gruppe {group['index']} ({group['memberCount']} ansikter)</h2>
  {person}
  <p class="command">bildebank face-person-add-group "Navn" {group['index']}</p>
  <div class="faces">
    {faces}
  </div>
</section>"""


def render_group_face(face: dict[str, Any]) -> str:
    style = crop_image_style(face)
    return f"""<div class="face">
  <div class="crop"><img src="{html_escape(face['url'])}" alt="" style="{style}"></div>
  <div class="meta">face-id {face['faceId']}<br>likhet {float(face['similarity']):.3f}<br>{html_escape(face['path'])}</div>
</div>"""


def crop_image_style(face: dict[str, Any]) -> str:
    crop = face_crop_percent(face, face["dimensions"], int(face.get("orientation", 1)))
    if crop is None:
        return "left: 0; top: 0; width: 100%;"
    left, top, width, _height = crop
    return (
        f"left: {-100.0 * left / width:.4f}%; "
        f"top: {-100.0 * top / width:.4f}%; "
        f"width: {10000.0 / width:.4f}%;"
    )


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


def face_crop_percent(
    face: dict[str, Any],
    dimensions,
    orientation: int = 1,
) -> tuple[float, float, float, float] | None:
    box = face_box_percent(face, dimensions, orientation)
    if box is None:
        return None
    left, top, width, height = box
    size = max(width, height) * 2.2
    size = min(max(size, 12.0), 100.0)
    center_x = left + width / 2
    center_y = top + height / 2
    crop_left = min(max(center_x - size / 2, 0.0), max(100.0 - size, 0.0))
    crop_top = min(max(center_y - size / 2, 0.0), max(100.0 - size, 0.0))
    return crop_left, crop_top, size, size


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
            "name": item["name"],
            "monthKey": item["monthKey"],
            "sizeText": item["sizeText"],
            "faces": item["faces"],
        }
        for item in items
    ]


def face_groups_json_items(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "index": group["index"],
            "memberCount": group["memberCount"],
            "personName": group["personName"],
            "faces": [
                {
                    key: value
                    for key, value in face.items()
                    if key != "dimensions"
                }
                for face in group["faces"]
            ],
        }
        for group in groups
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
    face_conn = connect_face_db(target)
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
            target_path = Path(str(row["target_path"]))
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
        with suppress_model_output(enabled=not show_model_output):
            app = load_face_app(config)
        for scan_index, row in enumerate(rows_to_scan, start=1):
            file_id = int(row["id"])
            target_path = Path(str(row["target_path"]))
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
                    target_path=target_path,
                    target_path_key=target_path_key,
                    sha256=sha256,
                    faces=faces,
                    embedding_model=config.model_name,
                )
                stats.scanned += 1
                stats.faces += len(faces)
            except Exception as exc:  # noqa: BLE001 - scan should continue and record failures
                mark_file_scan_error(
                    face_conn,
                    file_id=file_id,
                    target_path=target_path,
                    target_path_key=target_path_key,
                    sha256=sha256,
                    message=str(exc),
                )
                stats.errors += 1
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
        (file_id, str(target_path), target_path_key, sha256, len(faces)),
    )


def mark_file_scan_error(
    conn: sqlite3.Connection,
    *,
    file_id: int,
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
        (file_id, str(target_path), target_path_key, sha256, message[:1000]),
    )


def load_face_app(config: FaceRecognitionConfig):
    try:
        from insightface.app import FaceAnalysis
    except ImportError as exc:
        raise ValueError(
            "InsightFace er ikke installert. Kjør install-insightface.ps1 fra programmappen."
        ) from exc
    providers = ["CPUExecutionProvider"] if config.provider == "cpu" else None
    app = FaceAnalysis(name=config.model_name, root=str(config.model_root), providers=providers)
    app.prepare(ctx_id=-1 if config.provider == "cpu" else 0, det_size=(640, 640))
    return app


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
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


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


def similar_face_pairs(
    vectors: list[tuple[int, list[float]]],
    *,
    threshold: float,
    progress: FaceGroupProgress | None = None,
) -> tuple[dict[int, int], dict[tuple[int, int], float]]:
    try:
        return similar_face_pairs_numpy(vectors, threshold=threshold, progress=progress)
    except (ImportError, ValueError):
        return similar_face_pairs_python(vectors, threshold=threshold, progress=progress)


def similar_face_pairs_python(
    vectors: list[tuple[int, list[float]]],
    *,
    threshold: float,
    progress: FaceGroupProgress | None = None,
) -> tuple[dict[int, int], dict[tuple[int, int], float]]:
    parent = {face_id: face_id for face_id, _vector in vectors}
    similarities: dict[tuple[int, int], float] = {}
    total_pairs = len(vectors) * (len(vectors) - 1) // 2
    compared_pairs = 0
    for index, (left_id, left_vector) in enumerate(vectors):
        for right_id, right_vector in vectors[index + 1 :]:
            compared_pairs += 1
            score = cosine_similarity(left_vector, right_vector)
            if score >= threshold:
                union(parent, left_id, right_id)
                similarities[(min(left_id, right_id), max(left_id, right_id))] = score
            if progress is not None:
                progress("compare", compared_pairs, total_pairs)
    return parent, similarities


def similar_face_pairs_numpy(
    vectors: list[tuple[int, list[float]]],
    *,
    threshold: float,
    progress: FaceGroupProgress | None = None,
    block_size: int = 512,
) -> tuple[dict[int, int], dict[tuple[int, int], float]]:
    import numpy as np

    parent = {face_id: face_id for face_id, _vector in vectors}
    similarities: dict[tuple[int, int], float] = {}
    if len(vectors) < 2:
        if progress is not None:
            progress("compare", 0, 0)
        return parent, similarities

    dimensions = {len(vector) for _face_id, vector in vectors}
    if len(dimensions) != 1 or 0 in dimensions:
        raise ValueError("Kan ikke gruppere embeddings med ulik dimensjon med NumPy.")

    face_ids = [face_id for face_id, _vector in vectors]
    matrix = np.asarray([vector for _face_id, vector in vectors], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    nonzero = norms > 0
    matrix[nonzero] = matrix[nonzero] / norms[nonzero, None]
    matrix[~nonzero] = 0.0

    total_pairs = len(vectors) * (len(vectors) - 1) // 2
    compared_pairs = 0
    count = len(vectors)
    for left_start in range(0, count, block_size):
        left_end = min(left_start + block_size, count)
        left = matrix[left_start:left_end]
        for right_start in range(left_start, count, block_size):
            right_end = min(right_start + block_size, count)
            right = matrix[right_start:right_end]
            scores = left @ right.T
            if left_start == right_start:
                row_indexes, col_indexes = np.where(np.triu(scores >= threshold, k=1))
            else:
                row_indexes, col_indexes = np.where(scores >= threshold)
            for row_index, col_index in zip(row_indexes.tolist(), col_indexes.tolist()):
                left_id = face_ids[left_start + row_index]
                right_id = face_ids[right_start + col_index]
                score = float(scores[row_index, col_index])
                union(parent, left_id, right_id)
                similarities[(min(left_id, right_id), max(left_id, right_id))] = score
            compared_pairs += block_pair_count(left_start, left_end, right_start, right_end)
            if progress is not None:
                progress("compare", min(compared_pairs, total_pairs), total_pairs)
    return parent, similarities


def block_pair_count(left_start: int, left_end: int, right_start: int, right_end: int) -> int:
    if left_start == right_start:
        block_count = left_end - left_start
        return block_count * (block_count - 1) // 2
    return (left_end - left_start) * (right_end - right_start)


def find(parent: dict[int, int], value: int) -> int:
    root = value
    while parent[root] != root:
        root = parent[root]
    while parent[value] != value:
        next_value = parent[value]
        parent[value] = root
        value = next_value
    return root


def union(parent: dict[int, int], left: int, right: int) -> None:
    left_root = find(parent, left)
    right_root = find(parent, right)
    if left_root != right_root:
        parent[max(left_root, right_root)] = min(left_root, right_root)


def best_group_similarity(face_id: int, members: list[int], similarities: dict[tuple[int, int], float]) -> float:
    scores = [
        similarities[(min(face_id, other_id), max(face_id, other_id))]
        for other_id in members
        if other_id != face_id and (min(face_id, other_id), max(face_id, other_id)) in similarities
    ]
    return max(scores) if scores else 1.0
