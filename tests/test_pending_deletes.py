from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.pending_deletes import (
    cleanup_pending_deletes,
    enqueue_pending_delete,
    list_pending_deletes,
)


def insert_file_reference(target: Path, relative_path: Path) -> int:
    conn = db.connect(target)
    try:
        cursor = conn.execute(
            """
            INSERT INTO files(
                target_path, target_path_key, original_filename, stored_filename,
                sha256, size_bytes, date_source
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relative_path.as_posix(),
                db.relative_path_key(relative_path),
                relative_path.name,
                relative_path.name,
                "hash",
                4,
                "filename",
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def test_pending_delete_removes_unreferenced_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    path = target / "2024" / "01" / "orphan.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"data")
    enqueue_pending_delete(target, path, reason="test")

    results = cleanup_pending_deletes(target)

    assert [result.outcome for result in results] == ["deleted"]
    assert not path.exists()
    assert list_pending_deletes(target) == []


def test_pending_delete_removes_row_when_file_is_already_missing(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    enqueue_pending_delete(target, Path("2024/01/missing.jpg"), reason="test")

    results = cleanup_pending_deletes(target)

    assert [result.outcome for result in results] == ["missing"]
    assert list_pending_deletes(target) == []


def test_pending_delete_path_is_unique(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)

    first = enqueue_pending_delete(
        target,
        Path("2024/01/image.jpg"),
        reason="første årsak",
    )
    second = enqueue_pending_delete(
        target,
        target / "2024" / "01" / "image.jpg",
        reason="oppdatert årsak",
    )
    rows = list_pending_deletes(target)

    assert first.id == second.id
    assert len(rows) == 1
    assert rows[0].reason == "oppdatert årsak"


def test_pending_delete_keeps_file_that_still_has_database_reference(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    relative_path = Path("2024/01/referenced.jpg")
    path = target / relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"data")
    file_id = insert_file_reference(target, relative_path)
    enqueue_pending_delete(target, relative_path, reason="test")

    results = cleanup_pending_deletes(target)
    rows = list_pending_deletes(target)

    assert [result.outcome for result in results] == ["failed"]
    assert path.exists()
    assert len(rows) == 1
    assert rows[0].attempts == 1
    assert f"files #{file_id}" in (rows[0].last_error or "")


def test_pending_delete_does_not_delete_file_outside_managed_area(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"data")
    conn = db.connect(target)
    try:
        conn.execute(
            """
            INSERT INTO pending_file_deletes(path, reason)
            VALUES('../outside.jpg', 'malformed test row')
            """
        )
        conn.commit()
    finally:
        conn.close()

    results = cleanup_pending_deletes(target)
    rows = list_pending_deletes(target)

    assert [result.outcome for result in results] == ["failed"]
    assert outside.exists()
    assert rows[0].attempts == 1
    assert "pending-delete-sti" in (rows[0].last_error or "")


def test_pending_delete_failure_stays_queued_with_last_error(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    path = target / "2024" / "01" / "locked.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"data")
    enqueue_pending_delete(target, path, reason="test")

    def fail_selected_file(candidate: Path, *args, **kwargs) -> None:
        if candidate == path:
            raise PermissionError("filen er låst")
        os.unlink(candidate)

    with patch.object(Path, "unlink", autospec=True, side_effect=fail_selected_file):
        results = cleanup_pending_deletes(target)

    rows = list_pending_deletes(target)
    assert [result.outcome for result in results] == ["failed"]
    assert path.exists()
    assert len(rows) == 1
    assert rows[0].attempts == 1
    assert rows[0].last_error == "filen er låst"


def test_cleanup_pending_deletes_cli_defaults_to_list(tmp_path: Path, capsys) -> None:
    from bildebank.cli import main

    target = tmp_path / "target"
    db.init_database(target)
    path = target / "2024" / "01" / "queued.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"data")
    enqueue_pending_delete(target, path, reason="test")

    assert main(["--target", str(target), "cleanup-pending-deletes"]) == 0

    output = capsys.readouterr().out
    assert "2024/01/queued.jpg" in output
    assert path.exists()


def test_cleanup_pending_deletes_cli_requires_apply_for_deletion(
    tmp_path: Path,
    capsys,
) -> None:
    from bildebank.cli import main

    target = tmp_path / "target"
    db.init_database(target)
    path = target / "2024" / "01" / "queued.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"data")
    enqueue_pending_delete(target, path, reason="test")

    assert (
        main(
            [
                "--target",
                str(target),
                "cleanup-pending-deletes",
                "--apply",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "slettet=1" in output
    assert not path.exists()
