from __future__ import annotations

import os
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from . import db
from .media import (
    MediaDate,
    camera_info,
    is_supported_media,
    media_date,
    metadata_datetime,
    sha256_file,
    stored_media_filename,
)
from .progress import ProgressMeter
from .safe_file_move import move_file_no_replace
from .target_lock import TargetLock


COMMIT_EVERY = 200
REFRESH_METADATA_COMMIT_EVERY = 1000


@dataclass
class ImportStats:
    scanned: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped_existing: int = 0
    name_conflicts: int = 0
    errors: int = 0
    stopped: bool = False


@dataclass
class MetadataRefreshStats:
    checked: int = 0
    metadata_found: int = 0
    moved: int = 0
    already_correct: int = 0
    errors: int = 0
    stopped: bool = False


MetadataRefreshProgress = Callable[[str, int, int, MetadataRefreshStats, Path | None], None]


@dataclass
class WalkError:
    path: Path
    message: str


class DuplicateFileIntegrityError(ValueError):
    pass


def validate_source_target(source: Path, target: Path) -> None:
    source_resolved = source.resolve()
    target_resolved = target.resolve()
    if source_resolved == target_resolved:
        raise ValueError("Kildemappe og bildesamling kan ikke være samme mappe.")
    if _is_relative_to(source_resolved, target_resolved):
        raise ValueError("Kildemappen kan ikke ligge inni bildesamlingen.")
    if _is_relative_to(target_resolved, source_resolved):
        raise ValueError("Bildesamlingen kan ikke ligge inni kildemappen.")


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def import_source(conn, target: Path, source: db.Source, *, verbose: bool = True) -> ImportStats:
    stats = ImportStats()
    root = source.path
    if not root.exists() or not root.is_dir():
        db.insert_error(
            conn,
            source_id=source.id,
            source_path=root,
            stage="source",
            message="Kilden finnes ikke eller er ikke en mappe.",
        )
        db.mark_source_error(conn, source.id)
        conn.commit()
        stats.errors += 1
        return stats

    errors_before = stats.errors
    progress = ProgressMeter("Import", stream=sys.stderr) if verbose else None
    try:
        if progress is not None:
            progress.message(f"Import: scanner {root}.")
        for item in iter_media_files(root):
            if isinstance(item, WalkError):
                stats.errors += 1
                db.insert_error(
                    conn,
                    source_id=source.id,
                    source_path=item.path,
                    stage="scan",
                    message=item.message,
                )
                continue
            path = item
            stats.scanned += 1
            try:
                process_file(conn, target, source, path, stats)
                resolve_successful_import_errors(conn, path)
            except Exception as exc:  # noqa: BLE001 - errors must be logged, not abort import
                stats.errors += 1
                db.insert_error(
                    conn,
                    source_id=source.id,
                    source_path=path,
                    stage="import",
                    message=str(exc),
                )
            if progress is not None:
                progress.update_count(
                    stats.scanned,
                    action="scannet",
                    details=import_progress_details(stats),
                )
            if (stats.imported + stats.duplicates + stats.skipped_existing) % COMMIT_EVERY == 0:
                conn.commit()
    except KeyboardInterrupt:
        stats.stopped = True
        conn.commit()
        if progress is not None:
            progress.done()
        print("Avbrutt. Databaseendringer er lagret så langt det var mulig.", flush=True)
        return stats
    finally:
        if progress is not None:
            progress.done()

    if stats.errors == errors_before:
        db.mark_source_imported(conn, source.id)
    else:
        db.mark_source_error(conn, source.id)
    conn.commit()
    return stats


def resolve_successful_import_errors(conn, path: Path) -> None:
    db.resolve_errors_for_path(conn, stage="scan", source_path=path)
    db.resolve_errors_for_path(conn, stage="import", source_path=path)


def import_source_dry_run(
    conn, target: Path, source: db.Source, *, output: TextIO, verbose: bool = True
) -> ImportStats:
    stats = ImportStats()
    root = source.path
    if not root.exists() or not root.is_dir():
        stats.errors += 1
        print(f"FEIL\t{root}\tKilden finnes ikke eller er ikke en mappe.", file=output)
        return stats

    progress = ProgressMeter("Import dry-run", stream=sys.stderr) if verbose else None
    try:
        if progress is not None:
            progress.message(f"Import dry-run: scanner {root}.")
        for item in iter_media_files(root):
            if isinstance(item, WalkError):
                stats.errors += 1
                print(f"FEIL\t{item.path}\t{item.message}", file=output)
                continue
            path = item
            stats.scanned += 1
            try:
                process_file_dry_run(conn, target, source, path, stats, output=output)
            except Exception as exc:  # noqa: BLE001 - dry-run should keep reporting
                stats.errors += 1
                print(f"FEIL\t{path}\t{exc}", file=output)
            if progress is not None:
                progress.update_count(
                    stats.scanned,
                    action="scannet",
                    details=import_progress_details(stats, dry_run=True),
                )
    except KeyboardInterrupt:
        stats.stopped = True
        if progress is not None:
            progress.done()
        print("Avbrutt. Dry-run har ikke endret databasen.", flush=True)
        return stats
    finally:
        if progress is not None:
            progress.done()
    return stats


