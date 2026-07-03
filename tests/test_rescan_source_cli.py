from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bildebank.db import DB_FILENAME
from bildebank.target_lock import LOCK_FILENAME
from tests.test_cli import capture_cli, run_cli


class RescanSourceCliTests(unittest.TestCase):
    def test_rescan_source_imports_new_supported_files_without_new_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"jpeg")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            (source / "IMG_20240103.nef").write_bytes(b"raw-photo")

            code, stdout, stderr = capture_cli(["--target", str(target), "rescan-source", "--name", source.name, "--quiet"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("importert=1", stdout)
            self.assertIn("eksisterende=1", stdout)
            imported_raw = target / "2024" / "01" / "IMG_20240103.nef"
            self.assertEqual(imported_raw.read_bytes(), b"raw-photo")
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 2)
                raw_source = conn.execute(
                    """
                    SELECT sources.name
                    FROM file_sources
                    JOIN sources ON sources.id = file_sources.source_id
                    WHERE file_sources.source_path LIKE ?
                    """,
                    ("%IMG_20240103.nef",),
                ).fetchone()[0]
                self.assertEqual(raw_source, source.name)
            finally:
                conn.close()

    def test_rescan_source_dry_run_does_not_copy_or_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"jpeg")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            (source / "IMG_20240103.nef").write_bytes(b"raw-photo")
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "rescan-source", "--name", source.name, "--dry-run", "--quiet"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("importert=1", stdout)
            self.assertFalse((target / "2024" / "01" / "IMG_20240103.nef").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                commands_after = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
                self.assertEqual(commands_after, commands_before)
            finally:
                conn.close()

    def test_rescan_source_records_duplicate_without_copying_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source_a = root / "source-a"
            source_b = root / "source-b"
            source_a.mkdir()
            source_b.mkdir()
            (source_a / "IMG_20240102.jpg").write_bytes(b"same")
            (source_b / "placeholder_20240101.jpg").write_bytes(b"placeholder")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source_a.name, "--quiet", str(source_a)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source_b.name, "--quiet", str(source_b)]), 0)
            (source_b / "COPY_20240102.nef").write_bytes(b"same")

            code, stdout, stderr = capture_cli(["--target", str(target), "rescan-source", "--name", source_b.name, "--quiet"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("duplikater=1", stdout)
            self.assertFalse((target / "2024" / "01" / "COPY_20240102.nef").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 3)
                source_names = [
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT sources.name
                        FROM file_sources
                        JOIN sources ON sources.id = file_sources.source_id
                        WHERE file_sources.sha256 = (SELECT sha256 FROM files WHERE stored_filename = 'IMG_20240102.jpg')
                        ORDER BY sources.name
                        """
                    )
                ]
                self.assertEqual(source_names, ["source-a", "source-b"])
            finally:
                conn.close()

    def test_rescan_source_takes_target_lock_before_database_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=rescan-source\n", encoding="utf-8")

            with (
                patch("bildebank.cli.recover_pending_file_moves"),
                patch("bildebank.cli.db.connect", side_effect=AssertionError("db before lock")),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "rescan-source", "--name", "missing"]
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)

