from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_cli import capture_cli


class UpdateCliTests(unittest.TestCase):
    def test_update_runs_update_script_without_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            update_script = repo / "update.ps1"
            update_script.write_text("# update\n", encoding="utf-8")

            with (
                patch("bildebank.cli_update.sys.platform", "win32"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch("bildebank.cli_update.subprocess.run") as subprocess_run,
            ):
                subprocess_run.return_value.returncode = 7

                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 7)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "")
            subprocess_run.assert_called_once_with(
                [
                    "powershell.exe",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(update_script),
                ],
                check=False,
            )

    def test_update_runs_linux_update_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
            venv_python = repo / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("# python\n", encoding="utf-8")

            with (
                patch("bildebank.cli_update.sys.platform", "linux"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch("bildebank.cli_update.subprocess.run") as subprocess_run,
            ):
                subprocess_run.return_value.returncode = 0
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 0)
            self.assertIn("Ferdig", stdout)
            self.assertEqual(stderr, "")
            self.assertEqual(subprocess_run.call_count, 2)
            subprocess_run.assert_any_call(["git", "pull", "--ff-only"], cwd=repo, check=False)
            subprocess_run.assert_any_call(
                [str(venv_python), "-m", "pip", "install", "-e", "."],
                cwd=repo,
                check=False,
            )

    def test_update_creates_linux_venv_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")

            with (
                patch("bildebank.cli_update.sys.platform", "linux"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch("bildebank.cli_update.shutil.which", return_value="/usr/bin/python3.13"),
                patch("bildebank.cli_update.subprocess.run") as subprocess_run,
            ):
                subprocess_run.return_value.returncode = 0
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 0)
            self.assertIn("Ferdig", stdout)
            self.assertEqual(stderr, "")
            subprocess_run.assert_any_call(
                ["/usr/bin/python3.13", "-m", "venv", ".venv"],
                cwd=repo,
                check=False,
            )

    def test_update_reports_missing_update_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            with (
                patch("bildebank.cli_update.sys.platform", "win32"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Fant ikke update.ps1", stderr)

    def test_update_reports_missing_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "update.ps1").write_text("# update\n", encoding="utf-8")

            with (
                patch("bildebank.cli_update.sys.platform", "win32"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch("bildebank.cli_update.subprocess.run", side_effect=FileNotFoundError),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Fant ikke PowerShell", stderr)

