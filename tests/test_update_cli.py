from __future__ import annotations

import codecs
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.cli_update import (
    UPDATE_SMOKE_TEST,
    UPDATE_STATE_RELATIVE_PATH,
    run_update_linux,
)
from tests.cli_helpers import capture_cli


OLD_COMMIT = "a" * 40


def fake_linux_update_run(
    calls: list[list[str]],
    *,
    status: str = "",
    fail_first_install: bool = False,
    interrupt_first_install: bool = False,
    fail_reset: bool = False,
):
    install_calls = 0

    def run(command, **kwargs):
        nonlocal install_calls
        command = list(command)
        calls.append(command)
        stdout = ""
        stderr = ""
        returncode = 0
        if command == ["git", "status", "--porcelain=v1", "--untracked-files=no"]:
            stdout = status
        elif command == ["git", "rev-parse", "--verify", "HEAD"]:
            stdout = OLD_COMMIT + "\n"
        elif command[1:4] == ["-m", "pip", "install"]:
            install_calls += 1
            if interrupt_first_install and install_calls == 1:
                raise KeyboardInterrupt
            if fail_first_install and install_calls == 1:
                returncode = 7
                stderr = "install failed"
        elif command[1:] == ["-c", UPDATE_SMOKE_TEST]:
            if Path(kwargs["cwd"]).name != "update-smoke":
                returncode = 9
                stderr = "smoke test ran inside repository"
        elif command == ["git", "reset", "--hard", OLD_COMMIT] and fail_reset:
            returncode = 8
            stderr = "reset failed"
        return SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return run


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

            calls: list[list[str]] = []
            with (
                patch("bildebank.cli_update.sys.platform", "linux"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch(
                    "bildebank.cli_update.subprocess.run",
                    side_effect=fake_linux_update_run(calls),
                ),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 0)
            self.assertIn("Ferdig", stdout)
            self.assertEqual(stderr, "")
            self.assertEqual(
                calls,
                [
                    ["git", "status", "--porcelain=v1", "--untracked-files=no"],
                    ["git", "rev-parse", "--verify", "HEAD"],
                    ["git", "pull", "--ff-only"],
                    [str(venv_python), "-m", "pip", "install", "-e", "."],
                    [str(venv_python), "-c", UPDATE_SMOKE_TEST],
                ],
            )
            self.assertFalse((repo / UPDATE_STATE_RELATIVE_PATH).exists())

    def test_update_creates_linux_venv_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")

            calls: list[list[str]] = []
            with (
                patch("bildebank.cli_update.sys.platform", "linux"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch("bildebank.cli_update.shutil.which", return_value="/usr/bin/python3.13"),
                patch(
                    "bildebank.cli_update.subprocess.run",
                    side_effect=fake_linux_update_run(calls),
                ),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 0)
            self.assertIn("Ferdig", stdout)
            self.assertEqual(stderr, "")
            self.assertIn(
                ["/usr/bin/python3.13", "-m", "venv", ".venv"],
                calls,
            )

    def test_update_refuses_modified_tracked_linux_file_before_writing_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
            calls: list[list[str]] = []

            with (
                patch("bildebank.cli_update.sys.platform", "linux"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch(
                    "bildebank.cli_update.subprocess.run",
                    side_effect=fake_linux_update_run(
                        calls,
                        status=" M README.md\n",
                    ),
                ),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Git-sporede filer", stderr)
            self.assertIn("README.md", stderr)
            self.assertEqual(
                calls,
                [["git", "status", "--porcelain=v1", "--untracked-files=no"]],
            )
            self.assertFalse((repo / UPDATE_STATE_RELATIVE_PATH).exists())

    def test_failed_linux_install_rolls_back_source_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
            venv_python = repo / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("# python\n", encoding="utf-8")
            calls: list[list[str]] = []

            with (
                patch("bildebank.cli_update.sys.platform", "linux"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch(
                    "bildebank.cli_update.subprocess.run",
                    side_effect=fake_linux_update_run(
                        calls,
                        fail_first_install=True,
                    ),
                ),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("gamle versjonen er gjenopprettet", stderr)
            self.assertIn(["git", "reset", "--hard", OLD_COMMIT], calls)
            self.assertEqual(
                calls.count([str(venv_python), "-m", "pip", "install", "-e", "."]),
                1,
            )
            self.assertIn(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--no-build-isolation",
                    "-e",
                    ".",
                ],
                calls,
            )
            self.assertIn([str(venv_python), "-c", UPDATE_SMOKE_TEST], calls)
            self.assertFalse((repo / UPDATE_STATE_RELATIVE_PATH).exists())

    def test_interrupted_linux_install_rolls_back_before_reporting_interrupt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
            venv_python = repo / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("# python\n", encoding="utf-8")
            calls: list[list[str]] = []

            with (
                patch("bildebank.cli_update.sys.platform", "linux"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch(
                    "bildebank.cli_update.subprocess.run",
                    side_effect=fake_linux_update_run(
                        calls,
                        interrupt_first_install=True,
                    ),
                ),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 130)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "Avbrutt.\n")
            self.assertIn(["git", "reset", "--hard", OLD_COMMIT], calls)
            self.assertEqual(
                calls.count([str(venv_python), "-m", "pip", "install", "-e", "."]),
                1,
            )
            self.assertIn(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--no-build-isolation",
                    "-e",
                    ".",
                ],
                calls,
            )
            self.assertFalse((repo / UPDATE_STATE_RELATIVE_PATH).exists())

    def test_failed_linux_rollback_keeps_recovery_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
            venv_python = repo / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("# python\n", encoding="utf-8")
            calls: list[list[str]] = []

            with (
                patch("bildebank.cli_update.sys.platform", "linux"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch(
                    "bildebank.cli_update.subprocess.run",
                    side_effect=fake_linux_update_run(
                        calls,
                        fail_first_install=True,
                        fail_reset=True,
                    ),
                ),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("rollback feilet", stderr)
            self.assertTrue((repo / UPDATE_STATE_RELATIVE_PATH).exists())

    def test_pending_linux_update_is_recovered_before_new_pull(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
            venv_python = repo / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("# python\n", encoding="utf-8")
            state_path = repo / UPDATE_STATE_RELATIVE_PATH
            state_path.parent.mkdir(parents=True)
            state_path.write_text(OLD_COMMIT + "\n", encoding="ascii")
            calls: list[list[str]] = []

            with (
                patch("bildebank.cli_update.sys.platform", "linux"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch(
                    "bildebank.cli_update.subprocess.run",
                    side_effect=fake_linux_update_run(calls),
                ),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Forrige oppdatering ble avbrutt", stderr)
            self.assertIn(["git", "reset", "--hard", OLD_COMMIT], calls)
            self.assertNotIn(["git", "pull", "--ff-only"], calls)
            self.assertFalse(state_path.exists())

    def test_invalid_pending_linux_update_is_not_used_for_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
            state_path = repo / UPDATE_STATE_RELATIVE_PATH
            state_path.parent.mkdir(parents=True)
            state_path.write_text("not-a-commit\n", encoding="ascii")
            calls: list[list[str]] = []

            with (
                patch("bildebank.cli_update.sys.platform", "linux"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch(
                    "bildebank.cli_update.subprocess.run",
                    side_effect=fake_linux_update_run(calls),
                ),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("ugyldig commit-ID", stderr)
            self.assertFalse(
                any(command[:3] == ["git", "reset", "--hard"] for command in calls)
            )
            self.assertTrue(state_path.exists())

    def test_windows_update_script_contains_recovery_contract(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "update.ps1"
        script_bytes = script_path.read_bytes()
        self.assertTrue(script_bytes.startswith(codecs.BOM_UTF8))
        script = script_bytes.decode("utf-8-sig")

        self.assertIn("Assert-CleanRepo", script)
        self.assertIn('"--untracked-files=no"', script)
        self.assertIn("bildebank-tools\\update-pending.txt", script)
        self.assertIn("Write-UpdateState -OldCommit $oldCommit", script)
        self.assertIn(
            'Invoke-Native -FilePath "git" -ArgumentList @("reset", "--hard", $OldCommit)',
            script,
        )
        self.assertIn("Restore-PreviousVersion -OldCommit $oldCommit", script)
        self.assertIn('"from bildebank.cli import main"', script)

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "Integrasjonstesten bruker en falsk Linux-venv.",
    )
    def test_linux_update_rolls_back_real_fast_forward_on_install_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            seed = root / "seed"
            repo = root / "repo"

            subprocess.run(
                ["git", "init", "--bare", "--initial-branch=main", str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "init", "--initial-branch=main", str(seed)],
                check=True,
                capture_output=True,
                text=True,
            )

            def git(cwd: Path, *arguments: str) -> str:
                completed = subprocess.run(
                    ["git", *arguments],
                    cwd=cwd,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return completed.stdout.strip()

            git(seed, "config", "user.email", "test@example.invalid")
            git(seed, "config", "user.name", "Bildebank test")
            (seed / ".gitignore").write_text(
                ".venv/\nbildebank-tools/\n",
                encoding="utf-8",
            )
            (seed / "pyproject.toml").write_text(
                '[project]\nname = "update-test"\nversion = "1.0"\n',
                encoding="utf-8",
            )
            git(seed, "add", ".")
            git(seed, "commit", "-m", "old")
            old_commit = git(seed, "rev-parse", "HEAD")
            git(seed, "remote", "add", "origin", str(remote))
            git(seed, "push", "-u", "origin", "main")
            subprocess.run(
                ["git", "clone", str(remote), str(repo)],
                check=True,
                capture_output=True,
                text=True,
            )

            venv_python = repo / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text(
                """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "pip" ] && grep -q 'version = "2.0"' pyproject.toml; then
    exit 7
fi
exit 0
""",
                encoding="utf-8",
            )
            venv_python.chmod(0o755)
            local_note = repo / "lokale-notater.txt"
            local_note.write_text("skal bevares\n", encoding="utf-8")

            (seed / "pyproject.toml").write_text(
                '[project]\nname = "update-test"\nversion = "2.0"\n',
                encoding="utf-8",
            )
            git(seed, "add", "pyproject.toml")
            git(seed, "commit", "-m", "new")
            git(seed, "push")

            with self.assertRaisesRegex(ValueError, "gamle versjonen er gjenopprettet"):
                run_update_linux(repo)

            self.assertEqual(git(repo, "rev-parse", "HEAD"), old_commit)
            self.assertIn('version = "1.0"', (repo / "pyproject.toml").read_text())
            self.assertEqual(local_note.read_text(encoding="utf-8"), "skal bevares\n")
            self.assertFalse((repo / UPDATE_STATE_RELATIVE_PATH).exists())

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
