from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank.cli import main
from bildebank.exiftool import managed_exiftool_path, resolve_exiftool_path
from tests.cli_helpers import capture_cli, run_cli, write_fake_exiftool


class ExiftoolCliTests(unittest.TestCase):
    def test_exiftool_install_help_documents_force(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["exiftool-install", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("usage: bildebank exiftool-install [valg]", stdout)
        self.assertIn("--force", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_exiftool_resolver_prefers_explicit_path_then_managed_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            explicit = root / "custom-exiftool.exe"
            managed = managed_exiftool_path(repo)
            write_fake_exiftool(explicit)
            write_fake_exiftool(managed)
            (managed.parent / "exiftool_files").mkdir()

            with patch("bildebank.exiftool.exiftool_version", return_value="13.58"):
                self.assertEqual(resolve_exiftool_path(repo, explicit), explicit)
                self.assertEqual(resolve_exiftool_path(repo), managed)

    def test_exiftool_resolver_falls_back_to_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path_tool = root / "exiftool"
            write_fake_exiftool(path_tool)

            with (
                patch("bildebank.exiftool.shutil.which", return_value=str(path_tool)),
                patch("bildebank.exiftool.exiftool_version", return_value="13.58"),
            ):
                self.assertEqual(resolve_exiftool_path(root / "repo"), str(path_tool))

    def test_exiftool_resolver_requires_managed_support_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            write_fake_exiftool(managed_exiftool_path(repo))

            with self.assertRaisesRegex(FileNotFoundError, "exiftool_files"):
                resolve_exiftool_path(repo)

    def test_exiftool_install_downloads_zip_to_managed_tools_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            source_zip = root / "exiftool.zip"
            script = """#!/usr/bin/env python3
import sys
if "-ver" in sys.argv:
    print("13.58")
"""
            with zipfile.ZipFile(source_zip, "w") as archive:
                archive.writestr("exiftool-13.58_64/exiftool(-k).exe", script)
                archive.writestr("exiftool-13.58_64/exiftool_files/ExifTool_config", "config")

            def fake_urlretrieve(url: str, filename: str | Path):
                shutil.copyfile(source_zip, filename)
                return (str(filename), None)

            with (
                patch("bildebank.cli.sys.platform", "win32"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch("bildebank.exiftool.urllib.request.urlretrieve", side_effect=fake_urlretrieve),
                patch("bildebank.exiftool.validate_exiftool_install", return_value="13.58"),
            ):
                code, stdout, stderr = capture_cli(["exiftool-install"])

            installed = repo / "bildebank-tools" / "exiftool"
            self.assertEqual(code, 0, stderr)
            self.assertIn("Installerte ExifTool 13.58", stdout)
            self.assertTrue((installed / "exiftool.exe").exists())
            self.assertTrue((installed / "exiftool_files").is_dir())

    def test_exiftool_install_fails_on_linux(self) -> None:
        with patch("bildebank.cli.sys.platform", "linux"):
            code, stdout, stderr = capture_cli(["exiftool-install"])

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertIn("støttes bare på Windows", stderr)
        self.assertIn("libimage-exiftool-perl", stderr)

    def test_exiftool_metadata_gaps_lists_dates_bildebank_does_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            exiftool_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='[{"SourceFile": "x", "DateTimeOriginal": "2024:01:02 03:04:05"}]',
                stderr="",
            )

            with patch("bildebank.exiftool_probe.subprocess.run", return_value=exiftool_result):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "exiftool-metadata-gaps", "--exiftool", str(root / "exiftool.exe")]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("2024-01-02\tDateTimeOriginal", stdout)
            self.assertIn("bildebank=filename:2024-01-02", stdout)
            self.assertIn("IMG_20240102.jpg", stdout)
            self.assertIn("Oppsummering: exiftool_metadata_funnet=1", stdout)
            self.assertIn("exiftool: kontrollert=1/1", stderr)
            self.assertIn("gjenstår=0s", stderr)

    def test_exiftool_metadata_gaps_reads_files_in_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            for index, name in enumerate(("IMG_20240102.jpg", "IMG_20240103.jpg", "IMG_20240104.jpg")):
                (source / name).write_bytes(f"image-{index}".encode("ascii"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            def fake_exiftool(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
                paths = [argument for argument in command[1:] if not argument.startswith("-")]
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        [
                            {
                                "SourceFile": path,
                                "DateTimeOriginal": "2024:01:02 03:04:05",
                            }
                            for path in paths
                        ]
                    ),
                    stderr="",
                )

            with patch("bildebank.exiftool_probe.subprocess.run", side_effect=fake_exiftool) as run:
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "exiftool-metadata-gaps",
                        "--exiftool",
                        str(root / "exiftool.exe"),
                        "--batch-size",
                        "10",
                    ]
                )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(run.call_count, 1)
            command = run.call_args.args[0]
            command_paths = {
                Path(argument)
                for argument in command[1:]
                if not argument.startswith("-")
            }
            self.assertEqual(
                command_paths,
                {
                    target / "2024" / "01" / "IMG_20240102.jpg",
                    target / "2024" / "01" / "IMG_20240103.jpg",
                    target / "2024" / "01" / "IMG_20240104.jpg",
                },
            )
            self.assertIn("Oppsummering: exiftool_metadata_funnet=3", stdout)
