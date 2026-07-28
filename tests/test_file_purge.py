from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from bildebank import db
from bildebank.cli_doctor import doctor_check_file_purges
from bildebank.file_lifecycle import undelete_file
from bildebank.file_purge import (
    PurgeConfirmationIdentity,
    abort_file_purge,
    preview_deleted_file_purges,
    preview_file_purge,
    purge_deleted_files,
    purge_file,
    recover_pending_file_purges,
    retry_file_purge,
)
from bildebank.media import sha256_file
from bildebank.missing_file_repair import build_missing_file_repair_plan
from bildebank.snapshot_create import create_snapshot
from bildebank.thumbnails import thumbnail_relative_path
from bildebank.video_previews import video_preview_relative_path
from tests.cli_helpers import capture_cli


DELETED_AT = "2026-07-28 12:00:00"


def create_deleted_file(
    target: Path,
    *,
    filename: str,
    content: bytes,
    with_source: bool = False,
) -> tuple[int, Path]:
    relative_path = Path("deleted", "2024", "01", filename)
    path = target / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    conn = db.connect(target)
    try:
        file_id = int(
            conn.execute(
                """
                INSERT INTO files(
                    target_path, target_path_key, original_filename,
                    stored_filename, sha256, size_bytes, taken_date,
                    date_source, deleted_at, deleted_original_target_path
                )
                VALUES(?, ?, ?, ?, ?, ?, '2024-01-02', 'filename', ?, ?)
                RETURNING id
                """,
                (
                    relative_path.as_posix(),
                    db.relative_path_key(relative_path),
                    filename,
                    filename,
                    digest,
                    len(content),
                    DELETED_AT,
                    Path("2024", "01", filename).as_posix(),
                ),
            ).fetchone()["id"]
        )
        if with_source:
            source_id = int(
                conn.execute(
                    """
                    INSERT INTO sources(
                        path, path_key, name, imported_at, status
                    )
                    VALUES(?, ?, ?, CURRENT_TIMESTAMP, 'imported')
                    RETURNING id
                    """,
                    (
                        f"C:\\Bilder\\{filename}",
                        f"c:\\bilder\\{filename}",
                        f"source-{file_id}",
                    ),
                ).fetchone()["id"]
            )
            conn.execute(
                """
                INSERT INTO file_sources(
                    file_id, source_id, source_path, source_path_key,
                    sha256, size_bytes
                )
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    source_id,
                    f"C:\\Bilder\\{filename}",
                    f"c:\\bilder\\{filename}",
                    digest,
                    len(content),
                ),
            )
        conn.commit()
        return file_id, path
    finally:
        conn.close()


def pending_id(target: Path, file_id: int) -> int:
    conn = db.connect(target)
    try:
        row = db.pending_file_purge_for_file(conn, file_id=file_id)
        assert row is not None
        return int(row["id"])
    finally:
        conn.close()


def test_preview_is_read_only_and_excludes_unknown_deleted_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, _path = create_deleted_file(
        target,
        filename="known.jpg",
        content=b"known",
    )
    unknown = target / "deleted" / "unknown.jpg"
    unknown.write_bytes(b"unknown")
    database_before = db.db_path_for_target(target).read_bytes()

    preview = preview_deleted_file_purges(target)

    assert preview.count == 1
    assert preview.new_candidates[0].file_id == file_id
    assert preview.total_size_bytes == len(b"known")
    assert unknown.read_bytes() == b"unknown"
    assert db.db_path_for_target(target).read_bytes() == database_before


def test_successful_purge_removes_original_derived_and_database_rows(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
        with_source=True,
    )
    former_path = Path("2024", "01", "image.jpg")
    current_thumbnail = target / thumbnail_relative_path(former_path)
    current_thumbnail.parent.mkdir(parents=True)
    current_thumbnail.write_bytes(b"current thumb")
    legacy_thumbnail = target / "thumbs" / former_path
    legacy_thumbnail.parent.mkdir(parents=True)
    legacy_thumbnail.write_bytes(b"legacy thumb")

    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    result = purge_file(target, confirmation)

    assert result.status == "deleted"
    assert result.tombstone_id is not None
    assert not original.exists()
    assert not current_thumbnail.exists()
    assert not legacy_thumbnail.exists()
    conn = db.connect(target)
    try:
        assert conn.execute(
            "SELECT 1 FROM files WHERE id = ?",
            (file_id,),
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM file_sources WHERE file_id = ?",
            (file_id,),
        ).fetchone() is None
        tombstone = db.file_tombstone(
            conn,
            tombstone_id=result.tombstone_id,
        )
        assert tombstone is not None
        log = conn.execute(
            """
            SELECT command, args_json
            FROM command_log
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert log["command"] == "purge-file"
        args = json.loads(log["args_json"])
        assert args == {
            "deleted": 1,
            "integrity_errors": 0,
            "pending": 0,
            "requested": 1,
            "skipped": 0,
        }
        assert "image.jpg" not in log["args_json"]
        assert str(tombstone["sha256"]) not in log["args_json"]
    finally:
        conn.close()


def test_video_purge_removes_playback_copy(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="video.avi",
        content=b"video original",
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    preview_path = target / video_preview_relative_path(
        confirmation.sha256
    )
    preview_path.parent.mkdir(parents=True)
    preview_path.write_bytes(b"video preview")

    result = purge_file(target, confirmation)

    assert result.status == "deleted"
    assert not original.exists()
    assert not preview_path.exists()


def test_purge_preserves_thumbnail_path_referenced_by_another_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    deleted_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"deleted original",
    )
    active_relative = Path("2024", "01", "image.jpg")
    active = target / active_relative
    active.parent.mkdir(parents=True)
    active.write_bytes(b"new active file")
    conn = db.connect(target)
    try:
        conn.execute(
            """
            INSERT INTO files(
                target_path, target_path_key, original_filename,
                stored_filename, sha256, size_bytes, taken_date, date_source
            )
            VALUES(?, ?, 'image.jpg', 'image.jpg', ?, ?, '2024-01-02', 'filename')
            """,
            (
                active_relative.as_posix(),
                db.relative_path_key(active_relative),
                sha256_file(active),
                active.stat().st_size,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    current_thumbnail = target / thumbnail_relative_path(active_relative)
    current_thumbnail.parent.mkdir(parents=True)
    current_thumbnail.write_bytes(b"shared current thumbnail")
    legacy_thumbnail = target / "thumbs" / active_relative
    legacy_thumbnail.parent.mkdir(parents=True)
    legacy_thumbnail.write_bytes(b"shared legacy thumbnail")

    confirmation = preview_file_purge(
        target,
        file_id=deleted_id,
    ).new_candidates[0]
    result = purge_file(target, confirmation)

    assert result.status == "deleted"
    assert not original.exists()
    assert current_thumbnail.read_bytes() == b"shared current thumbnail"
    assert legacy_thumbnail.read_bytes() == b"shared legacy thumbnail"


@pytest.mark.parametrize("state", ["changed", "missing", "symlink"])
def test_invalid_original_does_not_start_new_purge(
    tmp_path: Path,
    state: str,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    if state == "changed":
        original.write_bytes(b"changed")
    elif state == "missing":
        original.unlink()
    else:
        original.unlink()
        link_target = target / "other.jpg"
        link_target.write_bytes(b"original")
        original.symlink_to(link_target)

    result = purge_file(target, confirmation)

    assert result.status in {"skipped", "integrity-error"}
    conn = db.connect(target)
    try:
        assert db.pending_file_purge_for_file(
            conn,
            file_id=file_id,
        ) is None
        assert db.file_tombstone_by_sha256(
            conn,
            sha256=confirmation.sha256,
        ) is None
    finally:
        conn.close()


def test_unsafe_derived_file_leaves_original_and_retryable_journal(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    current_thumbnail = target / thumbnail_relative_path(
        Path("2024", "01", "image.jpg")
    )
    current_thumbnail.parent.mkdir(parents=True)
    link_target = target / "unmanaged-thumbnail.jpg"
    link_target.write_bytes(b"thumb")
    current_thumbnail.symlink_to(link_target)

    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    result = purge_file(target, confirmation)

    assert result.status == "pending"
    assert result.purge_id is not None
    assert original.read_bytes() == b"original"
    assert current_thumbnail.is_symlink()


def test_journal_creation_failure_changes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]

    with patch(
        "bildebank.file_purge.db.create_pending_file_purge",
        side_effect=sqlite3.OperationalError("simulert journalfeil"),
    ), pytest.raises(sqlite3.OperationalError, match="journalfeil"):
        purge_file(target, confirmation)

    assert original.read_bytes() == b"original"
    conn = db.connect(target)
    try:
        assert db.pending_file_purge_for_file(
            conn,
            file_id=file_id,
        ) is None
        assert db.file_tombstone_by_sha256(
            conn,
            sha256=confirmation.sha256,
        ) is None
    finally:
        conn.close()


def test_failure_after_one_derived_unlink_keeps_original(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    former_path = Path("2024", "01", "image.jpg")
    for relative_path, content in (
        (thumbnail_relative_path(former_path), b"current"),
        (Path("thumbs") / former_path, b"legacy"),
    ):
        path = target / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    real_unlink = Path.unlink
    deleted_derived = 0

    def fail_second_derived_unlink(path: Path) -> None:
        nonlocal deleted_derived
        if "thumbs" in path.parts:
            deleted_derived += 1
            if deleted_derived == 2:
                raise PermissionError("simulert låst derived-fil")
        real_unlink(path)

    with patch.object(Path, "unlink", fail_second_derived_unlink):
        result = purge_file(target, confirmation)

    assert result.status == "pending"
    assert original.read_bytes() == b"original"
    assert deleted_derived == 2


def test_bulk_error_does_not_stop_other_valid_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    good_id, good = create_deleted_file(
        target,
        filename="good.jpg",
        content=b"good",
    )
    bad_id, bad = create_deleted_file(
        target,
        filename="bad.jpg",
        content=b"bad",
    )
    preview = preview_deleted_file_purges(target)
    bad.write_bytes(b"changed")

    result = purge_deleted_files(target, preview)

    by_id = {item.file_id: item for item in result.results}
    assert by_id[good_id].status == "deleted"
    assert by_id[bad_id].status == "integrity-error"
    assert not good.exists()
    assert bad.exists()


def test_abort_removes_only_journal_and_allows_undelete(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    with patch(
        "bildebank.file_purge._delete_derived_files",
        side_effect=OSError("simulert låst avledet fil"),
    ):
        result = purge_file(target, confirmation)
    assert result.status == "pending"
    assert result.purge_id is not None

    with pytest.raises(ValueError, match="ventende permanent sletting"):
        undelete_file(target, file_id=file_id)

    aborted = abort_file_purge(target, purge_id=result.purge_id)
    assert aborted == confirmation
    restored = undelete_file(target, file_id=file_id)
    assert restored == Path("2024", "01", "image.jpg")
    assert not original.exists()
    assert (target / restored).read_bytes() == b"original"


def test_recovery_does_not_delete_matching_original_but_retry_does(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    with patch(
        "bildebank.file_purge._delete_derived_files",
        side_effect=OSError("simulert feil"),
    ):
        result = purge_file(target, confirmation)
    assert result.purge_id is not None

    recovery = recover_pending_file_purges(target)

    assert recovery.results[0].status == "pending"
    assert original.exists()
    retried = retry_file_purge(target, purge_id=result.purge_id)
    assert retried.status == "deleted"
    assert not original.exists()


def test_recovery_completes_after_original_unlink_before_database_commit(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    with patch(
        "bildebank.file_purge.db.complete_pending_file_purge",
        side_effect=sqlite3.OperationalError("simulert commit-feil"),
    ):
        result = purge_file(target, confirmation)

    assert result.status == "pending"
    assert not original.exists()
    purge_id = pending_id(target, file_id)

    recovery = recover_pending_file_purges(target)

    assert recovery.results[0].status == "deleted"
    conn = db.connect(target)
    try:
        assert db.pending_file_purge(
            conn,
            purge_id=purge_id,
        ) is None
        assert db.file_tombstone_by_sha256(
            conn,
            sha256=confirmation.sha256,
        ) is not None
    finally:
        conn.close()


def test_recovery_preserves_unexpected_content(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    conn = db.connect(target)
    try:
        purge_id = db.create_pending_file_purge(
            conn,
            file_id=file_id,
            expected_path=confirmation.expected_path.as_posix(),
            expected_sha256=confirmation.sha256,
            expected_size_bytes=confirmation.size_bytes,
            expected_deleted_at=confirmation.deleted_at,
        )
        conn.commit()
    finally:
        conn.close()
    original.write_bytes(b"unexpected")

    result = recover_pending_file_purges(target)

    assert result.results[0].status == "integrity-error"
    assert original.read_bytes() == b"unexpected"
    conn = db.connect(target)
    try:
        pending = db.pending_file_purge(conn, purge_id=purge_id)
        assert pending is not None
        assert int(pending["attempts"]) == 1
        assert pending["last_error"]
        assert db.file_tombstone_by_sha256(
            conn,
            sha256=confirmation.sha256,
        ) is None
    finally:
        conn.close()


def test_doctor_reports_pending_states_without_changing_them(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    conn = db.connect(target)
    try:
        purge_id = db.create_pending_file_purge(
            conn,
            file_id=file_id,
            expected_path=confirmation.expected_path.as_posix(),
            expected_sha256=confirmation.sha256,
            expected_size_bytes=confirmation.size_bytes,
            expected_deleted_at=confirmation.deleted_at,
        )
        conn.commit()
    finally:
        conn.close()

    doctor_check_file_purges(target)
    matching_output = capsys.readouterr().out
    assert "fortsatt riktig original" in matching_output
    assert original.exists()

    original.unlink()
    doctor_check_file_purges(target)
    missing_output = capsys.readouterr().out
    assert "allerede er borte" in missing_output
    conn = db.connect(target)
    try:
        assert db.pending_file_purge(
            conn,
            purge_id=purge_id,
        ) is not None
    finally:
        conn.close()


def test_snapshot_keeps_matching_pending_purge_and_original(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    repository = tmp_path / "repository"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    conn = db.connect(target)
    try:
        purge_id = db.create_pending_file_purge(
            conn,
            file_id=file_id,
            expected_path=confirmation.expected_path.as_posix(),
            expected_sha256=confirmation.sha256,
            expected_size_bytes=confirmation.size_bytes,
            expected_deleted_at=confirmation.deleted_at,
        )
        conn.commit()
    finally:
        conn.close()

    result = create_snapshot(target, repository)

    assert result.status == "complete"
    assert original.read_bytes() == b"original"
    conn = db.connect(target)
    try:
        assert db.pending_file_purge(
            conn,
            purge_id=purge_id,
        ) is not None
        assert db.file_tombstone_by_sha256(
            conn,
            sha256=confirmation.sha256,
        ) is None
    finally:
        conn.close()


def test_import_skips_tombstone_and_reports_size_conflict(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    source = tmp_path / "source"
    source.mkdir()
    tombstoned = source / "IMG_20240102.jpg"
    tombstoned.write_bytes(b"tombstoned")
    good = source / "IMG_20240103.jpg"
    good.write_bytes(b"good")
    db.init_database(target)
    conn = db.connect(target)
    try:
        conn.execute(
            """
            INSERT INTO file_tombstones(
                sha256, size_bytes, original_filename,
                former_target_path, purged_at
            )
            VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                sha256_file(tombstoned),
                tombstoned.stat().st_size,
                tombstoned.name,
                f"2024/01/{tombstoned.name}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    code, stdout, stderr = capture_cli(
        [
            "--target",
            str(target),
            "import",
            "--name",
            "source",
            "--quiet",
            str(source),
        ]
    )

    assert code == 0, stderr
    assert "permanent_slettet=1" in stdout
    conn = db.connect(target)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM files"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM file_sources"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    conflict_target = tmp_path / "conflict-target"
    conflict_source = tmp_path / "conflict-source"
    conflict_source.mkdir()
    conflict = conflict_source / "IMG_20240104.jpg"
    conflict.write_bytes(b"conflict")
    another_good = conflict_source / "IMG_20240105.jpg"
    another_good.write_bytes(b"another good")
    db.init_database(conflict_target)
    conn = db.connect(conflict_target)
    try:
        conn.execute(
            """
            INSERT INTO file_tombstones(
                sha256, size_bytes, original_filename,
                former_target_path, purged_at
            )
            VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                sha256_file(conflict),
                conflict.stat().st_size + 1,
                conflict.name,
                f"2024/01/{conflict.name}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    code, stdout, stderr = capture_cli(
        [
            "--target",
            str(conflict_target),
            "import",
            "--name",
            "conflict-source",
            "--quiet",
            str(conflict_source),
        ]
    )

    assert code == 2, stderr
    assert "tombstone_integritetsfeil=1" in stdout
    assert (conflict_target / "2024" / "01" / another_good.name).exists()
    conn = db.connect(conflict_target)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM files"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM file_sources"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_import_reports_pending_purge_without_new_file_source(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    source = tmp_path / "new-source"
    source.mkdir()
    source_file = source / "IMG_20240102.jpg"
    source_file.write_bytes(b"original")
    db.init_database(target)
    file_id, _original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
        with_source=True,
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    conn = db.connect(target)
    try:
        db.create_pending_file_purge(
            conn,
            file_id=file_id,
            expected_path=confirmation.expected_path.as_posix(),
            expected_sha256=confirmation.sha256,
            expected_size_bytes=confirmation.size_bytes,
            expected_deleted_at=confirmation.deleted_at,
        )
        source_count_before = conn.execute(
            "SELECT COUNT(*) FROM file_sources WHERE file_id = ?",
            (file_id,),
        ).fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    code, stdout, stderr = capture_cli(
        [
            "--target",
            str(target),
            "import",
            "--name",
            "new-source",
            "--quiet",
            str(source),
        ]
    )

    assert code == 0, stderr
    assert "ventende_purge=1" in stdout
    conn = db.connect(target)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM file_sources WHERE file_id = ?",
            (file_id,),
        ).fetchone()[0] == source_count_before
    finally:
        conn.close()


def test_unimport_plan_is_blocked_by_pending_purge(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, _original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
        with_source=True,
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    conn = db.connect(target)
    try:
        db.create_pending_file_purge(
            conn,
            file_id=file_id,
            expected_path=confirmation.expected_path.as_posix(),
            expected_sha256=confirmation.sha256,
            expected_size_bytes=confirmation.size_bytes,
            expected_deleted_at=confirmation.deleted_at,
        )
        source = db.find_source_by_name(conn, f"source-{file_id}")
        assert source is not None
        with pytest.raises(ValueError, match="ventende permanent sletting"):
            db.build_unimport_plan(conn, target, source)
    finally:
        conn.close()


def test_missing_file_repair_is_blocked_by_pending_purge(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
        with_source=True,
    )
    candidate = tmp_path / "recovered.jpg"
    candidate.write_bytes(original.read_bytes())
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    conn = db.connect(target)
    try:
        db.create_pending_file_purge(
            conn,
            file_id=file_id,
            expected_path=confirmation.expected_path.as_posix(),
            expected_sha256=confirmation.sha256,
            expected_size_bytes=confirmation.size_bytes,
            expected_deleted_at=confirmation.deleted_at,
        )
        conn.commit()
    finally:
        conn.close()
    original.unlink()

    with pytest.raises(ValueError, match="ventende permanent sletting"):
        build_missing_file_repair_plan(
            target,
            file_id=file_id,
            candidate_path=candidate,
        )


def test_changed_confirmation_does_not_expand_or_retarget_selection(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="first.jpg",
        content=b"first",
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    second_id, second = create_deleted_file(
        target,
        filename="second.jpg",
        content=b"second",
    )
    stale = PurgeConfirmationIdentity(
        file_id=confirmation.file_id,
        sha256=confirmation.sha256,
        size_bytes=confirmation.size_bytes,
        expected_path=Path("deleted", "2024", "01", "elsewhere.jpg"),
        deleted_at=confirmation.deleted_at,
    )

    result = purge_file(target, stale)

    assert result.status == "integrity-error"
    assert original.exists()
    assert second.exists()
    assert preview_file_purge(
        target,
        file_id=second_id,
    ).count == 1
