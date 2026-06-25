from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import AppConfig
from .face import apply_face_schema, face_database_dir
from .media import sha256_file
from .openclip import openclip_db_path
from .pending_deletes import (
    PendingDeleteResult,
    cleanup_pending_deletes,
    enqueue_pending_delete_in_transaction,
)
from .target_lock import TargetLock


Confirmation = Callable[[db.UnimportPlan], bool]


@dataclass(frozen=True)
class UnimportResult:
    plan: db.UnimportPlan
    applied: bool
    cleanup_results: tuple[PendingDeleteResult, ...] = ()


def run_unimport(
    target: Path,
    source_name: str,
    *,
    config: AppConfig,
    dry_run: bool,
    confirm: Confirmation,
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
            if source.status == "superseded":
                raise ValueError(
                    "Kan ikke unimportere en superseded kilde. "
                    f"Kilden er dekket av en annen import: {source.path}"
                )
            plan = db.build_unimport_plan(conn, target, source)
            validate_source_files(
                conn,
                source,
                progress=source_progress,
            )
            validate_target_paths(target, plan, progress=target_progress)
            if dry_run or not confirm(plan):
                conn.rollback()
                return UnimportResult(plan=plan, applied=False)

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
        progress.message(f"Unimport: kontrollerer {total} kildefiler.")
        if total == 0:
            progress.update(0, 0, action="kildefiler", eta=True)
    try:
        for index, row in enumerate(rows, start=1):
            source_path = Path(str(row["source_path"]))
            if not source_path.exists():
                raise ValueError(
                    f"Kildefil mangler: {source_path}\n"
                    "Sjekk at riktig mappe, USB-disk, CD eller minnekort er tilgjengelig, "
                    "og at det har samme stasjon/path som da importen ble kjørt."
                )
            if not source_path.is_file():
                raise ValueError(f"Kildefil er ikke en fil: {source_path}")
            size_bytes = source_path.stat().st_size
            if size_bytes != int(row["size_bytes"]):
                raise ValueError(
                    f"Kildefil har endret størrelse: {source_path} "
                    f"(nå {size_bytes}, forventet {row['size_bytes']})"
                )
            if sha256_file(source_path) != row["sha256"]:
                raise ValueError(f"Kildefil har endret innhold: {source_path}")
            if progress is not None:
                progress.update(index, total, action="kildefiler", eta=True)
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
        progress.message(f"Unimport: kontrollerer {total} målfil(er) som kan fjernes.")
        if total == 0:
            progress.update(0, 0, action="målfiler", eta=True)
    try:
        for index, target_path in enumerate(plan.target_paths_to_delete, start=1):
            db.target_relative_path(target, target_path)
            if target_path.exists() and not target_path.is_file():
                raise ValueError(f"Målfilen som skulle fjernes er ikke en fil: {target_path}")
            if progress is not None:
                progress.update(index, total, action="målfiler", eta=True)
    finally:
        if progress is not None:
            progress.done()


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
            face_conn = sqlite3.connect(path)
            try:
                apply_face_schema(face_conn)
                face_conn.commit()
            finally:
                face_conn.close()
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
