from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from bildebank import db
from bildebank.cli import main
from bildebank.config import FaceRecognitionConfig
from bildebank.face import connect_face_db
from bildebank.file_lifecycle import remove_file, undelete_file
from bildebank.file_moves import recover_pending_file_moves
from bildebank.openclip import connect_openclip_db
from bildebank.server_actions import remove_file_from_browser
from tests.db_test_helpers import insert_basic_item_sidecar_fixture


def create_two_file_import(tmp_path: Path) -> tuple[Path, tuple[sqlite3.Row, ...]]:
    target = tmp_path / "target"
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_20240102.jpg").write_bytes(b"image-one")
    (source / "IMG_20240103.jpg").write_bytes(b"image-two")
    assert main(["create", str(target)]) == 0
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
    conn = db.connect(target)
    try:
        rows = tuple(
            conn.execute(
                "SELECT id, target_path, sha256 FROM files ORDER BY id"
            )
        )
    finally:
        conn.close()
    return target, rows


def test_remove_cleans_all_item_sidecars_and_undelete_does_not_restore(
    tmp_path: Path,
) -> None:
    target, rows = create_two_file_import(tmp_path)
    removed_row, kept_row = rows
    duplicate_source = tmp_path / "duplicate-source"
    duplicate_source.mkdir()
    (duplicate_source / "COPY_20240102.jpg").write_bytes(b"image-one")
    assert (
        main(
            [
                "--target",
                str(target),
                "import",
                "--name",
                duplicate_source.name,
                "--quiet",
                str(duplicate_source),
            ]
        )
        == 0
    )
    main_conn = db.connect(target)
    try:
        assert main_conn.execute(
            "SELECT COUNT(*) FROM file_sources WHERE file_id = ?",
            (int(removed_row["id"]),),
        ).fetchone()[0] == 2
    finally:
        main_conn.close()

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
        file_id=int(removed_row["id"]),
        target_path=str(removed_row["target_path"]),
        sha256=str(removed_row["sha256"]),
        face_configs=face_configs,
    )

    for face_config in face_configs:
        conn = connect_face_db(target, face_config)
        try:
            conn.execute(
                """
                INSERT INTO scanned_files(
                    file_id, target_path, target_path_key, sha256, status, face_count
                ) VALUES(?, ?, ?, ?, 'ok', 1)
                """,
                (
                    int(kept_row["id"]),
                    str(kept_row["target_path"]),
                    str(kept_row["target_path"]).casefold(),
                    str(kept_row["sha256"]),
                ),
            )
            kept_face_id = int(
                conn.execute(
                    """
                    INSERT INTO faces(
                        file_id, target_path_key, bbox_x, bbox_y, bbox_width,
                        bbox_height, detection_score, embedding_model, embedding
                    ) VALUES(?, ?, 1, 2, 3, 4, 0.9, 'test', X'01')
                    RETURNING id
                    """,
                    (
                        int(kept_row["id"]),
                        str(kept_row["target_path"]).casefold(),
                    ),
                ).fetchone()[0]
            )
            removed_face_id = int(
                conn.execute(
                    "SELECT id FROM faces WHERE file_id = ?",
                    (int(removed_row["id"]),),
                ).fetchone()[0]
            )
            person_id = int(
                conn.execute(
                    "INSERT INTO persons(name) VALUES('Ola') RETURNING id"
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO face_suggestions(
                    person_id, face_id, reference_face_id, similarity
                ) VALUES(?, ?, ?, 0.7)
                """,
                (person_id, kept_face_id, removed_face_id),
            )
            conn.execute(
                """
                INSERT INTO face_suggestions(
                    person_id, face_id, reference_face_id, similarity
                ) VALUES((SELECT id FROM persons WHERE name = 'Kari'), ?, NULL, 0.6)
                """,
                (kept_face_id,),
            )
            conn.commit()
        finally:
            conn.close()

    openclip_conn = connect_openclip_db(target)
    try:
        openclip_conn.execute(
            """
            INSERT INTO image_embeddings(
                file_id, target_path, target_path_key, sha256,
                model_name, pretrained, embedding
            ) VALUES(?, ?, ?, ?, 'test', 'test', X'01')
            """,
            (
                int(kept_row["id"]),
                str(kept_row["target_path"]),
                str(kept_row["target_path"]).casefold(),
                str(kept_row["sha256"]),
            ),
        )
        mixed_run_id = openclip_conn.execute(
            """
            INSERT INTO image_search_runs(
                query, model_name, pretrained, result_limit
            ) VALUES('mixed', 'test', 'test', 2)
            """
        ).lastrowid
        openclip_conn.executemany(
            """
            INSERT INTO image_search_results(
                run_id, file_id, target_path, target_path_key, similarity, rank
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    mixed_run_id,
                    int(removed_row["id"]),
                    str(removed_row["target_path"]),
                    str(removed_row["target_path"]).casefold(),
                    0.9,
                    1,
                ),
                (
                    mixed_run_id,
                    int(kept_row["id"]),
                    str(kept_row["target_path"]),
                    str(kept_row["target_path"]).casefold(),
                    0.8,
                    2,
                ),
            ),
        )
        openclip_conn.commit()
    finally:
        openclip_conn.close()

    remove_file_from_browser(
        target,
        int(removed_row["id"]),
        face_configs[0],
    )

    for face_config in face_configs:
        conn = connect_face_db(target, face_config)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM scanned_files WHERE file_id = ?",
                (int(removed_row["id"]),),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM faces WHERE file_id = ?",
                (int(removed_row["id"]),),
            ).fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM scanned_files WHERE file_id = ?",
                (int(kept_row["id"]),),
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM faces WHERE file_id = ?",
                (int(kept_row["id"]),),
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM face_suggestions"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT reference_face_id FROM face_suggestions"
            ).fetchone()[0] is None
            assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 2
        finally:
            conn.close()

    openclip_conn = connect_openclip_db(target)
    try:
        assert [
            int(row["file_id"])
            for row in openclip_conn.execute(
                "SELECT file_id FROM image_embeddings"
            )
        ] == [int(kept_row["id"])]
        assert [
            int(row["file_id"])
            for row in openclip_conn.execute(
                "SELECT file_id FROM image_search_results"
            )
        ] == [int(kept_row["id"])]
        assert openclip_conn.execute(
            "SELECT COUNT(*) FROM image_search_runs"
        ).fetchone()[0] == 1
    finally:
        openclip_conn.close()

    undelete_file(target, file_id=int(removed_row["id"]))

    for face_config in face_configs:
        conn = connect_face_db(target, face_config)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM scanned_files WHERE file_id = ?",
                (int(removed_row["id"]),),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM faces WHERE file_id = ?",
                (int(removed_row["id"]),),
            ).fetchone()[0] == 0
        finally:
            conn.close()
    openclip_conn = connect_openclip_db(target)
    try:
        assert openclip_conn.execute(
            "SELECT COUNT(*) FROM image_embeddings WHERE file_id = ?",
            (int(removed_row["id"]),),
        ).fetchone()[0] == 0
    finally:
        openclip_conn.close()


