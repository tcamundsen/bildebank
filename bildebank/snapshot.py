from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import stat
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import db
from .snapshot_progress import (
    SnapshotCancelCallback,
    SnapshotPlanProgress,
    SnapshotPlanProgressCallback,
    raise_if_snapshot_cancelled,
)


REPOSITORY_METADATA_FILENAME = ".bildebank-backup-repository.json"
REPOSITORY_LOCK_FILENAME = ".bildebank-repository.lock"
REPOSITORY_FORMAT_VERSION = 1
SUPPORTED_REQUIRED_FEATURES: frozenset[str] = frozenset()
FAT_MAX_FILE_SIZE = 4 * 1024 * 1024 * 1024 - 1
ABSOLUTE_FACE_DATABASE_RESTORE_WARNING = (
    "Snapshotet inneholder face-databaser fra en absolutt database_dir. "
    "Databasene legges under .bildebank-faces/ i den gjenopprettede samlingen, "
    "men konfigurasjonen endres ikke automatisk. Kontroller og eventuelt endre "
    "face_recognition.database_dir før face-funksjonene tas i bruk."
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_UTC_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_MIGRATION_BACKUP_FILENAME_RE = re.compile(
    rf"{re.escape(db.DB_FILENAME)}\.backup-before-schema-\d+-\d{{8}}-\d{{6}}"
)
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_INVALID_WINDOWS_PATH_CHARS = frozenset('<>:"|?*\\')
_ROOT_GENERATED_HTML_FILENAMES = frozenset(
    {
        "image-search.html",
        "index.html",
        "personer.html",
    }
)
_ROOT_RUNTIME_FILENAMES = frozenset({".bildebank.log", ".bildebank.lock"})
EXCLUSION_GENERATED_HTML = "generated_html"
EXCLUSION_RUNTIME = "runtime"
EXCLUSION_THUMBNAILS = "thumbnails"
EXCLUSION_VIDEO_PREVIEWS = "video_previews"
_ALLOWED_REPOSITORY_ROOT_ENTRIES = frozenset(
    {
        REPOSITORY_METADATA_FILENAME,
        REPOSITORY_LOCK_FILENAME,
        "README.txt",
        "objects",
        "snapshots",
        "incomplete",
    }
)


@dataclass(frozen=True)
class InventoryFile:
    relative_path: str
    absolute_path: Path
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class DatabaseFileRow:
    target_path: str
    sha256: str
    size_bytes: int


class MainDatabaseSourceError(ValueError):
    pass


@dataclass(frozen=True)
class RepositoryBindingChange:
    previous_collection_path: str
    current_collection_path: str
    previous_machine_name: str
    current_machine_name: str


@dataclass(frozen=True)
class SnapshotExclusionStats:
    reason: str
    files: int
    bytes: int


@dataclass(frozen=True)
class SnapshotInventoryStats:
    total_files: int
    total_bytes: int
    excluded_files: int
    excluded_bytes: int
    exclusions: tuple[SnapshotExclusionStats, ...]
    database_storage_files: int
    database_storage_bytes: int
    database_side_files: int
    database_files: int
    matched_database_files: int
    missing_database_files: int
    wrong_size_database_files: int
    invalid_database_paths: int
    migration_backup_files: int
    migration_backup_bytes: int
    unknown_files: int
    unknown_bytes: int
    recovery_only_files: int
    path_collisions: int


@dataclass(frozen=True)
class SnapshotStorageEstimate:
    reusable_objects: int
    estimated_new_objects: int
    estimated_new_bytes: int
    free_bytes: int

    @property
    def has_estimated_capacity(self) -> bool:
        return self.estimated_new_bytes <= self.free_bytes


@dataclass(frozen=True)
class SnapshotPlan:
    source_dir: Path
    repository_dir: Path
    collection_id: str
    repository_state: str
    inventory: SnapshotInventoryStats
    storage: SnapshotStorageEstimate
    warnings: tuple[str, ...]
    binding_change: RepositoryBindingChange | None = None
    note: str | None = None

    @property
    def would_initialize_repository(self) -> bool:
        return self.repository_state in {"missing", "empty"}


def plan_snapshot(
    source_dir: Path,
    repository_arg: Path,
    *,
    configured_face_database_dir: Path | None = None,
    note: str | None = None,
    progress: SnapshotPlanProgressCallback | None = None,
    should_cancel: SnapshotCancelCallback | None = None,
) -> SnapshotPlan:
    """Build a fully read-only snapshot plan.

    The planner deliberately does not hash source files, create locks, initialize
    repository metadata, or make staging directories.
    """

    raise_if_snapshot_cancelled(should_cancel)
    clean_note = validate_snapshot_note(note)
    source = source_dir.resolve()
    validate_source_collection(source, allow_missing_main=True)

    repository_input = repository_arg.expanduser()
    validate_non_network_path(repository_input, label="Repositoryet")
    validate_existing_path_components(repository_input)
    repository = repository_input.resolve()
    validate_repository_location(source, repository)

    if progress is not None:
        progress(SnapshotPlanProgress(stage="database"))
    collection_id, database_rows = read_main_database(source, should_cancel=should_cancel)
    if progress is not None:
        progress(
            SnapshotPlanProgress(
                stage="database_complete",
                completed_objects=len(database_rows),
                total_objects=len(database_rows),
            )
        )
    repository_state, binding_change = validate_repository(
        repository,
        source,
        collection_id,
        allow_binding_change=True,
    )

    if progress is not None:
        progress(SnapshotPlanProgress(stage="inventory"))
    inventory_files = inventory_tree(
        source,
        progress=(
            None
            if progress is None
            else lambda completed, completed_bytes: progress(
                SnapshotPlanProgress(
                    stage="inventory",
                    completed_objects=completed,
                    completed_bytes=completed_bytes,
                )
            )
        ),
        should_cancel=should_cancel,
    )
    if progress is not None:
        inventory_bytes = sum(file.size_bytes for file in inventory_files)
        progress(
            SnapshotPlanProgress(
                stage="inventory",
                completed_objects=len(inventory_files),
                total_objects=len(inventory_files),
                completed_bytes=inventory_bytes,
                total_bytes=inventory_bytes,
            )
        )
    external_database_files = inventory_external_face_databases(
        source,
        repository,
        configured_face_database_dir,
        should_cancel=should_cancel,
    )
    inventory, storage, warnings = build_snapshot_statistics(
        source,
        repository,
        repository_state,
        database_rows,
        inventory_files,
        external_database_files,
        progress=progress,
        should_cancel=should_cancel,
    )
    return SnapshotPlan(
        source_dir=source,
        repository_dir=repository,
        collection_id=collection_id,
        repository_state=repository_state,
        inventory=inventory,
        storage=storage,
        warnings=warnings,
        binding_change=binding_change,
        note=clean_note,
    )


def validate_snapshot_note(note: str | None) -> str | None:
    if note is None:
        return None
    if len(note) > 1_000:
        raise ValueError("Snapshot-kommentaren kan ikke være lengre enn 1000 tegn.")
    if any(unicodedata.category(character) == "Cc" for character in note):
        raise ValueError("Snapshot-kommentaren kan ikke inneholde kontrolltegn.")
    return note


def validate_source_collection(source: Path, *, allow_missing_main: bool = False) -> None:
    if not source.is_dir() or (
        not allow_missing_main and not db.db_path_for_target(source).is_file()
    ):
        raise ValueError(f"Bildesamlingen er ikke initialisert: {source}")


def read_main_database(
    source: Path,
    *,
    should_cancel: SnapshotCancelCallback | None = None,
) -> tuple[str, tuple[DatabaseFileRow, ...]]:
    raise_if_snapshot_cancelled(should_cancel)
    database_path = db.db_path_for_target(source).resolve()
    uri = f"{database_path.as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise MainDatabaseSourceError(
            f"Kunne ikke åpne hoveddatabasen skrivebeskyttet: {database_path}"
        ) from exc
    conn.row_factory = sqlite3.Row
    if should_cancel is not None:
        conn.set_progress_handler(lambda: int(should_cancel()), 10_000)
    try:
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            raise_if_snapshot_cancelled(should_cancel)
            raise MainDatabaseSourceError(f"Hoveddatabasen kunne ikke leses: {exc}") from exc
        try:
            db.validate_database_health(conn)
        except (sqlite3.Error, ValueError) as exc:
            raise_if_snapshot_cancelled(should_cancel)
            raise MainDatabaseSourceError(f"Hoveddatabasen har integritetsfeil: {exc}") from exc
        try:
            db.require_current_schema(conn)
        except sqlite3.Error as exc:
            raise_if_snapshot_cancelled(should_cancel)
            raise MainDatabaseSourceError(f"Hoveddatabasen kunne ikke leses: {exc}") from exc
        try:
            collection_id = db.validate_collection_id(conn)
            rows_list: list[DatabaseFileRow] = []
            for row in conn.execute(
                """
                SELECT target_path, sha256, size_bytes
                FROM files
                ORDER BY target_path_key, id
                """
            ):
                raise_if_snapshot_cancelled(should_cancel)
                rows_list.append(validate_database_file_row(row))
            rows = tuple(rows_list)
        except sqlite3.Error as exc:
            raise_if_snapshot_cancelled(should_cancel)
            raise MainDatabaseSourceError(f"Hoveddatabasen kunne ikke leses: {exc}") from exc
        except ValueError as exc:
            raise ValueError(f"Hoveddatabasen kunne ikke valideres: {exc}") from exc
    finally:
        if should_cancel is not None:
            conn.set_progress_handler(None, 0)
        conn.close()
    return collection_id, rows


