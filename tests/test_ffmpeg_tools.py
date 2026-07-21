from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from bildebank import ffmpeg_tools
from bildebank.ffmpeg_tools import FFmpegTools


def test_windows_archive_is_version_and_hash_pinned() -> None:
    assert ffmpeg_tools.FFMPEG_VERSION == "8.1.2"
    assert ffmpeg_tools.FFMPEG_ARCHIVE_URL.endswith(
        "/8.1.2/ffmpeg-8.1.2-essentials_build.zip"
    )
    assert ffmpeg_tools.FFMPEG_ARCHIVE_SHA256 == (
        "db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec"
    )


def test_install_managed_ffmpeg_verifies_archive_and_installs_complete_tree(tmp_path: Path) -> None:
    archive = tmp_path / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("ffmpeg-8.1.2-essentials_build/bin/ffmpeg.exe", b"ffmpeg")
        output.writestr("ffmpeg-8.1.2-essentials_build/bin/ffprobe.exe", b"ffprobe")
        output.writestr("ffmpeg-8.1.2-essentials_build/LICENSE", b"license")
    expected_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    repo_root = tmp_path / "repo"

    def download(_url: str, destination: Path) -> None:
        shutil.copyfile(archive, destination)

    def validate(ffmpeg: Path | str, ffprobe: Path | str, *, managed: bool = False) -> FFmpegTools:
        assert Path(ffmpeg).is_file()
        assert Path(ffprobe).is_file()
        return FFmpegTools(Path(ffmpeg), Path(ffprobe), "ffmpeg version 8.1.2", managed)

    with (
        patch.object(ffmpeg_tools, "FFMPEG_ARCHIVE_SHA256", expected_hash),
        patch.object(ffmpeg_tools, "_download_file", side_effect=download),
        patch.object(ffmpeg_tools, "validate_ffmpeg_tools", side_effect=validate),
    ):
        result = ffmpeg_tools.install_managed_ffmpeg(repo_root)

    assert result.installed
    assert result.tools.managed
    assert result.tools.ffmpeg.read_bytes() == b"ffmpeg"
    assert result.tools.ffprobe.read_bytes() == b"ffprobe"
    assert (result.tools.ffmpeg.parents[1] / "LICENSE").read_bytes() == b"license"


def test_install_managed_ffmpeg_rejects_wrong_archive_hash(tmp_path: Path) -> None:
    with patch.object(ffmpeg_tools, "_download_file", side_effect=lambda _url, path: path.write_bytes(b"wrong")):
        with pytest.raises(RuntimeError, match="feil SHA-256"):
            ffmpeg_tools.install_managed_ffmpeg(tmp_path / "repo")


def test_safe_extract_rejects_parent_path(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../ffmpeg.exe", b"bad")

    with pytest.raises(RuntimeError, match="utrygg filsti"):
        ffmpeg_tools._safe_extract_zip(archive, tmp_path / "extract")


def test_invalid_existing_install_is_replaced(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    destination = ffmpeg_tools.managed_ffmpeg_dir(repo_root)
    destination.mkdir(parents=True)
    (destination / "broken.txt").write_text("broken", encoding="utf-8")
    archive = tmp_path / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("build/bin/ffmpeg.exe", b"ffmpeg")
        output.writestr("build/bin/ffprobe.exe", b"ffprobe")
    expected_hash = hashlib.sha256(archive.read_bytes()).hexdigest()

    def validate(ffmpeg: Path | str, ffprobe: Path | str, *, managed: bool = False) -> FFmpegTools:
        if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
            raise FileNotFoundError("broken")
        return FFmpegTools(Path(ffmpeg), Path(ffprobe), "ffmpeg version 8.1.2", managed)

    with (
        patch.object(ffmpeg_tools, "FFMPEG_ARCHIVE_SHA256", expected_hash),
        patch.object(ffmpeg_tools, "_download_file", side_effect=lambda _url, path: shutil.copyfile(archive, path)),
        patch.object(ffmpeg_tools, "validate_ffmpeg_tools", side_effect=validate),
    ):
        result = ffmpeg_tools.install_managed_ffmpeg(repo_root)

    assert result.installed
    assert not (destination / "broken.txt").exists()
