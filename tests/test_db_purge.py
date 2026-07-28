from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bildebank import db


DELETED_AT = "2026-07-28 12:00:00"


def insert_deleted_file(
    conn: sqlite3.Connection,
    *,
    sha256: str,
    filename: str = "image.jpg",
) -> tuple[int, int]:
    source_id = int(
        conn.execute(
            """
            INSERT INTO sources(path, path_key, name, imported_at, status)
            VALUES('C:\\Bilder', 'c:\\bilder', ?, CURRENT_TIMESTAMP, 'imported')
            RETURNING id
            """,
            (f"source-{sha256}",),
        ).fetchone()["id"]
    )
    former_target_path = f"2024/01/{filename}"
    target_path = f"deleted/{former_target_path}"
    file_id = int(
        conn.execute(
            """
            INSERT INTO files(
                target_path, target_path_key, original_filename,
                stored_filename, sha256, size_bytes, date_source,
                deleted_at, deleted_original_target_path
            )
            VALUES(?, ?, ?, ?, ?, 123, 'filename', ?, ?)
            RETURNING id
            """,
            (
                target_path,
                target_path.casefold(),
                filename,
                filename,
                sha256,
                DELETED_AT,
                former_target_path,
            ),
        ).fetchone()["id"]
    )
    file_source_id = int(
        conn.execute(
            """
            INSERT INTO file_sources(
                file_id, source_id, source_path, source_path_key,
                sha256, size_bytes
            )
            VALUES(?, ?, ?, ?, ?, 123)
            RETURNING id
            """,
            (
                file_id,
                source_id,
                f"C:\\Bilder\\{filename}",
                f"c:\\bilder\\{filename}",
                sha256,
            ),
        ).fetchone()["id"]
    )
    return file_id, file_source_id


def create_pending_purge(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    sha256: str,
    filename: str = "image.jpg",
) -> int:
    return db.create_pending_file_purge(
        conn,
        file_id=file_id,
        expected_path=f"deleted/2024/01/{filename}",
        expected_sha256=sha256,
        expected_size_bytes=123,
        expected_deleted_at=DELETED_AT,
    )


def test_new_database_has_file_purge_schema(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)

    conn = db.connect(target)
    try:
        db.validate_file_purge_schema(conn)
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        triggers = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    finally:
        conn.close()

    assert {"file_tombstones", "pending_file_purges"} <= tables
    assert set(db.FILE_PURGE_TRIGGER_NAMES) <= triggers