def validate_database_file_row(row: sqlite3.Row) -> DatabaseFileRow:
    target_path = row["target_path"]
    sha256 = row["sha256"]
    size_bytes = row["size_bytes"]
    if not isinstance(target_path, str) or not target_path:
        raise ValueError("files.target_path må være en ikke-tom streng.")
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        raise ValueError(f"Ugyldig SHA-256 i files for {target_path!r}.")
    if type(size_bytes) is not int or size_bytes < 0:
        raise ValueError(f"Ugyldig filstørrelse i files for {target_path!r}.")
    return DatabaseFileRow(target_path=target_path, sha256=sha256, size_bytes=size_bytes)


def validate_repository_location(source: Path, repository: Path) -> None:
    if repository == source:
        raise ValueError(f"Repositoryet er samme mappe som bildesamlingen: {repository}")
    if is_relative_to(repository, source):
        raise ValueError(f"Repositoryet ligger inne i bildesamlingen: {repository}")
    if is_relative_to(source, repository):
        raise ValueError(f"Repositoryet er en overmappe til bildesamlingen: {repository}")
    for parent in repository.parents:
        if (parent / REPOSITORY_METADATA_FILENAME).exists():
            raise ValueError(f"Repositoryet kan ikke ligge inne i et annet repository: {repository}")


