from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import db
from .collection_paths import (
    COLLECTION_FILE_MISSING,
    COLLECTION_FILE_OK,
    CollectionFileHashError,
    hash_stable_collection_file,
    inspect_collection_file,
    inspect_existing_collection_path_components,
    is_deleted_collection_file_path,
    same_collection_file_version,
    parse_collection_relative_path,
)
from .derived_files import (
    cleanup_empty_derived_parents,
    derived_paths_for_file_row,
    is_managed_derived_file_path,
)
from .media import IMAGE_EXTENSIONS
from .target_lock import TargetLock
from .thumbnails import THUMB_ROOT_NAME


PurgeStatus = Literal["deleted", "pending", "skipped", "integrity-error"]
OriginalPurgeState = Literal["matching", "missing", "unexpected"]


class PurgeIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class PurgeConfirmationIdentity:
    file_id: int
    sha256: str
    size_bytes: int
    expected_path: Path
    deleted_at: str


@dataclass(frozen=True)
class PendingPurgePreview:
    purge_id: int
    identity: PurgeConfirmationIdentity
    attempts: int
    last_error: str | None
    original_state: OriginalPurgeState


@dataclass(frozen=True)
class PurgePreview:
    new_candidates: tuple[PurgeConfirmationIdentity, ...]
    pending_candidates: tuple[PendingPurgePreview, ...]

    @property
    def count(self) -> int:
        return len(self.new_candidates) + len(self.pending_candidates)

    @property
    def total_size_bytes(self) -> int:
        return sum(
            candidate.size_bytes for candidate in self.new_candidates
        ) + sum(
            candidate.identity.size_bytes
            for candidate in self.pending_candidates
        )


@dataclass(frozen=True)
class PurgeFileResult:
    file_id: int
    status: PurgeStatus
    purge_id: int | None = None
    tombstone_id: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class PurgeResult:
    results: tuple[PurgeFileResult, ...]

    @property
    def deleted(self) -> int:
        return sum(result.status == "deleted" for result in self.results)

    @property
    def pending(self) -> int:
        return sum(result.status == "pending" for result in self.results)

    @property
    def skipped(self) -> int:
        return sum(result.status == "skipped" for result in self.results)

    @property
    def integrity_errors(self) -> int:
        return sum(
            result.status == "integrity-error" for result in self.results
        )


@dataclass(frozen=True)
class TombstoneConfirmationIdentity:
    tombstone_id: int
    sha256: str
    size_bytes: int
    purged_at: str


@dataclass(frozen=True)
class TombstonePreview:
    identity: TombstoneConfirmationIdentity
    original_filename: str
    former_target_path: Path


def preview_file_purge(target: Path, *, file_id: int) -> PurgePreview:
    return _preview_file_purges(target, file_id=file_id)


def preview_deleted_file_purges(target: Path) -> PurgePreview:
    return _preview_file_purges(target)


def _preview_file_purges(
    target: Path,
    *,
    file_id: int | None = None,
) -> PurgePreview:
    conn = db.connect_read_only(target)
    try:
        rows = db.deleted_file_purge_rows(conn, file_id=file_id)
        new_candidates: list[PurgeConfirmationIdentity] = []
        pending_candidates: list[PendingPurgePreview] = []
        for row in rows:
            identity = _confirmation_identity(row)
            _require_deleted_path(identity.expected_path)
            purge_id = row["purge_id"]
            if purge_id is None:
                new_candidates.append(identity)
                continue
            pending = db.pending_file_purge(
                conn,
                purge_id=int(purge_id),
            )
            if pending is None:
                raise PurgeIntegrityError(
                    "Purge-posten forsvant under read-only forhåndsvisning."
                )
            pending_candidates.append(
                PendingPurgePreview(
                    purge_id=int(purge_id),
                    identity=identity,
                    attempts=int(pending["attempts"]),
                    last_error=(
                        str(pending["last_error"])
                        if pending["last_error"] is not None
                        else None
                    ),
                    original_state=_original_state(target, identity)[0],
                )
            )
        return PurgePreview(
            new_candidates=tuple(new_candidates),
            pending_candidates=tuple(pending_candidates),
        )
    finally:
        conn.close()


