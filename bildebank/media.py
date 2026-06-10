from __future__ import annotations

import datetime as dt
import hashlib
import re
import struct
import xml.etree.ElementTree as ET
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
ARCHIVE_IMAGE_EXTENSIONS = {
    ".nef",
    ".psd",
    ".raw",
}
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mp",
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
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | ARCHIVE_IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


@dataclass(frozen=True)
class MediaDate:
    date: dt.date | None
    source: str


@dataclass(frozen=True)
class DateCandidate:
    source: str
    date: dt.date | None
    detail: str


@dataclass(frozen=True)
class DateExplanation:
    path: Path
    supported_media: bool
    selected: MediaDate
    candidates: tuple[DateCandidate, ...]


@dataclass(frozen=True)
class MetadataInspection:
    path: Path
    lines: tuple[str, ...]


@dataclass(frozen=True)
class ImageDimensions:
    width: int
    height: int


@dataclass(frozen=True)
class CameraInfo:
    make: str | None
    model: str | None


def is_supported_media(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() == ".mp":
        return is_mp4_container(path)
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def is_mp4_container(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
    except OSError:
        return False
    return len(header) >= 8 and header[4:8] == b"ftyp"


def stored_media_filename(original_filename: str) -> str:
    path = Path(original_filename)
    if path.suffix.lower() == ".mp":
        return f"{path.stem}.mp4"
    return original_filename


def media_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in ARCHIVE_IMAGE_EXTENSIONS:
        return "file"
    return "file"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_dimensions(path: Path) -> ImageDimensions | None:
    try:
        with path.open("rb") as fh:
            header = fh.read(64)
            if is_jpeg_header(header):
                return jpeg_dimensions(path)
            if header.startswith(b"\x89PNG\r\n\x1a\n"):
                return png_dimensions(header)
            if header[:6] in {b"GIF87a", b"GIF89a"}:
                return gif_dimensions(header)
            if header.startswith(b"BM"):
                return bmp_dimensions(header)
    except OSError:
        return None
    return None


def is_jpeg_header(data: bytes) -> bool:
    return len(data) >= 2 and data[:2] == b"\xff\xd8"


def png_dimensions(header: bytes) -> ImageDimensions | None:
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if header[12:16] != b"IHDR":
        return None
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return ImageDimensions(width, height)


def gif_dimensions(header: bytes) -> ImageDimensions | None:
    if len(header) < 10 or header[:6] not in {b"GIF87a", b"GIF89a"}:
        return None
    width = int.from_bytes(header[6:8], "little")
    height = int.from_bytes(header[8:10], "little")
    if width <= 0 or height <= 0:
        return None
    return ImageDimensions(width, height)


def bmp_dimensions(header: bytes) -> ImageDimensions | None:
    if len(header) < 26 or not header.startswith(b"BM"):
        return None
    dib_size = int.from_bytes(header[14:18], "little")
    if dib_size == 12:
        if len(header) < 26:
            return None
        width = int.from_bytes(header[18:20], "little")
        height = int.from_bytes(header[20:22], "little")
    else:
        if len(header) < 26:
            return None
        width = int.from_bytes(header[18:22], "little", signed=True)
        height = abs(int.from_bytes(header[22:26], "little", signed=True))
    if width <= 0 or height <= 0:
        return None
    return ImageDimensions(width, height)


def jpeg_dimensions(path: Path) -> ImageDimensions | None:
    for marker, segment in _jpeg_segments(path):
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            if len(segment) < 5:
                return None
            height = int.from_bytes(segment[1:3], "big")
            width = int.from_bytes(segment[3:5], "big")
            if width <= 0 or height <= 0:
                return None
            return ImageDimensions(width, height)
    return None


def image_orientation(path: Path) -> int:
    if not is_jpeg_file(path):
        return 1
    for segment in _jpeg_app1_segments(path):
        if segment.startswith(b"Exif\x00\x00"):
            orientation = _orientation_from_tiff(segment[6:])
            if orientation is not None:
                return orientation
    return 1


def camera_info(path: Path) -> CameraInfo | None:
    if not is_jpeg_file(path):
        return None
    for segment in _jpeg_app1_segments(path):
        if not segment.startswith(b"Exif\x00\x00"):
            continue
        info = _camera_info_from_tiff(segment[6:])
        if info is not None:
            return info
    return None


def inspect_metadata(path: Path) -> MetadataInspection:
    lines: list[str] = []
    lines.append(f"Fil: {path}")
    lines.append(f"Støttet mediafil: {'ja' if is_supported_media(path) else 'nei'}")

    explanation = explain_date(path)
    selected_date = explanation.selected.date.isoformat() if explanation.selected.date else "-"
    lines.append(f"Valgt dato: {selected_date}")
    lines.append(f"Valgt kilde: {explanation.selected.source}")
    lines.append("Datokandidater:")
    for candidate in explanation.candidates:
        value = candidate.date.isoformat() if candidate.date else "-"
        lines.append(f"  {candidate.source}\t{value}\t{candidate.detail}")

    if is_jpeg_file(path):
        lines.extend(inspect_jpeg_metadata(path))
    elif path.suffix.lower() == ".avi":
        lines.extend(inspect_avi_metadata(path))
    else:
        lines.extend(inspect_text_dates(path))

    return MetadataInspection(path, tuple(lines))


def media_date(path: Path) -> MediaDate:
    return explain_date(path).selected


def explain_date(path: Path) -> DateExplanation:
    candidates: list[DateCandidate] = []

    exif_date = jpeg_exif_date(path)
    candidates.append(DateCandidate("metadata", exif_date, "JPEG EXIF"))
    if exif_date is not None:
        return DateExplanation(path, is_supported_media(path), MediaDate(exif_date, "metadata"), tuple(candidates))

    xmp_date = jpeg_xmp_date(path)
    candidates.append(DateCandidate("metadata", xmp_date, "JPEG XMP"))
    if xmp_date is not None:
        return DateExplanation(path, is_supported_media(path), MediaDate(xmp_date, "metadata"), tuple(candidates))

    video_date = video_metadata_date(path)
    candidates.append(DateCandidate("metadata", video_date, "Video metadata"))
    if video_date is not None:
        return DateExplanation(path, is_supported_media(path), MediaDate(video_date, "metadata"), tuple(candidates))

    filename_date = date_from_filename(path.name)
    candidates.append(DateCandidate("filename", filename_date, "Dato i filnavn"))
    if filename_date is not None:
        return DateExplanation(path, is_supported_media(path), MediaDate(filename_date, "filename"), tuple(candidates))

    try:
        mtime_date = dt.datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        mtime_date = None
    candidates.append(DateCandidate("mtime", mtime_date, "Filens endringsdato"))
    if mtime_date is not None:
        return DateExplanation(path, is_supported_media(path), MediaDate(mtime_date, "mtime"), tuple(candidates))

    selected = MediaDate(None, "unknown")
    candidates.append(DateCandidate("unknown", None, "Ingen dato funnet"))
    return DateExplanation(path, is_supported_media(path), selected, tuple(candidates))


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


def inspect_jpeg_metadata(path: Path) -> list[str]:
    lines: list[str] = ["JPEG metadata:"]
    segments = list(_jpeg_segments(path))
    if not segments:
        lines.append("  Fant ingen lesbare JPEG-segmenter.")
        return lines
    app_segments = [(marker, segment) for marker, segment in segments if 0xE0 <= marker <= 0xEF]
    for marker, segment in app_segments:
        label = f"APP{marker - 0xE0}"
        prefix = printable_sample(segment[:80])
        lines.append(f"  {label}: {len(segment)} bytes: {prefix}")
    lines.append(f"  EXIF dato: {jpeg_exif_date(path) or '-'}")
    lines.append(f"  XMP dato: {jpeg_xmp_date(path) or '-'}")
    lines.extend(inspect_text_dates(path))
    return lines


def jpeg_xmp_date(path: Path) -> dt.date | None:
    for segment in _jpeg_app1_segments(path):
        xmp_prefix = b"http://ns.adobe.com/xap/1.0/\x00"
        if not segment.startswith(xmp_prefix):
            continue
        xmp = segment[len(xmp_prefix) :]
        parsed = _date_from_xmp(xmp)
        if parsed is not None:
            return parsed
    for marker, segment in _jpeg_segments(path):
        if not 0xE0 <= marker <= 0xEF:
            continue
        if segment.startswith(b"Photoshop 3.0\x00"):
            xmp = _xmp_from_photoshop_app13(segment)
            if xmp is not None:
                parsed = _date_from_xmp(xmp)
                if parsed is not None:
                    return parsed
        # Fast prefilter: avoid parsing every APP segment.
        lower = segment.lower()
        if (
            b"xmp" not in lower
            and b"createdate" not in lower
            and b"metadata" not in lower
            and b"rdf:rdf" not in lower
            and b"<x:xmpmeta" not in lower
        ):
            continue
        parsed = _date_from_xmp(segment)
        if parsed is not None:
            return parsed
    return None


def _xmp_from_photoshop_app13(segment: bytes) -> bytes | None:
    # JPEG APP13 (0xFFED) can store Photoshop Image Resource Blocks.
    # XMP is typically resource ID 0x0424.
    prefix = b"Photoshop 3.0\x00"
    if not segment.startswith(prefix):
        return None
    offset = len(prefix)
    while offset + 12 <= len(segment):
        if segment[offset : offset + 4] != b"8BIM":
            break
        offset += 4
        resource_id = int.from_bytes(segment[offset : offset + 2], "big")
        offset += 2
        if offset >= len(segment):
            break
        name_len = segment[offset]
        offset += 1
        offset += name_len
        if offset % 2 == 1:
            offset += 1
        if offset + 4 > len(segment):
            break
        size = int.from_bytes(segment[offset : offset + 4], "big")
        offset += 4
        if offset + size > len(segment):
            break
        data = segment[offset : offset + size]
        offset += size
        if offset % 2 == 1:
            offset += 1
        if resource_id == 0x0424 and data:
            return data
    return None


def _jpeg_segments(path: Path):
    try:
        data = path.read_bytes()
    except OSError:
        return

    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return

    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            return
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD9, 0xDA}:
            return
        if offset + 2 > len(data):
            return
        length = int.from_bytes(data[offset : offset + 2], "big")
        offset += 2
        if length < 2 or offset + length - 2 > len(data):
            return
        segment = data[offset : offset + length - 2]
        offset += length - 2
        yield marker, segment


def is_jpeg_file(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return is_jpeg_header(fh.read(2))
    except OSError:
        return False


def _jpeg_app1_segments(path: Path):
    for marker, segment in _jpeg_segments(path):
        if marker == 0xE1:
            yield segment


def _date_from_xmp(xmp: bytes) -> dt.date | None:
    text = xmp.decode("utf-8", errors="ignore")
    for pattern in (
        r"(?:exif|xmp|photoshop):(?:DateTimeOriginal|DateTimeDigitized|CreateDate|ModifyDate|DateCreated)="
        r"['\"](?P<value>[^'\"]+)['\"]",
        r"<(?:exif|xmp|photoshop):(?:DateTimeOriginal|DateTimeDigitized|CreateDate|ModifyDate|DateCreated)>"
        r"(?P<value>[^<]+)</",
    ):
        for match in re.finditer(pattern, text):
            parsed = _parse_xmp_date(match.group("value"))
            if parsed is not None:
                return parsed

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    wanted_names = {
        "DateTimeOriginal",
        "DateTimeDigitized",
        "CreateDate",
        "ModifyDate",
        "DateCreated",
    }
    for element in root.iter():
        for key, value in element.attrib.items():
            if key.rsplit("}", 1)[-1] in wanted_names:
                parsed = _parse_xmp_date(value)
                if parsed is not None:
                    return parsed
        if element.tag.rsplit("}", 1)[-1] in wanted_names and element.text:
            parsed = _parse_xmp_date(element.text)
            if parsed is not None:
                return parsed
    return None


def _parse_xmp_date(value: str) -> dt.date | None:
    value = value.strip()
    patterns = [
        r"(?P<y>19\d{2}|20\d{2})-(?P<m>1[0-2]|0[1-9])-(?P<d>3[01]|[12]\d|0[1-9])",
        r"(?P<y>19\d{2}|20\d{2}):(?P<m>1[0-2]|0[1-9]):(?P<d>3[01]|[12]\d|0[1-9])",
    ]
    for pattern in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        try:
            return dt.date(
                int(match.group("y")), int(match.group("m")), int(match.group("d"))
            )
        except ValueError:
            continue
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


def _orientation_from_tiff(tiff: bytes) -> int | None:
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
    value = _parse_exif_short(values.get(0x0112), endian)
    if value is None or value < 1 or value > 8:
        return None
    return value


def _camera_info_from_tiff(tiff: bytes) -> CameraInfo | None:
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
    make = _parse_exif_ascii(values.get(0x010F))
    model = _parse_exif_ascii(values.get(0x0110))
    if make is None and model is None:
        return None
    return CameraInfo(make, model)


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
    match = re.match(r"(?P<y>\d{4})[:/-](?P<m>\d{2})[:/-](?P<d>\d{2})", text)
    if not match:
        return None
    try:
        return dt.date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
    except ValueError:
        return None


def _parse_exif_short(value: bytes | int | None, endian: str) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, bytes) or len(value) < 2:
        return None
    return struct.unpack(endian + "H", value[:2])[0]


def _parse_exif_ascii(value: bytes | int | None) -> str | None:
    if not isinstance(value, bytes):
        return None
    text = value.rstrip(b"\x00").decode("ascii", errors="ignore").strip()
    return text or None


def video_metadata_date(path: Path) -> dt.date | None:
    if path.suffix.lower() == ".avi":
        return avi_metadata_date(path)
    if path.suffix.lower() not in {".mp4", ".mp", ".mov", ".m4v", ".3gp"}:
        return None
    try:
        with path.open("rb") as fh:
            return _video_metadata_date_from_stream(fh, path.stat().st_size)
    except OSError:
        return None


def _video_metadata_date_from_stream(fh, file_size: int) -> dt.date | None:
    # ISO BMFF/QuickTime files store creation time in mvhd atoms. This covers
    # common MP4, MOV, M4V and 3GP files from phones and cameras.
    for atom_type, payload_offset, payload_size in _iter_atoms(fh, 0, file_size):
        if atom_type == b"moov":
            found = _find_mvhd_date(fh, payload_offset, payload_size)
            if found is not None:
                return found
    return None


def _find_mvhd_date(fh, start: int, size: int) -> dt.date | None:
    for atom_type, payload_offset, payload_size in _iter_atoms(fh, start, size):
        if atom_type == b"mvhd":
            return _read_mvhd_creation_date(fh, payload_offset, payload_size)
    return None


def _iter_atoms(fh, start: int, size: int):
    end = start + size
    offset = start
    while offset + 8 <= end:
        fh.seek(offset)
        header = fh.read(8)
        if len(header) != 8:
            return
        atom_size = int.from_bytes(header[:4], "big")
        atom_type = header[4:8]
        header_size = 8
        if atom_size == 1:
            extended = fh.read(8)
            if len(extended) != 8:
                return
            atom_size = int.from_bytes(extended, "big")
            header_size = 16
        elif atom_size == 0:
            atom_size = end - offset

        if atom_size < header_size:
            return
        payload_offset = offset + header_size
        payload_size = atom_size - header_size
        yield atom_type, payload_offset, payload_size
        offset += atom_size


def _read_mvhd_creation_date(fh, payload_offset: int, payload_size: int) -> dt.date | None:
    if payload_size < 12:
        return None
    fh.seek(payload_offset)
    version_flags = fh.read(4)
    if len(version_flags) != 4:
        return None
    version = version_flags[0]
    if version == 1:
        if payload_size < 20:
            return None
        raw = fh.read(8)
        if len(raw) != 8:
            return None
        seconds = int.from_bytes(raw, "big")
    elif version == 0:
        raw = fh.read(4)
        if len(raw) != 4:
            return None
        seconds = int.from_bytes(raw, "big")
    else:
        return None
    return _quicktime_seconds_to_date(seconds)


def _quicktime_seconds_to_date(seconds: int) -> dt.date | None:
    # QuickTime epoch is 1904-01-01 UTC. Ignore zero timestamps, which usually
    # mean that the metadata was not set.
    if seconds <= 0:
        return None
    epoch = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)
    try:
        value = epoch + dt.timedelta(seconds=seconds)
    except OverflowError:
        return None
    if value.year < 1970 or value.year > dt.datetime.now(dt.timezone.utc).year + 1:
        return None
    return value.date()


