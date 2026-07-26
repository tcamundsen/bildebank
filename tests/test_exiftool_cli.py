from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank import exiftool
from bildebank.cli import main
from bildebank.exiftool import managed_exiftool_path, resolve_exiftool_path
from bildebank.exiftool_probe import (
    EXIFTOOL_TIMEOUT_SECONDS,
    exiftool_dates_batch,
)
from tests.cli_helpers import capture_cli, run_cli, write_fake_exiftool


def write_exiftool_archive(path: Path, *, version: str = "13.58") -> None:
    script = f"""#!/usr/bin/env python3
import sys
if "-ver" in sys.argv:
    print("{version}")
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"exiftool-{version}_64/exiftool(-k).exe",
            script,
        )
        archive.writestr(
            f"exiftool-{version}_64/exiftool_files/ExifTool_config",
            "config",
        )


class ExiftoolCliTests(unittest.TestCase):
    def test_exiftool_metadata_timeout_is_reported(self) -> None:
        with (
            patch(
                "bildebank.exiftool_probe.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    ["exiftool"],
                    EXIFTOOL_TIMEOUT_SECONDS,
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "brukte mer enn"),
        ):
            exiftool_dates_batch("exiftool", [Path("image.jpg")])

    def test_exiftool_archive_is_version_and_hash_pinned(self) -> None:
        self.assertEqual(exiftool.EXIFTOOL_VERSION, "13.58")
        self.assertTrue(
            exiftool.EXIFTOOL_ZIP_URL.endswith(
                "/exiftool-13.58_64.zip/download"
            )
        )
        self.assertEqual(
            exiftool.EXIFTOOL_ARCHIVE_SHA256,
            "fd3b407a01e6ffc6160f2d5fde5ff0c003f6c4c2ba85eee1ce8928ccb51fa3e6",
        )

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

    def test_exiftool_validation_can_require_exact_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "exiftool.exe"
            write_fake_exiftool(tool)
            (tool.parent / "exiftool_files").mkdir()

            with (
                patch.object(exiftool, "exiftool_version", return_value="13.57"),
                self.assertRaisesRegex(RuntimeError, "forventet '13.58'"),
            ):
                exiftool.validate_exiftool_install(
                    tool,
                    expected_version="13.58",
                )

    def test_exiftool_install_downloads_zip_to_managed_tools_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            source_zip = root / "exiftool.zip"
            write_exiftool_archive(source_zip)
            expected_hash = hashlib.sha256(source_zip.read_bytes()).hexdigest()

            def fake_download(_url: str, destination: Path) -> None:
                shutil.copyfile(source_zip, destination)

            with (
                patch("bildebank.cli.sys.platform", "win32"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch.object(exiftool, "EXIFTOOL_ARCHIVE_SHA256", expected_hash),
                patch.object(exiftool, "_download_file", side_effect=fake_download),
                patch("bildebank.exiftool.validate_exiftool_install", return_value="13.58"),
            ):
                code, stdout, stderr = capture_cli(["exiftool-install"])

            installed = repo / "bildebank-tools" / "exiftool"
            self.assertEqual(code, 0, stderr)
            self.assertIn("Installerte ExifTool 13.58", stdout)
            self.assertTrue((installed / "exiftool.exe").exists())
            self.assertTrue((installed / "exiftool_files").is_dir())

    def test_exiftool_install_rejects_wrong_hash_and_preserves_existing_install(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            destination = exiftool.managed_exiftool_dir(repo)
            destination.mkdir(parents=True)
            existing = destination / "existing.txt"
            existing.write_text("behold", encoding="utf-8")

            with patch.object(
                exiftool,
                "_download_file",
                side_effect=lambda _url, path: path.write_bytes(b"wrong"),
            ):
                with self.assertRaisesRegex(RuntimeError, "feil SHA-256"):
                    exiftool.install_managed_exiftool(repo, force=True)

            self.assertEqual(existing.read_text(encoding="utf-8"), "behold")

    def test_exiftool_install_preserves_existing_install_when_staging_is_invalid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            destination = exiftool.managed_exiftool_dir(repo)
            destination.mkdir(parents=True)
            existing = destination / "existing.txt"
            existing.write_text("behold", encoding="utf-8")
            archive = root / "exiftool.zip"
            write_exiftool_archive(archive)
            expected_hash = hashlib.sha256(archive.read_bytes()).hexdigest()

            with (
                patch.object(exiftool, "EXIFTOOL_ARCHIVE_SHA256", expected_hash),
                patch.object(
                    exiftool,
                    "_download_file",
                    side_effect=lambda _url, path: shutil.copyfile(archive, path),
                ),
                patch.object(
                    exiftool,
                    "validate_exiftool_install",
                    side_effect=RuntimeError("ugyldig staging"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "ugyldig staging"):
                    exiftool.install_managed_exiftool(repo, force=True)

            self.assertEqual(existing.read_text(encoding="utf-8"), "behold")
            self.assertEqual(
                list(destination.parent.glob(".exiftool.installing-*")),
                [],
            )

    def test_exiftool_install_rolls_back_on_interrupt_during_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            destination = exiftool.managed_exiftool_dir(repo)
            destination.mkdir(parents=True)
            existing = destination / "existing.txt"
            existing.write_text("behold", encoding="utf-8")
            archive = root / "exiftool.zip"
            write_exiftool_archive(archive)
            expected_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
            original_rename = Path.rename

            def interrupt_staging_publication(path: Path, target: Path) -> Path:
                if (
                    path.name.startswith(".exiftool.installing-")
                    and Path(target) == destination
                ):
                    raise KeyboardInterrupt
                return original_rename(path, target)

            with (
                patch.object(exiftool, "EXIFTOOL_ARCHIVE_SHA256", expected_hash),
                patch.object(
                    exiftool,
                    "_download_file",
                    side_effect=lambda _url, path: shutil.copyfile(archive, path),
                ),
                patch.object(
                    exiftool,
                    "validate_exiftool_install",
                    return_value=exiftool.EXIFTOOL_VERSION,
                ),
                patch.object(Path, "rename", autospec=True, side_effect=interrupt_staging_publication),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    exiftool.install_managed_exiftool(repo, force=True)

            self.assertEqual(existing.read_text(encoding="utf-8"), "behold")
            self.assertEqual(
                list(destination.parent.glob(".exiftool.installing-*")),
                [],
            )
            self.assertEqual(
                list(destination.parent.glob(".exiftool.previous-*")),
                [],
            )

    def test_exiftool_install_rejects_link_as_managed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            destination = exiftool.managed_exiftool_dir(repo)
            destination.mkdir(parents=True)

            with (
                patch.object(exiftool, "_is_directory_link", return_value=True),
                self.assertRaisesRegex(RuntimeError, "kan ikke være en lenke"),
            ):
                exiftool.install_managed_exiftool(repo, force=True)

    def test_exiftool_safe_extract_rejects_parent_path_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extract = root / "extract"
            extract.mkdir()
            parent_archive = root / "parent.zip"
            with zipfile.ZipFile(parent_archive, "w") as archive:
                archive.writestr("../exiftool.exe", b"bad")

            with self.assertRaisesRegex(RuntimeError, "utrygg filsti"):
                exiftool._safe_extract_zip(parent_archive, extract)

            symlink_archive = root / "symlink.zip"
            link = zipfile.ZipInfo("exiftool-13.58_64/exiftool.exe")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(symlink_archive, "w") as archive:
                archive.writestr(link, "target")

            with self.assertRaisesRegex(RuntimeError, "symbolsk lenke"):
                exiftool._safe_extract_zip(symlink_archive, extract)

            drive_archive = root / "drive.zip"
            with zipfile.ZipFile(drive_archive, "w") as archive:
                archive.writestr("C:/outside/exiftool.exe", b"bad")

            with self.assertRaisesRegex(RuntimeError, "utrygg filsti"):
                exiftool._safe_extract_zip(drive_archive, extract)

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

    def test_exiftool_metadata_gaps_rejects_linked_collection_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image")
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
            imported = target / "2024" / "01" / source_file.name
            external = root / "external.jpg"
            external.write_bytes(b"private")
            imported.unlink()
            try:
                imported.symlink_to(external)
            except OSError as exc:
                self.skipTest(f"Kan ikke opprette symlink: {exc}")

            with patch(
                "bildebank.exiftool_probe.subprocess.run",
                side_effect=AssertionError("ExifTool skal ikke startes"),
            ):
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "exiftool-metadata-gaps",
                        "--exiftool",
                        str(root / "exiftool.exe"),
                    ]
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("vanlig fil uten lenker", stderr)

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
