from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bildebank import db


def open_test_db(target: Path) -> sqlite3.Connection:
    db.init_database(target)
    return db.connect(target)


def add_source(conn: sqlite3.Connection, tmp_path: Path, name: str) -> tuple[int, Path]:
    source = tmp_path / name
    source.mkdir()
    source_id = db.add_named_source(conn, source, name)
    return source_id, source


def add_imported_file(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    source_path: Path,
    target: Path,
    target_path: Path,
    sha256: str,
    size_bytes: int = 10,
    taken_date: str | None = "2024-01-02",
) -> int:
    return db.insert_imported_file(
        conn,
        source_id=source_id,
        source_path=source_path,
        target_root=target,
        target_path=target_path,
        original_filename=source_path.name,
        stored_filename=target_path.name,
        sha256=sha256,
        size_bytes=size_bytes,
        taken_date=taken_date,
        date_source="filename",
        name_conflict=False,
    )


def test_insert_imported_file_records_file_and_source_link(tmp_path: Path) -> None:
    target = tmp_path / "target"
    conn = open_test_db(target)
    try:
        source_id, source = add_source(conn, tmp_path, "source-a")
        source_path = source / "IMG_20240102.jpg"
        target_path = target / "2024" / "01" / "IMG_20240102.jpg"

        file_id = add_imported_file(
            conn,
            source_id=source_id,
            source_path=source_path,
            target=target,
            target_path=target_path,
            sha256="hash-a",
            size_bytes=123,
        )

        file_row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        source_row = conn.execute(
            "SELECT * FROM file_sources WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        by_target_path = db.file_sources_by_target_path(conn, target, target_path)

        assert file_row["target_path"] == "2024/01/IMG_20240102.jpg"
        assert file_row["target_path_key"] == db.relative_path_key(Path("2024/01/IMG_20240102.jpg"))
        assert file_row["sha256"] == "hash-a"
        assert source_row["source_id"] == source_id
        assert source_row["source_path"] == str(source_path.resolve())
        assert source_row["source_path_key"] == db.path_key(source_path)
        assert source_row["sha256"] == "hash-a"
        assert source_row["size_bytes"] == 123
        assert len(by_target_path) == 1
        assert by_target_path[0]["file_id"] == file_id
        assert by_target_path[0]["source_name"] == "source-a"
    finally:
        conn.close()


def test_insert_duplicate_records_only_new_provenance_row(tmp_path: Path) -> None:
    target = tmp_path / "target"
    conn = open_test_db(target)
    try:
        first_source_id, first_source = add_source(conn, tmp_path, "source-a")
        second_source_id, second_source = add_source(conn, tmp_path, "source-b")
        file_id = add_imported_file(
            conn,
            source_id=first_source_id,
            source_path=first_source / "IMG_20240102.jpg",
            target=target,
            target_path=target / "2024" / "01" / "IMG_20240102.jpg",
            sha256="same-hash",
        )

        db.insert_duplicate(
            conn,
            source_id=second_source_id,
            source_path=second_source / "COPY_20240102.jpg",
            matched_file_id=file_id,
            sha256="same-hash",
            size_bytes=10,
        )

        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0] == 2
        assert db.duplicate_source_count(conn) == 1
        second_rows = db.source_file_sources(conn, second_source_id)
        assert len(second_rows) == 1
        assert second_rows[0]["file_id"] == file_id
        assert second_rows[0]["target_path"] == "2024/01/IMG_20240102.jpg"
    finally:
        conn.close()


def test_file_source_conflict_is_rejected_for_same_source_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    conn = open_test_db(target)
    try:
        source_id, source = add_source(conn, tmp_path, "source-a")
        first_file_id = add_imported_file(
            conn,
            source_id=source_id,
            source_path=source / "IMG_20240102.jpg",
            target=target,
            target_path=target / "2024" / "01" / "IMG_20240102.jpg",
            sha256="hash-a",
        )
        second_file_id = add_imported_file(
            conn,
            source_id=source_id,
            source_path=source / "IMG_20240103.jpg",
            target=target,
            target_path=target / "2024" / "01" / "IMG_20240103.jpg",
            sha256="hash-b",
        )

        with pytest.raises(ValueError, match="Konflikt i file_sources"):
            db.insert_or_validate_file_source(
                conn,
                file_id=second_file_id,
                source_id=source_id,
                source_path=str((source / "IMG_20240102.jpg").resolve()),
                source_path_key=db.path_key(source / "IMG_20240102.jpg"),
                sha256="hash-b",
                size_bytes=10,
            )

        assert first_file_id != second_file_id
        assert conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0] == 2
    finally:
        conn.close()


