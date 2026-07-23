from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from . import db
from .snapshot import (
    REPOSITORY_FORMAT_VERSION,
    REPOSITORY_METADATA_FILENAME,
    SUPPORTED_REQUIRED_FEATURES,
    portable_path_key,
    read_repository_metadata,
    snapshot_object_path,
    validate_canonical_uuid,
    validate_existing_path_components,
    validate_non_network_path,
    validate_regular_file_without_links,
    validate_repository_metadata_for_read,
    validate_repository_root_entries,
    validate_utc_timestamp,
    validate_database_file_row,
)
from .snapshot_repository import (
    COPY_CHUNK_SIZE,
    SNAPSHOT_STATUSES,
    ExpectedFile,
    ObjectReference,
    RepositoryLock,
    SnapshotDatabaseRecord,
    SnapshotFileRecord,
    SnapshotStorageError,
    canonical_json_bytes,
    snapshot_directory_name,
    validate_database_record,
    validate_file_record,
)


_ENTRY_ID_RE = re.compile(r"e-\d{12}")
_OBJECT_FILENAME_RE = re.compile(r"([0-9a-f]{64})-(0|[1-9]\d*)")
_SNAPSHOT_DIRECTORY_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{6}Z-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)


@dataclass(frozen=True)
class ObjectUsage:
    snapshot_id: str
    entry_id: str | None
    logical_path: str


@dataclass(frozen=True)
class SnapshotCheckIssue:
    code: str
    message: str
    snapshot_id: str | None = None
    affected: tuple[ObjectUsage, ...] = ()


@dataclass(frozen=True)
class SnapshotSummary:
    snapshot_id: str
    directory: Path
    completed_at: str
    status: str
    note: str | None
    entry_count: int
    source_problem_count: int


@dataclass(frozen=True)
class IncompleteRun:
    run_id: str
    path: Path
    size_bytes: int
    age_seconds: int


@dataclass(frozen=True)
class SnapshotCheckProgress:
    checked_objects: int
    total_objects: int
    checked_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class SnapshotCheckResult:
    repository: Path
    full: bool
    completed: bool
    cancelled: bool
    snapshots: tuple[SnapshotSummary, ...]
    issues: tuple[SnapshotCheckIssue, ...]
    incomplete_runs: tuple[IncompleteRun, ...]
    referenced_objects: int
    unreferenced_objects: int
    checked_objects: int
    checked_bytes: int
    total_objects: int
    total_bytes: int

    @property
    def exit_code(self) -> int:
        if not self.completed:
            return 1
        return 3 if self.issues else 0


ProgressCallback = Callable[[SnapshotCheckProgress], None]
CancelCallback = Callable[[], bool]
ObjectKey = tuple[str, int]


@dataclass(frozen=True)
class _RepositoryObject:
    reference: ObjectReference
    path: Path
    observed_size: int


@dataclass(frozen=True)
class SnapshotExpectedEntry:
    entry_id: str
    path: str | None
    original_path_display: str
    restore_kind: str
    integrity_status: str
    object: ObjectReference | None
    expected: ObjectReference


@dataclass(frozen=True)
class SnapshotRead:
    summary: SnapshotSummary
    usages: dict[ObjectKey, tuple[ObjectUsage, ...]]
    expected_entries: tuple[SnapshotExpectedEntry, ...]
    main_database: ObjectReference | None


def list_repository_snapshots(repository_arg: Path) -> SnapshotCheckResult:
    """List committed snapshots using the same format reader as repository checks."""

    return _inspect_repository(repository_arg, full=False, check_objects=False)


def check_snapshot_repository(
    repository_arg: Path,
    *,
    full: bool = False,
    progress: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
) -> SnapshotCheckResult:
    return _inspect_repository(
        repository_arg,
        full=full,
        check_objects=True,
        progress=progress,
        should_cancel=should_cancel,
    )


