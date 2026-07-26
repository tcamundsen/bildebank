from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import load_config, load_launcher_collection_path, set_launcher_collection_path
from .ffmpeg_tools import resolve_ffmpeg_tools
from .insightface_models import insightface_model_files_exist


INSIGHTFACE_RUNTIME_PROBE = (
    "from insightface.app import FaceAnalysis; import onnxruntime"
)


@dataclass(frozen=True)
class LauncherConfig:
    collection_path: Path


@dataclass(frozen=True)
class LauncherUpdateStatus:
    status: str
    detail: str = ""
    commits_behind: int = 0


@dataclass(frozen=True)
class RegisteredPerson:
    name: str
    confirmed_file_count: int
    face_count: int
    suggestion_count: int
    updated_at: str


@dataclass(frozen=True)
class InsightFaceDependencyStatus:
    status: str
    detail: str = ""


@dataclass(frozen=True)
class InsightFaceModelStatus:
    model_name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class OpenClipModelStatus:
    model_name: str
    pretrained: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class FFmpegDependencyStatus:
    status: str
    detail: str = ""


def program_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_collection_path() -> Path:
    return Path.home() / "kode" / "bilde-samling"


def load_launcher_config() -> LauncherConfig:
    current_target = db.find_target()
    if current_target is not None:
        return LauncherConfig(collection_path=current_target)
    collection_path = load_launcher_collection_path(program_repo_root())
    if collection_path is not None:
        return LauncherConfig(collection_path=collection_path)
    return LauncherConfig(collection_path=default_collection_path())


def save_launcher_config(config: LauncherConfig) -> None:
    set_launcher_collection_path(program_repo_root(), config.collection_path)


def is_collection_created(collection_path: Path) -> bool:
    return db.db_path_for_target(collection_path).exists()


def check_launcher_update_status(repo_root: Path | None = None) -> LauncherUpdateStatus:
    root = repo_root or program_repo_root()
    try:
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
        upstream = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root)
        remote_name = _upstream_remote_name(upstream)
        remote_url = _run_git(["remote", "get-url", remote_name], root)
        if _git_remote_url_uses_ssh(remote_url):
            return LauncherUpdateStatus(
                "skipped",
                f"hopper over automatisk oppdateringssjekk for SSH-remote {remote_name}",
            )
        _run_git(["fetch", "--quiet"], root)
        commit_count_text = _run_git(["rev-list", "--count", "HEAD..@{u}"], root)
        commit_count = int(commit_count_text.strip())
    except FileNotFoundError as exc:
        return LauncherUpdateStatus("error", f"git finnes ikke: {exc}")
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        return LauncherUpdateStatus("error", str(exc))
    if commit_count > 0:
        return LauncherUpdateStatus(
            "available",
            f"{branch} ligger {commit_count} commits bak {upstream}",
            commits_behind=commit_count,
        )
    return LauncherUpdateStatus("current", f"{branch} er oppdatert mot {upstream}")


def _upstream_remote_name(upstream: str) -> str:
    remote_name, separator, _branch_name = upstream.partition("/")
    if not separator or not remote_name:
        raise ValueError(f"uventet upstream-navn: {upstream}")
    return remote_name


def _git_remote_url_uses_ssh(remote_url: str) -> bool:
    url = remote_url.strip()
    if url.startswith(("ssh://", "git+ssh://")):
        return True
    if "://" in url or "@" not in url:
        return False
    _user, _separator, host_and_path = url.partition("@")
    return ":" in host_and_path


