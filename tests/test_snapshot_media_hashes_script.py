from __future__ import annotations

import csv
import codecs
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


POWERSHELL = shutil.which("pwsh") or shutil.which("powershell") or shutil.which("powershell.exe")
SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "snapshot-media-hashes.ps1"


class SnapshotMediaHashesSourceTests(unittest.TestCase):
    def test_script_has_utf8_bom_for_windows_powershell(self) -> None:
        self.assertTrue(SCRIPT.read_bytes().startswith(codecs.BOM_UTF8))


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Testen krever Windows og PowerShell")
class SnapshotMediaHashesScriptTests(unittest.TestCase):
    def test_resolves_relative_output_from_powershell_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process_directory = root / "process-directory"
            powershell_directory = root / "powershell-directory"
            process_directory.mkdir()
            powershell_directory.mkdir()
            write_file(powershell_directory / "collection" / "bilde.jpg", b"image")

            quoted_script = str(SCRIPT).replace("'", "''")
            wrapper = powershell_directory / "run.ps1"
            wrapper.write_text(
                "$ErrorActionPreference = 'Stop'\n"
                "Set-Location -LiteralPath $PSScriptRoot\n"
                f"& '{quoted_script}' -Collection '.\\collection' -Output '.\\expected.csv'\n",
                encoding="utf-8-sig",
            )

            result = subprocess.run(
                powershell_command() + ["-File", str(wrapper)],
                cwd=process_directory,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((powershell_directory / "expected.csv").is_file())
            self.assertFalse((process_directory / "expected.csv").exists())

    def test_creates_media_list_and_compares_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collection = root / "collection"
            restored = root / "restored"
            expected_csv = root / "expected.csv"
            actual_csv = root / "actual.csv"

            write_file(collection / "2026" / "07" / "bilde med æ.jpg", b"image")
            write_file(collection / "deleted" / "gammelt.nef", b"raw")
            write_file(collection / "ukjent.txt", b"not media")
            write_file(collection / "thumbs" / "2026" / "07" / "bilde med æ.jpg", b"thumb")

            created = run_script(collection, expected_csv)
            self.assertEqual(created.returncode, 0, created.stderr)
            refused_overwrite = run_script(collection, expected_csv)
            self.assertNotEqual(refused_overwrite.returncode, 0)
            with expected_csv.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(
                rows,
                [
                    {
                        "path": "2026/07/bilde med æ.jpg",
                        "size_bytes": "5",
                        "sha256": hashlib.sha256(b"image").hexdigest(),
                    },
                    {
                        "path": "deleted/gammelt.nef",
                        "size_bytes": "3",
                        "sha256": hashlib.sha256(b"raw").hexdigest(),
                    },
                ],
            )

            shutil.copytree(collection, restored)
            compared = run_script(restored, actual_csv, compare_with=expected_csv)
            self.assertEqual(compared.returncode, 0, compared.stderr)

            (restored / "2026" / "07" / "bilde med æ.jpg").write_bytes(b"changed")
            changed_csv = root / "changed.csv"
            changed = run_script(restored, changed_csv, compare_with=expected_csv)
            self.assertEqual(changed.returncode, 1, changed.stderr)


def write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def run_script(
    collection: Path,
    output: Path,
    *,
    compare_with: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = powershell_command() + [
        "-File",
        str(SCRIPT),
        "-Collection",
        str(collection),
        "-Output",
        str(output),
    ]
    if compare_with is not None:
        command.extend(["-CompareWith", str(compare_with)])
    return subprocess.run(command, capture_output=True, check=False)


def powershell_command() -> list[str]:
    return [
        str(POWERSHELL),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
    ]
