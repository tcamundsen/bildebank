from __future__ import annotations

import sqlite3
import tempfile
import unittest
import os
import uuid
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank.cli import (
    build_parser,
    main,
    wsl_path_from_windows_path,
)
from bildebank.db import DB_FILENAME
from bildebank.media import ImageDimensions
from bildebank.media_cache import cached_image_dimensions, cached_image_orientation
from bildebank.target_lock import LOCK_FILENAME, TargetLockError
from tests.cli_helpers import capture_cli, run_cli
from tests.test_media import (
    minimal_png,
)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.program_root_tempdir = tempfile.TemporaryDirectory()
        self.program_root = Path(self.program_root_tempdir.name)
        self.program_root_patcher = patch("bildebank.cli.program_repo_root", return_value=self.program_root)
        self.program_root_patcher.start()

    def tearDown(self) -> None:
        self.program_root_patcher.stop()
        self.program_root_tempdir.cleanup()

    def enable_face_recognition_config(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[face_recognition]
enabled = true
provider = "cpu"
model_root = ".bildebank-insightface"
model_name = "buffalo_l"
""",
            encoding="utf-8",
        )

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

    def test_main_without_arguments_shows_help(self) -> None:
        code, stdout, stderr = capture_cli([])

        self.assertEqual(code, 0)
        self.assertIn("usage: bildebank [-h] [--version] <kommando> [<args>]", stdout)
        self.assertIn("Bildebank 0.4.0", stdout)
        self.assertIn("Kom i gang:", stdout)
        self.assertIn("bildebank start", stdout)
        self.assertIn("Bildebank-vinduet kan opprette samling", stdout)
        self.assertIn("Må fortsatt kjøres fra PowerShell:", stdout)
        self.assertIn("Kontrollister", stdout)
        self.assertIn("status, errors, conflicts, show-conflict, non-metadata", stdout)
        self.assertIn("Full kommandoliste:", stdout)
        self.assertIn("docs\\reference.md", stdout)
        self.assertIn("bildebank <kommando> -h", stdout)
        self.assertNotIn('bildebank import --name "Mobil 2024" --dry-run "E:\\DCIM"', stdout)
        self.assertNotIn("Vanlige kommandoer:", stdout)
        self.assertEqual(stderr, "")

    def test_main_help_points_to_window_and_remaining_cli_commands(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("usage: bildebank [-h] [--version] <kommando> [<args>]", stdout)
        self.assertIn("Bildebank 0.4.0", stdout)
        self.assertNotIn("--target", stdout)
        self.assertIn("Kom i gang:\n   bildebank start", stdout)
        self.assertIn("Bildebank-vinduet kan opprette samling", stdout)
        self.assertNotIn("launcher", stdout)
        self.assertIn("Må fortsatt kjøres fra PowerShell:", stdout)
        self.assertIn("Kontrollister", stdout)
        self.assertIn("status, errors, conflicts, show-conflict, non-metadata", stdout)
        self.assertIn("Metadata", stdout)
        self.assertIn("explain-date, inspect-metadata, refresh-metadata", stdout)
        self.assertIn("Ansikter og bildesøk", stdout)
        self.assertIn("face-report, face-reset, cleanup-image-search", stdout)
        self.assertIn("Full kommandoliste:", stdout)
        self.assertIn("bildebank <kommando> -h", stdout)
        self.assertNotIn("Vanlige kommandoer:", stdout)
        self.assertNotIn("HTML-eksport\n   make-thumbnails", stdout)
        self.assertNotIn("{create,add,import", stdout)
        self.assertNotIn("face-group", stdout)
        self.assertNotIn("face-person-add-group", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_debug_shows_traceback_for_unhandled_errors(self) -> None:
        with patch("bildebank.cli.run", side_effect=RuntimeError("boom")):
            code, stdout, stderr = capture_cli(["--debug", "status"])

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Traceback (most recent call last):", stderr)
        self.assertIn("RuntimeError: boom", stderr)

    def test_errors_are_short_without_debug(self) -> None:
        with patch("bildebank.cli.run", side_effect=RuntimeError("boom")):
            code, stdout, stderr = capture_cli(["status"])

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "Feil: boom\n")

    def test_subcommand_help_has_clean_usage(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["create", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("usage: bildebank create [valg] mappe", stdout)
        self.assertIn("mappe       Mappen som skal bli bildesamling", stdout)
        self.assertNotIn("<kommando> [<args>] create", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_start_and_launcher_commands_open_launcher(self) -> None:
        with patch("bildebank.launcher.main", return_value=0) as launcher_main:
            self.assertEqual(main(["start"]), 0)
            self.assertEqual(main(["launcher"]), 0)

        self.assertEqual(launcher_main.call_count, 2)

    def test_make_people_browser_help(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["make-people-browser", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("make-people-browser", stdout)
        self.assertIn("--month-preview-limit", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_media_metadata_cache_stores_dimensions_and_orientation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            target_path = target / "2024" / "01" / "IMG_20240102.png"

            dimensions = cached_image_dimensions(target, target_path)
            orientation = cached_image_orientation(target, target_path)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute(
                    """
                    SELECT media_width, media_height, media_orientation, media_metadata_mtime_ns
                    FROM files
                    WHERE stored_filename = 'IMG_20240102.png'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(dimensions, ImageDimensions(100, 80))
        self.assertEqual(orientation, 1)
        self.assertEqual(row[:3], (100, 80, 1))
        self.assertIsNotNone(row[3])

    def test_media_metadata_cache_miss_requires_target_lock_but_hit_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            target_path = target / "2024" / "01" / "IMG_20240102.png"
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=remove\n", encoding="utf-8")

            with self.assertRaises(TargetLockError):
                cached_image_dimensions(target, target_path)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                uncached = conn.execute(
                    "SELECT media_width, media_height, media_metadata_mtime_ns FROM files"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(uncached, (None, None, None))

            lock_path.unlink()
            self.assertEqual(cached_image_dimensions(target, target_path), ImageDimensions(100, 80))
            lock_path.write_text("command=remove\n", encoding="utf-8")

            self.assertEqual(cached_image_dimensions(target, target_path), ImageDimensions(100, 80))

    def test_target_command_is_not_available(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["target", "."])

    def test_old_commands_are_not_available(self) -> None:
        for command in (
            "add",
            "import-removable",
            "list-name-conflicts",
            "show-name-conflict",
            "delete",
            "list-deleted",
            "make-face-groups-browser",
            "remove-source",
        ):
            with self.subTest(command=command):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    build_parser().parse_args([command])

    def test_create_stores_collection_id_and_keeps_it_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                first_collection_id = conn.execute(
                    "SELECT value FROM meta WHERE key = 'collection_id'"
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(str(uuid.UUID(first_collection_id)), first_collection_id)

            self.assertEqual(run_cli(["--target", str(target), "status"]), 0)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                second_collection_id = conn.execute(
                    "SELECT value FROM meta WHERE key = 'collection_id'"
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(second_collection_id, first_collection_id)

    def test_create_rejects_existing_collection_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            code, stdout, stderr = capture_cli(["create", str(target)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamling finnes allerede", stderr)

    def test_rejects_target_inside_program_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "bildebank"
            target = repo / "samling"
            repo.mkdir()

            with patch("bildebank.cli.program_repo_root", return_value=repo.resolve()):
                code, stdout, stderr = capture_cli(["create", str(target)])

            self.assertEqual(code, 1)
            self.assertIn("Bildesamlingen kan ikke ligge inni programmappen", stderr)
            self.assertFalse((target / DB_FILENAME).exists())

    def test_wsl_path_from_windows_path_maps_drive_path(self) -> None:
        if os.name == "nt":
            self.skipTest("WSL path mapping is only used outside Windows")

        self.assertEqual(
            wsl_path_from_windows_path(r"C:\Users\TA487\kode\usbA"),
            Path("/mnt/c/Users/TA487/kode/usbA"),
        )



if __name__ == "__main__":
    unittest.main()
