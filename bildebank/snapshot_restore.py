from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
import stat
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import db
from .snapshot import (
    is_relative_to,
    portable_path_key,
    repository_file_size_limit,
    snapshot_object_path,
    validate_canonical_uuid,
    validate_existing_path_components,
    validate_non_network_path,
    validate_regular_file_without_links,
)
from .snapshot_check import (
    SnapshotSummary,
    expected_file,
    hash_regular_file,
    object_reference,
    read_canonical_json,
    read_snapshot,
    resolve_repository_for_check,
    validate_locked_repository_for_check,
)
from .snapshot_repository import (
    COPY_CHUNK_SIZE,
    ObjectReference,
    RepositoryLock,
    SnapshotStorageError,
    canonical_json_bytes,
    fsync_directory,
    open_source_without_following_links,
    write_new_durable_file,
)


RECOVERY_REPORT_FILENAME = "BILDEBANK-RECOVERY-REPORT.txt"


@dataclass(frozen=True)
class RestoreEntry:
    entry_id: str
    path: str | None
    original_path_display: str
    recovery_name: str | None
    restore_kind: str
    integrity_status: str
    object: ObjectReference | None
    expected: ObjectReference | None
    mtime_ns: int | None
    record_type: str


@dataclass(frozen=True)
class RestoreOutput:
    relative_path: str
    object: ObjectReference
    mtime_ns: int | None
    entry_id: str | None
    original_path_display: str
    variant: str
    reason: str


@dataclass(frozen=True)
class FullRestorePlan:
    repository: Path
    snapshot: SnapshotSummary
    target: Path
    target_state: str
    recovery_target: Path | None
    collection_outputs: tuple[RestoreOutput, ...]
    recovery_outputs: tuple[RestoreOutput, ...]
    missing_expected_entries: tuple[str, ...]
    required_bytes: int
    free_bytes: int
    original_collection: Path
    original_collection_exists: bool
    note: str | None

    @property
    def incomplete(self) -> bool:
        return bool(self.missing_expected_entries)

    @property
    def has_estimated_capacity(self) -> bool:
        return self.required_bytes <= self.free_bytes


@dataclass(frozen=True)
class SingleFileRestorePlan:
    repository: Path
    snapshot: SnapshotSummary
    export_directory: Path
    export_state: str
    output: RestoreOutput
    output_path: Path
    note: str | None


@dataclass(frozen=True)
class SingleFileRestoreResult:
    plan: SingleFileRestorePlan
    output_path: Path
    exit_code: int


@dataclass(frozen=True)
class FullRestoreResult:
    plan: FullRestorePlan
    published_target: Path
    published_recovery: Path | None
    exit_code: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _LoadedSnapshot:
    repository: Path
    metadata: dict[str, object]
    summary: SnapshotSummary
    manifest: dict[str, object]
    entries: tuple[RestoreEntry, ...]


def plan_full_restore(
    repository_arg: Path,
    snapshot_id: str,
    target_arg: Path,
) -> FullRestorePlan:
    with locked_snapshot(repository_arg, snapshot_id, command="snapshot restore --dry-run") as loaded:
        return build_full_restore_plan(loaded, target_arg)


