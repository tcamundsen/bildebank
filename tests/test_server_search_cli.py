from __future__ import annotations

import sqlite3
import tempfile
import unittest
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank import db, server_endpoints_browser
from bildebank.config import AppConfig, BrowserConfig, OpenClipConfig
from bildebank.db import init_database
from bildebank.openclip import (
    ImageSearchResult,
    connect_openclip_db,
    create_search_run,
    embedding_blob,
    openclip_db_path,
)
from bildebank.server_browser_queries import item_by_id, source_item_ids
from bildebank.server_browser_sources import (
    search_results_browser_source,
    tag_browser_source,
)
from bildebank.server_handler import BildebankRequestHandler
from bildebank.server_pages import (
    item_page_html,
    search_html,
    search_start_html,
    similar_search_html,
    source_item_page_html,
)
from bildebank.server_search import (
    DEFAULT_SEARCH_LIMIT,
    OpenClipSearchCache,
    ServerSearchStats,
    ServerSimilarSearchStats,
    load_stored_search_run,
    load_search_embedding_cache,
    search_server_images,
    search_server_similar_images,
)
from bildebank.target_lock import LOCK_FILENAME, TargetLockError
from tests.cli_helpers import run_cli
from tests.db_test_helpers import insert_test_file, register_target_file
from tests.test_media import minimal_png


