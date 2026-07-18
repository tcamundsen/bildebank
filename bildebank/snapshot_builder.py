from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import uuid

from . import db
from .config import FaceRecognitionConfig
from .face import LEGACY_FACE_DB_FILENAME, LEGACY_FACE_DB_MODEL_NAME
from .openclip import OPENCLIP_DB_FILENAME
from .snapshot import (
    DatabaseFileRow,
    InventoryFile,
    MainDatabaseSourceError,
    is_relative_to,
    is_migration_backup_file,
    is_sqlite_database_file,
    portable_path_key,
    read_main_database,
    read_repository_metadata,
    snapshot_exclusion_reason,
    snapshot_object_path,
    sqlite_database_path_for_side_file,
    inventory_tree,
    validate_existing_path_components,
    validate_non_network_path,
    validate_repository_metadata,
    validate_regular_file_without_links,
)
from .snapshot_repository import (
    ExpectedFile,
    RepositoryLockError,
    SnapshotDatabaseRecord,
    SnapshotFileRecord,
    SnapshotStorageError,
    StoredObject,
    SourceDatabaseError,
    SourceFileChangedError,
    SourceFileError,
    SourceFileUnreadableError,
    backup_sqlite_database,
    store_verified_file,
)
from .target_lock import LOCK_FILENAME


class SnapshotRecoveryRequiredError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatabaseSource:
    role: str
    source_path: Path
    source_path_display: str
    restore_path: str
    required: bool
    regenerable: bool
    model_name: str | None


@dataclass(frozen=True)
class SnapshotBuildResult:
    collection_id: str
    repository_id: str
    status: str
    collection_identity_source: str
    files: tuple[SnapshotFileRecord, ...]
    databases: tuple[SnapshotDatabaseRecord, ...]
    schema_versions: dict[str, int | None]
    exclusions: tuple[str, ...]
    warnings: tuple[str, ...]


def build_normal_snapshot(
    source: Path,
    repository: Path,
    staging: Path,
    *,
    face_config: FaceRecognitionConfig | None = None,
) -> SnapshotBuildResult:
    source = source.resolve()
    require_active_target_lock(source)
    try:
        collection_id, database_rows = read_main_database(source)
    except MainDatabaseSourceError as exc:
        raise SnapshotRecoveryRequiredError(str(exc)) from exc
    metadata = read_repository_metadata(repository / ".bildebank-backup-repository.json")
    if metadata.get("collection_id") != collection_id:
        raise SnapshotStorageError("Bildesamlingens collection_id stemmer ikke med repositoryet.")
    repository_id = metadata.get("repository_id")
    if not isinstance(repository_id, str):
        raise SnapshotStorageError("Repositorymetadata mangler gyldig repository_id.")

    inventory = inventory_tree(source)
    database_sources = build_database_catalog(source, repository, inventory, face_config=face_config)
    candidates, exclusions = classify_collection_files(inventory)
    file_records, file_warnings = build_file_records(
        repository,
        staging,
        database_rows=database_rows,
        candidates=candidates,
    )
    database_records, raw_database_files, schema_versions, database_warnings = capture_databases(
        repository,
        staging,
        database_sources,
    )
    all_file_records = (*file_records, *raw_database_files)
    has_deviations = any(record.integrity_status != "ok" for record in all_file_records) or any(
        record.status != "ok" for record in database_records
    )
    status = "degraded" if has_deviations else "complete"
    return SnapshotBuildResult(
        collection_id=collection_id,
        repository_id=repository_id,
        status=status,
        collection_identity_source="database",
        files=all_file_records,
        databases=database_records,
        schema_versions=schema_versions,
        exclusions=exclusions,
        warnings=tuple(sorted({*file_warnings, *database_warnings})),
    )