def rescan_source(conn, target: Path, source: db.Source, *, verbose: bool = True) -> ImportStats:
    stats = ImportStats()
    if not source.path.exists() or not source.path.is_dir():
        db.insert_error(
            conn,
            source_id=source.id,
            source_path=source.path,
            stage="rescan-source",
            message="Kilden finnes ikke eller er ikke en mappe.",
        )
        db.mark_source_error(conn, source.id)
        conn.commit()
        stats.errors += 1
        return stats

    progress = ProgressMeter("Rescan-source", stream=sys.stderr) if verbose else None
    try:
        if progress is not None:
            progress.message(f"Rescan-source: scanner {source.path}.")
        for item in iter_media_files(source.path):
            if isinstance(item, WalkError):
                stats.errors += 1
                db.insert_error(
                    conn,
                    source_id=source.id,
                    source_path=item.path,
                    stage="rescan-source",
                    message=item.message,
                )
                continue
            stats.scanned += 1
            try:
                process_file(conn, target, source, item, stats)
                resolve_successful_import_errors(conn, item)
                db.resolve_errors_for_path(
                    conn,
                    stage="rescan-source",
                    source_path=item,
                )
            except Exception as exc:  # noqa: BLE001 - errors must be logged, not abort rescan
                stats.errors += 1
                db.insert_error(
                    conn,
                    source_id=source.id,
                    source_path=item,
                    stage="rescan-source",
                    message=str(exc),
                )
            if progress is not None:
                progress.update_count(
                    stats.scanned,
                    action="scannet",
                    details=import_progress_details(stats),
                )
            if (stats.imported + stats.duplicates + stats.skipped_existing) % COMMIT_EVERY == 0:
                conn.commit()
    except KeyboardInterrupt:
        stats.stopped = True
        conn.commit()
        if progress is not None:
            progress.done()
        print("Avbrutt. Databaseendringer er lagret så langt det var mulig.", flush=True)
        return stats
    finally:
        if progress is not None:
            progress.done()

    if stats.errors == 0:
        db.mark_source_imported(conn, source.id)
    else:
        db.mark_source_error(conn, source.id)
    conn.commit()
    return stats


def rescan_source_dry_run(
    conn, target: Path, source: db.Source, *, output: TextIO, verbose: bool = True
) -> ImportStats:
    stats = ImportStats()
    if not source.path.exists() or not source.path.is_dir():
        stats.errors += 1
        print(f"FEIL\t{source.path}\tKilden finnes ikke eller er ikke en mappe.", file=output)
        return stats

    progress = ProgressMeter("Rescan-source dry-run", stream=sys.stderr) if verbose else None
    try:
        if progress is not None:
            progress.message(f"Rescan-source dry-run: scanner {source.path}.")
        for item in iter_media_files(source.path):
            if isinstance(item, WalkError):
                stats.errors += 1
                print(f"FEIL\t{item.path}\t{item.message}", file=output)
                continue
            stats.scanned += 1
            try:
                process_file_dry_run(conn, target, source, item, stats, output=output)
            except Exception as exc:  # noqa: BLE001 - dry-run should keep reporting
                stats.errors += 1
                print(f"FEIL\t{item}\t{exc}", file=output)
            if progress is not None:
                progress.update_count(
                    stats.scanned,
                    action="scannet",
                    details=import_progress_details(stats, dry_run=True),
                )
    except KeyboardInterrupt:
        stats.stopped = True
        if progress is not None:
            progress.done()
        print("Avbrutt. Dry-run har ikke endret databasen.", flush=True)
        return stats
    finally:
        if progress is not None:
            progress.done()
    return stats


def import_progress_details(stats: ImportStats, *, dry_run: bool = False) -> str:
    imported_label = "ville_importert" if dry_run else "importert"
    return (
        f"{imported_label}={stats.imported}, duplikater={stats.duplicates}, "
        f"eksisterende={stats.skipped_existing}, feil={stats.errors}"
    )