def validate_repository(
    repository: Path,
    source: Path,
    collection_id: str,
    *,
    allow_binding_change: bool = False,
) -> tuple[str, RepositoryBindingChange | None]:
    if not repository.exists():
        parent = repository.parent
        if not parent.exists():
            raise ValueError(
                "Foreldremappen til repositoryet finnes ikke:\n"
                f"\n  {parent}\n"
                "\nOpprett foreldremappen først. Dry-run har ikke gjort noen endringer."
            )
        if not parent.is_dir():
            raise ValueError(f"Forelderen til repositoryet er ikke en mappe: {parent}")
        return "missing", None
    if not repository.is_dir():
        raise ValueError(f"Repositoryplasseringen finnes, men er ikke en mappe: {repository}")

    metadata_path = repository / REPOSITORY_METADATA_FILENAME
    entries = list(repository.iterdir())
    if not entries:
        return "empty", None
    lock_path = repository / REPOSITORY_LOCK_FILENAME
    if lock_path.exists() or lock_path.is_symlink():
        raise ValueError(
            "Repositoryet er låst av en annen snapshot-operasjon. "
            f"Dry-run har ikke gjort noen endringer. Låsfil: {lock_path}"
        )
    if not metadata_path.exists():
        raise ValueError(
            "Repositorymappen er ikke tom og mangler gyldig Bildebank-metadata:\n"
            f"\n  {repository}\n"
            "\nMappen er ikke endret. Velg en tom mappe eller et eksisterende repository."
        )
    validate_regular_file_without_links(metadata_path, label="Repositorymetadata")
    metadata = read_repository_metadata(metadata_path)
    binding_change = repository_binding_change(metadata, source, collection_id)
    if binding_change is not None and not allow_binding_change:
        raise_repository_binding_confirmation_required(binding_change)
    validate_repository_root_entries(repository, entries)
    return "existing", binding_change