def _inspect_repository(
    repository_arg: Path,
    *,
    full: bool,
    check_objects: bool,
    progress: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
) -> SnapshotCheckResult:
    repository = resolve_repository_for_check(repository_arg)
    cancel = should_cancel or (lambda: False)
    lock_command = (
        "snapshot list"
        if not check_objects
        else ("snapshot check --full" if full else "snapshot check")
    )
    with RepositoryLock(repository, command=lock_command):
        metadata = validate_locked_repository_for_check(repository)
        repository_id = str(metadata["repository_id"])
        collection_id = str(metadata["collection_id"])
        issues: list[SnapshotCheckIssue] = []
        snapshots: list[SnapshotSummary] = []
        usages: dict[ObjectKey, list[ObjectUsage]] = {}
        cancelled = False

        for snapshot_path in snapshot_directories(repository, issues):
            if full and cancel():
                cancelled = True
                break
            try:
                read = read_snapshot(
                    snapshot_path,
                    repository_id=repository_id,
                    collection_id=collection_id,
                    should_cancel=cancel if full else None,
                    collect_expected_entries=check_objects,
                )
            except _HashCancelled:
                cancelled = True
                break
            except (OSError, UnicodeError, ValueError, SnapshotStorageError) as exc:
                issues.append(
                    SnapshotCheckIssue(
                        code="invalid_snapshot",
                        message=f"Ugyldig snapshotmappe {snapshot_path.name}: {exc}",
                    )
                )
                continue
            snapshots.append(read.summary)
            for key, snapshot_usages in read.usages.items():
                usages.setdefault(key, []).extend(snapshot_usages)
            if check_objects:
                try:
                    check_snapshot_database_files(
                        repository,
                        read,
                        issues,
                        expected_collection_id=collection_id,
                        should_cancel=cancel if full else None,
                    )
                except _HashCancelled:
                    cancelled = True
                    break

        incomplete_runs = () if cancelled else scan_incomplete_runs(repository, issues)
        objects: dict[ObjectKey, _RepositoryObject] = {}
        if check_objects and not cancelled:
            try:
                objects = scan_object_store(
                    repository,
                    issues,
                    should_cancel=cancel if full else None,
                )
            except _HashCancelled:
                cancelled = True
            if not cancelled:
                check_referenced_objects(repository, usages, objects, issues)

        total_objects = len(objects)
        total_bytes = sum(item.observed_size for item in objects.values())
        checked_objects = 0
        checked_bytes = 0
        if full and not cancelled:
            if progress is not None:
                progress(SnapshotCheckProgress(0, total_objects, 0, total_bytes))
            for key in sorted(objects):
                if cancel():
                    cancelled = True
                    break
                item = objects[key]

                def chunk_progress(chunk_size: int) -> None:
                    nonlocal checked_bytes
                    checked_bytes += chunk_size
                    if progress is not None:
                        progress(
                            SnapshotCheckProgress(
                                checked_objects,
                                total_objects,
                                checked_bytes,
                                total_bytes,
                            )
                        )

                try:
                    digest, observed_size = hash_regular_file(
                        item.path,
                        on_chunk=chunk_progress,
                        should_cancel=cancel,
                    )
                except _HashCancelled:
                    cancelled = True
                    break
                except (OSError, ValueError) as exc:
                    add_object_issue(
                        issues,
                        code="unreadable_object",
                        message=f"Objektet kunne ikke leses: {item.path}: {exc}",
                        key=key,
                        usages=usages,
                    )
                else:
                    if observed_size != item.reference.size_bytes:
                        add_object_issue(
                            issues,
                            code="object_size_mismatch",
                            message=f"Objektet har feil størrelse: {item.path}",
                            key=key,
                            usages=usages,
                        )
                    elif digest != item.reference.sha256:
                        add_object_issue(
                            issues,
                            code="object_hash_mismatch",
                            message=f"Objektet har feil SHA-256: {item.path}",
                            key=key,
                            usages=usages,
                        )
                checked_objects += 1
                if progress is not None:
                    progress(
                        SnapshotCheckProgress(
                            checked_objects,
                            total_objects,
                            checked_bytes,
                            total_bytes,
                        )
                    )

        completed = not cancelled
        referenced_keys = set(usages)
        unreferenced = len(set(objects).difference(referenced_keys)) if check_objects else 0
        return SnapshotCheckResult(
            repository=repository,
            full=full,
            completed=completed,
            cancelled=cancelled,
            snapshots=tuple(sorted(snapshots, key=lambda item: (item.completed_at, item.snapshot_id))),
            issues=tuple(issues),
            incomplete_runs=incomplete_runs,
            referenced_objects=len(referenced_keys),
            unreferenced_objects=unreferenced,
            checked_objects=checked_objects,
            checked_bytes=checked_bytes,
            total_objects=total_objects,
            total_bytes=total_bytes,
        )


def resolve_repository_for_check(repository_arg: Path) -> Path:
    repository_input = repository_arg.expanduser()
    validate_non_network_path(repository_input, label="Repositoryet")
    validate_existing_path_components(repository_input)
    repository = repository_input.resolve()
    if not repository.is_dir():
        raise ValueError(f"Repositoryet finnes ikke som mappe: {repository}")
    metadata_path = repository / REPOSITORY_METADATA_FILENAME
    validate_regular_file_without_links(metadata_path, label="Repositorymetadata")
    metadata = read_repository_metadata(metadata_path)
    validate_repository_metadata_for_read(metadata)
    entries = list(repository.iterdir())
    validate_repository_root_entries(repository, entries)
    for name in ("objects", "snapshots", "incomplete"):
        path = repository / name
        if not path.is_dir() or path.is_symlink():
            raise ValueError(f"Repositoryet mangler gyldig mappe: {path}")
    lock_path = repository / ".bildebank-repository.lock"
    if lock_path.exists() or lock_path.is_symlink():
        raise ValueError(f"Repositoryet er låst av en annen snapshot-operasjon: {lock_path}")
    return repository


def validate_locked_repository_for_check(repository: Path) -> dict[str, object]:
    metadata_path = repository / REPOSITORY_METADATA_FILENAME
    validate_regular_file_without_links(metadata_path, label="Repositorymetadata")
    metadata = read_repository_metadata(metadata_path)
    validate_repository_metadata_for_read(metadata)
    validate_repository_root_entries(repository, list(repository.iterdir()))
    validate_regular_file_without_links(
        repository / ".bildebank-repository.lock",
        label="Repositorylås",
    )
    for name in ("objects", "snapshots", "incomplete"):
        path = repository / name
        if not path.is_dir() or path.is_symlink():
            raise ValueError(f"Repositoryet mangler gyldig mappe: {path}")
    return metadata


