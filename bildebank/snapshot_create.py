from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import FaceRecognitionConfig
from .db import DB_FILENAME
from .snapshot import (
    MainDatabaseSourceError,
    REPOSITORY_LOCK_FILENAME,
    REPOSITORY_METADATA_FILENAME,
    RepositoryBindingChange,
    current_machine_name,
    inventory_tree,
    read_main_database,
    read_repository_metadata,
    repository_binding_change,
    validate_existing_path_components,
    validate_non_network_path,
    validate_regular_file_without_links,
    validate_repository_location,
    validate_repository_metadata,
    validate_repository_metadata_for_read,
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
    canonical_json_bytes,
    create_staging_run,
    fsync_directory,
    initialize_repository,
    publish_snapshot,
    replace_durable_file,
    utc_timestamp,
)
from .snapshot_progress import (
    SnapshotCancelCallback,
    SnapshotCreateProgress,
    SnapshotCreateProgressCallback,
    raise_if_snapshot_cancelled,
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
    confirmed_binding_change: RepositoryBindingChange | None = None,
    progress: SnapshotCreateProgressCallback | None = None,
    should_cancel: SnapshotCancelCallback | None = None,
) -> SnapshotCreationResult:
    raise_if_snapshot_cancelled(should_cancel)
    clean_note = validate_snapshot_note(note)
    source = source_dir.resolve()
    validate_source_collection(source, allow_missing_main=True)
    repository = prepare_repository_path(
        source,
        repository_arg,
        allow_binding_change=confirmed_binding_change is not None,
        require_initialized=confirmed_binding_change is not None,
    )
    started_at = utc_timestamp()

    with RepositoryLock(repository, command="snapshot create"):
        inspection = inspect_repository(
            repository,
            source,
            allow_binding_change=confirmed_binding_change is not None,
        )
        with TargetLock(source, command="snapshot create"):
            if progress is not None:
                progress(SnapshotCreateProgress(stage="inventory"))
            inventory = inventory_tree(source, should_cancel=should_cancel)
            if progress is not None:
                progress(
                    SnapshotCreateProgress(
                        stage="inventory",
                        completed_objects=len(inventory),
                        total_objects=len(inventory),
                        completed_bytes=sum(file.size_bytes for file in inventory),
                        total_bytes=sum(file.size_bytes for file in inventory),
                    )
                )
            build_database_catalog(
                source,
                repository,
                inventory,
                face_config=face_config,
                allow_missing_main=True,
                should_cancel=should_cancel,
            )

            database_error: str | None
            try:
                collection_id, _database_rows = read_main_database(
                    source,
                    should_cancel=should_cancel,
                )
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
                raise_if_snapshot_cancelled(should_cancel)
                if inspection.initialized:
                    assert inspection.metadata is not None
                    confirm_or_validate_repository_binding(
                        repository,
                        inspection.metadata,
                        source,
                        collection_id,
                        confirmed_binding_change=confirmed_binding_change,
                    )
                    repository_initialized = False
                else:
                    if confirmed_binding_change is not None:
                        raise SnapshotStorageError(
                            "Flyttebekreftelse kan bare brukes med et allerede "
                            "initialisert repository."
                        )
                    initialize_repository(repository, source, collection_id)
                    repository_initialized = True
                build_mode = "normal"
                database_error = None

            raise_if_snapshot_cancelled(should_cancel)
            staging = create_staging_run(repository)
            if build_mode == "recovery":
                assert database_error is not None
                build = build_recovery_snapshot(
                    source,
                    repository,
                    staging,
                    database_error=database_error,
                    face_config=face_config,
                    progress=progress,
                    should_cancel=should_cancel,
                )
            else:
                try:
                    build = build_normal_snapshot(
                        source,
                        repository,
                        staging,
                        face_config=face_config,
                        progress=progress,
                        should_cancel=should_cancel,
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
                        progress=progress,
                        should_cancel=should_cancel,
                    )

            raise_if_snapshot_cancelled(should_cancel)
            if progress is not None:
                progress(SnapshotCreateProgress(stage="publish"))
            published = publish_built_snapshot(
                repository,
                staging,
                build,
                started_at=started_at,
                completed_at=utc_timestamp(),
                note=clean_note,
                should_cancel=should_cancel,
            )
            if progress is not None:
                progress(
                    SnapshotCreateProgress(
                        stage="publish",
                        completed_objects=1,
                        total_objects=1,
                    )
                )
    return SnapshotCreationResult(
        repository=repository,
        repository_initialized=repository_initialized,
        build=build,
        published=published,
    )


