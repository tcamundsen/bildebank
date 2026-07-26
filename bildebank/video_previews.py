from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db
from .collection_paths import (
    COLLECTION_FILE_OK,
    CollectionFileAccessError,
    ensure_collection_directory_without_links,
    inspect_collection_file,
    is_active_collection_file_path,
    open_stable_collection_file,
    parse_collection_relative_path,
    same_collection_file_version,
)
from .ffmpeg_tools import FFmpegTools
from .target_lock import TargetLock


VIDEO_PREVIEW_ROOT_NAME = "video-previews"
VIDEO_PREVIEW_PROFILE = "v1"
VIDEO_PREVIEW_SOURCE_EXTENSIONS = frozenset({".avi", ".3gp"})
MAX_VIDEO_DECODE_PIXELS = 100_000_000
FFPROBE_TIMEOUT_SECONDS = 120
FFMPEG_MIN_TIMEOUT_SECONDS = 10 * 60
FFMPEG_DURATION_TIMEOUT_FACTOR = 20
FFMPEG_MAX_TIMEOUT_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class VideoProbe:
    duration: float
    width: int
    height: int
    video_codec: str
    pixel_format: str
    field_order: str
    audio_codec: str | None


@dataclass
class VideoPreviewStats:
    total: int = 0
    checked: int = 0
    created: int = 0
    skipped_current: int = 0
    errors: int = 0
    last_error_path: Path | None = None
    last_error_message: str | None = None


VideoPreviewProgress = Callable[[str, int, int, VideoPreviewStats, Path | None], None]


def video_preview_relative_path(sha256: str) -> Path:
    digest = sha256.casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"Ugyldig SHA-256 for videoavspillingskopi: {sha256!r}")
    return Path(VIDEO_PREVIEW_ROOT_NAME, VIDEO_PREVIEW_PROFILE, digest[:2], f"{digest}.mp4")


def video_preview_absolute_path(target: Path, sha256: str) -> Path:
    return target / video_preview_relative_path(sha256)


def existing_video_preview_path(target: Path, item: Any) -> Path | None:
    try:
        _require_active_video_path(item)
    except ValueError:
        return None
    preview_path = video_preview_absolute_path(target, str(item["sha256"]))
    return (
        preview_path
        if video_preview_is_valid(target, str(item["sha256"]))
        else None
    )


def active_video_preview_candidates(target: Path) -> list[Any]:
    conn = db.connect(target)
    try:
        rows = list(
            conn.execute(
                """
                SELECT id, target_path, target_path_key, stored_filename, sha256, size_bytes
                FROM files
                WHERE deleted_at IS NULL
                  AND (
                    lower(stored_filename) LIKE '%.avi'
                    OR lower(stored_filename) LIKE '%.3gp'
                  )
                ORDER BY target_path_key
                """
            )
        )
        for row in rows:
            _require_active_video_path(row)
        return rows
    finally:
        conn.close()


def probe_video(ffprobe: Path | str, path: Path) -> VideoProbe:
    try:
        result = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,pix_fmt,width,height,field_order,duration:format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"FFprobe brukte mer enn {FFPROBE_TIMEOUT_SECONDS} sekunder."
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"FFprobe feilet med exitkode {result.returncode}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FFprobe returnerte ugyldig JSON.") from exc
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise RuntimeError("FFprobe fant ingen mediestrømmer.")
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not isinstance(video, dict):
        raise RuntimeError("FFprobe fant ingen videostrøm.")
    width = _positive_int(video.get("width"), "videobredde")
    height = _positive_int(video.get("height"), "videohøyde")
    duration = _probe_duration(video, payload.get("format"))
    return VideoProbe(
        duration=duration,
        width=width,
        height=height,
        video_codec=str(video.get("codec_name") or ""),
        pixel_format=str(video.get("pix_fmt") or ""),
        field_order=str(video.get("field_order") or "unknown").casefold(),
        audio_codec=str(audio.get("codec_name") or "") if isinstance(audio, dict) else None,
    )


