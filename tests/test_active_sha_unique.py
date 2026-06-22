from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bildebank import db


INDEX_NAME = "idx_files_sha256_active_unique"


def insert_file(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_path: str,
    sha256: str,
    deleted: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO files(
            id, target_path, target_path_key, original_filename, stored_filename,
            sha256, size_bytes, date_source, deleted_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 1, 'filename',
               CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)
        """,
        (
            file_id,
            target_path,
            db.relative_path_key(Path(target_path)),
            Path(target_path).name,
            Path(target_path).name,
            sha256,
            deleted,
        ),
    )


def prepare_without_active_sha_index(target: Path) -> sqlite3.Connection:
    db.init_database(target)
    conn = sqlite3.connect(db.db_path_for_target(target))
    conn.row_factory = sqlite3.Row
    conn.execute(f"DROP INDEX {INDEX_NAME}")
    return conn


def test_migration_creates_active_sha_unique_index_without_duplicates(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    conn = prepare_without_active_sha_index(target)
    try:
        insert_file(
            conn,
            file_id=1,
            target_path="2024/01/first.jpg",
            sha256="first-hash",
        )
        insert_file(
            conn,
            file_id=2,
            target_path="2024/01/second.jpg",
            sha256="second-hash",
        )
        conn.commit()
    finally:
        conn.close()

    db.migrate_database(target)

    conn = sqlite3.connect(db.db_path_for_target(target))
    try:
        indexes = {
            row[1]: (bool(row[2]), bool(row[4]))
            for row in conn.execute("PRAGMA index_list(files)")
        }
    finally:
        conn.close()
    assert indexes[INDEX_NAME] == (True, True)


def test_migration_rejects_duplicate_active_sha256_with_doctor_message(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    conn = prepare_without_active_sha_index(target)
    try:
        insert_file(
            conn,
            file_id=1,
            target_path="2024/01/first.jpg",
            sha256="same-hash",
        )
        insert_file(
            conn,
            file_id=2,
            target_path="2024/01/second.jpg",
            sha256="same-hash",
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="bildebank doctor"):
        db.migrate_database(target)

    conn = sqlite3.connect(db.db_path_for_target(target))
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (INDEX_NAME,),
        ).fetchone() is None
    finally:
        conn.close()


def test_same_sha256_is_allowed_when_one_file_is_deleted(tmp_path: Path) -> None:
    target = tmp_path / "target"
    conn = prepare_without_active_sha_index(target)
    try:
        insert_file(
            conn,
            file_id=1,
            target_path="2024/01/active.jpg",
            sha256="same-hash",
        )
        insert_file(
            conn,
            file_id=2,
            target_path="deleted/2024/01/deleted.jpg",
            sha256="same-hash",
            deleted=True,
        )
        conn.commit()
    finally:
        conn.close()

    db.migrate_database(target)

    conn = sqlite3.connect(db.db_path_for_target(target))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM files WHERE sha256 = ?",
            ("same-hash",),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (INDEX_NAME,),
        ).fetchone() is not None
    finally:
        conn.close()
