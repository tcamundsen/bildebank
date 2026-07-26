from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from . import db
from .collection_paths import (
    COLLECTION_FILE_OK,
    CollectionFileAccessError,
    CollectionDirectoryError,
    ensure_collection_directory_without_links,
    inspect_collection_file,
    inspect_existing_collection_path_components,
    is_active_collection_file_path,
    is_reparse_stat,
    open_stable_collection_file,
    parse_collection_relative_path,
    same_collection_file_version,
)
from .html_paths import path_to_url
from .media import (
    IMAGE_EXTENSIONS,
    require_safe_pillow_image_size,
)
from .target_lock import TargetLock


THUMB_ROOT_NAME = "thumbs"
THUMB_PROFILE = "v2"
THUMB_MAX_SIZE = (360, 360)
THUMB_QUALITY = 82


class ThumbnailDependencyError(RuntimeError):
    pass


@dataclass
class ThumbnailStats:
    total: int = 0
    checked: int = 0
    created: int = 0
    skipped_current: int = 0
    skipped_non_image: int = 0
    errors: int = 0
    last_error_path: Path | None = None
    last_error_message: str | None = None


ThumbnailProgress = Callable[[str, int, int, ThumbnailStats, Path | None], None]


@dataclass(frozen=True)
class LegacyThumbnailFile:
    relative_path: Path
    size_bytes: int
    path_stat: os.stat_result


@dataclass(frozen=True)
class LegacyThumbnailCleanupPlan:
    files: tuple[LegacyThumbnailFile, ...]
    unsafe_paths: tuple[Path, ...] = ()

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)


@dataclass(frozen=True)
class LegacyThumbnailCleanupStats:
    found: int
    found_bytes: int
    deleted: int
    deleted_bytes: int
    skipped: int
    errors: int
    unsafe_paths: tuple[Path, ...] = ()


def thumbnail_relative_path(original_relative_path: Path) -> Path:
    relative = parse_collection_relative_path(Path(original_relative_path).as_posix())
    filename = hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest() + ".jpg"
    return Path(THUMB_ROOT_NAME, THUMB_PROFILE, *relative.parent.parts, filename)


def thumbnail_absolute_path(target: Path, original_relative_path: Path) -> Path:
    return target / thumbnail_relative_path(original_relative_path)


def thumbnail_url(original_relative_path: Path) -> str:
    return path_to_url(thumbnail_relative_path(original_relative_path))


def existing_thumbnail_url(target: Path, original_relative_path: Path) -> str:
    return path_to_url(existing_thumbnail_relative_path(target, original_relative_path))


def existing_thumbnail_relative_path(target: Path, original_relative_path: Path) -> Path:
    thumb_rel = thumbnail_relative_path(original_relative_path)
    if thumbnail_is_current(target, original_relative_path):
        return thumb_rel
    return original_relative_path


def thumbnail_is_current(target: Path, original_relative_path: Path) -> bool:
    try:
        relative_path = parse_collection_relative_path(
            Path(original_relative_path).as_posix()
        )
        original = inspect_collection_file(target, relative_path)
        thumbnail = inspect_collection_file(
            target,
            thumbnail_relative_path(relative_path),
        )
        if (
            original.status != COLLECTION_FILE_OK
            or original.path_stat is None
            or thumbnail.status != COLLECTION_FILE_OK
            or thumbnail.path_stat is None
            or thumbnail.path_stat.st_mtime_ns < original.path_stat.st_mtime_ns
        ):
            return False
        return _thumbnail_jpeg_is_valid(
            target,
            thumbnail_relative_path(relative_path),
        )
    except (CollectionFileAccessError, OSError, ValueError):
        return False