def iter_media_files(root: Path):
    walk_errors: list[WalkError] = []

    def onerror(exc: OSError) -> None:
        path = Path(exc.filename) if exc.filename else root
        walk_errors.append(WalkError(path=path, message=str(exc)))

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        while walk_errors:
            yield walk_errors.pop(0)
        dirnames.sort()
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if is_supported_media(path):
                yield path
    while walk_errors:
        yield walk_errors.pop(0)


def process_file(conn, target: Path, source: db.Source, path: Path, stats: ImportStats) -> None:
    validate_import_file(path, target)
    source_key = db.path_key(path)
    if db.get_file_source_for_source_path(conn, source.id, source_key) is not None:
        stats.skipped_existing += 1
        return

    file_hash = sha256_file(path)
    size_bytes = path.stat().st_size

    existing = verified_duplicate_file(conn, target, file_hash)
    if existing is not None:
        db.insert_duplicate(
            conn,
            source_id=source.id,
            source_path=path,
            matched_file_id=int(existing["id"]),
            sha256=file_hash,
            size_bytes=size_bytes,
        )
        stats.duplicates += 1
        return

    date = media_date(path)
    camera = camera_info(path)
    metadata_dt = metadata_datetime(path)
    destination_dir = destination_directory(target, date)
    destination_dir.mkdir(parents=True, exist_ok=True)
    stored_filename = stored_media_filename(path.name)
    destination_path, name_conflict, already_present = find_existing_or_available_destination(
        destination_dir, stored_filename, file_hash
    )
    if already_present:
        db.insert_imported_file(
            conn,
            source_id=source.id,
            source_path=path,
            target_root=target,
            target_path=destination_path,
            original_filename=path.name,
            stored_filename=destination_path.name,
            sha256=file_hash,
            size_bytes=size_bytes,
            taken_date=_date_string(date),
            date_source=date.source,
            name_conflict=name_conflict,
            camera_make=camera.make if camera is not None else None,
            camera_model=camera.model if camera is not None else None,
            metadata_datetime=_datetime_string(metadata_dt),
        )
        stats.skipped_existing += 1
        if name_conflict:
            stats.name_conflicts += 1
        return

    safe_copy(path, destination_path, file_hash)
    db.insert_imported_file(
        conn,
        source_id=source.id,
        source_path=path,
        target_root=target,
        target_path=destination_path,
        original_filename=path.name,
        stored_filename=destination_path.name,
        sha256=file_hash,
        size_bytes=size_bytes,
        taken_date=_date_string(date),
        date_source=date.source,
        name_conflict=name_conflict,
        camera_make=camera.make if camera is not None else None,
        camera_model=camera.model if camera is not None else None,
        metadata_datetime=_datetime_string(metadata_dt),
    )
    stats.imported += 1
    if name_conflict:
        stats.name_conflicts += 1


def process_file_dry_run(
    conn, target: Path, source: db.Source, path: Path, stats: ImportStats, *, output: TextIO
) -> None:
    validate_import_file(path, target)
    source_key = db.path_key(path)
    if db.get_file_source_for_source_path(conn, source.id, source_key) is not None:
        stats.skipped_existing += 1
        return

    file_hash = sha256_file(path)
    existing = verified_duplicate_file(conn, target, file_hash)
    if existing is not None:
        stats.duplicates += 1
        return

    date = media_date(path)
    destination_dir = destination_directory(target, date)
    stored_filename = stored_media_filename(path.name)
    destination_path, name_conflict, already_present = find_existing_or_available_destination(
        destination_dir, stored_filename, file_hash
    )
    if already_present:
        stats.skipped_existing += 1
        if name_conflict:
            stats.name_conflicts += 1
        return

    stats.imported += 1
    if name_conflict:
        stats.name_conflicts += 1


def verified_duplicate_file(conn, target: Path, file_hash: str):
    rows = db.files_by_hash(conn, file_hash)
    if not rows:
        return None
    row = rows[0]
    verify_duplicate_target_file(target, row, file_hash)
    return row


def validate_import_file(path: Path, target: Path) -> None:
    path_stat = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    if stat.S_ISLNK(path_stat.st_mode) or bool(file_attributes & reparse_flag):
        raise ValueError(
            f"Import støtter ikke symbolske lenker, junctions eller andre reparse points: {path}"
        )
    if not stat.S_ISREG(path_stat.st_mode):
        raise ValueError(f"Importkilden er ikke en vanlig fil: {path}")
    resolved_path = path.resolve(strict=True)
    if _is_relative_to(resolved_path, target.resolve()):
        raise ValueError(
            f"En fil i kilden peker inn i bildesamlingen og kan ikke importeres: {path}"
        )


