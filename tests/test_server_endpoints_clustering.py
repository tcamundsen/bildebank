from __future__ import annotations

from io import BytesIO
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank import db
from bildebank import server_browser_overview_html
from bildebank import server_browser_queries
from bildebank import server_endpoints_clustering as clustering_endpoints
from bildebank.config import AppConfig
from bildebank.image_clustering import delete_clustering_run
from bildebank.openclip import (
    connect_openclip_db,
    connect_openclip_db_read_only,
    embedding_blob,
)
from bildebank.server_browser_queries import (
    source_item_ids,
    source_month_keys,
    source_year_cards,
    source_year_month_cards,
)
from bildebank.server_browser_sources import cluster_browser_source
from bildebank.server_endpoints_clustering import (
    grouping_page_html,
    grouping_run,
    grouping_run_page_html,
)
from bildebank.server_handler import BildebankRequestHandler
from bildebank.server_pages import source_year_months_page_html
from bildebank.server_endpoints_browser import respond_browser_source
from tests.db_test_helpers import insert_basic_item_sidecar_fixture, insert_test_file


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
    assert f'action="/grouping/runs/{run_id}/delete"' in list_html
    assert '>Slett</button>' in list_html
    assert "Slette run #" in list_html
    assert 'value="token"' in list_html
    assert "MiniBatchKMeans" in list_html
    assert "Parametere: Ønskede grupper: 1 · Seed: 0" in list_html
    assert "Utvalg: year=2024" in list_html
    assert run_html is not None
    assert "Gruppe 1" in run_html
    assert "2 aktive bilder" in run_html
    assert f"/grouping/runs/{run_id}/clusters/{cluster_id}" in run_html
    assert f"/grouping/runs/{run_id}/delete" in run_html
    assert "Utvalg: year=2024" in run_html
    assert "Opprettet" in run_html
    assert "Valgte stillbilder" in run_html
    assert "grouping-preview-missing" in run_html

    read_only_html = grouping_page_html(
        SimpleNamespace(
            target=target,
            face_enabled=False,
            openclip_enabled=True,
            read_only=True,
        )
    )
    assert f'action="/grouping/runs/{run_id}/delete"' not in read_only_html

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


def test_grouping_run_batches_card_metadata_without_per_cluster_connections(
    tmp_path: Path,
) -> None:
    target = tmp_path / "collection"
    db.init_database(target)
    first = insert_test_file(target, "2024/01/first.png", sha256="sha-1")
    second = insert_test_file(target, "2024/01/second.png", sha256="sha-2")
    run_id, _cluster_id = insert_completed_run(target, (first, second))
    main_conn = db.connect(target)
    try:
        db.tag_file(main_conn, file_id=first, tag_name="Ferie")
        db.tag_file(main_conn, file_id=second, tag_name="Ferie")
        db.tag_file(main_conn, file_id=first, tag_name="Familie")
        main_conn.commit()
    finally:
        main_conn.close()
    face_config = AppConfig().face_recognition
    insert_basic_item_sidecar_fixture(
        target,
        file_id=first,
        target_path="2024/01/first.png",
        sha256="sha-1",
        face_configs=(face_config,),
    )
    server = SimpleNamespace(
        target=target,
        config=AppConfig(),
        face_enabled=True,
        openclip_enabled=True,
        read_only=True,
    )
    original_main_connect = db.connect_read_only
    original_openclip_connect = (
        clustering_endpoints.connect_openclip_db_read_only
    )

    with patch.object(
        clustering_endpoints.db,
        "connect_read_only",
        wraps=original_main_connect,
    ) as main_connect, patch.object(
        clustering_endpoints,
        "connect_openclip_db_read_only",
        wraps=original_openclip_connect,
    ) as openclip_connect:
        body = grouping_run_page_html(server, run_id)

    assert body is not None
    assert "Ferie (2)" in body
    assert "Familie (1)" in body
    assert "Kari (1)" in body
    assert main_connect.call_count == 1
    assert openclip_connect.call_count == 2