def test_remove_recovery_finishes_sidecar_cleanup(tmp_path: Path) -> None:
    target, rows = create_two_file_import(tmp_path)
    removed_row = rows[0]
    face_config = FaceRecognitionConfig(
        database_dir=Path(".custom-faces"),
        model_name="antelopev2",
    )
    insert_basic_item_sidecar_fixture(
        target,
        file_id=int(removed_row["id"]),
        target_path=str(removed_row["target_path"]),
        sha256=str(removed_row["sha256"]),
        face_configs=(face_config,),
    )

    with (
        patch(
            "bildebank.file_lifecycle.delete_attached_item_data",
            side_effect=OSError("simulert feil før sidecar-opprydding"),
        ),
        pytest.raises(OSError, match="simulert feil"),
    ):
        remove_file(
            target,
            file_id=int(removed_row["id"]),
            face_config=face_config,
        )

    assert recover_pending_file_moves(
        target,
        face_config=face_config,
    ) == 1

    conn = connect_face_db(target, face_config)
    try:
        assert conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
    finally:
        conn.close()
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


def test_remove_rejects_invalid_sidecar_before_moving_file(tmp_path: Path) -> None:
    target, rows = create_two_file_import(tmp_path)
    removed_row = rows[0]
    invalid_dir = target / ".custom-faces"
    invalid_dir.mkdir()
    (invalid_dir / "broken.sqlite3").write_bytes(b"not a database")
    face_config = FaceRecognitionConfig(database_dir=Path(".custom-faces"))

    with pytest.raises(sqlite3.DatabaseError):
        remove_file(
            target,
            file_id=int(removed_row["id"]),
            face_config=face_config,
        )

    original_path = target / Path(str(removed_row["target_path"]))
    assert original_path.exists()
    assert not (target / "deleted" / Path(str(removed_row["target_path"]))).exists()
    conn = db.connect(target)
    try:
        row = conn.execute(
            "SELECT target_path, deleted_at FROM files WHERE id = ?",
            (int(removed_row["id"]),),
        ).fetchone()
        assert tuple(row) == (str(removed_row["target_path"]), None)
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_file_moves"
        ).fetchone()[0] == 0
    finally:
        conn.close()
