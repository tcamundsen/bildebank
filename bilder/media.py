from __future__ import annotations

import datetime as dt
import hashlib
import re
import struct
from dataclasses import dataclass
from pathlib import Path


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".webp",
}
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".mts",
    ".m2ts",
    ".3gp",
    ".wmv",
}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


@dataclass(frozen=True)
class MediaDate:
    date: dt.date | None
    source: str


def is_supported_media(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def media_date(path: Path) -> MediaDate:
    exif_date = jpeg_exif_date(path)
    if exif_date is not None:
        return MediaDate(exif_date, "metadata")

    filename_date = date_from_filename(path.name)
    if filename_date is not None:
        return MediaDate(filename_date, "filename")

    try:
        return MediaDate(dt.datetime.fromtimestamp(path.stat().st_mtime).date(), "mtime")
    except OSError:
        return MediaDate(None, "unknown")


def date_from_filename(filename: str) -> dt.date | None:
    patterns = [
        r"(?P<y>19\d{2}|20\d{2})[-_ ]?(?P<m>0[1-9]|1[0-2])[-_ ]?(?P<d>0[1-9]|[12]\d|3[01])",
        r"(?P<d>0[1-9]|[12]\d|3[01])[-_ ](?P<m>0[1-9]|1[0-2])[-_ ](?P<y>19\d{2}|20\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, filename)
        if not match:
            continue
        try:
            return dt.date(
                int(match.group("y")), int(match.group("m")), int(match.group("d"))
            )
        except ValueError:
            continue
    return None


def jpeg_exif_date(path: Path) -> dt.date | None:
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None

    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None

    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD9, 0xDA}:
            return None
        if offset + 2 > len(data):
            return None
        length = int.from_bytes(data[offset : offset + 2], "big")
        offset += 2
        if length < 2 or offset + length - 2 > len(data):
            return None
        segment = data[offset : offset + length - 2]
        offset += length - 2
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            return _date_from_tiff(segment[6:])
    return None


def _date_from_tiff(tiff: bytes) -> dt.date | None:
    if len(tiff) < 8:
        return None
    endian_marker = tiff[:2]
    if endian_marker == b"II":
        endian = "<"
    elif endian_marker == b"MM":
        endian = ">"
    else:
        return None
    if struct.unpack(endian + "H", tiff[2:4])[0] != 42:
        return None

    first_ifd = struct.unpack(endian + "I", tiff[4:8])[0]
    values = _read_ifd_values(tiff, first_ifd, endian)
    for tag in (0x9003, 0x9004, 0x0132):
        raw = values.get(tag)
        parsed = _parse_exif_date(raw)
        if parsed is not None:
            return parsed

    exif_offset = values.get(0x8769)
    if isinstance(exif_offset, int):
        exif_values = _read_ifd_values(tiff, exif_offset, endian)
        for tag in (0x9003, 0x9004, 0x0132):
            parsed = _parse_exif_date(exif_values.get(tag))
            if parsed is not None:
                return parsed
    return None


def _read_ifd_values(tiff: bytes, offset: int, endian: str) -> dict[int, bytes | int]:
    values: dict[int, bytes | int] = {}
    if offset < 0 or offset + 2 > len(tiff):
        return values
    count = struct.unpack(endian + "H", tiff[offset : offset + 2])[0]
    pos = offset + 2
    for _ in range(count):
        if pos + 12 > len(tiff):
            break
        tag, typ, num = struct.unpack(endian + "HHI", tiff[pos : pos + 8])
        value_or_offset = tiff[pos + 8 : pos + 12]
        size = _type_size(typ) * num
        if size <= 4:
            raw = value_or_offset[:size]
        else:
            value_offset = struct.unpack(endian + "I", value_or_offset)[0]
            raw = tiff[value_offset : value_offset + size]
        if typ == 4 and num == 1:
            values[tag] = struct.unpack(endian + "I", value_or_offset)[0]
        else:
            values[tag] = raw
        pos += 12
    return values


def _type_size(typ: int) -> int:
    return {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1}.get(typ, 1)


def _parse_exif_date(value: bytes | int | None) -> dt.date | None:
    if not isinstance(value, bytes):
        return None
    text = value.rstrip(b"\x00").decode("ascii", errors="ignore")
    match = re.match(r"(?P<y>\d{4}):(?P<m>\d{2}):(?P<d>\d{2})", text)
    if not match:
        return None
    try:
        return dt.date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
    except ValueError:
        return None

