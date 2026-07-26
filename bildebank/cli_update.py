from __future__ import annotations

import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


UPDATE_STATE_RELATIVE_PATH = Path("bildebank-tools") / "update-pending.txt"
UPDATE_SMOKE_TEST = "from bildebank.cli import main"


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

    state_path = repo_root / UPDATE_STATE_RELATIVE_PATH
    if state_path.exists():
        old_commit = read_update_state(repo_root, state_path)
        try:
            rollback_linux_update(repo_root, old_commit, state_path)
        except Exception as exc:
            raise ValueError(
                "Fant en avbrutt oppdatering, men klarte ikke å gjenopprette "
                f"forrige versjon. Recovery-markøren er beholdt: {state_path}: {exc}"
            ) from exc
        raise ValueError(
            "Forrige oppdatering ble avbrutt. Den gamle versjonen er "
            "gjenopprettet og kontrollert. Kjør `bildebank update` på nytt."
        )

    require_clean_update_repo(repo_root)
    venv_python = ensure_linux_update_venv(repo_root)
    old_commit = run_update_output(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo_root,
    )
    write_update_state(state_path, old_commit)

    try:
        run_update_command(["git", "pull", "--ff-only"], cwd=repo_root)
        install_and_test_linux_update(repo_root, venv_python)
        state_path.unlink(missing_ok=True)
    except BaseException as update_error:
        try:
            rollback_linux_update(repo_root, old_commit, state_path)
        except BaseException as rollback_error:
            if isinstance(rollback_error, KeyboardInterrupt):
                raise
            raise ValueError(
                "Oppdateringen feilet, og automatisk rollback feilet også. "
                f"Recovery-markøren er beholdt: {state_path}. "
                f"Oppdateringsfeil: {update_error}. Rollback-feil: {rollback_error}"
            ) from rollback_error
        if isinstance(update_error, KeyboardInterrupt):
            raise
        raise ValueError(
            "Oppdateringen feilet. Den gamle versjonen er gjenopprettet og "
            f"kontrollert. Opprinnelig feil: {update_error}"
        ) from update_error

    print("Ferdig. Databasen migreres ikke automatisk.")
    print("Kjør bildebank migrate i en bildesamling hvis programmet ber om det.")
    return 0


def require_clean_update_repo(repo_root: Path) -> None:
    status = run_update_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=no"],
        cwd=repo_root,
    )
    if status:
        raise ValueError(
            "Programrepoet har lokale endringer i Git-sporede filer. "
            f"Commit eller tilbakestill dem før oppdatering:\n{status}"
        )


def ensure_linux_update_venv(repo_root: Path) -> Path:
    venv_python = repo_root / ".venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python
    python = shutil.which("python3.13") or shutil.which("python3")
    if python is None:
        raise ValueError("Fant ikke python3.13 eller python3 for å lage .venv.")
    run_update_command([python, "-m", "venv", ".venv"], cwd=repo_root)
    return venv_python


def install_and_test_linux_update(
    repo_root: Path,
    venv_python: Path,
    *,
    no_dependencies: bool = False,
) -> None:
    install_command = [str(venv_python), "-m", "pip", "install"]
    if no_dependencies:
        install_command.extend(["--no-deps", "--no-build-isolation"])
    install_command.extend(["-e", "."])
    run_update_command(
        install_command,
        cwd=repo_root,
    )
    smoke_directory = repo_root / "bildebank-tools" / "update-smoke"
    smoke_directory.mkdir(parents=True, exist_ok=True)
    run_update_command(
        [str(venv_python), "-c", UPDATE_SMOKE_TEST],
        cwd=smoke_directory,
    )


def rollback_linux_update(
    repo_root: Path,
    old_commit: str,
    state_path: Path,
) -> None:
    require_clean_update_repo(repo_root)
    validate_update_commit(repo_root, old_commit)
    run_update_command(
        ["git", "reset", "--hard", old_commit],
        cwd=repo_root,
    )
    venv_python = ensure_linux_update_venv(repo_root)
    install_and_test_linux_update(
        repo_root,
        venv_python,
        no_dependencies=True,
    )
    state_path.unlink(missing_ok=True)


def write_update_state(state_path: Path, old_commit: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", old_commit):
        raise ValueError(f"Git ga en ugyldig commit-ID: {old_commit!r}")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_name(
        f".{state_path.name}.tmp-{uuid.uuid4().hex}"
    )
    try:
        temp_path.write_text(f"{old_commit}\n", encoding="ascii")
        temp_path.replace(state_path)
    finally:
        temp_path.unlink(missing_ok=True)


def read_update_state(repo_root: Path, state_path: Path) -> str:
    try:
        old_commit = state_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Kunne ikke lese recovery-markøren {state_path}: {exc}") from exc
    validate_update_commit(repo_root, old_commit)
    return old_commit


def validate_update_commit(repo_root: Path, commit: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        raise ValueError(f"Recovery-markøren har en ugyldig commit-ID: {commit!r}")
    run_update_command(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=repo_root,
    )


def run_update_command(command: list[str], *, cwd: Path) -> None:
    try:
        completed = subprocess.run(command, cwd=cwd, check=False)
    except FileNotFoundError as exc:
        raise ValueError(f"Fant ikke kommandoen: {command[0]}") from exc
    if completed.returncode != 0:
        raise ValueError(
            f"Kommando feilet med exit code {completed.returncode}: {' '.join(command)}"
        )


def run_update_output(command: list[str], *, cwd: Path) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"Fant ikke kommandoen: {command[0]}") from exc
    if completed.returncode != 0:
        details = completed.stderr.strip()
        suffix = f": {details}" if details else ""
        raise ValueError(
            f"Kommando feilet med exit code {completed.returncode}: "
            f"{' '.join(command)}{suffix}"
        )
    return completed.stdout.strip()
