from __future__ import annotations

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
from typing import BinaryIO

from . import __version__
from .snapshot import (
    REPOSITORY_FORMAT_VERSION,
    REPOSITORY_LOCK_FILENAME,
    REPOSITORY_METADATA_FILENAME,
    snapshot_object_path,
    validate_existing_path_components,
    validate_regular_file_without_links,
)
from .target_lock import lock_details


COPY_CHUNK_SIZE = 1024 * 1024
README_FILENAME = "README.txt"


class RepositoryLockError(RuntimeError):
    pass


class SnapshotStorageError(RuntimeError):
    pass


class SourceDatabaseError(RuntimeError):
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
    staging_path: Path


class RepositoryLock:
    def __init__(self, repository: Path, *, command: str) -> None:
        self.path = repository / REPOSITORY_LOCK_FILENAME
        self.command = command
        self.fd: int | None = None

    def __enter__(self) -> RepositoryLock:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
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


def store_verified_file(repository: Path, staging: Path, source: Path) -> StoredObject:
    validate_staging_path(repository, staging)
    validate_existing_path_components(source)
    candidate_directory = staging / "object-candidates"
    candidate_directory.mkdir(exist_ok=True)
    candidate = candidate_directory / f"{uuid.uuid4()}.tmp"

    before = source.lstat()
    if not stat.S_ISREG(before.st_mode) or source.is_symlink():
        raise SnapshotStorageError(f"Objektkilden er ikke en vanlig fil uten lenke: {source}")
    source_fd = open_source_without_following_links(source)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode) or not same_file_identity(before, opened):
            raise SnapshotStorageError(f"Objektkilden ble byttet før kopiering: {source}")
        with os.fdopen(source_fd, "rb", closefd=True) as source_file, candidate.open("xb") as target_file:
            source_fd = -1
            size_bytes = copy_and_hash(source_file, target_file, digest)
            target_file.flush()
            os.fsync(target_file.fileno())
        after = source.lstat()
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
        raise SnapshotStorageError(f"Filen endret seg under snapshotkopiering: {source}")

    reference = ObjectReference(sha256=digest.hexdigest(), size_bytes=size_bytes)
    verify_file_hash(candidate, reference)
    destination = snapshot_object_path(repository, reference.sha256, reference.size_bytes)
    validate_existing_path_components(destination.parent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        validate_regular_file_without_links(destination, label="Backupobjekt")
        if destination.stat().st_size != reference.size_bytes:
            raise SnapshotStorageError(f"Eksisterende backupobjekt har feil størrelse: {destination}")
        candidate.unlink()
        return StoredObject(reference=reference, reused=True, source_mtime_ns=after.st_mtime_ns)

    os.replace(candidate, destination)
    fsync_directory(destination.parent)
    return StoredObject(reference=reference, reused=False, source_mtime_ns=after.st_mtime_ns)


def backup_sqlite_database(repository: Path, staging: Path, source: Path) -> SQLiteBackup:
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
                source_connection.backup(destination_connection)
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
    return SQLiteBackup(object=stored, schema_version=schema_version, staging_path=staging_path)


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
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
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
        raise SnapshotStorageError(f"Kunne ikke åpne objektkilden: {path}: {exc}") from exc


def copy_and_hash(source: BinaryIO, destination: BinaryIO, digest: object) -> int:
    size_bytes = 0
    while True:
        chunk = source.read(COPY_CHUNK_SIZE)
        if not chunk:
            return size_bytes
        destination.write(chunk)
        digest.update(chunk)  # type: ignore[attr-defined]
        size_bytes += len(chunk)


def verify_file_hash(path: Path, reference: ObjectReference) -> None:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as file:
        while chunk := file.read(COPY_CHUNK_SIZE):
            digest.update(chunk)
            size_bytes += len(chunk)
    if size_bytes != reference.size_bytes or digest.hexdigest() != reference.sha256:
        raise SnapshotStorageError(f"Stagingobjektet besto ikke kontroll etter kopiering: {path}")


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
