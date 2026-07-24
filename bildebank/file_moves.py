from __future__ import annotations

import sqlite3
from pathlib import Path

from . import db
from .config import FaceRecognitionConfig
from .item_sidecars import (
    attach_existing_item_databases,
    delete_attached_item_data,
)
from .media import camera_info, media_date, metadata_datetime, sha256_file
from .target_lock import TargetLock


def recover_pending_file_moves(
    target: Path,
    *,
    face_config: FaceRecognitionConfig | None = None,
) -> int:
    with TargetLock(target, command="recover-file-moves"):
        return recover_pending_file_moves_locked(
            target,
            face_config=face_config,
        )


def recover_pending_file_moves_locked(
    target: Path,
    *,
    face_config: FaceRecognitionConfig | None = None,
) -> int:
    """Recover pending moves while the caller already holds the target lock."""
    conn = db.connect(target)
    try:
        return recover_pending_file_moves_in_connection(
            conn,
            target,
            face_config=face_config,
        )
    finally:
        conn.close()


def recover_pending_file_moves_in_connection(
    conn: sqlite3.Connection,
    target: Path,
    *,
    face_config: FaceRecognitionConfig | None = None,
) -> int:
    try:
        rows = db.prepared_pending_file_moves(conn)
        recovered = 0
        for row in rows:
            _recover_pending_file_move(
                conn,
                target,
                row,
                face_config=face_config,
            )
            conn.commit()
            recovered += 1
        return recovered
    except Exception:
        conn.rollback()
        raise


def _recover_pending_file_move(
    conn: sqlite3.Connection,
    target: Path,
    row: sqlite3.Row,
    *,
    face_config: FaceRecognitionConfig | None,
) -> None:
    move_id = int(row["id"])
    from_path = db.absolute_target_path(target, Path(str(row["from_path"])))
    to_path = db.absolute_target_path(target, Path(str(row["to_path"])))
    from_exists = from_path.exists()
    to_exists = to_path.exists()

    if from_exists and not to_exists:
        db.abort_pending_file_move(conn, move_id=move_id)
        return

    if to_exists and not from_exists:
        actual_hash = sha256_file(to_path)
        expected_hash = str(row["sha256"])
        if actual_hash != expected_hash:
            _fail(
                conn,
                move_id=move_id,
                message=(
                    f"Kan ikke recovere pending_file_moves #{move_id}: "
                    f"{to_path} har sha256={actual_hash}, forventet {expected_hash}."
                ),
            )
        _complete_database_move(
            conn,
            target,
            row,
            from_path=from_path,
            to_path=to_path,
            face_config=face_config,
        )
        db.complete_pending_file_move(conn, move_id=move_id)
        return

    if from_exists and to_exists:
        _fail(
            conn,
            move_id=move_id,
            message=(
                f"Kan ikke recovere pending_file_moves #{move_id}: "
                f"både kilde og mål finnes ({from_path} og {to_path})."
            ),
        )

    _fail(
        conn,
        move_id=move_id,
        message=(
            f"Kan ikke recovere pending_file_moves #{move_id}: "
            f"hverken kilde eller mål finnes ({from_path} og {to_path})."
        ),
    )


def _complete_database_move(
    conn: sqlite3.Connection,
    target: Path,
    row: sqlite3.Row,
    *,
    from_path: Path,
    to_path: Path,
    face_config: FaceRecognitionConfig | None,
) -> None:
    operation = str(row["operation"])
    file_id = int(row["file_id"])
    if operation == "remove":
        attach_existing_item_databases(conn, target, face_config)
        delete_attached_item_data(conn, (file_id,))
        db.mark_file_deleted(
            conn,
            file_id=file_id,
            target_root=target,
            deleted_path=to_path,
            original_target_path=from_path,
        )
        return
    if operation == "undelete":
        db.mark_file_undeleted(
            conn,
            file_id=file_id,
            target_root=target,
            restored_path=to_path,
        )
        return
    if operation == "refresh-metadata":
        date = media_date(to_path)
        if date.source != "metadata" or date.date is None:
            raise ValueError(
                f"Kan ikke fullføre refresh-metadata recovery for {to_path}: metadata-dato mangler."
            )
        camera = camera_info(to_path)
        metadata_dt = metadata_datetime(to_path)
        db.update_file_placement(
            conn,
            file_id=file_id,
            target_root=target,
            target_path=to_path,
            stored_filename=to_path.name,
            taken_date=date.date.isoformat(),
            date_source="metadata",
            name_conflict=to_path.name != from_path.name,
            camera_make=camera.make if camera is not None else None,
            camera_model=camera.model if camera is not None else None,
            metadata_datetime=metadata_dt.isoformat(sep=" ") if metadata_dt is not None else None,
        )
        db.resolve_errors_for_path(conn, stage="refresh-metadata", source_path=from_path)
        return
    raise ValueError(f"Ukjent pending_file_moves-operasjon: {operation}")


def _fail(conn: sqlite3.Connection, *, move_id: int, message: str) -> None:
    db.fail_pending_file_move(conn, move_id=move_id, error=message)
    conn.commit()
    raise ValueError(message)
