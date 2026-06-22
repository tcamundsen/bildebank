from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from . import db
from .media import IMAGE_EXTENSIONS
from .target_lock import TargetLock


THUMB_ROOT_NAME = "thumbs"
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


def thumbnail_relative_path(original_relative_path: Path) -> Path:
    relative = Path(original_relative_path)
    suffix = relative.suffix.lower()
    filename = relative.name if suffix in {".jpg", ".jpeg"} else f"{relative.stem}.jpg"
    return Path(THUMB_ROOT_NAME, *relative.parent.parts, filename)


def thumbnail_absolute_path(target: Path, original_relative_path: Path) -> Path:
    return target / thumbnail_relative_path(original_relative_path)


def thumbnail_url(original_relative_path: Path) -> str:
    return path_to_url(thumbnail_relative_path(original_relative_path))


def existing_thumbnail_url(target: Path, original_relative_path: Path) -> str:
    original_path = db.absolute_target_path(target, original_relative_path)
    thumb_rel = thumbnail_relative_path(original_relative_path)
    thumb_path = target / thumb_rel
    if thumbnail_is_current(original_path, thumb_path):
        return path_to_url(thumb_rel)
    return path_to_url(original_relative_path)


def path_to_url(path: Path) -> str:
    return "/".join(quote(part) for part in path.parts)


def thumbnail_is_current(original_path: Path, thumb_path: Path) -> bool:
    try:
        return (
            thumb_path.is_file()
            and thumb_path.stat().st_mtime_ns >= original_path.stat().st_mtime_ns
        )
    except OSError:
        return False


def ensure_thumbnail(target: Path, original_relative_path: Path) -> Path | None:
    original_path = db.absolute_target_path(target, original_relative_path)
    if not original_path.is_file():
        return None

    if original_path.suffix.lower() not in IMAGE_EXTENSIONS:
        return None

    thumb_path = thumbnail_absolute_path(target, original_relative_path)
    if thumbnail_is_current(original_path, thumb_path):
        return thumb_path

    Image, ImageOps = require_pillow()

    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = thumb_path.with_name(f".{thumb_path.name}.tmp")

    try:
        with Image.open(original_path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail(THUMB_MAX_SIZE)
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(tmp_path, format="JPEG", quality=THUMB_QUALITY, optimize=True)
        tmp_path.replace(thumb_path)
    finally:
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
        return [db.relative_path(Path(str(row["target_path"]))) for row in rows]
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
) -> ThumbnailStats:
    require_pillow()
    stats = ThumbnailStats()
    with TargetLock(target, command="make-thumbnails"):
        candidates = active_thumbnail_candidates(target)
        if progress is not None:
            progress("start", 0, len(candidates), stats, None)
        current = 0
        for current, relative_path in enumerate(candidates, 1):
            stats.total += 1
            if limit is not None and stats.checked >= limit:
                break
            original_path = db.absolute_target_path(target, relative_path)
            if original_path.suffix.lower() not in IMAGE_EXTENSIONS:
                stats.skipped_non_image += 1
                if progress is not None:
                    progress("check", current, len(candidates), stats, relative_path)
                continue
            stats.checked += 1
            thumb_path = thumbnail_absolute_path(target, relative_path)
            if thumbnail_is_current(original_path, thumb_path):
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
