from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def run_update(repo_root: Path) -> int:
    if sys.platform == "win32":
        return run_update_windows(repo_root)
    return run_update_linux(repo_root)


def run_update_windows(repo_root: Path) -> int:
    update_script = repo_root / "update.ps1"
    if not update_script.exists():
        raise ValueError(
            f"Fant ikke update.ps1 i programmappen: {repo_root}. "
            f"Kjør manuelt fra programmappen hvis nødvendig."
        )
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(update_script),
            ],
            check=False,
        )
    except FileNotFoundError as exc:
        raise ValueError(
            "Fant ikke PowerShell. Kjør oppdatering manuelt fra programmappen: "
            f"cd {repo_root}; .\\update.ps1"
        ) from exc
    return completed.returncode


def run_update_linux(repo_root: Path) -> int:
    if not (repo_root / ".git").exists():
        raise ValueError(f"Fant ikke git-repo: {repo_root}")
    if not (repo_root / "pyproject.toml").exists():
        raise ValueError(f"Fant ikke pyproject.toml i: {repo_root}")

    run_update_command(["git", "pull", "--ff-only"], cwd=repo_root)

    venv_python = repo_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        python = shutil.which("python3.13") or shutil.which("python3")
        if python is None:
            raise ValueError("Fant ikke python3.13 eller python3 for å lage .venv.")
        run_update_command([python, "-m", "venv", ".venv"], cwd=repo_root)

    run_update_command([str(venv_python), "-m", "pip", "install", "-e", "."], cwd=repo_root)
    print("Ferdig. Databasen migreres ikke automatisk.")
    print("Kjør bildebank migrate i en bildesamling hvis programmet ber om det.")
    return 0


def run_update_command(command: list[str], *, cwd: Path) -> None:
    try:
        completed = subprocess.run(command, cwd=cwd, check=False)
    except FileNotFoundError as exc:
        raise ValueError(f"Fant ikke kommandoen: {command[0]}") from exc
    if completed.returncode != 0:
        raise ValueError(
            f"Kommando feilet med exit code {completed.returncode}: {' '.join(command)}"
        )
