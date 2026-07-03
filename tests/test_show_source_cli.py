from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tests.test_cli import capture_cli, minimal_png, run_cli


class ShowSourceCliTests(unittest.TestCase):
    def test_show_source_displays_origin_for_imported_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(["--target", str(target), "show-source", str(imported)])

            self.assertEqual(code, 0, stderr)
            self.assertIn(f"Målfil: {imported.resolve()}", stdout)
            self.assertIn(f"Kildefil: {source_file.resolve()}", stdout)
            self.assertIn("Kildefil finnes: ja", stdout)
            self.assertIn("Kilde-id: 1", stdout)
            self.assertIn("Kilde: source", stdout)
            self.assertIn("Originalt filnavn: IMG_20240102.jpg", stdout)
            self.assertIn("Lagret filnavn: IMG_20240102.jpg", stdout)
            self.assertIn("Dato: 2024-01-02 (filename)", stdout)
            self.assertIn("SHA-256:", stdout)

    def test_show_source_resolves_relative_path_under_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            workdir = root / "workdir"
            source.mkdir()
            workdir.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            old_cwd = Path.cwd()
            try:
                os.chdir(workdir)
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "show-source", "2024/01/IMG_20240102.jpg"]
                )
            finally:
                os.chdir(old_cwd)

            self.assertEqual(code, 0, stderr)
            self.assertIn(f"Målfil: {imported.resolve()}", stdout)
            self.assertIn(f"Kildefil: {source_file.resolve()}", stdout)

    def test_show_source_lists_duplicate_sources_for_same_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source1 = root / "source1"
            source2 = root / "source2"
            source1.mkdir()
            source2.mkdir()
            first = source1 / "IMG_20240102.jpg"
            duplicate = source2 / "COPY_20240203.jpg"
            first.write_bytes(b"same")
            duplicate.write_bytes(b"same")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source1.name, "--quiet", str(source1)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source2.name, "--quiet", str(source2)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            code, stdout, stderr = capture_cli(["--target", str(target), "show-source", str(imported)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Kildefiler:", stdout)
            self.assertIn(f"- {first.resolve()}", stdout)
            self.assertIn(f"- {duplicate.resolve()}", stdout)

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

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            first_target = target / "2024" / "01" / "IMG_20240102.png"
            second_target = target / "2024" / "01" / "IMG_20240102-1.png"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "show-conflict", str(second_target)]
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

    def test_show_name_conflict_resolves_relative_path_under_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            workdir = root / "workdir"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            workdir.mkdir()
            first_source = source / "a" / "IMG_20240102.png"
            second_source = source / "b" / "IMG_20240102.png"
            first_source.write_bytes(minimal_png(640, 480))
            second_source.write_bytes(minimal_png(320, 240))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            first_target = target / "2024" / "01" / "IMG_20240102.png"
            second_target = target / "2024" / "01" / "IMG_20240102-1.png"
            old_cwd = Path.cwd()
            try:
                os.chdir(workdir)
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "show-conflict", "2024/01/IMG_20240102-1.png"]
                )
            finally:
                os.chdir(old_cwd)

            self.assertEqual(code, 0, stderr)
            self.assertIn("Navnekollisjon: IMG_20240102.png", stdout)
            self.assertIn(str(first_target.resolve()), stdout)
            self.assertIn(str(second_target.resolve()), stdout)

    def test_show_name_conflict_sources_works_for_first_file_in_conflict(self) -> None:
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

            first_target = target / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "show-conflict", str(first_target)]
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

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            target_file = target / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "show-conflict", str(target_file)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("ikke del av en navnekollisjon", stdout)

