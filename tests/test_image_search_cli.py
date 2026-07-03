from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.cli import main
from bildebank.cli_image import print_image_search_progress
from bildebank.config import AppConfig, OpenClipConfig
from bildebank.db import init_database
from bildebank.openclip import (
    connect_openclip_db,
    embedding_blob,
    openclip_db_path,
    resolve_torch_device,
    search_images,
)
from bildebank.server_search import OpenClipSearchCache
from bildebank.target_lock import LOCK_FILENAME
from tests.cli_helpers import capture_cli, run_cli
from tests.db_test_helpers import insert_openclip_cleanup_fixture, insert_test_file


class ImageSearchCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.program_root_tempdir = tempfile.TemporaryDirectory()
        self.program_root = Path(self.program_root_tempdir.name)
        self.program_root_patcher = patch("bildebank.cli.program_repo_root", return_value=self.program_root)
        self.program_root_patcher.start()

    def tearDown(self) -> None:
        self.program_root_patcher.stop()
        self.program_root_tempdir.cleanup()

    def enable_openclip_config(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[openclip]
enabled = true
model_root = ".bildebank-openclip"
device = "cpu"
model_name = "ViT-B-32"
pretrained = "laion2b_s34b_b79k"
""",
            encoding="utf-8",
        )

    def test_image_search_help_documents_limit_and_no_browser(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["image-search", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("usage: bildebank image-search [valg] søk", stdout)
        self.assertIn("--limit", stdout)
        self.assertIn("--no-browser", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_cleanup_image_search_help_documents_apply(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["cleanup-image-search", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("usage: bildebank cleanup-image-search [valg]", stdout)
        self.assertIn("--apply", stdout)
        self.assertIn("foreldreløse", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_openclip_database_schema_is_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            conn = connect_openclip_db(target)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                conn.close()

            self.assertEqual(openclip_db_path(target), target / ".bilder-openclip.sqlite3")
            self.assertIn("image_embeddings", tables)
            self.assertIn("image_search_runs", tables)
            self.assertIn("image_search_results", tables)

    def test_openclip_device_validation(self) -> None:
        self.assertEqual(resolve_torch_device("cpu"), "cpu")
        with self.assertRaises(ValueError):
            resolve_torch_device("gpu")

    def test_openclip_search_cache_can_preload_model(self) -> None:
        config = OpenClipConfig(enabled=True)
        cache = OpenClipSearchCache(AppConfig(openclip=config))
        with patch("bildebank.server_search.load_text_model", return_value=("model", "tokenizer")) as load_model:
            cache.preload_model()

        load_model.assert_called_once_with(config)
        self.assertTrue(cache.loaded)

    def test_cli_image_search_ignores_orphan_openclip_embeddings(self) -> None:
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

            with (
                patch("bildebank.openclip.load_text_model", return_value=(object(), object())),
                patch("bildebank.openclip.text_embedding", return_value=[0.0, 1.0]),
            ):
                stats = search_images(target, config, query="cat", limit=10)

            self.assertEqual([result.file_id for result in stats.results], [active_id])
            self.assertEqual(stats.results[0].target_path, Path("2024/01/active.png"))
            conn = sqlite3.connect(openclip_db_path(target))
            try:
                self.assertEqual(
                    [
                        row[0]
                        for row in conn.execute(
                            "SELECT file_id FROM image_search_results ORDER BY rank"
                        )
                    ],
                    [active_id],
                )
            finally:
                conn.close()

    def test_cleanup_image_search_dry_run_lists_orphans_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            ids = insert_openclip_cleanup_fixture(target)

            code, stdout, stderr = capture_cli(["--target", str(target), "cleanup-image-search"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("foreldreløse_embeddings=2", stdout)
            self.assertIn("foreldreløse_søkeresultater=2", stdout)
            self.assertIn(f"image_embeddings\tfile #{ids['deleted_id']}", stdout)
            self.assertIn(f"image_search_results\tfile #{ids['missing_id']}", stdout)
            self.assertIn("Dry-run: ingen endringer er gjort.", stdout)
            self.assertIn("Kjør: bildebank cleanup-image-search --apply", stdout)
            conn = sqlite3.connect(openclip_db_path(target))
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_embeddings").fetchone()[0], 3)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_search_results").fetchone()[0], 3)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_search_runs").fetchone()[0], 3)
            finally:
                conn.close()

    def test_cleanup_image_search_apply_deletes_only_orphan_rows_and_empty_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            ids = insert_openclip_cleanup_fixture(target)

            code, stdout, stderr = capture_cli(["--target", str(target), "cleanup-image-search", "--apply"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("foreldreløse_embeddings=2", stdout)
            self.assertIn("foreldreløse_søkeresultater=2", stdout)
            self.assertIn("Slettet: image_embeddings=2, image_search_results=2, tomme_image_search_runs=2", stdout)
            conn = sqlite3.connect(openclip_db_path(target))
            try:
                self.assertEqual(
                    conn.execute("SELECT file_id FROM image_embeddings").fetchall(),
                    [(ids["active_id"],)],
                )
                self.assertEqual(
                    conn.execute("SELECT file_id FROM image_search_results").fetchall(),
                    [(ids["active_id"],)],
                )
                self.assertEqual(
                    conn.execute("SELECT id FROM image_search_runs").fetchall(),
                    [(ids["active_run_id"],)],
                )
            finally:
                conn.close()

    def test_cleanup_image_search_reports_missing_openclip_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)

            code, stdout, stderr = capture_cli(["--target", str(target), "cleanup-image-search"])

            self.assertEqual(code, 0, stderr)
            self.assertEqual(stdout, "Ingen OpenCLIP-database å rydde.\n")
            self.assertEqual(stderr, "")
            self.assertFalse(openclip_db_path(target).exists())

    def test_cleanup_image_search_reports_legacy_openclip_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = sqlite3.connect(openclip_db_path(target))
            try:
                conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "cleanup-image-search"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("OpenCLIP-databasen mangler tabellen image_embeddings", stderr)
            self.assertIn("Kjør bildebank image-scan på nytt", stderr)

    def test_openclip_database_rejects_absolute_target_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            absolute_image = target / "2024" / "01" / "IMG_20240102.jpg"
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
                        str(absolute_image),
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

            with self.assertRaisesRegex(ValueError, "OpenCLIP-databasen har absolutt target_path"):
                connect_openclip_db(target).close()

    def test_image_commands_require_enabled_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "image-scan", "--limit", "1"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Tekstbasert bildesøk er av", stderr)
            self.assertFalse(openclip_db_path(target).exists())

            code, stdout, stderr = capture_cli(["--target", str(target), "image-search", "strand"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Tekstbasert bildesøk er av", stderr)
            self.assertFalse(openclip_db_path(target).exists())

    def test_image_scan_and_search_refuse_to_run_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.enable_openclip_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            scan_code, _scan_stdout, scan_stderr = capture_cli(
                ["--target", str(target), "image-scan", "--limit", "1"]
            )
            search_code, _search_stdout, search_stderr = capture_cli(
                ["--target", str(target), "image-search", "strand", "--no-browser"]
            )

        self.assertEqual(scan_code, 1)
        self.assertIn("Bildesamlingen er låst", scan_stderr)
        self.assertEqual(search_code, 1)
        self.assertIn("Bildesamlingen er låst", search_stderr)

    def test_image_search_passes_browser_option_to_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.enable_openclip_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)

            with patch("bildebank.cli_image.run_image_search", return_value=0) as run_search:
                self.assertEqual(run_cli(["--target", str(target), "image-search", "strand"]), 0)
                run_search.assert_called_once_with(
                    target.resolve(),
                    repo_root=self.program_root,
                    query="strand",
                    limit=100,
                    browser=True,
                )

            with patch("bildebank.cli_image.run_image_search", return_value=0) as run_search:
                self.assertEqual(
                    run_cli(["--target", str(target), "image-search", "strand", "--no-browser"]),
                    0,
                )
                run_search.assert_called_once_with(
                    target.resolve(),
                    repo_root=self.program_root,
                    query="strand",
                    limit=100,
                    browser=False,
                )

    def test_image_search_progress_uses_progress_meter(self) -> None:
        stdout = StringIO()
        stats = SimpleNamespace(query="strand")

        with redirect_stdout(stdout):
            print_image_search_progress("load_model", 0, 10, stats)
            print_image_search_progress("compare_start", 0, 10, stats)
            print_image_search_progress("compare", 10, 10, stats)
            print_image_search_progress("write", 5, 5, stats)
            print_image_search_progress("done", 5, 5, stats)

        output = stdout.getvalue()
        self.assertIn("Image-search: søker etter \"strand\" i 10 bilder.", output)
        self.assertIn("Image-search: søkt=10/10, gjenstår=0s", output)
        self.assertIn("Image-search: skriver 5 treff til image-search.html.", output)
