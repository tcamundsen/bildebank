from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from bildebank import db
from bildebank.cli import main
from bildebank.config import AppConfig
from bildebank.derived_files import DERIVED_DELETE_REASON_MIGRATION
from bildebank.file_lifecycle import remove_file
from bildebank.media import sha256_file
from bildebank.pending_deletes import (
    cleanup_pending_deletes,
    enqueue_pending_delete,
    list_pending_deletes,
)
from bildebank.thumbnails import thumbnail_absolute_path, thumbnail_relative_path
from bildebank.unimport import run_unimport
from bildebank.video_previews import (
    video_preview_absolute_path,
    video_preview_relative_path,
)


def import_source(target: Path, source: Path) -> None:
    assert (
        main(
            [
                "--target",
                str(target),
                "import",
                "--name",
                source.name,
                "--quiet",
                str(source),
            ]
        )
        == 0
    )


def create_collection_with_source(
    tmp_path: Path,
    files: dict[str, bytes],
) -> tuple[Path, Path]:
    target = tmp_path / "target"
    source = tmp_path / "source"
    source.mkdir()
    for name, content in files.items():
        (source / name).write_bytes(content)
    assert main(["create", str(target)]) == 0
    import_source(target, source)
    return target, source


def write_cache_file(path: Path, content: bytes = b"derived") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def mark_as_schema_v19(target: Path) -> None:
    conn = db.connect(target)
    try:
        conn.execute(
            "UPDATE meta SET value = '19' WHERE key = 'schema_version'"
        )
        conn.commit()
    finally:
        conn.close()


def test_unimport_removes_thumbnail_and_video_preview(tmp_path: Path) -> None:
    target, source = create_collection_with_source(
        tmp_path,
        {
            "IMG_20240102.jpg": b"image",
            "VID_20240103.avi": b"video",
        },
    )
    conn = db.connect(target)
    try:
        rows = list(
            conn.execute(
                "SELECT target_path, stored_filename, sha256 FROM files ORDER BY id"
            )
        )
    finally:
        conn.close()

    image_row = next(
        row for row in rows if str(row["stored_filename"]).endswith(".jpg")
    )
    video_row = next(
        row for row in rows if str(row["stored_filename"]).endswith(".avi")
    )
    thumbnail = thumbnail_absolute_path(
        target,
        Path(str(image_row["target_path"])),
    )
    preview = video_preview_absolute_path(target, str(video_row["sha256"]))
    write_cache_file(thumbnail, b"thumbnail")
    write_cache_file(preview, b"preview")

    result = run_unimport(
        target,
        source.name,
        config=AppConfig(),
        dry_run=False,
        confirm=lambda _plan: True,
    )

    assert result.applied
    assert not thumbnail.exists()
    assert not preview.exists()
    assert list_pending_deletes(target) == []
    assert {
        cleanup.path
        for cleanup in result.cleanup_results
        if cleanup.outcome == "deleted"
    } >= {
        thumbnail.relative_to(target),
        preview.relative_to(target),
    }


def test_unimport_keeps_derived_file_while_another_source_owns_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.mkdir()
    second_source.mkdir()
    (first_source / "IMG_20240102.jpg").write_bytes(b"same")
    (second_source / "COPY_20240103.jpg").write_bytes(b"same")
    assert main(["create", str(target)]) == 0
    import_source(target, first_source)
    import_source(target, second_source)

    conn = db.connect(target)
    try:
        row = conn.execute("SELECT target_path FROM files").fetchone()
    finally:
        conn.close()
    thumbnail = thumbnail_absolute_path(target, Path(str(row["target_path"])))
    write_cache_file(thumbnail)

    result = run_unimport(
        target,
        first_source.name,
        config=AppConfig(),
        dry_run=False,
        confirm=lambda _plan: True,
    )

    assert result.applied
    assert thumbnail.exists()
    conn = db.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0] == 1
    finally:
        conn.close()