def build_recovery_snapshot(
    source: Path,
    repository: Path,
    staging: Path,
    *,
    database_error: str,
    face_config: FaceRecognitionConfig | None = None,
) -> SnapshotBuildResult:
    source = source.resolve()
    require_active_target_lock(source)
    metadata = read_repository_metadata(repository / ".bildebank-backup-repository.json")
    stored_collection_id = metadata.get("collection_id")
    if not isinstance(stored_collection_id, str):
        raise SnapshotStorageError("Repositorymetadata mangler gyldig collection_id.")
    try:
        validate_repository_metadata(metadata, source, stored_collection_id)
    except ValueError as exc:
        raise SnapshotStorageError(f"Recovery krever en bekreftet repositorybinding: {exc}") from exc
    repository_id = metadata.get("repository_id")
    if not isinstance(repository_id, str):
        raise SnapshotStorageError("Repositorymetadata mangler gyldig repository_id.")

    readable_collection_id = read_damaged_database_collection_id(source / db.DB_FILENAME)
    if readable_collection_id is not None and readable_collection_id != stored_collection_id:
        raise SnapshotStorageError(
            "Den skadede hoveddatabasen har en annen collection_id enn repositoryet."
        )

    inventory = inventory_tree(source)
    database_sources = build_database_catalog(
        source,
        repository,
        inventory,
        face_config=face_config,
        allow_missing_main=True,
    )
    candidates, exclusions = classify_recovery_files(inventory, database_sources)
    file_records, file_warnings = build_file_records(
        repository,
        staging,
        database_rows=(),
        candidates=candidates,
    )
    database_records, raw_database_files, schema_versions, database_warnings = capture_raw_databases(
        repository,
        staging,
        database_sources,
    )
    return SnapshotBuildResult(
        collection_id=stored_collection_id,
        repository_id=repository_id,
        status="recovery",
        collection_identity_source="repository",
        files=(*file_records, *raw_database_files),
        databases=database_records,
        schema_versions=schema_versions,
        exclusions=exclusions,
        warnings=tuple(
            sorted(
                {
                    *file_warnings,
                    *database_warnings,
                    f"Hoveddatabasen kunne ikke sikres som en konsistent SQLite-kopi: {database_error}",
                }
            )
        ),
    )


def read_damaged_database_collection_id(database_path: Path) -> str | None:
    try:
        connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'collection_id'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if row is None or not isinstance(row[0], str):
        return None
    try:
        return str(uuid.UUID(row[0]))
    except ValueError:
        return None


def require_active_target_lock(source: Path) -> None:
    lock_path = source / LOCK_FILENAME
    if not lock_path.exists():
        raise RepositoryLockError("Snapshotbygging krever at bildesamlingens target-lås holdes.")
    validate_regular_file_without_links(lock_path, label="Target-lås")


def classify_collection_files(
    inventory: tuple[InventoryFile, ...],
) -> tuple[tuple[InventoryFile, ...], tuple[str, ...]]:
    database_paths = {
        file.relative_path
        for file in inventory
        if is_sqlite_database_file(file.relative_path)
    }
    excluded_counts: Counter[str] = Counter()
    candidates: list[InventoryFile] = []
    for file in inventory:
        exclusion_reason = snapshot_exclusion_reason(file.relative_path)
        if exclusion_reason is not None:
            excluded_counts[exclusion_reason] += 1
        elif is_sqlite_database_file(file.relative_path):
            continue
        elif sqlite_database_path_for_side_file(file.relative_path) in database_paths:
            continue
        else:
            candidates.append(file)
    exclusions = tuple(f"{reason}: {count} fil(er)" for reason, count in sorted(excluded_counts.items()))
    return tuple(candidates), exclusions


def classify_recovery_files(
    inventory: tuple[InventoryFile, ...],
    database_sources: tuple[DatabaseSource, ...],
) -> tuple[tuple[InventoryFile, ...], tuple[str, ...]]:
    raw_database_paths = {
        candidate
        for source in database_sources
        for candidate in raw_database_source_paths(source.source_path)
    }
    excluded_counts: Counter[str] = Counter()
    candidates: list[InventoryFile] = []
    for file in inventory:
        exclusion_reason = snapshot_exclusion_reason(file.relative_path)
        if exclusion_reason is not None:
            excluded_counts[exclusion_reason] += 1
        elif file.absolute_path in raw_database_paths:
            continue
        else:
            candidates.append(file)
    exclusions = tuple(f"{reason}: {count} fil(er)" for reason, count in sorted(excluded_counts.items()))
    return tuple(candidates), exclusions


