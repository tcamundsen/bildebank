from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import db
from .media import sha256_file
from .target_lock import TargetLock


def remove_file(
    target: Path,
    *,
    file_id: int | None = None,
    collection_path: Path | None = None,
    command_args: dict[str, Any] | None = None,
    path_adapter: Callable[[Path], Path] | None = None,
) -> Path:
    _require_one_identifier(file_id=file_id, collection_path=collection_path)
    with TargetLock(target, command="remove"):
        conn = db.connect(target)
        try:
            if command_args is not None:
                db.log_command(conn, "remove", command_args)

            if collection_path is not None:
                if path_adapter is not None:
                    collection_path = path_adapter(collection_path)
                original_path = _resolve_collection_path(target, collection_path)
                relative_path = _active_relative_path(target, original_path)
                row = db.file_by_target_path(conn, target, original_path)
                if row is None:
                    raise ValueError(f"Filen finnes ikke i importdatabasen: {original_path}")
                if row["deleted_at"] is not None:
                    raise ValueError(f"Filen er allerede markert som slettet: {original_path}")
            else:
                row = conn.execute(
                    """
                    SELECT id, target_path, deleted_at, sha256
                    FROM files
                    WHERE id = ?
                    """,
                    (file_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("Filen finnes ikke i importdatabasen.")
                if row["deleted_at"] is not None:
                    raise ValueError("Filen er allerede markert som slettet.")
                original_path = db.absolute_target_path(
                    target, Path(str(row["target_path"]))
                ).resolve()
                relative_path = _active_relative_path(target, original_path)

            if not original_path.exists():
                raise ValueError(f"Filen finnes ikke på disk: {original_path}")
            _verify_expected_sha256(original_path, str(row["sha256"]))

            deleted_path = target / "deleted" / relative_path
            if deleted_path.exists():
                raise ValueError(f"Slettemål finnes allerede: {deleted_path}")

            deleted_path.parent.mkdir(parents=True, exist_ok=True)
            move_id = db.create_pending_file_move(
                conn,
                file_id=int(row["id"]),
                target_root=target,
                from_path=original_path,
                to_path=deleted_path,
                sha256=str(row["sha256"]),
                operation="remove",
            )
            conn.commit()
            shutil.move(str(original_path), str(deleted_path))
            _verify_expected_sha256(deleted_path, str(row["sha256"]))
            db.mark_file_deleted(
                conn,
                file_id=int(row["id"]),
                target_root=target,
                deleted_path=deleted_path,
                original_target_path=original_path,
            )
            db.complete_pending_file_move(conn, move_id=move_id)
            conn.commit()
            return db.target_relative_path(target, deleted_path)
        finally:
            conn.close()


def undelete_file(
    target: Path,
    *,
    file_id: int | None = None,
    collection_path: Path | None = None,
    command_args: dict[str, Any] | None = None,
    path_adapter: Callable[[Path], Path] | None = None,
) -> Path:
    _require_one_identifier(file_id=file_id, collection_path=collection_path)
    with TargetLock(target, command="undelete"):
        conn = db.connect(target)
        try:
            if command_args is not None:
                db.log_command(conn, "undelete", command_args)

            if collection_path is not None:
                if path_adapter is not None:
                    collection_path = path_adapter(collection_path)
                deleted_path = _resolve_collection_path(target, collection_path)
                _deleted_relative_path(target, deleted_path, collection_path)
                row = db.file_by_target_path(conn, target, deleted_path)
                if row is None:
                    raise ValueError(f"Filen finnes ikke i importdatabasen: {deleted_path}")
                if row["deleted_at"] is None:
                    raise ValueError(f"Filen er ikke markert som slettet: {deleted_path}")
                if row["deleted_original_target_path"] is None:
                    raise ValueError(
                        f"Filen mangler opprinnelig plassering i databasen: {deleted_path}"
                    )
            else:
                row = conn.execute(
                    """
                    SELECT id, target_path, deleted_at, deleted_original_target_path, sha256
                    FROM files
                    WHERE id = ?
                    """,
                    (file_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("Filen finnes ikke i importdatabasen.")
                if row["deleted_at"] is None:
                    raise ValueError("Filen er ikke markert som slettet.")
                if row["deleted_original_target_path"] is None:
                    raise ValueError("Filen mangler opprinnelig plassering i databasen.")
                deleted_path = db.absolute_target_path(
                    target, Path(str(row["target_path"]))
                ).resolve()
                _deleted_relative_path(
                    target,
                    deleted_path,
                    deleted_path,
                    invalid_message=f"Slettet fil ligger ikke under deleted/: {deleted_path}",
                )

            if not deleted_path.exists():
                raise ValueError(f"Slettet fil finnes ikke på disk: {deleted_path}")
            _verify_expected_sha256(deleted_path, str(row["sha256"]))

            restored_path = target / Path(str(row["deleted_original_target_path"]))
            if restored_path.exists():
                raise ValueError(f"En fil finnes allerede på opprinnelig plassering: {restored_path}")

            restored_path.parent.mkdir(parents=True, exist_ok=True)
            move_id = db.create_pending_file_move(
                conn,
                file_id=int(row["id"]),
                target_root=target,
                from_path=deleted_path,
                to_path=restored_path,
                sha256=str(row["sha256"]),
                operation="undelete",
            )
            conn.commit()
            shutil.move(str(deleted_path), str(restored_path))
            _verify_expected_sha256(restored_path, str(row["sha256"]))
            db.mark_file_undeleted(
                conn,
                file_id=int(row["id"]),
                target_root=target,
                restored_path=restored_path,
            )
            db.complete_pending_file_move(conn, move_id=move_id)
            conn.commit()
            return db.target_relative_path(target, restored_path)
        finally:
            conn.close()


def _require_one_identifier(*, file_id: int | None, collection_path: Path | None) -> None:
    if (file_id is None) == (collection_path is None):
        raise ValueError("Oppgi nøyaktig én av file_id og collection_path.")


def _resolve_collection_path(target: Path, path: Path) -> Path:
    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (target / path).resolve()
    _relative_collection_path(target, resolved)
    return resolved


def _relative_collection_path(target: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(target.resolve())
    except ValueError as exc:
        raise ValueError(f"Filen ligger ikke i bildesamlingen: {path}") from exc


def _active_relative_path(target: Path, path: Path) -> Path:
    relative_path = _relative_collection_path(target, path)
    if not relative_path.parts or relative_path.parts[0] == "deleted":
        raise ValueError(f"Kan ikke slette filer fra deleted/: {path}")
    return relative_path


def _deleted_relative_path(
    target: Path,
    path: Path,
    display_path: Path,
    *,
    invalid_message: str | None = None,
) -> Path:
    relative_path = _relative_collection_path(target, path)
    if len(relative_path.parts) < 2 or relative_path.parts[0] != "deleted":
        raise ValueError(
            invalid_message or f"Undelete krever sti under deleted/: {display_path}"
        )
    return relative_path


def _verify_expected_sha256(path: Path, expected_sha256: str) -> None:
    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"Fila på disk har feil SHA-256: {path} "
            f"(forventet {expected_sha256}, fant {actual_sha256})"
        )
