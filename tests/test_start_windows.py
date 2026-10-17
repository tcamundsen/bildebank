from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires Windows cmd.exe")


def run_start_script(script: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["cmd.exe", "/d", "/c", str(script)],
        cwd=cwd,
        input="\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("exit_code", [0, 7])
def test_start_from_another_directory_preserves_errors_and_opens_gui(
    tmp_path: Path, exit_code: int
) -> None:
    program_dir = tmp_path / "program with spaces"
    bin_dir = program_dir / "bin"
    bin_dir.mkdir(parents=True)
    script = program_dir / "start-bildebank.cmd"
    shutil.copyfile(REPO_ROOT / script.name, script)
    (bin_dir / "bildebank.cmd").write_text(
        "@echo off\n"
        "echo arguments=%*\n"
        "echo launcher_child=%BILDEBANK_WINDOWS_LAUNCHER_CHILD%\n"
        f"exit /b {exit_code}\n",
        encoding="ascii",
    )

    result = run_start_script(script, tmp_path)

    assert result.returncode == exit_code
    assert "arguments=start" in result.stdout
    assert "launcher_child=1" in result.stdout
    assert ("Bildebank kunne ikke starte" in result.stdout) == (exit_code != 0)


def test_missing_python_shows_startup_error(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    script = tmp_path / "start-bildebank.cmd"
    shutil.copyfile(REPO_ROOT / script.name, script)
    shutil.copyfile(REPO_ROOT / "bin" / "bildebank.cmd", tmp_path / "bin" / "bildebank.cmd")

    result = run_start_script(script, tmp_path)

    assert result.returncode == 1
    assert "Fant ikke" in result.stdout
    assert "Bildebank kunne ikke starte" in result.stdout
