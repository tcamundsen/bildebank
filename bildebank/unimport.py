from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import AppConfig
from .face import ensure_face_schema_path, face_database_dir
from .media import sha256_file
from .openclip import openclip_db_path
from .pending_deletes import (
    PendingDeleteResult,
    cleanup_pending_deletes,
    enqueue_pending_delete_in_transaction,
)
from .target_lock import TargetLock


Confirmation = Callable[[db.UnimportPlan], bool]
TargetContentConfirmation = Callable[[tuple["TargetContentChange", ...]], bool]


@dataclass(frozen=True)
class TargetContentChange:
    path: Path
    expected_size_bytes: int
    actual_size_bytes: int
    expected_sha256: str
    actual_sha256: str


@dataclass(frozen=True)
class UnimportResult:
    plan: db.UnimportPlan
    applied: bool
    target_content_changes: tuple[TargetContentChange, ...] = ()
    cleanup_results: tuple[PendingDeleteResult, ...] = ()


def run_unimport(
    target: Path,
    source_name: str,
    *,
    config: AppConfig,
    dry_run: bool,
    confirm: Confirmation,
    confirm_target_content_changes: TargetContentConfirmation | None = None,
    source_progress: Any | None = None,
    target_progress: Any | None = None,
) -> UnimportResult:
    pending_ids: tuple[int, ...] = ()
    with TargetLock(target, command="unimport"):
        conn = db.connect(target)
        try:
            source = db.find_source_by_name(conn, source_name)
            if source is None:
                raise ValueError(f"Fant ikke kilde med navn: {source_name}")
            plan = db.build_unimport_plan(conn, target, source)
            validate_source_files(
                conn,
                source,
                progress=source_progress,
            )
            validate_target_paths(target, plan, progress=target_progress)
            target_content_changes = find_target_content_changes(conn, target, plan)
            if dry_run or not confirm(plan):
                conn.rollback()
                return UnimportResult(
                    plan=plan,
                    applied=False,
                    target_content_changes=target_content_changes,
                )
            if (
                target_content_changes
                and confirm_target_content_changes is not None
                and not confirm_target_content_changes(target_content_changes)
            ):
                conn.rollback()
                return UnimportResult(
                    plan=plan,
                    applied=False,
                    target_content_changes=target_content_changes,
                )

            attach_item_databases(conn, target, config)
            conn.execute("BEGIN IMMEDIATE")
            try:
                pending_ids = apply_unimport_transaction(conn, target, plan)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    cleanup_results = tuple(
        cleanup_pending_deletes(target, pending_ids=pending_ids)
    )
    return UnimportResult(
        plan=plan,
        applied=True,
        target_content_changes=target_content_changes,
        cleanup_results=cleanup_results,
    )


def validate_source_files(
    conn: sqlite3.Connection,
    source: db.Source,
    *,
    progress: Any | None = None,
) -> None:
    rows = db.source_file_sources(conn, source.id)
    total = len(rows)
    if progress is not None:
        progress.message(f"Unimport: kontrollerer {total} filer i kilden.")
        if total == 0:
            progress.update(0, 0, action="filer i kilden", eta=True)
    try:
        for index, row in enumerate(rows, start=1):
            source_path = Path(str(row["source_path"]))
            if not source_path.exists():
                raise ValueError(
                    f"Originalfil mangler: {source_path}\n"
                    "Sjekk at riktig mappe, USB-disk, CD eller minnekort er tilgjengelig, "
                    "og at det har samme stasjon/path som da importen ble kjørt."
                )
            if not source_path.is_file():
                raise ValueError(f"Originalfilen er ikke en vanlig fil: {source_path}")
            size_bytes = source_path.stat().st_size
            if size_bytes != int(row["size_bytes"]):
                raise ValueError(
                    f"Originalfilen har endret størrelse: {source_path} "
                    f"(nå {size_bytes}, forventet {row['size_bytes']})"
                )
            if sha256_file(source_path) != row["sha256"]:
                raise ValueError(f"Originalfilen har endret innhold: {source_path}")
            if progress is not None:
                progress.update(index, total, action="filer i kilden", eta=True)
    finally:
        if progress is not None:
            progress.done()


def validate_target_paths(
    target: Path,
    plan: db.UnimportPlan,
    *,
    progress: Any | None = None,
) -> None:
    total = len(plan.target_paths_to_delete)
    if progress is not None:
        progress.message(f"Unimport: kontrollerer {total} fil(er) som kan fjernes.")
        if total == 0:
            progress.update(0, 0, action="filer", eta=True)
    try:
        for index, target_path in enumerate(plan.target_paths_to_delete, start=1):
            db.target_relative_path(target, target_path)
            if target_path.exists() and not target_path.is_file():
                raise ValueError(f"Filen som skulle fjernes er ikke en vanlig fil: {target_path}")
            if progress is not None:
                progress.update(index, total, action="filer", eta=True)
    finally:
        if progress is not None:
            progress.done()