def read_repository_metadata(metadata_path: Path) -> dict[str, object]:
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Repositorymetadata er ikke gyldig UTF-8-JSON: {metadata_path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Repositorymetadata må være et JSON-objekt: {metadata_path}")
    return value


def validate_repository_metadata(
    metadata: dict[str, object],
    source: Path,
    collection_id: str,
) -> None:
    binding_change = repository_binding_change(metadata, source, collection_id)
    if binding_change is not None:
        raise_repository_binding_confirmation_required(binding_change)


def repository_binding_change(
    metadata: dict[str, object],
    source: Path,
    collection_id: str,
) -> RepositoryBindingChange | None:
    validate_repository_metadata_for_read(metadata)

    stored_collection_id = metadata["collection_id"]
    if stored_collection_id != collection_id:
        raise ValueError("Repositoryet er bundet til en annen bildesamling (collection_id er ulik).")

    last_source = metadata["last_confirmed_source"]
    assert isinstance(last_source, dict)
    stored_path = last_source["collection_path"]
    stored_machine = last_source["machine_name"]
    assert isinstance(stored_path, str)
    assert isinstance(stored_machine, str)
    current_path = str(source.resolve())
    current_machine = current_machine_name()
    path_changed = normalized_path_text(Path(stored_path)) != normalized_path_text(source)
    machine_changed = stored_machine.casefold() != current_machine.casefold()
    if not path_changed and not machine_changed:
        return None
    return RepositoryBindingChange(
        previous_collection_path=stored_path,
        current_collection_path=current_path,
        previous_machine_name=stored_machine,
        current_machine_name=current_machine,
    )


def raise_repository_binding_confirmation_required(
    binding_change: RepositoryBindingChange,
) -> None:
    changed_parts = []
    if normalized_path_text(Path(binding_change.previous_collection_path)) != normalized_path_text(
        Path(binding_change.current_collection_path)
    ):
        changed_parts.append("en annen samlingssti")
    if (
        binding_change.previous_machine_name.casefold()
        != binding_change.current_machine_name.casefold()
    ):
        changed_parts.append("en annen maskin")
    changed_text = " og ".join(changed_parts)
    raise ValueError(
        f"Repositoryet er sist brukt med {changed_text}. "
        "Flytting av en samling krever eksplisitt bekreftelse."
    )


def current_machine_name() -> str:
    return platform.node() or socket.gethostname()


def validate_repository_metadata_for_read(metadata: dict[str, object]) -> None:
    """Validate repository metadata without requiring an active collection."""

    required_fields = {
        "collection_id",
        "collection_name",
        "created_at",
        "created_by",
        "format_version",
        "last_confirmed_source",
        "repository_id",
        "required_features",
    }
    missing = sorted(required_fields.difference(metadata))
    if missing:
        raise ValueError(f"Repositorymetadata mangler påkrevd felt: {missing[0]}")

    format_version = metadata["format_version"]
    if type(format_version) is not int or format_version != REPOSITORY_FORMAT_VERSION:
        raise ValueError(f"Repositoryet har en ustøttet format_version: {format_version!r}")

    repository_id = metadata["repository_id"]
    validate_canonical_uuid(repository_id, label="repository_id")
    stored_collection_id = metadata["collection_id"]
    validate_canonical_uuid(stored_collection_id, label="collection_id")

    collection_name = metadata["collection_name"]
    if not isinstance(collection_name, str) or not collection_name:
        raise ValueError("Repositorymetadata har ugyldig collection_name.")
    validate_utc_timestamp(metadata["created_at"], label="created_at")

    created_by = metadata["created_by"]
    if not isinstance(created_by, dict):
        raise ValueError("Repositorymetadata har ugyldig created_by.")
    if not isinstance(created_by.get("program"), str) or not created_by.get("program"):
        raise ValueError("Repositorymetadata mangler created_by.program.")
    if not isinstance(created_by.get("version"), str) or not created_by.get("version"):
        raise ValueError("Repositorymetadata mangler created_by.version.")

    required_features = metadata["required_features"]
    if not isinstance(required_features, list) or not all(
        isinstance(feature, str) and feature for feature in required_features
    ):
        raise ValueError("Repositorymetadata har ugyldig required_features.")
    unsupported = sorted(set(required_features).difference(SUPPORTED_REQUIRED_FEATURES))
    if unsupported:
        raise ValueError(f"Repositoryet krever en ukjent egenskap: {unsupported[0]}")

    last_source = metadata["last_confirmed_source"]
    if not isinstance(last_source, dict):
        raise ValueError("Repositorymetadata har ugyldig last_confirmed_source.")
    stored_path = last_source.get("collection_path")
    stored_machine = last_source.get("machine_name")
    if not isinstance(stored_path, str) or not stored_path:
        raise ValueError("Repositorymetadata mangler last_confirmed_source.collection_path.")
    if not isinstance(stored_machine, str) or not stored_machine:
        raise ValueError("Repositorymetadata mangler last_confirmed_source.machine_name.")
    validate_utc_timestamp(last_source.get("confirmed_at"), label="last_confirmed_source.confirmed_at")


def validate_repository_root_entries(repository: Path, entries: list[Path]) -> None:
    for entry in entries:
        if entry.name not in _ALLOWED_REPOSITORY_ROOT_ENTRIES:
            raise ValueError(f"Repositoryet har en ukjent fil eller mappe i roten: {entry.name}")
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"Kunne ikke kontrollere repositoryoppføring: {entry}: {exc}") from exc
        if entry.is_symlink() or is_reparse_stat(entry_stat):
            raise ValueError(f"Repositoryet kan ikke inneholde lenke eller reparse point i roten: {entry}")
        if entry.name in {"objects", "snapshots", "incomplete"} and not stat.S_ISDIR(entry_stat.st_mode):
            raise ValueError(f"Repositoryoppføringen skal være en mappe: {entry}")
        if entry.name not in {"objects", "snapshots", "incomplete"} and not stat.S_ISREG(entry_stat.st_mode):
            raise ValueError(f"Repositoryoppføringen skal være en vanlig fil: {entry}")