def ensure_thumbnail(target: Path, original_relative_path: Path) -> Path | None:
    target = target.resolve()
    relative_path = _require_active_relative_path(original_relative_path)
    if relative_path.suffix.lower() not in IMAGE_EXTENSIONS:
        return None

    original = inspect_collection_file(target, relative_path)
    if original.status != COLLECTION_FILE_OK:
        raise CollectionFileAccessError(
            original.message
            or f"Originalen er ikke en vanlig fil uten lenker: {original.path}"
        )

    thumb_rel = thumbnail_relative_path(relative_path)
    thumb_path = target / thumb_rel
    if thumbnail_is_current(target, relative_path):
        return thumb_path

    Image, ImageOps = require_pillow()

    thumb_parent = ensure_collection_directory_without_links(
        target,
        thumb_rel.parent,
    )
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{thumb_path.name}.",
        suffix=".tmp",
        dir=thumb_parent,
    )
    tmp_path = Path(temporary_name)

    try:
        with os.fdopen(temporary_fd, "w+b", closefd=True) as output:
            temporary_fd = -1
            with open_stable_collection_file(target, relative_path) as source:
                with Image.open(source) as image:
                    require_safe_pillow_image_size(image)
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail(THUMB_MAX_SIZE)
                    if image.mode not in {"RGB", "L"}:
                        image = image.convert("RGB")
                    image.save(
                        output,
                        format="JPEG",
                        quality=THUMB_QUALITY,
                        optimize=True,
                    )
            output.flush()
        tmp_rel = tmp_path.relative_to(target)
        if not _thumbnail_jpeg_is_valid(target, tmp_rel):
            raise RuntimeError("Pillow laget en ugyldig thumbnail.")
        original_after = inspect_collection_file(target, relative_path)
        if (
            original.path_stat is None
            or original_after.status != COLLECTION_FILE_OK
            or original_after.path_stat is None
            or not same_collection_file_version(
                original.path_stat,
                original_after.path_stat,
            )
        ):
            raise RuntimeError(
                "Originalbildet ble endret eller utrygt under thumbnail-genereringen."
            )
        ensure_collection_directory_without_links(target, thumb_rel.parent)
        os.replace(tmp_path, thumb_path)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    return thumb_path


def active_thumbnail_candidates(target: Path) -> list[Path]:
    conn = db.connect(target)
    try:
        rows = conn.execute(
            """
            SELECT id, target_path, target_path_key, sha256, stored_filename
            FROM files
            WHERE deleted_at IS NULL
            ORDER BY target_path_key
            """
        )
        candidates: list[Path] = []
        for row in rows:
            relative_path = _require_active_relative_path(
                Path(str(row["target_path"]))
            )
            if str(row["target_path_key"]) != db.relative_path_key(relative_path):
                raise ValueError(
                    f"files #{int(row['id'])} har target_path_key som ikke stemmer med target_path."
                )
            candidates.append(relative_path)
        return candidates
    finally:
        conn.close()


def require_pillow():
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ThumbnailDependencyError(
            "Pillow mangler. Installer avhengigheter på nytt før du kjører make-thumbnails."
        ) from exc
    return Image, ImageOps


def run_make_thumbnails(
    target: Path,
    *,
    limit: int | None = None,
    verbose: bool = False,
    progress: ThumbnailProgress | None = None,
    target_locked: bool = False,
) -> ThumbnailStats:
    require_pillow()
    stats = ThumbnailStats()
    lock = nullcontext() if target_locked else TargetLock(target, command="make-thumbnails")
    with lock:
        candidates = active_thumbnail_candidates(target)
        if progress is not None:
            progress("start", 0, len(candidates), stats, None)
        current = 0
        for current, relative_path in enumerate(candidates, 1):
            stats.total += 1
            if limit is not None and stats.checked >= limit:
                break
            if relative_path.suffix.lower() not in IMAGE_EXTENSIONS:
                stats.skipped_non_image += 1
                if progress is not None:
                    progress("check", current, len(candidates), stats, relative_path)
                continue
            stats.checked += 1
            if thumbnail_is_current(target, relative_path):
                stats.skipped_current += 1
                if progress is not None:
                    progress("check", current, len(candidates), stats, relative_path)
                continue
            try:
                result = ensure_thumbnail(target, relative_path)
            except Exception as exc:  # noqa: BLE001 - command must continue with next file
                stats.errors += 1
                stats.last_error_path = relative_path
                stats.last_error_message = str(exc)
                if verbose:
                    print(f"Feil ved thumbnail for {relative_path}: {exc}", file=sys.stderr)
                if progress is not None:
                    progress("error", current, len(candidates), stats, relative_path)
                continue
            if result is not None:
                stats.created += 1
            if progress is not None:
                progress("check", current, len(candidates), stats, relative_path)
        if progress is not None:
            progress("done", current, len(candidates), stats, None)
    return stats


