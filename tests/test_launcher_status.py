from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.launcher_status import (
    InsightFaceDependencyStatus,
    LauncherConfig,
    LauncherUpdateStatus,
    RegisteredPerson,
    check_launcher_update_status,
    collection_needs_migration,
    default_collection_path,
    dependency_setup_button_state,
    face_model_download_button_state,
    insightface_dependency_status,
    insightface_model_status,
    is_collection_created,
    load_launcher_config,
    migration_plan_needs_action,
    openclip_dependency_status,
    openclip_model_status,
    registered_persons,
    registered_sources,
    rescan_source_candidates,
    save_launcher_config,
)

def test_default_collection_path_uses_home_kode_bilde_samling() -> None:
    with patch("pathlib.Path.home", return_value=Path(r"C:\Users\tom")):
        assert default_collection_path() == Path(r"C:\Users\tom") / "kode" / "bilde-samling"


def test_load_config_uses_default_when_file_is_missing(tmp_path: Path) -> None:
    with (
        patch("pathlib.Path.home", return_value=tmp_path / "home"),
        patch("bildebank.launcher_status.program_repo_root", return_value=tmp_path / "repo"),
        patch("bildebank.launcher_status.db.find_target", return_value=None),
    ):
        config = load_launcher_config()

    assert config.collection_path == tmp_path / "home" / "kode" / "bilde-samling"