def prepare_repository_path(
    source: Path,
    repository_arg: Path,
    *,
    allow_binding_change: bool = False,
    require_initialized: bool = False,
) -> Path:
    repository_input = repository_arg.expanduser()
    validate_non_network_path(repository_input, label="Repositoryet")
    validate_existing_path_components(repository_input)
    repository = repository_input.resolve()
    validate_repository_location(source, repository)

    if not repository.exists():
        if require_initialized:
            raise SnapshotStorageError(
                "Flyttebekreftelse kan bare brukes med et allerede initialisert repository."
            )
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
    inspection = inspect_repository(
        repository,
        source,
        allow_binding_change=allow_binding_change,
    )
    if require_initialized and not inspection.initialized:
        raise SnapshotStorageError(
            "Flyttebekreftelse kan bare brukes med et allerede initialisert repository."
        )
    return repository


def validate_existing_recovery_repository(source_dir: Path, repository_arg: Path) -> Path:
    source = source_dir.resolve()
    validate_source_collection(source, allow_missing_main=True)
    repository_input = repository_arg.expanduser()
    validate_non_network_path(repository_input, label="Repositoryet")
    validate_existing_path_components(repository_input)
    repository = repository_input.resolve()
    validate_repository_location(source, repository)
    if not repository.is_dir():
        raise SnapshotStorageError(
            "Recovery krever et allerede initialisert repository. "
            f"Repositoryet finnes ikke som mappe: {repository}"
        )
    lock_path = repository / REPOSITORY_LOCK_FILENAME
    if lock_path.exists() or lock_path.is_symlink():
        raise RepositoryLockError(
            "Repositoryet er låst av en annen snapshot-operasjon. "
            f"Recovery-forhåndskontrollen har ikke gjort endringer: {lock_path}"
        )
    inspection = inspect_repository(repository, source)
    if not inspection.initialized or inspection.metadata is None:
        raise SnapshotStorageError(
            "Recovery krever et allerede initialisert repository som tidligere er "
            "bekreftet for denne bildesamlingen."
        )
    validate_recovery_identity_before_staging(source, inspection.metadata)
    return repository


def inspect_repository(
    repository: Path,
    source: Path,
    *,
    allow_binding_change: bool = False,
) -> RepositoryInspection:
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
        if allow_binding_change:
            validate_repository_metadata_for_read(metadata)
        else:
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
    try:
        validate_repository_metadata(metadata, source, stored_collection_id)
    except ValueError as exc:
        raise SnapshotStorageError(str(exc)) from exc
    readable_collection_id = read_damaged_database_collection_id(source / DB_FILENAME)
    if readable_collection_id is not None and readable_collection_id != stored_collection_id:
        raise SnapshotStorageError(
            "Den skadede hoveddatabasen har en annen collection_id enn repositoryet."
        )


def confirm_or_validate_repository_binding(
    repository: Path,
    metadata: dict[str, object],
    source: Path,
    collection_id: str,
    *,
    confirmed_binding_change: RepositoryBindingChange | None,
) -> None:
    try:
        actual_change = repository_binding_change(metadata, source, collection_id)
    except ValueError as exc:
        raise SnapshotStorageError(str(exc)) from exc

    if actual_change is None:
        if confirmed_binding_change is not None:
            raise SnapshotStorageError(
                "Flyttebekreftelse ble angitt, men bildesamlingen har ikke flyttet "
                "seg siden sist bekreftede snapshot."
            )
        return
    if confirmed_binding_change is None:
        raise SnapshotStorageError(
            "Repositoryet er sist brukt med en annen samlingssti eller på en annen "
            "maskin. Flytting av en samling krever eksplisitt bekreftelse."
        )
    if actual_change != confirmed_binding_change:
        raise SnapshotStorageError(
            "Flyttebekreftelsen er utdatert fordi gammelt eller nytt arbeidssted "
            "har endret seg. Kjør en ny skrivefri snapshot-plan."
        )

    updated_metadata = dict(metadata)
    updated_metadata["last_confirmed_source"] = {
        "collection_path": str(source.resolve()),
        "confirmed_at": utc_timestamp(),
        "machine_name": current_machine_name(),
    }
    try:
        validate_repository_metadata(updated_metadata, source, collection_id)
    except ValueError as exc:
        raise SnapshotStorageError(
            f"Den oppdaterte repositorybindingen er ugyldig: {exc}"
        ) from exc
    metadata_path = repository / REPOSITORY_METADATA_FILENAME
    validate_regular_file_without_links(metadata_path, label="Repositorymetadata")
    replace_durable_file(metadata_path, canonical_json_bytes(updated_metadata))


def publish_built_snapshot(
    repository: Path,
    staging: Path,
    build: SnapshotBuildResult,
    *,
    started_at: str,
    completed_at: str,
    note: str | None,
    should_cancel: SnapshotCancelCallback | None = None,
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
        should_cancel=should_cancel,
    )