def ensure_video_preview(target: Path, item: Any, tools: FFmpegTools, *, rebuild: bool = False) -> Path:
    target = target.resolve()
    original_relative = _require_active_video_path(item)
    original = inspect_collection_file(target, original_relative)
    if original.status != COLLECTION_FILE_OK or original.path_stat is None:
        raise CollectionFileAccessError(
            original.message
            or f"Videooriginalen er ikke en vanlig fil uten lenker: {original.path}"
        )
    original_path = original.path

    output_path = video_preview_absolute_path(target, str(item["sha256"]))
    if not rebuild and video_preview_is_valid(target, str(item["sha256"])):
        return output_path

    input_probe = probe_video(tools.ffprobe, original_path)
    if input_probe.width * input_probe.height > MAX_VIDEO_DECODE_PIXELS:
        raise RuntimeError(
            "Videoen er for stor til sikker dekoding "
            f"({input_probe.width}x{input_probe.height} piksler, "
            f"grense={MAX_VIDEO_DECODE_PIXELS})."
        )
    output_relative = video_preview_relative_path(str(item["sha256"]))
    ensure_collection_directory_without_links(target, output_relative.parent)
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.partial")
    filters = [
        "scale=trunc(iw/2)*2:trunc(ih/2)*2:out_range=tv",
        "format=pix_fmts=yuv420p",
    ]
    if input_probe.field_order not in {"", "unknown", "progressive"}:
        filters.insert(0, "bwdif=mode=send_frame:parity=auto:deint=interlaced")
    command = [
        str(tools.ffmpeg),
        "-nostdin",
        "-n",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(original_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-map_metadata",
        "0",
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "tv",
        "-vf",
        ",".join(filters),
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temporary_path),
    ]
    try:
        timeout_seconds = _ffmpeg_timeout_seconds(input_probe.duration)
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"FFmpeg brukte mer enn {timeout_seconds} sekunder."
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"FFmpeg feilet med exitkode {result.returncode}")
        temporary_relative = temporary_path.relative_to(target)
        if not _is_valid_mp4_file(target, temporary_relative):
            raise RuntimeError("FFmpeg laget ikke en gyldig MP4-container.")
        output_probe = probe_video(tools.ffprobe, temporary_path)
        _validate_video_preview(input_probe, output_probe)
        original_after = inspect_collection_file(target, original_relative)
        if (
            original_after.status != COLLECTION_FILE_OK
            or original_after.path_stat is None
            or not same_collection_file_version(
                original.path_stat,
                original_after.path_stat,
            )
        ):
            raise RuntimeError(
                "Videooriginalen ble endret eller utrygg under konverteringen."
            )
        ensure_collection_directory_without_links(target, output_relative.parent)
        os.replace(temporary_path, output_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return output_path


def run_make_video_previews(
    target: Path,
    tools: FFmpegTools | None,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    verbose: bool = False,
    rebuild: bool = False,
    progress: VideoPreviewProgress | None = None,
    target_locked: bool = False,
) -> VideoPreviewStats:
    stats = VideoPreviewStats()
    lock = nullcontext() if dry_run or target_locked else TargetLock(target, command="make-video-previews")
    with lock:
        candidates = active_video_preview_candidates(target)
        stats.total = len(candidates)
        if progress is not None:
            progress("start", 0, len(candidates), stats, None)
        current = 0
        for current, item in enumerate(candidates, 1):
            if limit is not None and stats.checked >= limit:
                break
            relative_path = db.relative_path(Path(str(item["target_path"])))
            stats.checked += 1
            if not rebuild and video_preview_is_valid(target, str(item["sha256"])):
                stats.skipped_current += 1
                if progress is not None:
                    progress("check", current, len(candidates), stats, relative_path)
                continue
            if dry_run:
                if progress is not None:
                    progress("check", current, len(candidates), stats, relative_path)
                continue
            assert tools is not None
            try:
                ensure_video_preview(target, item, tools, rebuild=rebuild)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - continue with remaining video files
                stats.errors += 1
                stats.last_error_path = relative_path
                stats.last_error_message = str(exc)
                if verbose:
                    print(f"Feil ved videoavspillingskopi for {relative_path}: {exc}", file=sys.stderr)
                if progress is not None:
                    progress("error", current, len(candidates), stats, relative_path)
                continue
            stats.created += 1
            if progress is not None:
                progress("check", current, len(candidates), stats, relative_path)
        if progress is not None:
            progress("done", stats.checked, len(candidates), stats, None)
    return stats


def _positive_int(value: object, label: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"FFprobe returnerte ugyldig {label}.") from exc
    if parsed <= 0:
        raise RuntimeError(f"FFprobe returnerte ugyldig {label}.")
    return parsed


def _probe_duration(video: dict[str, object], format_value: object) -> float:
    values = [video.get("duration")]
    if isinstance(format_value, dict):
        values.append(format_value.get("duration"))
    for value in values:
        try:
            duration = float(str(value))
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    raise RuntimeError("FFprobe returnerte ingen gyldig videovarighet.")


def _validate_video_preview(original: VideoProbe, preview: VideoProbe) -> None:
    if preview.video_codec != "h264":
        raise RuntimeError(f"Avspillingskopien har uventet videokodek: {preview.video_codec or '-'}")
    if preview.pixel_format != "yuv420p":
        raise RuntimeError(f"Avspillingskopien har uventet pikselformat: {preview.pixel_format or '-'}")
    if preview.width % 2 or preview.height % 2:
        raise RuntimeError("Avspillingskopien har dimensjoner som ikke er partall.")
    if original.audio_codec is not None and preview.audio_codec != "aac":
        raise RuntimeError(f"Avspillingskopien har uventet lydkodek: {preview.audio_codec or '-'}")
    tolerance = max(2.0, original.duration * 0.02)
    if abs(preview.duration - original.duration) > tolerance:
        raise RuntimeError(
            "Avspillingskopien har uventet varighet: "
            f"original={original.duration:.3f}s, kopi={preview.duration:.3f}s."
        )


def video_preview_is_valid(target: Path, sha256: str) -> bool:
    try:
        return _is_valid_mp4_file(
            target,
            video_preview_relative_path(sha256),
        )
    except (CollectionFileAccessError, OSError, ValueError):
        return False


def _is_valid_mp4_file(target: Path, relative_path: Path) -> bool:
    inspection = inspect_collection_file(target, relative_path)
    if (
        inspection.status != COLLECTION_FILE_OK
        or inspection.path_stat is None
        or inspection.path_stat.st_size < 12
    ):
        return False
    with open_stable_collection_file(target, relative_path) as stream:
        return _is_complete_mp4_container(
            stream,
            inspection.path_stat.st_size,
        )


def _is_complete_mp4_container(stream: Any, size_bytes: int) -> bool:
    offset = 0
    box_types: list[bytes] = []
    while offset < size_bytes:
        stream.seek(offset)
        header = stream.read(8)
        if len(header) != 8:
            return False
        box_size = int.from_bytes(header[:4], "big")
        box_type = header[4:8]
        header_size = 8
        if box_size == 1:
            extended_size = stream.read(8)
            if len(extended_size) != 8:
                return False
            box_size = int.from_bytes(extended_size, "big")
            header_size = 16
        elif box_size == 0:
            box_size = size_bytes - offset
        if box_size < header_size or offset + box_size > size_bytes:
            return False
        box_types.append(box_type)
        offset += box_size
    return (
        offset == size_bytes
        and bool(box_types)
        and box_types[0] == b"ftyp"
        and b"moov" in box_types
        and b"mdat" in box_types
    )


def _require_active_video_path(item: Any) -> Path:
    relative_path = parse_collection_relative_path(str(item["target_path"]))
    if (
        not is_active_collection_file_path(relative_path)
        or relative_path.suffix.casefold() not in VIDEO_PREVIEW_SOURCE_EXTENSIONS
    ):
        raise ValueError(
            "Videoavspillingskopi støttes bare for aktive AVI- og 3GP-filer: "
            f"{relative_path.as_posix()}"
        )
    target_path_key = _optional_item_value(item, "target_path_key")
    if (
        target_path_key is not None
        and str(target_path_key) != db.relative_path_key(relative_path)
    ):
        file_id = _optional_item_value(item, "id")
        label = f"files #{file_id}" if file_id is not None else "files-raden"
        raise ValueError(
            f"{label} har target_path_key som ikke stemmer med target_path."
        )
    return relative_path


def _optional_item_value(item: Any, key: str) -> object | None:
    try:
        return item[key]
    except (IndexError, KeyError):
        return None


def _ffmpeg_timeout_seconds(duration: float) -> int:
    estimated = int(duration * FFMPEG_DURATION_TIMEOUT_FACTOR)
    return min(
        max(estimated, FFMPEG_MIN_TIMEOUT_SECONDS),
        FFMPEG_MAX_TIMEOUT_SECONDS,
    )