def purge_file(
    target: Path,
    confirmation: PurgeConfirmationIdentity,
) -> PurgeFileResult:
    result = _purge_confirmed(
        target,
        (confirmation,),
        command="purge-file",
    )
    return result.results[0]


def purge_deleted_files(
    target: Path,
    confirmation: PurgePreview,
) -> PurgeResult:
    identities = confirmation.new_candidates
    result = _purge_confirmed(
        target,
        identities,
        command="purge-deleted",
    )
    pending_results = tuple(
        PurgeFileResult(
            file_id=candidate.identity.file_id,
            status="pending",
            purge_id=candidate.purge_id,
            error=candidate.last_error,
        )
        for candidate in confirmation.pending_candidates
    )
    return PurgeResult((*result.results, *pending_results))


def _purge_confirmed(
    target: Path,
    confirmations: tuple[PurgeConfirmationIdentity, ...],
    *,
    command: str,
) -> PurgeResult:
    _require_distinct_file_ids(confirmations)
    with TargetLock(target, command=command):
        conn = db.connect(target)
        try:
            prepared: list[tuple[PurgeConfirmationIdentity, sqlite3.Row]] = []
            results: dict[int, PurgeFileResult] = {}
            for confirmation in confirmations:
                try:
                    row = _revalidate_confirmation(conn, confirmation)
                    pending = db.pending_file_purge_for_file(
                        conn,
                        file_id=confirmation.file_id,
                    )
                    if pending is not None:
                        results[confirmation.file_id] = PurgeFileResult(
                            file_id=confirmation.file_id,
                            status="pending",
                            purge_id=int(pending["id"]),
                            error=(
                                str(pending["last_error"])
                                if pending["last_error"] is not None
                                else None
                            ),
                        )
                        continue
                    state, error = _original_state(target, confirmation)
                    if state == "missing":
                        results[confirmation.file_id] = PurgeFileResult(
                            file_id=confirmation.file_id,
                            status="skipped",
                            error="Filen mangler på forventet sti.",
                        )
                        continue
                    if state != "matching":
                        results[confirmation.file_id] = PurgeFileResult(
                            file_id=confirmation.file_id,
                            status="integrity-error",
                            error=error,
                        )
                        continue
                    prepared.append((confirmation, row))
                except (OSError, ValueError) as exc:
                    results[confirmation.file_id] = PurgeFileResult(
                        file_id=confirmation.file_id,
                        status="integrity-error",
                        error=str(exc),
                    )

            purge_ids: dict[int, int] = {}
            if prepared:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for confirmation, _row in prepared:
                        purge_ids[confirmation.file_id] = (
                            db.create_pending_file_purge(
                                conn,
                                file_id=confirmation.file_id,
                                expected_path=confirmation.expected_path.as_posix(),
                                expected_sha256=confirmation.sha256,
                                expected_size_bytes=confirmation.size_bytes,
                                expected_deleted_at=confirmation.deleted_at,
                            )
                        )
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise

            for confirmation, _row in prepared:
                purge_id = purge_ids[confirmation.file_id]
                results[confirmation.file_id] = _perform_pending_purge(
                    conn,
                    target,
                    purge_id=purge_id,
                )

            ordered_results = tuple(
                results[confirmation.file_id]
                for confirmation in confirmations
            )
            db.log_command(
                conn,
                command,
                {
                    "requested": len(confirmations),
                    "deleted": sum(
                        result.status == "deleted"
                        for result in ordered_results
                    ),
                    "pending": sum(
                        result.status == "pending"
                        for result in ordered_results
                    ),
                    "skipped": sum(
                        result.status == "skipped"
                        for result in ordered_results
                    ),
                    "integrity_errors": sum(
                        result.status == "integrity-error"
                        for result in ordered_results
                    ),
                },
            )
            conn.commit()
            return PurgeResult(ordered_results)
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def retry_file_purge(target: Path, *, purge_id: int) -> PurgeFileResult:
    with TargetLock(target, command="retry-file-purge"):
        conn = db.connect(target)
        try:
            pending = db.pending_file_purge(
                conn,
                purge_id=purge_id,
            )
            if pending is None:
                raise ValueError("Purge-posten finnes ikke.")
            result = _perform_pending_purge(
                conn,
                target,
                purge_id=purge_id,
            )
            db.log_command(
                conn,
                "retry-file-purge",
                {
                    "requested": 1,
                    "deleted": int(result.status == "deleted"),
                    "pending": int(result.status == "pending"),
                    "integrity_errors": int(
                        result.status == "integrity-error"
                    ),
                },
            )
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def abort_file_purge(
    target: Path,
    *,
    purge_id: int,
) -> PurgeConfirmationIdentity:
    with TargetLock(target, command="abort-file-purge"):
        conn = db.connect(target)
        try:
            row = _require_valid_pending_identity(conn, purge_id=purge_id)
            identity = _pending_confirmation_identity(row)
            state, error = _original_state(target, identity)
            if state != "matching":
                detail = error or "originalen mangler"
                raise PurgeIntegrityError(
                    "Kan ikke avbryte permanent sletting fordi originalen "
                    f"ikke finnes uendret: {detail}"
                )
            conn.execute("BEGIN IMMEDIATE")
            db.remove_pending_file_purge(conn, purge_id=purge_id)
            db.log_command(
                conn,
                "abort-file-purge",
                {"requested": 1, "aborted": 1},
            )
            conn.commit()
            return identity
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def recover_pending_file_purges(target: Path) -> PurgeResult:
    with TargetLock(target, command="recover-file-purges"):
        conn = db.connect(target)
        try:
            return recover_pending_file_purges_in_connection(conn, target)
        finally:
            conn.close()


