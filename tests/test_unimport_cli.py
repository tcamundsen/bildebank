from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections.abc import Iterable
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.cli import build_parser
from bildebank.db import DB_FILENAME
from tests.cli_helpers import capture_cli, run_cli


class UnimportCliTests(unittest.TestCase):
    def test_unimport_duplicate_source_keeps_shared_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source1 = root / "source1"
            source2 = root / "source2"
            source1.mkdir()
            source2.mkdir()
            (source1 / "IMG_20240102.jpg").write_bytes(b"same")
            (source2 / "COPY_20240203.jpg").write_bytes(b"same")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source1.name, "--quiet", str(source1)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source2.name, "--quiet", str(source2)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", source2.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Unimport: filer i kilden=1/1", stdout)
            self.assertIn("Unimport: filer=0/0", stdout)
            self.assertIn("Filer som fjernes fra aktiv samling: 0", stdout)
            self.assertIn("Filer som blir liggende fordi de også finnes i andre kilder: 1", stdout)
            self.assertTrue(imported.exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                self.assertIsNone(conn.execute(
                    "SELECT id FROM sources WHERE path = ?",
                    (str(source2.resolve()),),
                ).fetchone())
            finally:
                conn.close()

    def test_unimport_only_source_removes_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            self.assertTrue(imported.exists())

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", source.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Unimport: filer i kilden=1/1", stdout)
            self.assertIn("Unimport: filer=1/1", stdout)
            self.assertIn("Filer som fjernes fra aktiv samling: 1", stdout)
            self.assertIn("Kilden er fjernet fra kildelisten.", stdout)
            self.assertFalse(imported.exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
            finally:
                conn.close()

    def test_unimport_only_source_with_duplicate_content_removes_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"same image")
            (source / "COPY_20240103.jpg").write_bytes(b"same image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
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
                ),
                0,
            )
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0],
                    2,
                )
                imported_path = Path(
                    conn.execute("SELECT target_path FROM files").fetchone()[0]
                )
            finally:
                conn.close()

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", source.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Registrerte originalfiler kontrollert: 2", stdout)
            self.assertIn("Filer som fjernes fra aktiv samling: 1", stdout)
            self.assertFalse((target / imported_path).exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0],
                    0,
                )
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
            finally:
                conn.close()

    def test_unimport_dry_run_reports_directory_plan_without_changes_or_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"

            with patch("builtins.input", side_effect=AssertionError("dry-run should not ask")):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--dry-run", "--name", source.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Unimport: filer i kilden=1/1", stdout)
            self.assertIn("Unimport: filer=1/1", stdout)
            self.assertIn("Filer som fjernes fra aktiv samling: 1", stdout)
            self.assertIn("Kilden ville blitt fjernet fra kildelisten.", stdout)
            self.assertIn("Dry-run: ingen endringer er gjort.", stdout)
            self.assertTrue(imported.exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                status = conn.execute("SELECT status FROM sources").fetchone()[0]
                self.assertEqual(status, "imported")
                commands = [
                    row[0]
                    for row in conn.execute("SELECT command FROM command_log ORDER BY id").fetchall()
                ]
                self.assertEqual(commands, ["create", "import"])
            finally:
                conn.close()

    def test_unimport_plan_does_not_count_sources_per_file(self) -> None:
        class CountingConnection:
            def __init__(self, conn: sqlite3.Connection) -> None:
                self.conn = conn
                self.file_source_count_queries = 0

            def execute(self, sql: str, parameters: Iterable[object] = ()):
                normalized = " ".join(sql.split()).lower()
                if normalized.startswith("select count(*) from file_sources where file_id"):
                    self.file_source_count_queries += 1
                return self.conn.execute(sql, tuple(parameters))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            for index in range(3):
                (source / f"IMG_2024010{index + 1}.jpg").write_bytes(f"image-{index}".encode("ascii"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            conn = sqlite3.connect(target / DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                source_row = db.find_source_by_name(conn, source.name)
                assert source_row is not None
                counting_conn = CountingConnection(conn)
                plan = db.build_unimport_plan(counting_conn, target, source_row)  # type: ignore[arg-type]
            finally:
                conn.close()

        self.assertEqual(plan.source_file_count, 3)
        self.assertEqual(plan.active_remove_count, 3)
        self.assertEqual(counting_conn.file_source_count_queries, 0)

    def test_unimport_aborts_without_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"

            with patch("builtins.input", return_value="ja"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", source.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Avbrutt. Ingen endringer er gjort.", stdout)
            self.assertTrue(imported.exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
            finally:
                conn.close()

    def test_unimport_aborts_when_source_file_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            source_file.write_bytes(b"changed")

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", source.name]
                )

            self.assertEqual(code, 1)
            self.assertIn("Unimport: kontrollerer 1 filer i kilden.", stdout)
            self.assertIn("Originalfilen har endret", stderr)
            self.assertTrue((target / "2024" / "01" / "IMG_20240102.jpg").exists())

    def test_unimport_warns_and_aborts_when_target_file_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            imported.write_bytes(b"changed target")

            with patch("builtins.input", side_effect=["ja, det vil jeg", "nei"]):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", source.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("ADVARSEL: fil(er) i bildesamlingen er endret siden import.", stdout)
            self.assertIn("2024/01/IMG_20240102.jpg", stdout)
            self.assertIn("Avbrutt. Ingen endringer er gjort.", stdout)
            self.assertTrue(imported.exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending_file_deletes").fetchone()[0], 0)
            finally:
                conn.close()

    def test_unimport_can_continue_when_target_file_changed_after_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            imported.write_bytes(b"changed target")

            with patch("builtins.input", side_effect=["ja, det vil jeg", "ja"]):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", source.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("ADVARSEL: fil(er) i bildesamlingen er endret siden import.", stdout)
            self.assertIn("Unimport gjennomført.", stdout)
            self.assertFalse(imported.exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
            finally:
                conn.close()

    def test_unimport_dry_run_reports_changed_target_file_without_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            imported.write_bytes(b"changed target")
            report_path = root / "target-change-report.json"

            with patch("builtins.input", side_effect=AssertionError("dry-run should not ask")):
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "unimport",
                        "--dry-run",
                        "--name",
                        source.name,
                        "--target-change-report-json",
                        str(report_path),
                    ]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("ADVARSEL: fil(er) i bildesamlingen er endret siden import.", stdout)
            self.assertTrue(imported.exists())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["changed_targets"][0]["path"], "2024/01/IMG_20240102.jpg")

    def test_unimport_target_warning_ignores_files_kept_by_other_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source1 = root / "source1"
            source2 = root / "source2"
            source1.mkdir()
            source2.mkdir()
            (source1 / "IMG_20240102.jpg").write_bytes(b"same")
            (source2 / "COPY_20240203.jpg").write_bytes(b"same")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source1.name, "--quiet", str(source1)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source2.name, "--quiet", str(source2)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            imported.write_bytes(b"changed shared target")

            with patch("builtins.input", side_effect=AssertionError("dry-run should not ask")):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--dry-run", "--name", source2.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertNotIn("ADVARSEL: fil(er)", stdout)
            self.assertTrue(imported.exists())

    def test_unimport_overlapping_child_source_removes_only_child_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2006"
            child.mkdir(parents=True)
            (child / "IMG_20061003.jpg").write_bytes(b"child")
            (parent / "IMG_20070104.jpg").write_bytes(b"parent")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", child.name, "--quiet", str(child)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", parent.name, "--quiet", str(parent)]), 0)

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", child.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Unimport gjennomført.", stdout)
            self.assertTrue((target / "2006" / "10" / "IMG_20061003.jpg").exists())

    def test_unimport_named_source_requires_name_and_rejects_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            removable = root / "removable"
            removable.mkdir()
            (removable / "REM_20240203.jpg").write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", "usb-test", str(removable)]),
                0,
            )

            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(["--target", str(target), "unimport", str(removable)])
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(["--target", str(target), "unimport"])
            self.assertTrue((target / "2024" / "02" / "REM_20240203.jpg").exists())

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", "usb-test"]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Unimport gjennomført.", stdout)
            self.assertNotIn("Kilden er satt tilbake til pending.", stdout)
            self.assertIn("Kilden er fjernet fra kildelisten.", stdout)
            self.assertFalse((target / "2024" / "02" / "REM_20240203.jpg").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
            finally:
                conn.close()

    def test_unimport_dry_run_reports_removable_plan_without_changes_or_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            removable = root / "removable"
            removable.mkdir()
            (removable / "REM_20240203.jpg").write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", "usb-test", str(removable)]),
                0,
            )
            imported = target / "2024" / "02" / "REM_20240203.jpg"

            with patch("builtins.input", side_effect=AssertionError("dry-run should not ask")):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--dry-run", "--name", "usb-test"]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Filer som fjernes fra aktiv samling: 1", stdout)
            self.assertIn("Kilden ville blitt fjernet fra kildelisten.", stdout)
            self.assertIn("Dry-run: ingen endringer er gjort.", stdout)
            self.assertTrue(imported.exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                commands = [
                    row[0]
                    for row in conn.execute("SELECT command FROM command_log ORDER BY id").fetchall()
                ]
                self.assertEqual(commands, ["create", "import"])
            finally:
                conn.close()

    def test_unimport_named_source_missing_source_file_explains_media_may_be_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            removable = root / "removable"
            removable.mkdir()
            source_file = removable / "REM_20240203.jpg"
            source_file.write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", "usb-test", str(removable)]),
                0,
            )
            source_file.unlink()

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", "usb-test"]
            )

            self.assertEqual(code, 1)
            self.assertIn("Unimport: kontrollerer 1 filer i kilden.", stdout)
            self.assertIn("Originalfil mangler", stderr)
            self.assertIn("Sjekk at riktig mappe, USB-disk", stderr)
            self.assertTrue((target / "2024" / "02" / "REM_20240203.jpg").exists())
