from __future__ import annotations

import json
import shutil
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .snapshot import (
    is_relative_to,
    portable_path_key,
    snapshot_object_path,
    validate_canonical_uuid,
    validate_existing_path_components,
    validate_non_network_path,
    validate_regular_file_without_links,
)
from .snapshot_check import (
    SnapshotSummary,
    expected_file,
    object_reference,
    read_canonical_json,
    read_snapshot,
    resolve_repository_for_check,
    validate_locked_repository_for_check,
)
from .snapshot_repository import ObjectReference, RepositoryLock, SnapshotStorageError


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
        recovery_target = recovery_path(target, loaded.summary) if recovery_outputs else None
        if recovery_target is not None and (recovery_target.exists() or recovery_target.is_symlink()):
            raise SnapshotStorageError(
                f"Recovery-målet finnes allerede og blir ikke overskrevet: {recovery_target}"
            )
        reject_restore_staging_remnants(target)
        required_bytes = sum(output.object.size_bytes for output in (*collection_outputs, *recovery_outputs))
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
        return SingleFileRestorePlan(
            repository=loaded.repository,
            snapshot=loaded.summary,
            export_directory=export_directory,
            export_state=export_state,
            output=output,
            output_path=output_path,
            note=loaded.summary.note,
        )


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
