from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.cli import main
from bildebank.config import AppConfig, FaceRecognitionConfig
from bildebank.face import connect_face_db, face_db_path
from bildebank.file_lifecycle import remove_file
from bildebank.media import sha256_file
from bildebank.openclip import connect_openclip_db
from bildebank.pending_deletes import cleanup_pending_deletes, list_pending_deletes
from bildebank.unimport import run_unimport
from tests.db_test_helpers import insert_basic_item_sidecar_fixture


def run_cli(args: list[str]) -> int:
    return main(args)


def create_single_file_import(tmp_path: Path) -> tuple[Path, Path, Path]:
    target = tmp_path / "target"
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "IMG_20240102.jpg"
    source_file.write_bytes(b"image")
    assert run_cli(["create", str(target)]) == 0
    assert (
        run_cli(
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
    return target, source, target / "2024" / "01" / source_file.name


def test_unimport_delete_failure_commits_database_and_keeps_pending_row(
    tmp_path: Path,
) -> None:
    target, source, imported = create_single_file_import(tmp_path)

    def fail_imported(candidate: Path, *args, **kwargs) -> None:
        if candidate == imported:
            raise PermissionError("simulert låst fil")
        os.unlink(candidate)

    with patch.object(Path, "unlink", autospec=True, side_effect=fail_imported):
        result = run_unimport(
            target,
            source.name,
            config=AppConfig(),
            dry_run=False,
            confirm=lambda _plan: True,
        )

    assert result.applied
    assert imported.exists()
    conn = db.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    finally:
        conn.close()
    rows = list_pending_deletes(target)
    assert len(rows) == 1
    assert rows[0].reason == "unimport"
    assert rows[0].source_id == 1
    assert rows[0].expected_sha256 == sha256_file(imported)
    assert rows[0].expected_size_bytes == imported.stat().st_size
    assert rows[0].attempts == 1
    assert rows[0].last_error == "simulert låst fil"


def test_cleanup_can_finish_failed_unimport_delete(tmp_path: Path) -> None:
    target, source, imported = create_single_file_import(tmp_path)

    def fail_imported(candidate: Path, *args, **kwargs) -> None:
        if candidate == imported:
            raise PermissionError("simulert låst fil")
        os.unlink(candidate)

    with patch.object(Path, "unlink", autospec=True, side_effect=fail_imported):
        run_unimport(
            target,
            source.name,
            config=AppConfig(),
            dry_run=False,
            confirm=lambda _plan: True,
        )

    results = cleanup_pending_deletes(target)

    assert [item.outcome for item in results] == ["deleted"]
    assert not imported.exists()
    assert list_pending_deletes(target) == []


def test_unimport_dry_run_changes_neither_database_nor_filesystem(
    tmp_path: Path,
) -> None:
    target, source, imported = create_single_file_import(tmp_path)
    conn = db.connect(target)
    try:
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sources", "files", "file_sources", "pending_file_deletes")
        }
    finally:
        conn.close()

    result = run_unimport(
        target,
        source.name,
        config=AppConfig(),
        dry_run=True,
        confirm=lambda _plan: (_ for _ in ()).throw(
            AssertionError("dry-run skal ikke be om bekreftelse")
        ),
    )

    conn = db.connect(target)
    try:
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sources", "files", "file_sources", "pending_file_deletes")
        }
    finally:
        conn.close()
    assert not result.applied
    assert before == after
    assert imported.exists()
    assert result.plan.target_paths_to_delete == (imported,)
    assert result.plan.target_paths_to_keep == ()


