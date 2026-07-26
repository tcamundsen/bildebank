from __future__ import annotations

import mimetypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import db
from .collection_paths import (
    COLLECTION_FILE_MISSING,
    COLLECTION_FILE_NOT_REGULAR,
    COLLECTION_FILE_OK,
    COLLECTION_FILE_UNSAFE,
    CollectionFileInspection,
    InvalidCollectionRelativePath,
    inspect_collection_file,
    is_active_collection_file_path,
    is_deleted_collection_file_path,
    is_reparse_stat,
    parse_collection_relative_path,
    same_file_identity,
)
from .thumbnails import thumbnail_absolute_path, thumbnail_is_current
from .video_previews import (
    VIDEO_PREVIEW_SOURCE_EXTENSIONS,
    video_preview_absolute_path,
    video_preview_is_valid,
)


@dataclass(frozen=True)
class ServerFile:
    content: bytes
    content_type: str


@dataclass(frozen=True)
class ServerFilePath:
    root: Path
    path: Path
    content_type: str
    size: int
    path_stat: os.stat_result


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def read_server_file(
    target: Path,
    encoded_relative_path: str,
    *,
    require_active: bool = False,
) -> ServerFile:
    served = resolve_server_file(
        target,
        encoded_relative_path,
        require_active=require_active,
    )
    with open_server_file(served) as stream:
        content = stream.read()
    return ServerFile(content=content, content_type=served.content_type)


def resolve_server_file(
    target: Path,
    encoded_relative_path: str,
    *,
    require_active: bool = False,
) -> ServerFilePath:
    raw_file_id = encoded_relative_path.strip("/")
    if not raw_file_id.isascii() or not raw_file_id.isdecimal():
        raise FileNotFoundError("Filen finnes ikke.")
    return resolve_server_file_by_id(
        target,
        int(raw_file_id),
        require_active=require_active,
    )


def resolve_server_file_by_id(
    target: Path,
    file_id: int,
    *,
    require_active: bool = False,
) -> ServerFilePath:
    path = server_file_path_by_id(
        target,
        file_id,
        require_active=require_active,
    )
    return describe_server_file(target, path)


def resolve_server_thumbnail_file(
    target: Path,
    raw_file_id: str,
    *,
    require_active: bool = False,
) -> ServerFilePath:
    file_id = parse_server_file_id(raw_file_id)
    original = resolve_server_file_by_id(
        target,
        file_id,
        require_active=require_active,
    )
    relative_path = original.path.relative_to(original.root)
    if not thumbnail_is_current(original.root, relative_path):
        return original
    thumbnail_path = thumbnail_absolute_path(original.root, relative_path)
    try:
        thumbnail = describe_server_file(original.root, thumbnail_path)
    except (FileNotFoundError, PermissionError, OSError):
        return original
    return thumbnail


def resolve_video_preview_file(target: Path, raw_file_id: str) -> ServerFilePath:
    file_id = parse_server_file_id(raw_file_id)
    source_path = server_file_path_by_id(target, file_id, require_active=True)
    conn = db.connect_read_only(target, require_current=False)
    try:
        row = conn.execute(
            "SELECT sha256 FROM files WHERE id = ? AND deleted_at IS NULL",
            (file_id,),
        ).fetchone()
    finally:
        conn.close()
    if (
        row is None
        or source_path.suffix.casefold() not in VIDEO_PREVIEW_SOURCE_EXTENSIONS
        or not video_preview_is_valid(target, str(row["sha256"]))
    ):
        raise FileNotFoundError("Fant ikke en video med avspillingskopi.")
    path = video_preview_absolute_path(target.resolve(), str(row["sha256"]))
    return describe_server_file(target, path)


def describe_server_file(target: Path, path: Path) -> ServerFilePath:
    root = target.resolve()
    absolute_path = path if path.is_absolute() else path.absolute()
    try:
        relative_path = absolute_path.relative_to(root)
        relative_path = parse_collection_relative_path(relative_path.as_posix())
    except (ValueError, InvalidCollectionRelativePath) as exc:
        raise PermissionError("Ugyldig filsti.") from exc
    inspection = inspect_collection_file(root, relative_path)
    path_stat = require_safe_server_file_inspection(inspection)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return ServerFilePath(
        root=root,
        path=inspection.path,
        content_type=content_type,
        size=path_stat.st_size,
        path_stat=path_stat,
    )


