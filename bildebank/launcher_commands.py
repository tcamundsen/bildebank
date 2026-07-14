from __future__ import annotations

import json
import sys
from pathlib import Path

from .server_runtime import DEFAULT_HOST, DEFAULT_PORT


def bildebank_command(*args: str | Path) -> list[str]:
    return [sys.executable, "-m", "bildebank", *(str(arg) for arg in args)]


def create_command(collection_path: Path) -> list[str]:
    return bildebank_command("create", collection_path)


def import_command(collection_path: Path, source_folder: Path, import_name: str) -> list[str]:
    return bildebank_command("--target", collection_path, "import", "--name", import_name, source_folder)


def run_server_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "run-server")


def server_browser_url() -> str:
    return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/"


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


def make_thumbnails_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "make-thumbnails")


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


def backup_command(collection_path: Path, backup_parent: Path, dry_run: bool = False) -> list[str]:
    command = bildebank_command("--target", collection_path, "backup")
    if dry_run:
        command.append("--dry-run")
    command.append(str(backup_parent))
    return command


def _program_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def insightface_install_command(repo_root: Path | None = None) -> list[str]:
    script_path = (repo_root or _program_repo_root()) / "install-insightface.ps1"
    return ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]


def openclip_install_command(repo_root: Path | None = None) -> list[str]:
    script_path = (repo_root or _program_repo_root()) / "install-openclip.ps1"
    return ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]


def download_face_model_command() -> list[str]:
    return bildebank_command("download-face-model")
