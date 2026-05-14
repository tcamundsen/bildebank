from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, db
from .target_lock import TargetLock


BACKUP_METADATA_FILENAME = ".bildebank-backup.json"
BACKUP_FORMAT_VERSION = 1


@dataclass(frozen=True)
class BackupPlan:
    source_dir: Path
    backup_parent: Path
    backup_dir: Path
    collection_id: str
    existing_backup: bool


@dataclass(frozen=True)
class BackupStats:
    plan: BackupPlan
    dry_run: bool
    engine: str = "none"
    warning: str | None = None
    stats_available: bool = False
    files_copied: int = 0
    files_deleted: int = 0
    dirs_created: int = 0
    dirs_deleted: int = 0


def plan_backup(source_dir: Path, backup_parent_arg: Path, *, ensure_collection_id: bool = True) -> BackupPlan:
    source = source_dir.resolve()
    backup_parent = backup_parent_arg.expanduser().resolve()
    if not source.is_dir() or not db.db_path_for_target(source).exists():
        raise ValueError(f"Bildesamlingen er ikke initialisert: {source}")
    if not backup_parent.exists():
        raise ValueError(
            "Backup-plasseringen finnes ikke:\n"
            f"\n  {backup_parent}\n"
            "\nOpprett mappen først, eller velg en eksisterende plassering."
        )
    if not backup_parent.is_dir():
        raise ValueError(f"Backup-plasseringen er ikke en mappe: {backup_parent}")

    backup_dir = (backup_parent / source.name).resolve()
    validate_backup_location(source, backup_dir)
    validate_backup_not_nested_inside_existing_backup(backup_dir)

    conn = db.connect(source, require_current=False)
    try:
        db.require_current_schema(conn)
        if ensure_collection_id:
            collection_id = db.set_collection_id(conn)
            conn.commit()
        else:
            collection_id = db.get_meta(conn, db.COLLECTION_ID_META_KEY)
            if collection_id is None:
                raise ValueError("Databasen mangler collection_id. Kjør bildebank status før dry-run av backup.")
    finally:
        conn.close()

    existing_backup = backup_dir.exists()
    if existing_backup:
        validate_existing_backup(backup_dir, collection_id)
    return BackupPlan(source, backup_parent, backup_dir, collection_id, existing_backup)


def run_backup(source_dir: Path, backup_parent_arg: Path, *, dry_run: bool = False) -> BackupStats:
    if dry_run:
        plan = plan_backup(source_dir, backup_parent_arg, ensure_collection_id=False)
        engine = select_backup_engine()
        if engine is None:
            return BackupStats(
                plan,
                dry_run=True,
                engine="python",
                warning="robocopy/rsync mangler. Dry-run viser bare plan.",
            )
        run_external_mirror(plan, engine, dry_run=True)
        return BackupStats(plan, dry_run=True, engine=engine.name)

    source = source_dir.resolve()
    if not source.is_dir() or not db.db_path_for_target(source).exists():
        raise ValueError(f"Bildesamlingen er ikke initialisert: {source}")

    with TargetLock(source, command="backup"):
        plan = plan_backup(source, backup_parent_arg, ensure_collection_id=True)

        conn = db.connect(plan.source_dir)
        try:
            db.log_command(conn, "backup", {"destination": str(plan.backup_parent)})
            conn.commit()
        finally:
            conn.close()

        plan.backup_dir.mkdir(exist_ok=True)
        write_backup_metadata(plan, status="in-progress")
        engine = select_backup_engine()
        warning = None
        if engine is None:
            stats = mirror_directory(plan.source_dir, plan.backup_dir)
            engine_name = "python"
            warning = "robocopy/rsync mangler. Bruker tregere Python-kopiering."
            stats_available = True
        else:
            stats = run_external_mirror(plan, engine)
            engine_name = engine.name
            stats_available = False
        write_backup_metadata(plan, status="complete", engine=engine_name)
        return BackupStats(
            plan=plan,
            dry_run=False,
            engine=engine_name,
            warning=warning,
            stats_available=stats_available,
            files_copied=stats.files_copied,
            files_deleted=stats.files_deleted,
            dirs_created=stats.dirs_created,
            dirs_deleted=stats.dirs_deleted,
        )


def validate_backup_location(source: Path, backup_dir: Path) -> None:
    if backup_dir == source:
        raise ValueError(f"Backupmålet er samme mappe som bildesamlingen: {backup_dir}")
    if is_relative_to(source, backup_dir):
        raise ValueError(f"Backupmålet er en overmappe til bildesamlingen: {backup_dir}")
    if is_relative_to(backup_dir, source):
        raise ValueError(f"Backupmålet ligger inne i bildesamlingen: {backup_dir}")


