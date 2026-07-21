from __future__ import annotations

import json
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
from .ffmpeg_tools import FFmpegTools
from .target_lock import TargetLock


VIDEO_PREVIEW_ROOT_NAME = "video-previews"
VIDEO_PREVIEW_PROFILE = "v1"


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
    target_path = Path(str(item["target_path"]))
    if target_path.suffix.casefold() != ".avi":
        return None
    preview_path = video_preview_absolute_path(target, str(item["sha256"]))
    try:
        return preview_path if preview_path.is_file() and preview_path.stat().st_size > 0 else None
    except OSError:
        return None


def active_avi_candidates(target: Path) -> list[Any]:
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                """
                SELECT id, target_path, target_path_key, stored_filename, sha256, size_bytes
                FROM files
                WHERE deleted_at IS NULL
                  AND lower(stored_filename) LIKE '%.avi'
                ORDER BY target_path_key
                """
            )
        )
    finally:
        conn.close()


def probe_video(ffprobe: Path | str, path: Path) -> VideoProbe:
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
    )
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
    original_path = db.absolute_target_path(target, Path(str(item["target_path"])))
    if original_path.suffix.casefold() != ".avi":
        raise ValueError(f"Videoavspillingskopi støttes bare for AVI: {original_path}")
    if not original_path.is_file():
        raise FileNotFoundError(f"AVI-originalen finnes ikke: {original_path}")

    output_path = video_preview_absolute_path(target, str(item["sha256"]))
    if not rebuild and output_path.is_file() and output_path.stat().st_size > 0:
        return output_path

    input_probe = probe_video(tools.ffprobe, original_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.partial")
    filters = ["scale=trunc(iw/2)*2:trunc(ih/2)*2"]
    if input_probe.field_order not in {"", "unknown", "progressive"}:
        filters.insert(0, "bwdif=mode=send_frame:parity=auto:deint=interlaced")
    command = [
        str(tools.ffmpeg),
        "-nostdin",
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
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"FFmpeg feilet med exitkode {result.returncode}")
        output_probe = probe_video(tools.ffprobe, temporary_path)
        _validate_video_preview(input_probe, output_probe)
        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise RuntimeError("FFmpeg laget en tom avspillingskopi.")
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
        candidates = active_avi_candidates(target)
        stats.total = len(candidates)
        if progress is not None:
            progress("start", 0, len(candidates), stats, None)
        current = 0
        for current, item in enumerate(candidates, 1):
            if limit is not None and stats.checked >= limit:
                break
            relative_path = db.relative_path(Path(str(item["target_path"])))
            stats.checked += 1
            preview_path = video_preview_absolute_path(target, str(item["sha256"]))
            if not rebuild and preview_path.is_file() and preview_path.stat().st_size > 0:
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
            except Exception as exc:  # noqa: BLE001 - continue with remaining AVI files
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
        if duration > 0:
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
