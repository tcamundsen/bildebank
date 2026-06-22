from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.cli import main
from bildebank.config import AppConfig
from bildebank.face import connect_face_db
from bildebank.file_lifecycle import remove_file
from bildebank.openclip import connect_openclip_db
from bildebank.pending_deletes import cleanup_pending_deletes, list_pending_deletes
from bildebank.unimport import run_unimport


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
    openclip_conn = connect_openclip_db(target)
    try:
        assert openclip_conn.execute(
            "SELECT COUNT(*) FROM image_embeddings"
        ).fetchone()[0] == 0
        assert openclip_conn.execute(
            "SELECT COUNT(*) FROM image_search_results"
        ).fetchone()[0] == 0
    finally:
        openclip_conn.close()