def verify_duplicate_target_file(target: Path, row, source_hash: str) -> None:
    file_id = int(row["id"])
    relative_target_path = str(row["target_path"])
    expected_hash = str(row["sha256"])
    target_path = db.absolute_target_path(target, relative_target_path)
    if expected_hash != source_hash:
        raise DuplicateFileIntegrityError(
            f"Integritetsfeil for eksisterende database-treff files.id={file_id}, "
            f"lagret sti={relative_target_path}: databaseført SHA-256 matcher ikke SHA-256 for filen i kilden."
        )
    if not target_path.exists():
        raise DuplicateFileIntegrityError(
            f"Integritetsfeil for eksisterende database-treff files.id={file_id}, "
            f"lagret sti={relative_target_path}: filen i bildesamlingen mangler på disk."
        )
    if not target_path.is_file():
        raise DuplicateFileIntegrityError(
            f"Integritetsfeil for eksisterende database-treff files.id={file_id}, "
            f"lagret sti={relative_target_path}: filen i bildesamlingen er ikke en vanlig fil."
        )
    actual_hash = sha256_file(target_path)
    if actual_hash != expected_hash:
        raise DuplicateFileIntegrityError(
            f"Integritetsfeil for eksisterende database-treff files.id={file_id}, "
            f"lagret sti={relative_target_path}: SHA-256 på disk matcher ikke databaseført SHA-256."
        )


def destination_directory(target: Path, date: MediaDate) -> Path:
    if date.date is None:
        return target / "udatert"
    return target / f"{date.date.year:04d}" / f"{date.date.month:02d}"


def _date_string(date: MediaDate) -> str | None:
    return date.date.isoformat() if date.date else None


def _datetime_string(value) -> str | None:
    return value.isoformat(sep=" ") if value is not None else None


def find_existing_or_available_destination(
    directory: Path, filename: str, file_hash: str
) -> tuple[Path, bool, bool]:
    candidate = directory / filename
    if not candidate.exists():
        return candidate, False, False
    if sha256_file(candidate) == file_hash:
        return candidate, False, True

    stem = candidate.stem
    suffix = candidate.suffix
    index = 1
    while True:
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate, True, False
        if sha256_file(candidate) == file_hash:
            return candidate, True, True
        index += 1