def snapshot_directories(
    repository: Path,
    issues: list[SnapshotCheckIssue],
) -> tuple[Path, ...]:
    snapshots_root = repository / "snapshots"
    result: list[Path] = []
    for entry in sorted(os.scandir(snapshots_root), key=lambda item: item.name):
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError as exc:
            issues.append(SnapshotCheckIssue("unreadable_snapshot_entry", f"Kunne ikke lese {entry.path}: {exc}"))
            continue
        if entry.is_symlink() or not stat.S_ISDIR(entry_stat.st_mode):
            issues.append(
                SnapshotCheckIssue(
                    "invalid_snapshot_entry",
                    f"Ugyldig oppføring i snapshots-mappen: {entry.path}",
                )
            )
            continue
        result.append(Path(entry.path))
    return tuple(result)


def read_snapshot(
    snapshot_path: Path,
    *,
    repository_id: str,
    collection_id: str,
    should_cancel: CancelCallback | None = None,
    collect_expected_entries: bool = False,
) -> SnapshotRead:
    if _SNAPSHOT_DIRECTORY_RE.fullmatch(snapshot_path.name) is None:
        raise SnapshotStorageError("snapshotmappen har ugyldig navn")
    entries = sorted(entry.name for entry in os.scandir(snapshot_path))
    if entries != ["commit.json", "files.jsonl", "manifest.json"]:
        raise SnapshotStorageError("snapshotmappen skal bare inneholde commit.json, files.jsonl og manifest.json")

    commit_path = snapshot_path / "commit.json"
    manifest_path = snapshot_path / "manifest.json"
    files_path = snapshot_path / "files.jsonl"
    commit, commit_content = read_canonical_json(commit_path, label="commit.json")
    validate_commit(commit)
    manifest_reference = file_reference(manifest_path, should_cancel=should_cancel)
    files_reference = file_reference(files_path, should_cancel=should_cancel)
    require_reference(commit["manifest"], manifest_reference, label="manifest.json")
    require_reference(commit["files_jsonl"], files_reference, label="files.jsonl")

    manifest, _manifest_content = read_canonical_json(manifest_path, label="manifest.json")
    snapshot_id, completed_at, status, note, expected_entry_count = validate_manifest(
        manifest,
        repository_id=repository_id,
        collection_id=collection_id,
        files_reference=files_reference,
    )
    if commit["snapshot_id"] != snapshot_id:
        raise SnapshotStorageError("snapshot-ID er ulik i commit.json og manifest.json")
    if snapshot_path.name != snapshot_directory_name(completed_at, snapshot_id):
        raise SnapshotStorageError("snapshotmappen stemmer ikke med completed_at og snapshot_id")

    usages, entry_count, source_problem_count, file_state, expected_entries = read_file_entries(
        files_path,
        snapshot_id,
        should_cancel=should_cancel,
        collect_expected_entries=collect_expected_entries,
    )
    if entry_count != expected_entry_count:
        raise SnapshotStorageError("antall filposter stemmer ikke med manifestet")
    validate_snapshot_state(status, manifest["databases"], file_state)
    database_usages = database_object_usages(manifest["databases"], snapshot_id)
    assert isinstance(manifest["databases"], list)
    source_problem_count += sum(
        isinstance(database, dict) and database.get("status") != "ok"
        for database in manifest["databases"]
    )
    for key, values in database_usages.items():
        usages.setdefault(key, []).extend(values)
    return SnapshotRead(
        summary=SnapshotSummary(
            snapshot_id=snapshot_id,
            directory=snapshot_path,
            completed_at=completed_at,
            status=status,
            note=note,
            entry_count=entry_count,
            source_problem_count=source_problem_count,
        ),
        usages={key: tuple(values) for key, values in usages.items()},
        expected_entries=expected_entries,
        main_database=consistent_main_database_reference(status, manifest["databases"]),
    )


def validate_commit(value: dict[str, object]) -> None:
    if set(value) != {"files_jsonl", "format_version", "manifest", "snapshot_id"}:
        raise SnapshotStorageError("commit.json har feil felt")
    if value["format_version"] != REPOSITORY_FORMAT_VERSION:
        raise SnapshotStorageError("commit.json har en ustøttet format_version")
    validate_canonical_uuid(value["snapshot_id"], label="snapshot_id")
    validate_reference_mapping(value["manifest"], label="commit.manifest")
    validate_reference_mapping(value["files_jsonl"], label="commit.files_jsonl")


