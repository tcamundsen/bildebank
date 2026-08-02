from __future__ import annotations

from io import BytesIO
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from bildebank import db
from bildebank.config import AppConfig
from bildebank.image_clustering import delete_clustering_run
from bildebank.openclip import connect_openclip_db, embedding_blob
from bildebank.server_browser_queries import source_item_ids
from bildebank.server_browser_sources import cluster_browser_source
from bildebank.server_endpoints_clustering import (
    grouping_page_html,
    grouping_run,
    grouping_run_page_html,
)
from bildebank.server_handler import BildebankRequestHandler
from tests.db_test_helpers import insert_test_file


def insert_completed_run(
    target: Path,
    file_ids: tuple[int, ...],
) -> tuple[int, int]:
    conn = connect_openclip_db(target)
    try:
        for file_id in file_ids:
            row = db.connect_read_only(target)
            try:
                file_row = row.execute(
                    "SELECT target_path, target_path_key, sha256 "
                    "FROM files WHERE id = ?",
                    (file_id,),
                ).fetchone()
            finally:
                row.close()
            conn.execute(
                """
                INSERT INTO image_embeddings(
                    file_id, target_path, target_path_key, sha256,
                    model_name, pretrained, embedding
                ) VALUES(?, ?, ?, ?, 'model', 'weights', ?)
                """,
                (
                    file_id,
                    str(file_row["target_path"]),
                    str(file_row["target_path_key"]),
                    str(file_row["sha256"]),
                    embedding_blob([1.0, 0.0]),
                ),
            )
        run_id = int(
            conn.execute(
                """
                INSERT INTO image_clustering_runs(
                    selection_kind, selection_json, model_name, pretrained,
                    embedding_dimension, algorithm, parameters_json,
                    random_seed, status, selected_file_count,
                    selected_image_count, embedded_file_count,
                    clustered_file_count, actual_cluster_count,
                    started_at, finished_at
                ) VALUES(
                    'filter', '{"hide_out_of_focus":false,"kind":"filter","query":"year=2024"}',
                    'model', 'weights', 2, 'minibatch_kmeans',
                    '{"n_clusters":1}', 0, 'completed', ?, ?, ?, ?, 1,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                ) RETURNING id
                """,
                (len(file_ids),) * 4,
            ).fetchone()[0]
        )
        cluster_id = int(
            conn.execute(
                """
                INSERT INTO image_clusters(
                    run_id, algorithm_label, display_order
                ) VALUES(?, 0, 1) RETURNING id
                """,
                (run_id,),
            ).fetchone()[0]
        )
        conn.executemany(
            """
            INSERT INTO image_cluster_members(
                run_id, cluster_id, file_id, distance_to_center, center_rank
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (
                (run_id, cluster_id, file_id, index / 10, index)
                for index, file_id in enumerate(file_ids, start=1)
            ),
        )
        conn.commit()
        return run_id, cluster_id
    finally:
        conn.close()


def test_grouping_pages_render_runs_and_active_cluster_members(
    tmp_path: Path,
) -> None:
    target = tmp_path / "collection"
    db.init_database(target)
    first = insert_test_file(target, "2024/01/first.png", sha256="sha-1")
    second = insert_test_file(target, "2024/01/second.png", sha256="sha-2")
    run_id, cluster_id = insert_completed_run(target, (first, second))
    server = SimpleNamespace(
        target=target,
        config=AppConfig(),
        face_enabled=False,
        openclip_enabled=True,
        read_only=False,
        csrf_token="token",
    )

    list_html = grouping_page_html(server)
    run_html = grouping_run_page_html(server, run_id)

    assert f"Kjøring #{run_id}" in list_html
    assert run_html is not None
    assert "Gruppe 1" in run_html
    assert "2 aktive bilder" in run_html
    assert f"/grouping/runs/{run_id}/clusters/{cluster_id}" in run_html
    assert f"/grouping/runs/{run_id}/delete" in run_html
    assert "Utvalg: year=2024" in run_html
    assert "Opprettet" in run_html
    assert "Valgte stillbilder" in run_html
    assert "grouping-preview-missing" in run_html

    source = cluster_browser_source(run_id, cluster_id, 1)
    assert source_item_ids(target, source) == [first, second]

    conn = db.connect(target)
    try:
        conn.execute(
            "UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
            (first,),
        )
        conn.commit()
    finally:
        conn.close()
    assert source_item_ids(target, source) == [second]


def test_delete_run_cascades_only_grouping_data(tmp_path: Path) -> None:
    target = tmp_path / "collection"
    db.init_database(target)
    file_id = insert_test_file(
        target,
        "2024/01/first.png",
        sha256="sha-1",
    )
    run_id, _cluster_id = insert_completed_run(target, (file_id,))

    assert delete_clustering_run(target, run_id) is True
    conn = connect_openclip_db(target)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM image_clustering_runs"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM image_clusters"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM image_cluster_members"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM image_embeddings"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    main_conn = db.connect_read_only(target)
    try:
        assert main_conn.execute(
            "SELECT COUNT(*) FROM files"
        ).fetchone()[0] == 1
    finally:
        main_conn.close()


def test_grouping_routes_block_lan_and_require_csrf_for_delete(
    tmp_path: Path,
) -> None:
    target = tmp_path / "collection"
    db.init_database(target)
    file_id = insert_test_file(target, "2024/01/first.png", sha256="sha-1")
    run_id, _cluster_id = insert_completed_run(target, (file_id,))

    lan_handler = object.__new__(BildebankRequestHandler)
    lan_handler.path = "/grouping"
    lan_handler.server = SimpleNamespace(slideshow=None, lan_share=True)
    lan_responses: list[tuple[str, HTTPStatus | None]] = []
    lan_handler.respond_text = (  # type: ignore[method-assign]
        lambda content, *, status=None: lan_responses.append((content, status))
    )
    BildebankRequestHandler.do_GET(lan_handler)
    assert lan_responses == [
        (
            "Grupperingssider er ikke tilgjengelige i LAN-deling.",
            HTTPStatus.FORBIDDEN,
        )
    ]

    no_csrf = object.__new__(BildebankRequestHandler)
    no_csrf.path = f"/grouping/runs/{run_id}/delete"
    no_csrf.server = SimpleNamespace(
        target=target,
        config=AppConfig(),
        slideshow=None,
        lan_share=False,
        read_only=False,
        openclip_enabled=True,
        csrf_token="token",
    )
    no_csrf.headers = {"Content-Length": "0"}  # type: ignore[assignment]
    no_csrf.rfile = BytesIO()
    csrf_responses: list[tuple[object, HTTPStatus | None]] = []
    no_csrf.respond_json = (  # type: ignore[method-assign]
        lambda content, *, status=None: csrf_responses.append((content, status))
    )
    BildebankRequestHandler.do_POST(no_csrf)
    assert csrf_responses == [
        (
            {"ok": False, "error": "Ugyldig eller manglende CSRF-token."},
            HTTPStatus.FORBIDDEN,
        )
    ]
    assert grouping_run(target, run_id) is not None

    form = b"csrf_token=token"
    confirmed = object.__new__(BildebankRequestHandler)
    confirmed.path = f"/grouping/runs/{run_id}/delete"
    confirmed.server = no_csrf.server
    confirmed.headers = {  # type: ignore[assignment]
        "Content-Length": str(len(form)),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    confirmed.rfile = BytesIO(form)
    redirects: list[str] = []
    confirmed.redirect = redirects.append  # type: ignore[method-assign]
    BildebankRequestHandler.do_POST(confirmed)
    assert redirects == ["/grouping"]
    assert grouping_run(target, run_id) is None
