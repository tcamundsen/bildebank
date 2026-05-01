from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from bilder.cli import main
from bilder.db import DB_FILENAME


def run_cli(args: list[str]) -> int:
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return main(args)


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


if __name__ == "__main__":
    unittest.main()
