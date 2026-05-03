from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import db
from .media import MediaDate, is_supported_media, media_date, sha256_file


COMMIT_EVERY = 200


@dataclass
class ImportStats:
    scanned: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped_existing: int = 0
    skipped_covered: int = 0
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


@dataclass
class WalkError:
    path: Path
    message: str


def validate_source_target(source: Path, target: Path) -> None:
    source_resolved = source.resolve()
    target_resolved = target.resolve()
    if source_resolved == target_resolved:
        raise ValueError("Kildemappe og målmappe kan ikke være samme mappe.")
    if _is_relative_to(source_resolved, target_resolved):
        raise ValueError("Kildemappen kan ikke ligge inni målmappen.")
    if _is_relative_to(target_resolved, source_resolved):
        raise ValueError("Målmappen kan ikke ligge inni kildemappen.")


def validate_new_directory_source(conn, source: Path) -> None:
    source_resolved = source.resolve()
    for registered in db.get_sources(conn):
        if registered.kind != "directory":
            continue
        registered_path = registered.path.resolve()
        if _same_path(source_resolved, registered_path):
            raise ValueError(f"Kildemappen er allerede registrert: {registered.path}")
        if _is_same_or_under(source_resolved, registered_path):
            raise ValueError(
                "Kildemappen ligger under en allerede registrert kildemappe: "
                f"{registered.path}"
            )


def _same_path(left: Path, right: Path) -> bool:
    return db.path_key(left) == db.path_key(right)


def _is_same_or_under(child: Path, parent: Path) -> bool:
    child_key = db.path_key(child)
    parent_key = db.path_key(parent)
    try:
        return os.path.commonpath([child_key, parent_key]) == parent_key
    except ValueError:
        return False


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


def import_pending_sources_dry_run(
    target: Path, *, output: TextIO, verbose: bool = True
) -> ImportStats:
    stats = ImportStats()
    conn = db.connect(target)
    try:
        sources = db.get_sources(conn, pending_only=True)
        for source in sources:
            source_stats = import_source_dry_run(
                conn, target, source, output=output, verbose=verbose
            )
            _merge_stats(stats, source_stats)
            if source_stats.stopped:
                break
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

    covered_sources = covered_imported_subsources(conn, source)
    covered_roots = [covered.path.resolve() for covered in covered_sources]
    errors_before = stats.errors
    try:
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
            if is_under_any(path, covered_roots):
                stats.skipped_covered += 1
                continue
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
                    f"importert={stats.imported} duplikater={stats.duplicates} "
                    f"dekket={stats.skipped_covered} feil={stats.errors}",
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
        db.mark_sources_superseded(
            conn,
            source_ids=[covered.id for covered in covered_sources],
            superseded_by_source_id=source.id,
        )
    else:
        db.mark_source_error(conn, source.id)
    conn.commit()
    return stats


def import_source_dry_run(
    conn, target: Path, source: db.Source, *, output: TextIO, verbose: bool = True
) -> ImportStats:
    stats = ImportStats()
    root = source.path
    if not root.exists() or not root.is_dir():
        stats.errors += 1
        print(f"FEIL\t{root}\tKilden finnes ikke eller er ikke en mappe.", file=output)
        return stats

    covered_sources = covered_imported_subsources(conn, source)
    covered_roots = [covered.path.resolve() for covered in covered_sources]
    try:
        for item in iter_media_files(root):
            if isinstance(item, WalkError):
                stats.errors += 1
                print(f"FEIL\t{item.path}\t{item.message}", file=output)
                continue
            path = item
            if is_under_any(path, covered_roots):
                stats.skipped_covered += 1
                continue
            stats.scanned += 1
            try:
                process_file_dry_run(conn, target, source, path, stats, output=output)
            except Exception as exc:  # noqa: BLE001 - dry-run should keep reporting
                stats.errors += 1
                print(f"FEIL\t{path}\t{exc}", file=output)
            if verbose and stats.scanned % 50 == 0:
                print(
                    f"{source.id}: dry-run scannet={stats.scanned} "
                    f"ville_importert={stats.imported} duplikater={stats.duplicates} "
                    f"dekket={stats.skipped_covered} feil={stats.errors}",
                    flush=True,
                )
    except KeyboardInterrupt:
        stats.stopped = True
        print("Avbrutt. Dry-run har ikke endret databasen.", flush=True)
        return stats
    return stats


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


def covered_imported_subsources(conn, source: db.Source) -> list[db.Source]:
    if source.kind != "directory":
        return []
    source_root = source.path.resolve()
    covered: list[db.Source] = []
    for candidate in db.get_sources(conn):
        if candidate.id == source.id or candidate.kind != "directory":
            continue
        if candidate.status == "superseded" or candidate.imported_at is None:
            continue
        candidate_path = candidate.path.resolve()
        if candidate_path == source_root:
            continue
        if _is_relative_to(candidate_path, source_root):
            covered.append(candidate)
    return covered