def plan_legacy_thumbnail_cleanup(target: Path) -> LegacyThumbnailCleanupPlan:
    target = target.resolve()
    thumb_root = target / THUMB_ROOT_NAME
    try:
        root_stat = thumb_root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return LegacyThumbnailCleanupPlan(())
    except OSError as exc:
        raise CollectionDirectoryError(
            f"Kunne ikke kontrollere gammel thumbnail-mappe: {thumb_root}: {exc}"
        ) from exc
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or is_reparse_stat(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        raise CollectionDirectoryError(
            f"Thumbnail-mappen er ikke en vanlig mappe uten lenker: {thumb_root}"
        )

    files: list[LegacyThumbnailFile] = []
    unsafe_paths: list[Path] = []
    for root_entry in _scandir_sorted(thumb_root):
        if root_entry.name == THUMB_PROFILE:
            continue
        if root_entry.name == "udatert":
            if _entry_is_plain_directory(root_entry):
                _collect_legacy_thumbnail_files(
                    target,
                    Path(THUMB_ROOT_NAME, root_entry.name),
                    files,
                    unsafe_paths,
                )
            elif _entry_exists(root_entry):
                unsafe_paths.append(Path(THUMB_ROOT_NAME, root_entry.name))
            continue
        if not _is_legacy_thumbnail_year(root_entry.name):
            continue
        if not _entry_is_plain_directory(root_entry):
            if _entry_exists(root_entry):
                unsafe_paths.append(Path(THUMB_ROOT_NAME, root_entry.name))
            continue
        year_relative = Path(THUMB_ROOT_NAME, root_entry.name)
        for month_entry in _scandir_sorted(target / year_relative):
            if not _is_legacy_thumbnail_month(month_entry.name):
                continue
            month_relative = year_relative / month_entry.name
            if not _entry_is_plain_directory(month_entry):
                if _entry_exists(month_entry):
                    unsafe_paths.append(month_relative)
                continue
            _collect_legacy_thumbnail_files(
                target,
                month_relative,
                files,
                unsafe_paths,
            )
    return LegacyThumbnailCleanupPlan(
        tuple(files),
        tuple(unsafe_paths),
    )


def cleanup_legacy_thumbnails(
    target: Path,
    *,
    target_locked: bool = False,
) -> LegacyThumbnailCleanupStats:
    target = target.resolve()
    lock = (
        nullcontext()
        if target_locked
        else TargetLock(target, command="cleanup-thumbnails")
    )
    with lock:
        plan = plan_legacy_thumbnail_cleanup(target)
        deleted = 0
        deleted_bytes = 0
        skipped = 0
        errors = 0
        touched_directories: set[Path] = set()
        for item in plan.files:
            if not _is_legacy_thumbnail_relative_path(item.relative_path):
                errors += 1
                continue
            inspection = inspect_collection_file(target, item.relative_path)
            if (
                inspection.status != COLLECTION_FILE_OK
                or inspection.path_stat is None
                or not same_collection_file_version(
                    item.path_stat,
                    inspection.path_stat,
                )
            ):
                skipped += 1
                continue
            try:
                inspection.path.unlink()
            except FileNotFoundError:
                skipped += 1
                continue
            except OSError:
                errors += 1
                continue
            deleted += 1
            deleted_bytes += item.size_bytes
            touched_directories.add(item.relative_path.parent)

        _remove_empty_legacy_thumbnail_directories(target, touched_directories)
        return LegacyThumbnailCleanupStats(
            found=plan.file_count,
            found_bytes=plan.total_bytes,
            deleted=deleted,
            deleted_bytes=deleted_bytes,
            skipped=skipped,
            errors=errors,
            unsafe_paths=plan.unsafe_paths,
        )


def _require_active_relative_path(path: Path) -> Path:
    relative_path = parse_collection_relative_path(Path(path).as_posix())
    if not is_active_collection_file_path(relative_path):
        raise ValueError(
            f"Ugyldig aktiv samlingssti for thumbnail: {relative_path.as_posix()}"
        )
    return relative_path


def _thumbnail_jpeg_is_valid(target: Path, relative_path: Path) -> bool:
    try:
        Image, _ImageOps = require_pillow()
        with open_stable_collection_file(target, relative_path) as stream:
            with Image.open(stream) as image:
                if (
                    image.format != "JPEG"
                    or image.width > THUMB_MAX_SIZE[0]
                    or image.height > THUMB_MAX_SIZE[1]
                ):
                    return False
                image.verify()
        return True
    except Exception:  # noqa: BLE001 - invalid cache files are treated as missing
        return False


def _collect_legacy_thumbnail_files(
    target: Path,
    directory_relative: Path,
    files: list[LegacyThumbnailFile],
    unsafe_paths: list[Path],
) -> None:
    directory = target / directory_relative
    for entry in _scandir_sorted(directory):
        relative_path = directory_relative / entry.name
        if not _is_legacy_thumbnail_filename(entry.name):
            continue
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            unsafe_paths.append(relative_path)
            continue
        if (
            stat.S_ISLNK(entry_stat.st_mode)
            or is_reparse_stat(entry_stat)
            or not stat.S_ISREG(entry_stat.st_mode)
        ):
            unsafe_paths.append(relative_path)
            continue
        files.append(
            LegacyThumbnailFile(
                relative_path=relative_path,
                size_bytes=entry_stat.st_size,
                path_stat=entry_stat,
            )
        )


def _scandir_sorted(directory: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            return sorted(entries, key=lambda entry: entry.name)
    except OSError as exc:
        raise CollectionDirectoryError(
            f"Kunne ikke lese gammel thumbnail-mappe: {directory}: {exc}"
        ) from exc


def _entry_exists(entry: os.DirEntry[str]) -> bool:
    try:
        entry.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _entry_is_plain_directory(entry: os.DirEntry[str]) -> bool:
    try:
        entry_stat = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(entry_stat.st_mode)
        and not stat.S_ISLNK(entry_stat.st_mode)
        and not is_reparse_stat(entry_stat)
    )


def _is_legacy_thumbnail_year(value: str) -> bool:
    return (
        len(value) == 4
        and value.isascii()
        and value.isdigit()
        and value != "0000"
    )


def _is_legacy_thumbnail_month(value: str) -> bool:
    return (
        len(value) == 2
        and value.isascii()
        and value.isdigit()
        and 1 <= int(value) <= 12
    )


def _is_legacy_thumbnail_filename(value: str) -> bool:
    path = Path(value)
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        return True
    if not (value.startswith(".") and value.lower().endswith(".tmp")):
        return False
    return Path(value[:-4]).suffix.lower() in {".jpg", ".jpeg"}


def _is_legacy_thumbnail_relative_path(relative_path: Path) -> bool:
    parts = relative_path.parts
    if (
        len(parts) == 3
        and parts[:2] == (THUMB_ROOT_NAME, "udatert")
        and _is_legacy_thumbnail_filename(parts[2])
    ):
        return True
    return (
        len(parts) == 4
        and parts[0] == THUMB_ROOT_NAME
        and _is_legacy_thumbnail_year(parts[1])
        and _is_legacy_thumbnail_month(parts[2])
        and _is_legacy_thumbnail_filename(parts[3])
    )


def _remove_empty_legacy_thumbnail_directories(
    target: Path,
    directories: set[Path],
) -> None:
    candidates = set(directories)
    candidates.update(
        directory.parent
        for directory in directories
        if len(directory.parts) == 3
    )
    for relative_path in sorted(
        candidates,
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        if relative_path == Path(THUMB_ROOT_NAME):
            continue
        issue = inspect_existing_collection_path_components(target, relative_path)
        if issue is not None:
            continue
        path = target / relative_path
        try:
            path_stat = path.stat(follow_symlinks=False)
        except OSError:
            continue
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or is_reparse_stat(path_stat)
            or not stat.S_ISDIR(path_stat.st_mode)
        ):
            continue
        try:
            path.rmdir()
        except OSError:
            pass