def build_full_restore_plan(loaded: _LoadedSnapshot, target_arg: Path) -> FullRestorePlan:
    if loaded.summary.status == "recovery":
        raise SnapshotStorageError(
            "Et recovery-snapshot kan ikke gjenopprettes som en vanlig bildesamling. "
            "Bruk restore-file for å hente ut redningsinnhold."
        )
    target, target_state = validate_full_restore_target(
        loaded.repository,
        loaded.metadata,
        target_arg,
    )
    collection_outputs: list[RestoreOutput] = []
    recovery_outputs: list[RestoreOutput] = []
    missing_expected: list[str] = []

    for entry in loaded.entries:
        if entry.restore_kind == "recovery_only":
            if entry.object is not None:
                require_available_object(loaded.repository, entry.object, required=True)
                assert entry.recovery_name is not None
                recovery_outputs.append(
                    RestoreOutput(
                        relative_path=entry.recovery_name,
                        object=entry.object,
                        mtime_ns=entry.mtime_ns,
                        entry_id=entry.entry_id,
                        original_path_display=entry.original_path_display,
                        variant="observed",
                        reason=entry.integrity_status,
                    )
                )
            continue

        assert entry.path is not None
        if entry.integrity_status == "ok":
            if entry.object is None:
                raise SnapshotStorageError(f"Gyldig filpost mangler objekt: {entry.entry_id}")
            require_available_object(loaded.repository, entry.object, required=True)
            collection_outputs.append(output_for_entry(entry, entry.path, entry.object, "normal"))
            continue

        expected_available = (
            entry.expected is not None
            and require_available_object(loaded.repository, entry.expected, required=False)
        )
        if expected_available:
            assert entry.expected is not None
            collection_outputs.append(output_for_entry(entry, entry.path, entry.expected, "expected"))
        else:
            missing_expected.append(entry.entry_id)

        if entry.object is not None and entry.object != entry.expected:
            require_available_object(loaded.repository, entry.object, required=True)
            recovery_outputs.append(
                output_for_entry(
                    entry,
                    observed_recovery_path(entry.path, entry.object.sha256),
                    entry.object,
                    "observed",
                )
            )

    databases = loaded.manifest["databases"]
    assert isinstance(databases, list)
    for database in databases:
        assert isinstance(database, dict)
        if database["capture"] != "sqlite_backup":
            continue
        reference = object_reference(database["object"], allow_none=False)
        assert reference is not None
        require_available_object(loaded.repository, reference, required=True)
        restore_path = database["restore_path"]
        assert isinstance(restore_path, str)
        collection_outputs.append(
            RestoreOutput(
                relative_path=restore_path,
                object=reference,
                mtime_ns=None,
                entry_id=None,
                original_path_display=str(database["source_path_display"]),
                variant="database",
                reason=str(database["role"]),
            )
        )

    validate_output_paths(collection_outputs, label="samlingsmappen")
    validate_output_paths(recovery_outputs, label="recovery-mappen")
    recovery_report_key = portable_path_key(RECOVERY_REPORT_FILENAME)
    if any(portable_path_key(output.relative_path) == recovery_report_key for output in recovery_outputs):
        raise SnapshotStorageError(
            f"En recovery-post kolliderer med den reserverte rapportfilen {RECOVERY_REPORT_FILENAME}."
        )
    recovery_target = recovery_path(target, loaded.summary) if recovery_outputs else None
    if recovery_target is not None and (recovery_target.exists() or recovery_target.is_symlink()):
        raise SnapshotStorageError(
            f"Recovery-målet finnes allerede og blir ikke overskrevet: {recovery_target}"
        )
    reject_restore_staging_remnants(target)
    required_bytes = sum(output.object.size_bytes for output in (*collection_outputs, *recovery_outputs))
    validate_restore_file_sizes(target.parent, (*collection_outputs, *recovery_outputs))
    free_bytes = shutil.disk_usage(target.parent).free
    last_source = loaded.metadata["last_confirmed_source"]
    assert isinstance(last_source, dict)
    original_collection = Path(str(last_source["collection_path"]))
    return FullRestorePlan(
        repository=loaded.repository,
        snapshot=loaded.summary,
        target=target,
        target_state=target_state,
        recovery_target=recovery_target,
        collection_outputs=tuple(sorted(collection_outputs, key=lambda item: item.relative_path)),
        recovery_outputs=tuple(sorted(recovery_outputs, key=lambda item: item.relative_path)),
        missing_expected_entries=tuple(sorted(missing_expected)),
        required_bytes=required_bytes,
        free_bytes=free_bytes,
        original_collection=original_collection,
        original_collection_exists=original_collection.is_dir(),
        note=loaded.summary.note,
    )


def restore_full_snapshot(
    repository_arg: Path,
    snapshot_id: str,
    target_arg: Path,
) -> FullRestoreResult:
    with locked_snapshot(repository_arg, snapshot_id, command="snapshot restore") as loaded:
        plan = build_full_restore_plan(loaded, target_arg)
        if not plan.has_estimated_capacity:
            raise SnapshotStorageError(
                "Målmediet har ikke nok ledig plass for det konservative restore-estimatet."
            )
        staging = create_restore_staging(plan)
        collection_staging = staging / "collection"
        recovery_staging = staging / "recovery"
        try:
            copy_restore_outputs(
                plan.repository,
                collection_staging,
                plan.collection_outputs,
            )
            if plan.recovery_outputs:
                copy_restore_outputs(
                    plan.repository,
                    recovery_staging,
                    plan.recovery_outputs,
                )
                write_recovery_report(recovery_staging, plan)
            validate_staged_restore(collection_staging, recovery_staging, plan, loaded.metadata)
            published_recovery = publish_staged_restore(
                staging,
                collection_staging,
                recovery_staging,
                plan,
            )
        except Exception as exc:
            raise SnapshotStorageError(
                "Restore feilet før samlingen ble publisert. "
                f"Staging er bevart for undersøkelse: {staging}: {exc}"
            ) from exc

        warnings = cleanup_published_restore_staging(staging)
        return FullRestoreResult(
            plan=plan,
            published_target=plan.target,
            published_recovery=published_recovery,
            exit_code=3 if plan.incomplete else 0,
            warnings=warnings,
        )