def validate_canonical_uuid(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Repositorymetadata har ugyldig {label}.")
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"Repositorymetadata har ugyldig {label}: {value!r}") from exc
    if normalized != value:
        raise ValueError(f"Repositorymetadata har ikke-kanonisk {label}: {value!r}")
    return value


def validate_utc_timestamp(value: object, *, label: str) -> None:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise ValueError(f"Repositorymetadata har ugyldig {label}.")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"Repositorymetadata har ugyldig {label}.") from exc


def inventory_tree(
    root: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: SnapshotCancelCallback | None = None,
) -> tuple[InventoryFile, ...]:
    files: list[InventoryFile] = []
    total_bytes = 0
    pending: list[tuple[Path, Path]] = [(root, Path())]
    while pending:
        raise_if_snapshot_cancelled(should_cancel)
        directory, relative_directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold(), reverse=True)
        except OSError as exc:
            raise ValueError(f"Kunne ikke lese katalog under inventar: {directory}: {exc}") from exc
        for entry in entries:
            raise_if_snapshot_cancelled(should_cancel)
            relative_path = relative_directory / entry.name
            absolute_path = Path(entry.path)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"Kunne ikke lese filstatus uten å følge lenke: {relative_path}: {exc}") from exc
            if entry.is_symlink() or is_reparse_stat(entry_stat):
                target = safe_link_target(absolute_path)
                target_text = f" Mål: {target}" if target is not None else ""
                raise ValueError(
                    "Symbolske lenker, junctions og andre reparse points støttes ikke i snapshot. "
                    f"Funnet: {relative_path}.{target_text}"
                )
            if stat.S_ISDIR(entry_stat.st_mode):
                pending.append((absolute_path, relative_path))
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise ValueError(f"Snapshot støtter bare vanlige filer og mapper. Funnet: {relative_path}")
            files.append(
                InventoryFile(
                    relative_path=relative_path.as_posix(),
                    absolute_path=absolute_path,
                    size_bytes=entry_stat.st_size,
                    mtime_ns=entry_stat.st_mtime_ns,
                )
            )
            total_bytes += entry_stat.st_size
            if progress is not None:
                progress(len(files), total_bytes)
    return tuple(sorted(files, key=lambda item: item.relative_path.casefold()))


def inventory_external_face_databases(
    source: Path,
    repository: Path,
    configured_database_dir: Path | None,
    *,
    should_cancel: SnapshotCancelCallback | None = None,
) -> tuple[InventoryFile, ...]:
    raise_if_snapshot_cancelled(should_cancel)
    if configured_database_dir is None or not configured_database_dir.is_absolute():
        return ()
    validate_non_network_path(configured_database_dir, label="Absolutt face-databasekatalog")
    validate_existing_path_components(configured_database_dir)
    database_dir = configured_database_dir.resolve()
    if is_relative_to(database_dir, repository) or is_relative_to(repository, database_dir):
        raise ValueError("Absolutt face-databasekatalog og repository kan ikke ligge i hverandre.")
    if is_relative_to(database_dir, source):
        return ()
    if not database_dir.exists():
        return ()
    if not database_dir.is_dir():
        raise ValueError(f"Absolutt face-databasekatalog er ikke en mappe: {database_dir}")
    files = inventory_tree(database_dir, should_cancel=should_cancel)
    return tuple(file for file in files if file.relative_path.lower().endswith(".sqlite3"))