def recover_pending_file_purges_in_connection(
    conn: sqlite3.Connection,
    target: Path,
) -> PurgeResult:
    results: list[PurgeFileResult] = []
    for pending in db.pending_file_purges(conn):
        purge_id = int(pending["id"])
        file_id = int(pending["file_id"])
        try:
            identity_row = _require_valid_pending_identity(
                conn,
                purge_id=purge_id,
            )
            identity = _pending_confirmation_identity(identity_row)
            state, error = _original_state(target, identity)
            if state == "matching":
                results.append(
                    PurgeFileResult(
                        file_id=file_id,
                        status="pending",
                        purge_id=purge_id,
                        error=(
                            str(pending["last_error"])
                            if pending["last_error"] is not None
                            else None
                        ),
                    )
                )
                continue
            if state == "missing":
                results.append(
                    _perform_pending_purge(
                        conn,
                        target,
                        purge_id=purge_id,
                    )
                )
                continue
            message = error or "Uventet innhold på purge-stien."
            _record_pending_error(conn, purge_id=purge_id, error=message)
            results.append(
                PurgeFileResult(
                    file_id=file_id,
                    status="integrity-error",
                    purge_id=purge_id,
                    error=message,
                )
            )
        except (OSError, ValueError) as exc:
            message = str(exc)
            try:
                _record_pending_error(
                    conn,
                    purge_id=purge_id,
                    error=message,
                )
            except ValueError:
                conn.rollback()
            results.append(
                PurgeFileResult(
                    file_id=file_id,
                    status="integrity-error",
                    purge_id=purge_id,
                    error=message,
                )
            )
    return PurgeResult(tuple(results))


def preview_file_tombstones(target: Path) -> tuple[TombstonePreview, ...]:
    conn = db.connect_read_only(target)
    try:
        return tuple(_tombstone_preview(row) for row in db.file_tombstones(conn))
    finally:
        conn.close()


def preview_file_tombstone(
    target: Path,
    *,
    tombstone_id: int,
) -> TombstonePreview:
    conn = db.connect_read_only(target)
    try:
        row = db.file_tombstone(conn, tombstone_id=tombstone_id)
        if row is None:
            raise ValueError("Tombstonen finnes ikke.")
        return _tombstone_preview(row)
    finally:
        conn.close()


