from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank.cli import build_parser
from bildebank.db import DB_FILENAME
from bildebank.importer import safe_copy
from bildebank.media import sha256_file
from bildebank.target_lock import LOCK_FILENAME
from tests.test_cli import (
    capture_cli,
    run_cli,
)
from tests.test_media import (
    jpeg_with_exif_datetime,
    minimal_avi_with_creation_date,
    minimal_mp4_with_creation_date,
    minimal_tiff_with_datetime,
)


class ImportCliTests(unittest.TestCase):

    def test_import_requires_name(self) -> None:
        for args in (["import", "."], ["import", "--dry-run", "."]):
            with self.subTest(args=args):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    build_parser().parse_args(args)

    def test_target_add_and_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertTrue((target / DB_FILENAME).exists())
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            self.assertTrue(imported.exists())
            self.assertEqual(imported.read_bytes(), b"image-one")

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                    "14",
                )
                file_columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
                self.assertNotIn("source_id", file_columns)
                self.assertNotIn("source_path", file_columns)
                self.assertNotIn("source_path_key", file_columns)
                source_columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
                file_source_columns = {row[1] for row in conn.execute("PRAGMA table_info(file_sources)")}
                self.assertNotIn("kind", source_columns)
                self.assertNotIn("superseded_by_source_id", source_columns)
                self.assertNotIn("kind", file_source_columns)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources WHERE name IS NULL").fetchone()[0], 0)
            finally:
                conn.close()

    def test_import_accepts_raw_nef_and_psd_archive_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.raw").write_bytes(b"raw")
            (source / "IMG_20240103.nef").write_bytes(b"nef")
            (source / "edited_20240104.psd").write_bytes(b"psd")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertEqual((target / "2024" / "01" / "IMG_20240102.raw").read_bytes(), b"raw")
            self.assertEqual((target / "2024" / "01" / "IMG_20240103.nef").read_bytes(), b"nef")
            self.assertEqual((target / "2024" / "01" / "edited_20240104.psd").read_bytes(), b"psd")

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 3)
            finally:
                conn.close()

    def test_import_accepts_path_with_accidental_trailing_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, str(source) + '"']),
                0,
            )

    def test_import_dry_run_lists_files_without_database_or_copy_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", source.name, "--dry-run", "--quiet", str(source)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertNotIn("IMPORT\t", stdout)
            self.assertNotIn(str(source_file.resolve()), stdout)
            self.assertNotIn(str((target / "2024" / "01" / "IMG_20240102.jpg").resolve()), stdout)
            self.assertIn("importert=1", stdout)
            self.assertFalse((target / "2024").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
                commands_after = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
                self.assertEqual(commands_after, commands_before)
            finally:
                conn.close()

    def test_import_stops_when_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=import\npid=123\n", encoding="utf-8")

            with (
                patch("bildebank.cli.recover_pending_file_moves"),
                patch("bildebank.cli.db.connect", side_effect=AssertionError("db before lock")),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertIn(str(lock_path), stderr)
            self.assertTrue(lock_path.exists())
            self.assertFalse((target / "2024").exists())

    def test_duplicate_is_recorded_not_copied(self) -> None:
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

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 2)
                self.assertFalse(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'duplicate_findings'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_duplicate_import_verifies_active_target_file(self) -> None:
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

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", source2.name, "--quiet", str(source2)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("duplikater=1", stdout)
            self.assertFalse((target / "2024" / "02" / "COPY_20240203.jpg").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                linked_file_id = conn.execute(
                    """
                    SELECT file_id
                    FROM file_sources
                    WHERE source_path LIKE ?
                    """,
                    ("%COPY_20240203.jpg",),
                ).fetchone()[0]
                active_file_id = conn.execute(
                    "SELECT id FROM files WHERE deleted_at IS NULL"
                ).fetchone()[0]
                self.assertEqual(linked_file_id, active_file_id)
            finally:
                conn.close()

    def test_duplicate_import_links_deleted_file_without_restoring_it(self) -> None:
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
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", source2.name, "--quiet", str(source2)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("duplikater=1", stdout)
            self.assertFalse((target / "2024" / "01" / "IMG_20240102.jpg").exists())
            self.assertFalse((target / "2024" / "02" / "COPY_20240203.jpg").exists())
            self.assertTrue((target / "deleted" / "2024" / "01" / "IMG_20240102.jpg").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute("SELECT id, target_path, deleted_at FROM files").fetchone()
                self.assertIsNotNone(row[2])
                linked_file_id = conn.execute(
                    "SELECT file_id FROM file_sources WHERE source_path LIKE ?",
                    ("%COPY_20240203.jpg",),
                ).fetchone()[0]
                self.assertEqual(linked_file_id, row[0])
                self.assertEqual(row[1], "deleted/2024/01/IMG_20240102.jpg")
            finally:
                conn.close()

    def test_duplicate_import_reports_error_when_target_file_is_missing_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source1 = root / "source1"
            source2 = root / "source2"
            source1.mkdir()
            source2.mkdir()
            (source1 / "IMG_20240102.jpg").write_bytes(b"same")
            (source2 / "COPY_20240203.jpg").write_bytes(b"same")
            (source2 / "IMG_20240204.jpg").write_bytes(b"new")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source1.name, "--quiet", str(source1)]), 0)
            (target / "2024" / "01" / "IMG_20240102.jpg").unlink()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", source2.name, "--quiet", str(source2)]
            )

            self.assertEqual(code, 2, stderr)
            self.assertIn("importert=1", stdout)
            self.assertIn("feil=1", stdout)
            self.assertTrue((target / "2024" / "02" / "IMG_20240204.jpg").exists())
            self.assertFalse((target / "2024" / "02" / "COPY_20240203.jpg").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("SELECT status FROM sources WHERE name = ?", (source2.name,)).fetchone()[0],
                    "error",
                )
                error = conn.execute("SELECT message FROM errors ORDER BY id DESC LIMIT 1").fetchone()[0]
                self.assertIn("files.id=1", error)
                self.assertIn("2024/01/IMG_20240102.jpg", error)
                self.assertIn("målfilen mangler på disk", error)
                self.assertIsNone(
                    conn.execute(
                        "SELECT id FROM file_sources WHERE source_path LIKE ?",
                        ("%COPY_20240203.jpg",),
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_duplicate_import_reports_error_when_target_file_hash_differs(self) -> None:
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
            (target / "2024" / "01" / "IMG_20240102.jpg").write_bytes(b"changed")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", source2.name, "--quiet", str(source2)]
            )

            self.assertEqual(code, 2, stderr)
            self.assertIn("feil=1", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                error = conn.execute("SELECT message FROM errors ORDER BY id DESC LIMIT 1").fetchone()[0]
                self.assertIn("files.id=1", error)
                self.assertIn("SHA-256 på disk matcher ikke databaseført SHA-256", error)
                self.assertIsNone(
                    conn.execute(
                        "SELECT id FROM file_sources WHERE source_path LIKE ?",
                        ("%COPY_20240203.jpg",),
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_import_dry_run_reports_invalid_duplicate_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source1 = root / "source1"
            source2 = root / "source2"
            source1.mkdir()
            source2.mkdir()
            (source1 / "IMG_20240102.jpg").write_bytes(b"same")
            duplicate = source2 / "COPY_20240203.jpg"
            duplicate.write_bytes(b"same")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source1.name, "--quiet", str(source1)]), 0)
            (target / "2024" / "01" / "IMG_20240102.jpg").write_bytes(b"changed")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", source2.name, "--dry-run", "--quiet", str(source2)]
            )

            self.assertEqual(code, 2, stderr)
            self.assertIn(f"FEIL\t{duplicate}", stdout)
            self.assertIn("SHA-256 på disk matcher ikke databaseført SHA-256", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM errors").fetchone()[0], 0)
            finally:
                conn.close()

    def test_parent_source_after_child_import_records_duplicate_reference(self) -> None:
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
            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", parent.name, "--quiet", str(parent)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("duplikater=1", stdout)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 3)
                statuses = conn.execute("SELECT path, status FROM sources ORDER BY id").fetchall()
                self.assertEqual(statuses[0][1], "imported")
                self.assertEqual(statuses[1][1], "imported")
            finally:
                conn.close()

    def test_overlapping_child_source_after_parent_import_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2007"
            child.mkdir(parents=True)
            (parent / "IMG_20070104.jpg").write_bytes(b"parent")
            (child / "IMG_20070203.jpg").write_bytes(b"child")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", parent.name, "--quiet", str(parent)]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", child.name, "--quiet", str(child)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("duplikater=1", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 3)
            finally:
                conn.close()

    def test_overlapping_child_source_added_again_records_duplicate_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2006"
            child.mkdir(parents=True)
            (child / "IMG_20061003.jpg").write_bytes(b"child")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", child.name, "--quiet", str(child)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", parent.name, "--quiet", str(parent)]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", "child-again", "--quiet", str(child)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("duplikater=1", stdout)

    def test_name_conflict_gets_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            (source / "a" / "IMG_20240102.jpg").write_bytes(b"first")
            (source / "b" / "IMG_20240102.jpg").write_bytes(b"second")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertTrue((target / "2024" / "01" / "IMG_20240102.jpg").exists())
            self.assertTrue((target / "2024" / "01" / "IMG_20240102-1.jpg").exists())

            code, stdout, stderr = capture_cli(["--target", str(target), "conflicts"])
            self.assertEqual(code, 0, stderr)
            self.assertIn(str(target / "2024" / "01" / "IMG_20240102-1.jpg"), stdout)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM files WHERE name_conflict = 1").fetchone()[0],
                    1,
                )
            finally:
                conn.close()

    def test_rejects_target_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = source / "target"
            source.mkdir()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, str(source)]),
                1,
            )

    def test_import_records_walk_errors_and_keeps_source_pending_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            blocked = source / "blocked"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"visible")

            def fake_walk(path, *args, onerror=None, **kwargs):
                if onerror is not None:
                    onerror(PermissionError(13, "Permission denied", str(blocked)))
                yield str(path), [], ["IMG_20240102.jpg"]

            self.assertEqual(run_cli(["create", str(target)]), 0)

            with patch("bildebank.importer.os.walk", fake_walk):
                self.assertEqual(
                    run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                    2,
                )

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                error = conn.execute("SELECT stage, source_path FROM errors").fetchone()
                self.assertEqual(error[0], "scan")
                self.assertEqual(error[1], str(blocked))
                status = conn.execute("SELECT status FROM sources").fetchone()[0]
                self.assertEqual(status, "error")
            finally:
                conn.close()

    def test_safe_copy_does_not_overwrite_existing_different_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            source.write_bytes(b"new-image")
            destination.write_bytes(b"existing-image")

            with self.assertRaises(FileExistsError):
                safe_copy(source, destination, sha256_file(source))

            self.assertEqual(destination.read_bytes(), b"existing-image")

    def test_safe_copy_does_not_require_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            source.write_bytes(b"image")

            with patch("bildebank.importer.os.link", side_effect=OSError("hardlink unsupported")):
                safe_copy(source, destination, sha256_file(source))

            self.assertEqual(destination.read_bytes(), b"image")

    def test_import_recovers_file_copied_before_database_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"already-copied")

            self.assertEqual(run_cli(["create", str(target)]), 0)

            recovered = target / "2024" / "01" / "IMG_20240102.jpg"
            recovered.parent.mkdir(parents=True)
            recovered.write_bytes(b"already-copied")

            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertFalse((target / "2024" / "01" / "IMG_20240102-1.jpg").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
            finally:
                conn.close()

    def test_named_import_only_imports_that_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            normal = root / "normal"
            removable = root / "removable"
            normal.mkdir()
            removable.mkdir()
            (normal / "NORMAL_20240102.jpg").write_bytes(b"normal")
            (removable / "REM_20240203.jpg").write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", "usb-test", str(removable)]),
                0,
            )

            self.assertFalse((target / "2024" / "01" / "NORMAL_20240102.jpg").exists())
            self.assertTrue((target / "2024" / "02" / "REM_20240203.jpg").exists())

    def test_import_rejects_reused_imported_name_without_changing_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "REM_20240203.jpg").write_bytes(b"first")
            (second / "REM_20240304.jpg").write_bytes(b"second")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", "usb-test", str(first)]),
                0,
            )

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", "usb-test", str(second)]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("er allerede importert", stderr)
            self.assertIn("Bruk et nytt --name", stderr)
            self.assertFalse((target / "2024" / "03" / "REM_20240304.jpg").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                rows = conn.execute("SELECT path FROM sources WHERE name = 'usb-test'").fetchall()
                self.assertEqual(rows, [(str(first.resolve()),)])
            finally:
                conn.close()

    def test_import_dry_run_does_not_register_or_copy_named_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            removable = root / "removable"
            removable.mkdir()
            removable_file = removable / "REM_20240203.jpg"
            removable_file.write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "import",
                    "--name",
                    "usb-test",
                    "--dry-run",
                    str(removable),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertNotIn("IMPORT\t", stdout)
            self.assertNotIn(str(removable_file.resolve()), stdout)
            self.assertIn("importert=1", stdout)
            self.assertFalse((target / "2024" / "02" / "REM_20240203.jpg").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
                commands_after = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
                self.assertEqual(commands_after, commands_before)
            finally:
                conn.close()

    def test_import_video_uses_mp4_metadata_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertTrue((target / "2010" / "07" / "video.mp4").exists())

    def test_import_mp_motion_file_stores_copy_as_mp4_and_keeps_original_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            motion = source / "PXL_20250102_123.MP"
            motion.write_bytes(minimal_mp4_with_creation_date(dt.date(2025, 1, 2)))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertTrue((target / "2025" / "01" / "PXL_20250102_123.mp4").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute("SELECT original_filename, stored_filename FROM files").fetchone()
            finally:
                conn.close()
            self.assertEqual(row, ("PXL_20250102_123.MP", "PXL_20250102_123.mp4"))

    def test_import_mp_motion_name_conflict_uses_mp4_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            (source / "a" / "PXL_20250102_123.MP").write_bytes(
                minimal_mp4_with_creation_date(dt.date(2025, 1, 2))
            )
            (source / "b" / "PXL_20250102_123.MP").write_bytes(
                minimal_mp4_with_creation_date(dt.date(2025, 1, 3))
            )

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertTrue((target / "2025" / "01" / "PXL_20250102_123.mp4").exists())
            self.assertTrue((target / "2025" / "01" / "PXL_20250102_123-1.mp4").exists())

    def test_import_avi_uses_metadata_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "oktnov07 063.avi").write_bytes(
                minimal_avi_with_creation_date(dt.date(2007, 10, 31))
            )

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertTrue((target / "2007" / "10" / "oktnov07 063.avi").exists())

    def test_import_nef_uses_tiff_metadata_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "DSC_0170.JPG").write_bytes(jpeg_with_exif_datetime("2019:03:03 12:00:00"))
            (source / "DSC_0170.NEF").write_bytes(minimal_tiff_with_datetime("2019:03:03 12:00:00"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertTrue((target / "2019" / "03" / "DSC_0170.JPG").exists())
            self.assertTrue((target / "2019" / "03" / "DSC_0170.NEF").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                rows = conn.execute(
                    "SELECT stored_filename, taken_date, date_source, metadata_datetime FROM files ORDER BY stored_filename"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(
                rows,
                [
                    ("DSC_0170.JPG", "2019-03-03", "metadata", "2019-03-03 12:00:00"),
                    ("DSC_0170.NEF", "2019-03-03", "metadata", "2019-03-03 12:00:00"),
                ],
            )
