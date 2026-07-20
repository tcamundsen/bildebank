from __future__ import annotations

import sqlite3
from typing import Iterable

from .value_parsing import optional_int

TAG_KIND_USER = "user"
TAG_KIND_SYSTEM = "system"
SYSTEM_TAG_OUT_OF_FOCUS = "Ute av fokus"
SYSTEM_TAG_NAMES = (SYSTEM_TAG_OUT_OF_FOCUS,)


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
            conn.execute(
                "UPDATE tags SET name = ?, kind = ? WHERE id = ?",
                (clean_name, TAG_KIND_SYSTEM, int(row["id"])),
            )
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
    from .db_schema import BROWSER_DATE_ORDER_SQL, H3_FILE_COLUMNS_SQL

    return list(
        conn.execute(
            f"""
            SELECT files.id, files.target_path, files.target_path_key, files.stored_filename,
                   files.taken_date, files.date_source, files.size_bytes, files.view_rotation_degrees,
                   files.comment,
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