def validate_manifest(
    value: dict[str, object],
    *,
    repository_id: str,
    collection_id: str,
    files_reference: ObjectReference,
) -> tuple[str, str, str, str | None, int]:
    required = {
        "collection_id",
        "collection_identity",
        "completed_at",
        "created_by",
        "databases",
        "exclusions",
        "files_jsonl",
        "format_version",
        "note",
        "repository_id",
        "required_features",
        "schema_versions",
        "snapshot_id",
        "started_at",
        "status",
        "warnings",
    }
    missing = sorted(required.difference(value))
    if missing:
        raise SnapshotStorageError(f"manifest.json mangler feltet {missing[0]}")
    if value["format_version"] != REPOSITORY_FORMAT_VERSION:
        raise SnapshotStorageError("manifest.json har en ustøttet format_version")
    required_features = value["required_features"]
    if not isinstance(required_features, list) or not all(isinstance(item, str) for item in required_features):
        raise SnapshotStorageError("manifest.json har ugyldig required_features")
    unsupported = sorted(set(required_features).difference(SUPPORTED_REQUIRED_FEATURES))
    if unsupported:
        raise SnapshotStorageError(f"snapshotet krever en ukjent egenskap: {unsupported[0]}")
    snapshot_id = validate_canonical_uuid(value["snapshot_id"], label="snapshot_id")
    stored_repository_id = validate_canonical_uuid(value["repository_id"], label="repository_id")
    stored_collection_id = validate_canonical_uuid(value["collection_id"], label="collection_id")
    if stored_repository_id != repository_id or stored_collection_id != collection_id:
        raise SnapshotStorageError("manifestet er bundet til et annet repository eller en annen samling")
    validate_utc_timestamp(value["started_at"], label="started_at")
    validate_utc_timestamp(value["completed_at"], label="completed_at")
    started_at = str(value["started_at"])
    completed_at = str(value["completed_at"])
    if completed_at < started_at:
        raise SnapshotStorageError("completed_at er før started_at")
    status = value["status"]
    if status not in SNAPSHOT_STATUSES:
        raise SnapshotStorageError(f"manifest.json har ugyldig status: {status!r}")
    identity = value["collection_identity"]
    expected_identity = (
        {"source": "repository", "verified": False}
        if status == "recovery"
        else {"source": "database", "verified": True}
    )
    if identity != expected_identity:
        raise SnapshotStorageError("collection_identity stemmer ikke med snapshotstatus")
    note = value["note"]
    if note is not None and (
        not isinstance(note, str)
        or len(note) > 1_000
        or any(unicodedata.category(char) == "Cc" for char in note)
    ):
        raise SnapshotStorageError("manifest.json har ugyldig kommentar")
    created_by = value["created_by"]
    if not isinstance(created_by, dict) or not all(
        isinstance(created_by.get(field), str) and created_by.get(field)
        for field in ("program", "version")
    ):
        raise SnapshotStorageError("manifest.json har ugyldig created_by")
    for label in ("exclusions", "warnings"):
        items = value[label]
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise SnapshotStorageError(f"manifest.json har ugyldig {label}")
    schema_versions = value["schema_versions"]
    if not isinstance(schema_versions, dict) or not all(
        isinstance(role, str)
        and bool(role)
        and (version is None or (type(version) is int and version >= 0))
        for role, version in schema_versions.items()
    ):
        raise SnapshotStorageError("manifest.json har ugyldig schema_versions")
    databases = value["databases"]
    if not isinstance(databases, list):
        raise SnapshotStorageError("manifest.json har ugyldig databases")
    validate_database_entries(databases, schema_versions)
    files_mapping = value["files_jsonl"]
    if not isinstance(files_mapping, dict) or set(files_mapping) != {"entry_count", "sha256", "size_bytes"}:
        raise SnapshotStorageError("manifest.json har ugyldig files_jsonl")
    entry_count = decimal_string(files_mapping["entry_count"], label="files_jsonl.entry_count")
    require_reference(
        {"sha256": files_mapping["sha256"], "size_bytes": files_mapping["size_bytes"]},
        files_reference,
        label="manifest.files_jsonl",
    )
    return snapshot_id, completed_at, str(status), note, entry_count


def validate_database_entries(databases: list[object], schema_versions: object) -> None:
    assert isinstance(schema_versions, dict)
    roles: set[str] = set()
    for raw in databases:
        if not isinstance(raw, dict):
            raise SnapshotStorageError("manifestet har en ugyldig databasepost")
        required = {
            "capture",
            "model_name",
            "object",
            "regenerable",
            "required",
            "restore_path",
            "role",
            "schema_version",
            "source_path_display",
            "status",
        }
        if not required.issubset(raw):
            raise SnapshotStorageError("en databasepost mangler påkrevde felt")
        reference = object_reference(raw["object"], allow_none=True)
        record = SnapshotDatabaseRecord(
            role=raw["role"],
            source_path_display=raw["source_path_display"],
            restore_path=raw["restore_path"],
            required=raw["required"],
            regenerable=raw["regenerable"],
            capture=raw["capture"],
            status=raw["status"],
            object=reference,
            schema_version=raw["schema_version"],
            model_name=raw["model_name"],
        )
        validate_database_record(record)
        if record.role in roles:
            raise SnapshotStorageError(f"duplisert databaserolle: {record.role}")
        roles.add(record.role)
        if record.role not in schema_versions or schema_versions[record.role] != record.schema_version:
            raise SnapshotStorageError(f"schema_versions stemmer ikke for databaserollen {record.role}")