def _run_git(args: list[str], repo_root: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


def insightface_install_supported() -> bool:
    return os.name == "nt"


def openclip_install_supported() -> bool:
    return os.name == "nt"


def ffmpeg_install_supported() -> bool:
    return os.name == "nt"


def ffmpeg_dependency_status() -> FFmpegDependencyStatus:
    try:
        tools = resolve_ffmpeg_tools(program_repo_root())
    except FileNotFoundError as exc:
        return FFmpegDependencyStatus("Mangler", str(exc))
    except (OSError, RuntimeError) as exc:
        return FFmpegDependencyStatus("Feil", str(exc))
    return FFmpegDependencyStatus("Klar", tools.version)


def dependency_setup_button_state(
    *,
    enabled: bool,
    migration_required: bool,
    migration_status_error: str | None,
    install_supported: bool,
) -> str:
    if enabled and not migration_required and migration_status_error is None and install_supported:
        return "normal"
    return "disabled"


def face_model_download_button_state(
    *,
    enabled: bool,
    migration_required: bool,
    migration_status_error: str | None,
    insightface_status: InsightFaceDependencyStatus,
) -> str:
    if (
        enabled
        and not migration_required
        and migration_status_error is None
        and insightface_status.status == "Klar"
    ):
        return "normal"
    return "disabled"


def openclip_dependency_status() -> str:
    if importlib.util.find_spec("open_clip") is not None:
        return "Installert"
    return "Mangler"


def openclip_model_status(repo_root: Path | None = None) -> OpenClipModelStatus:
    config = load_config(repo_root or program_repo_root()).openclip
    if _openclip_model_files_exist(config.model_root):
        return OpenClipModelStatus(config.model_name, config.pretrained, "Tilgjengelig", str(config.model_root))
    return OpenClipModelStatus(config.model_name, config.pretrained, "Mangler", str(config.model_root))


def _openclip_model_files_exist(model_root: Path) -> bool:
    if not model_root.is_dir():
        return False
    model_extensions = {".bin", ".pt", ".pth", ".safetensors"}
    return any(path.is_file() and path.suffix.lower() in model_extensions for path in model_root.rglob("*"))


def insightface_dependency_status() -> InsightFaceDependencyStatus:
    missing = [
        module_name
        for module_name in ("insightface", "onnxruntime")
        if importlib.util.find_spec(module_name) is None
    ]
    if missing:
        return InsightFaceDependencyStatus(
            "Mangler",
            "Mangler: " + ", ".join(missing),
        )

    probe_environment = os.environ.copy()
    probe_environment["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            [sys.executable, "-c", INSIGHTFACE_RUNTIME_PROBE],
            cwd=program_repo_root(),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=probe_environment,
        )
    except OSError as exc:
        return InsightFaceDependencyStatus(
            "Feil",
            f"Kunne ikke starte InsightFace-kontrollen: {exc}",
        )
    if result.returncode != 0:
        return InsightFaceDependencyStatus(
            "Feil",
            _insightface_probe_error(result.stdout, result.stderr, result.returncode),
        )
    return InsightFaceDependencyStatus("Klar")


def _insightface_probe_error(
    stdout: str,
    stderr: str,
    returncode: int,
) -> str:
    output = "\n".join(part.strip() for part in (stderr, stdout) if part.strip())
    if "libGL.so.1" in output:
        return (
            "InsightFace er installert, men OpenCV mangler Linux-biblioteket "
            "libGL.so.1. Installer det i WSL/Linux med `sudo apt install libgl1`."
        )
    last_line = output.splitlines()[-1] if output else f"avsluttet med kode {returncode}"
    return f"InsightFace er installert, men kan ikke lastes: {last_line}"


def insightface_model_status(repo_root: Path | None = None) -> InsightFaceModelStatus:
    config = load_config(repo_root or program_repo_root()).face_recognition
    if insightface_model_files_exist(config):
        return InsightFaceModelStatus(config.model_name, "Lastet ned", str(config.model_root))
    return InsightFaceModelStatus(config.model_name, "Mangler", str(config.model_root))


def registered_sources(collection_path: Path) -> list[db.Source]:
    conn = db.connect(collection_path)
    try:
        return db.get_sources(conn)
    finally:
        conn.close()


def registered_persons(collection_path: Path) -> list[RegisteredPerson]:
    from .face import list_persons

    config = load_config(program_repo_root()).face_recognition
    return [
        RegisteredPerson(
            name=str(row["name"]),
            confirmed_file_count=int(row["confirmed_file_count"]),
            face_count=int(row["face_count"]),
            suggestion_count=int(row["suggestion_count"]),
            updated_at=str(row["updated_at"]),
        )
        for row in list_persons(collection_path, config)
    ]


def migration_plan_needs_action(plan: db.MigrationPlan) -> bool:
    return (
        plan.current_version != plan.target_version
        or plan.refreshes_performance_indexes
        or bool(plan.internal_repairs)
    )


def collection_needs_migration(collection_path: Path) -> bool:
    return migration_plan_needs_action(db.migration_plan(collection_path, validate=False))


def rescan_source_candidates(sources: list[db.Source]) -> list[db.Source]:
    return list(sources)
