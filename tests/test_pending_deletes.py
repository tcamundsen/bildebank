from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.media import sha256_file
from bildebank.pending_deletes import (
    cleanup_pending_deletes,
    enqueue_pending_delete,
    list_pending_deletes,
    pending_delete_integrity_rows,
)


def enqueue_for_test(
    target: Path,
    path: Path,
    *,
    reason: str = "test",
):
    absolute_path = path if path.is_absolute() else target / path
    if absolute_path.is_file():
        expected_sha256 = sha256_file(absolute_path)
        expected_size_bytes = absolute_path.stat().st_size
    else:
        expected_sha256 = "missing"
        expected_size_bytes = 0
    return enqueue_pending_delete(
        target,
        path,
        reason=reason,
        expected_sha256=expected_sha256,
        expected_size_bytes=expected_size_bytes,
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
    enqueue_for_test(target, path)

    results = cleanup_pending_deletes(target)

    assert [result.outcome for result in results] == ["deleted"]
    assert not path.exists()
    assert list_pending_deletes(target) == []


def test_pending_delete_removes_row_when_file_is_already_missing(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    enqueue_for_test(target, Path("2024/01/missing.jpg"))

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
        expected_sha256="first",
        expected_size_bytes=1,
    )
    second = enqueue_pending_delete(
        target,
        target / "2024" / "01" / "image.jpg",
        reason="oppdatert årsak",
        expected_sha256="second",
        expected_size_bytes=2,
    )
    rows = list_pending_deletes(target)

    assert first.id == second.id
    assert len(rows) == 1
    assert rows[0].reason == "oppdatert årsak"
    assert rows[0].expected_sha256 == "second"
    assert rows[0].expected_size_bytes == 2


def test_pending_delete_integrity_rows_are_read_from_existing_connection(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    pending = enqueue_pending_delete(
        target,
        Path("2024/01/image.jpg"),
        reason="doctor",
        expected_sha256="a" * 64,
        expected_size_bytes=12,
    )
    conn = db.connect_read_only(target)
    try:
        rows = pending_delete_integrity_rows(conn)
    finally:
        conn.close()

    assert len(rows) == 1
    assert int(rows[0]["id"]) == pending.id
    assert rows[0]["path"] == "2024/01/image.jpg"
    assert rows[0]["reason"] == "doctor"
    assert rows[0]["expected_sha256"] == "a" * 64
    assert rows[0]["expected_size_bytes"] == 12


def test_migrate_repairs_legacy_pending_source_foreign_key(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    conn = db.connect(target)
    try:
        source_id = conn.execute(
            """
            INSERT INTO sources(path, path_key, name)
            VALUES('/source', '/source', 'source')
            """
        ).lastrowid
        conn.execute("DROP TABLE pending_file_deletes")
        conn.execute(
            """
            CREATE TABLE pending_file_deletes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                reason TEXT NOT NULL,
                source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO pending_file_deletes(path, reason, source_id)
            VALUES('2024/01/image.jpg', 'unimport', ?)
            """,
            (source_id,),
        )
        conn.commit()
    finally:
        conn.close()

    db.migrate_database(target)

    conn = db.connect(target)
    try:
        assert conn.execute(
            "PRAGMA foreign_key_list(pending_file_deletes)"
        ).fetchall() == []
        assert conn.execute(
            "SELECT source_id FROM pending_file_deletes"
        ).fetchone()[0] == source_id
    finally:
        conn.close()


def test_pending_delete_keeps_file_that_still_has_database_reference(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    relative_path = Path("2024/01/referenced.jpg")
    path = target / relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"data")
    file_id = insert_file_reference(target, relative_path)
    enqueue_for_test(target, relative_path)

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
    enqueue_for_test(target, path)

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


def test_pending_delete_does_not_delete_replacement_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    path = target / "2024" / "01" / "queued.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original")
    enqueue_for_test(target, path)

    def fail_selected_file(candidate: Path, *args, **kwargs) -> None:
        if candidate == path:
            raise PermissionError("filen er låst")
        os.unlink(candidate)

    with patch.object(
        Path,
        "unlink",
        autospec=True,
        side_effect=fail_selected_file,
    ):
        first_results = cleanup_pending_deletes(target)

    assert [result.outcome for result in first_results] == ["failed"]
    path.write_bytes(b"replacement")

    second_results = cleanup_pending_deletes(target)

    assert [result.outcome for result in second_results] == ["failed"]
    assert path.read_bytes() == b"replacement"
    rows = list_pending_deletes(target)
    assert len(rows) == 1
    assert rows[0].attempts == 2
    assert "endret størrelse" in (rows[0].last_error or "")


def test_pending_delete_does_not_delete_same_size_replacement_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    path = target / "2024" / "01" / "queued.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original")
    enqueue_for_test(target, path)

    path.write_bytes(b"replaced")
    results = cleanup_pending_deletes(target)

    assert [result.outcome for result in results] == ["failed"]
    assert path.read_bytes() == b"replaced"
    rows = list_pending_deletes(target)
    assert len(rows) == 1
    assert rows[0].attempts == 1
    assert "endret innhold" in (rows[0].last_error or "")


def test_legacy_pending_delete_without_identity_does_not_delete_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    path = target / "2024" / "01" / "legacy.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"legacy")
    conn = db.connect(target)
    try:
        conn.execute(
            """
            INSERT INTO pending_file_deletes(path, reason)
            VALUES('2024/01/legacy.jpg', 'legacy')
            """
        )
        conn.commit()
    finally:
        conn.close()

    results = cleanup_pending_deletes(target)

    assert [result.outcome for result in results] == ["failed"]
    assert path.read_bytes() == b"legacy"
    assert "mangler forventet SHA-256" in (results[0].error or "")


def test_cleanup_pending_deletes_cli_defaults_to_list(tmp_path: Path, capsys) -> None:
    from bildebank.cli import main

    target = tmp_path / "target"
    db.init_database(target)
    path = target / "2024" / "01" / "queued.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"data")
    enqueue_for_test(target, path)

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
    enqueue_for_test(target, path)

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