def read_file_entries(
    files_path: Path,
    snapshot_id: str,
    *,
    should_cancel: CancelCallback | None = None,
    collect_expected_entries: bool = False,
) -> tuple[
    dict[ObjectKey, list[ObjectUsage]],
    int,
    int,
    tuple[bool, bool],
    tuple[SnapshotExpectedEntry, ...],
]:
    usages: dict[ObjectKey, list[ObjectUsage]] = {}
    expected_entries: list[SnapshotExpectedEntry] = []
    path_keys: set[str] = set()
    seen_entry_ids: set[str] = set()
    previous_sort_key: tuple[int, str, str] | None = None
    source_problem_count = 0
    has_file_problem = False
    has_recovery_only = False
    entry_count = 0
    with files_path.open("rb") as stream:
        for entry_count, raw_line in enumerate(stream, start=1):
            if should_cancel is not None and should_cancel():
                raise _HashCancelled
            if not raw_line.endswith(b"\n") or raw_line == b"\n":
                raise SnapshotStorageError(f"files.jsonl har ugyldig linje {entry_count}")
            try:
                raw = json.loads(raw_line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise SnapshotStorageError(f"files.jsonl har ugyldig JSON på linje {entry_count}") from exc
            if not isinstance(raw, dict) or canonical_json_bytes(raw) != raw_line:
                raise SnapshotStorageError(f"files.jsonl har ikke-kanonisk post på linje {entry_count}")
            required = {
                "entry_id",
                "expected",
                "integrity_status",
                "mtime_ns",
                "object",
                "original_path_display",
                "path",
                "record_type",
                "recovery_name",
                "restore_kind",
            }
            if not required.issubset(raw):
                raise SnapshotStorageError(f"files.jsonl mangler felt på linje {entry_count}")
            entry_id = raw["entry_id"]
            if not isinstance(entry_id, str) or _ENTRY_ID_RE.fullmatch(entry_id) is None:
                raise SnapshotStorageError(f"ugyldig entry_id på linje {entry_count}")
            if entry_id in seen_entry_ids or entry_id != f"e-{entry_count:012d}":
                raise SnapshotStorageError(f"duplisert eller ustabil entry_id på linje {entry_count}")
            seen_entry_ids.add(entry_id)
            expected = expected_file(raw["expected"])
            reference = object_reference(raw["object"], allow_none=True)
            mtime_ns = optional_decimal_string(raw["mtime_ns"], label="mtime_ns")
            record = SnapshotFileRecord(
                original_path_display=raw["original_path_display"],
                restore_kind=raw["restore_kind"],
                integrity_status=raw["integrity_status"],
                object=reference,
                mtime_ns=mtime_ns,
                expected=expected,
                path=raw["path"],
                record_type=raw["record_type"],
            )
            validate_file_record(record)
            if collect_expected_entries and expected is not None:
                expected_entries.append(
                    SnapshotExpectedEntry(
                        entry_id=entry_id,
                        path=record.path,
                        original_path_display=record.original_path_display,
                        restore_kind=record.restore_kind,
                        integrity_status=record.integrity_status,
                        object=record.object,
                        expected=ObjectReference(expected.sha256, expected.size_bytes),
                    )
                )
            recovery_name = raw["recovery_name"]
            if record.restore_kind == "normal":
                if recovery_name is not None:
                    raise SnapshotStorageError(f"normal post har recovery_name på linje {entry_count}")
                assert record.path is not None
                path_key = portable_path_key(record.path)
                assert path_key is not None
                if path_key in path_keys:
                    raise SnapshotStorageError(f"duplisert portabel sti på linje {entry_count}")
                path_keys.add(path_key)
                sort_key = (0, record.path, record.original_path_display)
                logical_path = record.path
            else:
                expected_recovery_name = f"entry-{entry_count:012d}.bin"
                if recovery_name != expected_recovery_name:
                    raise SnapshotStorageError(f"ugyldig recovery_name på linje {entry_count}")
                sort_key = (1, "", record.original_path_display)
                logical_path = f"{recovery_name} ({record.original_path_display})"
                has_recovery_only = True
            if previous_sort_key is not None and sort_key < previous_sort_key:
                raise SnapshotStorageError("files.jsonl er ikke sortert kanonisk")
            previous_sort_key = sort_key
            if record.integrity_status != "ok" or record.restore_kind != "normal":
                source_problem_count += 1
                has_file_problem = True
            if reference is not None:
                key = (reference.sha256, reference.size_bytes)
                usages.setdefault(key, []).append(ObjectUsage(snapshot_id, entry_id, logical_path))
    return (
        usages,
        entry_count,
        source_problem_count,
        (has_file_problem, has_recovery_only),
        tuple(expected_entries),
    )


def validate_snapshot_state(status: str, databases: object, file_state: tuple[bool, bool]) -> None:
    assert isinstance(databases, list)
    has_file_problem, _has_recovery_only = file_state
    main = [raw for raw in databases if isinstance(raw, dict) and raw.get("role") == "main"]
    if len(main) != 1:
        raise SnapshotStorageError("snapshotet må ha nøyaktig én hoveddatabase")
    main_record = main[0]
    database_problem = any(isinstance(raw, dict) and raw.get("status") != "ok" for raw in databases)
    if status == "complete" and (has_file_problem or database_problem):
        raise SnapshotStorageError("complete-snapshotet inneholder kildeavvik")
    if status in {"complete", "degraded"} and main_record.get("capture") != "sqlite_backup":
        raise SnapshotStorageError(f"{status}-snapshotet mangler konsistent hoveddatabase")
    if status == "recovery" and main_record.get("capture") != "raw_recovery":
        raise SnapshotStorageError("recovery-snapshotet mangler rå hoveddatabase")


def consistent_main_database_reference(
    status: str,
    databases: object,
) -> ObjectReference | None:
    if status == "recovery":
        return None
    assert isinstance(databases, list)
    main = next(
        raw
        for raw in databases
        if isinstance(raw, dict) and raw.get("role") == "main"
    )
    reference = object_reference(main["object"], allow_none=False)
    assert reference is not None
    return reference


def database_object_usages(databases: object, snapshot_id: str) -> dict[ObjectKey, list[ObjectUsage]]:
    assert isinstance(databases, list)
    usages: dict[ObjectKey, list[ObjectUsage]] = {}
    for raw in databases:
        assert isinstance(raw, dict)
        reference = object_reference(raw["object"], allow_none=True)
        if reference is None:
            continue
        role = str(raw["role"])
        restore_path = raw.get("restore_path")
        logical_path = str(restore_path) if isinstance(restore_path, str) else f"database:{role}"
        usages.setdefault((reference.sha256, reference.size_bytes), []).append(
            ObjectUsage(snapshot_id, None, logical_path)
        )
    return usages


def check_snapshot_database_files(
    repository: Path,
    snapshot: SnapshotRead,
    issues: list[SnapshotCheckIssue],
    *,
    expected_collection_id: str,
    should_cancel: CancelCallback | None,
) -> None:
    if should_cancel is not None and should_cancel():
        raise _HashCancelled
    reference = snapshot.main_database
    if reference is None:
        return
    database_path = snapshot_object_path(
        repository,
        reference.sha256,
        reference.size_bytes,
    )
    try:
        path_stat = database_path.stat(follow_symlinks=False)
    except OSError:
        return
    if (
        database_path.is_symlink()
        or not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_size != reference.size_bytes
    ):
        return
    try:
        validate_snapshot_database_files(
            database_path,
            expected_collection_id=expected_collection_id,
            expected_entries=snapshot.expected_entries,
            should_cancel=should_cancel,
        )
    except _HashCancelled:
        raise
    except SnapshotStorageError as exc:
        issues.append(
            SnapshotCheckIssue(
                code="database_file_mismatch",
                message=(
                    f"Snapshot {snapshot.summary.snapshot_id} har avvik mellom "
                    f"hoveddatabasen og files.jsonl: {exc}"
                ),
                snapshot_id=snapshot.summary.snapshot_id,
            )
        )


def validate_snapshot_database_files(
    database_path: Path,
    *,
    expected_collection_id: str,
    expected_entries: tuple[SnapshotExpectedEntry, ...],
    should_cancel: CancelCallback | None = None,
) -> None:
    entries_by_path: dict[str, SnapshotExpectedEntry] = {}
    for entry in expected_entries:
        path = entry.original_path_display
        if path in entries_by_path:
            raise SnapshotStorageError(
                f"Flere databaseførte filposter gjelder {path!r}."
            )
        if entry.integrity_status == "ok" and entry.object != entry.expected:
            raise SnapshotStorageError(
                f"Filpost {entry.entry_id} har status ok, men objektet stemmer "
                f"ikke med expected for {path!r}."
            )
        entries_by_path[path] = entry

    try:
        validate_regular_file_without_links(
            database_path,
            label="Snapshotets hoveddatabaseobjekt",
        )
    except (OSError, ValueError) as exc:
        raise SnapshotStorageError(
            f"Kunne ikke kontrollere snapshotets hoveddatabaseobjekt: {database_path}: {exc}"
        ) from exc
    uri = f"{database_path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise SnapshotStorageError(
            f"Kunne ikke åpne snapshotets hoveddatabase skrivebeskyttet: {database_path}: {exc}"
        ) from exc
    connection.row_factory = sqlite3.Row
    if should_cancel is not None:
        connection.set_progress_handler(lambda: int(should_cancel()), 10_000)
    try:
        connection.execute("PRAGMA query_only = ON")
        collection_id = db.validate_collection_id(connection)
        seen_database_paths: set[str] = set()
        for row in connection.execute(
            """
            SELECT target_path, sha256, size_bytes
            FROM files
            ORDER BY target_path_key, id
            """
        ):
            if should_cancel is not None and should_cancel():
                raise _HashCancelled
            database_row = validate_database_file_row(row)
            path = database_row.target_path
            if path in seen_database_paths:
                raise SnapshotStorageError(
                    f"Hoveddatabasen har flere files-rader for {path!r}."
                )
            seen_database_paths.add(path)
            selected_entry = entries_by_path.get(path)
            if selected_entry is None:
                raise SnapshotStorageError(
                    f"Hoveddatabasens files-rad for {path!r} "
                    "mangler filpost i snapshotet."
                )
            del entries_by_path[path]
            expected = ObjectReference(
                database_row.sha256,
                database_row.size_bytes,
            )
            if selected_entry.expected != expected:
                raise SnapshotStorageError(
                    f"Expected-verdien for {selected_entry.entry_id} stemmer ikke med "
                    f"hoveddatabasens files-rad for {path!r}."
                )
            if (
                selected_entry.restore_kind == "normal"
                and selected_entry.path != path
            ):
                raise SnapshotStorageError(
                    f"Restore-stien for {selected_entry.entry_id} stemmer ikke med "
                    f"hoveddatabasens files-rad for {path!r}."
                )
    except _HashCancelled:
        raise
    except (sqlite3.Error, ValueError) as exc:
        if should_cancel is not None and should_cancel():
            raise _HashCancelled from exc
        raise SnapshotStorageError(
            f"Snapshotets hoveddatabase kunne ikke leses semantisk: {database_path}: {exc}"
        ) from exc
    finally:
        if should_cancel is not None:
            connection.set_progress_handler(None, 0)
        connection.close()
    if collection_id != expected_collection_id:
        raise SnapshotStorageError(
            "Snapshotets hoveddatabase har feil collection_id."
        )
    if entries_by_path:
        path = sorted(entries_by_path)[0]
        entry = entries_by_path[path]
        raise SnapshotStorageError(
            f"Databaseført filpost {entry.entry_id} for {path!r} "
            "finnes ikke i hoveddatabasens files-tabell."
        )