def open_server_file(served_file: ServerFilePath) -> BinaryIO:
    relative_path = served_file.path.relative_to(served_file.root)
    before = inspect_collection_file(served_file.root, relative_path)
    before_stat = require_safe_server_file_inspection(before)
    if not same_server_file_version(served_file.path_stat, before_stat):
        raise PermissionError("Filen ble byttet eller endret før den kunne åpnes.")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        file_descriptor = os.open(served_file.path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PermissionError(
            f"Filen kunne ikke åpnes uten å følge lenker: {exc}"
        ) from exc

    try:
        opened_stat = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or is_reparse_stat(opened_stat)
            or not same_server_file_version(before_stat, opened_stat)
        ):
            raise PermissionError(
                "Filen ble byttet eller er ikke en vanlig fil uten lenker."
            )
        after = inspect_collection_file(served_file.root, relative_path)
        after_stat = require_safe_server_file_inspection(after)
        if not (
            same_server_file_version(opened_stat, after_stat)
            and same_server_file_version(served_file.path_stat, after_stat)
        ):
            raise PermissionError(
                "Filen ble byttet eller endret mens den ble åpnet."
            )
        return os.fdopen(file_descriptor, "rb", closefd=True)
    except BaseException:
        os.close(file_descriptor)
        raise


def require_safe_server_file_inspection(
    inspection: CollectionFileInspection,
) -> os.stat_result:
    if inspection.status == COLLECTION_FILE_MISSING:
        raise FileNotFoundError("Filen finnes ikke.")
    if inspection.status in {
        COLLECTION_FILE_NOT_REGULAR,
        COLLECTION_FILE_UNSAFE,
    }:
        raise PermissionError(
            inspection.message or "Filen er ikke en vanlig fil uten lenker."
        )
    if (
        inspection.status != COLLECTION_FILE_OK
        or inspection.path_stat is None
    ):
        raise OSError(inspection.message or "Filen kunne ikke kontrolleres.")
    return inspection.path_stat


def same_server_file_version(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return (
        same_file_identity(first, second)
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def parse_byte_range(value: str | None, size: int) -> ByteRange | None:
    if not value:
        return None
    unit, separator, raw_range = value.strip().partition("=")
    if separator != "=" or unit.casefold() != "bytes" or "," in raw_range:
        raise ValueError("Ugyldig Range-header.")
    raw_start, dash, raw_end = raw_range.strip().partition("-")
    if dash != "-" or (not raw_start and not raw_end) or size <= 0:
        raise ValueError("Ugyldig Range-header.")
    try:
        if not raw_start:
            suffix_length = int(raw_end)
            if suffix_length <= 0:
                raise ValueError
            start = max(size - suffix_length, 0)
            end = size - 1
        else:
            start = int(raw_start)
            end = size - 1 if not raw_end else int(raw_end)
            if start < 0 or end < start or start >= size:
                raise ValueError
            end = min(end, size - 1)
    except ValueError as exc:
        raise ValueError("Ugyldig eller utilfredsstillelig Range-header.") from exc
    return ByteRange(start, end)


def server_file_path(
    target: Path,
    encoded_relative_path: str,
    *,
    require_active: bool = False,
) -> Path:
    return server_file_path_by_id(
        target,
        parse_server_file_id(encoded_relative_path),
        require_active=require_active,
    )


def parse_server_file_id(value: str) -> int:
    raw_file_id = value.strip("/")
    if not raw_file_id.isascii() or not raw_file_id.isdecimal():
        raise FileNotFoundError("Filen finnes ikke.")
    return int(raw_file_id)


def server_file_path_by_id(
    target: Path,
    file_id: int,
    *,
    require_active: bool = False,
) -> Path:
    conn = db.connect_read_only(target, require_current=False)
    try:
        row = conn.execute(
            """
            SELECT
                target_path,
                target_path_key,
                deleted_at,
                deleted_original_target_path
            FROM files
            WHERE id = ?
              AND (? = 0 OR deleted_at IS NULL)
            """,
            (file_id, int(require_active)),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise FileNotFoundError("Filen finnes ikke.")

    try:
        relative_path = parse_collection_relative_path(row["target_path"])
    except InvalidCollectionRelativePath as exc:
        raise PermissionError("Ugyldig filsti i databasen.") from exc
    if row["target_path_key"] != db.relative_path_key(relative_path):
        raise PermissionError("Ugyldig filsti i databasen.")

    if row["deleted_at"] is None:
        if not is_active_collection_file_path(relative_path):
            raise PermissionError("Ugyldig filsti i databasen.")
    else:
        if not is_deleted_collection_file_path(relative_path):
            raise PermissionError("Ugyldig filsti i databasen.")
        try:
            original_path = parse_collection_relative_path(
                row["deleted_original_target_path"]
            )
        except InvalidCollectionRelativePath as exc:
            raise PermissionError("Ugyldig filsti i databasen.") from exc
        if (
            not is_active_collection_file_path(original_path)
            or relative_path != Path("deleted", *original_path.parts)
        ):
            raise PermissionError("Ugyldig filsti i databasen.")

    return target.resolve() / relative_path
