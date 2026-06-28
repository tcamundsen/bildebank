from __future__ import annotations

import sqlite3
from pathlib import Path

from .db_core import target_relative_path, target_relative_path_key
from .value_parsing import optional_int


def set_manual_date(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    date_from: str,
    date_to: str,
    note: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE files
        SET manual_date_from = ?,
            manual_date_to = ?,
            manual_date_note = ?
        WHERE id = ?
          AND deleted_at IS NULL
        """,
        (date_from, date_to, clean_manual_date_note(note), file_id),
    )


def clear_manual_date(conn: sqlite3.Connection, *, file_id: int) -> None:
    conn.execute(
        """
        UPDATE files
        SET manual_date_from = NULL,
            manual_date_to = NULL,
            manual_date_note = NULL
        WHERE id = ?
          AND deleted_at IS NULL
        """,
        (file_id,),
    )


def clean_manual_date_note(note: str | None) -> str | None:
    if note is None:
        return None
    clean = " ".join(note.strip().split())
    return clean or None


def create_pending_file_move(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_root: Path,
    from_path: Path,
    to_path: Path,
    sha256: str,
    operation: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO pending_file_moves(
            file_id, from_path, to_path, sha256, operation, state
        )
        VALUES(?, ?, ?, ?, ?, 'prepared')
        """,
        (
            file_id,
            target_relative_path(target_root, from_path).as_posix(),
            target_relative_path(target_root, to_path).as_posix(),
            sha256,
            operation,
        ),
    )
    move_id = optional_int(cursor.lastrowid, "ventende filflytting-id")
    if move_id is None:
        raise ValueError("Databasen returnerte ikke id for den nye ventende filflyttingen.")
    return move_id


def prepared_pending_file_moves(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT id, file_id, from_path, to_path, sha256, operation, state,
                   created_at, updated_at, completed_at, last_error
            FROM pending_file_moves
            WHERE state = 'prepared'
              AND completed_at IS NULL
            ORDER BY id
            """
        )
    )


def complete_pending_file_move(conn: sqlite3.Connection, *, move_id: int) -> None:
    conn.execute(
        """
        UPDATE pending_file_moves
        SET state = 'completed',
            updated_at = CURRENT_TIMESTAMP,
            completed_at = CURRENT_TIMESTAMP,
            last_error = NULL
        WHERE id = ?
          AND state = 'prepared'
          AND completed_at IS NULL
        """,
        (move_id,),
    )


def abort_pending_file_move(conn: sqlite3.Connection, *, move_id: int) -> None:
    conn.execute(
        """
        UPDATE pending_file_moves
        SET state = 'aborted',
            updated_at = CURRENT_TIMESTAMP,
            completed_at = CURRENT_TIMESTAMP,
            last_error = NULL
        WHERE id = ?
          AND state = 'prepared'
          AND completed_at IS NULL
        """,
        (move_id,),
    )


def fail_pending_file_move(
    conn: sqlite3.Connection,
    *,
    move_id: int,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE pending_file_moves
        SET updated_at = CURRENT_TIMESTAMP,
            last_error = ?
        WHERE id = ?
          AND state = 'prepared'
          AND completed_at IS NULL
        """,
        (error, move_id),
    )


def mark_file_deleted(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_root: Path,
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
            target_relative_path(target_root, deleted_path).as_posix(),
            target_relative_path_key(target_root, deleted_path),
            target_relative_path(target_root, original_target_path).as_posix(),
            file_id,
        ),
    )


def mark_file_undeleted(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_root: Path,
    restored_path: Path,
) -> None:
    conn.execute(
        """
        UPDATE files
        SET target_path = ?,
            target_path_key = ?,
            deleted_at = NULL,
            deleted_original_target_path = NULL
        WHERE id = ?
          AND deleted_at IS NOT NULL
        """,
        (
            target_relative_path(target_root, restored_path).as_posix(),
            target_relative_path_key(target_root, restored_path),
            file_id,
        ),
    )


def update_file_placement(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_root: Path,
    target_path: Path,
    stored_filename: str,
    taken_date: str,
    date_source: str,
    name_conflict: bool,
    camera_make: str | None = None,
    camera_model: str | None = None,
    metadata_datetime: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE files
        SET target_path = ?,
            target_path_key = ?,
            stored_filename = ?,
            taken_date = ?,
            date_source = ?,
            name_conflict = ?,
            camera_make = ?,
            camera_model = ?,
            metadata_datetime = ?
        WHERE id = ?
        """,
        (
            target_relative_path(target_root, target_path).as_posix(),
            target_relative_path_key(target_root, target_path),
            stored_filename,
            taken_date,
            date_source,
            1 if name_conflict else 0,
            camera_make,
            camera_model,
            metadata_datetime,
            file_id,
        ),
    )