def avi_metadata_date(path: Path) -> dt.date | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"AVI ":
        return None

    values = _riff_date_values(data)
    for key in (b"IDIT", b"ICRD", b"IDAT"):
        parsed = _parse_avi_date(values.get(key))
        if parsed is not None:
            return parsed
    return None


def inspect_avi_metadata(path: Path) -> list[str]:
    lines = ["AVI metadata:"]
    try:
        data = path.read_bytes()
    except OSError as exc:
        return lines + [f"  Kunne ikke lese fil: {exc}"]
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"AVI ":
        return lines + ["  Ikke en lesbar RIFF AVI-fil."]
    values = _riff_date_values(data)
    if values:
        for key, value in sorted(values.items()):
            text = value.decode("utf-8", errors="ignore")
            lines.append(f"  {key.decode('ascii', errors='ignore')}: {text}")
    else:
        lines.append("  Fant ingen IDIT/ICRD/IDAT-felt.")
    lines.append(f"  AVI dato: {avi_metadata_date(path) or '-'}")
    lines.extend(inspect_text_dates(path))
    return lines


def _riff_date_values(data: bytes) -> dict[bytes, bytes]:
    values: dict[bytes, bytes] = {}
    _read_riff_chunks(data, 12, len(data), values)
    return values


def _read_riff_chunks(data: bytes, start: int, end: int, values: dict[bytes, bytes]) -> None:
    offset = start
    while offset + 8 <= end:
        chunk_id = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        payload_start = offset + 8
        payload_end = min(payload_start + size, end)
        if payload_start > end or payload_end > end:
            return

        if chunk_id == b"LIST" and size >= 4:
            list_type = data[payload_start : payload_start + 4]
            if list_type == b"INFO":
                _read_info_chunks(data, payload_start + 4, payload_end, values)
            else:
                _read_riff_chunks(data, payload_start + 4, payload_end, values)
        elif chunk_id in {b"IDIT", b"ICRD", b"IDAT"}:
            values[chunk_id] = data[payload_start:payload_end].rstrip(b"\x00 ")
        offset = payload_end + (size % 2)