def scan_object_store(
    repository: Path,
    issues: list[SnapshotCheckIssue],
    *,
    should_cancel: CancelCallback | None = None,
) -> dict[ObjectKey, _RepositoryObject]:
    root = repository / "objects" / "sha256"
    if not root.is_dir() or root.is_symlink():
        issues.append(SnapshotCheckIssue("invalid_object_store", f"Objektlageret mangler: {root}"))
        return {}
    objects: dict[ObjectKey, _RepositoryObject] = {}
    pending = [root]
    while pending:
        if should_cancel is not None and should_cancel():
            raise _HashCancelled
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name, reverse=True)
        except OSError as exc:
            issues.append(SnapshotCheckIssue("unreadable_object_store", f"Kunne ikke lese {directory}: {exc}"))
            continue
        for entry in entries:
            if should_cancel is not None and should_cancel():
                raise _HashCancelled
            path = Path(entry.path)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                issues.append(SnapshotCheckIssue("unreadable_object", f"Kunne ikke kontrollere {path}: {exc}"))
                continue
            if entry.is_symlink():
                issues.append(SnapshotCheckIssue("linked_object_entry", f"Objektlageret inneholder en lenke: {path}"))
            elif stat.S_ISDIR(entry_stat.st_mode):
                pending.append(path)
            elif stat.S_ISREG(entry_stat.st_mode):
                relative = path.relative_to(root)
                parsed = parse_object_path(relative)
                if parsed is None:
                    issues.append(SnapshotCheckIssue("invalid_object_name", f"Ugyldig objektnavn eller plassering: {path}"))
                    continue
                reference = ObjectReference(*parsed)
                key = (reference.sha256, reference.size_bytes)
                if key in objects:
                    issues.append(SnapshotCheckIssue("duplicate_object", f"Duplisert objekt: {path}"))
                    continue
                objects[key] = _RepositoryObject(reference, path, entry_stat.st_size)
            else:
                issues.append(SnapshotCheckIssue("invalid_object_entry", f"Ugyldig oppføring i objektlageret: {path}"))
    return objects


