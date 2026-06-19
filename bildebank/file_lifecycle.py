from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import db
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
                    SELECT id, target_path, deleted_at
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
                raise ValueError(f"Målfilen finnes ikke på disk: {original_path}")

            deleted_path = target / "deleted" / relative_path
            if deleted_path.exists():
                raise ValueError(f"Slettemål finnes allerede: {deleted_path}")

            deleted_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(original_path), str(deleted_path))
            db.mark_file_deleted(
                conn,
                file_id=int(row["id"]),
                target_root=target,
                deleted_path=deleted_path,
                original_target_path=original_path,
            )
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
                        f"Filen mangler opprinnelig målsti i databasen: {deleted_path}"
                    )
            else:
                row = conn.execute(
                    """
                    SELECT id, target_path, deleted_at, deleted_original_target_path
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
                    raise ValueError("Filen mangler opprinnelig målsti i databasen.")
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

            restored_path = target / Path(str(row["deleted_original_target_path"]))
            if restored_path.exists():
                raise ValueError(f"Målfilen finnes allerede: {restored_path}")

            restored_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(deleted_path), str(restored_path))
            db.mark_file_undeleted(
                conn,
                file_id=int(row["id"]),
                target_root=target,
                restored_path=restored_path,
            )
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
