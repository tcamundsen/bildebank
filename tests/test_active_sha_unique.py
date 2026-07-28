from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from bildebank import db
from bildebank.pending_deletes import cleanup_pending_deletes, list_pending_deletes
from tests.cli_helpers import capture_cli


INDEX_NAME = "idx_files_sha256_unique"


def prepare_v18_database(target: Path) -> sqlite3.Connection:
    db.init_database(target)
    conn = sqlite3.connect(db.db_path_for_target(target))
    conn.row_factory = sqlite3.Row
    conn.execute(f"DROP INDEX {INDEX_NAME}")
    conn.execute("CREATE INDEX idx_files_sha256 ON files(sha256)")
    conn.execute(
        """
        CREATE UNIQUE INDEX idx_files_sha256_active_unique
        ON files(sha256)
        WHERE deleted_at IS NULL
        """
    )
    conn.execute(
        "UPDATE meta SET value = '18' WHERE key = 'schema_version'"
    )
    return conn


def insert_file(
    conn: sqlite3.Connection,
    target: Path,
    *,
    file_id: int,
    target_path: str,
    content: bytes,
    sha256: str | None = None,
    deleted: bool = False,
) -> str:
    media_path = target / target_path
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(content)
    digest = sha256 or hashlib.sha256(content).hexdigest()
    conn.execute(
        """
        INSERT INTO files(
            id, target_path, target_path_key, original_filename, stored_filename,
            sha256, size_bytes, date_source, deleted_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, 'filename',
               CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)
        """,
        (
            file_id,
            target_path,
            db.relative_path_key(Path(target_path)),
            media_path.name,
            media_path.name,
            digest,
            len(content),
            deleted,
        ),
    )
    return digest


def sha_indexes(conn: sqlite3.Connection) -> dict[str, tuple[bool, bool]]:
    return {
        str(row["name"]): (bool(row["unique"]), bool(row["partial"]))
        for row in conn.execute("PRAGMA index_list(files)")
        if "sha256" in str(row["name"])
    }