def parse_object_path(relative: Path) -> ObjectKey | None:
    if len(relative.parts) != 3:
        return None
    first, second, filename = relative.parts
    match = _OBJECT_FILENAME_RE.fullmatch(filename)
    if match is None:
        return None
    sha256, size_text = match.groups()
    if first != sha256[:2] or second != sha256[2:4]:
        return None
    return sha256, int(size_text)


def check_referenced_objects(
    repository: Path,
    usages: dict[ObjectKey, list[ObjectUsage]],
    objects: dict[ObjectKey, _RepositoryObject],
    issues: list[SnapshotCheckIssue],
) -> None:
    for key in sorted(usages):
        expected_path = snapshot_object_path(repository, key[0], key[1])
        item = objects.get(key)
        if item is None or item.path != expected_path:
            add_object_issue(
                issues,
                code="missing_object",
                message=f"Referert objekt mangler: {expected_path}",
                key=key,
                usages=usages,
            )
    for key, item in sorted(objects.items()):
        if item.observed_size != item.reference.size_bytes:
            add_object_issue(
                issues,
                code="object_size_mismatch",
                message=f"Objektet har feil størrelse: {item.path}",
                key=key,
                usages=usages,
            )


def add_object_issue(
    issues: list[SnapshotCheckIssue],
    *,
    code: str,
    message: str,
    key: ObjectKey,
    usages: dict[ObjectKey, list[ObjectUsage]],
) -> None:
    affected = tuple(sorted(usages.get(key, ()), key=lambda item: (item.snapshot_id, item.logical_path)))
    issue = SnapshotCheckIssue(code=code, message=message, affected=affected)
    if issue not in issues:
        issues.append(issue)