def safe_copy(source: Path, destination: Path, expected_hash: str) -> None:
    temp = destination.with_name(f".{destination.name}.bdbtmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        shutil.copy2(source, temp)
        copied_hash = sha256_file(temp)
        if copied_hash != expected_hash:
            raise OSError("Hash på kopiert fil matcher ikke filen i kilden.")
        install_copied_file(temp, destination, expected_hash)
    finally:
        if temp.exists():
            temp.unlink()


def install_copied_file(temp: Path, destination: Path, expected_hash: str) -> None:
    if destination.exists():
        if sha256_file(destination) == expected_hash:
            return
        raise FileExistsError(f"En fil finnes allerede med annet innhold: {destination}")
    temp.rename(destination)


def refresh_non_metadata_files(
    target: Path,
    *,
    dry_run: bool = False,
    rescan: bool = False,
    verbose: bool = False,
    progress: MetadataRefreshProgress | None = None,
    target_locked: bool = False,
) -> MetadataRefreshStats:
    if dry_run:
        return _refresh_non_metadata_files_unlocked(
            target,
            dry_run=True,
            rescan=rescan,
            verbose=verbose,
            progress=progress,
        )
    if target_locked:
        return _refresh_non_metadata_files_unlocked(
            target,
            dry_run=False,
            rescan=rescan,
            verbose=verbose,
            progress=progress,
        )
    with TargetLock(target, command="refresh-metadata"):
        return _refresh_non_metadata_files_unlocked(
            target,
            dry_run=False,
            rescan=rescan,
            verbose=verbose,
            progress=progress,
        )


def _refresh_non_metadata_files_unlocked(
    target: Path,
    *,
    dry_run: bool,
    rescan: bool,
    verbose: bool,
    progress: MetadataRefreshProgress | None,
) -> MetadataRefreshStats:
    stats = MetadataRefreshStats()
    conn = db.connect(target)
    try:
        rows = list(db.metadata_refresh_files(conn, rescan=rescan))
        total = len(rows)
        if progress is not None:
            progress("start", 0, total, stats, None)
        try:
            for index, row in enumerate(rows, start=1):
                stats.checked += 1
                target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
                try:
                    refresh_non_metadata_file(
                        conn, target, row, stats, dry_run=dry_run, verbose=verbose
                    )
                except Exception as exc:  # noqa: BLE001 - keep processing and record the file
                    stats.errors += 1
                    if progress is not None:
                        progress("error", index, total, stats, target_path)
                    if verbose:
                        print(f"FEIL\t{row['target_path']}\t{exc}", flush=True)
                    db.insert_error(
                        conn,
                        source_id=None,
                        source_path=target_path,
                        stage="refresh-metadata",
                        message=str(exc),
                    )
                if not dry_run and stats.checked % REFRESH_METADATA_COMMIT_EVERY == 0:
                    conn.commit()
                if progress is not None:
                    progress("check", index, total, stats, target_path)
        except KeyboardInterrupt:
            stats.stopped = True
            if not dry_run:
                conn.commit()
            print("Avbrutt. Databaseendringer er lagret så langt det var mulig.", flush=True)
        else:
            if not dry_run:
                conn.commit()
        if progress is not None:
            progress("done", total, total, stats, None)
    finally:
        conn.close()
    return stats


def refresh_non_metadata_file(
    conn,
    target: Path,
    row,
    stats: MetadataRefreshStats,
    *,
    dry_run: bool,
    verbose: bool,
) -> None:
    current_path = db.absolute_target_path(target, Path(str(row["target_path"])))
    if not current_path.exists():
        raise FileNotFoundError(f"Filen finnes ikke: {current_path}")
    file_hash = str(row["sha256"])

    date = media_date(current_path)
    camera = camera_info(current_path)
    metadata_dt = metadata_datetime(current_path)
    if date.source != "metadata" or date.date is None:
        if verbose:
            print(f"INGEN_METADATA\t{current_path}", flush=True)
        if not dry_run and camera is not None:
            db.update_file_camera(
                conn,
                file_id=int(row["id"]),
                camera_make=camera.make,
                camera_model=camera.model,
            )
        if not dry_run:
            db.resolve_errors_for_path(conn, stage="refresh-metadata", source_path=current_path)
        return

    stats.metadata_found += 1
    destination_dir = destination_directory(target, date)
    if not dry_run:
        destination_dir.mkdir(parents=True, exist_ok=True)
    destination_path, name_conflict, already_present = find_existing_or_available_destination(
        destination_dir, current_path.name, file_hash
    )

    move_id: int | None = None
    if already_present:
        if db.path_key(destination_path) == db.path_key(current_path):
            stats.already_correct += 1
            if verbose:
                print(f"ALLEREDE_RIKTIG\t{current_path}", flush=True)
        else:
            raise FileExistsError(f"Matchende fil finnes allerede: {destination_path}")
    elif db.path_key(destination_path) == db.path_key(current_path):
        stats.already_correct += 1
        if verbose:
            print(f"ALLEREDE_RIKTIG\t{current_path}", flush=True)
    else:
        actual_hash = sha256_file(current_path)
        if actual_hash != file_hash:
            raise ValueError(
                f"Fila på disk har feil SHA-256: {current_path} "
                f"(forventet {file_hash}, fant {actual_hash})"
            )
        if not dry_run:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            move_id = db.create_pending_file_move(
                conn,
                file_id=int(row["id"]),
                target_root=target,
                from_path=current_path,
                to_path=destination_path,
                sha256=file_hash,
                operation="refresh-metadata",
            )
            conn.commit()
            move_file_no_replace(
                current_path,
                destination_path,
                expected_sha256=file_hash,
            )
        stats.moved += 1
        if verbose:
            action = "VILLE_FLYTTE" if dry_run else "FLYTTET"
            print(f"{action}\t{current_path}\t->\t{destination_path}", flush=True)

    if not dry_run:
        db.update_file_placement(
            conn,
            file_id=int(row["id"]),
            target_root=target,
            target_path=destination_path,
            stored_filename=destination_path.name,
            taken_date=_date_string(date) or "",
            date_source="metadata",
            name_conflict=name_conflict,
            camera_make=camera.make if camera is not None else None,
            camera_model=camera.model if camera is not None else None,
            metadata_datetime=_datetime_string(metadata_dt),
        )
        if move_id is not None:
            db.complete_pending_file_move(conn, move_id=move_id)
        db.resolve_errors_for_path(conn, stage="refresh-metadata", source_path=current_path)