def build_file_records(
    repository: Path,
    staging: Path,
    *,
    database_rows: tuple[DatabaseFileRow, ...],
    candidates: tuple[InventoryFile, ...],
) -> tuple[tuple[SnapshotFileRecord, ...], tuple[str, ...]]:
    candidate_by_path = {file.relative_path: file for file in candidates}
    database_paths = {row.target_path for row in database_rows}
    path_key_counts = Counter(
        key for file in candidates if (key := portable_path_key(file.relative_path)) is not None
    )
    warnings: list[str] = []
    records: list[SnapshotFileRecord] = []

    for row in database_rows:
        candidate = candidate_by_path.get(row.target_path)
        expected = ExpectedFile(row.sha256, row.size_bytes)
        path_key = portable_path_key(row.target_path)
        unsafe_path = path_key is None or (path_key is not None and path_key_counts[path_key] > 1)
        restore_kind = "recovery_only" if unsafe_path else "normal"
        restore_path = None if unsafe_path else row.target_path
        if candidate is None:
            integrity_status = "unsafe_path" if unsafe_path else "missing"
            records.append(
                SnapshotFileRecord(
                    path=restore_path,
                    original_path_display=row.target_path,
                    restore_kind=restore_kind,
                    integrity_status=integrity_status,
                    expected=expected,
                    object=None,
                    mtime_ns=None,
                )
            )
            warnings.append(f"Databaseført fil mangler eller har utrygg sti: {row.target_path}")
            continue

        try:
            stored = store_verified_file(repository, staging, candidate.absolute_path)
        except SourceFileChangedError:
            raise
        except SourceFileUnreadableError:
            records.append(
                SnapshotFileRecord(
                    path=restore_path,
                    original_path_display=row.target_path,
                    restore_kind=restore_kind,
                    integrity_status="unreadable",
                    expected=expected,
                    object=None,
                    mtime_ns=candidate.mtime_ns,
                )
            )
            warnings.append(f"Databaseført fil kunne ikke leses: {row.target_path}")
            continue

        if stored.reference.size_bytes != row.size_bytes:
            integrity_status = "size_mismatch"
        elif stored.reference.sha256 != row.sha256:
            integrity_status = "hash_mismatch"
        elif unsafe_path:
            integrity_status = "unsafe_path"
        else:
            integrity_status = "ok"
        if integrity_status != "ok":
            warnings.append(f"Databaseført fil har avvik ({integrity_status}): {row.target_path}")
            warn_if_expected_object_missing(repository, row, warnings)
        records.append(
            SnapshotFileRecord(
                path=restore_path,
                original_path_display=row.target_path,
                restore_kind=restore_kind,
                integrity_status=integrity_status,
                expected=expected,
                object=stored.reference,
                mtime_ns=stored.source_mtime_ns,
            )
        )

    untracked = sorted(
        (file for file in candidates if file.relative_path not in database_paths),
        key=lambda file: file.relative_path,
    )
    for file in untracked:
        migration_backup = is_migration_backup_file(file.relative_path)
        path_key = portable_path_key(file.relative_path)
        unsafe_path = path_key is None or (path_key is not None and path_key_counts[path_key] > 1)
        unknown_stored: StoredObject | None = None
        last_error: SourceFileError | None = None
        for _attempt in range(2):
            try:
                unknown_stored = store_verified_file(repository, staging, file.absolute_path)
                break
            except SourceFileError as exc:
                last_error = exc
        if unknown_stored is None:
            integrity_status = (
                "changed_during_snapshot" if isinstance(last_error, SourceFileChangedError) else "unreadable"
            )
            if migration_backup:
                warnings.append(
                    "Bildebank-migreringsbackup kunne ikke sikres stabilt "
                    f"({integrity_status}): {file.relative_path}"
                )
            else:
                warnings.append(
                    f"Ukjent fil kunne ikke sikres stabilt ({integrity_status}): {file.relative_path}"
                )
            object_reference = None
            mtime_ns = file.mtime_ns
        else:
            integrity_status = "unsafe_path" if unsafe_path else "ok"
            object_reference = unknown_stored.reference
            mtime_ns = unknown_stored.source_mtime_ns
            if not migration_backup:
                warnings.append(f"Ukjent fil ble tatt med i snapshotet: {file.relative_path}")
        records.append(
            SnapshotFileRecord(
                path=None if unsafe_path else file.relative_path,
                original_path_display=file.relative_path,
                restore_kind="recovery_only" if unsafe_path else "normal",
                integrity_status=integrity_status,
                expected=None,
                object=object_reference,
                mtime_ns=mtime_ns,
            )
        )
    return tuple(records), tuple(warnings)


def warn_if_expected_object_missing(
    repository: Path,
    row: DatabaseFileRow,
    warnings: list[str],
) -> None:
    expected_path = snapshot_object_path(repository, row.sha256, row.size_bytes)
    if not expected_path.exists() and not expected_path.is_symlink():
        warnings.append(f"Forventet tidligere variant finnes ikke i repositoryet: {row.target_path}")
        return
    validate_regular_file_without_links(expected_path, label="Forventet backupobjekt")
    if expected_path.stat().st_size != row.size_bytes:
        raise SnapshotStorageError(f"Forventet backupobjekt har feil størrelse: {expected_path}")


