from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path


UPDATE_STATE_RELATIVE_PATH = Path("bildebank-tools") / "update-pending.txt"
UPDATE_SMOKE_TEST = "from bildebank.cli import main"
UPDATE_PROFILE_PROBE = (
    "import importlib.util, sys; "
    "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)"
)
UPDATE_PROFILE_MODULES = {
    "face": "insightface",
    "openclip": "open_clip",
}
UPDATE_PROFILE_SMOKE_TESTS = {
    "face": "import insightface; import onnxruntime",
    "openclip": "import igraph; import open_clip; import sklearn; import torch",
}


@dataclass(frozen=True)
class UpdateState:
    old_commit: str
    profiles: tuple[str, ...] = ()
    legacy: bool = False


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
        state = read_update_state(repo_root, state_path)
        if state.legacy:
            venv_python = ensure_linux_update_venv(repo_root)
            state = UpdateState(
                old_commit=state.old_commit,
                profiles=detect_linux_update_profiles(repo_root, venv_python),
            )
        try:
            rollback_linux_update(repo_root, state, state_path)
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
    profiles = detect_linux_update_profiles(repo_root, venv_python)
    old_commit = run_update_output(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo_root,
    )
    state = UpdateState(old_commit=old_commit, profiles=profiles)
    write_update_state(state_path, state)

    try:
        run_update_command(["git", "pull", "--ff-only"], cwd=repo_root)
        install_and_test_linux_update(repo_root, venv_python, profiles)
        state_path.unlink(missing_ok=True)
    except BaseException as update_error:
        try:
            rollback_linux_update(repo_root, state, state_path)
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


def detect_linux_update_profiles(
    repo_root: Path,
    venv_python: Path,
) -> tuple[str, ...]:
    profiles: list[str] = []
    for profile, module in UPDATE_PROFILE_MODULES.items():
        try:
            completed = subprocess.run(
                [str(venv_python), "-c", UPDATE_PROFILE_PROBE, module],
                cwd=repo_root,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ValueError(f"Fant ikke Python-miljøet: {venv_python}") from exc
        if completed.returncode == 0:
            profiles.append(profile)
        elif completed.returncode != 1:
            raise ValueError(
                f"Kunne ikke kontrollere installert profil {profile!r}: "
                f"Python avsluttet med kode {completed.returncode}."
            )
    return tuple(profiles)


def install_and_test_linux_update(
    repo_root: Path,
    venv_python: Path,
    profiles: tuple[str, ...] = (),
) -> None:
    run_update_command(
        [str(venv_python), "-m", "pip", "install", "-e", "."],
        cwd=repo_root,
    )
    for profile in validate_update_profiles(profiles):
        run_update_command(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "-e",
                f".[{profile}]",
            ],
            cwd=repo_root,
        )
    smoke_directory = repo_root / "bildebank-tools" / "update-smoke"
    smoke_directory.mkdir(parents=True, exist_ok=True)
    run_update_command(
        [str(venv_python), "-c", UPDATE_SMOKE_TEST],
        cwd=smoke_directory,
    )
    for profile in profiles:
        run_update_command(
            [str(venv_python), "-c", UPDATE_PROFILE_SMOKE_TESTS[profile]],
            cwd=smoke_directory,
        )


def rollback_linux_update(
    repo_root: Path,
    state: UpdateState,
    state_path: Path,
) -> None:
    require_clean_update_repo(repo_root)
    validate_update_commit(repo_root, state.old_commit)
    run_update_command(
        ["git", "reset", "--hard", state.old_commit],
        cwd=repo_root,
    )
    venv_python = ensure_linux_update_venv(repo_root)
    install_and_test_linux_update(repo_root, venv_python, state.profiles)
    state_path.unlink(missing_ok=True)


def write_update_state(state_path: Path, state: UpdateState) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", state.old_commit):
        raise ValueError(f"Git ga en ugyldig commit-ID: {state.old_commit!r}")
    profiles = validate_update_profiles(state.profiles)
    payload = json.dumps(
        {
            "version": 1,
            "old_commit": state.old_commit,
            "profiles": profiles,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_name(
        f".{state_path.name}.tmp-{uuid.uuid4().hex}"
    )
    try:
        temp_path.write_text(f"{payload}\n", encoding="ascii")
        temp_path.replace(state_path)
    finally:
        temp_path.unlink(missing_ok=True)


def read_update_state(repo_root: Path, state_path: Path) -> UpdateState:
    try:
        raw_state = state_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Kunne ikke lese recovery-markøren {state_path}: {exc}") from exc

    if not raw_state.startswith("{"):
        state = UpdateState(old_commit=raw_state, legacy=True)
    else:
        try:
            payload = json.loads(raw_state)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Recovery-markøren har ugyldig format: {state_path}"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("old_commit"), str)
            or not isinstance(payload.get("profiles"), list)
            or not all(isinstance(profile, str) for profile in payload["profiles"])
        ):
            raise ValueError(f"Recovery-markøren har ugyldig format: {state_path}")
        state = UpdateState(
            old_commit=payload["old_commit"],
            profiles=validate_update_profiles(tuple(payload["profiles"])),
        )
    validate_update_commit(repo_root, state.old_commit)
    return state


def validate_update_profiles(profiles: tuple[str, ...]) -> tuple[str, ...]:
    unknown = set(profiles) - UPDATE_PROFILE_MODULES.keys()
    if unknown:
        raise ValueError(
            "Recovery-markøren har ukjente installasjonsprofiler: "
            + ", ".join(sorted(unknown))
        )
    if len(profiles) != len(set(profiles)):
        raise ValueError("Recovery-markøren har dupliserte installasjonsprofiler.")
    return tuple(profile for profile in UPDATE_PROFILE_MODULES if profile in profiles)


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