def test_files_by_hash_prefers_active_file_before_deleted_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    conn = open_test_db(target)
    try:
        conn.execute(
            """
            INSERT INTO files(
                target_path, target_path_key, original_filename, stored_filename,
                sha256, size_bytes, date_source, deleted_at
            )
            VALUES
                ('deleted/2024/01/old.jpg', 'deleted/2024/01/old.jpg',
                 'old.jpg', 'old.jpg', 'same-hash', 10, 'filename', CURRENT_TIMESTAMP),
                ('2024/01/active.jpg', '2024/01/active.jpg',
                 'active.jpg', 'active.jpg', 'same-hash', 10, 'filename', NULL)
            """
        )

        rows = db.files_by_hash(conn, "same-hash")

        assert [row["target_path"] for row in rows] == [
            "2024/01/active.jpg",
            "deleted/2024/01/old.jpg",
        ]
    finally:
        conn.close()


def test_file_source_integrity_queries_include_deleted_files(tmp_path: Path) -> None:
    target = tmp_path / "target"
    conn = open_test_db(target)
    try:
        source_id, source = add_source(conn, tmp_path, "source-a")
        active_id = add_imported_file(
            conn,
            source_id=source_id,
            source_path=source / "active.jpg",
            target=target,
            target_path=target / "2024" / "01" / "active.jpg",
            sha256="active-hash",
            size_bytes=10,
        )
        deleted_id = add_imported_file(
            conn,
            source_id=source_id,
            source_path=source / "deleted.jpg",
            target=target,
            target_path=target / "deleted" / "2024" / "01" / "deleted.jpg",
            sha256="deleted-hash",
            size_bytes=20,
        )
        conn.execute(
            "UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
            (deleted_id,),
        )

        assert db.files_without_sources(conn) == []
        assert db.file_source_integrity_mismatches(conn) == []

        conn.execute("DELETE FROM file_sources WHERE file_id = ?", (deleted_id,))
        conn.execute(
            """
            UPDATE file_sources
            SET sha256 = 'wrong-hash', size_bytes = 11
            WHERE file_id = ?
            """,
            (active_id,),
        )

        missing = db.files_without_sources(conn)
        mismatches = db.file_source_integrity_mismatches(conn)

        assert [row["id"] for row in missing] == [deleted_id]
        assert missing[0]["deleted_at"] is not None
        assert len(mismatches) == 1
        assert mismatches[0]["file_id"] == active_id
        assert mismatches[0]["file_sha256"] == "active-hash"
        assert mismatches[0]["source_sha256"] == "wrong-hash"
        assert mismatches[0]["file_size_bytes"] == 10
        assert mismatches[0]["source_size_bytes"] == 11
    finally:
        conn.close()


def test_unimport_plan_keeps_file_when_another_source_also_references_it(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    conn = open_test_db(target)
    try:
        first_source_id, first_source = add_source(conn, tmp_path, "source-a")
        second_source_id, second_source = add_source(conn, tmp_path, "source-b")
        only_first_id = add_imported_file(
            conn,
            source_id=first_source_id,
            source_path=first_source / "ONLY_20240102.jpg",
            target=target,
            target_path=target / "2024" / "01" / "ONLY_20240102.jpg",
            sha256="only-first",
        )
        shared_id = add_imported_file(
            conn,
            source_id=first_source_id,
            source_path=first_source / "SHARED_20240102.jpg",
            target=target,
            target_path=target / "2024" / "01" / "SHARED_20240102.jpg",
            sha256="shared",
        )
        db.insert_duplicate(
            conn,
            source_id=second_source_id,
            source_path=second_source / "COPY_20240102.jpg",
            matched_file_id=shared_id,
            sha256="shared",
            size_bytes=10,
        )

        source = db.get_source(conn, first_source_id)
        plan = db.build_unimport_plan(conn, target, source)

        assert plan.source_file_count == 2
        assert plan.active_remove_count == 1
        assert plan.active_keep_count == 1
        assert plan.file_ids_to_delete == (only_first_id,)
        assert plan.target_paths_to_delete == (
            target / "2024" / "01" / "ONLY_20240102.jpg",
        )
        assert plan.target_paths_to_keep == (
            target / "2024" / "01" / "SHARED_20240102.jpg",
        )
    finally:
        conn.close()