def validate_restore_file_sizes(parent: Path, outputs: tuple[RestoreOutput, ...]) -> None:
    size_limit = repository_file_size_limit(parent)
    if size_limit is None:
        return
    oversized = next((output for output in outputs if output.object.size_bytes > size_limit), None)
    if oversized is not None:
        raise SnapshotStorageError(
            "En restorefil er større enn målfilsystemets per-fil-grense: "
            f"{oversized.relative_path} ({oversized.object.size_bytes} byte)."
        )


def create_restore_staging(plan: FullRestorePlan, *, run_id: str | None = None) -> Path:
    canonical_run_id = str(uuid.UUID(run_id)) if run_id is not None else str(uuid.uuid4())
    if run_id is not None and canonical_run_id != run_id:
        raise SnapshotStorageError(f"Ikke-kanonisk restore run-id: {run_id!r}")
    staging = plan.target.parent / f".bildebank-restore-{plan.target.name}-{canonical_run_id}"
    try:
        staging.mkdir()
    except OSError as exc:
        raise SnapshotStorageError(f"Kunne ikke opprette restore-staging: {staging}: {exc}") from exc
    try:
        (staging / "collection").mkdir()
        if plan.recovery_outputs:
            (staging / "recovery").mkdir()
        write_new_durable_file(
            staging / "run.json",
            canonical_json_bytes(
                {
                    "recovery_target": str(plan.recovery_target) if plan.recovery_target else None,
                    "repository": str(plan.repository),
                    "run_id": canonical_run_id,
                    "snapshot_id": plan.snapshot.snapshot_id,
                    "target": str(plan.target),
                }
            ),
        )
        fsync_directory(staging)
        fsync_directory(staging.parent)
    except Exception as exc:
        raise SnapshotStorageError(
            f"Restore-staging ble bare delvis opprettet og er bevart: {staging}: {exc}"
        ) from exc
    return staging