def build_snapshot_statistics(
    source: Path,
    repository: Path,
    repository_state: str,
    database_rows: tuple[DatabaseFileRow, ...],
    inventory_files: tuple[InventoryFile, ...],
    external_database_files: tuple[InventoryFile, ...],
    *,
    progress: SnapshotPlanProgressCallback | None = None,
    should_cancel: SnapshotCancelCallback | None = None,
) -> tuple[SnapshotInventoryStats, SnapshotStorageEstimate, tuple[str, ...]]:
    raise_if_snapshot_cancelled(should_cancel)
    excluded_by_reason: dict[str, list[InventoryFile]] = {
        EXCLUSION_THUMBNAILS: [],
        EXCLUSION_VIDEO_PREVIEWS: [],
        EXCLUSION_GENERATED_HTML: [],
        EXCLUSION_RUNTIME: [],
    }
    database_storage: list[InventoryFile] = []
    database_side_files: list[InventoryFile] = []
    candidates: list[InventoryFile] = []
    database_path_keys = {
        file.relative_path
        for file in inventory_files
        if is_sqlite_database_file(file.relative_path)
    }
    for file in inventory_files:
        raise_if_snapshot_cancelled(should_cancel)
        exclusion_reason = snapshot_exclusion_reason(file.relative_path)
        if exclusion_reason is not None:
            excluded_by_reason[exclusion_reason].append(file)
        elif sqlite_database_path_for_side_file(file.relative_path) in database_path_keys:
            database_side_files.append(file)
        elif is_sqlite_database_file(file.relative_path):
            database_storage.append(file)
        else:
            candidates.append(file)

    exclusions = tuple(
        SnapshotExclusionStats(
            reason=reason,
            files=len(files),
            bytes=sum(file.size_bytes for file in files),
        )
        for reason, files in excluded_by_reason.items()
        if files
    )

    candidate_by_path = {file.relative_path: file for file in candidates}
    database_paths = {row.target_path for row in database_rows}
    invalid_database_paths = sum(portable_path_key(row.target_path) is None for row in database_rows)
    matched = 0
    missing = 0
    wrong_size = 0
    reusable_keys: set[tuple[str, int]] = set()
    new_known_keys: set[tuple[str, int]] = set()
    wrong_size_bytes = 0
    invalid_path_candidates: dict[str, InventoryFile] = {}
    if progress is not None:
        progress(
            SnapshotPlanProgress(
                stage="files",
                total_objects=len(database_rows),
            )
        )
    for completed, row in enumerate(database_rows, start=1):
        raise_if_snapshot_cancelled(should_cancel)
        candidate = candidate_by_path.get(row.target_path)
        if candidate is None:
            missing += 1
        elif portable_path_key(row.target_path) is None:
            invalid_path_candidates[row.target_path] = candidate
        elif candidate.size_bytes != row.size_bytes:
            wrong_size += 1
            wrong_size_bytes += candidate.size_bytes
        else:
            matched += 1
            key = (row.sha256, row.size_bytes)
            object_path = snapshot_object_path(repository, row.sha256, row.size_bytes)
            if object_path.exists() or object_path.is_symlink():
                validate_regular_file_without_links(object_path, label="Backupobjekt")
                actual_size = object_path.stat().st_size
                if actual_size != row.size_bytes:
                    raise ValueError(
                        "Et eksisterende backupobjekt har feil størrelse. "
                        "Repositoryet må kontrolleres før videre bruk: "
                        f"{object_path}"
                    )
                reusable_keys.add(key)
            else:
                new_known_keys.add(key)
        if progress is not None:
            progress(
                SnapshotPlanProgress(
                    stage="files",
                    completed_objects=completed,
                    total_objects=len(database_rows),
                )
            )

    if progress is not None:
        progress(SnapshotPlanProgress(stage="storage"))
    raise_if_snapshot_cancelled(should_cancel)

    migration_backups = [
        file
        for file in candidates
        if file.relative_path not in database_paths and is_migration_backup_file(file.relative_path)
    ]
    unknown = [
        file
        for file in candidates
        if file.relative_path not in database_paths and not is_migration_backup_file(file.relative_path)
    ]
    recovery_only_files, path_collisions = count_recovery_only_paths([*candidates, *database_storage])
    database_candidates = [*database_storage, *external_database_files]
    new_known_keys.difference_update(reusable_keys)
    known_new_bytes = sum(size for _sha256, size in new_known_keys)
    estimated_new_objects = (
        len(new_known_keys)
        + len(unknown)
        + len(migration_backups)
        + wrong_size
        + len(invalid_path_candidates)
        + len(database_candidates)
    )
    estimated_new_bytes = (
        known_new_bytes
        + sum(file.size_bytes for file in unknown)
        + sum(file.size_bytes for file in migration_backups)
        + wrong_size_bytes
        + sum(file.size_bytes for file in invalid_path_candidates.values())
        + sum(file.size_bytes for file in database_candidates)
    )
    disk_usage_path = repository if repository_state != "missing" else repository.parent
    file_size_limit = repository_file_size_limit(disk_usage_path)
    if file_size_limit is not None:
        oversized = next(
            (
                file
                for file in [*candidates, *database_candidates]
                if file.size_bytes > file_size_limit
            ),
            None,
        )
        if oversized is not None:
            raise ValueError(
                "En fil er større enn repositoryfilsystemets per-fil-grense: "
                f"{oversized.absolute_path} ({oversized.size_bytes} byte, grense {file_size_limit} byte)."
            )
    try:
        free_bytes = shutil.disk_usage(disk_usage_path).free
    except OSError as exc:
        raise ValueError(f"Kunne ikke lese ledig plass for repositoryet: {disk_usage_path}: {exc}") from exc

    stats = SnapshotInventoryStats(
        total_files=len(inventory_files) + len(external_database_files),
        total_bytes=sum(file.size_bytes for file in inventory_files)
        + sum(file.size_bytes for file in external_database_files),
        excluded_files=sum(exclusion.files for exclusion in exclusions),
        excluded_bytes=sum(exclusion.bytes for exclusion in exclusions),
        exclusions=exclusions,
        database_storage_files=len(database_candidates),
        database_storage_bytes=sum(file.size_bytes for file in database_candidates),
        database_side_files=len(database_side_files),
        database_files=len(database_rows),
        matched_database_files=matched,
        missing_database_files=missing,
        wrong_size_database_files=wrong_size,
        invalid_database_paths=invalid_database_paths,
        migration_backup_files=len(migration_backups),
        migration_backup_bytes=sum(file.size_bytes for file in migration_backups),
        unknown_files=len(unknown),
        unknown_bytes=sum(file.size_bytes for file in unknown),
        recovery_only_files=recovery_only_files,
        path_collisions=path_collisions,
    )
    storage = SnapshotStorageEstimate(
        reusable_objects=len(reusable_keys),
        estimated_new_objects=estimated_new_objects,
        estimated_new_bytes=estimated_new_bytes,
        free_bytes=free_bytes,
    )
    warnings = snapshot_plan_warnings(stats, storage)
    return stats, storage, warnings


