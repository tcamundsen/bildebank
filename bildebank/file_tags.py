from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db
from .target_lock import TargetLock


@dataclass(frozen=True)
class SetFileTagResult:
    changed: bool
    tag_name: str
    target_path: Path


def set_file_tag(
    target: Path,
    *,
    tag_name: str,
    tagged: bool,
    file_id: int | None = None,
    collection_path: Path | None = None,
    command_args: dict[str, Any] | None = None,
    path_adapter: Callable[[Path], Path] | None = None,
) -> SetFileTagResult:
    if (file_id is None) == (collection_path is None):
        raise ValueError("Oppgi enten file_id eller filsti.")
    command = "tag-add" if tagged else "tag-remove"
    with TargetLock(target, command=command):
        conn = db.connect(target)
        try:
            if collection_path is not None:
                if path_adapter is not None:
                    collection_path = path_adapter(collection_path)
                resolved_path = _resolve_active_collection_path(target, collection_path)
                row = db.file_by_target_path(conn, target, resolved_path)
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
                raise ValueError("Filen er markert som slettet.")
            changed = db.set_file_tag(
                conn,
                file_id=int(row["id"]),
                tag_name=tag_name,
                tagged=tagged,
            )
            clean_name = db.normalize_tag_name(tag_name)
            target_path = Path(str(row["target_path"]))
            if command_args is not None:
                db.log_command(conn, command, command_args)
            conn.commit()
            return SetFileTagResult(changed=changed, tag_name=clean_name, target_path=target_path)
        finally:
            conn.close()


def _resolve_active_collection_path(target: Path, collection_path: Path) -> Path:
    resolved_path = collection_path.resolve() if collection_path.is_absolute() else (target / collection_path).resolve()
    try:
        relative_path = resolved_path.relative_to(target.resolve())
    except ValueError as exc:
        raise ValueError(f"Filen ligger ikke i bildesamlingen: {collection_path}") from exc
    if not relative_path.parts or relative_path.parts[0] == "deleted":
        raise ValueError(f"Kan ikke endre tagger for filer i deleted/: {collection_path}")
    return resolved_path