def copy_restore_outputs(
    repository: Path,
    staging_root: Path,
    outputs: tuple[RestoreOutput, ...],
) -> None:
    for output in outputs:
        destination = staging_root.joinpath(*output.relative_path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy_verified_restore_object(repository, output, destination)


def copy_verified_restore_object(
    repository: Path,
    output: RestoreOutput,
    destination: Path,
) -> None:
    source = snapshot_object_path(
        repository,
        output.object.sha256,
        output.object.size_bytes,
    )
    require_available_object(repository, output.object, required=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4()}")
    source_fd = open_source_without_following_links(source)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise SnapshotStorageError(f"Snapshotobjektet er ikke en vanlig fil: {source}")
        with os.fdopen(source_fd, "rb", closefd=True) as source_stream, temporary.open("xb") as target:
            source_fd = -1
            while chunk := source_stream.read(COPY_CHUNK_SIZE):
                target.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            target.flush()
            os.fsync(target.fileno())
    finally:
        if source_fd >= 0:
            os.close(source_fd)
    if size_bytes != output.object.size_bytes or digest.hexdigest() != output.object.sha256:
        raise SnapshotStorageError(f"Snapshotobjektet besto ikke SHA-256-kontroll under kopiering: {source}")
    os.rename(temporary, destination)
    fsync_directory(destination.parent)
    digest_after, size_after = hash_regular_file(destination)
    if size_after != output.object.size_bytes or digest_after != output.object.sha256:
        raise SnapshotStorageError(f"Restorekopien besto ikke SHA-256-kontroll: {destination}")
    if output.mtime_ns is not None:
        os.utime(destination, ns=(output.mtime_ns, output.mtime_ns), follow_symlinks=False)


def write_recovery_report(recovery_staging: Path, plan: FullRestorePlan) -> None:
    lines = [
        "Bildebank recovery report",
        "",
        f"Snapshot-ID: {plan.snapshot.snapshot_id}",
        f"Snapshot status: {plan.snapshot.status}",
        "",
    ]
    for output in plan.recovery_outputs:
        lines.extend(
            (
                f"Entry-ID: {output.entry_id}",
                f"Recovery path: {output.relative_path}",
                f"Original display path: {output.original_path_display}",
                f"Reason: {output.reason}",
                f"Variant: {output.variant}",
                f"SHA-256: {output.object.sha256}",
                "",
            )
        )
    write_new_durable_file(
        recovery_staging / RECOVERY_REPORT_FILENAME,
        ("\n".join(lines) + "\n").encode("utf-8"),
    )


def validate_staged_restore(
    collection_staging: Path,
    recovery_staging: Path,
    plan: FullRestorePlan,
    metadata: dict[str, object],
) -> None:
    for output in plan.collection_outputs:
        path = collection_staging.joinpath(*output.relative_path.split("/"))
        validate_restored_file(path, output)
    for output in plan.recovery_outputs:
        path = recovery_staging.joinpath(*output.relative_path.split("/"))
        validate_restored_file(path, output)
    main_database = next(
        (output for output in plan.collection_outputs if output.variant == "database" and output.reason == "main"),
        None,
    )
    if main_database is None:
        raise SnapshotStorageError("Restoreplanen mangler hoveddatabasen.")
    database_path = collection_staging.joinpath(*main_database.relative_path.split("/"))
    validate_restored_main_database(database_path, str(metadata["collection_id"]))
    fsync_tree_directories(collection_staging)
    if plan.recovery_outputs:
        fsync_tree_directories(recovery_staging)


def validate_restored_file(path: Path, output: RestoreOutput) -> None:
    validate_regular_file_without_links(path, label="Gjenopprettet fil")
    digest, size_bytes = hash_regular_file(path)
    if size_bytes != output.object.size_bytes or digest != output.object.sha256:
        raise SnapshotStorageError(f"Gjenopprettet fil besto ikke sluttkontrollen: {path}")


def validate_restored_main_database(path: Path, expected_collection_id: str) -> None:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise SnapshotStorageError(f"Kunne ikke åpne gjenopprettet hoveddatabase: {path}: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        db.validate_database_health(connection)
        collection_id = db.validate_collection_id(connection)
    except (sqlite3.Error, ValueError) as exc:
        raise SnapshotStorageError(f"Gjenopprettet hoveddatabase besto ikke kontrollen: {path}: {exc}") from exc
    finally:
        connection.close()
    if collection_id != expected_collection_id:
        raise SnapshotStorageError("Gjenopprettet hoveddatabase har feil collection_id.")


def fsync_tree_directories(root: Path) -> None:
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        fsync_directory(directory)
    fsync_directory(root)


def publish_staged_restore(
    staging: Path,
    collection_staging: Path,
    recovery_staging: Path,
    plan: FullRestorePlan,
) -> Path | None:
    if plan.recovery_target is not None and (
        plan.recovery_target.exists() or plan.recovery_target.is_symlink()
    ):
        raise SnapshotStorageError(
            f"Recovery-målet ble opprettet etter planlegging og blir ikke overskrevet: {plan.recovery_target}"
        )
    published_recovery: Path | None = None
    if plan.recovery_target is not None:
        try:
            os.rename(recovery_staging, plan.recovery_target)
        except OSError as exc:
            raise SnapshotStorageError(
                f"Kunne ikke publisere recovery-mappen atomisk: {plan.recovery_target}: {exc}"
            ) from exc
        fsync_directory(plan.target.parent)
        published_recovery = plan.recovery_target

    prepare_collection_target_for_publish(plan)
    try:
        os.rename(collection_staging, plan.target)
    except OSError as exc:
        recovery_detail = (
            f" Recovery-mappen er allerede publisert: {published_recovery}."
            if published_recovery is not None
            else ""
        )
        raise SnapshotStorageError(
            f"Kunne ikke publisere den gjenopprettede samlingen atomisk: {plan.target}: {exc}."
            + recovery_detail
        ) from exc
    fsync_directory(plan.target.parent)
    return published_recovery


def prepare_collection_target_for_publish(plan: FullRestorePlan) -> None:
    if plan.target_state == "missing":
        if plan.target.exists() or plan.target.is_symlink():
            raise SnapshotStorageError(
                f"Målmappen ble opprettet etter planlegging og blir ikke overskrevet: {plan.target}"
            )
        return
    if plan.target.is_symlink() or not plan.target.is_dir():
        raise SnapshotStorageError(f"Den tomme målmappen ble erstattet under restore: {plan.target}")
    if any(plan.target.iterdir()):
        raise SnapshotStorageError(f"Målmappen er ikke lenger tom og blir ikke endret: {plan.target}")
    try:
        plan.target.rmdir()
    except OSError as exc:
        raise SnapshotStorageError(f"Kunne ikke klargjøre den fortsatt tomme målmappen: {plan.target}: {exc}") from exc
    fsync_directory(plan.target.parent)


def cleanup_published_restore_staging(staging: Path) -> tuple[str, ...]:
    warnings: list[str] = []
    try:
        (staging / "run.json").unlink()
        staging.rmdir()
        fsync_directory(staging.parent)
    except OSError as exc:
        warnings.append(
            f"Restore ble publisert, men tom staging kunne ikke ryddes: {staging}: {exc}"
        )
    return tuple(warnings)


def plan_single_file_restore(
    repository_arg: Path,
    snapshot_id: str,
    export_directory_arg: Path,
    *,
    path: str | None = None,
    entry_id: str | None = None,
    variant: str | None = None,
) -> SingleFileRestorePlan:
    if (path is None) == (entry_id is None):
        raise ValueError("Velg nøyaktig én av path og entry_id.")
    if variant not in {None, "expected", "observed"}:
        raise ValueError("Variant må være expected eller observed.")
    with locked_snapshot(repository_arg, snapshot_id, command="snapshot restore-file --dry-run") as loaded:
        return build_single_file_restore_plan(
            loaded,
            export_directory_arg,
            path=path,
            entry_id=entry_id,
            variant=variant,
        )


def build_single_file_restore_plan(
    loaded: _LoadedSnapshot,
    export_directory_arg: Path,
    *,
    path: str | None,
    entry_id: str | None,
    variant: str | None,
) -> SingleFileRestorePlan:
    entry = select_entry(loaded.entries, path=path, entry_id=entry_id)
    if entry.restore_kind == "recovery_only" and path is not None:
        raise SnapshotStorageError("recovery_only-poster kan bare velges med entry_id.")
    output = choose_single_file_output(loaded.repository, entry, variant=variant)
    export_directory, export_state = validate_export_directory(
        loaded.repository,
        loaded.metadata,
        export_directory_arg,
    )
    output_path = export_directory.joinpath(*output.relative_path.split("/"))
    validate_existing_path_components(output_path)
    if output_path.exists() or output_path.is_symlink():
        raise SnapshotStorageError(f"Eksportmålet finnes allerede og blir ikke overskrevet: {output_path}")
    size_check_root = export_directory if export_state == "existing" else export_directory.parent
    validate_restore_file_sizes(size_check_root, (output,))
    return SingleFileRestorePlan(
        repository=loaded.repository,
        snapshot=loaded.summary,
        export_directory=export_directory,
        export_state=export_state,
        output=output,
        output_path=output_path,
        note=loaded.summary.note,
    )


def restore_single_file(
    repository_arg: Path,
    snapshot_id: str,
    export_directory_arg: Path,
    *,
    path: str | None = None,
    entry_id: str | None = None,
    variant: str | None = None,
) -> SingleFileRestoreResult:
    if (path is None) == (entry_id is None):
        raise ValueError("Velg nøyaktig én av path og entry_id.")
    if variant not in {None, "expected", "observed"}:
        raise ValueError("Variant må være expected eller observed.")
    with locked_snapshot(repository_arg, snapshot_id, command="snapshot restore-file") as loaded:
        plan = build_single_file_restore_plan(
            loaded,
            export_directory_arg,
            path=path,
            entry_id=entry_id,
            variant=variant,
        )
        capacity_root = (
            plan.export_directory
            if plan.export_state == "existing"
            else plan.export_directory.parent
        )
        if shutil.disk_usage(capacity_root).free < plan.output.object.size_bytes:
            raise SnapshotStorageError("Eksportmediet har ikke nok ledig plass for den valgte filen.")
        try:
            prepare_single_file_destination(plan)
            copy_verified_restore_object_exclusive(
                plan.repository,
                plan.output,
                plan.output_path,
            )
        except Exception as exc:
            raise SnapshotStorageError(
                "Enkeltfil-restore feilet. Opprettede mapper og eventuell ufullstendig "
                f"utdatafil er bevart for undersøkelse: {plan.output_path}: {exc}"
            ) from exc
        return SingleFileRestoreResult(plan=plan, output_path=plan.output_path, exit_code=0)


def prepare_single_file_destination(plan: SingleFileRestorePlan) -> None:
    if plan.export_state == "missing":
        try:
            plan.export_directory.mkdir()
        except OSError as exc:
            raise SnapshotStorageError(
                f"Kunne ikke opprette eksportmappen uten overskriving: {plan.export_directory}: {exc}"
            ) from exc
        fsync_directory(plan.export_directory.parent)
    elif plan.export_directory.is_symlink() or not plan.export_directory.is_dir():
        raise SnapshotStorageError(
            f"Eksportmappen ble erstattet etter planlegging: {plan.export_directory}"
        )

    current = plan.export_directory
    relative_parent = Path(plan.output.relative_path).parent
    for part in relative_parent.parts:
        current /= part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise SnapshotStorageError(
                    f"En del av eksportstien er ikke en vanlig mappe uten lenke: {current}"
                )
            continue
        try:
            current.mkdir()
        except OSError as exc:
            raise SnapshotStorageError(f"Kunne ikke opprette eksportmappe: {current}: {exc}") from exc
        fsync_directory(current.parent)

    validate_existing_path_components(plan.output_path)
    if plan.output_path.exists() or plan.output_path.is_symlink():
        raise SnapshotStorageError(
            f"Eksportmålet ble opprettet etter planlegging og blir ikke overskrevet: {plan.output_path}"
        )


def copy_verified_restore_object_exclusive(
    repository: Path,
    output: RestoreOutput,
    destination: Path,
) -> None:
    source = snapshot_object_path(repository, output.object.sha256, output.object.size_bytes)
    require_available_object(repository, output.object, required=True)
    source_fd = open_source_without_following_links(source)
    destination_fd = -1
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise SnapshotStorageError(f"Snapshotobjektet er ikke en vanlig fil: {source}")
        try:
            destination_fd = os.open(destination, flags, 0o666)
        except FileExistsError as exc:
            raise SnapshotStorageError(
                f"Eksportmålet finnes allerede og blir ikke overskrevet: {destination}"
            ) from exc
        with ExitStack() as stack:
            source_stream = stack.enter_context(os.fdopen(source_fd, "rb", closefd=True))
            source_fd = -1
            target = stack.enter_context(os.fdopen(destination_fd, "wb", closefd=True))
            destination_fd = -1
            while chunk := source_stream.read(COPY_CHUNK_SIZE):
                target.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            target.flush()
            os.fsync(target.fileno())
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
    if size_bytes != output.object.size_bytes or digest.hexdigest() != output.object.sha256:
        raise SnapshotStorageError(
            f"Snapshotobjektet besto ikke SHA-256-kontroll under eksport: {source}"
        )
    validate_restored_file(destination, output)
    if output.mtime_ns is not None:
        os.utime(destination, ns=(output.mtime_ns, output.mtime_ns), follow_symlinks=False)
    fsync_directory(destination.parent)


@contextmanager
def locked_snapshot(
    repository_arg: Path,
    snapshot_id: str,
    *,
    command: str,
) -> Iterator[_LoadedSnapshot]:
    canonical_snapshot_id = validate_canonical_uuid(snapshot_id, label="snapshot_id")
    repository = resolve_repository_for_check(repository_arg)
    with RepositoryLock(repository, command=command):
        metadata = validate_locked_repository_for_check(repository)
        snapshot_path = find_snapshot_path(repository, canonical_snapshot_id)
        read = read_snapshot(
            snapshot_path,
            repository_id=str(metadata["repository_id"]),
            collection_id=str(metadata["collection_id"]),
        )
        manifest, _content = read_canonical_json(snapshot_path / "manifest.json", label="manifest.json")
        entries = load_restore_entries(snapshot_path / "files.jsonl")
        yield _LoadedSnapshot(repository, metadata, read.summary, manifest, entries)


def find_snapshot_path(repository: Path, snapshot_id: str) -> Path:
    matches = [
        path
        for path in (repository / "snapshots").iterdir()
        if path.name.endswith(f"-{snapshot_id}")
    ]
    if len(matches) != 1:
        raise SnapshotStorageError(
            f"Fant ikke nøyaktig ett publisert snapshot med ID {snapshot_id}."
        )
    path = matches[0]
    try:
        path_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SnapshotStorageError(f"Kunne ikke kontrollere snapshotmappen: {path}: {exc}") from exc
    if path.is_symlink() or not stat.S_ISDIR(path_stat.st_mode):
        raise SnapshotStorageError(f"Snapshotmålet er ikke en vanlig mappe uten lenke: {path}")
    return path


def load_restore_entries(path: Path) -> tuple[RestoreEntry, ...]:
    entries: list[RestoreEntry] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        for line in stream:
            raw = json.loads(line)
            expected = expected_file(raw["expected"])
            entries.append(
                RestoreEntry(
                    entry_id=raw["entry_id"],
                    path=raw["path"],
                    original_path_display=raw["original_path_display"],
                    recovery_name=raw["recovery_name"],
                    restore_kind=raw["restore_kind"],
                    integrity_status=raw["integrity_status"],
                    object=object_reference(raw["object"], allow_none=True),
                    expected=(
                        ObjectReference(expected.sha256, expected.size_bytes)
                        if expected is not None
                        else None
                    ),
                    mtime_ns=int(raw["mtime_ns"]) if raw["mtime_ns"] is not None else None,
                    record_type=raw["record_type"],
                )
            )
    return tuple(entries)


def validate_full_restore_target(
    repository: Path,
    metadata: dict[str, object],
    target_arg: Path,
) -> tuple[Path, str]:
    target = validate_restore_location(repository, metadata, target_arg, label="Målmappen")
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise SnapshotStorageError(f"Målmappen er ikke en vanlig mappe uten lenke: {target}")
        if any(target.iterdir()):
            raise SnapshotStorageError(f"Målmappen er ikke tom og blir ikke endret: {target}")
        return target, "empty"
    return target, "missing"


def validate_export_directory(
    repository: Path,
    metadata: dict[str, object],
    target_arg: Path,
) -> tuple[Path, str]:
    target = validate_restore_location(repository, metadata, target_arg, label="Eksportmappen")
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise SnapshotStorageError(f"Eksportmappen er ikke en vanlig mappe uten lenke: {target}")
        return target, "existing"
    return target, "missing"


def validate_restore_location(
    repository: Path,
    metadata: dict[str, object],
    target_arg: Path,
    *,
    label: str,
) -> Path:
    target_input = target_arg.expanduser()
    validate_non_network_path(target_input, label=label)
    validate_existing_path_components(target_input)
    target = target_input.resolve()
    parent = target.parent
    if not parent.is_dir():
        raise SnapshotStorageError(f"Foreldremappen til {label.lower()} finnes ikke: {parent}")
    if is_relative_to(target, repository) or is_relative_to(repository, target):
        raise SnapshotStorageError(f"{label} og repositoryet kan ikke ligge i hverandre: {target}")
    last_source = metadata["last_confirmed_source"]
    assert isinstance(last_source, dict)
    original = Path(str(last_source["collection_path"])).expanduser().resolve()
    if is_relative_to(target, original) or is_relative_to(original, target):
        raise SnapshotStorageError(f"{label} og den opprinnelige bildesamlingen kan ikke ligge i hverandre: {target}")
    return target


def require_available_object(
    repository: Path,
    reference: ObjectReference,
    *,
    required: bool,
) -> bool:
    path = snapshot_object_path(repository, reference.sha256, reference.size_bytes)
    if not path.exists() and not path.is_symlink():
        if required:
            raise SnapshotStorageError(f"Nødvendig snapshotobjekt mangler: {path}")
        return False
    validate_regular_file_without_links(path, label="Snapshotobjekt")
    if path.stat().st_size != reference.size_bytes:
        raise SnapshotStorageError(f"Snapshotobjektet har feil størrelse: {path}")
    return True


def output_for_entry(
    entry: RestoreEntry,
    relative_path: str,
    reference: ObjectReference,
    variant: str,
) -> RestoreOutput:
    return RestoreOutput(
        relative_path=relative_path,
        object=reference,
        mtime_ns=entry.mtime_ns,
        entry_id=entry.entry_id,
        original_path_display=entry.original_path_display,
        variant=variant,
        reason=entry.integrity_status,
    )


def observed_recovery_path(path: str, sha256: str) -> str:
    path_value = Path(path)
    suffix = path_value.suffix
    marker = f".observed-{sha256[:8]}"
    filename = f"{path_value.stem}{marker}{suffix}" if suffix else f"{path_value.name}{marker}"
    parent = path_value.parent.as_posix()
    return filename if parent == "." else f"{parent}/{filename}"


def validate_output_paths(outputs: list[RestoreOutput], *, label: str) -> None:
    keys: set[str] = set()
    for output in outputs:
        key = portable_path_key(output.relative_path)
        if key is None:
            raise SnapshotStorageError(f"Utrygg restore-sti i {label}: {output.relative_path!r}")
        if key in keys:
            raise SnapshotStorageError(f"Flere restoreposter kolliderer i {label}: {output.relative_path}")
        keys.add(key)


def recovery_path(target: Path, snapshot: SnapshotSummary) -> Path:
    date = snapshot.completed_at[:10].replace("-", "")
    return target.with_name(f"{target.name}-recovery-{date}-{snapshot.snapshot_id[:8]}")


def reject_restore_staging_remnants(target: Path) -> None:
    prefix = f".bildebank-restore-{target.name}-"
    remnants = sorted(path.name for path in target.parent.iterdir() if path.name.startswith(prefix))
    if remnants:
        raise SnapshotStorageError(
            "Fant en tidligere, ufullstendig restore som ikke blir endret automatisk: "
            + ", ".join(remnants)
        )


def select_entry(
    entries: tuple[RestoreEntry, ...],
    *,
    path: str | None,
    entry_id: str | None,
) -> RestoreEntry:
    if path is not None and portable_path_key(path) is None:
        raise SnapshotStorageError(f"Ugyldig snapshotsti: {path!r}")
    matches = [
        entry
        for entry in entries
        if (path is not None and entry.path == path)
        or (entry_id is not None and entry.entry_id == entry_id)
    ]
    if len(matches) != 1:
        selected = f"sti {path!r}" if path is not None else f"entry_id {entry_id!r}"
        raise SnapshotStorageError(f"Fant ikke nøyaktig én filpost med {selected}.")
    return matches[0]


def choose_single_file_output(
    repository: Path,
    entry: RestoreEntry,
    *,
    variant: str | None,
) -> RestoreOutput:
    if entry.restore_kind == "recovery_only":
        if variant == "expected":
            raise SnapshotStorageError("En recovery_only-post har ingen forventet variant.")
        if entry.object is None:
            raise SnapshotStorageError("Den valgte recovery_only-posten har ingen lagrede byte.")
        require_available_object(repository, entry.object, required=True)
        assert entry.recovery_name is not None
        return output_for_entry(entry, entry.recovery_name, entry.object, "observed")

    assert entry.path is not None
    expected_available = (
        entry.expected is not None
        and require_available_object(repository, entry.expected, required=False)
    )
    observed_available = entry.object is not None
    if observed_available:
        assert entry.object is not None
        require_available_object(repository, entry.object, required=True)
    observed_differs = observed_available and entry.object != entry.expected

    if variant is None and expected_available and observed_differs:
        raise SnapshotStorageError(
            "Både forventet og observert variant finnes. Velg --variant expected eller observed."
        )
    selected_variant = variant
    if selected_variant is None:
        selected_variant = "expected" if expected_available else "observed"
    if selected_variant == "expected":
        if not expected_available or entry.expected is None:
            raise SnapshotStorageError("Forventet variant finnes ikke i repositoryet.")
        return output_for_entry(entry, entry.path, entry.expected, "expected")
    if not observed_available or entry.object is None:
        raise SnapshotStorageError("Observert variant finnes ikke i repositoryet.")
    relative_path = (
        observed_recovery_path(entry.path, entry.object.sha256)
        if entry.integrity_status != "ok" and entry.object != entry.expected
        else entry.path
    )
    return output_for_entry(entry, relative_path, entry.object, "observed")
