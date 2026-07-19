from __future__ import annotations

import builtins
import hashlib
import json
import os
import platform
import socket
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable

from . import __version__
from .snapshot import (
    REPOSITORY_FORMAT_VERSION,
    REPOSITORY_LOCK_FILENAME,
    REPOSITORY_METADATA_FILENAME,
    snapshot_object_path,
    portable_path_key,
    read_repository_metadata,
    validate_snapshot_note,
    validate_existing_path_components,
    validate_regular_file_without_links,
)
from .target_lock import lock_details


COPY_CHUNK_SIZE = 1024 * 1024
README_FILENAME = "README.txt"
FILE_INTEGRITY_STATUSES = frozenset(
    {
        "ok",
        "missing",
        "unreadable",
        "hash_mismatch",
        "size_mismatch",
        "changed_during_snapshot",
        "unsafe_path",
        "database_backup_failed",
    }
)
SNAPSHOT_STATUSES = frozenset({"complete", "degraded", "recovery"})


class RepositoryLockError(RuntimeError):
    pass


class SnapshotStorageError(RuntimeError):
    pass


class SourceDatabaseError(RuntimeError):
    pass


class SourceFileError(SnapshotStorageError):
    pass


class SourceFileChangedError(SourceFileError):
    pass


class SourceFileUnreadableError(SourceFileError):
    pass


@dataclass(frozen=True)
class ObjectReference:
    sha256: str
    size_bytes: int

    def as_json(self) -> dict[str, str]:
        return {
            "algorithm": "sha256",
            "sha256": self.sha256,
            "size_bytes": str(self.size_bytes),
        }


@dataclass(frozen=True)
class StoredObject:
    reference: ObjectReference
    reused: bool
    source_mtime_ns: int


@dataclass(frozen=True)
class SQLiteBackup:
    object: StoredObject
    schema_version: int | None


@dataclass(frozen=True)
class ExpectedFile:
    sha256: str
    size_bytes: int

    def as_json(self) -> dict[str, str]:
        validate_sha256(self.sha256)
        validate_nonnegative_integer(self.size_bytes, label="expected.size_bytes")
        return {"sha256": self.sha256, "size_bytes": str(self.size_bytes)}


@dataclass(frozen=True)
class SnapshotFileRecord:
    original_path_display: str
    restore_kind: str
    integrity_status: str
    object: ObjectReference | None
    mtime_ns: int | None
    expected: ExpectedFile | None = None
    path: str | None = None
    record_type: str = "file"


@dataclass(frozen=True)
class SnapshotDatabaseRecord:
    role: str
    source_path_display: str
    restore_path: str | None
    required: bool
    regenerable: bool
    capture: str
    status: str
    object: ObjectReference | None
    schema_version: int | None
    model_name: str | None

    def as_json(self) -> dict[str, builtins.object]:
        validate_database_record(self)
        return {
            "capture": self.capture,
            "model_name": self.model_name,
            "object": self.object.as_json() if self.object is not None else None,
            "regenerable": self.regenerable,
            "required": self.required,
            "restore_path": self.restore_path,
            "role": self.role,
            "schema_version": self.schema_version,
            "source_path_display": self.source_path_display,
            "status": self.status,
        }


@dataclass(frozen=True)
class PublishedSnapshot:
    snapshot_id: str
    snapshot_dir: Path
    status: str
    entry_count: int