def test_sha256_cannot_exist_in_files_and_tombstones(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    conn = db.connect(target)
    try:
        file_id, _ = insert_deleted_file(conn, sha256="file-hash")
        conn.execute(
            """
            INSERT INTO file_tombstones(
                sha256, size_bytes, original_filename,
                former_target_path, purged_at
            )
            VALUES('tombstone-hash', 5, 'old.jpg', '2020/01/old.jpg',
                   CURRENT_TIMESTAMP)
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO file_tombstones(
                    sha256, size_bytes, original_filename,
                    former_target_path, purged_at
                )
                VALUES('file-hash', 123, 'image.jpg', '2024/01/image.jpg',
                       CURRENT_TIMESTAMP)
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE files SET sha256 = 'tombstone-hash' WHERE id = ?",
                (file_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                UPDATE file_tombstones
                SET sha256 = 'file-hash'
                WHERE sha256 = 'tombstone-hash'
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO files(
                    target_path, target_path_key, original_filename,
                    stored_filename, sha256, size_bytes, date_source
                )
                VALUES('2024/02/new.jpg', '2024/02/new.jpg', 'new.jpg',
                       'new.jpg', 'tombstone-hash', 5, 'filename')
                """
            )
    finally:
        conn.close()


def test_pending_purge_restricts_file_delete(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    conn = db.connect(target)
    try:
        file_id, _ = insert_deleted_file(conn, sha256="restricted-hash")
        create_pending_purge(
            conn,
            file_id=file_id,
            sha256="restricted-hash",
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.rollback()

        assert db.pending_file_purge_for_file(conn, file_id=file_id) is not None
        assert conn.execute(
            "SELECT 1 FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
    finally:
        conn.close()


def test_pending_purge_can_be_listed_updated_and_removed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    conn = db.connect(target)
    try:
        file_id, _ = insert_deleted_file(conn, sha256="pending-hash")
        purge_id = create_pending_purge(
            conn,
            file_id=file_id,
            sha256="pending-hash",
        )

        identity = db.pending_file_purge_identity(
            conn,
            purge_id=purge_id,
        )
        assert identity is not None
        assert identity["file_sha256"] == "pending-hash"
        assert [row["id"] for row in db.pending_file_purges(conn)] == [
            purge_id
        ]

        db.update_pending_file_purge_error(
            conn,
            purge_id=purge_id,
            error="simulert feil",
        )
        pending = db.pending_file_purge(conn, purge_id=purge_id)
        assert pending is not None
        assert pending["attempts"] == 1
        assert pending["last_error"] == "simulert feil"

        db.remove_pending_file_purge(conn, purge_id=purge_id)
        assert db.pending_file_purge(conn, purge_id=purge_id) is None
        assert conn.execute(
            "SELECT 1 FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
    finally:
        conn.close()


def test_complete_pending_purge_replaces_file_with_tombstone(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    conn = db.connect(target)
    try:
        file_id, file_source_id = insert_deleted_file(
            conn,
            sha256="completed-hash",
        )
        tag_id = int(
            conn.execute(
                """
                INSERT INTO tags(name, name_key, kind)
                VALUES('Test', 'test', 'user')
                RETURNING id
                """
            ).fetchone()["id"]
        )
        conn.execute(
            "INSERT INTO file_tags(file_id, tag_id) VALUES(?, ?)",
            (file_id, tag_id),
        )
        purge_id = create_pending_purge(
            conn,
            file_id=file_id,
            sha256="completed-hash",
        )

        tombstone_id = db.complete_pending_file_purge(
            conn,
            purge_id=purge_id,
        )
        conn.commit()

        tombstone = db.file_tombstone(
            conn,
            tombstone_id=tombstone_id,
        )
        assert tombstone is not None
        assert tombstone["sha256"] == "completed-hash"
        assert tombstone["size_bytes"] == 123
        assert tombstone["original_filename"] == "image.jpg"
        assert tombstone["former_target_path"] == "2024/01/image.jpg"
        assert db.file_tombstone_by_sha256(
            conn,
            sha256="completed-hash",
        )["id"] == tombstone_id
        assert [row["id"] for row in db.file_tombstones(conn)] == [
            tombstone_id
        ]
        assert db.pending_file_purge(conn, purge_id=purge_id) is None
        assert conn.execute(
            "SELECT 1 FROM files WHERE id = ?",
            (file_id,),
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM file_sources WHERE id = ?",
            (file_source_id,),
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM file_tags WHERE file_id = ?",
            (file_id,),
        ).fetchone() is None
    finally:
        conn.close()


def test_complete_pending_purge_rolls_back_every_database_change(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    conn = db.connect(target)
    try:
        file_id, file_source_id = insert_deleted_file(
            conn,
            sha256="rollback-hash",
        )
        purge_id = create_pending_purge(
            conn,
            file_id=file_id,
            sha256="rollback-hash",
        )
        conn.commit()
        conn.execute(
            """
            CREATE TRIGGER fail_test_tombstone_insert
            BEFORE INSERT ON file_tombstones
            BEGIN
                SELECT RAISE(ABORT, 'injisert tombstone-feil');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="injisert"):
            db.complete_pending_file_purge(conn, purge_id=purge_id)

        assert db.pending_file_purge(conn, purge_id=purge_id) is not None
        assert conn.execute(
            "SELECT 1 FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM file_sources WHERE id = ?",
            (file_source_id,),
        ).fetchone()
        assert db.file_tombstone_by_sha256(
            conn,
            sha256="rollback-hash",
        ) is None
    finally:
        conn.close()
