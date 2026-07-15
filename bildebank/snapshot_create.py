from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import FaceRecognitionConfig
from .db import DB_FILENAME
from .snapshot import (
    MainDatabaseSourceError,
    REPOSITORY_LOCK_FILENAME,
    REPOSITORY_METADATA_FILENAME,
    inventory_tree,
    read_main_database,
    read_repository_metadata,
    validate_existing_path_components,
    validate_non_network_path,
    validate_regular_file_without_links,
    validate_repository_location,
    validate_repository_metadata,
    validate_repository_root_entries,
    validate_snapshot_note,
    validate_source_collection,
)
from .snapshot_builder import (
    SnapshotBuildResult,
    SnapshotRecoveryRequiredError,
    build_database_catalog,
    build_normal_snapshot,
    build_recovery_snapshot,
    read_damaged_database_collection_id,
)
from .snapshot_repository import (
    PublishedSnapshot,
    RepositoryLock,
    RepositoryLockError,
    SnapshotStorageError,
    create_staging_run,
    fsync_directory,
    initialize_repository,
    publish_snapshot,
    utc_timestamp,
)
from .target_lock import TargetLock


@dataclass(frozen=True)
class SnapshotCreationResult:
    repository: Path
    repository_initialized: bool
    build: SnapshotBuildResult
    published: PublishedSnapshot

    @property
    def status(self) -> str:
        return self.published.status

    @property
    def exit_code(self) -> int:
        return {"complete": 0, "degraded": 3, "recovery": 4}[self.status]


@dataclass(frozen=True)
class RepositoryInspection:
    initialized: bool
    metadata: dict[str, object] | None


def create_snapshot(
    source_dir: Path,
    repository_arg: Path,
    *,
    face_config: FaceRecognitionConfig | None = None,
    note: str | None = None,
) -> SnapshotCreationResult:
    clean_note = validate_snapshot_note(note)
    source = source_dir.resolve()
    validate_source_collection(source)
    repository = prepare_repository_path(source, repository_arg)
    started_at = utc_timestamp()

    with RepositoryLock(repository, command="snapshot create"):
        inspection = inspect_repository(repository, source)
        with TargetLock(source, command="snapshot create"):
            inventory = inventory_tree(source)
            build_database_catalog(
                source,
                repository,
                inventory,
                face_config=face_config,
                allow_missing_main=True,
            )

            database_error: str | None
            try:
                collection_id, _database_rows = read_main_database(source)
            except MainDatabaseSourceError as exc:
                if not inspection.initialized or inspection.metadata is None:
                    raise SnapshotStorageError(
                        "Recovery krever et allerede initialisert repository som tidligere er "
                        "bekreftet for denne bildesamlingen. Repositoryet er ikke initialisert."
                    ) from exc
                validate_recovery_identity_before_staging(source, inspection.metadata)
                build_mode = "recovery"
                database_error = str(exc)
                repository_initialized = False
            else:
                if inspection.initialized:
                    assert inspection.metadata is not None
                    validate_repository_metadata(inspection.metadata, source, collection_id)
                    repository_initialized = False
                else:
                    initialize_repository(repository, source, collection_id)
                    repository_initialized = True
                build_mode = "normal"
                database_error = None

            staging = create_staging_run(repository)
            if build_mode == "recovery":
                assert database_error is not None
                build = build_recovery_snapshot(
                    source,
                    repository,
                    staging,
                    database_error=database_error,
                    face_config=face_config,
                )
            else:
                try:
                    build = build_normal_snapshot(
                        source,
                        repository,
                        staging,
                        face_config=face_config,
                    )
                except SnapshotRecoveryRequiredError as exc:
                    if repository_initialized:
                        raise SnapshotStorageError(
                            "Hoveddatabasen feilet etter at et nytt repository ble initialisert. "
                            "Kjøringen avbrytes uten recovery-snapshot."
                        ) from exc
                    build = build_recovery_snapshot(
                        source,
                        repository,
                        staging,
                        database_error=str(exc),
                        face_config=face_config,
                    )

            published = publish_built_snapshot(
                repository,
                staging,
                build,
                started_at=started_at,
                completed_at=utc_timestamp(),
                note=clean_note,
            )
    return SnapshotCreationResult(
        repository=repository,
        repository_initialized=repository_initialized,
        build=build,
        published=published,
    )


