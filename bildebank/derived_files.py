from __future__ import annotations

import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .collection_paths import (
    COLLECTION_FILE_MISSING,
    COLLECTION_FILE_OK,
    CollectionFileHashError,
    hash_stable_collection_file,
    inspect_collection_file,
    inspect_existing_collection_path_components,
    is_reparse_stat,
    parse_collection_relative_path,
)
from .media import IMAGE_EXTENSIONS
from .thumbnails import (
    THUMB_PROFILE,
    THUMB_ROOT_NAME,
    thumbnail_relative_path,
)
from .video_previews import (
    VIDEO_PREVIEW_PROFILE,
    VIDEO_PREVIEW_ROOT_NAME,
    VIDEO_PREVIEW_SOURCE_EXTENSIONS,
    video_preview_relative_path,
)


DERIVED_DELETE_REASON_MIGRATION = "schema-v20-orphan-derived-file"
DERIVED_DELETE_REASON_UNIMPORT = "unimport-derived-file"

DERIVED_KIND_THUMBNAIL = "thumbnail"
DERIVED_KIND_THUMBNAIL_TEMP = "thumbnail-temp"
DERIVED_KIND_LEGACY_THUMBNAIL = "legacy-thumbnail"
DERIVED_KIND_VIDEO_PREVIEW = "video-preview"
DERIVED_KIND_VIDEO_PREVIEW_TEMP = "video-preview-temp"

_HEX64 = r"[0-9a-f]{64}"
_THUMBNAIL_FILENAME_RE = re.compile(rf"{_HEX64}\.jpg")
_THUMBNAIL_TEMP_FILENAME_RE = re.compile(
    rf"\.{_HEX64}\.jpg\.[A-Za-z0-9_-]+\.tmp"
)
_VIDEO_PREVIEW_FILENAME_RE = re.compile(rf"({_HEX64})\.mp4")
_VIDEO_PREVIEW_TEMP_FILENAME_RE = re.compile(
    rf"\.({_HEX64})\.mp4\.[0-9a-f]{{32}}\.partial"
)


@dataclass(frozen=True)
class DerivedFileCandidate:
    relative_path: Path
    kind: str
    size_bytes: int


@dataclass(frozen=True)
class DerivedCleanupPlan:
    candidates: tuple[DerivedFileCandidate, ...]
    unsafe_paths: tuple[Path, ...] = ()


def expected_derived_paths(conn: sqlite3.Connection) -> frozenset[Path]:
    expected: set[Path] = set()
    for row in conn.execute(
        """
        SELECT
            target_path,
            stored_filename,
            sha256,
            deleted_at,
            deleted_original_target_path
        FROM files
        """
    ):
        expected.update(derived_paths_for_file_row(row))
    return frozenset(expected)


def derived_paths_for_file_row(row: Any) -> tuple[Path, ...]:
    stored_filename = Path(str(row["stored_filename"]))
    paths: list[Path] = []
    if stored_filename.suffix.casefold() in VIDEO_PREVIEW_SOURCE_EXTENSIONS:
        try:
            paths.append(video_preview_relative_path(str(row["sha256"])))
        except ValueError:
            pass
    original_path_value = (
        row["deleted_original_target_path"]
        if row["deleted_at"] is not None
        and row["deleted_original_target_path"] is not None
        else row["target_path"]
    )
    if stored_filename.suffix.casefold() in IMAGE_EXTENSIONS:
        try:
            original_path = parse_collection_relative_path(
                str(original_path_value)
            )
            paths.append(thumbnail_relative_path(original_path))
        except ValueError:
            pass
    return tuple(paths)


def existing_derived_paths_for_file_ids(
    conn: sqlite3.Connection,
    target: Path,
    file_ids: tuple[int, ...],
) -> tuple[Path, ...]:
    if not file_ids:
        return ()
    placeholders = ",".join("?" for _ in file_ids)
    rows = tuple(
        conn.execute(
            f"""
            SELECT
                target_path,
                stored_filename,
                sha256,
                deleted_at,
                deleted_original_target_path
            FROM files
            WHERE id IN ({placeholders})
            """,
            file_ids,
        )
    )
    paths = {
        relative_path
        for row in rows
        for relative_path in derived_paths_for_file_row(row)
        if inspect_collection_file(target, relative_path).status
        != COLLECTION_FILE_MISSING
    }
    return tuple(sorted(paths, key=lambda value: value.as_posix()))


