from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db
from .target_lock import TargetLock


@dataclass(frozen=True)
class PendingFileDelete:
    id: int
    path: Path
    reason: str
    source_id: int | None
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PendingDeleteResult:
    pending_id: int
    path: Path
    outcome: str
    error: str | None = None


def enqueue_pending_delete(
    target: Path,
    path: Path,
    *,
    reason: str,
    source_id: int | None = None,
) -> PendingFileDelete:
    relative_path, _ = managed_pending_delete_path(target, path)
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("Pending-delete krever en årsak.")
    with TargetLock(target, command="pending-delete-enqueue"):
        conn = db.connect(target)
        try:
            conn.execute(
                """
                INSERT INTO pending_file_deletes(path, reason, source_id)
                VALUES(?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    reason = excluded.reason,
                    source_id = COALESCE(excluded.source_id, pending_file_deletes.source_id),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (relative_path.as_posix(), clean_reason, source_id),
            )
            row = conn.execute(
                "SELECT * FROM pending_file_deletes WHERE path = ?",
                (relative_path.as_posix(),),
            ).fetchone()
            conn.commit()
        finally:
            conn.close()
    if row is None:
        raise ValueError("Klarte ikke å legge filen i pending-delete-køen.")
    return pending_delete_from_row(row)


def list_pending_deletes(target: Path) -> list[PendingFileDelete]:
    conn = db.connect(target)
    try:
        return [
            pending_delete_from_row(row)
            for row in conn.execute(
                "SELECT * FROM pending_file_deletes ORDER BY id"
            )
        ]
    finally:
        conn.close()


def try_pending_delete(target: Path, pending_id: int) -> PendingDeleteResult | None:
    with TargetLock(target, command="cleanup-pending-deletes"):
        conn = db.connect(target)
        try:
            result = _try_pending_delete(conn, target, pending_id)
            conn.commit()
            return result
        finally:
            conn.close()


def cleanup_pending_deletes(
    target: Path,
    *,
    limit: int | None = None,
) -> list[PendingDeleteResult]:
    if limit is not None and limit <= 0:
        raise ValueError("limit må være større enn 0.")
    with TargetLock(target, command="cleanup-pending-deletes"):
        conn = db.connect(target)
        try:
            sql = "SELECT id FROM pending_file_deletes ORDER BY id"
            params: tuple[int, ...] = ()
            if limit is not None:
                sql += " LIMIT ?"
                params = (limit,)
            pending_ids = [int(row["id"]) for row in conn.execute(sql, params)]
            results: list[PendingDeleteResult] = []
            for pending_id in pending_ids:
                result = _try_pending_delete(conn, target, pending_id)
                if result is not None:
                    results.append(result)
                conn.commit()
            return results
        finally:
            conn.close()


def managed_pending_delete_path(target: Path, path: Path) -> tuple[Path, Path]:
    target_root = target.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = target_root / candidate
    unresolved = candidate.absolute()
    try:
        lexical_relative = unresolved.relative_to(target_root)
    except ValueError as exc:
        raise ValueError(f"Filen ligger utenfor bildesamlingen: {path}") from exc
    if (
        not lexical_relative.parts
        or lexical_relative == Path(".")
        or ".." in lexical_relative.parts
    ):
        raise ValueError(f"Ugyldig pending-delete-sti: {path}")
    first = lexical_relative.parts[0]
    if not ((len(first) == 4 and first.isdigit()) or first == "udatert"):
        raise ValueError(
            "Pending-delete kan bare brukes for ordinære mediefiler under "
            "årsmappene eller udatert/."
        )
    for parent in [unresolved, *unresolved.parents]:
        if parent == target_root.parent:
            break
        if parent.is_symlink():
            raise ValueError(f"Pending-delete-stien kan ikke inneholde symlinker: {path}")
        if parent == target_root:
            break
    resolved = unresolved.resolve(strict=False)
    try:
        relative_path = resolved.relative_to(target_root)
    except ValueError as exc:
        raise ValueError(f"Filen ligger utenfor bildesamlingen: {path}") from exc
    return relative_path, unresolved


def _try_pending_delete(
    conn: sqlite3.Connection,
    target: Path,
    pending_id: int,
) -> PendingDeleteResult | None:
    row = conn.execute(
        "SELECT * FROM pending_file_deletes WHERE id = ?",
        (pending_id,),
    ).fetchone()
    if row is None:
        return None
    relative_path = Path(str(row["path"]))
    try:
        normalized_path, absolute_path = managed_pending_delete_path(target, relative_path)
        if normalized_path.as_posix() != relative_path.as_posix():
            raise ValueError("Pending-delete-stien er ikke normalisert.")
        reference = conn.execute(
            "SELECT id FROM files WHERE target_path_key = ? LIMIT 1",
            (db.relative_path_key(normalized_path),),
        ).fetchone()
        if reference is not None:
            raise ValueError(
                f"Filen har fortsatt database-referanse i files #{int(reference['id'])}."
            )
        if not absolute_path.exists():
            conn.execute("DELETE FROM pending_file_deletes WHERE id = ?", (pending_id,))
            return PendingDeleteResult(pending_id, normalized_path, "missing")
        if not absolute_path.is_file():
            raise ValueError("Pending-delete-stien er ikke en vanlig fil.")
        absolute_path.unlink()
        conn.execute("DELETE FROM pending_file_deletes WHERE id = ?", (pending_id,))
        return PendingDeleteResult(pending_id, normalized_path, "deleted")
    except (OSError, ValueError) as exc:
        error = str(exc)
        conn.execute(
            """
            UPDATE pending_file_deletes
            SET attempts = attempts + 1,
                last_error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error, pending_id),
        )
        return PendingDeleteResult(pending_id, relative_path, "failed", error)


def pending_delete_from_row(row: sqlite3.Row) -> PendingFileDelete:
    source_id = int(row["source_id"]) if row["source_id"] is not None else None
    return PendingFileDelete(
        id=int(row["id"]),
        path=Path(str(row["path"])),
        reason=str(row["reason"]),
        source_id=source_id,
        attempts=int(row["attempts"]),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