def test_migration_creates_global_sha_unique_index_without_duplicates(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    conn = prepare_v18_database(target)
    try:
        insert_file(
            conn,
            target,
            file_id=1,
            target_path="2024/01/first.jpg",
            content=b"first",
        )
        insert_file(
            conn,
            target,
            file_id=2,
            target_path="2024/01/second.jpg",
            content=b"second",
        )
        conn.commit()
    finally:
        conn.close()

    result = db.migrate_database(target)

    assert result.current_version == 18
    assert result.target_version == db.SCHEMA_VERSION
    assert result.duplicate_sha256_groups == 0
    conn = db.connect(target)
    try:
        assert sha_indexes(conn) == {INDEX_NAME: (True, False)}
    finally:
        conn.close()


def test_current_migration_repairs_non_unique_sha_index_with_expected_name(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    conn = sqlite3.connect(db.db_path_for_target(target))
    try:
        conn.execute(f"DROP INDEX {INDEX_NAME}")
        conn.execute(
            f"CREATE INDEX {INDEX_NAME} ON files(sha256)"
        )
        conn.commit()
    finally:
        conn.close()

    assert db.migration_plan(target).refreshes_performance_indexes
    db.migrate_database(target)

    conn = db.connect(target)
    try:
        assert sha_indexes(conn) == {INDEX_NAME: (True, False)}
    finally:
        conn.close()


def test_current_migration_repairs_duplicates_when_unique_index_is_damaged(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    conn = sqlite3.connect(db.db_path_for_target(target))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"DROP INDEX {INDEX_NAME}")
        conn.execute(f"CREATE INDEX {INDEX_NAME} ON files(sha256)")
        insert_file(
            conn,
            target,
            file_id=1,
            target_path="2024/01/first.jpg",
            content=b"same image",
        )
        insert_file(
            conn,
            target,
            file_id=2,
            target_path="2024/01/second.jpg",
            content=b"same image",
        )
        conn.commit()
    finally:
        conn.close()

    result = db.migrate_database(target)

    assert result.current_version == db.SCHEMA_VERSION
    assert result.target_version == db.SCHEMA_VERSION
    assert result.duplicate_sha256_groups == 1
    conn = db.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert sha_indexes(conn) == {INDEX_NAME: (True, False)}
    finally:
        conn.close()


def test_migration_merges_duplicate_rows_sources_and_tags(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    conn = prepare_v18_database(target)
    try:
        conn.execute("DROP INDEX idx_files_sha256_active_unique")
        conn.execute(
            "INSERT INTO sources(id, path, name, status) VALUES(1, 'D:/one', 'one', 'imported')"
        )
        conn.execute(
            "INSERT INTO sources(id, path, name, status) VALUES(2, 'D:/two', 'two', 'imported')"
        )
        digest = insert_file(
            conn,
            target,
            file_id=1,
            target_path="2024/01/first.jpg",
            content=b"same image",
        )
        insert_file(
            conn,
            target,
            file_id=2,
            target_path="2024/01/second.jpg",
            content=b"same image",
        )
        for source_id, file_id in ((1, 1), (2, 2)):
            conn.execute(
                """
                INSERT INTO file_sources(
                    file_id, source_id, source_path, source_path_key,
                    sha256, size_bytes
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    source_id,
                    f"source-{source_id}.jpg",
                    f"source-{source_id}.jpg",
                    digest,
                    len(b"same image"),
                ),
            )
        db.tag_file(conn, file_id=2, tag_name="Fra duplikatraden")
        conn.commit()
    finally:
        conn.close()

    result = db.migrate_database(target)

    assert result.duplicate_sha256_groups == 1
    assert result.duplicate_sha256_files == 1
    assert result.duplicate_review_files == 1
    assert len(result.duplicate_pending_delete_ids) == 1

    conn = db.connect(target)
    try:
        assert [
            tuple(row)
            for row in conn.execute("SELECT id, sha256 FROM files ORDER BY id")
        ] == [(1, digest)]
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT source_id, file_id FROM file_sources ORDER BY source_id"
            )
        ] == [(1, 1), (2, 1)]
        tag_names = {
            str(row["name"])
            for row in db.tags_for_file(conn, 1)
        }
        assert tag_names == {
            "Fra duplikatraden",
            db.SYSTEM_TAG_DUPLICATE_REPAIR_REVIEW,
        }
        assert sha_indexes(conn) == {INDEX_NAME: (True, False)}
        with pytest.raises(sqlite3.IntegrityError):
            insert_file(
                conn,
                target,
                file_id=3,
                target_path="deleted/2024/01/third.jpg",
                content=b"same image",
                deleted=True,
            )
    finally:
        conn.close()

    assert (target / "2024/01/first.jpg").exists()
    assert (target / "2024/01/second.jpg").exists()
    cleanup_results = cleanup_pending_deletes(
        target,
        pending_ids=result.duplicate_pending_delete_ids,
    )
    assert [cleanup_result.outcome for cleanup_result in cleanup_results] == [
        "deleted"
    ]
    assert (target / "2024/01/first.jpg").exists()
    assert not (target / "2024/01/second.jpg").exists()


def test_migration_prefers_active_row_over_deleted_duplicate(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    conn = prepare_v18_database(target)
    try:
        digest = insert_file(
            conn,
            target,
            file_id=1,
            target_path="2024/01/active.jpg",
            content=b"same image",
        )
        insert_file(
            conn,
            target,
            file_id=2,
            target_path="deleted/2024/01/deleted.jpg",
            content=b"same image",
            deleted=True,
        )
        conn.commit()
    finally:
        conn.close()

    result = db.migrate_database(target)

    conn = db.connect(target)
    try:
        row = conn.execute(
            "SELECT id, deleted_at FROM files WHERE sha256 = ?",
            (digest,),
        ).fetchone()
        assert row is not None
        assert (int(row["id"]), row["deleted_at"]) == (1, None)
    finally:
        conn.close()
    cleanup_pending_deletes(
        target,
        pending_ids=result.duplicate_pending_delete_ids,
    )
    assert (target / "2024/01/active.jpg").exists()
    assert not (target / "deleted/2024/01/deleted.jpg").exists()


def test_migration_keeps_first_physically_valid_duplicate(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    conn = prepare_v18_database(target)
    try:
        digest = hashlib.sha256(b"expected image").hexdigest()
        insert_file(
            conn,
            target,
            file_id=1,
            target_path="2024/01/active-but-changed.jpg",
            content=b"changed image!",
            sha256=digest,
        )
        insert_file(
            conn,
            target,
            file_id=2,
            target_path="deleted/2024/01/valid.jpg",
            content=b"expected image",
            deleted=True,
        )
        conn.commit()
    finally:
        conn.close()

    result = db.migrate_database(target)

    conn = db.connect(target)
    try:
        assert conn.execute(
            "SELECT id FROM files WHERE sha256 = ?",
            (digest,),
        ).fetchone()[0] == 2
    finally:
        conn.close()
    cleanup_results = cleanup_pending_deletes(
        target,
        pending_ids=result.duplicate_pending_delete_ids,
    )
    assert cleanup_results[0].outcome == "failed"
    assert (target / "2024/01/active-but-changed.jpg").exists()
    assert (target / "deleted/2024/01/valid.jpg").exists()
    assert len(list_pending_deletes(target)) == 1


def test_migration_completes_when_no_duplicate_copy_matches_database_hash(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    conn = prepare_v18_database(target)
    try:
        conn.execute("DROP INDEX idx_files_sha256_active_unique")
        expected_digest = hashlib.sha256(b"expected image").hexdigest()
        insert_file(
            conn,
            target,
            file_id=1,
            target_path="2024/01/first-changed.jpg",
            content=b"first changed",
            sha256=expected_digest,
        )
        insert_file(
            conn,
            target,
            file_id=2,
            target_path="2024/01/second-changed.jpg",
            content=b"second changed",
            sha256=expected_digest,
        )
        conn.commit()
    finally:
        conn.close()

    result = db.migrate_database(target)

    assert result.target_version == db.SCHEMA_VERSION
    conn = db.connect(target)
    try:
        assert conn.execute(
            "SELECT id FROM files WHERE sha256 = ?",
            (expected_digest,),
        ).fetchone()[0] == 1
    finally:
        conn.close()
    cleanup_results = cleanup_pending_deletes(
        target,
        pending_ids=result.duplicate_pending_delete_ids,
    )
    assert cleanup_results[0].outcome == "failed"
    assert (target / "2024/01/first-changed.jpg").exists()
    assert (target / "2024/01/second-changed.jpg").exists()


def test_duplicate_repair_rolls_back_before_physical_cleanup_on_late_failure(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    conn = prepare_v18_database(target)
    try:
        conn.execute("DROP INDEX idx_files_sha256_active_unique")
        insert_file(
            conn,
            target,
            file_id=1,
            target_path="2024/01/first.jpg",
            content=b"same image",
        )
        insert_file(
            conn,
            target,
            file_id=2,
            target_path="2024/01/second.jpg",
            content=b"same image",
        )
        conn.commit()
    finally:
        conn.close()

    with (
        patch(
            "bildebank.db_schema.validate_database_health",
            side_effect=RuntimeError("injisert sen feil"),
        ),
        pytest.raises(RuntimeError, match="injisert sen feil"),
    ):
        db.migrate_database(target)

    conn = sqlite3.connect(db.db_path_for_target(target))
    try:
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "18"
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_file_deletes"
        ).fetchone()[0] == 0
    finally:
        conn.close()
    assert (target / "2024/01/first.jpg").exists()
    assert (target / "2024/01/second.jpg").exists()


def test_duplicate_repair_rejects_unresolved_move_for_redundant_row(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    conn = prepare_v18_database(target)
    try:
        conn.execute("DROP INDEX idx_files_sha256_active_unique")
        digest = insert_file(
            conn,
            target,
            file_id=1,
            target_path="2024/01/first.jpg",
            content=b"same image",
        )
        insert_file(
            conn,
            target,
            file_id=2,
            target_path="2024/01/second.jpg",
            content=b"same image",
        )
        conn.execute(
            """
            INSERT INTO pending_file_moves(
                file_id, from_path, to_path, sha256, operation, state
            ) VALUES(
                2, '2024/01/second.jpg',
                'deleted/2024/01/second.jpg', ?, 'remove', 'prepared'
            )
            """,
            (digest,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="pending_file_moves"):
        db.migrate_database(target)

    conn = sqlite3.connect(db.db_path_for_target(target))
    try:
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "18"
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2
    finally:
        conn.close()


def test_migrate_cli_removes_redundant_copy_and_prints_review_tag(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    conn = prepare_v18_database(target)
    try:
        conn.execute("DROP INDEX idx_files_sha256_active_unique")
        insert_file(
            conn,
            target,
            file_id=1,
            target_path="2024/01/first.jpg",
            content=b"same image",
        )
        insert_file(
            conn,
            target,
            file_id=2,
            target_path="2024/01/second.jpg",
            content=b"same image",
        )
        conn.commit()
    finally:
        conn.close()

    code, stdout, stderr = capture_cli(
        ["--target", str(target), "migrate"]
    )

    assert code == 0, stderr
    assert "Reparerte duplikate SHA-256-verdier for 1 bilde(r)." in stdout
    assert db.SYSTEM_TAG_DUPLICATE_REPAIR_REVIEW in stdout
    assert (target / "2024/01/first.jpg").exists()
    assert not (target / "2024/01/second.jpg").exists()
    assert list_pending_deletes(target) == []