def is_managed_derived_file_path(relative_path: Path) -> bool:
    return derived_file_kind(relative_path) is not None


def derived_file_kind(relative_path: Path) -> str | None:
    try:
        relative_path = parse_collection_relative_path(relative_path.as_posix())
    except ValueError:
        return None
    parts = relative_path.parts

    if _is_current_thumbnail_parent(parts[:-1]):
        filename = parts[-1]
        if _THUMBNAIL_FILENAME_RE.fullmatch(filename) is not None:
            return DERIVED_KIND_THUMBNAIL
        if _THUMBNAIL_TEMP_FILENAME_RE.fullmatch(filename) is not None:
            return DERIVED_KIND_THUMBNAIL_TEMP

    if _is_legacy_thumbnail_path(relative_path):
        return DERIVED_KIND_LEGACY_THUMBNAIL

    if (
        len(parts) == 4
        and parts[:2] == (VIDEO_PREVIEW_ROOT_NAME, VIDEO_PREVIEW_PROFILE)
        and _is_hex_prefix(parts[2])
    ):
        final_match = _VIDEO_PREVIEW_FILENAME_RE.fullmatch(parts[3])
        if final_match is not None and final_match.group(1).startswith(parts[2]):
            return DERIVED_KIND_VIDEO_PREVIEW
        temporary_match = _VIDEO_PREVIEW_TEMP_FILENAME_RE.fullmatch(parts[3])
        if (
            temporary_match is not None
            and temporary_match.group(1).startswith(parts[2])
        ):
            return DERIVED_KIND_VIDEO_PREVIEW_TEMP
    return None


def plan_orphaned_derived_files(
    conn: sqlite3.Connection,
    target: Path,
) -> DerivedCleanupPlan:
    target = target.resolve()
    expected = expected_derived_paths(conn)
    candidates: dict[Path, DerivedFileCandidate] = {}
    unsafe_paths: set[Path] = set()

    _scan_known_tree(
        target,
        Path(THUMB_ROOT_NAME),
        expected,
        candidates,
        unsafe_paths,
    )
    _scan_known_tree(
        target,
        Path(VIDEO_PREVIEW_ROOT_NAME),
        expected,
        candidates,
        unsafe_paths,
    )

    return DerivedCleanupPlan(
        candidates=tuple(
            candidates[path]
            for path in sorted(candidates, key=lambda value: value.as_posix())
        ),
        unsafe_paths=tuple(
            sorted(unsafe_paths, key=lambda value: value.as_posix())
        ),
    )


def enqueue_orphaned_derived_files_in_transaction(
    conn: sqlite3.Connection,
    target: Path,
) -> tuple[DerivedCleanupPlan, tuple[int, ...]]:
    from .pending_deletes import enqueue_pending_delete_in_transaction

    plan = plan_orphaned_derived_files(conn, target)
    unsafe_paths = set(plan.unsafe_paths)
    pending_ids: list[int] = []
    queued_candidates: list[DerivedFileCandidate] = []
    expected = expected_derived_paths(conn)
    for candidate in plan.candidates:
        if candidate.relative_path in expected:
            continue
        try:
            actual_sha256, actual_size = hash_stable_collection_file(
                target,
                candidate.relative_path,
            )
        except (CollectionFileHashError, OSError, ValueError):
            unsafe_paths.add(candidate.relative_path)
            continue
        pending = enqueue_pending_delete_in_transaction(
            conn,
            target,
            candidate.relative_path,
            reason=DERIVED_DELETE_REASON_MIGRATION,
            expected_sha256=actual_sha256,
            expected_size_bytes=actual_size,
        )
        pending_ids.append(pending.id)
        queued_candidates.append(
            DerivedFileCandidate(
                relative_path=candidate.relative_path,
                kind=candidate.kind,
                size_bytes=actual_size,
            )
        )
    return (
        DerivedCleanupPlan(
            candidates=tuple(queued_candidates),
            unsafe_paths=tuple(
                sorted(unsafe_paths, key=lambda value: value.as_posix())
            ),
        ),
        tuple(pending_ids),
    )