def test_unimport_dry_run_lists_derived_files_without_deleting(
    tmp_path: Path,
    capsys,
) -> None:
    target, source = create_collection_with_source(
        tmp_path,
        {"IMG_20240102.jpg": b"image"},
    )
    conn = db.connect(target)
    try:
        row = conn.execute("SELECT target_path FROM files").fetchone()
    finally:
        conn.close()
    thumbnail = thumbnail_absolute_path(
        target,
        Path(str(row["target_path"])),
    )
    write_cache_file(thumbnail)

    assert (
        main(
            [
                "--target",
                str(target),
                "unimport",
                "--dry-run",
                "--name",
                source.name,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out

    assert "Avledede filer som legges i pending_file_deletes:" in output
    assert thumbnail.relative_to(target).as_posix() in output
    assert thumbnail.exists()


def test_unimport_keeps_unsafe_derived_file_and_database_unchanged(
    tmp_path: Path,
) -> None:
    target, source = create_collection_with_source(
        tmp_path,
        {"IMG_20240102.jpg": b"image"},
    )
    conn = db.connect(target)
    try:
        row = conn.execute("SELECT target_path FROM files").fetchone()
    finally:
        conn.close()
    thumbnail = thumbnail_absolute_path(
        target,
        Path(str(row["target_path"])),
    )
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    thumbnail.parent.mkdir(parents=True)
    thumbnail.symlink_to(outside)

    with pytest.raises(ValueError, match="Avledet fil kan ikke slettes trygt"):
        run_unimport(
            target,
            source.name,
            config=AppConfig(),
            dry_run=False,
            confirm=lambda _plan: True,
        )

    assert outside.read_bytes() == b"outside"
    assert thumbnail.is_symlink()
    conn = db.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_file_deletes"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_unimport_keeps_locked_derived_file_in_pending_queue(
    tmp_path: Path,
) -> None:
    target, source = create_collection_with_source(
        tmp_path,
        {"IMG_20240102.jpg": b"image"},
    )
    conn = db.connect(target)
    try:
        row = conn.execute("SELECT target_path FROM files").fetchone()
    finally:
        conn.close()
    thumbnail = thumbnail_absolute_path(
        target,
        Path(str(row["target_path"])),
    )
    write_cache_file(thumbnail)

    def fail_thumbnail(candidate: Path, *args, **kwargs) -> None:
        if candidate == thumbnail:
            raise PermissionError("simulert låst thumbnail")
        os.unlink(candidate)

    with patch.object(
        Path,
        "unlink",
        autospec=True,
        side_effect=fail_thumbnail,
    ):
        result = run_unimport(
            target,
            source.name,
            config=AppConfig(),
            dry_run=False,
            confirm=lambda _plan: True,
        )

    assert result.applied
    assert thumbnail.exists()
    pending = list_pending_deletes(target)
    assert len(pending) == 1
    assert pending[0].path == thumbnail.relative_to(target)
    assert pending[0].attempts == 1
    assert pending[0].last_error == "simulert låst thumbnail"
    conn = db.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    finally:
        conn.close()


def test_v20_migration_cleans_only_recognized_orphaned_derived_files(
    tmp_path: Path,
    capsys,
) -> None:
    target, _source = create_collection_with_source(
        tmp_path,
        {
            "IMG_20240102.jpg": b"image",
            "VID_20240103.avi": b"video",
        },
    )
    conn = db.connect(target)
    try:
        rows = list(
            conn.execute(
                "SELECT target_path, stored_filename, sha256 FROM files"
            )
        )
    finally:
        conn.close()
    image_row = next(
        row for row in rows if str(row["stored_filename"]).endswith(".jpg")
    )
    video_row = next(
        row for row in rows if str(row["stored_filename"]).endswith(".avi")
    )
    owned_thumbnail = thumbnail_absolute_path(
        target,
        Path(str(image_row["target_path"])),
    )
    owned_preview = video_preview_absolute_path(
        target,
        str(video_row["sha256"]),
    )
    orphan_thumbnail = target / thumbnail_relative_path(
        Path("2022/03/orphan.jpg")
    )
    orphan_preview = target / video_preview_relative_path("a" * 64)
    legacy_thumbnail = target / "thumbs" / "2020" / "04" / "old.jpg"
    temporary_thumbnail = orphan_thumbnail.with_name(
        f".{orphan_thumbnail.name}.cache_1.tmp"
    )
    temporary_preview = orphan_preview.with_name(
        f".{orphan_preview.name}.{'b' * 32}.partial"
    )
    unknown = orphan_thumbnail.parent / "keep.txt"
    for path in (
        owned_thumbnail,
        owned_preview,
        orphan_thumbnail,
        orphan_preview,
        legacy_thumbnail,
        temporary_thumbnail,
        temporary_preview,
        unknown,
    ):
        write_cache_file(path, path.name.encode("utf-8"))
    mark_as_schema_v19(target)

    assert main(["--target", str(target), "migrate"]) == 0
    output = capsys.readouterr().out

    assert "schema_version=20" in output
    assert owned_thumbnail.exists()
    assert owned_preview.exists()
    assert unknown.exists()
    for path in (
        orphan_thumbnail,
        orphan_preview,
        legacy_thumbnail,
        temporary_thumbnail,
        temporary_preview,
    ):
        assert not path.exists()
    assert list_pending_deletes(target) == []


def test_v20_migration_keeps_derived_file_for_image_in_trash(
    tmp_path: Path,
) -> None:
    target, _source = create_collection_with_source(
        tmp_path,
        {"IMG_20240102.jpg": b"image"},
    )
    conn = db.connect(target)
    try:
        row = conn.execute("SELECT id, target_path FROM files").fetchone()
    finally:
        conn.close()
    thumbnail = thumbnail_absolute_path(
        target,
        Path(str(row["target_path"])),
    )
    write_cache_file(thumbnail)
    remove_file(target, file_id=int(row["id"]))
    mark_as_schema_v19(target)

    assert main(["--target", str(target), "migrate"]) == 0

    assert thumbnail.exists()
    assert list_pending_deletes(target) == []


def test_current_v20_migrate_does_not_rescan_derived_files(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    orphan = target / thumbnail_relative_path(Path("2022/03/orphan.jpg"))
    write_cache_file(orphan)

    assert main(["--target", str(target), "migrate"]) == 0

    assert orphan.exists()


def test_v20_migration_keeps_failed_derived_delete_in_queue(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    orphan = target / thumbnail_relative_path(Path("2022/03/orphan.jpg"))
    write_cache_file(orphan)
    mark_as_schema_v19(target)

    def fail_orphan(candidate: Path, *args, **kwargs) -> None:
        if candidate == orphan:
            raise PermissionError("simulert låst cachefil")
        os.unlink(candidate)

    with patch.object(
        Path,
        "unlink",
        autospec=True,
        side_effect=fail_orphan,
    ):
        assert main(["--target", str(target), "migrate"]) == 0

    conn = db.connect(target)
    try:
        assert db.schema_version(conn) == 20
    finally:
        conn.close()
    pending = list_pending_deletes(target)
    assert orphan.exists()
    assert len(pending) == 1
    assert pending[0].path == orphan.relative_to(target)
    assert pending[0].reason == DERIVED_DELETE_REASON_MIGRATION
    assert pending[0].attempts == 1
    assert pending[0].last_error == "simulert låst cachefil"


def test_pending_derived_delete_does_not_delete_cache_for_new_file_owner(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    original_relative = Path("2022/03/image.jpg")
    thumbnail_relative = thumbnail_relative_path(original_relative)
    thumbnail = target / thumbnail_relative
    write_cache_file(thumbnail)
    pending = enqueue_pending_delete(
        target,
        thumbnail_relative,
        reason=DERIVED_DELETE_REASON_MIGRATION,
        expected_sha256=sha256_file(thumbnail),
        expected_size_bytes=thumbnail.stat().st_size,
    )

    conn = db.connect(target)
    try:
        conn.execute(
            """
            INSERT INTO files(
                target_path,
                target_path_key,
                original_filename,
                stored_filename,
                sha256,
                size_bytes,
                date_source
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                original_relative.as_posix(),
                db.relative_path_key(original_relative),
                original_relative.name,
                original_relative.name,
                "c" * 64,
                5,
                "filename",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    results = cleanup_pending_deletes(target, pending_ids=(pending.id,))

    assert [result.outcome for result in results] == ["failed"]
    assert thumbnail.exists()
    assert "tilhører fortsatt en files-rad" in (results[0].error or "")


def test_v20_migration_does_not_follow_derived_directory_symlink(
    tmp_path: Path,
    capsys,
) -> None:
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    db.init_database(target)
    outside.mkdir()
    outside_file = outside / "orphan.jpg"
    outside_file.write_bytes(b"outside")
    (target / "thumbs").symlink_to(outside, target_is_directory=True)
    mark_as_schema_v19(target)

    assert main(["--target", str(target), "migrate"]) == 0
    output = capsys.readouterr().out

    assert outside_file.read_bytes() == b"outside"
    assert (target / "thumbs").is_symlink()
    assert "utrygg" in output