def scan_incomplete_runs(
    repository: Path,
    issues: list[SnapshotCheckIssue],
) -> tuple[IncompleteRun, ...]:
    root = repository / "incomplete"
    runs: list[IncompleteRun] = []
    now = datetime.now(timezone.utc).timestamp()
    for entry in sorted(os.scandir(root), key=lambda item: item.name):
        path = Path(entry.path)
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError as exc:
            issues.append(SnapshotCheckIssue("unreadable_incomplete", f"Kunne ikke kontrollere {path}: {exc}"))
            continue
        if entry.is_symlink() or not stat.S_ISDIR(entry_stat.st_mode):
            issues.append(SnapshotCheckIssue("invalid_incomplete_entry", f"Ugyldig oppføring i incomplete: {path}"))
            continue
        size_bytes, has_entries, safe = tree_size_without_links(path)
        if not safe:
            issues.append(SnapshotCheckIssue("unsafe_incomplete", f"Utrygg eller uleselig oppføring i {path}"))
        if has_entries:
            runs.append(
                IncompleteRun(
                    run_id=entry.name,
                    path=path,
                    size_bytes=size_bytes,
                    age_seconds=max(int(now - entry_stat.st_mtime), 0),
                )
            )
    return tuple(runs)


def tree_size_without_links(root: Path) -> tuple[int, bool, bool]:
    size_bytes = 0
    has_entries = False
    safe = True
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            safe = False
            continue
        for entry in entries:
            has_entries = True
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                safe = False
                continue
            if entry.is_symlink():
                safe = False
            elif stat.S_ISDIR(entry_stat.st_mode):
                pending.append(Path(entry.path))
            elif stat.S_ISREG(entry_stat.st_mode):
                size_bytes += entry_stat.st_size
            else:
                safe = False
    return size_bytes, has_entries, safe


def read_canonical_json(path: Path, *, label: str) -> tuple[dict[str, object], bytes]:
    validate_regular_file_without_links(path, label=label)
    try:
        content = path.read_bytes()
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotStorageError(f"{label} er ikke gyldig UTF-8-JSON") from exc
    if not isinstance(value, dict):
        raise SnapshotStorageError(f"{label} må være et JSON-objekt")
    if canonical_json_bytes(value) != content:
        raise SnapshotStorageError(f"{label} er ikke kanonisk v1-JSON")
    return value, content


def file_reference(
    path: Path,
    *,
    should_cancel: CancelCallback | None = None,
) -> ObjectReference:
    digest, size_bytes = hash_regular_file(path, should_cancel=should_cancel)
    return ObjectReference(digest, size_bytes)


def hash_regular_file(
    path: Path,
    *,
    on_chunk: Callable[[int], None] | None = None,
    should_cancel: CancelCallback | None = None,
) -> tuple[str, int]:
    validate_regular_file_without_links(path, label="Fil")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"Filen er ikke en vanlig fil: {path}")
        with os.fdopen(fd, "rb", closefd=True) as stream:
            fd = -1
            size_bytes = hash_stream(
                stream,
                digest,
                on_chunk=on_chunk,
                should_cancel=should_cancel,
            )
    finally:
        if fd >= 0:
            os.close(fd)
    return digest.hexdigest(), size_bytes


def hash_stream(
    stream: BinaryIO,
    digest: object,
    *,
    on_chunk: Callable[[int], None] | None,
    should_cancel: CancelCallback | None,
) -> int:
    size_bytes = 0
    while True:
        if should_cancel is not None and should_cancel():
            raise _HashCancelled
        chunk = stream.read(COPY_CHUNK_SIZE)
        if not chunk:
            return size_bytes
        digest.update(chunk)  # type: ignore[attr-defined]
        size_bytes += len(chunk)
        if on_chunk is not None:
            on_chunk(len(chunk))


def require_reference(value: object, actual: ObjectReference, *, label: str) -> None:
    validate_reference_mapping(value, label=label)
    assert isinstance(value, dict)
    if value != {"sha256": actual.sha256, "size_bytes": str(actual.size_bytes)}:
        raise SnapshotStorageError(f"{label} stemmer ikke med kontrollsummen")


def validate_reference_mapping(value: object, *, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"sha256", "size_bytes"}:
        raise SnapshotStorageError(f"{label} har ugyldig objektreferanse")
    sha256 = value["sha256"]
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise SnapshotStorageError(f"{label} har ugyldig SHA-256")
    decimal_string(value["size_bytes"], label=f"{label}.size_bytes")


def object_reference(value: object, *, allow_none: bool) -> ObjectReference | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, dict) or set(value) != {"algorithm", "sha256", "size_bytes"}:
        raise SnapshotStorageError("ugyldig objektreferanse")
    if value["algorithm"] != "sha256":
        raise SnapshotStorageError("ukjent objektalgoritme")
    sha256 = value["sha256"]
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise SnapshotStorageError("ugyldig objekt-SHA-256")
    return ObjectReference(sha256, decimal_string(value["size_bytes"], label="object.size_bytes"))


def expected_file(value: object) -> ExpectedFile | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"sha256", "size_bytes"}:
        raise SnapshotStorageError("ugyldig expected-referanse")
    sha256 = value["sha256"]
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise SnapshotStorageError("ugyldig expected SHA-256")
    return ExpectedFile(sha256, decimal_string(value["size_bytes"], label="expected.size_bytes"))


def decimal_string(value: object, *, label: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"0|[1-9]\d*", value) is None:
        raise SnapshotStorageError(f"{label} må være en kanonisk ikke-negativ desimalstreng")
    return int(value)


def optional_decimal_string(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    return decimal_string(value, label=label)


class _HashCancelled(Exception):
    pass
