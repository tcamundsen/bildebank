from __future__ import annotations

import sqlite3
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank import db
from bildebank.config import AppConfig, BrowserConfig, OpenClipConfig
from bildebank.db import init_database
from bildebank.openclip import ImageSearchResult, connect_openclip_db, embedding_blob, openclip_db_path
from bildebank.server_handler import BildebankRequestHandler
from bildebank.server_pages import search_html, search_start_html
from bildebank.server_search import (
    DEFAULT_SEARCH_LIMIT,
    OpenClipSearchCache,
    ServerSearchStats,
    load_search_embedding_cache,
    search_server_images,
)
from bildebank.target_lock import LOCK_FILENAME, TargetLockError
from tests.cli_helpers import run_cli
from tests.db_test_helpers import insert_test_file, register_target_file
from tests.test_media import minimal_png


class ServerSearchCliTests(unittest.TestCase):
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

        handler = FakeHandler()
        BildebankRequestHandler.respond_search_preload(handler)  # type: ignore[arg-type]

        self.assertTrue(handler.server.search_cache.started)
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.body, {"ok": True, "status": "loading", "loaded": False})

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

    def test_run_server_search_route_reports_target_lock_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            (target / LOCK_FILENAME).write_text("command=image-scan\n", encoding="utf-8")

            class FakeHandler:
                path = "/search?q=test"
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(openclip=OpenClipConfig(enabled=True)),
                    face_enabled=True,
                    openclip_enabled=True,
                    search_cache=OpenClipSearchCache(AppConfig(openclip=OpenClipConfig(enabled=True))),
                )
                body = ""
                status: HTTPStatus | None = None

                def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", handler.body)

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

    def test_run_server_image_search_links_item_but_uses_target_path_for_image_url(self) -> None:
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

        self.assertIn('src="/file/2025/07/PXL%2020250709_193516074.jpg"', body)
        self.assertIn('href="/item/999"', body)
        self.assertNotIn('href="/file/2025/07/PXL%2020250709_193516074.jpg"', body)
        self.assertNotIn('src="/file/999"', body)

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