def test_cluster_year_page_fetches_all_month_cards_once(tmp_path: Path) -> None:
    target = tmp_path / "collection"
    db.init_database(target)
    january = insert_test_file(target, "2024/01/january.png", sha256="sha-1")
    february = insert_test_file(target, "2024/02/february.png", sha256="sha-2")
    second_february = insert_test_file(
        target,
        "2024/02/second.png",
        sha256="sha-3",
    )
    conn = db.connect(target)
    try:
        conn.execute(
            "UPDATE files SET taken_date = '2024-02-03' WHERE id IN (?, ?)",
            (february, second_february),
        )
        conn.commit()
    finally:
        conn.close()
    run_id, cluster_id = insert_completed_run(
        target,
        (january, february, second_february),
    )
    source = cluster_browser_source(run_id, cluster_id, 1)
    original_attach = server_browser_queries.attach_source_sql_filter_databases

    with patch.object(
        server_browser_queries,
        "attach_source_sql_filter_databases",
        wraps=original_attach,
    ) as attach:
        cards = source_year_month_cards(target, source, "2024")

    assert [(card["month_key"], card["item_count"]) for card in cards] == [
        ("2024-01", 1),
        ("2024-02", 2),
    ]
    assert attach.call_count == 1

    with patch.object(
        server_browser_overview_html,
        "source_year_month_cards",
        wraps=source_year_month_cards,
    ) as month_cards:
        body = source_year_months_page_html(
            target,
            source,
            "2024",
            month_keys=["2024-01", "2024-02"],
        )

    assert "2024-01" in body
    assert "2024-02" in body
    assert month_cards.call_count == 1


def test_cluster_year_page_does_not_scan_collection_sidecars(
    tmp_path: Path,
) -> None:
    target = tmp_path / "collection"
    db.init_database(target)
    file_id = insert_test_file(
        target,
        "2024/01/image.png",
        sha256="sha-1",
    )
    run_id, cluster_id = insert_completed_run(target, (file_id,))
    source = cluster_browser_source(run_id, cluster_id, 1)

    with (
        patch.object(
            server_browser_queries,
            "motion_video_file_ids",
            side_effect=AssertionError("scanned motion videos"),
        ),
        patch.object(
            server_browser_queries,
            "raw_sidecar_file_ids",
            side_effect=AssertionError("scanned RAW sidecars"),
        ),
    ):
        cards = source_year_cards(target, source)

    assert [(card["year"], card["item_count"]) for card in cards] == [
        ("2024", 1),
    ]


def test_cluster_year_and_month_requests_validate_openclip_once_each(
    tmp_path: Path,
) -> None:
    target = tmp_path / "collection"
    db.init_database(target)
    file_id = insert_test_file(target, "2024/01/image.png", sha256="sha-1")
    run_id, cluster_id = insert_completed_run(target, (file_id,))
    source = cluster_browser_source(run_id, cluster_id, 1)

    class Server(SimpleNamespace):
        def source_month_keys(
            self,
            browser_source: object,
            *,
            hide_out_of_focus: bool = False,
            conn: object = None,
        ) -> list[str]:
            return source_month_keys(
                self.target,
                browser_source,  # type: ignore[arg-type]
                hide_out_of_focus=hide_out_of_focus,
                conn=conn,  # type: ignore[arg-type]
            )

    server = Server(
        target=target,
        config=AppConfig(),
        face_enabled=False,
        openclip_enabled=True,
    )
    handler = SimpleNamespace(
        server=server,
        respond_html=lambda _body: None,
        respond_text=lambda _body, **_kwargs: None,
    )

    with patch(
        "bildebank.openclip.connect_openclip_db_read_only",
        wraps=connect_openclip_db_read_only,
    ) as validate_openclip:
        respond_browser_source(
            handler,
            source,
            "year",
            "2024",
            item_not_found_message="Fant ikke bildet.",
            invalid_page_message="Ugyldig side.",
        )
        assert validate_openclip.call_count == 1
        assert validate_openclip.call_args.kwargs == {"full": False}
        validate_openclip.reset_mock()
        respond_browser_source(
            handler,
            source,
            "month",
            "2024-01",
            item_not_found_message="Fant ikke bildet.",
            invalid_page_message="Ugyldig side.",
        )

    assert validate_openclip.call_count == 1
    assert validate_openclip.call_args.kwargs == {"full": False}