def remove_tombstone(
    target: Path,
    confirmation: TombstoneConfirmationIdentity,
) -> None:
    with TargetLock(target, command="remove-file-tombstone"):
        conn = db.connect(target)
        try:
            row = db.file_tombstone(
                conn,
                tombstone_id=confirmation.tombstone_id,
            )
            if row is None or _tombstone_identity(row) != confirmation:
                raise PurgeIntegrityError(
                    "Tombstonen er endret. Oppdater forhåndsvisningen."
                )
            conn.execute("BEGIN IMMEDIATE")
            db.remove_file_tombstone(
                conn,
                tombstone_id=confirmation.tombstone_id,
            )
            db.log_command(
                conn,
                "remove-file-tombstone",
                {"requested": 1, "removed": 1},
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def _perform_pending_purge(
    conn: sqlite3.Connection,
    target: Path,
    *,
    purge_id: int,
) -> PurgeFileResult:
    row = _require_valid_pending_identity(conn, purge_id=purge_id)
    file_id = int(row["file_id"])
    identity = _pending_confirmation_identity(row)
    derived_paths: tuple[Path, ...] = ()
    try:
        file_row = _file_row_for_purge(conn, file_id=file_id)
        derived_paths = _purge_derived_paths(
            conn,
            file_row=file_row,
        )
        _delete_derived_files(
            target,
            derived_paths,
        )

        state, error = _original_state(target, identity)
        if state == "matching":
            _unlink_matching_original(target, identity)
        elif state != "missing":
            raise PurgeIntegrityError(
                error or "Uventet innhold på purge-stien."
            )

        tombstone_id = db.complete_pending_file_purge(
            conn,
            purge_id=purge_id,
        )
        conn.commit()
        cleanup_empty_derived_parents(target, derived_paths)
        _cleanup_empty_deleted_parents(target, identity.expected_path)
        return PurgeFileResult(
            file_id=file_id,
            status="deleted",
            tombstone_id=tombstone_id,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        conn.rollback()
        message = str(exc)
        try:
            _record_pending_error(
                conn,
                purge_id=purge_id,
                error=message,
            )
        except ValueError:
            conn.rollback()
        return PurgeFileResult(
            file_id=file_id,
            status="pending",
            purge_id=purge_id,
            error=message,
        )


def _revalidate_confirmation(
    conn: sqlite3.Connection,
    confirmation: PurgeConfirmationIdentity,
) -> sqlite3.Row:
    rows = db.deleted_file_purge_rows(
        conn,
        file_id=confirmation.file_id,
    )
    if not rows:
        raise PurgeIntegrityError(
            "Den bekreftede filen finnes ikke lenger som slettet fil."
        )
    row = rows[0]
    actual = _confirmation_identity(row)
    _require_deleted_path(actual.expected_path)
    if actual != confirmation:
        raise PurgeIntegrityError(
            "Den bekreftede filidentiteten er endret."
        )
    return row


def _require_valid_pending_identity(
    conn: sqlite3.Connection,
    *,
    purge_id: int,
) -> sqlite3.Row:
    row = db.pending_file_purge_identity(conn, purge_id=purge_id)
    if row is None:
        raise ValueError("Purge-posten finnes ikke.")
    if row["file_target_path"] is None:
        raise PurgeIntegrityError("Purge-posten mangler tilhørende files-rad.")
    pairs = (
        ("expected_path", "file_target_path"),
        ("expected_sha256", "file_sha256"),
        ("expected_size_bytes", "file_size_bytes"),
        ("expected_deleted_at", "file_deleted_at"),
        ("original_filename", "file_original_filename"),
        ("former_target_path", "file_former_target_path"),
    )
    if any(row[expected] != row[actual] for expected, actual in pairs):
        raise PurgeIntegrityError(
            "Purge-identiteten stemmer ikke med files-raden."
        )
    identity = _pending_confirmation_identity(row)
    _require_deleted_path(identity.expected_path)
    return row


def _confirmation_identity(row: Any) -> PurgeConfirmationIdentity:
    deleted_at = row["deleted_at"]
    if deleted_at is None:
        raise PurgeIntegrityError("Filen er ikke markert som slettet.")
    size_bytes = row["size_bytes"]
    if type(size_bytes) is not int or size_bytes < 0:
        raise PurgeIntegrityError("Filen har ugyldig databaseført størrelse.")
    return PurgeConfirmationIdentity(
        file_id=int(row["id"]),
        sha256=str(row["sha256"]),
        size_bytes=size_bytes,
        expected_path=parse_collection_relative_path(
            str(row["target_path"])
        ),
        deleted_at=str(deleted_at),
    )


def _pending_confirmation_identity(
    row: sqlite3.Row,
) -> PurgeConfirmationIdentity:
    return PurgeConfirmationIdentity(
        file_id=int(row["file_id"]),
        sha256=str(row["expected_sha256"]),
        size_bytes=int(row["expected_size_bytes"]),
        expected_path=parse_collection_relative_path(
            str(row["expected_path"])
        ),
        deleted_at=str(row["expected_deleted_at"]),
    )


def _require_deleted_path(relative_path: Path) -> None:
    if not is_deleted_collection_file_path(relative_path):
        raise PurgeIntegrityError(
            "Permanent sletting krever en gyldig databaseført sti under deleted/."
        )


def _original_state(
    target: Path,
    identity: PurgeConfirmationIdentity,
) -> tuple[OriginalPurgeState, str | None]:
    inspection = inspect_collection_file(target, identity.expected_path)
    if inspection.status == COLLECTION_FILE_MISSING:
        return "missing", None
    if inspection.status != COLLECTION_FILE_OK:
        return (
            "unexpected",
            inspection.message
            or "Purge-stien er ikke en vanlig, trygg fil.",
        )
    if inspection.size_bytes != identity.size_bytes:
        return (
            "unexpected",
            "Filstørrelsen på purge-stien stemmer ikke med bekreftelsen.",
        )
    try:
        actual_sha256, actual_size = hash_stable_collection_file(
            target,
            identity.expected_path,
        )
    except (CollectionFileHashError, OSError) as exc:
        return "unexpected", f"Filen kunne ikke hashes stabilt: {exc}"
    if (
        actual_size != identity.size_bytes
        or actual_sha256 != identity.sha256
    ):
        return (
            "unexpected",
            "Innholdet på purge-stien stemmer ikke med bekreftelsen.",
        )
    return "matching", None


def _unlink_matching_original(
    target: Path,
    identity: PurgeConfirmationIdentity,
) -> None:
    _stable_validated_unlink(
        target,
        identity.expected_path,
        expected_sha256=identity.sha256,
        expected_size_bytes=identity.size_bytes,
    )


def _delete_derived_files(
    target: Path,
    relative_paths: tuple[Path, ...],
) -> tuple[Path, ...]:
    deleted: list[Path] = []
    for relative_path in relative_paths:
        inspection = inspect_collection_file(target, relative_path)
        if inspection.status == COLLECTION_FILE_MISSING:
            continue
        if inspection.status != COLLECTION_FILE_OK:
            raise PurgeIntegrityError(
                "Avledet fil kan ikke slettes trygt: "
                f"{relative_path.as_posix()}: "
                f"{inspection.message or inspection.status}"
            )
        try:
            actual_sha256, actual_size = hash_stable_collection_file(
                target,
                relative_path,
            )
        except (CollectionFileHashError, OSError) as exc:
            raise PurgeIntegrityError(
                "Avledet fil kunne ikke identifiseres stabilt: "
                f"{relative_path.as_posix()}: {exc}"
            ) from exc
        _stable_validated_unlink(
            target,
            relative_path,
            expected_sha256=actual_sha256,
            expected_size_bytes=actual_size,
        )
        deleted.append(relative_path)
    return tuple(deleted)


def _stable_validated_unlink(
    target: Path,
    relative_path: Path,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
) -> None:
    before = inspect_collection_file(target, relative_path)
    if (
        before.status != COLLECTION_FILE_OK
        or before.path_stat is None
        or before.size_bytes != expected_size_bytes
    ):
        raise PurgeIntegrityError(
            f"Filen er endret før sletting: {relative_path.as_posix()}."
        )
    actual_sha256, actual_size = hash_stable_collection_file(
        target,
        relative_path,
    )
    after = inspect_collection_file(target, relative_path)
    if (
        actual_sha256 != expected_sha256
        or actual_size != expected_size_bytes
        or after.status != COLLECTION_FILE_OK
        or after.path_stat is None
        or not same_collection_file_version(
            before.path_stat,
            after.path_stat,
        )
    ):
        raise PurgeIntegrityError(
            f"Filen er endret før sletting: {relative_path.as_posix()}."
        )
    after.path.unlink()


def _file_row_for_purge(
    conn: sqlite3.Connection,
    *,
    file_id: int,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            id, target_path, stored_filename, sha256, deleted_at,
            deleted_original_target_path
        FROM files
        WHERE id = ?
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        raise PurgeIntegrityError("Files-raden mangler.")
    return row


def _purge_derived_paths(
    conn: sqlite3.Connection,
    *,
    file_row: sqlite3.Row,
) -> tuple[Path, ...]:
    candidates = set(derived_paths_for_file_row(file_row))
    legacy_path = _legacy_thumbnail_path(file_row)
    if legacy_path is not None:
        candidates.add(legacy_path)

    file_id = int(file_row["id"])
    referenced_by_others: set[Path] = set()
    for other in conn.execute(
        """
        SELECT
            id, target_path, stored_filename, sha256, deleted_at,
            deleted_original_target_path
        FROM files
        WHERE id <> ?
        """,
        (file_id,),
    ):
        referenced_by_others.update(derived_paths_for_file_row(other))
        other_legacy = _legacy_thumbnail_path(other)
        if other_legacy is not None:
            referenced_by_others.add(other_legacy)

    return tuple(
        sorted(
            (
                path
                for path in candidates
                if path not in referenced_by_others
                and is_managed_derived_file_path(path)
            ),
            key=lambda value: value.as_posix(),
        )
    )


def _legacy_thumbnail_path(row: Any) -> Path | None:
    stored_filename = Path(str(row["stored_filename"]))
    if (
        stored_filename.suffix.casefold() not in IMAGE_EXTENSIONS
        or stored_filename.suffix.casefold() not in {".jpg", ".jpeg"}
    ):
        return None
    original_value = (
        row["deleted_original_target_path"]
        if row["deleted_at"] is not None
        and row["deleted_original_target_path"] is not None
        else row["target_path"]
    )
    try:
        original_path = parse_collection_relative_path(str(original_value))
    except ValueError:
        return None
    return Path(THUMB_ROOT_NAME, *original_path.parts)


def _cleanup_empty_deleted_parents(
    target: Path,
    relative_path: Path,
) -> None:
    stop = Path("deleted")
    parent = relative_path.parent
    candidates: list[Path] = []
    while parent != stop and len(parent.parts) > len(stop.parts):
        candidates.append(parent)
        parent = parent.parent
    for candidate in candidates:
        if (
            inspect_existing_collection_path_components(target, candidate)
            is not None
        ):
            continue
        try:
            (target / candidate).rmdir()
        except OSError:
            continue


def _record_pending_error(
    conn: sqlite3.Connection,
    *,
    purge_id: int,
    error: str,
) -> None:
    db.update_pending_file_purge_error(
        conn,
        purge_id=purge_id,
        error=error,
    )
    conn.commit()


def _require_distinct_file_ids(
    confirmations: tuple[PurgeConfirmationIdentity, ...],
) -> None:
    file_ids = [confirmation.file_id for confirmation in confirmations]
    if len(file_ids) != len(set(file_ids)):
        raise ValueError("Bekreftelsen inneholder samme file_id flere ganger.")


def _tombstone_preview(row: Any) -> TombstonePreview:
    return TombstonePreview(
        identity=_tombstone_identity(row),
        original_filename=str(row["original_filename"]),
        former_target_path=parse_collection_relative_path(
            str(row["former_target_path"])
        ),
    )


def _tombstone_identity(row: Any) -> TombstoneConfirmationIdentity:
    size_bytes = row["size_bytes"]
    if type(size_bytes) is not int or size_bytes < 0:
        raise PurgeIntegrityError(
            "Tombstonen har ugyldig databaseført størrelse."
        )
    purged_at = row["purged_at"]
    if not isinstance(purged_at, str) or not purged_at:
        raise PurgeIntegrityError(
            "Tombstonen mangler gyldig slettetidspunkt."
        )
    return TombstoneConfirmationIdentity(
        tombstone_id=int(row["id"]),
        sha256=str(row["sha256"]),
        size_bytes=size_bytes,
        purged_at=purged_at,
    )