def is_under_any(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    return any(_is_relative_to(resolved, root) for root in roots)


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


def process_file_dry_run(
    conn, target: Path, source: db.Source, path: Path, stats: ImportStats, *, output: TextIO
) -> None:
    source_key = db.path_key(path)
    if db.get_file_for_source_path(conn, source.id, source_key) is not None:
        stats.skipped_existing += 1
        return
    if db.get_duplicate_for_source_path(conn, source.id, source_key) is not None:
        stats.skipped_existing += 1
        return

    file_hash = sha256_file(path)
    existing = db.find_file_by_hash(conn, file_hash)
    if existing is not None:
        stats.duplicates += 1
        return

    date = media_date(path)
    destination_dir = destination_directory(target, date)
    destination_path, name_conflict, already_present = find_existing_or_available_destination(
        destination_dir, path.name, file_hash
    )
    if already_present:
        stats.skipped_existing += 1
        if name_conflict:
            stats.name_conflicts += 1
        return

    stats.imported += 1
    if name_conflict:
        stats.name_conflicts += 1
    taken_date = _date_string(date) or "-"
    print(
        f"IMPORT\t{taken_date}\t{date.source}\t{path.resolve()}\t->\t{destination_path.resolve()}",
        file=output,
    )


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
    temp = destination.with_name(f".{destination.name}.bdbtmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        shutil.copy2(source, temp)
        copied_hash = sha256_file(temp)
        if copied_hash != expected_hash:
            raise OSError("Hash på kopiert fil matcher ikke kildefilen.")
        install_copied_file(temp, destination, expected_hash)
    finally:
        if temp.exists():
            temp.unlink()


def install_copied_file(temp: Path, destination: Path, expected_hash: str) -> None:
    try:
        os.link(temp, destination)
    except FileExistsError:
        if sha256_file(destination) == expected_hash:
            return
        raise FileExistsError(f"Målfil finnes allerede med annet innhold: {destination}")


def refresh_non_metadata_files(
    target: Path, *, dry_run: bool = False, verbose: bool = False
) -> MetadataRefreshStats:
    stats = MetadataRefreshStats()
    conn = db.connect(target)
    try:
        rows = list(db.non_metadata_files(conn))
        for row in rows:
            stats.checked += 1
            try:
                refresh_non_metadata_file(
                    conn, target, row, stats, dry_run=dry_run, verbose=verbose
                )
            except Exception as exc:  # noqa: BLE001 - keep processing and record the file
                stats.errors += 1
                if verbose:
                    print(f"FEIL\t{row['target_path']}\t{exc}", flush=True)
                db.insert_error(
                    conn,
                    source_id=None,
                    source_path=Path(row["target_path"]),
                    stage="refresh-metadata",
                    message=str(exc),
                )
        if not dry_run:
            conn.commit()
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
    current_path = Path(row["target_path"])
    if not current_path.exists():
        repaired_path = find_file_in_target_by_hash(target, str(row["sha256"]))
        if repaired_path is None:
            raise FileNotFoundError(f"Målfil finnes ikke: {current_path}")
        if verbose:
            print(f"REPARERER_DB_PATH\t{current_path}\t->\t{repaired_path}", flush=True)
        if not dry_run:
            db.resolve_errors_for_path(conn, stage="refresh-metadata", source_path=current_path)
        current_path = repaired_path

    date = media_date(current_path)
    if date.source != "metadata" or date.date is None:
        if verbose:
            print(f"INGEN_METADATA\t{current_path}", flush=True)
        return

    stats.metadata_found += 1
    destination_dir = destination_directory(target, date)
    if not dry_run:
        destination_dir.mkdir(parents=True, exist_ok=True)
    file_hash = str(row["sha256"])
    destination_path, name_conflict, already_present = find_existing_or_available_destination(
        destination_dir, current_path.name, file_hash
    )

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
        if not dry_run:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current_path), str(destination_path))
        stats.moved += 1
        if verbose:
            action = "VILLE_FLYTTE" if dry_run else "FLYTTET"
            print(f"{action}\t{current_path}\t->\t{destination_path}", flush=True)

    if not dry_run:
        db.update_file_placement(
            conn,
            file_id=int(row["id"]),
            target_path=destination_path,
            stored_filename=destination_path.name,
            taken_date=_date_string(date) or "",
            date_source="metadata",
            name_conflict=name_conflict,
        )
        db.resolve_errors_for_path(conn, stage="refresh-metadata", source_path=Path(row["target_path"]))


def find_file_in_target_by_hash(target: Path, expected_hash: str) -> Path | None:
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [dirname for dirname in dirnames if dirname not in {"__pycache__"}]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.name == db.DB_FILENAME or ".bdbtmp-" in path.name:
                continue
            try:
                if sha256_file(path) == expected_hash:
                    return path
            except OSError:
                continue
    return None


def _merge_stats(total: ImportStats, item: ImportStats) -> None:
    total.scanned += item.scanned
    total.imported += item.imported
    total.duplicates += item.duplicates
    total.skipped_existing += item.skipped_existing
    total.skipped_covered += item.skipped_covered
    total.name_conflicts += item.name_conflicts
    total.errors += item.errors
    total.stopped = total.stopped or item.stopped