def _read_info_chunks(data: bytes, start: int, end: int, values: dict[bytes, bytes]) -> None:
    offset = start
    while offset + 8 <= end:
        chunk_id = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        payload_start = offset + 8
        payload_end = min(payload_start + size, end)
        if payload_start > end or payload_end > end:
            return
        values[chunk_id] = data[payload_start:payload_end].rstrip(b"\x00 ")
        offset = payload_end + (size % 2)


def _parse_avi_date(value: bytes | None) -> dt.date | None:
    if not value:
        return None
    text = value.decode("utf-8", errors="ignore").strip()
    patterns = [
        r"(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
        r"(?P<month_name>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
        r"(?P<d>3[01]|[12]\d|0?[1-9])\s+"
        r"\d{2}:\d{2}:\d{2}\s+"
        r"(?P<y>19\d{2}|20\d{2})",
        r"(?P<y>19\d{2}|20\d{2})[-:/\. ](?P<m>1[0-2]|0?[1-9])[-:/\. ](?P<d>3[01]|[12]\d|0?[1-9])",
        r"(?P<d>3[01]|[12]\d|0?[1-9])[-:/\. ](?P<m>1[0-2]|0?[1-9])[-:/\. ](?P<y>19\d{2}|20\d{2})",
        r"(?P<m>1[0-2]|0?[1-9])[-:/\. ](?P<d>3[01]|[12]\d|0?[1-9])[-:/\. ](?P<y>19\d{2}|20\d{2})",
        r"(?P<y>19\d{2}|20\d{2}):(?P<m>1[0-2]|0?[1-9]):(?P<d>3[01]|[12]\d|0?[1-9])",
    ]
    for pattern in patterns:
        match = re.search(pattern + r"(?!\d)", text)
        if not match:
            continue
        month = match.groupdict().get("m")
        if month is None:
            month = str(_month_number(match.group("month_name")))
        try:
            return dt.date(
                int(match.group("y")), int(month), int(match.group("d"))
            )
        except ValueError:
            continue
    return None


