from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import db
from .collection_paths import (
    COLLECTION_FILE_MISSING,
    hash_stable_collection_file,
    inspect_collection_file,
    inspect_existing_collection_path_components,
    is_reparse_stat,
    parse_collection_relative_path,
    same_file_identity,
)
from .snapshot import (
    is_relative_to,
    validate_existing_path_components,
    validate_regular_file_without_links,
)
from .snapshot_repository import (
    COPY_CHUNK_SIZE,
    fsync_directory,
    open_source_without_following_links,
)
from .target_lock import TargetLock


_HARDLINK_FALLBACK_ERRNOS = {
    errno.EACCES,
    errno.EXDEV,
    errno.EINVAL,
    errno.EMLINK,
    errno.ENOSYS,
    errno.EPERM,
}
if hasattr(errno, "ENOTSUP"):
    _HARDLINK_FALLBACK_ERRNOS.add(errno.ENOTSUP)
if hasattr(errno, "EOPNOTSUPP"):
    _HARDLINK_FALLBACK_ERRNOS.add(errno.EOPNOTSUPP)


@dataclass(frozen=True)
class MissingFileRepairPlan:
    file_id: int
    state: str
    target_path: Path
    candidate_path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class MissingFileRepairResult:
    plan: MissingFileRepairPlan
    applied: bool


def repair_missing_file(
    target: Path,
    *,
    file_id: int,
    candidate_path: Path,
    apply: bool = False,
) -> MissingFileRepairResult:
    target_root = target.resolve(strict=True)
    if not apply:
        plan = build_missing_file_repair_plan(
            target_root,
            file_id=file_id,
            candidate_path=candidate_path,
        )
        return MissingFileRepairResult(plan=plan, applied=False)

    with TargetLock(target_root, command="repair-missing-file"):
        plan = build_missing_file_repair_plan(
            target_root,
            file_id=file_id,
            candidate_path=candidate_path,
        )
        apply_missing_file_repair(target_root, plan)
        return MissingFileRepairResult(plan=plan, applied=True)


def build_missing_file_repair_plan(
    target: Path,
    *,
    file_id: int,
    candidate_path: Path,
) -> MissingFileRepairPlan:
    if type(file_id) is not int or file_id < 1:
        raise ValueError("Fil-ID må være et positivt heltall.")

    target_root = target.resolve(strict=True)
    validate_existing_path_components(target_root)
    validate_regular_file_without_links(
        db.db_path_for_target(target_root),
        label="Hoveddatabasen",
    )

    row = read_repair_file_row(target_root, file_id)
    target_path = parse_collection_relative_path(row["target_path"])
    expected_sha256 = str(row["sha256"])
    expected_size = row["size_bytes"]
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError(
            f"file #{file_id} har ugyldig databaseført SHA-256."
        )
    if type(expected_size) is not int or expected_size < 0:
        raise ValueError(
            f"file #{file_id} har ugyldig databaseført størrelse."
        )

    destination_status = inspect_collection_file(target_root, target_path)
    if destination_status.status != COLLECTION_FILE_MISSING:
        detail = destination_status.message or destination_status.status
        raise ValueError(
            f"Den databaseførte målfilen for file #{file_id} mangler ikke: "
            f"{target_path.as_posix()} ({detail}). Ingen fil blir overskrevet."
        )

    candidate = Path(candidate_path).expanduser().absolute()
    validate_regular_file_without_links(
        candidate,
        label="Den gjenopprettede kopien",
    )
    candidate = candidate.resolve(strict=True)
    if is_relative_to(candidate, target_root):
        raise ValueError(
            "Den gjenopprettede kopien må ligge utenfor bildesamlingen."
        )

    actual_sha256, actual_size = hash_stable_candidate(candidate)
    if actual_size != expected_size:
        raise ValueError(
            f"Den gjenopprettede kopien har feil størrelse "
            f"(database={expected_size}, kopi={actual_size})."
        )
    if actual_sha256 != expected_sha256:
        raise ValueError(
            "Den gjenopprettede kopien har ikke samme SHA-256 som "
            f"file #{file_id}."
        )

    return MissingFileRepairPlan(
        file_id=file_id,
        state="slettet" if row["deleted_at"] is not None else "aktiv",
        target_path=target_path,
        candidate_path=candidate,
        sha256=expected_sha256,
        size_bytes=expected_size,
    )


