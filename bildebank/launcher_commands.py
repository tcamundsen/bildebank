from __future__ import annotations

import json
import sys
from pathlib import Path

from .server_runtime import DEFAULT_HOST, DEFAULT_PORT
from .server_slideshow import DEFAULT_SLIDESHOW_DELAY_SECONDS


def bildebank_command(*args: str | Path) -> list[str]:
    return [sys.executable, "-m", "bildebank", *(str(arg) for arg in args)]


def create_command(collection_path: Path) -> list[str]:
    return bildebank_command("create", collection_path)


def import_command(collection_path: Path, source_folder: Path, import_name: str) -> list[str]:
    return bildebank_command("--target", collection_path, "import", "--name", import_name, source_folder)


def run_server_command(
    collection_path: Path,
    *,
    port: int | None = None,
    read_only: bool = False,
    lan_share: bool = False,
    slideshow: bool = False,
    delay: int = DEFAULT_SLIDESHOW_DELAY_SECONDS,
    filter: str | None = None,
) -> list[str]:
    command = bildebank_command("--target", collection_path, "run-server")
    if port is not None:
        command.extend(["--port", str(port)])
    if read_only and not slideshow:
        command.append("--read-only")
    if lan_share and not slideshow:
        command.append("--lan-share")
    if slideshow:
        command.append("--slideshow")
        command.extend(["--delay", str(delay)])
        if filter:
            command.extend(["--filter", filter])
    return command


def server_browser_url(port: int = DEFAULT_PORT) -> str:
    return f"http://{DEFAULT_HOST}:{port}/"


def launcher_command() -> list[str]:
    return bildebank_command("start")


def update_command() -> list[str]:
    return bildebank_command("update")


def doctor_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "doctor")


def deep_doctor_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "doctor", "--deep")


def geo_scan_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "geo-scan")


def face_scan_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "face-scan")


def image_scan_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "image-scan")


def image_clustering_command(
    collection_path: Path,
    *,
    filter_query: str,
    hide_out_of_focus: bool,
    algorithm: str = "minibatch_kmeans",
    n_clusters: int | None = None,
    random_seed: int = 0,
    min_cluster_size: int | None = None,
    min_samples: int | None = None,
) -> list[str]:
    if algorithm not in {"minibatch_kmeans", "hdbscan"}:
        raise ValueError(f"Ukjent grupperingsalgoritme: {algorithm}")
    command = bildebank_command(
        "--target",
        collection_path,
        "_image-clustering-worker",
        "--algorithm",
        algorithm,
        "--filter",
        filter_query,
    )
    if algorithm == "hdbscan":
        if min_cluster_size is None:
            raise ValueError("HDBSCAN krever minste gruppestørrelse.")
        command.extend(["--min-cluster-size", str(min_cluster_size)])
        if min_samples is not None:
            command.extend(["--min-samples", str(min_samples)])
    else:
        if n_clusters is None:
            raise ValueError("MiniBatchKMeans krever antall grupper.")
        command.extend(
            ["--clusters", str(n_clusters), "--seed", str(random_seed)]
        )
    if hide_out_of_focus:
        command.append("--hide-out-of-focus")
    return command


def make_thumbnails_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "make-thumbnails")


def cleanup_thumbnails_apply_command(collection_path: Path) -> list[str]:
    return bildebank_command(
        "--target",
        collection_path,
        "cleanup-thumbnails",
        "--apply",
    )


def make_video_previews_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "make-video-previews")


def ffmpeg_install_command() -> list[str]:
    return bildebank_command("ffmpeg-install")


def make_browser_command(collection_path: Path, *, hide_out_of_focus: bool = False) -> list[str]:
    command = bildebank_command("--target", collection_path, "make-browser")
    if hide_out_of_focus:
        command.append("--hide-out-of-focus")
    return command


def make_person_browser_command(
    collection_path: Path,
    person_name: str,
    *,
    hide_out_of_focus: bool = False,
) -> list[str]:
    command = bildebank_command("--target", collection_path, "make-person-browser", person_name)
    if hide_out_of_focus:
        command.append("--hide-out-of-focus")
    return command


def make_people_browser_command(collection_path: Path, *, hide_out_of_focus: bool = False) -> list[str]:
    command = bildebank_command("--target", collection_path, "make-people-browser")
    if hide_out_of_focus:
        command.append("--hide-out-of-focus")
    return command


def vacuum_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "vacuum")


def migrate_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "migrate")


def cleanup_pending_deletes_list_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "cleanup-pending-deletes", "--list")


def cleanup_pending_deletes_apply_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "cleanup-pending-deletes", "--apply")


def check_source_command(collection_path: Path, source_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "check-source", source_path)


def rescan_source_command(collection_path: Path, source_name: str) -> list[str]:
    return bildebank_command("--target", collection_path, "rescan-source", "--name", source_name)


def unimport_source_command(collection_path: Path, source_name: str) -> list[str]:
    return bildebank_command("--target", collection_path, "unimport", "--name", source_name)


def unimport_source_dry_run_command(
    collection_path: Path,
    source_name: str,
    *,
    target_change_report_json: Path | None = None,
) -> list[str]:
    command = bildebank_command("--target", collection_path, "unimport", "--dry-run", "--name", source_name)
    if target_change_report_json is not None:
        command.extend(["--target-change-report-json", str(target_change_report_json)])
    return command


def read_unimport_target_change_report(report_path: Path) -> list[str]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    changed_targets = payload.get("changed_targets", [])
    if not isinstance(changed_targets, list):
        raise ValueError("uventet target-change-rapport fra unimport dry-run")
    paths: list[str] = []
    for item in changed_targets:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("uventet target-change-rad fra unimport dry-run")
        paths.append(item["path"])
    return paths


def export_person_command(
    collection_path: Path,
    person_name: str,
    destination_root: Path,
    *,
    dry_run: bool = False,
) -> list[str]:
    command = bildebank_command("--target", collection_path, "export-person", person_name, "--dest", destination_root)
    if dry_run:
        command.append("--dry-run")
    return command


def _program_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def insightface_install_command(repo_root: Path | None = None) -> list[str]:
    root = repo_root or _program_repo_root()
    if sys.platform.startswith("linux"):
        return ["bash", str(root / "install-insightface.sh")]
    if sys.platform == "win32":
        script_path = root / "install-insightface.ps1"
        return ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
    raise RuntimeError("Automatisk InsightFace-installasjon støttes bare på Windows og Linux.")


def openclip_install_command(repo_root: Path | None = None) -> list[str]:
    root = repo_root or _program_repo_root()
    if sys.platform.startswith("linux"):
        return ["bash", str(root / "install-openclip.sh")]
    if sys.platform == "win32":
        script_path = root / "install-openclip.ps1"
        return ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
    raise RuntimeError("Automatisk OpenCLIP-installasjon støttes bare på Windows og Linux.")


def download_face_model_command() -> list[str]:
    return bildebank_command("download-face-model")


def download_openclip_model_command() -> list[str]:
    return bildebank_command("download-openclip-model")