def enqueue_unimport_derived_files_in_transaction(
    conn: sqlite3.Connection,
    target: Path,
    rows: tuple[Any, ...],
    *,
    source_id: int,
) -> tuple[int, ...]:
    from .pending_deletes import enqueue_pending_delete_in_transaction

    pending_ids: list[int] = []
    paths = {
        relative_path
        for row in rows
        for relative_path in derived_paths_for_file_row(row)
    }
    for relative_path in sorted(paths, key=lambda value: value.as_posix()):
        inspection = inspect_collection_file(target, relative_path)
        if inspection.status == COLLECTION_FILE_MISSING:
            continue
        if inspection.status != COLLECTION_FILE_OK:
            raise ValueError(
                "Avledet fil kan ikke slettes trygt ved unimport: "
                f"{relative_path.as_posix()}: "
                f"{inspection.message or inspection.status}"
            )
        try:
            expected_sha256, expected_size = hash_stable_collection_file(
                target,
                relative_path,
            )
        except (CollectionFileHashError, OSError, ValueError) as exc:
            raise ValueError(
                "Avledet fil kunne ikke identifiseres stabilt ved unimport: "
                f"{relative_path.as_posix()}: {exc}"
            ) from exc
        pending = enqueue_pending_delete_in_transaction(
            conn,
            target,
            relative_path,
            reason=DERIVED_DELETE_REASON_UNIMPORT,
            source_id=source_id,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size,
        )
        pending_ids.append(pending.id)
    return tuple(pending_ids)


def cleanup_empty_derived_parents(
    target: Path,
    relative_paths: tuple[Path, ...],
) -> None:
    candidates: set[Path] = set()
    for relative_path in relative_paths:
        stop = _derived_parent_stop(relative_path)
        if stop is None:
            continue
        parent = relative_path.parent
        while parent != stop and len(parent.parts) > len(stop.parts):
            candidates.add(parent)
            parent = parent.parent
    for relative_path in sorted(
        candidates,
        key=lambda value: len(value.parts),
        reverse=True,
    ):
        issue = inspect_existing_collection_path_components(target, relative_path)
        if issue is not None:
            continue
        try:
            (target / relative_path).rmdir()
        except OSError:
            continue


