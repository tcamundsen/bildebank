from __future__ import annotations

import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import AppConfig
from .item_sidecars import (
    attach_existing_item_databases,
    delete_attached_item_data,
)
from .media import sha256_file
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
    file_id: int
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
                target,
                source,
                progress=source_progress,
            )
            validate_target_paths(target, plan, progress=target_progress)
            target_content_changes = find_target_content_changes(conn, target, plan)
            if dry_run:
                conn.rollback()
                return UnimportResult(
                    plan=plan,
                    applied=False,
                    target_content_changes=target_content_changes,
                )
            if not confirm(plan):
                conn.rollback()
                return UnimportResult(
                    plan=plan,
                    applied=False,
                    target_content_changes=target_content_changes,
                )

            validate_source_files(conn, target, source)
            validate_target_paths(target, plan)
            target_content_changes = find_target_content_changes(conn, target, plan)
            if target_content_changes:
                if confirm_target_content_changes is None:
                    raise ValueError(
                        "Unimport krever eksplisitt bekreftelse for endrede filer "
                        "i bildesamlingen."
                    )
                if not confirm_target_content_changes(target_content_changes):
                    conn.rollback()
                    return UnimportResult(
                        plan=plan,
                        applied=False,
                        target_content_changes=target_content_changes,
                    )
                validate_source_files(conn, target, source)
                validate_target_paths(target, plan)
                changes_after_confirmation = find_target_content_changes(
                    conn,
                    target,
                    plan,
                )
                if changes_after_confirmation != target_content_changes:
                    raise ValueError(
                        "En fil i bildesamlingen ble endret mens bekreftelsen ventet. "
                        "Unimport er avbrutt; kjør kommandoen på nytt."
                    )

            attach_existing_item_databases(
                conn,
                target,
                config.face_recognition,
            )
            validate_source_files(conn, target, source)
            validate_target_paths(target, plan)
            final_target_content_changes = find_target_content_changes(
                conn,
                target,
                plan,
            )
            if final_target_content_changes != target_content_changes:
                raise ValueError(
                    "En fil i bildesamlingen ble endret etter kontrollen. "
                    "Unimport er avbrutt; kjør kommandoen på nytt."
                )
            validate_source_files(conn, target, source)
            conn.execute("BEGIN IMMEDIATE")
            try:
                pending_ids = apply_unimport_transaction(
                    conn,
                    target,
                    plan,
                    target_content_changes=target_content_changes,
                )
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
    target: Path,
    source: db.Source,
    *,
    progress: Any | None = None,
) -> None:
    rows = db.source_file_sources(conn, source.id)
    target_root = target.resolve()
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
            validate_path_is_not_link_or_reparse(
                source_path,
                label="Originalfilen",
            )
            try:
                source_path.resolve(strict=True).relative_to(target_root)
            except ValueError:
                pass
            else:
                raise ValueError(
                    "En registrert originalfil ligger inne i bildesamlingen og "
                    f"kan ikke brukes som grunnlag for unimport: {source_path}"
                )
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
            if target_path.exists():
                validate_path_is_not_link_or_reparse(
                    target_path,
                    label="Filen som skulle fjernes",
                )
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
                    file_id=file_id,
                    path=relative_path,
                    expected_size_bytes=expected_size,
                    actual_size_bytes=actual_size,
                    expected_sha256=expected_sha256,
                    actual_sha256=actual_sha256,
                )
            )
    return tuple(changes)


def validate_path_is_not_link_or_reparse(path: Path, *, label: str) -> None:
    path_stat = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    if stat.S_ISLNK(path_stat.st_mode) or bool(file_attributes & reparse_flag):
        raise ValueError(
            f"{label} kan ikke være en symbolsk lenke, junction eller annet "
            f"reparse point: {path}"
        )


def apply_unimport_transaction(
    conn: sqlite3.Connection,
    target: Path,
    plan: db.UnimportPlan,
    *,
    target_content_changes: tuple[TargetContentChange, ...],
) -> tuple[int, ...]:
    db.log_command(
        conn,
        "unimport",
        {"name": plan.source.name, "source_id": plan.source.id},
    )
    changed_by_id = {change.file_id: change for change in target_content_changes}
    rows: dict[int, sqlite3.Row] = {}
    if plan.file_ids_to_delete:
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
    pending_ids: list[int] = []
    for file_id in plan.file_ids_to_delete:
        row = rows[file_id]
        change = changed_by_id.get(file_id)
        expected_sha256 = (
            change.actual_sha256 if change is not None else str(row["sha256"])
        )
        expected_size_bytes = (
            change.actual_size_bytes if change is not None else int(row["size_bytes"])
        )
        pending_ids.append(
            enqueue_pending_delete_in_transaction(
                conn,
                target,
                db.absolute_target_path(target, Path(str(row["target_path"]))),
                reason="unimport",
                source_id=plan.source.id,
                expected_sha256=expected_sha256,
                expected_size_bytes=expected_size_bytes,
            ).id
        )
    delete_attached_item_data(conn, plan.file_ids_to_delete)
    db.apply_unimport(conn, plan)
    return tuple(pending_ids)