def find_target_content_changes(
    conn: sqlite3.Connection,
    target: Path,
    plan: db.UnimportPlan,
) -> tuple[TargetContentChange, ...]:
    if not plan.file_ids_to_delete:
        return ()
    placeholders = ",".join("?" for _ in plan.file_ids_to_delete)
    rows = {
        int(row["id"]): row
        for row in conn.execute(
            f"""
            SELECT id, target_path, sha256, size_bytes
            FROM files
            WHERE id IN ({placeholders})
            """,
            plan.file_ids_to_delete,
        )
    }
    changes: list[TargetContentChange] = []
    for file_id in plan.file_ids_to_delete:
        row = rows.get(file_id)
        if row is None:
            continue
        target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
        relative_path = db.target_relative_path(target, target_path)
        if not target_path.exists():
            continue
        if not target_path.is_file():
            raise ValueError(f"Filen som skulle fjernes er ikke en vanlig fil: {target_path}")
        expected_size = int(row["size_bytes"])
        expected_sha256 = str(row["sha256"])
        actual_size = target_path.stat().st_size
        actual_sha256 = sha256_file(target_path)
        if actual_size != expected_size or actual_sha256 != expected_sha256:
            changes.append(
                TargetContentChange(
                    path=relative_path,
                    expected_size_bytes=expected_size,
                    actual_size_bytes=actual_size,
                    expected_sha256=expected_sha256,
                    actual_sha256=actual_sha256,
                )
            )
    return tuple(changes)


def apply_unimport_transaction(
    conn: sqlite3.Connection,
    target: Path,
    plan: db.UnimportPlan,
) -> tuple[int, ...]:
    db.log_command(
        conn,
        "unimport",
        {"name": plan.source.name, "source_id": plan.source.id},
    )
    pending_ids = tuple(
        enqueue_pending_delete_in_transaction(
            conn,
            target,
            path,
            reason="unimport",
            source_id=plan.source.id,
        ).id
        for path in plan.target_paths_to_delete
    )
    delete_attached_item_data(conn, plan.file_ids_to_delete)
    db.apply_unimport(conn, plan)
    return pending_ids


def attach_item_databases(
    conn: sqlite3.Connection,
    target: Path,
    config: AppConfig,
) -> None:
    face_dir = face_database_dir(target, config.face_recognition)
    if face_dir.is_dir():
        for index, path in enumerate(sorted(face_dir.glob("*.sqlite3"))):
            ensure_face_schema_path(path)
            conn.execute(
                f"ATTACH DATABASE ? AS face_db_{index}",
                (str(path),),
            )
    openclip_path = openclip_db_path(target)
    if openclip_path.is_file():
        conn.execute("ATTACH DATABASE ? AS openclip_db", (str(openclip_path),))


def delete_attached_item_data(
    conn: sqlite3.Connection,
    file_ids: tuple[int, ...],
) -> None:
    if not file_ids:
        return
    placeholders = ",".join("?" for _ in file_ids)
    databases = [
        str(row["name"])
        for row in conn.execute("PRAGMA database_list")
    ]
    for database in databases:
        if database.startswith("face_db_"):
            face_ids_sql = (
                f"SELECT id FROM {database}.faces "
                f"WHERE file_id IN ({placeholders})"
            )
            conn.execute(
                f"DELETE FROM {database}.face_suggestions "
                f"WHERE face_id IN ({face_ids_sql}) "
                f"OR reference_face_id IN ({face_ids_sql})",
                (*file_ids, *file_ids),
            )
            conn.execute(
                f"DELETE FROM {database}.person_faces "
                f"WHERE face_id IN ({face_ids_sql})",
                file_ids,
            )
            conn.execute(
                f"DELETE FROM {database}.person_files "
                f"WHERE file_id IN ({placeholders})",
                file_ids,
            )
            conn.execute(
                f"DELETE FROM {database}.faces "
                f"WHERE file_id IN ({placeholders})",
                file_ids,
            )
            conn.execute(
                f"DELETE FROM {database}.scanned_files "
                f"WHERE file_id IN ({placeholders})",
                file_ids,
            )
        elif database == "openclip_db":
            conn.execute(
                f"DELETE FROM openclip_db.image_search_results "
                f"WHERE file_id IN ({placeholders})",
                file_ids,
            )
            conn.execute(
                f"DELETE FROM openclip_db.image_embeddings "
                f"WHERE file_id IN ({placeholders})",
                file_ids,
            )