class RepositoryLock:
    def __init__(self, repository: Path, *, command: str) -> None:
        self.path = repository / REPOSITORY_LOCK_FILENAME
        self.command = command
        self.fd: int | None = None

    def __enter__(self) -> RepositoryLock:
        flags = _binary_write_flags(os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            self.fd = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            details = lock_details(self.path)
            raise RepositoryLockError(
                "Repositoryet er låst av en annen snapshot-operasjon. "
                "Vent til den er ferdig og kontroller en eventuell gammel lås manuelt."
                f"\n{details}"
            ) from exc
        except OSError as exc:
            raise RepositoryLockError(f"Kunne ikke opprette repositorylås: {self.path}: {exc}") from exc

        content = canonical_json_bytes(
            {
                "command": self.command,
                "machine_name": socket.gethostname(),
                "pid": os.getpid(),
                "started_at": utc_timestamp(),
            }
        )
        try:
            write_all(self.fd, content)
            os.fsync(self.fd)
        except OSError:
            os.close(self.fd)
            self.fd = None
            try:
                self.path.unlink()
            except OSError:
                pass
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def initialize_repository(
    repository: Path,
    source: Path,
    collection_id: str,
    *,
    repository_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, object]:
    lock_path = repository / REPOSITORY_LOCK_FILENAME
    if not lock_path.is_file():
        raise SnapshotStorageError("Repositoryet kan bare initialiseres mens repositorylåsen holdes.")
    metadata_path = repository / REPOSITORY_METADATA_FILENAME
    if metadata_path.exists():
        raise SnapshotStorageError(f"Repositorymetadata finnes allerede: {metadata_path}")
    unexpected = [entry for entry in repository.iterdir() if entry != lock_path]
    if unexpected:
        raise SnapshotStorageError(
            "Repositoryet er ikke tomt ved initialisering: "
            + ", ".join(sorted(entry.name for entry in unexpected))
        )

    canonical_collection_id = canonical_uuid(collection_id, label="collection_id")
    canonical_repository_id = canonical_uuid(repository_id or str(uuid.uuid4()), label="repository_id")
    timestamp = created_at or utc_timestamp()
    validate_timestamp(timestamp)
    metadata: dict[str, object] = {
        "collection_id": canonical_collection_id,
        "collection_name": source.name,
        "created_at": timestamp,
        "created_by": {"program": "bildebank", "version": __version__},
        "format_version": REPOSITORY_FORMAT_VERSION,
        "last_confirmed_source": {
            "collection_path": str(source.resolve()),
            "confirmed_at": timestamp,
            "machine_name": platform.node() or socket.gethostname(),
        },
        "repository_id": canonical_repository_id,
        "required_features": [],
    }
    write_new_durable_file(metadata_path, canonical_json_bytes(metadata))
    ensure_repository_layout(repository)
    return metadata


def ensure_repository_layout(repository: Path) -> None:
    validate_existing_path_components(repository)
    for directory in (
        repository / "objects" / "sha256",
        repository / "snapshots",
        repository / "incomplete",
    ):
        validate_existing_path_components(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise SnapshotStorageError(f"Repositoryoppføringen er ikke en mappe: {directory}")
    readme_path = repository / README_FILENAME
    if not readme_path.exists():
        write_new_durable_file(
            readme_path,
            (
                "Bildebank versioned backup repository\n"
                "\n"
                "Do not edit or rename files in this directory.\n"
                "Published snapshots are stored under snapshots/.\n"
                "File objects are stored under objects/sha256/.\n"
            ).encode("utf-8"),
        )
    fsync_directory(repository)


def create_staging_run(repository: Path, *, run_id: str | None = None) -> Path:
    metadata_path = repository / REPOSITORY_METADATA_FILENAME
    if not metadata_path.exists():
        raise SnapshotStorageError("Staging krever et initialisert repository med gyldig metadata.")
    validate_regular_file_without_links(metadata_path, label="Repositorymetadata")
    lock_path = repository / REPOSITORY_LOCK_FILENAME
    if not lock_path.exists():
        raise SnapshotStorageError("Staging kan bare opprettes mens repositorylåsen holdes.")
    validate_regular_file_without_links(lock_path, label="Repositorylås")
    ensure_repository_layout(repository)
    canonical_run_id = run_id or str(uuid.uuid4())
    try:
        if str(uuid.UUID(canonical_run_id)) != canonical_run_id:
            raise ValueError
    except ValueError as exc:
        raise SnapshotStorageError(f"Ugyldig run-id: {canonical_run_id!r}") from exc
    staging = repository / "incomplete" / canonical_run_id
    try:
        staging.mkdir()
    except FileExistsError as exc:
        raise SnapshotStorageError(f"Stagingområdet finnes allerede: {staging}") from exc
    fsync_directory(staging.parent)
    return staging


def store_verified_file(
    repository: Path,
    staging: Path,
    source: Path,
    *,
    on_source_chunk: Callable[[int], None] | None = None,
) -> StoredObject:
    validate_staging_path(repository, staging)
    validate_existing_path_components(source)
    candidate_directory = staging / "object-candidates"
    candidate_directory.mkdir(exist_ok=True)
    candidate = candidate_directory / f"{uuid.uuid4()}.tmp"

    try:
        before = source.lstat()
    except OSError as exc:
        raise SourceFileUnreadableError(f"Kunne ikke lese objektkilden: {source}: {exc}") from exc
    if not stat.S_ISREG(before.st_mode) or source.is_symlink():
        raise SourceFileUnreadableError(f"Objektkilden er ikke en vanlig fil uten lenke: {source}")
    source_fd = open_source_without_following_links(source)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode) or not same_file_identity(before, opened):
            raise SourceFileChangedError(f"Objektkilden ble byttet før kopiering: {source}")
        with os.fdopen(source_fd, "rb", closefd=True) as source_file, candidate.open("xb") as target_file:
            source_fd = -1
            if on_source_chunk is None:
                size_bytes = copy_and_hash(source_file, target_file, digest)
            else:
                size_bytes = copy_and_hash(
                    source_file,
                    target_file,
                    digest,
                    on_chunk=on_source_chunk,
                )
            target_file.flush()
            os.fsync(target_file.fileno())
        try:
            after = source.lstat()
        except OSError as exc:
            raise SourceFileChangedError(f"Objektkilden forsvant under kopiering: {source}: {exc}") from exc
    except Exception:
        if source_fd >= 0:
            os.close(source_fd)
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        raise

    if not stable_source(before, after, size_bytes):
        candidate.unlink(missing_ok=True)
        raise SourceFileChangedError(f"Filen endret seg under snapshotkopiering: {source}")

    reference = ObjectReference(sha256=digest.hexdigest(), size_bytes=size_bytes)
    verify_file_hash(candidate, reference, label="Stagingobjekt")
    destination = snapshot_object_path(repository, reference.sha256, reference.size_bytes)
    validate_existing_path_components(destination.parent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        validate_regular_file_without_links(destination, label="Backupobjekt")
        if destination.stat().st_size != reference.size_bytes:
            raise SnapshotStorageError(f"Eksisterende backupobjekt har feil størrelse: {destination}")
        verify_file_hash(destination, reference, label="Eksisterende backupobjekt")
        candidate.unlink()
        return StoredObject(reference=reference, reused=True, source_mtime_ns=after.st_mtime_ns)

    os.replace(candidate, destination)
    fsync_directory(destination.parent)
    return StoredObject(reference=reference, reused=False, source_mtime_ns=after.st_mtime_ns)


def backup_sqlite_database(
    repository: Path,
    staging: Path,
    source: Path,
    *,
    on_backup_progress: Callable[[int, int], None] | None = None,
) -> SQLiteBackup:
    validate_staging_path(repository, staging)
    sqlite_directory = staging / "sqlite-copies"
    sqlite_directory.mkdir(exist_ok=True)
    staging_path = sqlite_directory / f"{uuid.uuid4()}.sqlite3"

    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        source_connection = sqlite3.connect(source_uri, uri=True)
    except sqlite3.Error as exc:
        raise SourceDatabaseError(f"Kunne ikke åpne SQLite-databasen: {source}: {exc}") from exc
    try:
        require_sqlite_integrity(source_connection, source, source_error=True)
        schema_version = optional_schema_version(source_connection)
        try:
            destination_connection = sqlite3.connect(staging_path)
            try:
                if on_backup_progress is None:
                    source_connection.backup(destination_connection)
                else:
                    page_size = int(source_connection.execute("PRAGMA page_size").fetchone()[0])

                    def report_backup_progress(
                        _status: int,
                        remaining_pages: int,
                        total_pages: int,
                    ) -> None:
                        on_backup_progress(
                            max(total_pages - remaining_pages, 0) * page_size,
                            total_pages * page_size,
                        )

                    source_connection.backup(
                        destination_connection,
                        pages=256,
                        progress=report_backup_progress,
                    )
                destination_connection.commit()
            finally:
                destination_connection.close()
        except sqlite3.Error as exc:
            raise SnapshotStorageError(f"SQLite backup-API feilet for {source}: {exc}") from exc
    finally:
        source_connection.close()

    try:
        copied_connection = sqlite3.connect(f"{staging_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            require_sqlite_integrity(copied_connection, staging_path, source_error=False)
        finally:
            copied_connection.close()
    except sqlite3.Error as exc:
        raise SnapshotStorageError(f"Kunne ikke kontrollere SQLite-stagingkopien: {staging_path}: {exc}") from exc

    stored = store_verified_file(repository, staging, staging_path)
    staging_path.unlink()
    fsync_directory(sqlite_directory)
    return SQLiteBackup(object=stored, schema_version=schema_version)


def publish_snapshot(
    repository: Path,
    staging: Path,
    *,
    collection_id: str,
    repository_id: str,
    status: str,
    collection_identity_source: str,
    started_at: str,
    completed_at: str,
    files: tuple[SnapshotFileRecord, ...],
    databases: tuple[SnapshotDatabaseRecord, ...],
    schema_versions: dict[str, int | None],
    exclusions: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
    note: str | None = None,
    snapshot_id: str | None = None,
) -> PublishedSnapshot:
    validate_staging_path(repository, staging)
    canonical_collection_id = canonical_uuid(collection_id, label="collection_id")
    canonical_repository_id = canonical_uuid(repository_id, label="repository_id")
    canonical_snapshot_id = canonical_uuid(snapshot_id or str(uuid.uuid4()), label="snapshot_id")
    validate_snapshot_manifest_parameters(
        status=status,
        collection_identity_source=collection_identity_source,
        started_at=started_at,
        completed_at=completed_at,
        schema_versions=schema_versions,
        exclusions=exclusions,
        warnings=warnings,
        note=note,
    )
    validate_repository_binding(
        repository,
        collection_id=canonical_collection_id,
        repository_id=canonical_repository_id,
    )
    validate_snapshot_status_consistency(status, files, databases)

    file_entries = build_file_entries(repository, files)
    database_entries = tuple(
        record.as_json()
        for record in sorted(databases, key=lambda item: (item.role, item.source_path_display))
    )
    for record in databases:
        if record.object is not None:
            validate_published_object(repository, record.object)
        if record.role not in schema_versions or schema_versions[record.role] != record.schema_version:
            raise SnapshotStorageError(
                f"Databaseposten {record.role!r} stemmer ikke med manifestets schema_versions."
            )

    snapshot_staging = staging / "snapshot"
    try:
        snapshot_staging.mkdir()
    except FileExistsError as exc:
        raise SnapshotStorageError(f"Snapshot-staging finnes allerede: {snapshot_staging}") from exc

    files_path = snapshot_staging / "files.jsonl"
    files_reference = write_files_jsonl(files_path, file_entries)

    manifest: dict[str, object] = {
        "collection_id": canonical_collection_id,
        "collection_identity": {
            "source": collection_identity_source,
            "verified": collection_identity_source == "database",
        },
        "completed_at": completed_at,
        "created_by": {"program": "bildebank", "version": __version__},
        "databases": list(database_entries),
        "exclusions": sorted(exclusions),
        "files_jsonl": {
            "entry_count": str(len(file_entries)),
            "sha256": files_reference.sha256,
            "size_bytes": str(files_reference.size_bytes),
        },
        "format_version": REPOSITORY_FORMAT_VERSION,
        "note": note,
        "repository_id": canonical_repository_id,
        "required_features": [],
        "schema_versions": dict(schema_versions),
        "snapshot_id": canonical_snapshot_id,
        "started_at": started_at,
        "status": status,
        "warnings": sorted(warnings),
    }
    manifest_content = canonical_json_bytes(manifest)
    manifest_path = snapshot_staging / "manifest.json"
    write_new_durable_file(manifest_path, manifest_content)
    manifest_reference = reference_for_bytes(manifest_content)

    commit = {
        "files_jsonl": {
            "sha256": files_reference.sha256,
            "size_bytes": str(files_reference.size_bytes),
        },
        "format_version": REPOSITORY_FORMAT_VERSION,
        "manifest": {
            "sha256": manifest_reference.sha256,
            "size_bytes": str(manifest_reference.size_bytes),
        },
        "snapshot_id": canonical_snapshot_id,
    }
    write_new_durable_file(snapshot_staging / "commit.json", canonical_json_bytes(commit))
    verify_snapshot_staging(snapshot_staging, commit)
    fsync_directory(snapshot_staging)
    fsync_directory(staging)

    snapshots_directory = repository / "snapshots"
    validate_existing_path_components(snapshots_directory)
    snapshots_directory.mkdir(exist_ok=True)
    destination = snapshots_directory / snapshot_directory_name(completed_at, canonical_snapshot_id)
    if destination.exists() or destination.is_symlink():
        raise SnapshotStorageError(f"Snapshotmålet finnes allerede og blir ikke overskrevet: {destination}")
    try:
        os.rename(snapshot_staging, destination)
    except OSError as exc:
        raise SnapshotStorageError(f"Kunne ikke publisere snapshotet atomisk: {destination}: {exc}") from exc
    fsync_directory(snapshots_directory)
    remove_empty_staging_directories(staging)
    return PublishedSnapshot(
        snapshot_id=canonical_snapshot_id,
        snapshot_dir=destination,
        status=status,
        entry_count=len(file_entries),
    )


def build_file_entries(
    repository: Path,
    records: tuple[SnapshotFileRecord, ...],
) -> tuple[dict[str, object], ...]:
    normal_keys: set[str] = set()
    sorted_records = sorted(records, key=file_record_sort_key)
    entries: list[dict[str, object]] = []
    for index, record in enumerate(sorted_records, start=1):
        validate_file_record(record)
        if record.object is not None:
            validate_published_object(repository, record.object)
        if record.restore_kind == "normal":
            assert record.path is not None
            path_key = portable_path_key(record.path)
            assert path_key is not None
            if path_key in normal_keys:
                raise SnapshotStorageError(f"Flere snapshotposter har samme portable restore-sti: {record.path}")
            normal_keys.add(path_key)
        sequence = f"{index:012d}"
        entries.append(
            {
                "entry_id": f"e-{sequence}",
                "expected": record.expected.as_json() if record.expected is not None else None,
                "integrity_status": record.integrity_status,
                "mtime_ns": str(record.mtime_ns) if record.mtime_ns is not None else None,
                "object": record.object.as_json() if record.object is not None else None,
                "original_path_display": record.original_path_display,
                "path": record.path,
                "record_type": record.record_type,
                "recovery_name": f"entry-{sequence}.bin" if record.restore_kind == "recovery_only" else None,
                "restore_kind": record.restore_kind,
            }
        )
    return tuple(entries)


def require_sqlite_integrity(connection: sqlite3.Connection, path: Path, *, source_error: bool) -> None:
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        error_type = SourceDatabaseError if source_error else SnapshotStorageError
        raise error_type(f"Kunne ikke integritetskontrollere SQLite-databasen {path}: {exc}") from exc
    messages = [str(row[0]) for row in rows]
    if messages != ["ok"]:
        error_type = SourceDatabaseError if source_error else SnapshotStorageError
        details = "; ".join(messages[:5]) or "ukjent integritetsfeil"
        raise error_type(f"SQLite-databasen har integritetsfeil: {path}: {details}")


def optional_schema_version(connection: sqlite3.Connection) -> int | None:
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        value = int(row[0])
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def validate_snapshot_manifest_parameters(
    *,
    status: str,
    collection_identity_source: str,
    started_at: str,
    completed_at: str,
    schema_versions: dict[str, int | None],
    exclusions: tuple[str, ...],
    warnings: tuple[str, ...],
    note: str | None,
) -> None:
    if status not in SNAPSHOT_STATUSES:
        raise SnapshotStorageError(f"Ugyldig snapshotstatus: {status!r}")
    expected_identity_source = "repository" if status == "recovery" else "database"
    if collection_identity_source != expected_identity_source:
        raise SnapshotStorageError(
            f"Snapshotstatus {status!r} krever collection identity fra {expected_identity_source!r}."
        )
    validate_timestamp(started_at)
    validate_timestamp(completed_at)
    started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ")
    completed = datetime.strptime(completed_at, "%Y-%m-%dT%H:%M:%SZ")
    if completed < started:
        raise SnapshotStorageError("Snapshotets completed_at kan ikke være før started_at.")
    try:
        validate_snapshot_note(note)
    except ValueError as exc:
        raise SnapshotStorageError(str(exc)) from exc
    if not all(isinstance(item, str) for item in exclusions):
        raise SnapshotStorageError("Snapshotets exclusions må bare inneholde strenger.")
    if not all(isinstance(item, str) for item in warnings):
        raise SnapshotStorageError("Snapshotets warnings må bare inneholde strenger.")
    for role, version in schema_versions.items():
        if not isinstance(role, str) or not role:
            raise SnapshotStorageError("schema_versions har en ugyldig databaserolle.")
        if version is not None:
            validate_nonnegative_integer(version, label=f"schema_versions[{role!r}]")


def validate_repository_binding(repository: Path, *, collection_id: str, repository_id: str) -> None:
    metadata = read_repository_metadata(repository / REPOSITORY_METADATA_FILENAME)
    if metadata.get("collection_id") != collection_id:
        raise SnapshotStorageError("Snapshotets collection_id stemmer ikke med repositorymetadata.")
    if metadata.get("repository_id") != repository_id:
        raise SnapshotStorageError("Snapshotets repository_id stemmer ikke med repositorymetadata.")


def validate_file_record(record: SnapshotFileRecord) -> None:
    if not record.original_path_display:
        raise SnapshotStorageError("Snapshotposten mangler original_path_display.")
    if record.restore_kind not in {"normal", "recovery_only"}:
        raise SnapshotStorageError(f"Ugyldig restore_kind: {record.restore_kind!r}")
    if record.record_type not in {"file", "database_raw"}:
        raise SnapshotStorageError(f"Ugyldig record_type: {record.record_type!r}")
    if record.integrity_status not in FILE_INTEGRITY_STATUSES:
        raise SnapshotStorageError(f"Ugyldig integrity_status: {record.integrity_status!r}")
    if record.mtime_ns is not None:
        validate_nonnegative_integer(record.mtime_ns, label="mtime_ns")
    if record.expected is not None:
        record.expected.as_json()
    if record.object is not None:
        validate_object_reference(record.object)
    if record.restore_kind == "normal":
        if record.path is None or portable_path_key(record.path) is None:
            raise SnapshotStorageError(f"Normal snapshotpost har en utrygg restore-sti: {record.path!r}")
    elif record.path is not None:
        raise SnapshotStorageError("En recovery_only-post kan ikke ha normal restore-sti.")
    if record.integrity_status == "ok" and record.object is None:
        raise SnapshotStorageError("En snapshotpost med integrity_status 'ok' må ha et objekt.")
    if record.integrity_status == "missing" and record.object is not None:
        raise SnapshotStorageError("En manglende fil kan ikke ha en observert objektreferanse.")
    if record.record_type == "database_raw" and (
        record.restore_kind != "recovery_only" or record.expected is not None
    ):
        raise SnapshotStorageError("En rå databasepost må være recovery_only uten forventet mediehash.")


def file_record_sort_key(record: SnapshotFileRecord) -> tuple[int, str, str]:
    if record.restore_kind == "normal":
        return (0, record.path or "", record.original_path_display)
    return (1, "", record.original_path_display)


def validate_database_record(record: SnapshotDatabaseRecord) -> None:
    valid_role = record.role in {"main", "openclip"} or record.role.startswith(("face:", "auxiliary:"))
    if not valid_role or record.role.endswith(":"):
        raise SnapshotStorageError(f"Ugyldig databaserolle: {record.role!r}")
    if not record.source_path_display:
        raise SnapshotStorageError("Databaseposten mangler source_path_display.")
    if type(record.required) is not bool or type(record.regenerable) is not bool:
        raise SnapshotStorageError("Databasepostens required og regenerable må være boolske verdier.")
    if record.capture not in {"sqlite_backup", "raw_recovery"}:
        raise SnapshotStorageError(f"Ugyldig database-capture: {record.capture!r}")
    if record.status not in {"ok", "backup_failed", "unreadable"}:
        raise SnapshotStorageError(f"Ugyldig databasestatus: {record.status!r}")
    if record.schema_version is not None:
        validate_nonnegative_integer(record.schema_version, label="database.schema_version")
    if record.model_name is not None and not isinstance(record.model_name, str):
        raise SnapshotStorageError("Databasepostens model_name må være en streng eller null.")
    if record.object is not None:
        validate_object_reference(record.object)
    if record.capture == "sqlite_backup":
        if record.status != "ok" or record.object is None:
            raise SnapshotStorageError("En konsistent SQLite-kopi må ha status ok og objektreferanse.")
        if record.restore_path is None or portable_path_key(record.restore_path) is None:
            raise SnapshotStorageError("En konsistent SQLite-kopi må ha en portabel restore_path.")
    elif record.restore_path is not None:
        raise SnapshotStorageError("En raw_recovery-database kan ikke ha normal restore_path.")
    if record.capture == "raw_recovery" and record.status == "ok":
        raise SnapshotStorageError("En raw_recovery-database kan ikke ha status ok.")


def validate_snapshot_status_consistency(
    status: str,
    files: tuple[SnapshotFileRecord, ...],
    databases: tuple[SnapshotDatabaseRecord, ...],
) -> None:
    roles = [database.role for database in databases]
    if len(set(roles)) != len(roles):
        raise SnapshotStorageError("Snapshotet kan ikke ha flere databaseposter med samme rolle.")
    main_records = [database for database in databases if database.role == "main"]
    if len(main_records) != 1:
        raise SnapshotStorageError("Snapshotet må ha nøyaktig én databasepost med rollen main.")
    main = main_records[0]
    if status in {"complete", "degraded"}:
        if main.capture != "sqlite_backup" or main.status != "ok" or main.object is None:
            raise SnapshotStorageError(f"Et {status}-snapshot krever en gyldig SQLite-kopi av hoveddatabasen.")
    elif main.capture != "raw_recovery" or main.status == "ok":
        raise SnapshotStorageError("Et recovery-snapshot krever hoveddatabasen som raw_recovery.")
    if status == "complete":
        if any(file.integrity_status != "ok" or file.restore_kind != "normal" for file in files):
            raise SnapshotStorageError("Et complete-snapshot kan ikke inneholde filavvik eller recovery_only-poster.")
        if any(database.status != "ok" for database in databases):
            raise SnapshotStorageError("Et complete-snapshot kan ikke inneholde databaseavvik.")


def validate_object_reference(reference: ObjectReference) -> None:
    validate_sha256(reference.sha256)
    validate_nonnegative_integer(reference.size_bytes, label="object.size_bytes")


def validate_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SnapshotStorageError(f"Ugyldig SHA-256: {value!r}")


def validate_nonnegative_integer(value: int, *, label: str) -> None:
    if type(value) is not int or value < 0:
        raise SnapshotStorageError(f"{label} må være et ikke-negativt heltall.")


def validate_published_object(repository: Path, reference: ObjectReference) -> None:
    validate_object_reference(reference)
    path = snapshot_object_path(repository, reference.sha256, reference.size_bytes)
    try:
        validate_regular_file_without_links(path, label="Snapshotobjekt")
    except ValueError as exc:
        raise SnapshotStorageError(str(exc)) from exc
    if path.stat().st_size != reference.size_bytes:
        raise SnapshotStorageError(f"Snapshotobjektet har feil størrelse: {path}")


def reference_for_bytes(content: bytes) -> ObjectReference:
    return ObjectReference(sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content))


def write_files_jsonl(path: Path, entries: tuple[dict[str, object], ...]) -> ObjectReference:
    flags = _binary_write_flags(os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    fd: int | None = None
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        fd = os.open(path, flags, 0o600)
        for entry in entries:
            line = canonical_json_bytes(entry)
            write_all(fd, line)
            digest.update(line)
            size_bytes += len(line)
        os.fsync(fd)
        os.close(fd)
        fd = None
        fsync_directory(path.parent)
    except Exception:
        if fd is not None:
            os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return ObjectReference(sha256=digest.hexdigest(), size_bytes=size_bytes)


def snapshot_directory_name(completed_at: str, snapshot_id: str) -> str:
    validate_timestamp(completed_at)
    compact_timestamp = completed_at[:10] + "T" + completed_at[11:19].replace(":", "") + "Z"
    return f"{compact_timestamp}-{snapshot_id}"


def verify_snapshot_staging(snapshot_staging: Path, commit: dict[str, object]) -> None:
    files_path = snapshot_staging / "files.jsonl"
    manifest_path = snapshot_staging / "manifest.json"
    commit_path = snapshot_staging / "commit.json"
    for path in (files_path, manifest_path, commit_path):
        validate_regular_file_without_links(path, label="Snapshotmetadata")
    files_reference = reference_for_bytes(files_path.read_bytes())
    manifest_reference = reference_for_bytes(manifest_path.read_bytes())
    expected_files = commit["files_jsonl"]
    expected_manifest = commit["manifest"]
    if not isinstance(expected_files, dict) or not isinstance(expected_manifest, dict):
        raise SnapshotStorageError("commit.json har ugyldige kontrollsummereferanser.")
    if expected_files != {
        "sha256": files_reference.sha256,
        "size_bytes": str(files_reference.size_bytes),
    }:
        raise SnapshotStorageError("files.jsonl stemmer ikke med commit.json.")
    if expected_manifest != {
        "sha256": manifest_reference.sha256,
        "size_bytes": str(manifest_reference.size_bytes),
    }:
        raise SnapshotStorageError("manifest.json stemmer ikke med commit.json.")
    try:
        stored_commit = json.loads(commit_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotStorageError("commit.json kunne ikke leses tilbake etter skriving.") from exc
    if stored_commit != commit:
        raise SnapshotStorageError("commit.json endret seg etter skriving.")


def remove_empty_staging_directories(staging: Path) -> None:
    directories = sorted(
        (path for path in staging.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        staging.rmdir()
    except OSError:
        pass


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_uuid(value: str, *, label: str) -> str:
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as exc:
        raise SnapshotStorageError(f"Ugyldig {label}: {value!r}") from exc
    if normalized != value:
        raise SnapshotStorageError(f"Ikke-kanonisk {label}: {value!r}")
    return value


def validate_timestamp(value: str) -> None:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SnapshotStorageError(f"Ugyldig UTC-tidspunkt: {value!r}") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise SnapshotStorageError(f"Ugyldig UTC-tidspunkt: {value!r}")


def write_new_durable_file(path: Path, content: bytes) -> None:
    flags = _binary_write_flags(os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    fd: int | None = None
    try:
        fd = os.open(path, flags, 0o600)
        write_all(fd, content)
        os.fsync(fd)
        os.close(fd)
        fd = None
        fsync_directory(path.parent)
    except Exception:
        if fd is not None:
            os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _binary_write_flags(flags: int) -> int:
    return flags | getattr(os, "O_BINARY", 0)


def write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("Kunne ikke skrive alle byte til filen.")
        view = view[written:]


def open_source_without_following_links(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise SourceFileUnreadableError(f"Kunne ikke åpne objektkilden: {path}: {exc}") from exc


def copy_and_hash(
    source: BinaryIO,
    destination: BinaryIO,
    digest: object,
    *,
    on_chunk: Callable[[int], None] | None = None,
) -> int:
    size_bytes = 0
    while True:
        chunk = source.read(COPY_CHUNK_SIZE)
        if not chunk:
            return size_bytes
        destination.write(chunk)
        digest.update(chunk)  # type: ignore[attr-defined]
        size_bytes += len(chunk)
        if on_chunk is not None:
            on_chunk(len(chunk))


def verify_file_hash(path: Path, reference: ObjectReference, *, label: str) -> None:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as file:
        while chunk := file.read(COPY_CHUNK_SIZE):
            digest.update(chunk)
            size_bytes += len(chunk)
    if size_bytes != reference.size_bytes or digest.hexdigest() != reference.sha256:
        raise SnapshotStorageError(f"{label} besto ikke SHA-256-kontroll: {path}")


def validate_staging_path(repository: Path, staging: Path) -> None:
    expected_parent = (repository / "incomplete").resolve()
    try:
        staging.resolve().relative_to(expected_parent)
    except ValueError as exc:
        raise SnapshotStorageError(f"Stagingområdet ligger ikke under repositoryets incomplete/: {staging}") from exc
    if not staging.is_dir():
        raise SnapshotStorageError(f"Stagingområdet finnes ikke eller er ikke en mappe: {staging}")
    validate_existing_path_components(staging)
    metadata_path = repository / REPOSITORY_METADATA_FILENAME
    lock_path = repository / REPOSITORY_LOCK_FILENAME
    if not metadata_path.is_file() or not lock_path.is_file():
        raise SnapshotStorageError("Objektlagring krever initialisert repository og aktiv repositorylås.")
    validate_regular_file_without_links(metadata_path, label="Repositorymetadata")
    validate_regular_file_without_links(lock_path, label="Repositorylås")


def same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def stable_source(before: os.stat_result, after: os.stat_result, copied_size: int) -> bool:
    return (
        same_file_identity(before, after)
        and before.st_size == after.st_size == copied_size
        and before.st_mtime_ns == after.st_mtime_ns
    )


def fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(fd)
    except OSError:
        if os.name != "nt":
            raise
    finally:
        os.close(fd)