def validate_backup_not_nested_inside_existing_backup(backup_dir: Path) -> None:
    for parent in backup_dir.resolve().parents:
        if parent == backup_dir:
            continue
        metadata_path = parent / BACKUP_METADATA_FILENAME
        if metadata_path.exists():
            raise ValueError(
                "Kan ikke lage backup inni en annen backup:\n"
                f"\n  {backup_dir}\n"
                "\nVelg en plassering utenfor denne backupen."
            )


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_existing_backup(backup_dir: Path, collection_id: str) -> None:
    if not backup_dir.is_dir():
        raise ValueError(f"Backupmålet finnes, men er ikke en mappe: {backup_dir}")
    metadata_path = backup_dir / BACKUP_METADATA_FILENAME
    if not metadata_path.exists():
        raise ValueError(
            "Kan ikke lage backup.\n"
            "\nMålmappen finnes allerede, men ser ikke ut til å være en bildebank-backup:\n"
            f"\n  {backup_dir}\n"
            "\nVelg en annen backup-plassering, eller flytt/gi nytt navn til denne mappen."
        )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Backupmetadata er ikke gyldig JSON: {metadata_path}") from exc
    backup_of = metadata.get("backup_of")
    if backup_of != collection_id:
        raise ValueError(
            "Kan ikke oppdatere backup.\n"
            "\nMålmappen er merket som backup for en annen bildesamling:\n"
            f"\n  {backup_dir}"
        )


@dataclass(frozen=True)
class BackupEngine:
    name: str
    executable: str


def select_backup_engine() -> BackupEngine | None:
    if sys.platform == "win32":
        executable = shutil.which("robocopy")
        return BackupEngine("robocopy", executable) if executable else None
    executable = shutil.which("rsync")
    return BackupEngine("rsync", executable) if executable else None


def run_external_mirror(plan: BackupPlan, engine: BackupEngine, *, dry_run: bool = False) -> "MirrorStats":
    if engine.name == "robocopy":
        return run_robocopy(plan, engine.executable, dry_run=dry_run)
    if engine.name == "rsync":
        return run_rsync(plan, engine.executable, dry_run=dry_run)
    raise ValueError(f"Ukjent backupmotor: {engine.name}")


def run_robocopy(plan: BackupPlan, executable: str, *, dry_run: bool = False) -> "MirrorStats":
    command = [
        executable,
        str(plan.source_dir),
        str(plan.backup_dir),
        "/MIR",
        "/Z",
        "/DCOPY:DAT",
        "/COPY:DAT",
        "/R:2",
        "/W:5",
        "/XJ",
        "/FFT",
        "/XF",
        BACKUP_METADATA_FILENAME,
    ]
    if dry_run:
        command.append("/L")
    result = subprocess.run(command, check=False)
    if result.returncode > 7:
        raise ValueError(f"robocopy feilet med exitkode {result.returncode}.")
    return MirrorStats()


def run_rsync(plan: BackupPlan, executable: str, *, dry_run: bool = False) -> "MirrorStats":
    command = [
        executable,
        "--progress",
        "--stats",
        "-a",
        "--delete",
        "--exclude",
        BACKUP_METADATA_FILENAME,
        source_arg_with_trailing_slash(plan.source_dir),
        destination_arg_with_trailing_slash(plan.backup_dir),
    ]
    if dry_run:
        command.insert(1, "--dry-run")
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise ValueError(f"rsync feilet med exitkode {result.returncode}.")
    return MirrorStats()


def source_arg_with_trailing_slash(path: Path) -> str:
    return str(path) + "/"


def destination_arg_with_trailing_slash(path: Path) -> str:
    return str(path) + "/"


def write_backup_metadata(plan: BackupPlan, *, status: str, engine: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    metadata = {
        "backup_of": plan.collection_id,
        "source_name": plan.source_dir.name,
        "created_by": "bildebank",
        "bildebank_version": __version__,
        "format_version": BACKUP_FORMAT_VERSION,
        "status": status,
        "updated_at": now,
    }
    if status == "in-progress":
        metadata["started_at"] = now
    if status == "complete":
        metadata["completed_at"] = now
    if engine is not None:
        metadata["engine"] = engine
    path = plan.backup_dir / BACKUP_METADATA_FILENAME
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class MirrorStats:
    files_copied: int = 0
    files_deleted: int = 0
    dirs_created: int = 0
    dirs_deleted: int = 0


def mirror_directory(source: Path, destination: Path) -> MirrorStats:
    files_copied = 0
    dirs_created = 0
    for source_path in source.rglob("*"):
        if source_path.is_dir() and source_path.is_symlink():
            continue
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if source_path.is_dir():
            if not destination_path.exists():
                destination_path.mkdir()
                dirs_created += 1
            continue
        if source_path.is_file() and should_copy_file(source_path, destination_path):
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
            files_copied += 1

    files_deleted, dirs_deleted = delete_extra_destination_entries(source, destination)
    return MirrorStats(
        files_copied=files_copied,
        files_deleted=files_deleted,
        dirs_created=dirs_created,
        dirs_deleted=dirs_deleted,
    )


def should_copy_file(source: Path, destination: Path) -> bool:
    if not destination.exists():
        return True
    if not destination.is_file():
        return True
    source_stat = source.stat()
    destination_stat = destination.stat()
    return (
        source_stat.st_size != destination_stat.st_size
        or int(source_stat.st_mtime) != int(destination_stat.st_mtime)
    )


def delete_extra_destination_entries(source: Path, destination: Path) -> tuple[int, int]:
    files_deleted = 0
    dirs_deleted = 0
    for destination_path in sorted(destination.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        if destination_path.name == BACKUP_METADATA_FILENAME:
            continue
        relative = destination_path.relative_to(destination)
        source_path = source / relative
        if source_path.exists():
            continue
        if destination_path.is_dir() and not destination_path.is_symlink():
            shutil.rmtree(destination_path)
            dirs_deleted += 1
        else:
            destination_path.unlink()
            files_deleted += 1
    return files_deleted, dirs_deleted