def test_deleted_duplicate_stays_deleted_until_last_source_is_unimported(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    source_a = tmp_path / "source-a"
    source_b = tmp_path / "source-b"
    source_a.mkdir()
    source_b.mkdir()
    (source_a / "IMG_20240102.jpg").write_bytes(b"same image")
    (source_b / "COPY_20240102.jpg").write_bytes(b"same image")

    assert run_cli(["create", str(target)]) == 0
    for source in (source_a, source_b):
        if source == source_b:
            remove_file(target, file_id=1)
        assert (
            run_cli(
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

    deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
    conn = db.connect(target)
    try:
        file_row = conn.execute(
            "SELECT id, target_path, deleted_at FROM files"
        ).fetchone()
        source_count = conn.execute(
            "SELECT COUNT(*) FROM file_sources WHERE file_id = ?",
            (file_row["id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert file_row["target_path"] == "deleted/2024/01/IMG_20240102.jpg"
    assert file_row["deleted_at"] is not None
    assert source_count == 2
    assert deleted.exists()

    first = run_unimport(
        target,
        source_a.name,
        config=AppConfig(),
        dry_run=False,
        confirm=lambda _plan: True,
    )

    assert first.plan.active_keep_count == 0
    assert first.plan.target_paths_to_keep == (deleted,)
    assert deleted.exists()
    conn = db.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0] == 1
    finally:
        conn.close()

    second = run_unimport(
        target,
        source_b.name,
        config=AppConfig(),
        dry_run=False,
        confirm=lambda _plan: True,
    )

    assert second.plan.active_remove_count == 0
    assert second.plan.target_paths_to_delete == (deleted,)
    assert not deleted.exists()
    conn = db.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    finally:
        conn.close()
    assert list_pending_deletes(target) == []


def test_unimport_keeps_sidecars_until_last_source_reference(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    source_a = tmp_path / "source-a"
    source_b = tmp_path / "source-b"
    source_a.mkdir()
    source_b.mkdir()
    (source_a / "IMG_20240102.jpg").write_bytes(b"same image")
    (source_b / "COPY_20240102.jpg").write_bytes(b"same image")

    assert run_cli(["create", str(target)]) == 0
    for source in (source_a, source_b):
        assert (
            run_cli(
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

    conn = db.connect(target)
    try:
        file_row = conn.execute(
            "SELECT id, target_path, sha256 FROM files"
        ).fetchone()
        assert conn.execute(
            "SELECT COUNT(*) FROM file_sources WHERE file_id = ?",
            (int(file_row["id"]),),
        ).fetchone()[0] == 2
    finally:
        conn.close()

    face_configs = (
        FaceRecognitionConfig(
            database_dir=Path(".custom-faces"),
            model_name="buffalo_l",
        ),
        FaceRecognitionConfig(
            database_dir=Path(".custom-faces"),
            model_name="antelopev2",
        ),
    )
    insert_basic_item_sidecar_fixture(
        target,
        file_id=int(file_row["id"]),
        target_path=str(file_row["target_path"]),
        sha256=str(file_row["sha256"]),
        face_configs=face_configs,
    )
    config = AppConfig(face_recognition=face_configs[0])

    first = run_unimport(
        target,
        source_a.name,
        config=config,
        dry_run=False,
        confirm=lambda _plan: True,
    )

    assert first.plan.file_ids_to_delete == ()
    for face_config in face_configs:
        face_conn = connect_face_db(target, face_config)
        try:
            for table in (
                "scanned_files",
                "faces",
                "person_faces",
                "person_files",
                "face_suggestions",
            ):
                assert face_conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0] == 1
        finally:
            face_conn.close()
    openclip_conn = connect_openclip_db(target)
    try:
        assert openclip_conn.execute(
            "SELECT COUNT(*) FROM image_embeddings"
        ).fetchone()[0] == 1
        assert openclip_conn.execute(
            "SELECT COUNT(*) FROM image_search_results"
        ).fetchone()[0] == 1
    finally:
        openclip_conn.close()

    second = run_unimport(
        target,
        source_b.name,
        config=config,
        dry_run=False,
        confirm=lambda _plan: True,
    )

    assert second.plan.file_ids_to_delete == (int(file_row["id"]),)
    for face_config in face_configs:
        face_conn = connect_face_db(target, face_config)
        try:
            for table in (
                "scanned_files",
                "faces",
                "person_faces",
                "person_files",
                "face_suggestions",
            ):
                assert face_conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0] == 0
            assert face_conn.execute(
                "SELECT COUNT(*) FROM persons"
            ).fetchone()[0] == 1
        finally:
            face_conn.close()
    openclip_conn = connect_openclip_db(target)
    try:
        assert openclip_conn.execute(
            "SELECT COUNT(*) FROM image_embeddings"
        ).fetchone()[0] == 0
        assert openclip_conn.execute(
            "SELECT COUNT(*) FROM image_search_results"
        ).fetchone()[0] == 0
        assert openclip_conn.execute(
            "SELECT COUNT(*) FROM image_search_runs"
        ).fetchone()[0] == 0
    finally:
        openclip_conn.close()


def test_unimport_removes_item_dependent_face_and_openclip_rows(
    tmp_path: Path,
) -> None:
    target, source, _imported = create_single_file_import(tmp_path)
    config = AppConfig()
    face_conn = connect_face_db(target, config.face_recognition)
    try:
        face_conn.execute(
            """
            INSERT INTO scanned_files(
                file_id, target_path, target_path_key, sha256, status, face_count
            )
            VALUES(1, '2024/01/IMG_20240102.jpg',
                   '2024/01/IMG_20240102.jpg', 'hash', 'ok', 1)
            """
        )
        face_conn.execute(
            """
            INSERT INTO faces(
                id, file_id, target_path_key, bbox_x, bbox_y, bbox_width,
                bbox_height, detection_score, embedding_model, embedding
            )
            VALUES(1, 1, '2024/01/IMG_20240102.jpg',
                   1, 2, 3, 4, 0.9, 'test', X'00')
            """
        )
        face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
        face_conn.execute(
            "INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)"
        )
        face_conn.execute(
            "INSERT INTO person_files(person_id, file_id) VALUES(1, 1)"
        )
        face_conn.execute(
            """
            INSERT INTO face_suggestions(
                person_id, face_id, reference_face_id, similarity
            )
            VALUES(1, 1, 1, 0.8)
            """
        )
        face_conn.commit()
    finally:
        face_conn.close()

    openclip_conn = connect_openclip_db(target)
    try:
        openclip_conn.execute(
            """
            INSERT INTO image_embeddings(
                file_id, target_path, target_path_key, sha256,
                model_name, pretrained, embedding
            )
            VALUES(1, '2024/01/IMG_20240102.jpg',
                   '2024/01/IMG_20240102.jpg', 'hash',
                   'test', 'test', X'00')
            """
        )
        run_id = openclip_conn.execute(
            """
            INSERT INTO image_search_runs(
                query, model_name, pretrained, result_limit
            )
            VALUES('test', 'test', 'test', 1)
            """
        ).lastrowid
        openclip_conn.execute(
            """
            INSERT INTO image_search_results(
                run_id, file_id, target_path, target_path_key, similarity, rank
            )
            VALUES(?, 1, '2024/01/IMG_20240102.jpg',
                   '2024/01/IMG_20240102.jpg', 0.9, 1)
            """,
            (run_id,),
        )
        openclip_conn.execute("DELETE FROM meta WHERE key = 'schema_version'")
        openclip_conn.commit()
    finally:
        openclip_conn.close()

    run_unimport(
        target,
        source.name,
        config=config,
        dry_run=False,
        confirm=lambda _plan: True,
    )

    face_conn = connect_face_db(target, config.face_recognition)
    try:
        for table in (
            "scanned_files",
            "faces",
            "person_faces",
            "person_files",
            "face_suggestions",
        ):
            assert face_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        face_conn.close()
    openclip_conn = sqlite3.connect(target / ".bilder-openclip.sqlite3")
    try:
        assert openclip_conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "1"
        assert openclip_conn.execute(
            "SELECT COUNT(*) FROM image_embeddings"
        ).fetchone()[0] == 0
        assert openclip_conn.execute(
            "SELECT COUNT(*) FROM image_search_results"
        ).fetchone()[0] == 0
        assert openclip_conn.execute(
            "SELECT COUNT(*) FROM image_search_runs"
        ).fetchone()[0] == 0
    finally:
        openclip_conn.close()


def test_unimport_migrates_legacy_face_db_before_deleting_item_rows(
    tmp_path: Path,
) -> None:
    target, source, _imported = create_single_file_import(tmp_path)
    config = AppConfig()
    legacy_face_path = face_db_path(target, config.face_recognition)
    legacy_face_path.parent.mkdir(parents=True, exist_ok=True)
    face_conn = sqlite3.connect(legacy_face_path)
    try:
        face_conn.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO meta(key, value) VALUES('schema_version', '4');

            CREATE TABLE scanned_files (
                file_id INTEGER PRIMARY KEY,
                target_path TEXT NOT NULL,
                target_path_key TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                face_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE faces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                target_path_key TEXT NOT NULL,
                bbox_x REAL NOT NULL,
                bbox_y REAL NOT NULL,
                bbox_width REAL NOT NULL,
                bbox_height REAL NOT NULL,
                detection_score REAL NOT NULL,
                embedding_model TEXT NOT NULL,
                embedding BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE person_faces (
                person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
                face_id INTEGER NOT NULL,
                confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(person_id, face_id)
            );
            CREATE TABLE person_files (
                person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
                file_id INTEGER NOT NULL,
                confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(person_id, file_id)
            );
            CREATE TABLE face_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
                face_id INTEGER NOT NULL,
                similarity REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(person_id, face_id)
            );

            INSERT INTO scanned_files(
                file_id, target_path, target_path_key, sha256, status, face_count
            )
            VALUES(1, '2024/01/IMG_20240102.jpg',
                   '2024/01/IMG_20240102.jpg', 'hash', 'ok', 1);
            INSERT INTO faces(
                id, file_id, target_path_key, bbox_x, bbox_y, bbox_width,
                bbox_height, detection_score, embedding_model, embedding
            )
            VALUES(1, 1, '2024/01/IMG_20240102.jpg',
                   1, 2, 3, 4, 0.9, 'test', X'00');
            INSERT INTO persons(id, name) VALUES(1, 'Kari');
            INSERT INTO person_faces(person_id, face_id) VALUES(1, 1);
            INSERT INTO person_files(person_id, file_id) VALUES(1, 1);
            INSERT INTO face_suggestions(person_id, face_id, similarity)
            VALUES(1, 1, 0.8);
            """
        )
        face_conn.commit()
    finally:
        face_conn.close()

    run_unimport(
        target,
        source.name,
        config=config,
        dry_run=False,
        confirm=lambda _plan: True,
    )

    face_conn = connect_face_db(target, config.face_recognition)
    try:
        columns = {row["name"] for row in face_conn.execute("PRAGMA table_info(face_suggestions)")}
        assert "reference_face_id" in columns
        for table in (
            "scanned_files",
            "faces",
            "person_faces",
            "person_files",
            "face_suggestions",
        ):
            assert face_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        face_conn.close()