def snapshot_plan_warnings(
    stats: SnapshotInventoryStats,
    storage: SnapshotStorageEstimate,
) -> tuple[str, ...]:
    warnings = [
        "Dry-run beregner ikke SHA-256. Endelige objekter og avvik avgjøres under reell kjøring."
    ]
    if stats.unknown_files:
        warnings.append(f"Samlingen inneholder {stats.unknown_files} fil(er) som ikke finnes i files-tabellen.")
    if stats.missing_database_files:
        warnings.append(f"{stats.missing_database_files} databaseført(e) fil(er) mangler i samlingen.")
    if stats.wrong_size_database_files:
        warnings.append(
            f"{stats.wrong_size_database_files} databaseført(e) fil(er) har en annen størrelse enn databasen."
        )
    if stats.invalid_database_paths:
        warnings.append(f"{stats.invalid_database_paths} databaseført(e) sti(er) er ikke portable.")
    if stats.recovery_only_files:
        warnings.append(
            f"{stats.recovery_only_files} fil(er) må sikres som recovery_only på grunn av sti eller kollisjon."
        )
    if not storage.has_estimated_capacity:
        warnings.append("Repositorymediet ser ikke ut til å ha nok ledig plass for det konservative estimatet.")
    return tuple(warnings)


def snapshot_exclusion_reason(relative_path: str) -> str | None:
    parts = relative_path.split("/")
    root_name = parts[0].casefold()
    if root_name == "thumbs":
        return EXCLUSION_THUMBNAILS
    if root_name == "video-previews":
        return EXCLUSION_VIDEO_PREVIEWS
    if len(parts) != 1:
        return None
    filename = parts[0].casefold()
    if filename in _ROOT_RUNTIME_FILENAMES:
        return EXCLUSION_RUNTIME
    if filename in _ROOT_GENERATED_HTML_FILENAMES or (
        filename.startswith("person-") and filename.endswith(".html")
    ):
        return EXCLUSION_GENERATED_HTML
    return None


def is_migration_backup_file(relative_path: str) -> bool:
    """Return whether this is a Bildebank migration backup at the collection root."""
    return "/" not in relative_path and _MIGRATION_BACKUP_FILENAME_RE.fullmatch(relative_path) is not None


def is_snapshot_excluded(relative_path: str) -> bool:
    return snapshot_exclusion_reason(relative_path) is not None


def is_sqlite_database_file(relative_path: str) -> bool:
    return relative_path.casefold().endswith(".sqlite3")