def build_database_catalog(
    source: Path,
    repository: Path,
    inventory: tuple[InventoryFile, ...],
    *,
    face_config: FaceRecognitionConfig | None,
    allow_missing_main: bool = False,
) -> tuple[DatabaseSource, ...]:
    face_dir = configured_face_database_dir(source, repository, face_config)
    sources: list[DatabaseSource] = []
    for file in inventory:
        if not is_sqlite_database_file(file.relative_path):
            continue
        relative_path = file.relative_path
        if relative_path == db.DB_FILENAME:
            sources.append(database_source("main", file, relative_path, required=True, regenerable=False))
        elif relative_path == OPENCLIP_DB_FILENAME:
            sources.append(database_source("openclip", file, relative_path, required=False, regenerable=True))
        elif relative_path == LEGACY_FACE_DB_FILENAME:
            sources.append(
                database_source(
                    f"face:{LEGACY_FACE_DB_MODEL_NAME}",
                    file,
                    relative_path,
                    required=False,
                    regenerable=False,
                    model_name=LEGACY_FACE_DB_MODEL_NAME,
                )
            )
        elif face_dir is not None and is_relative_to(file.absolute_path, face_dir):
            model_name = Path(relative_path).stem
            sources.append(
                database_source(
                    f"face:{model_name}",
                    file,
                    relative_path,
                    required=False,
                    regenerable=False,
                    model_name=model_name,
                )
            )
        else:
            sources.append(
                database_source(
                    f"auxiliary:{relative_path}",
                    file,
                    relative_path,
                    required=False,
                    regenerable=False,
                )
            )

    if (
        face_dir is not None
        and face_config is not None
        and face_config.database_dir.is_absolute()
        and not is_relative_to(face_dir, source)
    ):
        for file in inventory_tree(face_dir):
            if not is_sqlite_database_file(file.relative_path):
                continue
            model_name = Path(file.relative_path).stem
            restore_path = f".bildebank-faces/{Path(file.relative_path).name}"
            sources.append(
                database_source(
                    f"face:{model_name}",
                    file,
                    restore_path,
                    required=False,
                    regenerable=False,
                    model_name=model_name,
                    source_path_display=str(file.absolute_path),
                )
            )
    roles = [item.role for item in sources]
    if allow_missing_main and "main" not in roles:
        sources.append(
            DatabaseSource(
                role="main",
                source_path=source / db.DB_FILENAME,
                source_path_display=db.DB_FILENAME,
                restore_path=db.DB_FILENAME,
                required=True,
                regenerable=False,
                model_name=None,
            )
        )
        roles.append("main")
    if len(set(roles)) != len(roles):
        raise SnapshotStorageError("Databasekatalogen har flere databaser med samme rolle.")
    if roles.count("main") != 1:
        raise SnapshotRecoveryRequiredError("Hoveddatabasen mangler fra databasekatalogen.")
    return tuple(sorted(sources, key=lambda item: item.role))


def configured_face_database_dir(
    source: Path,
    repository: Path,
    face_config: FaceRecognitionConfig | None,
) -> Path | None:
    if face_config is None:
        return None
    configured = face_config.database_dir
    face_dir = configured.resolve() if configured.is_absolute() else (source / configured).resolve()
    validate_existing_path_components(face_dir)
    if configured.is_absolute():
        validate_non_network_path(face_dir, label="Absolutt face-databasekatalog")
        if is_relative_to(face_dir, repository) or is_relative_to(repository, face_dir):
            raise SnapshotStorageError("Absolutt face-databasekatalog og repository kan ikke ligge i hverandre.")
        if face_dir != source and is_relative_to(source, face_dir):
            raise SnapshotStorageError("Absolutt face-databasekatalog kan ikke være en overmappe til samlingen.")
    if not face_dir.exists():
        return None
    if not face_dir.is_dir():
        raise SnapshotStorageError(f"Face-databasekatalogen er ikke en mappe: {face_dir}")
    return face_dir


def database_source(
    role: str,
    file: InventoryFile,
    restore_path: str,
    *,
    required: bool,
    regenerable: bool,
    model_name: str | None = None,
    source_path_display: str | None = None,
) -> DatabaseSource:
    if portable_path_key(restore_path) is None:
        raise SnapshotStorageError(f"Databasen har utrygg restore-sti: {restore_path}")
    return DatabaseSource(
        role=role,
        source_path=file.absolute_path,
        source_path_display=source_path_display or file.relative_path,
        restore_path=restore_path,
        required=required,
        regenerable=regenerable,
        model_name=model_name,
    )


