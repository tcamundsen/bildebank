from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bildebank import db
from bildebank.db import DB_FILENAME
from bildebank.server_browser import out_of_focus_file_ids
from tests.test_cli import capture_cli, minimal_mp4_with_creation_date, run_cli


class StatusReportingCliTests(unittest.TestCase):
    def test_status_and_browser_database_preparation_do_not_write_current_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            database_path = target / DB_FILENAME
            before = database_path.read_bytes()
            before_mtime = database_path.stat().st_mtime_ns

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])
            db.prepare_database(target)
            out_of_focus_file_ids(target)

            self.assertEqual(code, 0, stderr)
            self.assertIn("Importerte filer: 0", stdout)
            self.assertEqual(database_path.read_bytes(), before)
            self.assertEqual(database_path.stat().st_mtime_ns, before_mtime)

    def test_status_counts_media_types_and_date_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Totalt: 2", stdout)
            self.assertIn("Bilder: 1", stdout)
            self.assertIn("Videoer: 1", stdout)
            self.assertIn("  metadata: 1", stdout)
            self.assertIn("  filename: 1", stdout)
            self.assertIn("  mtime: 0", stdout)
            self.assertIn("Kilder: 1", stdout)
            self.assertIn("Importerte filer: 2", stdout)
            self.assertIn("Kildefilforekomster: 2", stdout)
            self.assertIn("Duplikatkilder: 0", stdout)
            self.assertIn("Uløste feil: 0", stdout)
            self.assertIn("Navnekollisjoner: 0", stdout)
            self.assertIn("Filer uten dato: 0", stdout)

    def test_errors_lists_recorded_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            missing = root / "missing-source"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    "insert into errors(stage, source_path, message) values(?, ?, ?)",
                    ("refresh-metadata", str(missing), "Målfil finnes ikke"),
                )
                conn.execute(
                    """
                    insert into errors(stage, source_path, message, resolved_at)
                    values(?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    ("refresh-metadata", str(root / "fixed"), "Løst feil"),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "errors"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("refresh-metadata", stdout)
            self.assertIn("Målfil finnes ikke", stdout)
            self.assertNotIn("Løst feil", stdout)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "errors", "--include-resolved"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Løst feil", stdout)

    def test_report_prints_status_merge_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "report"])

        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "report er slått sammen med status\n")

    def test_vacuum_packs_current_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            openclip_db = target / ".bilder-openclip.sqlite3"
            face_dir = target / ".bildebank-faces"
            face_dir.mkdir()
            face_db = face_dir / "buffalo_l.sqlite3"
            other_face_db = face_dir / "buffalo_s.sqlite3"
            for database_path in (openclip_db, face_db, other_face_db):
                conn = sqlite3.connect(database_path)
                try:
                    conn.execute("CREATE TABLE test_data(value TEXT)")
                    conn.execute("INSERT INTO test_data(value) VALUES('test')")
                    conn.commit()
                finally:
                    conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "vacuum"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Database: Hoveddatabase", stdout)
            self.assertIn("Database: Bildesøkdatabase", stdout)
            self.assertEqual(stdout.count("Database: Ansiktsdatabase"), 2)
            self.assertIn(str(openclip_db), stdout)
            self.assertIn(str(face_db), stdout)
            self.assertIn(str(other_face_db), stdout)
            self.assertIn("Størrelse før:", stdout)
            self.assertIn("Størrelse etter:", stdout)
            self.assertIn("Ferdig. Databasene er pakket.", stdout)

