from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import db
from .config import FaceRecognitionConfig
from .face import face_db_path, normalize_person_name
from .html_export import face_tables_exist
from .server_browser import items_by_file_ids, person_item_url


def current_face_db_path(target: Path, face_config: FaceRecognitionConfig | None = None) -> Path:
    if face_config is None:
        face_config = FaceRecognitionConfig()
    return face_db_path(target, face_config)


def confirmed_people_for_file(
    target: Path,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> list[dict[str, object]]:
    db_path = current_face_db_path(target, face_config)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        return []
    return [
        {"name": name, "url": person_item_url(name, file_id, show_faces=False), "confirmed": priority == 0}
        for name, priority in cached_confirmed_people_for_file(str(db_path), mtime_ns, file_id)
    ]


@lru_cache(maxsize=512)
def cached_confirmed_people_for_file(face_db_path: str, face_db_mtime_ns: int, file_id: int) -> tuple[tuple[str, int], ...]:
    conn = sqlite3.connect(face_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return ()
        rows = conn.execute(
            """
            SELECT persons.name, 0 AS priority
            FROM person_faces
            JOIN persons ON persons.id = person_faces.person_id
            JOIN faces ON faces.id = person_faces.face_id
            WHERE faces.file_id = ?
            UNION ALL
            SELECT persons.name, 1 AS priority
            FROM face_suggestions
            JOIN persons ON persons.id = face_suggestions.person_id
            JOIN faces ON faces.id = face_suggestions.face_id
            WHERE faces.file_id = ?
            ORDER BY name, priority
            """,
            (file_id, file_id),
        )
        people: dict[str, int] = {}
        for row in rows:
            name = str(row["name"])
            priority = int(row["priority"])
            if name not in people or priority < people[name]:
                people[name] = priority
        return tuple(sorted(people.items()))
    except sqlite3.Error:
        return ()
    finally:
        conn.close()


def active_file_id_set(target: Path, file_ids: list[int]) -> set[int]:
    unique_ids = sorted(set(file_ids))
    if not unique_ids:
        return set()
    placeholders = ",".join("?" for _ in unique_ids)
    conn = db.connect(target)
    try:
        rows = conn.execute(
            f"""
            SELECT id
            FROM files
            WHERE deleted_at IS NULL
              AND id IN ({placeholders})
            """,
            tuple(unique_ids),
        )
        return {int(row["id"]) for row in rows}
    finally:
        conn.close()


def unconfirmed_face_count_for_item(
    target: Path,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> int:
    db_path = current_face_db_path(target, face_config)
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return 0
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM faces
            WHERE faces.file_id = ?
              AND NOT EXISTS (
                SELECT 1
                FROM person_faces
                WHERE person_faces.face_id = faces.id
              )
            """,
            (file_id,),
        ).fetchone()
        return int(row[0] or 0) if row is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def person_by_name(
    target: Path,
    person_name: str,
    face_config: FaceRecognitionConfig | None = None,
) -> sqlite3.Row | None:
    db_path = current_face_db_path(target, face_config)
    if not db_path.exists():
        return None
    clean_name = normalize_person_name(person_name)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return None
        return conn.execute("SELECT id, name FROM persons WHERE name = ?", (clean_name,)).fetchone()
    finally:
        conn.close()


def person_file_ids(
    target: Path,
    person_name: str,
    *,
    include_suggestions: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> list[int]:
    db_path = current_face_db_path(target, face_config)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        return []
    clean_name = normalize_person_name(person_name)
    file_ids = list(cached_person_file_ids(str(db_path), mtime_ns, clean_name, include_suggestions))
    active_file_ids = active_file_id_set(target, file_ids)
    return [file_id for file_id in file_ids if file_id in active_file_ids]


@lru_cache(maxsize=256)
def cached_person_file_ids(
    face_db_path: str,
    face_db_mtime_ns: int,
    person_name: str,
    include_suggestions: bool,
) -> tuple[int, ...]:
    conn = sqlite3.connect(face_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return ()
        person = conn.execute("SELECT id FROM persons WHERE name = ?", (person_name,)).fetchone()
        if person is None:
            return ()
        if include_suggestions:
            rows = conn.execute(
                """
                SELECT DISTINCT faces.file_id
                FROM person_faces
                JOIN faces ON faces.id = person_faces.face_id
                WHERE person_faces.person_id = ?
                UNION
                SELECT DISTINCT faces.file_id
                FROM face_suggestions
                JOIN faces ON faces.id = face_suggestions.face_id
                WHERE face_suggestions.person_id = ?
                ORDER BY file_id
                """,
                (int(person["id"]), int(person["id"])),
            )
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT faces.file_id
                FROM person_faces
                JOIN faces ON faces.id = person_faces.face_id
                WHERE person_faces.person_id = ?
                ORDER BY faces.file_id
                """,
                (int(person["id"]),),
            )
        return tuple(int(row["file_id"]) for row in rows)
    finally:
        conn.close()


def person_items(
    target: Path,
    person_name: str,
    *,
    include_suggestions: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> list[Any]:
    file_ids = person_file_ids(
        target,
        person_name,
        include_suggestions=include_suggestions,
        face_config=face_config,
    )
    return items_by_file_ids(target, file_ids)