def test_cluster_identity_is_cached_until_openclip_database_changes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "collection"
    db.init_database(target)
    file_id = insert_test_file(target, "2024/01/image.png", sha256="sha-1")
    run_id, cluster_id = insert_completed_run(target, (file_id,))
    clustering_endpoints._cached_grouping_cluster_display_order_and_kind.cache_clear()
    original_connect = clustering_endpoints.connect_openclip_db_read_only

    with (
        patch.object(
            clustering_endpoints,
            "_openclip_database_fingerprint",
            side_effect=((1, 100), (1, 100), (2, 100)),
        ),
        patch.object(
            clustering_endpoints,
            "connect_openclip_db_read_only",
            wraps=original_connect,
        ) as openclip_connect,
    ):
        first = clustering_endpoints.grouping_cluster_display_order_and_kind(
            target,
            run_id,
            cluster_id,
        )
        second = clustering_endpoints.grouping_cluster_display_order_and_kind(
            target,
            run_id,
            cluster_id,
        )
        after_change = (
            clustering_endpoints.grouping_cluster_display_order_and_kind(
                target,
                run_id,
                cluster_id,
            )
        )

    assert first == (1, "cluster")
    assert second == first
    assert after_change == first
    assert openclip_connect.call_count == 2
    assert all(
        call.kwargs == {"full": False}
        for call in openclip_connect.call_args_list
    )
    clustering_endpoints._cached_grouping_cluster_display_order_and_kind.cache_clear()


def test_hdbscan_run_renders_noise_as_ungrouped_images(tmp_path: Path) -> None:
    target = tmp_path / "collection"
    db.init_database(target)
    file_id = insert_test_file(target, "2024/01/noise.png", sha256="sha-1")
    run_id, cluster_id = insert_completed_run(target, (file_id,))
    conn = connect_openclip_db(target)
    try:
        conn.execute(
            """
            UPDATE image_clustering_runs
            SET algorithm = 'hdbscan',
                parameters_json = '{"min_cluster_size":5,"min_samples":2}',
                actual_cluster_count = 0
            WHERE id = ?
            """,
            (run_id,),
        )
        conn.execute(
            """
            UPDATE image_clusters
            SET algorithm_label = -1, kind = 'noise'
            WHERE id = ?
            """,
            (cluster_id,),
        )
        conn.commit()
    finally:
        conn.close()
    server = SimpleNamespace(
        target=target,
        config=AppConfig(),
        face_enabled=False,
        openclip_enabled=True,
        read_only=True,
    )

    list_html = grouping_page_html(server)
    run_html = grouping_run_page_html(server, run_id)

    assert run_html is not None
    assert "HDBSCAN" in list_html
    assert (
        "Parametere: Minste gruppestørrelse: 5 · Min samples: 2"
        in list_html
    )
    assert "HDBSCAN" in run_html
    assert "Ugrupperte bilder" in run_html
    assert "<dt>Seed</dt>" not in run_html
    assert cluster_browser_source(
        run_id,
        cluster_id,
        1,
        kind="noise",
    ).title == "Ugrupperte bilder"


def test_leiden_run_renders_parameters_graph_stats_and_noise(
    tmp_path: Path,
) -> None:
    target = tmp_path / "collection"
    db.init_database(target)
    file_id = insert_test_file(target, "2024/01/noise.png", sha256="sha-1")
    run_id, cluster_id = insert_completed_run(target, (file_id,))
    conn = connect_openclip_db(target)
    try:
        conn.execute(
            """
            UPDATE image_clustering_runs
            SET algorithm = 'leiden',
                parameters_json = '{"requested_k":20,"neighbor_mode":"union","resolution":0.2}',
                random_seed = 42,
                actual_cluster_count = 0,
                effective_neighbor_count = 1,
                graph_node_count = 1,
                graph_edge_count = 0,
                isolated_file_count = 1,
                threshold_removed_edge_count = 0,
                nearest_similarity_median = 0.5,
                kth_similarity_median = 0.5
            WHERE id = ?
            """,
            (run_id,),
        )
        conn.execute(
            """
            UPDATE image_clusters
            SET algorithm_label = -1, kind = 'noise'
            WHERE id = ?
            """,
            (cluster_id,),
        )
        conn.commit()
    finally:
        conn.close()
    server = SimpleNamespace(
        target=target,
        config=AppConfig(),
        face_enabled=False,
        openclip_enabled=True,
        read_only=True,
    )

    list_html = grouping_page_html(server)
    run_html = grouping_run_page_html(server, run_id)

    assert run_html is not None
    assert "Leiden" in list_html
    assert "Naboer: 20 · Åpen graf · CPM-oppløsning: 0.2" in list_html
    assert "<dt>Seed</dt><dd>42</dd>" in run_html
    assert "<dt>Grafnoder</dt><dd>1</dd>" in run_html
    assert "<dt>Grafkanter</dt><dd>0</dd>" in run_html
    assert "Ugrupperte bilder" in run_html


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