def prepare_repository_path(source: Path, repository_arg: Path) -> Path:
    repository_input = repository_arg.expanduser()
    validate_non_network_path(repository_input, label="Repositoryet")
    validate_existing_path_components(repository_input)
    repository = repository_input.resolve()
    validate_repository_location(source, repository)

    if not repository.exists():
        parent = repository.parent
        if not parent.exists():
            raise ValueError(
                "Foreldremappen til repositoryet finnes ikke:\n"
                f"\n  {parent}\n"
                "\nOpprett foreldremappen først."
            )
        if not parent.is_dir():
            raise ValueError(f"Forelderen til repositoryet er ikke en mappe: {parent}")
        try:
            repository.mkdir()
        except FileExistsError:
            pass
        else:
            fsync_directory(parent)

    validate_existing_path_components(repository)
    if not repository.is_dir():
        raise ValueError(f"Repositoryplasseringen finnes, men er ikke en mappe: {repository}")
    inspect_repository(repository, source)
    return repository


def inspect_repository(repository: Path, source: Path) -> RepositoryInspection:
    lock_path = repository / REPOSITORY_LOCK_FILENAME
    if lock_path.is_symlink():
        raise RepositoryLockError(f"Repositorylåsen kan ikke være en lenke: {lock_path}")

    entries = list(repository.iterdir())
    entries_without_lock = [entry for entry in entries if entry != lock_path]
    metadata_path = repository / REPOSITORY_METADATA_FILENAME
    if not metadata_path.exists() and not metadata_path.is_symlink():
        if entries_without_lock:
            raise SnapshotStorageError(
                "Repositorymappen er ikke tom og mangler gyldig Bildebank-metadata: "
                f"{repository}"
            )
        return RepositoryInspection(initialized=False, metadata=None)

    validate_regular_file_without_links(metadata_path, label="Repositorymetadata")
    metadata = read_repository_metadata(metadata_path)
    stored_collection_id = metadata.get("collection_id")
    if not isinstance(stored_collection_id, str):
        raise SnapshotStorageError("Repositorymetadata mangler gyldig collection_id.")
    try:
        validate_repository_metadata(metadata, source, stored_collection_id)
        validate_repository_root_entries(repository, entries)
    except ValueError as exc:
        raise SnapshotStorageError(str(exc)) from exc
    return RepositoryInspection(initialized=True, metadata=metadata)


def validate_recovery_identity_before_staging(
    source: Path,
    metadata: dict[str, object],
) -> None:
    stored_collection_id = metadata.get("collection_id")
    if not isinstance(stored_collection_id, str):
        raise SnapshotStorageError("Repositorymetadata mangler gyldig collection_id.")
    readable_collection_id = read_damaged_database_collection_id(source / DB_FILENAME)
    if readable_collection_id is not None and readable_collection_id != stored_collection_id:
        raise SnapshotStorageError(
            "Den skadede hoveddatabasen har en annen collection_id enn repositoryet."
        )


def publish_built_snapshot(
    repository: Path,
    staging: Path,
    build: SnapshotBuildResult,
    *,
    started_at: str,
    completed_at: str,
    note: str | None,
) -> PublishedSnapshot:
    return publish_snapshot(
        repository,
        staging,
        collection_id=build.collection_id,
        repository_id=build.repository_id,
        status=build.status,
        collection_identity_source=build.collection_identity_source,
        started_at=started_at,
        completed_at=completed_at,
        files=build.files,
        databases=build.databases,
        schema_versions=build.schema_versions,
        exclusions=build.exclusions,
        warnings=build.warnings,
        note=note,
    )
