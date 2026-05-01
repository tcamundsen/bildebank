from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import db
from .media import MediaDate, is_supported_media, media_date, sha256_file


COMMIT_EVERY = 200


@dataclass
class ImportStats:
    scanned: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped_existing: int = 0
    name_conflicts: int = 0
    errors: int = 0
    stopped: bool = False


def validate_source_target(source: Path, target: Path) -> None:
    source_resolved = source.resolve()
    target_resolved = target.resolve()
    if source_resolved == target_resolved:
        raise ValueError("Kildemappe og målmappe kan ikke være samme mappe.")
    if _is_relative_to(source_resolved, target_resolved):
        raise ValueError("Kildemappen kan ikke ligge inni målmappen.")
    if _is_relative_to(target_resolved, source_resolved):
        raise ValueError("Målmappen kan ikke ligge inni kildemappen.")


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def import_pending_sources(target: Path, *, verbose: bool = True) -> ImportStats:
    stats = ImportStats()
    conn = db.connect(target)
    try:
        sources = db.get_sources(conn, pending_only=True)
        for source in sources:
            source_stats = import_source(conn, target, source, verbose=verbose)
            _merge_stats(stats, source_stats)
            if source_stats.stopped:
                break
        conn.commit()
    finally:
        conn.close()
    return stats


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
    try:
        for path in iter_media_files(root):
            stats.scanned += 1
            try:
                process_file(conn, target, source, path, stats)
            except Exception as exc:  # noqa: BLE001 - errors must be logged, not abort import
                stats.errors += 1
                db.insert_error(
                    conn,
                    source_id=source.id,
                    source_path=path,
                    stage="import",
                    message=str(exc),
                )
            if verbose and stats.scanned % 50 == 0:
                print(
                    f"{source.id}: scannet={stats.scanned} "
                    f"importert={stats.imported} duplikater={stats.duplicates} feil={stats.errors}",
                    flush=True,
                )
            if (stats.imported + stats.duplicates + stats.skipped_existing) % COMMIT_EVERY == 0:
                conn.commit()
    except KeyboardInterrupt:
        stats.stopped = True
        conn.commit()
        print("Avbrutt. Databaseendringer er lagret så langt det var mulig.", flush=True)
        return stats

    if stats.errors == errors_before:
        db.mark_source_imported(conn, source.id)
    else:
        db.mark_source_error(conn, source.id)
    conn.commit()
    return stats


def iter_media_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if is_supported_media(path):
                yield path


def process_file(conn, target: Path, source: db.Source, path: Path, stats: ImportStats) -> None:
    source_key = db.path_key(path)
    if db.get_file_for_source_path(conn, source.id, source_key) is not None:
        stats.skipped_existing += 1
        return
    if db.get_duplicate_for_source_path(conn, source.id, source_key) is not None:
        stats.skipped_existing += 1
        return

    file_hash = sha256_file(path)
    size_bytes = path.stat().st_size

    existing = db.find_file_by_hash(conn, file_hash)
    if existing is not None:
        db.insert_duplicate(
            conn,
            source_id=source.id,
            source_path=path,
            matched_file_id=int(existing["id"]),
            sha256=file_hash,
        )
        stats.duplicates += 1
        return

    date = media_date(path)
    destination_dir = destination_directory(target, date)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_path, name_conflict, already_present = find_existing_or_available_destination(
        destination_dir, path.name, file_hash
    )
    if already_present:
        db.insert_imported_file(
            conn,
            source_id=source.id,
            source_path=path,
            target_path=destination_path,
            original_filename=path.name,
            stored_filename=destination_path.name,
            sha256=file_hash,
            size_bytes=size_bytes,
            taken_date=_date_string(date),
            date_source=date.source,
            name_conflict=name_conflict,
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
        target_path=destination_path,
        original_filename=path.name,
        stored_filename=destination_path.name,
        sha256=file_hash,
        size_bytes=size_bytes,
        taken_date=_date_string(date),
        date_source=date.source,
        name_conflict=name_conflict,
    )
    stats.imported += 1
    if name_conflict:
        stats.name_conflicts += 1


def destination_directory(target: Path, date: MediaDate) -> Path:
    if date.date is None:
        return target / "udatert"
    return target / f"{date.date.year:04d}" / f"{date.date.month:02d}"


def _date_string(date: MediaDate) -> str | None:
    return date.date.isoformat() if date.date else None


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
    temp = destination.with_name(f".{destination.name}.bdbtmp-{os.getpid()}")
    try:
        shutil.copy2(source, temp)
        copied_hash = sha256_file(temp)
        if copied_hash != expected_hash:
            raise OSError("Hash på kopiert fil matcher ikke kildefilen.")
        os.replace(temp, destination)
    finally:
        if temp.exists():
            temp.unlink()


def _merge_stats(total: ImportStats, item: ImportStats) -> None:
    total.scanned += item.scanned
    total.imported += item.imported
    total.duplicates += item.duplicates
    total.skipped_existing += item.skipped_existing
    total.name_conflicts += item.name_conflicts
    total.errors += item.errors
    total.stopped = total.stopped or item.stopped
