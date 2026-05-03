from __future__ import annotations

import sqlite3
import tempfile
import unittest
import datetime as dt
import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bilder.cli import main
from bilder.importer import safe_copy
from bilder.db import DB_FILENAME
from bilder.media import sha256_file
from tests.test_media import (
    jpeg_with_xmp_date,
    minimal_avi_with_creation_date,
    minimal_avi_with_idit_outside_info,
    minimal_mp4_with_creation_date,
    minimal_png,
)


def run_cli(args: list[str]) -> int:
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return main(args)


def capture_cli(args: list[str]) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(args)
    return code, stdout.getvalue(), stderr.getvalue()


class CliTests(unittest.TestCase):
    def test_target_add_and_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertTrue((target / DB_FILENAME).exists())
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            self.assertTrue(imported.exists())
            self.assertEqual(imported.read_bytes(), b"image-one")

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM duplicate_findings").fetchone()[0], 0
                )
            finally:
                conn.close()

    def test_add_accepts_path_with_accidental_trailing_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "add", str(source) + '"']),
                0,
            )

    def test_show_source_displays_origin_for_imported_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image-one")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(["--target", str(target), "show-source", str(imported)])

            self.assertEqual(code, 0, stderr)
            self.assertIn(f"Målfil: {imported.resolve()}", stdout)
            self.assertIn(f"Kildefil: {source_file.resolve()}", stdout)
            self.assertIn("Kildefil finnes: ja", stdout)
            self.assertIn("Kilde-id: 1", stdout)
            self.assertIn("Kildetype: directory", stdout)
            self.assertIn(f"Kilde: {source.resolve()}", stdout)
            self.assertIn("Originalt filnavn: IMG_20240102.jpg", stdout)
            self.assertIn("Lagret filnavn: IMG_20240102.jpg", stdout)
            self.assertIn("Dato: 2024-01-02 (filename)", stdout)
            self.assertIn("SHA-256:", stdout)

    def test_delete_moves_file_marks_database_and_hides_from_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "delete", "2024/01/IMG_20240102.jpg"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Flyttet til slettet mappe", stdout)
            self.assertFalse(imported.exists())
            self.assertTrue(deleted.exists())
            self.assertEqual(deleted.read_bytes(), b"image-one")

            conn = sqlite3.connect(target / DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT * FROM files").fetchone()
                self.assertEqual(Path(row["target_path"]), deleted.resolve())
                self.assertEqual(Path(row["deleted_original_target_path"]), imported.resolve())
                self.assertIsNotNone(row["deleted_at"])
            finally:
                conn.close()

            self.assertEqual(run_cli(["--target", str(target), "export-html"]), 0)
            html = (target / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("IMG_20240102.jpg", html)

    def test_import_dry_run_lists_files_without_database_or_copy_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image-one")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--dry-run", "--quiet"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("IMPORT\t2024-01-02\tfilename", stdout)
            self.assertIn(str(source_file.resolve()), stdout)
            self.assertIn(str((target / "2024" / "01" / "IMG_20240102.jpg").resolve()), stdout)
            self.assertIn("importert=1", stdout)
            self.assertFalse((target / "2024").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
                self.assertIsNone(conn.execute("SELECT imported_at FROM sources").fetchone()[0])
                commands_after = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
                self.assertEqual(commands_after, commands_before)
            finally:
                conn.close()

    def test_import_dry_run_log_file_writes_list_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            log_file = root / "dry-run.txt"

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "import",
                    "--dry-run",
                    "--quiet",
                    "--log-file",
                    str(log_file),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev dry-run importliste", stdout)
            self.assertNotIn("IMG_20240102.jpg\t->", stdout)
            content = log_file.read_text(encoding="utf-8")
            self.assertIn("IMPORT\t2024-01-02\tfilename", content)
            self.assertIn("IMG_20240102.jpg", content)

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

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source1)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source2)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM duplicate_findings").fetchone()[0], 1
                )
            finally:
                conn.close()

    def test_status_counts_media_types_and_date_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Totalt: 2", stdout)
            self.assertIn("Bilder: 1", stdout)
            self.assertIn("Videoer: 1", stdout)
            self.assertIn("  metadata: 1", stdout)
            self.assertIn("  filename: 1", stdout)
            self.assertIn("  mtime: 0", stdout)

    def test_parent_source_supersedes_imported_child_without_duplicate_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2006"
            child.mkdir(parents=True)
            (child / "IMG_20061003.jpg").write_bytes(b"child")
            (parent / "IMG_20070104.jpg").write_bytes(b"parent")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(child)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(parent)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "import", "--quiet"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("dekket=1", stdout)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM duplicate_findings").fetchone()[0], 0
                )
                statuses = conn.execute(
                    "SELECT path, status, superseded_by_source_id FROM sources ORDER BY id"
                ).fetchall()
                self.assertEqual(statuses[0][1], "superseded")
                self.assertEqual(statuses[0][2], 2)
                self.assertEqual(statuses[1][1], "imported")
            finally:
                conn.close()

    def test_rejects_child_source_after_parent_is_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2007"
            child.mkdir(parents=True)

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(parent)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "add", str(child)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("allerede registrert kildemappe", stderr)

    def test_rejects_superseded_child_source_added_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2006"
            child.mkdir(parents=True)
            (child / "IMG_20061003.jpg").write_bytes(b"child")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(child)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(parent)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "add", str(child)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Kildemappen er allerede registrert", stderr)

    def test_name_conflict_gets_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            (source / "a" / "IMG_20240102.jpg").write_bytes(b"first")
            (source / "b" / "IMG_20240102.jpg").write_bytes(b"second")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            self.assertTrue((target / "2024" / "01" / "IMG_20240102.jpg").exists())
            self.assertTrue((target / "2024" / "01" / "IMG_20240102-1.jpg").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM files WHERE name_conflict = 1").fetchone()[0],
                    1,
                )
            finally:
                conn.close()

    def test_show_name_conflict_sources_lists_all_files_in_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            first_source = source / "a" / "IMG_20240102.png"
            second_source = source / "b" / "IMG_20240102.png"
            first_source.write_bytes(minimal_png(640, 480))
            second_source.write_bytes(minimal_png(320, 240))

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            first_target = target / "2024" / "01" / "IMG_20240102.png"
            second_target = target / "2024" / "01" / "IMG_20240102-1.png"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "show-name-conflict", str(second_target)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Navnekollisjon: IMG_20240102.png", stdout)
            self.assertIn(str(first_target.resolve()), stdout)
            self.assertIn(str(second_target.resolve()), stdout)
            self.assertIn(str(first_source.resolve()), stdout)
            self.assertIn(str(second_source.resolve()), stdout)
            self.assertIn("oppløsning: 640x480", stdout)
            self.assertIn("oppløsning: 320x240", stdout)
            self.assertIn("dato: 2024-01-02 (filename)", stdout)
            self.assertIn("sha256:", stdout)
            self.assertIn("kildefil finnes: ja", stdout)

    def test_show_name_conflict_sources_works_for_first_file_in_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            (source / "a" / "IMG_20240102.jpg").write_bytes(b"first")
            (source / "b" / "IMG_20240102.jpg").write_bytes(b"second")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            first_target = target / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "show-name-conflict", str(first_target)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("IMG_20240102-1.jpg", stdout)

    def test_show_name_conflict_sources_reports_non_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"one")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            target_file = target / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "show-name-conflict", str(target_file)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("ikke del av en navnekollisjon", stdout)

    def test_exiftool_metadata_gaps_lists_dates_bdb_does_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            exiftool = target / "exiftool.exe"
            exiftool.write_text(
                """#!/usr/bin/env python3
import json
print(json.dumps([{"SourceFile": "x", "DateTimeOriginal": "2024:01:02 03:04:05"}]))
""",
                encoding="utf-8",
                newline="\n",
            )
            exiftool.chmod(0o755)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "exiftool-metadata-gaps"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("2024-01-02\tDateTimeOriginal", stdout)
            self.assertIn("bdb=filename:2024-01-02", stdout)
            self.assertIn("IMG_20240102.jpg", stdout)
            self.assertIn("Oppsummering: exiftool_metadata_funnet=1", stdout)
            self.assertIn("exiftool 1/1:", stderr)

    def test_rejects_target_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = source / "target"
            source.mkdir()
            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 1)

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

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)

            with patch("bilder.importer.os.walk", fake_walk):
                self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 2)

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

    def test_import_recovers_file_copied_before_database_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"already-copied")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)

            recovered = target / "2024" / "01" / "IMG_20240102.jpg"
            recovered.parent.mkdir(parents=True)
            recovered.write_bytes(b"already-copied")

            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertFalse((target / "2024" / "01" / "IMG_20240102-1.jpg").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
            finally:
                conn.close()

    def test_import_removable_only_imports_that_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            normal = root / "normal"
            removable = root / "removable"
            normal.mkdir()
            removable.mkdir()
            (normal / "NORMAL_20240102.jpg").write_bytes(b"normal")
            (removable / "REM_20240203.jpg").write_bytes(b"removable")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(normal)]), 0)
            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "import-removable",
                        "--name",
                        "usb-test",
                        str(removable),
                    ]
                ),
                0,
            )

            self.assertFalse((target / "2024" / "01" / "NORMAL_20240102.jpg").exists())
            self.assertTrue((target / "2024" / "02" / "REM_20240203.jpg").exists())

    def test_import_video_uses_mp4_metadata_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            self.assertTrue((target / "2010" / "07" / "video.mp4").exists())

    def test_import_avi_uses_metadata_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "oktnov07 063.avi").write_bytes(
                minimal_avi_with_creation_date(dt.date(2007, 10, 31))
            )

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            self.assertTrue((target / "2007" / "10" / "oktnov07 063.avi").exists())

    def test_non_metadata_lists_files_not_placed_by_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))
            (source / "IMG_20240102.jpg").write_bytes(b"filename-date")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "non-metadata"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("filename\t2024-01-02", stdout)
            self.assertIn("IMG_20240102.jpg", stdout)
            self.assertNotIn("video.mp4", stdout)

    def test_explain_date_shows_selected_date_and_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "IMG_20240102.jpg"
            path.write_bytes(b"not-a-real-jpeg")

            code, stdout, stderr = capture_cli(["explain-date", str(path)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Valgt dato: 2024-01-02", stdout)
            self.assertIn("Valgt kilde: filename", stdout)
            self.assertIn("JPEG EXIF", stdout)
            self.assertIn("Dato i filnavn", stdout)

    def test_inspect_metadata_shows_metadata_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "xmp-only.jpg"
            path.write_bytes(jpeg_with_xmp_date("2007-03-12T19:54:18+01:00"))

            code, stdout, stderr = capture_cli(["inspect-metadata", str(path)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("JPEG metadata:", stdout)
            self.assertIn("APP1", stdout)
            self.assertIn("XMP dato: 2007-03-12", stdout)

    def test_refresh_metadata_moves_non_metadata_file_when_metadata_becomes_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "video.avi"
            source_file.write_bytes(b"RIFF\x04\x00\x00\x00AVI ")
            old_time = dt.datetime(2008, 2, 29, 12, 0).timestamp()
            os.utime(source_file, (old_time, old_time))

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            old_target = target / "2008" / "02" / "video.avi"
            new_target = target / "2007" / "03" / "video.avi"
            self.assertTrue(old_target.exists())

            old_target.write_bytes(minimal_avi_with_idit_outside_info())

            code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("flyttet=1", stdout)
            self.assertFalse(old_target.exists())
            self.assertTrue(new_target.exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute(
                    "SELECT target_path, taken_date, date_source FROM files"
                ).fetchone()
                self.assertEqual(row[0], str(new_target.resolve()))
                self.assertEqual(row[1], "2007-03-12")
                self.assertEqual(row[2], "metadata")
            finally:
                conn.close()

    def test_errors_lists_recorded_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            missing = root / "missing-source"

            self.assertEqual(run_cli(["target", str(target)]), 0)
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

            code, stdout, stderr = capture_cli(["--target", str(target), "errors", "--all"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Løst feil", stdout)

    def test_refresh_metadata_verbose_prints_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"filename-date")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            imported.unlink()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "refresh-metadata", "--verbose"]
            )

            self.assertEqual(code, 2, stderr)
            self.assertIn("FEIL", stdout)
            self.assertIn("Målfil finnes ikke", stdout)

    def test_refresh_metadata_repairs_missing_target_path_and_resolves_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            old_target = target / "2008" / "02" / "video.avi"
            repaired_target = target / "2007" / "03" / "video.avi"

            self.assertEqual(run_cli(["target", str(target)]), 0)
            repaired_target.parent.mkdir(parents=True)
            repaired_target.write_bytes(minimal_avi_with_idit_outside_info())
            file_hash = sha256_file(repaired_target)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                source_id = conn.execute(
                    "insert into sources(kind, path, path_key) values('directory', ?, ?) returning id",
                    (str(root / "source"), str(root / "source")),
                ).fetchone()[0]
                conn.execute(
                    """
                    insert into files(
                        source_id, source_path, source_path_key, target_path, target_path_key,
                        original_filename, stored_filename, sha256, size_bytes, taken_date,
                        date_source, name_conflict
                    ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        str(root / "source" / "video.avi"),
                        str(root / "source" / "video.avi"),
                        str(old_target),
                        str(old_target),
                        "video.avi",
                        "video.avi",
                        file_hash,
                        repaired_target.stat().st_size,
                        "2008-02-29",
                        "mtime",
                        0,
                    ),
                )
                conn.execute(
                    "insert into errors(stage, source_path, message) values(?, ?, ?)",
                    ("refresh-metadata", str(old_target), "Målfil finnes ikke"),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "refresh-metadata", "--verbose"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("REPARERER_DB_PATH", stdout)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute(
                    "select target_path, taken_date, date_source from files"
                ).fetchone()
                self.assertEqual(row[0], str(repaired_target.resolve()))
                self.assertEqual(row[1], "2007-03-12")
                self.assertEqual(row[2], "metadata")
                unresolved = conn.execute(
                    "select count(*) from errors where resolved_at is null"
                ).fetchone()[0]
                self.assertEqual(unresolved, 0)
            finally:
                conn.close()

    def test_export_html_writes_index_with_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG 20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "export-html"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser", stdout)
            html = (target / "index.html").read_text(encoding="utf-8")
            self.assertIn('"path": "2024/01/IMG 20240102.jpg"', html)
            self.assertIn('"url": "2024/01/IMG%2020240102.jpg"', html)
            self.assertIn('"sizeText": "9 bytes"', html)
            self.assertIn("item.sizeText", html)
            self.assertIn('const THUMB_LIMIT = 40;', html)
            self.assertIn('state.viewMode = "month";', html)
            self.assertIn("function representativeItems(items, limit)", html)
            self.assertIn('img.loading = "lazy";', html)

    def test_export_html_filters_by_media_and_date_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "export-html",
                    "--media",
                    "video",
                    "--date-source",
                    "metadata",
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser", stdout)
            html = (target / "index.html").read_text(encoding="utf-8")
            self.assertIn('"path": "2010/07/video.mp4"', html)
            self.assertNotIn("IMG_20240102.jpg", html)

    def test_export_html_conlict_writes_conflict_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            (source / "a" / "IMG_20240102.png").write_bytes(minimal_png(640, 480))
            (source / "b" / "IMG_20240102.png").write_bytes(minimal_png(320, 240))

            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "export-html-conflict"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser for navnekollisjoner", stdout)
            html = (target / "name-conflicts.html").read_text(encoding="utf-8")
            self.assertIn("<title>Navnekollisjoner</title>", html)
            self.assertIn('"originalFilename": "IMG_20240102.png"', html)
            self.assertIn('"storedFilename": "IMG_20240102-1.png"', html)
            self.assertIn('"dimensions": "640x480"', html)
            self.assertIn('"dimensions": "320x240"', html)
            self.assertIn('"sourceExists": true', html)

    def test_report_migrates_old_errors_table_without_resolved_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.executescript(
                    """
                    create table meta (key text primary key, value text not null);
                    create table sources (
                        id integer primary key autoincrement,
                        kind text not null,
                        path text not null,
                        path_key text,
                        name text,
                        added_at text not null default current_timestamp,
                        imported_at text,
                        status text not null default 'pending'
                    );
                    create table files (
                        id integer primary key autoincrement,
                        source_id integer not null,
                        source_path text not null,
                        source_path_key text not null,
                        target_path text not null,
                        target_path_key text not null unique,
                        original_filename text not null,
                        stored_filename text not null,
                        sha256 text not null,
                        size_bytes integer not null,
                        taken_date text,
                        date_source text not null,
                        name_conflict integer not null default 0,
                        imported_at text not null default current_timestamp
                    );
                    create table duplicate_findings (
                        id integer primary key autoincrement,
                        source_id integer not null,
                        source_path text not null,
                        source_path_key text not null,
                        matched_file_id integer not null,
                        sha256 text not null,
                        found_at text not null default current_timestamp
                    );
                    create table errors (
                        id integer primary key autoincrement,
                        source_id integer,
                        source_path text,
                        stage text not null,
                        message text not null,
                        created_at text not null default current_timestamp
                    );
                    insert into errors(stage, message) values('test', 'old error');
                    """
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "report"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Uløste feil: 1", stdout)


if __name__ == "__main__":
    unittest.main()