def test_save_and_load_launcher_config_uses_bildebank_config_toml(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    collection_path = tmp_path / "samling"

    with patch("bildebank.launcher_status.program_repo_root", return_value=repo_root):
        save_launcher_config(LauncherConfig(collection_path=collection_path))

    config_text = (repo_root / "bildebank-config.toml").read_text(encoding="utf-8")
    assert "[launcher]" in config_text
    assert tomllib.loads(config_text)["launcher"]["collection_path"] == str(collection_path)
    with (
        patch("bildebank.launcher_status.program_repo_root", return_value=repo_root),
        patch("bildebank.launcher_status.db.find_target", return_value=None),
    ):
        assert load_launcher_config().collection_path == collection_path


def test_load_launcher_config_prefers_current_collection_over_saved_path(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    saved_path = tmp_path / "gammel"
    current_path = tmp_path / "ny"

    with patch("bildebank.launcher_status.program_repo_root", return_value=repo_root):
        save_launcher_config(LauncherConfig(collection_path=saved_path))

    with (
        patch("bildebank.launcher_status.program_repo_root", return_value=repo_root),
        patch("bildebank.launcher_status.db.find_target", return_value=current_path),
    ):
        assert load_launcher_config().collection_path == current_path


def test_is_collection_created_uses_bildebank_database_marker(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    collection.mkdir()
    assert not is_collection_created(collection)

    db.init_database(collection)

    assert is_collection_created(collection)


def test_check_launcher_update_status_fetches_and_detects_available_update(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path]] = []

    def fake_run(command: list[str], *, cwd: Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        if command[1:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="main\n")
        if command[1:] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return subprocess.CompletedProcess(command, 0, stdout="origin/main\n")
        if command[1:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://github.com/example/bildebank.git\n")
        if command[1:] == ["fetch", "--quiet"]:
            return subprocess.CompletedProcess(command, 0, stdout="")
        if command[1:] == ["rev-list", "--count", "HEAD..@{u}"]:
            return subprocess.CompletedProcess(command, 0, stdout="2\n")
        raise AssertionError(f"unexpected command: {command}")

    with patch("subprocess.run", side_effect=fake_run):
        status = check_launcher_update_status(tmp_path)

    assert status == LauncherUpdateStatus(
        "available",
        "main ligger 2 commits bak origin/main",
        commits_behind=2,
    )
    assert calls == [
        (["git", "rev-parse", "--abbrev-ref", "HEAD"], tmp_path),
        (["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], tmp_path),
        (["git", "remote", "get-url", "origin"], tmp_path),
        (["git", "fetch", "--quiet"], tmp_path),
        (["git", "rev-list", "--count", "HEAD..@{u}"], tmp_path),
    ]


def test_check_launcher_update_status_detects_current_branch(tmp_path: Path) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="devel\n")
        if command[1:] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return subprocess.CompletedProcess(command, 0, stdout="origin/devel\n")
        if command[1:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://github.com/example/bildebank.git\n")
        if command[1:] == ["fetch", "--quiet"]:
            return subprocess.CompletedProcess(command, 0, stdout="")
        if command[1:] == ["rev-list", "--count", "HEAD..@{u}"]:
            return subprocess.CompletedProcess(command, 0, stdout="0\n")
        raise AssertionError(f"unexpected command: {command}")

    with patch("subprocess.run", side_effect=fake_run):
        status = check_launcher_update_status(tmp_path)

    assert status == LauncherUpdateStatus("current", "devel er oppdatert mot origin/devel")


def test_check_launcher_update_status_skips_ssh_remote_before_fetch(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="main\n")
        if command[1:] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return subprocess.CompletedProcess(command, 0, stdout="origin/main\n")
        if command[1:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, stdout="git@github.com:tom/bildebank.git\n")
        raise AssertionError(f"unexpected command: {command}")

    with patch("subprocess.run", side_effect=fake_run):
        status = check_launcher_update_status(tmp_path)

    assert status == LauncherUpdateStatus(
        "skipped",
        "hopper over automatisk oppdateringssjekk for SSH-remote origin",
    )
    assert ["git", "fetch", "--quiet"] not in calls


def test_check_launcher_update_status_handles_missing_upstream(tmp_path: Path) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="main\n")
        raise subprocess.CalledProcessError(128, command)

    with patch("subprocess.run", side_effect=fake_run):
        status = check_launcher_update_status(tmp_path)

    assert status.status == "error"
    assert status.commits_behind == 0


def test_check_launcher_update_status_handles_missing_git(tmp_path: Path) -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError("git")):
        status = check_launcher_update_status(tmp_path)

    assert status.status == "error"
    assert "git finnes ikke" in status.detail


def test_check_launcher_update_status_handles_git_output_errors(tmp_path: Path) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="main\n")
        if command[1:] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return subprocess.CompletedProcess(command, 0, stdout="origin/main\n")
        if command[1:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://github.com/example/bildebank.git\n")
        if command[1:] == ["fetch", "--quiet"]:
            return subprocess.CompletedProcess(command, 0, stdout="")
        if command[1:] == ["rev-list", "--count", "HEAD..@{u}"]:
            return subprocess.CompletedProcess(command, 0, stdout="ikke-et-tall\n")
        raise AssertionError(f"unexpected command: {command}")

    with patch("subprocess.run", side_effect=fake_run):
        status = check_launcher_update_status(tmp_path)

    assert status.status == "error"


def test_registered_persons_maps_face_person_rows(tmp_path: Path) -> None:
    rows = [
        {
            "name": "Kari",
            "confirmed_file_count": 12,
            "face_count": 14,
            "suggestion_count": 83,
            "updated_at": "2026-05-09 12:34:56",
        },
        {
            "name": "Ola",
            "confirmed_file_count": 2,
            "face_count": 3,
            "suggestion_count": 4,
            "updated_at": "2026-05-10 11:22:33",
        },
    ]

    with (
        patch("bildebank.launcher_status.program_repo_root", return_value=tmp_path),
        patch("bildebank.launcher_status.load_config") as load_config,
        patch("bildebank.face.list_persons", return_value=rows) as list_persons,
    ):
        load_config.return_value.face_recognition = object()
        persons = registered_persons(tmp_path / "samling")

    assert persons == [
        RegisteredPerson("Kari", 12, 14, 83, "2026-05-09 12:34:56"),
        RegisteredPerson("Ola", 2, 3, 4, "2026-05-10 11:22:33"),
    ]
    list_persons.assert_called_once_with(tmp_path / "samling", load_config.return_value.face_recognition)


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


def test_collection_needs_migration_handles_old_schema_without_full_validation(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    db.init_database(collection)
    conn = db.connect(collection)
    try:
        conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
        conn.execute("DROP INDEX idx_files_active_browser_order")
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


def test_openclip_status_reports_installed_when_module_exists() -> None:
    with patch("importlib.util.find_spec", return_value=object()):
        assert openclip_dependency_status() == "Installert"


def test_openclip_status_reports_missing_when_module_is_missing() -> None:
    with patch("importlib.util.find_spec", return_value=None):
        assert openclip_dependency_status() == "Mangler"


def test_dependency_setup_buttons_remain_enabled_when_dependencies_are_installed() -> None:
    assert (
        dependency_setup_button_state(
            enabled=True,
            migration_required=False,
            migration_status_error=None,
            install_supported=True,
        )
        == "normal"
    )


def test_dependency_setup_buttons_are_disabled_when_install_flow_is_not_supported() -> None:
    assert (
        dependency_setup_button_state(
            enabled=True,
            migration_required=False,
            migration_status_error=None,
            install_supported=False,
        )
        == "disabled"
    )


def test_face_model_download_button_is_enabled_when_insightface_is_ready() -> None:
    assert (
        face_model_download_button_state(
            enabled=True,
            migration_required=False,
            migration_status_error=None,
            insightface_status=InsightFaceDependencyStatus("Klar"),
        )
        == "normal"
    )


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


def test_rescan_source_candidates_returns_registered_sources(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    first = tmp_path / "første"
    second = tmp_path / "andre"
    first.mkdir()
    second.mkdir()
    db.init_database(collection)
    conn = db.connect(collection)
    try:
        db.add_named_source(conn, first, "Første")
        db.add_named_source(conn, second, "Andre")
        conn.commit()
    finally:
        conn.close()

    candidates = rescan_source_candidates(registered_sources(collection))

    assert [source.name for source in candidates] == ["Første", "Andre"]