def _month_number(month_name: str) -> int:
    months = {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }
    return months[month_name]


def inspect_text_dates(path: Path) -> list[str]:
    lines = ["Tekstlige datotreff:"]
    try:
        data = path.read_bytes()
    except OSError as exc:
        return lines + [f"  Kunne ikke lese fil: {exc}"]
    text = data[:2_000_000].decode("latin-1", errors="ignore")
    patterns = [
        r"(19\d{2}|20\d{2})[-:/](0[1-9]|1[0-2])[-:/]([0-3]\d)",
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+([0-3]?\d)\s+\d{2}:\d{2}:\d{2}\s+(19\d{2}|20\d{2})",
        r"(DateTimeOriginal|DateTimeDigitized|CreateDate|ModifyDate|DateCreated)[^\\n\\r<]{0,80}",
    ]
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            sample = printable_sample(match.group(0).encode("latin-1", errors="ignore"))
            if sample in seen:
                continue
            seen.add(sample)
            lines.append(f"  {sample}")
            if len(seen) >= 20:
                break
        if len(seen) >= 20:
            break
    if not seen:
        lines.append("  Ingen tekstlige datotreff i de første 2 MB.")
    return lines


def printable_sample(data: bytes) -> str:
    text = data.decode("utf-8", errors="ignore")
    text = "".join(char if char.isprintable() else "." for char in text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:160] or "-"
