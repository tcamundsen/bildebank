from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import db
from .config import FaceRecognitionConfig
from .face import face_db_path, normalize_person_name
from .html_export import browser_face_items_from_metadata, face_tables_exist
from .media import ImageDimensions, image_dimensions, image_orientation
from .server_browser import items_by_file_ids, person_item_url, person_url


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


def person_item_url_for_face(
    target: Path,
    person_name: str,
    face_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    db_path = current_face_db_path(target, face_config)
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT file_id FROM faces WHERE id = ?", (face_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        row = None
    if row is None:
        return person_url(person_name, show_faces=False)
    return person_item_url(person_name, int(row[0]), show_faces=False)


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


def unconfirmed_faces_for_item(
    target: Path,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> list[dict[str, object]]:
    db_path = current_face_db_path(target, face_config)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return []
        rows = conn.execute(
            """
            SELECT
                faces.id,
                faces.bbox_x,
                faces.bbox_y,
                faces.bbox_width,
                faces.bbox_height,
                faces.detection_score
            FROM faces
            WHERE faces.file_id = ?
              AND NOT EXISTS (
                SELECT 1
                FROM person_faces
                WHERE person_faces.face_id = faces.id
              )
            ORDER BY faces.id
            """,
            (int(item["id"]),),
        )
        faces = [
            {
                "faceId": int(row["id"]),
                "score": float(row["detection_score"]),
                "x": float(row["bbox_x"]),
                "y": float(row["bbox_y"]),
                "width": float(row["bbox_width"]),
                "height": float(row["bbox_height"]),
            }
            for row in rows
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return cached_face_box_items_for_item(target, item, faces)


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


def person_faces_for_item(
    target: Path,
    person_name: str,
    item: Any,
    *,
    include_suggestions: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> list[dict[str, object]]:
    person = person_by_name(target, person_name, face_config)
    if person is None:
        return []
    conn = sqlite3.connect(current_face_db_path(target, face_config))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                'bekreftet' AS status,
                faces.id,
                1.0 AS similarity,
                faces.bbox_x,
                faces.bbox_y,
                faces.bbox_width,
                faces.bbox_height,
                faces.detection_score
            FROM person_faces
            JOIN faces ON faces.id = person_faces.face_id
            WHERE person_faces.person_id = ?
              AND faces.file_id = ?
            """ + (
                """
                UNION ALL
                SELECT
                    'forslag' AS status,
                    faces.id,
                    face_suggestions.similarity,
                    faces.bbox_x,
                    faces.bbox_y,
                    faces.bbox_width,
                    faces.bbox_height,
                    faces.detection_score
                FROM face_suggestions
                JOIN faces ON faces.id = face_suggestions.face_id
                WHERE face_suggestions.person_id = ?
                  AND faces.file_id = ?
                """
                if include_suggestions
                else ""
            ) + """
            ORDER BY status, id
            """,
            (
                (int(person["id"]), int(item["id"]), int(person["id"]), int(item["id"]))
                if include_suggestions
                else (int(person["id"]), int(item["id"]))
            ),
        )
        faces = [
            {
                "faceId": int(row["id"]),
                "status": str(row["status"]),
                "similarity": float(row["similarity"]),
                "score": float(row["detection_score"]),
                "x": float(row["bbox_x"]),
                "y": float(row["bbox_y"]),
                "width": float(row["bbox_width"]),
                "height": float(row["bbox_height"]),
            }
            for row in rows
        ]
    finally:
        conn.close()
    face_meta = {int(face["faceId"]): face for face in faces}
    rendered = cached_face_box_items_for_item(target, item, faces)
    for face in rendered:
        meta = face_meta.get(int(face["faceId"]))
        if meta is not None:
            face["status"] = meta["status"]
            face["similarity"] = meta["similarity"]
    return rendered


def cached_face_box_items_for_item(target: Path, item: Any, faces: list[dict[str, object]]) -> list[dict[str, object]]:
    dimensions, orientation = cached_face_box_media_metadata(target, item)
    return browser_face_items_from_metadata(faces, dimensions, orientation)


def cached_face_box_media_metadata(target: Path, item: Any) -> tuple[ImageDimensions | None, int]:
    target_path = db.absolute_target_path(target, Path(str(item["target_path"])))
    mtime_ns = file_mtime_ns(target_path)
    cached_dimensions, cached_orientation = face_box_media_metadata_from_item(item, mtime_ns)
    if cached_orientation is not None:
        return cached_dimensions, cached_orientation

    dimensions = image_dimensions(target_path)
    orientation = image_orientation(target_path)
    update_face_box_media_metadata(target, int(item["id"]), dimensions, orientation, mtime_ns)
    return dimensions, orientation


def face_box_media_metadata_from_item(item: Any, mtime_ns: int | None) -> tuple[ImageDimensions | None, int | None]:
    cached_mtime = item_field(item, "media_metadata_mtime_ns")
    if cached_mtime is None or mtime_ns is None or int(cached_mtime) != mtime_ns:
        return None, None
    orientation = item_field(item, "media_orientation")
    if orientation is None:
        return None, None
    width = item_field(item, "media_width")
    height = item_field(item, "media_height")
    dimensions = None
    if width is not None and height is not None and int(width) > 0 and int(height) > 0:
        dimensions = ImageDimensions(int(width), int(height))
    return dimensions, int(orientation)


def item_field(item: Any, key: str) -> Any | None:
    try:
        return item[key]
    except (KeyError, IndexError):
        return None


def file_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def update_face_box_media_metadata(
    target: Path,
    file_id: int,
    dimensions: ImageDimensions | None,
    orientation: int,
    mtime_ns: int | None,
) -> None:
    conn = db.connect(target)
    try:
        conn.execute(
            """
            UPDATE files
            SET media_width = ?,
                media_height = ?,
                media_orientation = ?,
                media_metadata_mtime_ns = ?
            WHERE id = ?
            """,
            (
                dimensions.width if dimensions is not None else None,
                dimensions.height if dimensions is not None else None,
                orientation,
                mtime_ns,
                file_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
