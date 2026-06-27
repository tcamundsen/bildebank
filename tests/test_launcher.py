from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.launcher import (
    LauncherConfig,
    check_source_command,
    cleanup_pending_deletes_apply_command,
    cleanup_pending_deletes_list_command,
    collection_needs_migration,
    create_command,
    default_collection_path,
    deep_doctor_command,
    doctor_command,
    download_face_model_command,
    face_scan_command,
    geo_scan_command,
    image_scan_command,
    import_command,
    insightface_dependency_status,
    insightface_install_command,
    insightface_model_status,
    is_collection_created,
    launcher_command,
    load_launcher_config,
    make_thumbnails_command,
    migrate_command,
    migration_plan_needs_action,
    open_server_browser_window,
    openclip_dependency_status,
    openclip_install_command,
    openclip_model_status,
    progress_log_key,
    registered_sources,
    rescan_source_candidates,
    rescan_source_command,
    save_launcher_config,
    server_browser_url,
    source_is_collection_or_inside,
    subprocess_output_encoding,
    suggest_import_name,
    unimport_source_dry_run_command,
    unimport_source_command,
    update_command,
    vacuum_command,
)


def test_default_collection_path_uses_home_kode_bilde_samling() -> None:
    with patch("pathlib.Path.home", return_value=Path(r"C:\Users\tom")):
        assert default_collection_path() == Path(r"C:\Users\tom") / "kode" / "bilde-samling"


def test_load_config_uses_default_when_file_is_missing(tmp_path: Path) -> None:
    with (
        patch("pathlib.Path.home", return_value=tmp_path / "home"),
        patch("bildebank.launcher.program_repo_root", return_value=tmp_path / "repo"),
    ):
        config = load_launcher_config()

    assert config.collection_path == tmp_path / "home" / "kode" / "bilde-samling"