def _scan_known_tree(
    target: Path,
    root_relative: Path,
    expected: frozenset[Path],
    candidates: dict[Path, DerivedFileCandidate],
    unsafe_paths: set[Path],
) -> None:
    root = target / root_relative
    try:
        root_stat = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        unsafe_paths.add(root_relative)
        return
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or is_reparse_stat(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        unsafe_paths.add(root_relative)
        return

    stack = [root_relative]
    while stack:
        directory_relative = stack.pop()
        directory = target / directory_relative
        try:
            with os.scandir(directory) as entries:
                directory_entries = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            unsafe_paths.add(directory_relative)
            continue
        for entry in directory_entries:
            relative_path = directory_relative / entry.name
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                if (
                    derived_file_kind(relative_path) is not None
                    or _is_known_derived_directory(relative_path)
                ):
                    unsafe_paths.add(relative_path)
                continue
            if _is_known_derived_directory(relative_path):
                if (
                    stat.S_ISLNK(entry_stat.st_mode)
                    or is_reparse_stat(entry_stat)
                    or not stat.S_ISDIR(entry_stat.st_mode)
                ):
                    unsafe_paths.add(relative_path)
                else:
                    stack.append(relative_path)
                continue

            kind = derived_file_kind(relative_path)
            if kind is None:
                continue
            if (
                stat.S_ISLNK(entry_stat.st_mode)
                or is_reparse_stat(entry_stat)
                or not stat.S_ISREG(entry_stat.st_mode)
            ):
                unsafe_paths.add(relative_path)
                continue
            if kind in {DERIVED_KIND_THUMBNAIL, DERIVED_KIND_VIDEO_PREVIEW}:
                if relative_path in expected:
                    continue
            candidates[relative_path] = DerivedFileCandidate(
                relative_path=relative_path,
                kind=kind,
                size_bytes=entry_stat.st_size,
            )


def _is_known_derived_directory(relative_path: Path) -> bool:
    parts = relative_path.parts
    if parts == (THUMB_ROOT_NAME, THUMB_PROFILE):
        return True
    if (
        len(parts) == 3
        and parts[:2] == (THUMB_ROOT_NAME, THUMB_PROFILE)
        and (parts[2] == "udatert" or _is_year(parts[2]))
    ):
        return True
    if (
        len(parts) == 4
        and parts[:2] == (THUMB_ROOT_NAME, THUMB_PROFILE)
        and _is_year(parts[2])
        and _is_month(parts[3])
    ):
        return True
    if len(parts) == 2 and parts[0] == THUMB_ROOT_NAME:
        return parts[1] == "udatert" or _is_year(parts[1])
    if (
        len(parts) == 3
        and parts[0] == THUMB_ROOT_NAME
        and _is_year(parts[1])
        and _is_month(parts[2])
    ):
        return True
    if parts == (VIDEO_PREVIEW_ROOT_NAME, VIDEO_PREVIEW_PROFILE):
        return True
    return (
        len(parts) == 3
        and parts[:2] == (VIDEO_PREVIEW_ROOT_NAME, VIDEO_PREVIEW_PROFILE)
        and _is_hex_prefix(parts[2])
    )


def _is_current_thumbnail_parent(parts: tuple[str, ...]) -> bool:
    if (
        len(parts) == 3
        and parts[:2] == (THUMB_ROOT_NAME, THUMB_PROFILE)
        and parts[2] == "udatert"
    ):
        return True
    return (
        len(parts) == 4
        and parts[:2] == (THUMB_ROOT_NAME, THUMB_PROFILE)
        and _is_year(parts[2])
        and _is_month(parts[3])
    )


def _is_legacy_thumbnail_path(relative_path: Path) -> bool:
    parts = relative_path.parts
    if (
        len(parts) == 3
        and parts[:2] == (THUMB_ROOT_NAME, "udatert")
    ):
        return _is_legacy_thumbnail_filename(parts[2])
    return (
        len(parts) == 4
        and parts[0] == THUMB_ROOT_NAME
        and _is_year(parts[1])
        and _is_month(parts[2])
        and _is_legacy_thumbnail_filename(parts[3])
    )


def _is_legacy_thumbnail_filename(value: str) -> bool:
    path = Path(value)
    if path.suffix.casefold() in {".jpg", ".jpeg"}:
        return True
    if not (value.startswith(".") and value.casefold().endswith(".tmp")):
        return False
    return Path(value[:-4]).suffix.casefold() in {".jpg", ".jpeg"}


def _derived_parent_stop(relative_path: Path) -> Path | None:
    kind = derived_file_kind(relative_path)
    if kind in {DERIVED_KIND_THUMBNAIL, DERIVED_KIND_THUMBNAIL_TEMP}:
        return Path(THUMB_ROOT_NAME, THUMB_PROFILE)
    if kind == DERIVED_KIND_LEGACY_THUMBNAIL:
        return Path(THUMB_ROOT_NAME)
    if kind in {
        DERIVED_KIND_VIDEO_PREVIEW,
        DERIVED_KIND_VIDEO_PREVIEW_TEMP,
    }:
        return Path(VIDEO_PREVIEW_ROOT_NAME, VIDEO_PREVIEW_PROFILE)
    return None


def _is_hex_prefix(value: str) -> bool:
    return (
        len(value) == 2
        and value.isascii()
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_year(value: str) -> bool:
    return (
        len(value) == 4
        and value.isascii()
        and value.isdigit()
        and value != "0000"
    )


def _is_month(value: str) -> bool:
    return (
        len(value) == 2
        and value.isascii()
        and value.isdigit()
        and 1 <= int(value) <= 12
    )
