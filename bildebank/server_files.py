from __future__ import annotations

import mimetypes
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import db
from .video_previews import video_preview_absolute_path


@dataclass(frozen=True)
class ServerFile:
    content: bytes
    content_type: str


@dataclass(frozen=True)
class ServerFilePath:
    path: Path
    content_type: str
    size: int


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def read_server_file(target: Path, encoded_relative_path: str) -> ServerFile:
    served = resolve_server_file(target, encoded_relative_path)
    return ServerFile(content=served.path.read_bytes(), content_type=served.content_type)


def resolve_server_file(target: Path, encoded_relative_path: str) -> ServerFilePath:
    path = server_file_path(target, encoded_relative_path)
    return describe_server_file(path)


def resolve_video_preview_file(target: Path, raw_file_id: str) -> ServerFilePath:
    try:
        file_id = int(raw_file_id)
    except ValueError as exc:
        raise FileNotFoundError("Ugyldig fil-ID.") from exc
    conn = db.connect(target)
    try:
        row = conn.execute(
            "SELECT target_path, sha256 FROM files WHERE id = ? AND deleted_at IS NULL",
            (file_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or Path(str(row["target_path"])).suffix.casefold() != ".avi":
        raise FileNotFoundError("Fant ikke AVI-filen.")
    path = video_preview_absolute_path(target, str(row["sha256"])).resolve()
    try:
        path.relative_to(target.resolve())
    except ValueError as exc:
        raise PermissionError("Ugyldig sti til videoavspillingskopi.") from exc
    return describe_server_file(path)


def describe_server_file(path: Path) -> ServerFilePath:
    if not path.is_file():
        raise FileNotFoundError("Filen finnes ikke.")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return ServerFilePath(path=path, content_type=content_type, size=path.stat().st_size)


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


def server_file_path(target: Path, encoded_relative_path: str) -> Path:
    raw_path = urllib.parse.unquote(encoded_relative_path).strip("/")
    if raw_path.isdigit():
        return server_file_path_by_id(target, int(raw_path))

    relative = Path(raw_path)
    path = (target / relative).resolve()
    try:
        path.relative_to(target.resolve())
    except ValueError as exc:
        raise PermissionError("Ugyldig filsti.") from exc
    return path


def server_file_path_by_id(target: Path, file_id: int) -> Path:
    conn = db.connect(target)
    try:
        row = conn.execute("SELECT target_path FROM files WHERE id = ?", (file_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise FileNotFoundError("Filen finnes ikke.")
    raw_path = Path(str(row["target_path"]))
    path = db.absolute_target_path(target, raw_path).resolve()
    try:
        path.relative_to(target.resolve())
    except ValueError as exc:
        raise PermissionError("Ugyldig filsti i databasen.") from exc
    return path
