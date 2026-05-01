from __future__ import annotations

import sqlite3
import tempfile
import unittest
import datetime as dt
import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from bilder.cli import main
from bilder.db import DB_FILENAME
from bilder.media import sha256_file
from tests.test_media import (
    minimal_avi_with_creation_date,
    minimal_avi_with_idit_outside_info,
    minimal_mp4_with_creation_date,
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

    def test_rejects_target_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = source / "target"
            source.mkdir()
            self.assertEqual(run_cli(["target", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 1)

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


if __name__ == "__main__":
    unittest.main()