def test_save_and_load_launcher_config_uses_bildebank_config_toml(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    collection_path = tmp_path / "samling"

    with patch("bildebank.launcher.program_repo_root", return_value=repo_root):
        save_launcher_config(LauncherConfig(collection_path=collection_path))

    config_text = (repo_root / "bildebank-config.toml").read_text(encoding="utf-8")
    assert "[launcher]" in config_text
    assert f'collection_path = "{collection_path}"' in config_text
    with patch("bildebank.launcher.program_repo_root", return_value=repo_root):
        assert load_launcher_config().collection_path == collection_path


def test_suggest_import_name_uses_last_folder_name() -> None:
    assert suggest_import_name(Path(r"D:\Bilder\Sommer 2024")) == "Sommer 2024"


def test_source_is_collection_or_inside_rejects_collection_and_child(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    source = tmp_path / "bilder"
    child = collection / "2024"
    collection.mkdir()
    source.mkdir()
    child.mkdir()

    assert source_is_collection_or_inside(collection, collection)
    assert source_is_collection_or_inside(child, collection)
    assert not source_is_collection_or_inside(source, collection)


def test_is_collection_created_uses_bildebank_database_marker(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    collection.mkdir()
    assert not is_collection_created(collection)

    db.init_database(collection)

    assert is_collection_created(collection)


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
    assert launcher_command()[-1:] == ["launcher"]
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
    assert download_face_model_command()[-1:] == ["download-face-model"]


def test_open_server_browser_window_opens_default_run_server_url() -> None:
    with patch("webbrowser.open", return_value=True) as open_browser:
        assert open_server_browser_window()

    assert server_browser_url() == "http://127.0.0.1:8765/"
    open_browser.assert_called_once_with("http://127.0.0.1:8765/", new=1)


def test_migration_plan_needs_action_detects_version_change() -> None:
    plan = db.MigrationPlan(
        current_version=12,
        target_version=13,
        imported_files=0,
        duplicate_findings=0,
        creates_file_sources=False,
    )

    assert migration_plan_needs_action(plan)


def test_migration_plan_needs_action_detects_current_schema_repairs() -> None:
    plan = db.MigrationPlan(
        current_version=13,
        target_version=13,
        imported_files=0,
        duplicate_findings=0,
        creates_file_sources=False,
        refreshes_performance_indexes=True,
    )

    assert migration_plan_needs_action(plan)


def test_migration_plan_needs_action_accepts_current_database() -> None:
    plan = db.MigrationPlan(
        current_version=13,
        target_version=13,
        imported_files=0,
        duplicate_findings=0,
        creates_file_sources=False,
    )

    assert not migration_plan_needs_action(plan)


def test_collection_needs_migration_detects_old_schema_version(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    db.init_database(collection)
    conn = db.connect(collection)
    try:
        conn.execute("UPDATE meta SET value = '12' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    assert collection_needs_migration(collection)


def test_insightface_status_is_ready_when_dependencies_are_available() -> None:
    with (
        patch("bildebank.face.insightface_runtime_error", return_value=None),
        patch("importlib.util.find_spec", return_value=object()),
    ):
        status = insightface_dependency_status()

    assert status.status == "Klar"


def test_insightface_status_is_missing_when_insightface_is_missing() -> None:
    with (
        patch(
            "bildebank.face.insightface_runtime_error",
            return_value="InsightFace er ikke installert. Kjør install-insightface.ps1 fra programmappen.",
        ),
        patch("importlib.util.find_spec", return_value=object()),
    ):
        status = insightface_dependency_status()

    assert status.status == "Mangler"
    assert "insightface" in status.detail


def test_insightface_status_is_missing_when_onnxruntime_is_missing() -> None:
    with (
        patch("bildebank.face.insightface_runtime_error", return_value=None),
        patch("importlib.util.find_spec", return_value=None),
    ):
        status = insightface_dependency_status()

    assert status.status == "Mangler"
    assert "onnxruntime" in status.detail


def test_insightface_status_is_error_when_insightface_cannot_load() -> None:
    with (
        patch(
            "bildebank.face.insightface_runtime_error",
            return_value="InsightFace er installert, men kan ikke lastes: runtime-feil",
        ),
        patch("importlib.util.find_spec", return_value=object()),
    ):
        status = insightface_dependency_status()

    assert status.status == "Feil"
    assert "runtime-feil" in status.detail


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


def test_openclip_status_reports_installed_when_module_exists() -> None:
    with patch("importlib.util.find_spec", return_value=object()):
        assert openclip_dependency_status() == "Installert"


def test_openclip_status_reports_missing_when_module_is_missing() -> None:
    with patch("importlib.util.find_spec", return_value=None):
        assert openclip_dependency_status() == "Mangler"


def test_openclip_model_status_reports_available_when_model_file_exists(tmp_path: Path) -> None:
    (tmp_path / "bildebank-config.toml").write_text(
        """
[openclip]
model_root = ".bildebank-openclip"
model_name = "ViT-B-32"
pretrained = "laion2b_s34b_b79k"
""",
        encoding="utf-8",
    )
    model_dir = tmp_path / ".bildebank-openclip" / "models--laion"
    model_dir.mkdir(parents=True)
    (model_dir / "open_clip_pytorch_model.bin").write_bytes(b"model")

    status = openclip_model_status(tmp_path)

    assert status.model_name == "ViT-B-32"
    assert status.pretrained == "laion2b_s34b_b79k"
    assert status.status == "Tilgjengelig"


def test_openclip_model_status_reports_missing_when_model_file_is_missing(tmp_path: Path) -> None:
    (tmp_path / "bildebank-config.toml").write_text(
        """
[openclip]
model_root = ".bildebank-openclip"
model_name = "ViT-B-32"
pretrained = "laion2b_s34b_b79k"
""",
        encoding="utf-8",
    )

    status = openclip_model_status(tmp_path)

    assert status.model_name == "ViT-B-32"
    assert status.pretrained == "laion2b_s34b_b79k"
    assert status.status == "Mangler"


def test_insightface_model_status_reports_downloaded_selected_model(tmp_path: Path) -> None:
    (tmp_path / "bildebank-config.toml").write_text(
        """
[face_recognition]
model_root = ".bildebank-insightface"
model_name = "buffalo_l"
""",
        encoding="utf-8",
    )
    model_dir = tmp_path / ".bildebank-insightface" / "models" / "buffalo_l"
    model_dir.mkdir(parents=True)
    (model_dir / "det_10g.onnx").write_bytes(b"model")

    status = insightface_model_status(tmp_path)

    assert status.model_name == "buffalo_l"
    assert status.status == "Lastet ned"


def test_insightface_model_status_reports_missing_selected_model(tmp_path: Path) -> None:
    (tmp_path / "bildebank-config.toml").write_text(
        """
[face_recognition]
model_root = ".bildebank-insightface"
model_name = "buffalo_l"
""",
        encoding="utf-8",
    )

    status = insightface_model_status(tmp_path)

    assert status.model_name == "buffalo_l"
    assert status.status == "Mangler"


def test_subprocess_output_encoding_uses_locale() -> None:
    with patch("locale.getpreferredencoding", return_value="cp1252"):
        assert subprocess_output_encoding() == "cp1252"


def test_progress_log_key_recognizes_progress_updates_only() -> None:
    assert progress_log_key("Thumbnails: kontrollert=25/84, sjekket=25") == "Thumbnails"
    assert progress_log_key("Check-source: kontrollert=4/10, filer=4") == "Check-source"
    assert progress_log_key("geo-scan: scannet=12/40, gps=3, feil=0") == "geo-scan"
    assert progress_log_key("Import: importert=2/10, duplikater=1") == "Import"
    assert progress_log_key("Rescan-source: kontrollert=2/10, nye=1") == "Rescan-source"
    assert progress_log_key(" 59%|#####     | 206485/352210 [00:13<00:08, 16523.67KB/s]") == "tqdm-progress"
    assert progress_log_key("Thumbnails: 84 filer skal kontrolleres.") is None
    assert progress_log_key("Thumbnails: ferdig kontrollert 84/84 filer.") is None
    assert progress_log_key("Import fullført.") is None


def test_registered_sources_reads_sources_from_collection_database(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    source = tmp_path / "bilder"
    source.mkdir()
    db.init_database(collection)
    conn = db.connect(collection)
    try:
        db.add_named_source(conn, source, "Bilder")
        conn.commit()
    finally:
        conn.close()

    sources = registered_sources(collection)

    assert len(sources) == 1
    assert sources[0].name == "Bilder"
    assert sources[0].path == source.resolve()


def test_rescan_source_candidates_excludes_superseded_sources(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    first = tmp_path / "første"
    second = tmp_path / "andre"
    first.mkdir()
    second.mkdir()
    db.init_database(collection)
    conn = db.connect(collection)
    try:
        first_id = db.add_named_source(conn, first, "Første")
        second_id = db.add_named_source(conn, second, "Andre")
        conn.execute(
            "UPDATE sources SET status = 'superseded', superseded_by_source_id = ? WHERE id = ?",
            (second_id, first_id),
        )
        conn.commit()
    finally:
        conn.close()

    candidates = rescan_source_candidates(registered_sources(collection))

    assert [source.name for source in candidates] == ["Andre"]