def is_sqlite_side_file(relative_path: str) -> bool:
    lowered = relative_path.casefold()
    return lowered.endswith((".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal"))


def sqlite_database_path_for_side_file(relative_path: str) -> str | None:
    lowered = relative_path.casefold()
    for suffix in ("-wal", "-shm", "-journal"):
        if lowered.endswith(f".sqlite3{suffix}"):
            return relative_path[: -len(suffix)]
    return None


def snapshot_object_path(repository: Path, sha256: str, size_bytes: int) -> Path:
    if _SHA256_RE.fullmatch(sha256) is None:
        raise ValueError(f"Ugyldig SHA-256 for objektreferanse: {sha256!r}")
    if type(size_bytes) is not int or size_bytes < 0:
        raise ValueError(f"Ugyldig størrelse for objektreferanse: {size_bytes!r}")
    return repository / "objects" / "sha256" / sha256[:2] / sha256[2:4] / f"{sha256}-{size_bytes}"


def portable_path_key(path: str) -> str | None:
    if not path or path.startswith(("/", "\\")):
        return None
    parts = path.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        return None
    for part in parts:
        if part[-1] in {".", " "}:
            return None
        if any(character in _INVALID_WINDOWS_PATH_CHARS for character in part):
            return None
        if any(ord(character) < 32 or unicodedata.category(character) == "Cc" for character in part):
            return None
        reserved_base = part.split(".", 1)[0].upper()
        if reserved_base in _RESERVED_WINDOWS_NAMES:
            return None
    return "/".join(part.casefold() for part in parts)


def count_recovery_only_paths(files: list[InventoryFile]) -> tuple[int, int]:
    unsafe_indexes: set[int] = set()
    first_index_by_key: dict[str, int] = {}
    collision_keys: set[str] = set()
    for index, file in enumerate(files):
        key = portable_path_key(file.relative_path)
        if key is None:
            unsafe_indexes.add(index)
            continue
        previous = first_index_by_key.get(key)
        if previous is None:
            first_index_by_key[key] = index
            continue
        unsafe_indexes.add(previous)
        unsafe_indexes.add(index)
        collision_keys.add(key)
    return len(unsafe_indexes), len(collision_keys)


def validate_regular_file_without_links(path: Path, *, label: str) -> None:
    validate_existing_path_components(path)
    try:
        path_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{label} kunne ikke leses: {path}: {exc}") from exc
    if is_reparse_stat(path_stat) or not stat.S_ISREG(path_stat.st_mode):
        raise ValueError(f"{label} er ikke en vanlig fil uten lenker: {path}")


def validate_existing_path_components(path: Path) -> None:
    expanded = path.expanduser()
    components = [expanded, *expanded.parents]
    for component in reversed(components):
        if not component.exists() and not component.is_symlink():
            continue
        try:
            component_stat = component.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"Kunne ikke kontrollere stikomponent: {component}: {exc}") from exc
        if component.is_symlink() or is_reparse_stat(component_stat):
            raise ValueError(f"Stien kan ikke gå gjennom en lenke eller et reparse point: {component}")


def is_reparse_stat(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def safe_link_target(path: Path) -> str | None:
    try:
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError):
        return None


def validate_non_network_path(path: Path, *, label: str) -> None:
    text = str(path)
    if text.startswith(("\\\\", "//")):
        raise ValueError(f"{label} kan ikke ligge på en UNC- eller nettverkssti: {path}")
    if os.name != "nt":
        return
    try:
        import ctypes

        resolved = path.resolve(strict=False)
        root = resolved.anchor
        if root and ctypes.windll.kernel32.GetDriveTypeW(root) == 4:  # type: ignore[attr-defined]
            raise ValueError(f"{label} kan ikke ligge på en nettverksdisk: {path}")
    except OSError:
        return


def repository_file_size_limit(path: Path) -> int | None:
    filesystem = windows_filesystem_type(path)
    if filesystem in {"FAT", "FAT32"}:
        return FAT_MAX_FILE_SIZE
    return None


def windows_filesystem_type(path: Path) -> str | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        volume_path = ctypes.create_unicode_buffer(261)
        if not kernel32.GetVolumePathNameW(str(path.resolve(strict=False)), volume_path, len(volume_path)):
            return None
        filesystem_name = ctypes.create_unicode_buffer(261)
        if not kernel32.GetVolumeInformationW(
            volume_path.value,
            None,
            0,
            None,
            None,
            None,
            filesystem_name,
            len(filesystem_name),
        ):
            return None
        return filesystem_name.value.upper()
    except OSError:
        return None


def normalized_path_text(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.expanduser().resolve(strict=False))))


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