class ServerSearchCliTests(unittest.TestCase):
    def insert_embeddings(
        self,
        target: Path,
        config: OpenClipConfig,
        rows: list[tuple[int, str, str, list[float]]],
    ) -> None:
        conn = connect_openclip_db(target)
        try:
            conn.executemany(
                """
                INSERT INTO image_embeddings(
                    file_id, target_path, target_path_key, sha256,
                    model_name, pretrained, embedding
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        file_id,
                        target_path,
                        target_path.casefold(),
                        sha256,
                        config.model_name,
                        config.pretrained,
                        embedding_blob(vector),
                    )
                    for file_id, target_path, sha256, vector in rows
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def insert_search_run(
        self,
        target: Path,
        query: str,
        results: list[tuple[int, str, float]],
    ) -> int:
        config = OpenClipConfig()
        conn = connect_openclip_db(target)
        try:
            run_id = create_search_run(conn, query, config, len(results))
            conn.executemany(
                """
                INSERT INTO image_search_results(
                    run_id, file_id, target_path, target_path_key,
                    similarity, rank
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        file_id,
                        target_path,
                        target_path.casefold(),
                        similarity,
                        rank,
                    )
                    for rank, (file_id, target_path, similarity) in enumerate(
                        results,
                        start=1,
                    )
                ],
            )
            conn.commit()
            return run_id
        finally:
            conn.close()

    def test_run_server_search_page_warns_when_model_is_not_loaded(self) -> None:
        server = SimpleNamespace(
            config=AppConfig(openclip=OpenClipConfig(enabled=True)),
            face_enabled=True,
            openclip_enabled=True,
            search_cache=SimpleNamespace(loaded=False),
        )

        body = search_start_html(server)

        self.assertIn("Dette kan ta 10-20 sekunder", body)
        self.assertIn("Laster bildesøkmodellen", body)
        self.assertIn('data-model-loaded="false"', body)
        self.assertIn("data-search-loading", body)
        self.assertIn('<form action="/search" method="post"', body)

    def test_run_server_search_results_marks_model_loaded(self) -> None:
        server = SimpleNamespace(
            target=Path("/tmp/target"),
            config=AppConfig(openclip=OpenClipConfig(enabled=True)),
            face_enabled=True,
            openclip_enabled=True,
            search_cache=SimpleNamespace(loaded=True),
        )

        body = search_html(server, ServerSearchStats("strand", ()), DEFAULT_SEARCH_LIMIT)

        self.assertIn("Dette kan ta 10-20 sekunder", body)
        self.assertIn('data-model-loaded="true"', body)

    def test_run_server_search_preload_endpoint_starts_background_load(self) -> None:
        class FakeSearchCache:
            loaded = False
            started = False

            def preload_model_async(self) -> str:
                self.started = True
                return "loading"

        class FakeHandler:
            server = SimpleNamespace(openclip_enabled=True, search_cache=FakeSearchCache())
            body: dict[str, object] | None = None
            status = HTTPStatus.OK

            def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                self.body = content
                self.status = status

            def respond_search_preload(self) -> None:
                BildebankRequestHandler.respond_search_preload(self)  # type: ignore[arg-type]

        handler = FakeHandler()
        handler.path = "/api/search-preload"
        handler.headers = {"X-CSRF-Token": "test-token"}
        handler.rfile = BytesIO()
        handler.server.csrf_token = "test-token"
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertTrue(handler.server.search_cache.started)
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.body, {"ok": True, "status": "loading", "loaded": False})

    def test_run_server_search_preload_get_does_not_start_model_load(self) -> None:
        class FakeSearchCache:
            loaded = False
            started = False

            def preload_model_async(self) -> str:
                self.started = True
                return "loading"

        class FakeHandler:
            path = "/api/search-preload"
            server = SimpleNamespace(
                read_only=False,
                openclip_enabled=True,
                search_cache=FakeSearchCache(),
            )
            body: dict[str, object] | None = None
            status = HTTPStatus.OK

            def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                self.body = content
                self.status = status

        handler = FakeHandler()
        BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]

        self.assertFalse(handler.server.search_cache.started)
        self.assertEqual(handler.status, HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(
            handler.body,
            {"ok": False, "error": "Endepunktet krever POST."},
        )

    def test_run_server_image_search_stores_relative_result_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            config = OpenClipConfig()
            conn = connect_openclip_db(target)
            try:
                conn.execute(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        "2024/01/IMG_20240102.jpg",
                        "2024/01/img_20240102.jpg",
                        "sha",
                        config.model_name,
                        config.pretrained,
                        embedding_blob([1.0, 0.0]),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=config),
                search_cache=OpenClipSearchCache(AppConfig(openclip=config)),
            )

            with (
                patch("bildebank.server_search.load_text_model", return_value=(object(), object())),
                patch("bildebank.server_search.text_embedding", return_value=[1.0, 0.0]),
            ):
                stats = search_server_images(server, query="test", limit=10)

            self.assertEqual(len(stats.results), 1)
            self.assertIsNotNone(stats.run_id)
            conn = sqlite3.connect(openclip_db_path(target))
            try:
                self.assertEqual(
                    conn.execute("SELECT target_path FROM image_search_results").fetchone()[0],
                    "2024/01/IMG_20240102.jpg",
                )
            finally:
                conn.close()

    def test_run_server_image_search_refuses_to_run_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            config = OpenClipConfig()
            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=config),
                search_cache=OpenClipSearchCache(AppConfig(openclip=config)),
            )
            (target / LOCK_FILENAME).write_text("command=image-scan\n", encoding="utf-8")

            with self.assertRaises(TargetLockError):
                search_server_images(server, query="test", limit=10)

    def test_run_server_search_get_with_query_only_renders_post_form(self) -> None:
        class FakeSearchCache:
            loaded = False

            def preload_model_async(self) -> str:
                raise AssertionError("GET skal ikke laste modellen")

        class FakeHandler:
            path = "/search?q=test&limit=7"
            server = SimpleNamespace(
                read_only=False,
                config=AppConfig(openclip=OpenClipConfig(enabled=True)),
                face_enabled=True,
                openclip_enabled=True,
                search_cache=FakeSearchCache(),
            )
            body = ""
            status: HTTPStatus | None = None

            def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                self.body = content
                self.status = status

        handler = FakeHandler()
        with patch(
            "bildebank.server_handler.search_server_images",
            side_effect=AssertionError("GET skal ikke kjøre søk"),
        ):
            BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertIn('method="post"', handler.body)
        self.assertIn('name="q" value="test"', handler.body)
        self.assertIn('name="limit" value="7"', handler.body)
        self.assertIn("Trykk Søk for å kjøre dette søket.", handler.body)

    def test_stored_search_run_has_a_bookmarkable_get_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            result_id = insert_test_file(target, "2024/01/result.png")
            run_id = self.insert_search_run(
                target,
                "a red cabin",
                [(result_id, "2024/01/result.png", 0.91)],
            )
            config = AppConfig(openclip=OpenClipConfig(enabled=True))

            class FakeHandler:
                path = f"/search/runs/{run_id}"
                server = SimpleNamespace(
                    target=target,
                    config=config,
                    face_enabled=False,
                    openclip_enabled=True,
                    hide_out_of_focus=False,
                    search_cache=SimpleNamespace(loaded=False),
                    read_only=True,
                    slideshow=None,
                    lan_share=False,
                    source_item_order=lambda source, *, hide_out_of_focus=False: (
                        (ids := source_item_ids(target, source)),
                        {file_id: index for index, file_id in enumerate(ids)},
                    ),
                )
                response: tuple[str, HTTPStatus] | None = None

                def read_only_get_blocked(self, path: str) -> bool:
                    return BildebankRequestHandler.read_only_get_blocked(
                        self,
                        path,
                    )

                def respond_html(
                    self,
                    content: str,
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.response = (content, status)

                def respond_text(
                    self,
                    content: str,
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.response = (content, status)

            handler = FakeHandler()
            with patch(
                "bildebank.server_search.load_text_model",
                side_effect=AssertionError("GET skal ikke laste søkemodellen"),
            ):
                BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]

        self.assertEqual(
            handler.response[1],
            HTTPStatus.OK,
            msg=handler.response[0],
        )
        self.assertIn("a red cabin", handler.response[0])
        self.assertIn(
            f'href="/search/runs/{run_id}/item/{result_id}"',
            handler.response[0],
        )

    def test_run_server_search_post_reports_target_lock_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            (target / LOCK_FILENAME).write_text("command=image-scan\n", encoding="utf-8")
            encoded = b"q=test&limit=100&csrf_token=test-token"

            class FakeHandler:
                path = "/search"
                headers = {
                    "Content-Length": str(len(encoded)),
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                rfile = BytesIO(encoded)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(openclip=OpenClipConfig(enabled=True)),
                    face_enabled=True,
                    openclip_enabled=True,
                    read_only=False,
                    csrf_token="test-token",
                    search_cache=OpenClipSearchCache(AppConfig(openclip=OpenClipConfig(enabled=True))),
                )
                body = ""
                status: HTTPStatus | None = None

                def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", handler.body)

    def test_search_post_redirects_to_stored_result_page(self) -> None:
        encoded = b"q=test&limit=10&csrf_token=test-token"

        class FakeHandler:
            path = "/search"
            headers = {
                "Content-Length": str(len(encoded)),
                "Content-Type": "application/x-www-form-urlencoded",
            }
            rfile = BytesIO(encoded)
            server = SimpleNamespace(
                read_only=False,
                slideshow=None,
                csrf_token="test-token",
                openclip_enabled=True,
                face_enabled=False,
            )
            redirect_url: str | None = None

            def redirect(self, url: str) -> None:
                self.redirect_url = url

            def respond_html(
                self,
                content: str,
                *,
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                raise AssertionError(f"Forventet redirect, fikk HTML ({status}): {content}")

        handler = FakeHandler()
        with patch(
            "bildebank.server_handler.search_server_images",
            return_value=ServerSearchStats("test", (), run_id=23),
        ):
            BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.redirect_url, "/search/runs/23")

    def test_run_server_image_search_reuses_embedding_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            config = OpenClipConfig()
            conn = connect_openclip_db(target)
            try:
                conn.executemany(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (1, "2024/01/a.jpg", "2024/01/a.jpg", "sha1", config.model_name, config.pretrained, embedding_blob([1.0, 0.0])),
                        (2, "2024/01/b.jpg", "2024/01/b.jpg", "sha2", config.model_name, config.pretrained, embedding_blob([0.0, 1.0])),
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=config),
                search_cache=OpenClipSearchCache(AppConfig(openclip=config)),
            )

            with (
                patch("bildebank.server_search.load_text_model", return_value=(object(), object())),
                patch("bildebank.server_search.text_embedding", return_value=[1.0, 0.0]),
                patch("bildebank.server_search.load_search_embedding_cache", wraps=load_search_embedding_cache) as load_cache,
            ):
                first = search_server_images(server, query="test", limit=1)
                second = search_server_images(server, query="test igjen", limit=1)

        self.assertEqual(load_cache.call_count, 1)
        self.assertEqual(first.results[0].file_id, 1)
        self.assertEqual(second.results[0].file_id, 1)

    def test_run_server_image_search_reloads_embedding_cache_when_database_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            config = OpenClipConfig()
            conn = connect_openclip_db(target)
            try:
                conn.execute(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (1, "2024/01/a.jpg", "2024/01/a.jpg", "sha1", config.model_name, config.pretrained, embedding_blob([1.0, 0.0])),
                )
                conn.commit()
            finally:
                conn.close()
            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=config),
                search_cache=OpenClipSearchCache(AppConfig(openclip=config)),
            )

            with (
                patch("bildebank.server_search.load_text_model", return_value=(object(), object())),
                patch("bildebank.server_search.text_embedding", return_value=[0.0, 1.0]),
                patch("bildebank.server_search.load_search_embedding_cache", wraps=load_search_embedding_cache) as load_cache,
            ):
                first = search_server_images(server, query="test", limit=10)
                conn = connect_openclip_db(target)
                try:
                    conn.execute(
                        """
                        INSERT INTO image_embeddings(
                            file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            2,
                            "2024/01/b.jpg",
                            "2024/01/b.jpg",
                            "sha2",
                            config.model_name,
                            config.pretrained,
                            embedding_blob([0.0, 1.0]),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
                second = search_server_images(server, query="test igjen", limit=10)

        self.assertEqual(load_cache.call_count, 2)
        self.assertEqual([result.file_id for result in first.results], [1])
        self.assertEqual([result.file_id for result in second.results], [2, 1])

    def test_run_server_image_search_numpy_ranking_matches_cosine_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            config = OpenClipConfig()
            conn = connect_openclip_db(target)
            try:
                conn.executemany(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (1, "2024/01/a.jpg", "2024/01/a.jpg", "sha1", config.model_name, config.pretrained, embedding_blob([1.0, 0.0])),
                        (2, "2024/01/b.jpg", "2024/01/b.jpg", "sha2", config.model_name, config.pretrained, embedding_blob([0.8, 0.6])),
                        (3, "2024/01/c.jpg", "2024/01/c.jpg", "sha3", config.model_name, config.pretrained, embedding_blob([0.0, 1.0])),
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=config),
                search_cache=OpenClipSearchCache(AppConfig(openclip=config)),
            )

            with (
                patch("bildebank.server_search.load_text_model", return_value=(object(), object())),
                patch("bildebank.server_search.text_embedding", return_value=[1.0, 0.0]),
            ):
                stats = search_server_images(server, query="test", limit=2)

        self.assertEqual([result.file_id for result in stats.results], [1, 2])
        self.assertGreater(stats.results[0].similarity, stats.results[1].similarity)

    def test_run_server_image_search_filters_out_of_focus_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            first_path = target / "2024" / "01" / "a.jpg"
            second_path = target / "2024" / "01" / "b.jpg"
            first_path.parent.mkdir(parents=True, exist_ok=True)
            first_path.write_bytes(b"image-a")
            second_path.write_bytes(b"image-b")
            register_target_file(target, Path("2024/01/a.jpg"))
            register_target_file(target, Path("2024/01/b.jpg"))
            conn = db.connect(target)
            try:
                db.tag_file(conn, file_id=1, tag_name=db.SYSTEM_TAG_OUT_OF_FOCUS)
                conn.execute(
                    """
                    UPDATE tags
                    SET name = 'Uskarpt', name_key = 'uskarpt'
                    WHERE system_key = ?
                    """,
                    (db.SYSTEM_TAG_OUT_OF_FOCUS_KEY,),
                )
                conn.commit()
            finally:
                conn.close()

            config = OpenClipConfig()
            conn = connect_openclip_db(target)
            try:
                conn.executemany(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (1, "2024/01/a.jpg", "2024/01/a.jpg", "sha1", config.model_name, config.pretrained, embedding_blob([1.0, 0.0])),
                        (2, "2024/01/b.jpg", "2024/01/b.jpg", "sha2", config.model_name, config.pretrained, embedding_blob([0.9, 0.1])),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            app_config = AppConfig(openclip=config, browser=BrowserConfig(hide_out_of_focus=True))
            server = SimpleNamespace(
                target=target,
                config=app_config,
                search_cache=OpenClipSearchCache(app_config),
            )

            with (
                patch("bildebank.server_search.load_text_model", return_value=(object(), object())),
                patch("bildebank.server_search.text_embedding", return_value=[1.0, 0.0]),
            ):
                stats = search_server_images(server, query="test", limit=2)

        self.assertEqual([result.file_id for result in stats.results], [2])

    def test_run_server_image_search_ignores_orphan_openclip_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            active_id = insert_test_file(target, "2024/01/active.png", sha256="sha-active")
            missing_id = active_id + 100
            config = OpenClipConfig()
            conn = connect_openclip_db(target)
            try:
                conn.executemany(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256,
                        model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            active_id,
                            "2024/01/active.png",
                            "2024/01/active.png",
                            "sha-active",
                            config.model_name,
                            config.pretrained,
                            embedding_blob([1.0, 0.0]),
                        ),
                        (
                            missing_id,
                            "2026/01/unimported.png",
                            "2026/01/unimported.png",
                            "sha-missing",
                            config.model_name,
                            config.pretrained,
                            embedding_blob([0.0, 1.0]),
                        ),
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=config),
                search_cache=OpenClipSearchCache(AppConfig(openclip=config)),
            )

            with (
                patch("bildebank.server_search.load_text_model", return_value=(object(), object())),
                patch("bildebank.server_search.text_embedding", return_value=[0.0, 1.0]),
            ):
                stats = search_server_images(server, query="cat", limit=10)

        self.assertEqual([result.file_id for result in stats.results], [active_id])
        self.assertEqual(stats.results[0].target_path, Path("2024/01/active.png"))

    def test_run_server_image_search_uses_file_id_for_image_url(self) -> None:
        target = Path("/tmp/target")
        server = SimpleNamespace(
            target=target,
            config=AppConfig(openclip=OpenClipConfig(enabled=True)),
            face_enabled=False,
            openclip_enabled=True,
            search_cache=SimpleNamespace(loaded=True),
        )
        result = ImageSearchResult(
            rank=1,
            file_id=999,
            target_path=Path("2025/07/PXL 20250709_193516074.jpg"),
            similarity=0.301,
        )

        body = search_html(server, ServerSearchStats("red wine", (result,)), DEFAULT_SEARCH_LIMIT)

        self.assertIn('src="/file/999"', body)
        self.assertIn('href="/item/999"', body)

    def test_stored_search_is_a_ranked_browser_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            first_id = insert_test_file(target, "2024/01/first.png")
            second_id = insert_test_file(target, "2024/01/second.png")
            deleted_id = insert_test_file(
                target,
                "deleted/2024/01/deleted.png",
                deleted=True,
            )
            run_id = self.insert_search_run(
                target,
                "a snowy mountain",
                [
                    (second_id, "2024/01/second.png", 0.9),
                    (first_id, "2024/01/first.png", 0.8),
                    (deleted_id, "deleted/2024/01/deleted.png", 0.7),
                ],
            )

            run = load_stored_search_run(target, run_id)
            source = search_results_browser_source(
                run_id,
                "Bildesøk: a snowy mountain",
            )
            ranked_ids = source_item_ids(target, source)

        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run.query, "a snowy mountain")
        self.assertEqual(
            [result.file_id for result in run.results],
            [second_id, first_id, deleted_id],
        )
        self.assertEqual(ranked_ids, [second_id, first_id])

    def test_search_result_page_links_to_ranked_source_and_random_item(self) -> None:
        target = Path("/tmp/target")
        server = SimpleNamespace(
            target=target,
            config=AppConfig(openclip=OpenClipConfig(enabled=True)),
            face_enabled=False,
            openclip_enabled=True,
            search_cache=SimpleNamespace(loaded=True),
        )
        result = ImageSearchResult(1, 42, Path("2025/07/result.png"), 0.9)

        body = search_html(
            server,
            ServerSearchStats("mountain", (result,), run_id=17),
            DEFAULT_SEARCH_LIMIT,
        )

        self.assertIn('href="/search/runs/17/item/42"', body)
        self.assertIn('href="/search/runs/17/random"', body)
        self.assertIn("Tilfeldig i utvalget", body)

    def test_stored_search_item_uses_rank_for_previous_and_next(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            first_id = insert_test_file(target, "2024/01/first.png")
            second_id = insert_test_file(target, "2024/01/second.png")
            third_id = insert_test_file(target, "2024/01/third.png")
            run_id = self.insert_search_run(
                target,
                "ranked",
                [
                    (second_id, "2024/01/second.png", 0.9),
                    (first_id, "2024/01/first.png", 0.8),
                    (third_id, "2024/01/third.png", 0.7),
                ],
            )
            config = AppConfig(openclip=OpenClipConfig(enabled=True))

            def source_order(source: object, *, hide_out_of_focus: bool = False):
                ids = source_item_ids(target, source)  # type: ignore[arg-type]
                return ids, {
                    file_id: index for index, file_id in enumerate(ids)
                }

            class FakeHandler:
                server = SimpleNamespace(
                    target=target,
                    config=config,
                    face_enabled=False,
                    openclip_enabled=True,
                    hide_out_of_focus=False,
                    read_only=True,
                    lan_share=False,
                    source_item_order=source_order,
                    source_month_keys=lambda source, **kwargs: ["2024-01"],
                    source_first_day_item_id=lambda source, day, **kwargs: None,
                )
                response = ""

                def record_server_timing(self, name: str, start: float) -> None:
                    pass

                def respond_html(self, content: str, **kwargs: object) -> None:
                    self.response = content

                def respond_text(self, content: str, **kwargs: object) -> None:
                    raise AssertionError(content)

                def redirect(self, url: str) -> None:
                    raise AssertionError(f"Uventet redirect: {url}")

            handler = FakeHandler()
            with patch(
                "bildebank.server_endpoints_browser.source_item_page_html",
                return_value="item page",
            ) as item_page:
                server_endpoints_browser.respond_search_run(
                    handler,  # type: ignore[arg-type]
                    f"{run_id}/item/{first_id}",
                )

        args = item_page.call_args.args
        self.assertEqual(int(args[3]["id"]), second_id)
        self.assertEqual(int(args[4]["id"]), third_id)
        self.assertEqual(args[1].root_url, f"/search/runs/{run_id}")

    def test_run_server_image_search_rotates_rotated_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                conn.execute("UPDATE files SET view_rotation_degrees = 90 WHERE id = 1")
                conn.commit()
            finally:
                conn.close()

            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=OpenClipConfig(enabled=True)),
                face_enabled=False,
                openclip_enabled=True,
                search_cache=SimpleNamespace(loaded=True),
            )
            result = ImageSearchResult(
                rank=1,
                file_id=1,
                target_path=Path("2024/01/IMG_20240102.png"),
                similarity=0.301,
            )

            body = search_html(server, ServerSearchStats("strand", (result,)), DEFAULT_SEARCH_LIMIT)

        self.assertIn('class="media-link quarter-turn"', body)
        self.assertIn('data-view-rotation="90"', body)
        self.assertIn("transform: rotate(90deg)", body)

    def test_similar_search_ranks_by_cosine_excludes_reference_and_caps_at_100(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            config = OpenClipConfig()
            reference_id = insert_test_file(
                target,
                "2024/01/reference.png",
                sha256="sha-reference",
            )
            embedding_rows = [
                (
                    reference_id,
                    "2024/01/reference.png",
                    "sha-reference",
                    [1.0, 0.0],
                )
            ]
            candidate_ids: list[int] = []
            for index in range(101):
                relative_path = f"2024/02/candidate-{index:03d}.png"
                file_id = insert_test_file(
                    target,
                    relative_path,
                    sha256=f"sha-{index}",
                )
                candidate_ids.append(file_id)
                embedding_rows.append(
                    (
                        file_id,
                        relative_path,
                        f"sha-{index}",
                        [1.0, index / 100.0],
                    )
                )
            self.insert_embeddings(target, config, embedding_rows)
            app_config = AppConfig(openclip=config)
            cache = OpenClipSearchCache(app_config)
            server = SimpleNamespace(
                target=target,
                config=app_config,
                search_cache=cache,
            )

            with patch(
                "bildebank.server_search.load_text_model",
                side_effect=AssertionError("Likhetssøk skal ikke laste tekstmodellen"),
            ):
                stats = search_server_similar_images(
                    server,
                    file_id=reference_id,
                    limit=500,
                )

            self.assertFalse(cache.loaded)
            self.assertIsNotNone(stats.run_id)
            self.assertEqual(len(stats.results), 100)
            self.assertNotIn(reference_id, [result.file_id for result in stats.results])
            self.assertEqual(stats.results[0].file_id, candidate_ids[0])
            self.assertGreater(
                stats.results[0].similarity,
                stats.results[-1].similarity,
            )
            conn = sqlite3.connect(openclip_db_path(target))
            try:
                run = conn.execute(
                    "SELECT id, query, result_limit FROM image_search_runs"
                ).fetchone()
                stored_results = conn.execute(
                    """
                    SELECT file_id, target_path, rank
                    FROM image_search_results
                    WHERE run_id = ?
                    ORDER BY rank
                    """,
                    (run[0],),
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(run[1:], (f"similar:file_id={reference_id}", 100))
        self.assertEqual(len(stored_results), 100)
        self.assertEqual(stored_results[0], (candidate_ids[0], "2024/02/candidate-000.png", 1))
        self.assertEqual([row[2] for row in stored_results], list(range(1, 101)))

    def test_similar_search_uses_and_stores_active_tag_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            config = OpenClipConfig()
            reference_id = insert_test_file(
                target,
                "2024/01/reference.png",
                sha256="sha-reference",
            )
            inside_id = insert_test_file(
                target,
                "2024/01/inside.png",
                sha256="sha-inside",
            )
            outside_id = insert_test_file(
                target,
                "2024/01/outside.png",
                sha256="sha-outside",
            )
            conn = db.connect(target)
            try:
                db.tag_file(conn, file_id=reference_id, tag_name="Familie")
                db.tag_file(conn, file_id=inside_id, tag_name="Familie")
                conn.commit()
            finally:
                conn.close()
            self.insert_embeddings(
                target,
                config,
                [
                    (reference_id, "2024/01/reference.png", "sha-reference", [1.0, 0.0]),
                    (inside_id, "2024/01/inside.png", "sha-inside", [0.6, 0.4]),
                    (outside_id, "2024/01/outside.png", "sha-outside", [1.0, 0.0]),
                ],
            )
            app_config = AppConfig(openclip=config)

            def source_order(source: object, *, hide_out_of_focus: bool = False):
                ids = source_item_ids(target, source)  # type: ignore[arg-type]
                return ids, {
                    file_id: index for index, file_id in enumerate(ids)
                }

            server = SimpleNamespace(
                target=target,
                config=app_config,
                search_cache=OpenClipSearchCache(app_config),
                source_item_order=source_order,
            )

            stats = search_server_similar_images(
                server,
                file_id=reference_id,
                source_url="/tag/Familie",
            )
            run = load_stored_search_run(target, stats.run_id or 0)

        self.assertEqual([result.file_id for result in stats.results], [inside_id])
        self.assertNotIn(outside_id, [result.file_id for result in stats.results])
        self.assertEqual(stats.scope_title, "Tagg: Familie")
        self.assertEqual(stats.scope_root_url, "/tag/Familie")
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run.similar_reference_file_id, reference_id)
        self.assertEqual(run.similar_scope_title, "Tagg: Familie")
        self.assertEqual(run.similar_scope_root_url, "/tag/Familie")

    def test_similar_search_filters_deleted_orphaned_and_out_of_focus_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            config = OpenClipConfig()
            reference_id = insert_test_file(target, "2024/01/reference.png", sha256="sha-reference")
            visible_id = insert_test_file(target, "2024/01/visible.png", sha256="sha-visible")
            hidden_id = insert_test_file(target, "2024/01/hidden.png", sha256="sha-hidden")
            deleted_id = insert_test_file(
                target,
                "deleted/2024/01/deleted.png",
                sha256="sha-deleted",
                deleted=True,
            )
            orphan_id = deleted_id + 1000
            conn = db.connect(target)
            try:
                db.tag_file(conn, file_id=hidden_id, tag_name=db.SYSTEM_TAG_OUT_OF_FOCUS)
                conn.commit()
            finally:
                conn.close()
            self.insert_embeddings(
                target,
                config,
                [
                    (reference_id, "2024/01/reference.png", "sha-reference", [1.0, 0.0]),
                    (visible_id, "2024/01/visible.png", "sha-visible", [0.7, 0.3]),
                    (hidden_id, "2024/01/hidden.png", "sha-hidden", [1.0, 0.0]),
                    (deleted_id, "deleted/2024/01/deleted.png", "sha-deleted", [1.0, 0.0]),
                    (orphan_id, "2024/01/orphan.png", "sha-orphan", [1.0, 0.0]),
                ],
            )
            app_config = AppConfig(
                openclip=config,
                browser=BrowserConfig(hide_out_of_focus=True),
            )
            server = SimpleNamespace(
                target=target,
                config=app_config,
                search_cache=OpenClipSearchCache(app_config),
            )

            stats = search_server_similar_images(
                server,
                file_id=reference_id,
                limit=100,
            )

        self.assertEqual([result.file_id for result in stats.results], [visible_id])

    def test_similar_search_reuses_embedding_cache_and_detects_new_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            config = OpenClipConfig()
            reference_id = insert_test_file(target, "2024/01/reference.png", sha256="sha-reference")
            old_id = insert_test_file(target, "2024/01/old.png", sha256="sha-old")
            self.insert_embeddings(
                target,
                config,
                [
                    (reference_id, "2024/01/reference.png", "sha-reference", [1.0, 0.0]),
                    (old_id, "2024/01/old.png", "sha-old", [0.0, 1.0]),
                ],
            )
            app_config = AppConfig(openclip=config)
            cache = OpenClipSearchCache(app_config)
            server = SimpleNamespace(target=target, config=app_config, search_cache=cache)

            with (
                patch(
                    "bildebank.server_search.load_text_model",
                    side_effect=AssertionError("Likhetssøk skal ikke laste tekstmodellen"),
                ),
                patch(
                    "bildebank.server_search.load_search_embedding_cache",
                    wraps=load_search_embedding_cache,
                ) as load_cache,
            ):
                first = search_server_similar_images(server, file_id=reference_id)
                second = search_server_similar_images(server, file_id=reference_id)
                new_id = insert_test_file(target, "2024/01/new.png", sha256="sha-new")
                self.insert_embeddings(
                    target,
                    config,
                    [(new_id, "2024/01/new.png", "sha-new", [0.9, 0.1])],
                )
                third = search_server_similar_images(server, file_id=reference_id)

        self.assertEqual(load_cache.call_count, 2)
        self.assertEqual([result.file_id for result in first.results], [old_id])
        self.assertEqual([result.file_id for result in second.results], [old_id])
        self.assertEqual([result.file_id for result in third.results], [new_id, old_id])
        self.assertFalse(cache.loaded)

    def test_similar_search_rejects_invalid_inactive_non_image_and_missing_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            config = OpenClipConfig()
            reference_id = insert_test_file(target, "2024/01/reference.png", sha256="sha-reference")
            embedded_id = insert_test_file(target, "2024/01/embedded.png", sha256="sha-embedded")
            deleted_id = insert_test_file(
                target,
                "deleted/2024/01/deleted.png",
                sha256="sha-deleted",
                deleted=True,
            )
            video_id = insert_test_file(target, "2024/01/video.mp4", sha256="sha-video")
            self.insert_embeddings(
                target,
                config,
                [(embedded_id, "2024/01/embedded.png", "sha-embedded", [1.0, 0.0])],
            )
            app_config = AppConfig(openclip=config)
            server = SimpleNamespace(
                target=target,
                config=app_config,
                search_cache=OpenClipSearchCache(app_config),
            )

            for invalid_id in (99999, deleted_id, video_id):
                with self.subTest(file_id=invalid_id), self.assertRaisesRegex(
                    ValueError,
                    "aktivt bilde",
                ):
                    search_server_similar_images(server, file_id=invalid_id)
            with self.assertRaisesRegex(ValueError, "image-scan"):
                search_server_similar_images(server, file_id=reference_id)

    def test_similar_search_refuses_to_run_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            reference_id = insert_test_file(target, "2024/01/reference.png")
            app_config = AppConfig(openclip=OpenClipConfig())
            server = SimpleNamespace(
                target=target,
                config=app_config,
                search_cache=OpenClipSearchCache(app_config),
            )
            (target / LOCK_FILENAME).write_text("command=image-scan\n", encoding="utf-8")

            with self.assertRaises(TargetLockError):
                search_server_similar_images(server, file_id=reference_id)

    def test_similar_search_page_reuses_result_cards_rotation_and_item_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            reference_id = insert_test_file(target, "2024/01/reference & one.png")
            result_id = insert_test_file(target, "2024/01/result.png")
            conn = db.connect(target)
            try:
                conn.execute(
                    "UPDATE files SET view_rotation_degrees = 90 WHERE id = ?",
                    (result_id,),
                )
                conn.commit()
            finally:
                conn.close()
            server = SimpleNamespace(
                target=target,
                face_enabled=False,
                openclip_enabled=True,
            )
            stats = ServerSimilarSearchStats(
                reference_file_id=reference_id,
                reference_target_path=Path("2024/01/reference & one.png"),
                results=(
                    ImageSearchResult(1, result_id, Path("2024/01/result.png"), 0.75),
                ),
            )

            body = similar_search_html(server, stats)
            scoped_body = similar_search_html(
                server,
                ServerSimilarSearchStats(
                    reference_file_id=reference_id,
                    reference_target_path=Path("2024/01/reference & one.png"),
                    results=stats.results,
                    run_id=12,
                    scope_title="Tagg: Familie",
                    scope_root_url="/tag/Familie",
                ),
            )

        self.assertIn("Bilder som ligner på reference &amp; one.png", body)
        self.assertIn(f'href="/item/{reference_id}"', body)
        self.assertIn(f'href="/item/{result_id}"', body)
        self.assertIn(f'src="/file/{result_id}"', body)
        self.assertIn('class="media-link quarter-turn"', body)
        self.assertIn('data-view-rotation="90"', body)
        self.assertIn("score=0.750", body)
        self.assertIn('href="/tag/Familie">«Tagg: Familie»</a>', scoped_body)
        self.assertIn(
            f'href="/tag/Familie/item/{reference_id}"',
            scoped_body,
        )
        self.assertIn(">Søk i alle bilder</button>", scoped_body)
        self.assertNotIn('name="source_url"', scoped_body)

    def test_similar_search_button_only_appears_for_writable_openclip_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            image_id = insert_test_file(target, "2024/01/image.png")
            video_id = insert_test_file(target, "2024/01/video.mp4")
            image = item_by_id(target, image_id)
            video = item_by_id(target, video_id)
            month_nav = {
                "previous_year": None,
                "next_year": None,
                "previous_month": None,
                "next_month": None,
            }
            image_body = item_page_html(target, image, None, None, month_nav)
            disabled_body = item_page_html(
                target,
                image,
                None,
                None,
                month_nav,
                openclip_enabled=False,
            )
            read_only_body = item_page_html(
                target,
                image,
                None,
                None,
                month_nav,
                read_only=True,
            )
            video_body = item_page_html(target, video, None, None, month_nav)
            scoped_body = source_item_page_html(
                target,
                tag_browser_source("Familie"),
                image,
                None,
                None,
                month_nav,
            )

        self.assertIn('action="/search/similar"', image_body)
        self.assertIn('name="file_id" value="1"', image_body)
        self.assertIn('name="limit" value="100"', image_body)
        self.assertIn('aria-label="Finn lignende bilder">🔍≈</button>', image_body)
        self.assertIn('name="source_url" value="/tag/Familie"', scoped_body)
        self.assertIn(
            'aria-label="Finn lignende i utvalget «Tagg: Familie»"',
            scoped_body,
        )
        self.assertNotIn("🔍≈", disabled_body)
        self.assertNotIn("🔍≈", read_only_body)
        self.assertNotIn("🔍≈", video_body)

    def test_similar_search_get_does_not_run_search(self) -> None:
        class FakeHandler:
            path = "/search/similar?file_id=1"
            server = SimpleNamespace(read_only=False, slideshow=None)
            response: tuple[str, HTTPStatus] | None = None

            def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                self.response = (content, status)

        handler = FakeHandler()
        with patch(
            "bildebank.server_handler.search_server_similar_images",
            side_effect=AssertionError("GET skal ikke kjøre likhetssøk"),
        ):
            BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.response, ("Endepunktet krever POST.", HTTPStatus.METHOD_NOT_ALLOWED))

    def test_similar_search_post_requires_csrf_and_is_blocked_in_read_only_mode(self) -> None:
        class FakeHandler:
            path = "/search/similar"
            headers = {"Content-Length": "0"}
            rfile = BytesIO()
            server = SimpleNamespace(read_only=False, slideshow=None, csrf_token="token")
            response: tuple[object, HTTPStatus] | None = None

            def respond_json(self, content: object, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                self.response = (content, status)

            def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                self.response = (content, status)

            def respond_read_only_forbidden(self, path: str) -> None:
                BildebankRequestHandler.respond_read_only_forbidden(self, path)  # type: ignore[arg-type]

        handler = FakeHandler()
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]
        self.assertEqual(handler.response[1], HTTPStatus.FORBIDDEN)
        self.assertIn("CSRF", handler.response[0]["error"])

        handler.server.read_only = True
        handler.response = None
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]
        self.assertEqual(handler.response[1], HTTPStatus.FORBIDDEN)
        self.assertIn("read-only", handler.response[0])

    def test_similar_search_post_rejects_disabled_openclip_and_invalid_file_id(self) -> None:
        def make_handler(*, enabled: bool, body: bytes) -> object:
            class FakeHandler:
                path = "/search/similar"
                headers = {
                    "Content-Length": str(len(body)),
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                rfile = BytesIO(body)
                server = SimpleNamespace(
                    read_only=False,
                    slideshow=None,
                    csrf_token="token",
                    openclip_enabled=enabled,
                    face_enabled=False,
                )
                response: tuple[str, HTTPStatus] | None = None

                def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.response = (content, status)

                def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.response = (content, status)

            return FakeHandler()

        disabled_body = b"file_id=1&csrf_token=token"
        disabled = make_handler(enabled=False, body=disabled_body)
        BildebankRequestHandler.do_POST(disabled)  # type: ignore[arg-type]
        self.assertEqual(disabled.response, ("Bildelikhetssøk er av.", HTTPStatus.NOT_FOUND))

        invalid_body = b"file_id=nope&csrf_token=token"
        invalid = make_handler(enabled=True, body=invalid_body)
        BildebankRequestHandler.do_POST(invalid)  # type: ignore[arg-type]
        self.assertEqual(invalid.response[1], HTTPStatus.BAD_REQUEST)
        self.assertIn("Ugyldig file_id", invalid.response[0])

    def test_similar_search_post_renders_results_and_caps_hidden_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            encoded = (
                b"file_id=7&limit=999&source_url=%2Ftag%2FFamilie"
                b"&csrf_token=token"
            )

            class FakeHandler:
                path = "/search/similar"
                headers = {
                    "Content-Length": str(len(encoded)),
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                rfile = BytesIO(encoded)
                server = SimpleNamespace(
                    target=target,
                    read_only=False,
                    slideshow=None,
                    csrf_token="token",
                    openclip_enabled=True,
                    face_enabled=False,
                )
                response: tuple[str, HTTPStatus] | None = None

                def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.response = (content, status)

            stats = ServerSimilarSearchStats(
                reference_file_id=7,
                reference_target_path=Path("2024/01/reference.png"),
                results=(),
            )
            handler = FakeHandler()
            with patch(
                "bildebank.server_handler.search_server_similar_images",
                return_value=stats,
            ) as similar_search:
                BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        similar_search.assert_called_once_with(
            handler.server,
            file_id=7,
            limit=100,
            source_url="/tag/Familie",
        )
        self.assertEqual(handler.response[1], HTTPStatus.OK)
        self.assertIn("Bilder som ligner på reference.png", handler.response[0])

    def test_similar_search_post_reports_target_lock_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            (target / LOCK_FILENAME).write_text("command=image-scan\n", encoding="utf-8")
            encoded = b"file_id=1&limit=100&csrf_token=token"

            class FakeHandler:
                path = "/search/similar"
                headers = {
                    "Content-Length": str(len(encoded)),
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                rfile = BytesIO(encoded)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(openclip=OpenClipConfig()),
                    search_cache=OpenClipSearchCache(AppConfig(openclip=OpenClipConfig())),
                    read_only=False,
                    slideshow=None,
                    csrf_token="token",
                    openclip_enabled=True,
                    face_enabled=False,
                )
                response: tuple[str, HTTPStatus] | None = None

                def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.response = (content, status)

            handler = FakeHandler()
            BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.response[1], HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", handler.response[0])
