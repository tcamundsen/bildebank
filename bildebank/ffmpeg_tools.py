from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .downloaded_artifacts import (
    download_https_file,
    ensure_directory_without_links,
    reject_directory_link,
    safe_extract_zip,
)


FFMPEG_VERSION = "8.1.2"
FFMPEG_ARCHIVE_NAME = f"ffmpeg-{FFMPEG_VERSION}-essentials_build.zip"
FFMPEG_ARCHIVE_URL = (
    "https://github.com/GyanD/codexffmpeg/releases/download/"
    f"{FFMPEG_VERSION}/{FFMPEG_ARCHIVE_NAME}"
)
FFMPEG_ARCHIVE_SHA256 = "db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec"
FFMPEG_ARCHIVE_MAX_BYTES = 512 * 1024 * 1024
FFMPEG_EXTRACT_MAX_BYTES = 2 * 1024 * 1024 * 1024
FFMPEG_EXTRACT_MAX_MEMBERS = 10_000
FFMPEG_VALIDATION_TIMEOUT_SECONDS = 30
TOOLS_DIRNAME = "bildebank-tools"
FFMPEG_DIRNAME = "ffmpeg"


@dataclass(frozen=True)
class FFmpegTools:
    ffmpeg: Path
    ffprobe: Path
    version: str
    managed: bool


@dataclass(frozen=True)
class FFmpegInstallResult:
    tools: FFmpegTools
    installed: bool


def managed_ffmpeg_dir(repo_root: Path) -> Path:
    return repo_root / TOOLS_DIRNAME / FFMPEG_DIRNAME / FFMPEG_VERSION


def managed_ffmpeg_paths(repo_root: Path) -> tuple[Path, Path]:
    binary_dir = managed_ffmpeg_dir(repo_root) / "bin"
    return binary_dir / "ffmpeg.exe", binary_dir / "ffprobe.exe"


def validate_ffmpeg_tools(ffmpeg: Path | str, ffprobe: Path | str, *, managed: bool = False) -> FFmpegTools:
    ffmpeg_path = Path(ffmpeg)
    ffprobe_path = Path(ffprobe)
    for label, path in (("FFmpeg", ffmpeg_path), ("FFprobe", ffprobe_path)):
        if not path.is_file():
            raise FileNotFoundError(f"Fant ikke {label}: {path}")

    version_result = _run_tool([str(ffmpeg_path), "-hide_banner", "-version"], "FFmpeg")
    version_line = version_result.stdout.splitlines()[0].strip() if version_result.stdout else ""
    if not version_line:
        raise RuntimeError(f"Kunne ikke lese FFmpeg-versjon: {ffmpeg_path}")

    probe_result = _run_tool([str(ffprobe_path), "-hide_banner", "-version"], "FFprobe")
    if not probe_result.stdout.strip():
        raise RuntimeError(f"Kunne ikke lese FFprobe-versjon: {ffprobe_path}")

    encoders = _run_tool([str(ffmpeg_path), "-hide_banner", "-encoders"], "FFmpeg")
    if "libx264" not in encoders.stdout:
        raise RuntimeError(f"FFmpeg mangler H.264-encoderen libx264: {ffmpeg_path}")
    return FFmpegTools(ffmpeg_path, ffprobe_path, version_line, managed)


def resolve_ffmpeg_tools(repo_root: Path) -> FFmpegTools:
    managed_ffmpeg, managed_ffprobe = managed_ffmpeg_paths(repo_root)
    if managed_ffmpeg.exists() or managed_ffprobe.exists():
        return validate_ffmpeg_tools(managed_ffmpeg, managed_ffprobe, managed=True)

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return validate_ffmpeg_tools(ffmpeg, ffprobe)
    raise FileNotFoundError(
        "Fant ikke FFmpeg og FFprobe. På Windows kan du kjøre "
        "`bildebank ffmpeg-install` fra programmappen."
    )


def install_managed_ffmpeg(repo_root: Path, *, force: bool = False) -> FFmpegInstallResult:
    destination = managed_ffmpeg_dir(repo_root)
    ffmpeg_path, ffprobe_path = managed_ffmpeg_paths(repo_root)
    reject_directory_link(destination, label="FFmpeg-mappen")
    if destination.exists() and not force:
        try:
            tools = validate_ffmpeg_tools(ffmpeg_path, ffprobe_path, managed=True)
        except (FileNotFoundError, OSError, RuntimeError):
            pass
        else:
            return FFmpegInstallResult(tools, installed=False)

    tools_root = ensure_directory_without_links(
        destination.parent,
        label="FFmpeg-verktøymappen",
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        archive_path = tmp_dir / FFMPEG_ARCHIVE_NAME
        _download_file(
            FFMPEG_ARCHIVE_URL,
            archive_path,
            max_bytes=FFMPEG_ARCHIVE_MAX_BYTES,
        )
        actual_hash = _sha256_file(archive_path)
        if actual_hash != FFMPEG_ARCHIVE_SHA256:
            raise RuntimeError(
                "FFmpeg-arkivet har feil SHA-256: "
                f"forventet {FFMPEG_ARCHIVE_SHA256}, fikk {actual_hash}."
            )

        extract_dir = tmp_dir / "extract"
        extract_dir.mkdir()
        _safe_extract_zip(archive_path, extract_dir)
        extracted_root = _find_ffmpeg_root(extract_dir)

        staging = tools_root / f".{FFMPEG_VERSION}.installing-{uuid.uuid4().hex}"
        backup = tools_root / f".{FFMPEG_VERSION}.previous-{uuid.uuid4().hex}"
        shutil.copytree(extracted_root, staging)
        staged_ffmpeg = staging / "bin" / "ffmpeg.exe"
        staged_ffprobe = staging / "bin" / "ffprobe.exe"
        validate_ffmpeg_tools(staged_ffmpeg, staged_ffprobe, managed=True)

        try:
            if destination.exists():
                destination.rename(backup)
            staging.rename(destination)
        except BaseException:
            if backup.exists() and not destination.exists():
                backup.rename(destination)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        if backup.exists():
            shutil.rmtree(backup)

    return FFmpegInstallResult(
        validate_ffmpeg_tools(ffmpeg_path, ffprobe_path, managed=True),
        installed=True,
    )


def _run_tool(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FFMPEG_VALIDATION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{label}-kontrollen brukte mer enn "
            f"{FFMPEG_VALIDATION_TIMEOUT_SECONDS} sekunder."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Kunne ikke starte {label}: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(message or f"{label} feilet med exitkode {result.returncode}")
    return result


def _download_file(url: str, destination: Path, *, max_bytes: int) -> None:
    download_https_file(
        url,
        destination,
        user_agent="Bildebank FFmpeg installer",
        max_bytes=max_bytes,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_zip(archive_path: Path, destination: Path) -> None:
    safe_extract_zip(
        archive_path,
        destination,
        label="FFmpeg",
        max_members=FFMPEG_EXTRACT_MAX_MEMBERS,
        max_uncompressed_bytes=FFMPEG_EXTRACT_MAX_BYTES,
    )


def _find_ffmpeg_root(extracted: Path) -> Path:
    candidates = sorted(
        path.parent.parent
        for path in extracted.rglob("ffmpeg.exe")
        if path.parent.name.casefold() == "bin" and (path.parent / "ffprobe.exe").is_file()
    )
    if len(candidates) != 1:
        raise RuntimeError("FFmpeg-arkivet har ikke én entydig bin-mappe med ffmpeg.exe og ffprobe.exe.")
    return candidates[0]
