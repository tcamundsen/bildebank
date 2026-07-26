from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import bildebank.thumbnails as thumbnails
from bildebank.collection_paths import CollectionDirectoryError
from bildebank.db import init_database
from bildebank.thumbnails import (
    cleanup_legacy_thumbnails,
    plan_legacy_thumbnail_cleanup,
)
from tests.cli_helpers import capture_cli


def write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_legacy_thumbnail_cleanup_only_deletes_exact_old_layout(
    tmp_path: Path,
) -> None:
    target = tmp_path / "samling"
    target.mkdir()
    old_jpg = target / "thumbs" / "2024" / "01" / "image.jpg"
    old_jpeg = target / "thumbs" / "udatert" / "scan.JPEG"
    old_temp = target / "thumbs" / "2024" / "02" / ".avbrutt.jpg.tmp"
    current = target / "thumbs" / "v2" / "2024" / "01" / "current.jpg"
    unexpected_file = target / "thumbs" / "2024" / "01" / "behold.txt"
    unexpected_month = target / "thumbs" / "2024" / "13" / "behold.jpg"
    unexpected_root = target / "thumbs" / "annet" / "behold.jpg"
    original = target / "2024" / "01" / "image.jpg"
    for path, content in (
        (old_jpg, b"old-jpg"),
        (old_jpeg, b"old-jpeg"),
        (old_temp, b"old-temp"),
        (current, b"current"),
        (unexpected_file, b"text"),
        (unexpected_month, b"month"),
        (unexpected_root, b"root"),
        (original, b"original"),
    ):
        write_file(path, content)

    plan = plan_legacy_thumbnail_cleanup(target)

    assert plan.file_count == 3
    assert plan.total_bytes == len(b"old-jpgold-jpegold-temp")
    assert {item.relative_path for item in plan.files} == {
        Path("thumbs/2024/01/image.jpg"),
        Path("thumbs/udatert/scan.JPEG"),
        Path("thumbs/2024/02/.avbrutt.jpg.tmp"),
    }

    stats = cleanup_legacy_thumbnails(target)

    assert stats.deleted == 3
    assert stats.deleted_bytes == plan.total_bytes
    assert stats.errors == 0
    assert stats.skipped == 0
    assert not old_jpg.exists()
    assert not old_jpeg.exists()
    assert not old_temp.exists()
    assert current.read_bytes() == b"current"
    assert unexpected_file.read_bytes() == b"text"
    assert unexpected_month.read_bytes() == b"month"
    assert unexpected_root.read_bytes() == b"root"
    assert original.read_bytes() == b"original"


def test_legacy_thumbnail_cleanup_removes_empty_old_directories(
    tmp_path: Path,
) -> None:
    target = tmp_path / "samling"
    old_thumbnail = target / "thumbs" / "2024" / "01" / "image.jpg"
    current_root = target / "thumbs" / "v2"
    write_file(old_thumbnail, b"old")
    current_root.mkdir(parents=True)

    cleanup_legacy_thumbnails(target)

    assert not (target / "thumbs" / "2024" / "01").exists()
    assert not (target / "thumbs" / "2024").exists()
    assert current_root.is_dir()
    assert (target / "thumbs").is_dir()


def test_legacy_thumbnail_cleanup_rejects_linked_root(tmp_path: Path) -> None:
    target = tmp_path / "samling"
    outside = tmp_path / "utenfor"
    target.mkdir()
    write_file(outside / "2024" / "01" / "image.jpg", b"outside")
    try:
        (target / "thumbs").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Kan ikke opprette symlink: {exc}")

    with pytest.raises(CollectionDirectoryError, match="vanlig mappe uten lenker"):
        plan_legacy_thumbnail_cleanup(target)

    assert (outside / "2024" / "01" / "image.jpg").read_bytes() == b"outside"


def test_legacy_thumbnail_cleanup_ignores_linked_file(tmp_path: Path) -> None:
    target = tmp_path / "samling"
    outside = tmp_path / "utenfor.jpg"
    leaf = target / "thumbs" / "2024" / "01"
    leaf.mkdir(parents=True)
    outside.write_bytes(b"outside")
    linked = leaf / "image.jpg"
    try:
        linked.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Kan ikke opprette symlink: {exc}")

    plan = plan_legacy_thumbnail_cleanup(target)
    stats = cleanup_legacy_thumbnails(target)

    assert plan.file_count == 0
    assert plan.unsafe_paths == (Path("thumbs/2024/01/image.jpg"),)
    assert stats.deleted == 0
    assert linked.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_legacy_thumbnail_cleanup_does_not_enter_linked_year(
    tmp_path: Path,
) -> None:
    target = tmp_path / "samling"
    outside = tmp_path / "utenfor"
    (target / "thumbs").mkdir(parents=True)
    write_file(outside / "01" / "image.jpg", b"outside")
    linked_year = target / "thumbs" / "2024"
    try:
        linked_year.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Kan ikke opprette symlink: {exc}")

    plan = plan_legacy_thumbnail_cleanup(target)
    stats = cleanup_legacy_thumbnails(target)

    assert plan.file_count == 0
    assert plan.unsafe_paths == (Path("thumbs/2024"),)
    assert stats.deleted == 0
    assert linked_year.is_symlink()
    assert (outside / "01" / "image.jpg").read_bytes() == b"outside"


def test_legacy_scan_uses_path_stat_for_windows_compatible_identity(
    tmp_path: Path,
) -> None:
    target = tmp_path / "samling"
    old_thumbnail = target / "thumbs" / "2024" / "01" / "image.jpg"
    write_file(old_thumbnail, b"old")
    fake_entry = SimpleNamespace(
        name="image.jpg",
        stat=lambda **_kwargs: pytest.fail(
            "Filidentiteten må ikke hentes fra DirEntry.stat"
        ),
    )
    files: list[thumbnails.LegacyThumbnailFile] = []
    unsafe_paths: list[Path] = []

    with patch(
        "bildebank.thumbnails._scandir_sorted",
        return_value=[fake_entry],
    ):
        thumbnails._collect_legacy_thumbnail_files(
            target,
            Path("thumbs/2024/01"),
            files,
            unsafe_paths,
        )

    assert unsafe_paths == []
    assert len(files) == 1
    assert files[0].path_stat.st_ino == old_thumbnail.stat().st_ino


def test_cleanup_thumbnails_cli_is_dry_run_until_apply(tmp_path: Path) -> None:
    target = tmp_path / "samling"
    init_database(target)
    old_thumbnail = target / "thumbs" / "2024" / "01" / "image.jpg"
    current_thumbnail = target / "thumbs" / "v2" / "2024" / "01" / "current.jpg"
    write_file(old_thumbnail, b"old")
    write_file(current_thumbnail, b"current")

    code, stdout, stderr = capture_cli(
        ["--target", str(target), "cleanup-thumbnails"]
    )

    assert code == 0
    assert stderr == ""
    assert "filer=1" in stdout
    assert "Ingen filer ble slettet" in stdout
    assert old_thumbnail.is_file()

    code, stdout, stderr = capture_cli(
        ["--target", str(target), "cleanup-thumbnails", "--apply"]
    )

    assert code == 0
    assert stderr == ""
    assert "slettet=1" in stdout
    assert not old_thumbnail.exists()
    assert current_thumbnail.read_bytes() == b"current"