def capture_databases(
    repository: Path,
    staging: Path,
    sources: tuple[DatabaseSource, ...],
) -> tuple[
    tuple[SnapshotDatabaseRecord, ...],
    tuple[SnapshotFileRecord, ...],
    dict[str, int | None],
    tuple[str, ...],
]:
    records: list[SnapshotDatabaseRecord] = []
    raw_files: list[SnapshotFileRecord] = []
    schema_versions: dict[str, int | None] = {}
    warnings: list[str] = []
    for source in sources:
        try:
            backup = backup_sqlite_database(repository, staging, source.source_path)
        except SourceDatabaseError as exc:
            if source.role == "main":
                raise SnapshotRecoveryRequiredError(str(exc)) from exc
            database_record, database_raw_files = capture_raw_database(
                repository,
                staging,
                source,
            )
            records.append(database_record)
            raw_files.extend(database_raw_files)
            schema_versions[source.role] = None
            warnings.append(
                f"Tilleggsdatabasen ble bare sikret som rå redningsdata: "
                f"{source.source_path_display}: {exc}"
            )
            continue
        records.append(
            SnapshotDatabaseRecord(
                role=source.role,
                source_path_display=source.source_path_display,
                restore_path=source.restore_path,
                required=source.required,
                regenerable=source.regenerable,
                capture="sqlite_backup",
                status="ok",
                object=backup.object.reference,
                schema_version=backup.schema_version,
                model_name=source.model_name,
            )
        )
        schema_versions[source.role] = backup.schema_version
        if source.role.startswith("auxiliary:"):
            warnings.append(f"Ukjent SQLite-database ble tatt med: {source.source_path_display}")
    return tuple(records), tuple(raw_files), schema_versions, tuple(warnings)


def capture_raw_databases(
    repository: Path,
    staging: Path,
    sources: tuple[DatabaseSource, ...],
) -> tuple[
    tuple[SnapshotDatabaseRecord, ...],
    tuple[SnapshotFileRecord, ...],
    dict[str, int | None],
    tuple[str, ...],
]:
    records: list[SnapshotDatabaseRecord] = []
    raw_files: list[SnapshotFileRecord] = []
    schema_versions: dict[str, int | None] = {}
    warnings: list[str] = []
    for source in sources:
        database_record, database_raw_files = capture_raw_database(repository, staging, source)
        records.append(database_record)
        raw_files.extend(database_raw_files)
        schema_versions[source.role] = None
        if database_record.object is None:
            warnings.append(f"Rå databasefil kunne ikke leses: {source.source_path_display}")
        else:
            warnings.append(f"Database ble sikret som rå redningsdata: {source.source_path_display}")
    return tuple(records), tuple(raw_files), schema_versions, tuple(warnings)


def capture_raw_database(
    repository: Path,
    staging: Path,
    source: DatabaseSource,
) -> tuple[SnapshotDatabaseRecord, tuple[SnapshotFileRecord, ...]]:
    file_records: list[SnapshotFileRecord] = []
    primary_object = None
    for index, raw_path in enumerate(raw_database_source_paths(source.source_path)):
        if index > 0 and not raw_path.exists() and not raw_path.is_symlink():
            continue
        source_path_display = raw_database_path_display(source, raw_path)
        try:
            stored = store_verified_file(repository, staging, raw_path)
        except SourceFileError:
            object_reference = None
            mtime_ns = None
        else:
            object_reference = stored.reference
            mtime_ns = stored.source_mtime_ns
        if index == 0:
            primary_object = object_reference
        file_records.append(
            SnapshotFileRecord(
                path=None,
                original_path_display=source_path_display,
                restore_kind="recovery_only",
                integrity_status="database_backup_failed",
                expected=None,
                object=object_reference,
                mtime_ns=mtime_ns,
                record_type="database_raw",
            )
        )
    return (
        SnapshotDatabaseRecord(
            role=source.role,
            source_path_display=source.source_path_display,
            restore_path=None,
            required=source.required,
            regenerable=source.regenerable,
            capture="raw_recovery",
            status="backup_failed" if primary_object is not None else "unreadable",
            object=primary_object,
            schema_version=None,
            model_name=source.model_name,
        ),
        tuple(file_records),
    )


def raw_database_source_paths(database_path: Path) -> tuple[Path, ...]:
    return (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
        Path(f"{database_path}-journal"),
    )


def raw_database_path_display(source: DatabaseSource, raw_path: Path) -> str:
    suffix = str(raw_path)[len(str(source.source_path)) :]
    return f"{source.source_path_display}{suffix}"
