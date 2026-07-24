from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from bildebank import db
from bildebank.config import FaceRecognitionConfig
from bildebank.face import LEGACY_FACE_DB_FILENAME, ensure_face_schema_path
from bildebank.openclip import connect_openclip_db
from tests.cli_helpers import capture_cli
from tests.db_test_helpers import insert_openclip_cleanup_fixture


def database_dump(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def add_source_references(
    target: Path,
    *,
    active_id: int,
    deleted_id: int,
) -> None:
    conn = db.connect(target)
    try:
        source_id = int(
            conn.execute(
                """
                INSERT INTO sources(path, path_key, name, imported_at, status)
                VALUES('C:\\Bilder', 'c:\\bilder', 'testkilde',
                       CURRENT_TIMESTAMP, 'imported')
                RETURNING id
                """
            ).fetchone()[0]
        )
        conn.executemany(
            """
            INSERT INTO file_sources(
                file_id, source_id, source_path, source_path_key,
                sha256, size_bytes
            )
            SELECT id, ?, ?, ?, sha256, size_bytes
            FROM files
            WHERE id = ?
            """,
            (
                (
                    source_id,
                    r"C:\Bilder\active.png",
                    r"c:\bilder\active.png",
                    active_id,
                ),
                (
                    source_id,
                    r"C:\Bilder\deleted.png",
                    r"c:\bilder\deleted.png",
                    deleted_id,
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def create_face_cleanup_fixture(
    path: Path,
    *,
    active_id: int,
    deleted_id: int,
    missing_id: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_face_schema_path(path)
    conn = sqlite3.connect(path)
    try:
        person_id = int(
            conn.execute(
                "INSERT INTO persons(name) VALUES('Kari') RETURNING id"
            ).fetchone()[0]
        )
        second_person_id = int(
            conn.execute(
                "INSERT INTO persons(name) VALUES('Ola') RETURNING id"
            ).fetchone()[0]
        )
        face_ids: dict[int, int] = {}
        for file_id, target_path in (
            (active_id, "2024/01/active.png"),
            (deleted_id, "deleted/2024/01/deleted.png"),
            (missing_id, "2026/01/unimported.png"),
        ):
            conn.execute(
                """
                INSERT INTO scanned_files(
                    file_id, target_path, target_path_key, sha256,
                    status, face_count
                ) VALUES(?, ?, ?, ?, 'ok', 1)
                """,
                (file_id, target_path, target_path.casefold(), f"sha-{file_id}"),
            )
            face_ids[file_id] = int(
                conn.execute(
                    """
                    INSERT INTO faces(
                        file_id, target_path_key, bbox_x, bbox_y,
                        bbox_width, bbox_height, detection_score,
                        embedding_model, embedding
                    ) VALUES(?, ?, 1, 2, 3, 4, 0.9, 'test', X'01')
                    RETURNING id
                    """,
                    (file_id, target_path.casefold()),
                ).fetchone()[0]
            )
            conn.execute(
                "INSERT INTO person_faces(person_id, face_id) VALUES(?, ?)",
                (person_id, face_ids[file_id]),
            )
            conn.execute(
                "INSERT INTO person_files(person_id, file_id) VALUES(?, ?)",
                (person_id, file_id),
            )
            conn.execute(
                """
                INSERT INTO face_suggestions(
                    person_id, face_id, reference_face_id, similarity
                ) VALUES(?, ?, ?, 0.8)
                """,
                (person_id, face_ids[file_id], face_ids[file_id]),
            )
        conn.execute(
            """
            INSERT INTO face_suggestions(
                person_id, face_id, reference_face_id, similarity
            ) VALUES(?, ?, ?, 0.7)
            """,
            (
                second_person_id,
                face_ids[active_id],
                face_ids[deleted_id],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def set_main_schema_version(target: Path, version: int) -> None:
    conn = db.connect(target)
    try:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(version),),
        )
        conn.commit()
    finally:
        conn.close()


def add_terminal_and_prepared_file_moves(
    target: Path,
    *,
    file_id: int,
) -> None:
    conn = db.connect(target)
    try:
        conn.executemany(
            """
            INSERT INTO pending_file_moves(
                file_id, from_path, to_path, sha256, operation,
                state, completed_at, last_error
            ) VALUES(?, ?, ?, 'sha-active', 'remove', ?, ?, ?)
            """,
            (
                (
                    file_id,
                    "2024/01/completed.png",
                    "deleted/2024/01/completed.png",
                    "completed",
                    "2026-07-23 10:00:00",
                    None,
                ),
                (
                    file_id,
                    "2024/01/aborted.png",
                    "deleted/2024/01/aborted.png",
                    "aborted",
                    "2026-07-23 11:00:00",
                    None,
                ),
                (
                    file_id,
                    "2024/01/prepared.png",
                    "deleted/2024/01/prepared.png",
                    "prepared",
                    None,
                    "uavklart flytting",
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_migrate_v16_cleans_all_existing_item_sidecars_and_backs_up_faces(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    ids = insert_openclip_cleanup_fixture(target)
    add_source_references(
        target,
        active_id=ids["active_id"],
        deleted_id=ids["deleted_id"],
    )
    face_config = FaceRecognitionConfig(database_dir=Path(".custom-faces"))
    face_paths = (
        target / LEGACY_FACE_DB_FILENAME,
        target / ".custom-faces" / "antelopev2.sqlite3",
        target / ".custom-faces" / "buffalo_l.sqlite3",
    )
    for face_path in face_paths:
        create_face_cleanup_fixture(
            face_path,
            active_id=ids["active_id"],
            deleted_id=ids["deleted_id"],
            missing_id=ids["missing_id"],
        )
    add_terminal_and_prepared_file_moves(
        target,
        file_id=ids["active_id"],
    )
    set_main_schema_version(target, 16)

    plan = db.migration_plan(target)
    result = db.migrate_database(target, face_config=face_config)

    assert plan.terminal_file_moves == 2
    assert result.current_version == 16
    assert result.target_version == 18
    assert result.cleans_item_sidecars
    assert result.terminal_file_moves == 2
    assert len(result.face_database_backups) == len(face_paths)
    assert all(path.is_file() for path in result.face_database_backups)

    for face_path in face_paths:
        conn = sqlite3.connect(face_path)
        try:
            assert conn.execute(
                "SELECT file_id FROM scanned_files"
            ).fetchall() == [(ids["active_id"],)]
            assert conn.execute(
                "SELECT file_id FROM faces"
            ).fetchall() == [(ids["active_id"],)]
            assert conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 2
        finally:
            conn.close()

    for backup_path in result.face_database_backups:
        conn = sqlite3.connect(backup_path)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0] == 4
        finally:
            conn.close()

    openclip_conn = connect_openclip_db(target)
    try:
        assert [
            int(row["file_id"])
            for row in openclip_conn.execute(
                "SELECT file_id FROM image_embeddings"
            )
        ] == [ids["active_id"]]
        assert [
            int(row["file_id"])
            for row in openclip_conn.execute(
                "SELECT file_id FROM image_search_results"
            )
        ] == [ids["active_id"]]
        assert [
            str(row["query"])
            for row in openclip_conn.execute(
                "SELECT query FROM image_search_runs ORDER BY id"
            )
        ] == ["active", "empty"]
    finally:
        openclip_conn.close()

    conn = db.connect(target)
    try:
        assert db.schema_version(conn) == 18
        assert conn.execute(
            "SELECT COUNT(*) FROM file_sources WHERE file_id = ?",
            (ids["active_id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM file_sources WHERE file_id = ?",
            (ids["deleted_id"],),
        ).fetchone()[0] == 1
        assert [
            tuple(row)
            for row in conn.execute(
                """
                SELECT state, completed_at, last_error
                FROM pending_file_moves
                """
            )
        ] == [("prepared", None, "uavklart flytting")]
    finally:
        conn.close()


def test_migrate_v16_rolls_back_main_and_sidecars_after_late_failure(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    ids = insert_openclip_cleanup_fixture(target)
    face_config = FaceRecognitionConfig(database_dir=Path(".custom-faces"))
    face_path = target / ".custom-faces" / "antelopev2.sqlite3"
    create_face_cleanup_fixture(
        face_path,
        active_id=ids["active_id"],
        deleted_id=ids["deleted_id"],
        missing_id=ids["missing_id"],
    )
    add_terminal_and_prepared_file_moves(
        target,
        file_id=ids["active_id"],
    )
    set_main_schema_version(target, 16)
    main_path = db.db_path_for_target(target)
    openclip_path = target / ".bilder-openclip.sqlite3"
    before = {
        main_path: database_dump(main_path),
        face_path: database_dump(face_path),
        openclip_path: database_dump(openclip_path),
    }

    with (
        patch(
            "bildebank.db_schema.validate_database_health",
            side_effect=RuntimeError("injisert sen feil"),
        ),
        pytest.raises(RuntimeError, match="injisert sen feil"),
    ):
        db.migrate_database(target, face_config=face_config)

    assert {path: database_dump(path) for path in before} == before
    backups = list(
        face_path.parent.glob(
            f"{face_path.name}.backup-before-main-schema-17-*"
        )
    )
    assert len(backups) == 1
    assert database_dump(backups[0]) == before[face_path]

    result = db.migrate_database(target, face_config=face_config)
    assert result.current_version == 16
    assert result.target_version == 18
    conn = db.connect(target)
    try:
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT state, last_error FROM pending_file_moves"
            )
        ] == [("prepared", "uavklart flytting")]
    finally:
        conn.close()


def test_migrate_v17_only_cleans_file_move_journal(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    ids = insert_openclip_cleanup_fixture(target)
    face_config = FaceRecognitionConfig(database_dir=Path(".custom-faces"))
    face_path = target / ".custom-faces" / "antelopev2.sqlite3"
    create_face_cleanup_fixture(
        face_path,
        active_id=ids["active_id"],
        deleted_id=ids["deleted_id"],
        missing_id=ids["missing_id"],
    )
    add_terminal_and_prepared_file_moves(
        target,
        file_id=ids["active_id"],
    )
    set_main_schema_version(target, 17)
    openclip_path = target / ".bilder-openclip.sqlite3"
    sidecars_before = {
        face_path: database_dump(face_path),
        openclip_path: database_dump(openclip_path),
    }

    plan = db.migration_plan(target)
    result = db.migrate_database(target, face_config=face_config)

    assert plan.current_version == 17
    assert plan.target_version == 18
    assert not plan.cleans_item_sidecars
    assert plan.terminal_file_moves == 2
    assert result.current_version == 17
    assert result.target_version == 18
    assert not result.cleans_item_sidecars
    assert result.terminal_file_moves == 2
    assert result.face_database_backups == ()
    assert {path: database_dump(path) for path in sidecars_before} == sidecars_before
    assert not list(
        face_path.parent.glob(
            f"{face_path.name}.backup-before-main-schema-17-*"
        )
    )

    conn = db.connect(target)
    try:
        assert db.schema_version(conn) == 18
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT state, last_error FROM pending_file_moves"
            )
        ] == [("prepared", "uavklart flytting")]
    finally:
        conn.close()


def test_migrate_v16_check_does_not_touch_or_back_up_sidecars(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    ids = insert_openclip_cleanup_fixture(target)
    face_path = target / ".bildebank-faces" / "antelopev2.sqlite3"
    create_face_cleanup_fixture(
        face_path,
        active_id=ids["active_id"],
        deleted_id=ids["deleted_id"],
        missing_id=ids["missing_id"],
    )
    add_terminal_and_prepared_file_moves(
        target,
        file_id=ids["active_id"],
    )
    set_main_schema_version(target, 16)
    main_path = db.db_path_for_target(target)
    openclip_path = target / ".bilder-openclip.sqlite3"
    before = {
        main_path: database_dump(main_path),
        face_path: database_dump(face_path),
        openclip_path: database_dump(openclip_path),
    }

    code, stdout, stderr = capture_cli(
        ["--target", str(target), "migrate", "--check"]
    )

    assert code == 0, stderr
    assert "fjerne OpenCLIP- og InsightFace-data" in stdout
    assert (
        "fjerne ferdigbehandlede filflyttinger fra arbeidsjournalen: 2"
        in stdout
    )
    assert "Ingen endringer er gjort (--check)." in stdout
    assert {path: database_dump(path) for path in before} == before
    assert not list(
        target.glob(".bilder.sqlite3.backup-before-schema-18-*")
    )
    assert not list(
        face_path.parent.glob(
            f"{face_path.name}.backup-before-main-schema-17-*"
        )
    )