def read_repair_file_row(target: Path, file_id: int):
    conn = db.connect_read_only(target)
    try:
        db.validate_database_health(conn)
        pending_moves = db.prepared_pending_file_moves(conn)
        if pending_moves:
            raise ValueError(
                f"Bildesamlingen har {len(pending_moves)} uavklart(e) "
                "filflytting(er). Kjør en vanlig Bildebank-kommando og "
                "kontroller samlingen før reparasjon."
            )

        row = conn.execute(
            """
            SELECT
                id, target_path, target_path_key, sha256, size_bytes,
                deleted_at
            FROM files
            WHERE id = ?
            """,
            (file_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Fant ikke file #{file_id} i hoveddatabasen.")
        pending_purge = db.pending_file_purge_for_file(
            conn,
            file_id=file_id,
        )
        if pending_purge is not None:
            raise ValueError(
                f"file #{file_id} har ventende permanent sletting "
                f"(purge #{int(pending_purge['id'])}) og kan ikke repareres."
            )

        path_issues = [
            issue
            for issue in db.file_path_integrity_issues(conn)
            if issue.file_id == file_id
        ]
        if path_issues:
            issue = path_issues[0]
            raise ValueError(
                f"file #{file_id} har databaseført stifeil i "
                f"{issue.field}: {issue.message}"
            )

        source_identity = conn.execute(
            """
            SELECT
                COUNT(file_sources.id) AS source_count,
                COALESCE(SUM(
                    CASE
                        WHEN file_sources.sha256 <> files.sha256
                          OR file_sources.size_bytes <> files.size_bytes
                        THEN 1
                        ELSE 0
                    END
                ), 0) AS mismatch_count
            FROM files
            LEFT JOIN file_sources ON file_sources.file_id = files.id
            WHERE files.id = ?
            """,
            (file_id,),
        ).fetchone()
        if int(source_identity["source_count"]) == 0:
            raise ValueError(
                f"file #{file_id} mangler file_sources-proveniens."
            )
        if int(source_identity["mismatch_count"]) != 0:
            raise ValueError(
                f"file #{file_id} har motstridende identitet i "
                "files og file_sources."
            )

        expected_key = str(row["target_path_key"])
        for pending_row in conn.execute(
            "SELECT id, path FROM pending_file_deletes"
        ):
            try:
                pending_path = parse_collection_relative_path(
                    pending_row["path"]
                )
            except ValueError:
                continue
            if db.relative_path_key(pending_path) == expected_key:
                raise ValueError(
                    f"Målstien står i pending_file_deletes "
                    f"(køpost #{int(pending_row['id'])})."
                )
        return row
    finally:
        conn.close()


def hash_stable_candidate(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = read_stable_candidate(path, digest=digest)
    return digest.hexdigest(), size_bytes


def read_stable_candidate(
    path: Path,
    *,
    digest: object,
    destination: BinaryIO | None = None,
) -> int:
    validate_regular_file_without_links(
        path,
        label="Den gjenopprettede kopien",
    )
    before = path.stat(follow_symlinks=False)
    source_fd = open_source_without_following_links(path)
    size_bytes = 0
    try:
        opened_before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or is_reparse_stat(opened_before)
            or not same_file_identity(before, opened_before)
            or before.st_size != opened_before.st_size
            or before.st_mtime_ns != opened_before.st_mtime_ns
        ):
            raise ValueError(
                "Den gjenopprettede kopien ble byttet eller endret "
                "før lesing."
            )
        with os.fdopen(source_fd, "rb", closefd=True) as source:
            source_fd = -1
            while chunk := source.read(COPY_CHUNK_SIZE):
                digest.update(chunk)  # type: ignore[attr-defined]
                size_bytes += len(chunk)
                if destination is not None:
                    destination.write(chunk)
            opened_after = os.fstat(source.fileno())
    finally:
        if source_fd >= 0:
            os.close(source_fd)

    validate_regular_file_without_links(
        path,
        label="Den gjenopprettede kopien",
    )
    after = path.stat(follow_symlinks=False)
    if not (
        same_file_identity(before, opened_after)
        and same_file_identity(opened_after, after)
        and before.st_size
        == opened_after.st_size
        == after.st_size
        == size_bytes
        and before.st_mtime_ns
        == opened_after.st_mtime_ns
        == after.st_mtime_ns
    ):
        raise ValueError(
            "Den gjenopprettede kopien ble endret under lesing."
        )
    return size_bytes


def apply_missing_file_repair(
    target: Path,
    plan: MissingFileRepairPlan,
) -> None:
    current_plan = build_missing_file_repair_plan(
        target,
        file_id=plan.file_id,
        candidate_path=plan.candidate_path,
    )
    if current_plan != plan:
        raise ValueError(
            "Reparasjonsgrunnlaget endret seg etter planlegging. "
            "Ingen fil ble kopiert."
        )

    created_directories = ensure_safe_destination_parent(
        target,
        plan.target_path,
    )
    destination = target / plan.target_path
    temporary = destination.with_name(
        f".{destination.name}.bildebank-repair-{uuid.uuid4().hex}.tmp"
    )
    published = False
    try:
        copy_candidate_to_temporary(target, plan, temporary)
        target_inspection = inspect_collection_file(target, plan.target_path)
        if target_inspection.status != COLLECTION_FILE_MISSING:
            raise FileExistsError(
                f"Målfilen dukket opp under reparasjonen og blir ikke "
                f"overskrevet: {destination}"
            )
        component_issue = inspect_existing_collection_path_components(
            target,
            plan.target_path.parent,
        )
        if component_issue is not None:
            raise ValueError(
                f"Målstien ble utrygg under reparasjonen: "
                f"{component_issue.path}: {component_issue.reason}"
            )
        publish_temporary_no_replace(
            temporary,
            destination,
            expected_sha256=plan.sha256,
            expected_size=plan.size_bytes,
        )
        published = True
        fsync_directory(destination.parent)

        actual_sha256, actual_size = hash_stable_collection_file(
            target,
            plan.target_path,
        )
        if (
            actual_sha256 != plan.sha256
            or actual_size != plan.size_bytes
        ):
            raise ValueError(
                "Den publiserte filen besto ikke avsluttende "
                "integritetskontroll. Filen er beholdt for undersøkelse."
            )
    finally:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            if not published:
                remove_created_directories_if_empty(created_directories)


def ensure_safe_destination_parent(
    target: Path,
    relative_path: Path,
) -> tuple[Path, ...]:
    created: list[Path] = []
    current = target
    for part in relative_path.parent.parts:
        current /= part
        try:
            path_stat = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            current.mkdir()
            created.append(current)
            fsync_directory(current.parent)
            continue
        except OSError as exc:
            raise ValueError(
                f"Kunne ikke kontrollere målmappen {current}: {exc}"
            ) from exc
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or is_reparse_stat(path_stat)
            or not stat.S_ISDIR(path_stat.st_mode)
        ):
            raise ValueError(
                f"Målstien går gjennom en mappe som ikke er trygg: "
                f"{current}"
            )
    return tuple(created)


def copy_candidate_to_temporary(
    target: Path,
    plan: MissingFileRepairPlan,
    temporary: Path,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    destination_fd = os.open(temporary, flags, 0o666)
    digest = hashlib.sha256()
    try:
        with os.fdopen(destination_fd, "wb", closefd=True) as destination:
            destination_fd = -1
            size_bytes = read_stable_candidate(
                plan.candidate_path,
                digest=digest,
                destination=destination,
            )
            destination.flush()
            os.fsync(destination.fileno())
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)

    if size_bytes != plan.size_bytes or digest.hexdigest() != plan.sha256:
        raise ValueError(
            "Den gjenopprettede kopien endret seg eller fikk feil innhold "
            "under kopiering."
        )
    temporary_relative = temporary.relative_to(target)
    actual_sha256, actual_size = hash_stable_collection_file(
        target,
        temporary_relative,
    )
    if actual_sha256 != plan.sha256 or actual_size != plan.size_bytes:
        raise ValueError(
            "Den midlertidige reparasjonskopien besto ikke "
            "integritetskontrollen."
        )


def publish_temporary_no_replace(
    temporary: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    try:
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except OSError as exc:
                if isinstance(exc, FileExistsError):
                    raise
                if exc.errno not in _HARDLINK_FALLBACK_ERRNOS:
                    raise
                copy_temporary_no_replace(
                    temporary,
                    destination,
                    expected_sha256=expected_sha256,
                    expected_size=expected_size,
                )
    except FileExistsError as exc:
        raise FileExistsError(
            f"Målfilen finnes allerede og blir ikke overskrevet: "
            f"{destination}"
        ) from exc
    except OSError as exc:
        if os.path.lexists(destination):
            raise FileExistsError(
                f"Målfilen finnes allerede og blir ikke overskrevet: "
                f"{destination}"
            ) from exc
        raise OSError(
            "Filsystemet kunne ikke publisere reparasjonskopien "
            f"uten overskriving: {destination}: {exc}"
        ) from exc


def copy_temporary_no_replace(
    temporary: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    source_fd = open_source_without_following_links(temporary)
    destination_fd = -1
    destination_identity: os.stat_result | None = None
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode) or is_reparse_stat(source_stat):
            raise ValueError(
                f"Den midlertidige reparasjonskopien er ikke en vanlig fil: "
                f"{temporary}"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            destination_fd = os.open(
                destination,
                flags,
                source_stat.st_mode & 0o777,
            )
        except FileExistsError as exc:
            raise FileExistsError(
                f"Målfilen finnes allerede og blir ikke overskrevet: "
                f"{destination}"
            ) from exc
        destination_identity = os.fstat(destination_fd)
        with (
            os.fdopen(source_fd, "rb", closefd=True) as source,
            os.fdopen(destination_fd, "wb", closefd=True) as target,
        ):
            source_fd = -1
            destination_fd = -1
            while chunk := source.read(COPY_CHUNK_SIZE):
                target.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            target.flush()
            os.fsync(target.fileno())
            source_after = os.fstat(source.fileno())
        if (
            not same_file_identity(source_stat, source_after)
            or source_stat.st_size != source_after.st_size
            or size_bytes != expected_size
            or digest.hexdigest() != expected_sha256
        ):
            raise ValueError(
                "Den midlertidige reparasjonskopien endret seg under "
                "publisering."
            )
    except Exception:
        remove_owned_incomplete_destination(
            destination,
            destination_identity,
        )
        raise
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)


def remove_owned_incomplete_destination(
    destination: Path,
    expected_identity: os.stat_result | None,
) -> None:
    if expected_identity is None:
        return
    try:
        current = destination.stat(follow_symlinks=False)
    except OSError:
        return
    if same_file_identity(expected_identity, current):
        try:
            destination.unlink()
        except OSError:
            pass


def remove_created_directories_if_empty(
    directories: tuple[Path, ...],
) -> None:
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except OSError:
            pass
