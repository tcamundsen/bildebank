from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from bildebank import db
from bildebank.config import OpenClipConfig
from bildebank.embedding_vectors import (
    load_validated_embedding_matrix,
    normalized_embedding_blob,
)
from bildebank.image_clustering import (
    ClusterMemberResult,
    ClusterResult,
    ClusteringParameters,
    ClusteringResult,
    cluster_embedding_matrix,
    parse_clustering_selection,
    run_image_clustering,
)
from bildebank.openclip import (
    connect_openclip_db,
    connect_openclip_db_read_only,
    embedding_blob,
    get_meta,
)
from tests.db_test_helpers import insert_test_file


def memory_embedding_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE image_embeddings (
            file_id INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            model_name TEXT NOT NULL,
            pretrained TEXT NOT NULL,
            embedding BLOB NOT NULL,
            PRIMARY KEY(file_id, model_name, pretrained)
        )
        """
    )
    return conn


def clusterable_target(tmp_path: Path) -> Path:
    target = tmp_path / "collection"
    target.mkdir()
    db.init_database(target)
    file_id = insert_test_file(target, "2024/01/one.png", sha256="sha-1")
    conn = connect_openclip_db(target)
    try:
        conn.execute(
            """
            INSERT INTO image_embeddings(
                file_id, target_path, target_path_key, sha256,
                model_name, pretrained, embedding
            ) VALUES(?, '2024/01/one.png', '2024/01/one.png',
                     'sha-1', 'model', 'weights', ?)
            """,
            (file_id, embedding_blob([1.0, 0.0])),
        )
        conn.commit()
    finally:
        conn.close()
    return target


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"\x00",
        np.asarray([0.0, 0.0], dtype=np.float32).tobytes(),
        np.asarray([np.nan, 1.0], dtype=np.float32).tobytes(),
        np.asarray([np.inf, 1.0], dtype=np.float32).tobytes(),
    ],
)
def test_embedding_blob_validation_rejects_invalid_vectors(blob: bytes) -> None:
    with pytest.raises(ValueError):
        normalized_embedding_blob(blob)


def test_embedding_loader_counts_missing_invalid_and_normalizes() -> None:
    conn = memory_embedding_connection()
    try:
        conn.executemany(
            """
            INSERT INTO image_embeddings(
                file_id, sha256, model_name, pretrained, embedding
            ) VALUES(?, ?, 'model', 'weights', ?)
            """,
            (
                (1, "sha-1", embedding_blob([3.0, 4.0])),
                (2, "wrong-sha", embedding_blob([1.0, 0.0])),
                (3, "sha-3", embedding_blob([1.0, 0.0, 0.0])),
                (4, "sha-4", embedding_blob([0.0, 0.0])),
            ),
        )
        result = load_validated_embedding_matrix(
            conn,
            (
                (5, "sha-5"),
                (4, "sha-4"),
                (3, "sha-3"),
                (2, "sha-2"),
                (1, "sha-1"),
            ),
            model_name="model",
            pretrained="weights",
        )
    finally:
        conn.close()

    assert result.file_ids == (1,)
    assert result.dimension == 2
    assert result.valid_count == 1
    assert result.missing_count == 1
    assert result.invalid_count == 3
    np.testing.assert_allclose(result.matrix, [[0.6, 0.8]])


def test_algorithm_is_deterministic_and_ranks_equal_distance_by_file_id() -> None:
    matrix = np.asarray(
        [
            [1.0, 0.0],
            [1.0, 0.0],
            [0.0, 1.0],
            [0.0, 1.0],
        ],
        dtype=np.float32,
    )
    parameters = ClusteringParameters(n_clusters=2, random_seed=7)

    first = cluster_embedding_matrix(matrix, (10, 11, 20, 21), parameters)
    second = cluster_embedding_matrix(matrix, (10, 11, 20, 21), parameters)

    assert [
        tuple(member.file_id for member in cluster.members)
        for cluster in first.clusters
    ] == [
        tuple(member.file_id for member in cluster.members)
        for cluster in second.clusters
    ]
    assert [cluster.members[0].file_id for cluster in first.clusters] == [10, 20]
    assert first.actual_cluster_count == 2


def test_algorithm_rejects_fewer_embeddings_than_clusters() -> None:
    with pytest.raises(ValueError, match="Fant bare 1"):
        cluster_embedding_matrix(
            np.asarray([[1.0, 0.0]], dtype=np.float32),
            (1,),
            ClusteringParameters(n_clusters=2),
        )


def test_selection_canonicalizes_filter_and_rejects_deleted(
    tmp_path: Path,
) -> None:
    selection = parse_clustering_selection(
        tmp_path,
        "year >= 1990 tag:Ferie",
        hide_out_of_focus=True,
    )
    assert selection.kind == "filter"
    assert selection.query == "year>=1990 tag:Ferie"
    assert selection.hide_out_of_focus is True
    with pytest.raises(ValueError, match="is:deleted"):
        parse_clustering_selection(tmp_path, "is:deleted")


def test_run_service_persists_atomic_completed_result(tmp_path: Path) -> None:
    target = tmp_path / "collection"
    target.mkdir()
    db.init_database(target)
    file_ids = [
        insert_test_file(target, f"2024/01/{index}.png", sha256=f"sha-{index}")
        for index in range(4)
    ]
    conn = connect_openclip_db(target)
    try:
        for file_id, vector in zip(
            file_ids,
            ([1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [0.1, 0.9]),
            strict=True,
        ):
            conn.execute(
                """
                INSERT INTO image_embeddings(
                    file_id, target_path, target_path_key, sha256,
                    model_name, pretrained, embedding
                ) VALUES(?, ?, ?, ?, 'model', 'weights', ?)
                """,
                (
                    file_id,
                    f"2024/01/{file_id}.png",
                    f"2024/01/{file_id}.png",
                    f"sha-{file_id - file_ids[0]}",
                    embedding_blob(vector),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = run_image_clustering(
        target,
        OpenClipConfig(
            enabled=True,
            model_name="model",
            pretrained="weights",
        ),
        parameters=ClusteringParameters(n_clusters=2, random_seed=4),
    )

    assert result.status == "completed"
    assert result.clustered_file_count == 4
    assert result.actual_cluster_count == 2
    conn = connect_openclip_db(target)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM image_cluster_members WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()[0] == 4
        assert conn.execute(
            "SELECT COUNT(DISTINCT file_id) FROM image_cluster_members "
            "WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()[0] == 4
    finally:
        conn.close()


def test_v1_migration_preserves_embeddings(tmp_path: Path) -> None:
    target = tmp_path / "collection"
    target.mkdir()
    conn = connect_openclip_db(target)
    try:
        conn.execute(
            """
            INSERT INTO image_embeddings(
                file_id, target_path, target_path_key, sha256,
                model_name, pretrained, embedding
            ) VALUES(1, '2024/01/a.jpg', '2024/01/a.jpg',
                     'sha', 'model', 'weights', X'0000803f')
            """
        )
        conn.execute("DROP TABLE image_cluster_members")
        conn.execute("DROP TABLE image_clusters")
        conn.execute("DROP TABLE image_clustering_runs")
        conn.execute(
            "UPDATE meta SET value = '1' WHERE key = 'schema_version'"
        )
        before = bytes(
            conn.execute(
                "SELECT embedding FROM image_embeddings"
            ).fetchone()[0]
        )
        conn.commit()
    finally:
        conn.close()

    migrated = connect_openclip_db(target)
    try:
        assert get_meta(migrated, "schema_version") == "2"
        assert bytes(
            migrated.execute(
                "SELECT embedding FROM image_embeddings"
            ).fetchone()[0]
        ) == before
        assert migrated.execute(
            "SELECT COUNT(*) FROM image_clustering_runs"
        ).fetchone()[0] == 0
    finally:
        migrated.close()


def test_v1_migration_rolls_back_all_new_tables_on_health_failure(
    tmp_path: Path,
) -> None:
    target = tmp_path / "collection"
    target.mkdir()
    conn = connect_openclip_db(target)
    try:
        conn.execute("DROP TABLE image_cluster_members")
        conn.execute("DROP TABLE image_clusters")
        conn.execute("DROP TABLE image_clustering_runs")
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    with patch(
        "bildebank.openclip.db.validate_database_health",
        side_effect=ValueError("health failure"),
    ), pytest.raises(ValueError, match="health failure"):
        connect_openclip_db(target)

    raw = sqlite3.connect(target / ".bilder-openclip.sqlite3")
    try:
        assert raw.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "1"
        tables = {
            str(row[0])
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "image_clustering_runs" not in tables
        assert "image_clusters" not in tables
        assert "image_cluster_members" not in tables
    finally:
        raw.close()


def test_read_only_open_rejects_v1_without_migrating(tmp_path: Path) -> None:
    target = tmp_path / "collection"
    target.mkdir()
    conn = connect_openclip_db(target)
    try:
        conn.execute("DROP TABLE image_cluster_members")
        conn.execute("DROP TABLE image_clusters")
        conn.execute("DROP TABLE image_clustering_runs")
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="skrivbar OpenCLIP-operasjon"):
        connect_openclip_db_read_only(target)

    raw = sqlite3.connect(target / ".bilder-openclip.sqlite3")
    try:
        assert raw.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "1"
        assert raw.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'image_clustering_runs'"
        ).fetchone()[0] == 0
    finally:
        raw.close()


def test_cluster_member_constraints_enforce_run_and_unique_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "collection"
    target.mkdir()
    conn = connect_openclip_db(target)
    try:
        run_ids = []
        for _index in range(2):
            run_ids.append(
                int(
                    conn.execute(
                        """
                        INSERT INTO image_clustering_runs(
                            selection_kind, selection_json, model_name,
                            pretrained, algorithm, parameters_json,
                            random_seed, status
                        ) VALUES('all', '{"kind":"all"}', 'model',
                                 'weights', 'minibatch_kmeans', '{}', 0,
                                 'completed') RETURNING id
                        """
                    ).fetchone()[0]
                )
            )
        first_cluster = int(
            conn.execute(
                "INSERT INTO image_clusters(run_id, algorithm_label, display_order) "
                "VALUES(?, 0, 1) RETURNING id",
                (run_ids[0],),
            ).fetchone()[0]
        )
        second_cluster = int(
            conn.execute(
                "INSERT INTO image_clusters(run_id, algorithm_label, display_order) "
                "VALUES(?, 1, 2) RETURNING id",
                (run_ids[0],),
            ).fetchone()[0]
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO image_cluster_members(run_id, cluster_id, file_id) "
                "VALUES(?, ?, 10)",
                (run_ids[1], first_cluster),
            )
        conn.execute(
            "INSERT INTO image_cluster_members(run_id, cluster_id, file_id) "
            "VALUES(?, ?, 10)",
            (run_ids[0], first_cluster),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO image_cluster_members(run_id, cluster_id, file_id) "
                "VALUES(?, ?, 10)",
                (run_ids[0], second_cluster),
            )
    finally:
        conn.close()


def test_run_service_rolls_back_partial_clusters_and_logs_technical_error(
    tmp_path: Path,
) -> None:
    target = clusterable_target(tmp_path)
    duplicate_result = ClusteringResult(
        clusters=(
            ClusterResult(
                0,
                1,
                embedding_blob([1.0, 0.0]),
                (ClusterMemberResult(1, 0.0, 1),),
            ),
            ClusterResult(
                1,
                2,
                embedding_blob([1.0, 0.0]),
                (ClusterMemberResult(1, 0.0, 1),),
            ),
        ),
        requested_cluster_count=1,
        actual_cluster_count=2,
    )
    progress: list[tuple[str, dict[str, object]]] = []
    with patch(
        "bildebank.image_clustering.cluster_embedding_matrix",
        return_value=duplicate_result,
    ):
        result = run_image_clustering(
            target,
            OpenClipConfig(
                enabled=True,
                model_name="model",
                pretrained="weights",
            ),
            parameters=ClusteringParameters(n_clusters=1),
            progress=lambda stage, values: progress.append((stage, values)),
        )

    assert result.status == "failed"
    assert any(stage == "technical-error" for stage, _values in progress)
    conn = connect_openclip_db(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM image_clusters").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM image_cluster_members"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_run_service_marks_controlled_interrupt_cancelled(tmp_path: Path) -> None:
    target = clusterable_target(tmp_path)
    with patch(
        "bildebank.image_clustering.cluster_embedding_matrix",
        side_effect=KeyboardInterrupt,
    ), pytest.raises(KeyboardInterrupt):
        run_image_clustering(
            target,
            OpenClipConfig(
                enabled=True,
                model_name="model",
                pretrained="weights",
            ),
            parameters=ClusteringParameters(n_clusters=1),
        )

    conn = connect_openclip_db(target)
    try:
        row = conn.execute(
            "SELECT status, finished_at FROM image_clustering_runs"
        ).fetchone()
        assert row["status"] == "cancelled"
        assert row["finished_at"] is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM image_cluster_members"
        ).fetchone()[0] == 0
    finally:
        conn.close()
