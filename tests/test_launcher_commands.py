from __future__ import annotations

import os
from pathlib import Path

from bildebank.launcher_commands import (
    backup_command,
    check_source_command,
    cleanup_pending_deletes_apply_command,
    cleanup_pending_deletes_list_command,
    create_command,
    deep_doctor_command,
    doctor_command,
    download_face_model_command,
    export_person_command,
    face_scan_command,
    geo_scan_command,
    image_scan_command,
    import_command,
    insightface_install_command,
    launcher_command,
    make_browser_command,
    make_people_browser_command,
    make_person_browser_command,
    make_thumbnails_command,
    migrate_command,
    openclip_install_command,
    read_unimport_target_change_report,
    rescan_source_command,
    run_server_command,
    unimport_source_command,
    unimport_source_dry_run_command,
    update_command,
    vacuum_command,
)


def test_launcher_commands_use_existing_cli_semantics(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    source = tmp_path / "bilder"

    assert create_command(collection)[-2:] == ["create", str(collection)]
    import_args = import_command(collection, source, "Sommer 2024")
    assert import_args[-6:] == [
        "--target",
        str(collection),
        "import",
        "--name",
        "Sommer 2024",
        str(source),
    ]
    assert os.path.basename(import_args[0]).startswith("python")

    assert run_server_command(collection)[-3:] == ["--target", str(collection), "run-server"]
    assert geo_scan_command(collection)[-3:] == ["--target", str(collection), "geo-scan"]
    assert doctor_command(collection)[-3:] == ["--target", str(collection), "doctor"]
    assert deep_doctor_command(collection)[-4:] == ["--target", str(collection), "doctor", "--deep"]
    assert face_scan_command(collection)[-3:] == ["--target", str(collection), "face-scan"]
    assert image_scan_command(collection)[-3:] == ["--target", str(collection), "image-scan"]
    assert make_thumbnails_command(collection)[-3:] == [
        "--target",
        str(collection),
        "make-thumbnails",
    ]
    assert make_browser_command(collection)[-3:] == ["--target", str(collection), "make-browser"]
    assert make_browser_command(collection, hide_out_of_focus=True)[-4:] == [
        "--target",
        str(collection),
        "make-browser",
        "--hide-out-of-focus",
    ]
    assert make_person_browser_command(collection, "Kari")[-4:] == [
        "--target",
        str(collection),
        "make-person-browser",
        "Kari",
    ]
    assert make_person_browser_command(collection, "Kari", hide_out_of_focus=True)[-5:] == [
        "--target",
        str(collection),
        "make-person-browser",
        "Kari",
        "--hide-out-of-focus",
    ]
    assert make_people_browser_command(collection)[-3:] == [
        "--target",
        str(collection),
        "make-people-browser",
    ]
    assert make_people_browser_command(collection, hide_out_of_focus=True)[-4:] == [
        "--target",
        str(collection),
        "make-people-browser",
        "--hide-out-of-focus",
    ]
    assert vacuum_command(collection)[-3:] == ["--target", str(collection), "vacuum"]
    assert migrate_command(collection)[-3:] == ["--target", str(collection), "migrate"]
    assert cleanup_pending_deletes_list_command(collection)[-4:] == [
        "--target",
        str(collection),
        "cleanup-pending-deletes",
        "--list",
    ]
    assert cleanup_pending_deletes_apply_command(collection)[-4:] == [
        "--target",
        str(collection),
        "cleanup-pending-deletes",
        "--apply",
    ]
    assert launcher_command()[-1:] == ["start"]
    assert update_command()[-1:] == ["update"]
    assert check_source_command(collection, source)[-4:] == [
        "--target",
        str(collection),
        "check-source",
        str(source),
    ]
    assert rescan_source_command(collection, "Sommer 2024")[-5:] == [
        "--target",
        str(collection),
        "rescan-source",
        "--name",
        "Sommer 2024",
    ]
    assert unimport_source_command(collection, "Sommer 2024")[-5:] == [
        "--target",
        str(collection),
        "unimport",
        "--name",
        "Sommer 2024",
    ]
    assert unimport_source_dry_run_command(collection, "Sommer 2024")[-6:] == [
        "--target",
        str(collection),
        "unimport",
        "--dry-run",
        "--name",
        "Sommer 2024",
    ]
    assert unimport_source_dry_run_command(
        collection,
        "Sommer 2024",
        target_change_report_json=tmp_path / "unimport.json",
    )[-8:] == [
        "--target",
        str(collection),
        "unimport",
        "--dry-run",
        "--name",
        "Sommer 2024",
        "--target-change-report-json",
        str(tmp_path / "unimport.json"),
    ]
    assert export_person_command(collection, "Kari", tmp_path / "eksport")[-6:] == [
        "--target",
        str(collection),
        "export-person",
        "Kari",
        "--dest",
        str(tmp_path / "eksport"),
    ]
    assert export_person_command(collection, "Kari", tmp_path / "eksport", dry_run=True)[-7:] == [
        "--target",
        str(collection),
        "export-person",
        "Kari",
        "--dest",
        str(tmp_path / "eksport"),
        "--dry-run",
    ]
    assert backup_command(collection, tmp_path / "backup")[-4:] == [
        "--target",
        str(collection),
        "backup",
        str(tmp_path / "backup"),
    ]
    assert backup_command(collection, tmp_path / "backup", dry_run=True)[-5:] == [
        "--target",
        str(collection),
        "backup",
        "--dry-run",
        str(tmp_path / "backup"),
    ]
    assert download_face_model_command()[-1:] == ["download-face-model"]


def test_read_unimport_target_change_report_returns_changed_paths(tmp_path: Path) -> None:
    report_path = tmp_path / "unimport-report.json"
    report_path.write_text(
        '{"changed_targets": [{"path": "2024/01/IMG.jpg"}]}',
        encoding="utf-8",
    )

    assert read_unimport_target_change_report(report_path) == ["2024/01/IMG.jpg"]


def test_insightface_install_command_runs_existing_powershell_script(tmp_path: Path) -> None:
    command = insightface_install_command(tmp_path)

    assert command == [
        "powershell.exe",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(tmp_path / "install-insightface.ps1"),
    ]


def test_openclip_install_command_runs_existing_powershell_script(tmp_path: Path) -> None:
    command = openclip_install_command(tmp_path)

    assert command == [
        "powershell.exe",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(tmp_path / "install-openclip.ps1"),
    ]
