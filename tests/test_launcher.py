from __future__ import annotations

import inspect
import os
import signal
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.launcher import (
    BildebankLauncher,
    InsightFaceDependencyStatus,
    InsightFaceModelStatus,
    LauncherConfig,
    LauncherUpdateStatus,
    OpenClipModelStatus,
    RegisteredPerson,
    check_launcher_update_status,
    check_source_command,
    close_blocked_by_running_command,
    cleanup_pending_deletes_apply_command,
    cleanup_pending_deletes_list_command,
    collection_needs_migration,
    create_command,
    default_collection_path,
    deep_doctor_command,
    dependency_setup_button_state,
    doctor_command,
    download_face_model_command,
    export_person_command,
    face_model_download_button_state,
    face_scan_command,
    geo_scan_command,
    image_scan_command,
    import_command,
    insightface_dependency_status,
    insightface_install_command,
    insightface_model_status,
    interrupt_process,
    interruptible_command_creationflags,
    is_collection_created,
    launcher_command,
    load_launcher_config,
    make_browser_command,
    make_people_browser_command,
    make_person_browser_command,
    make_thumbnails_command,
    migrate_command,
    migration_plan_needs_action,
    open_server_browser_window,
    openclip_dependency_status,
    openclip_install_command,
    openclip_model_status,
    progress_log_key,
    registered_persons,
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
    assert download_face_model_command()[-1:] == ["download-face-model"]


def test_check_launcher_update_status_fetches_and_detects_available_update(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path]] = []

    def fake_run(command: list[str], *, cwd: Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        if command[1:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="main\n")
        if command[1:] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return subprocess.CompletedProcess(command, 0, stdout="origin/main\n")
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
        (["git", "fetch", "--quiet"], tmp_path),
        (["git", "rev-list", "--count", "HEAD..@{u}"], tmp_path),
    ]


def test_check_launcher_update_status_detects_current_branch(tmp_path: Path) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="devel\n")
        if command[1:] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return subprocess.CompletedProcess(command, 0, stdout="origin/devel\n")
        if command[1:] == ["fetch", "--quiet"]:
            return subprocess.CompletedProcess(command, 0, stdout="")
        if command[1:] == ["rev-list", "--count", "HEAD..@{u}"]:
            return subprocess.CompletedProcess(command, 0, stdout="0\n")
        raise AssertionError(f"unexpected command: {command}")

    with patch("subprocess.run", side_effect=fake_run):
        status = check_launcher_update_status(tmp_path)

    assert status == LauncherUpdateStatus("current", "devel er oppdatert mot origin/devel")


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
        if command[1:] == ["fetch", "--quiet"]:
            return subprocess.CompletedProcess(command, 0, stdout="")
        if command[1:] == ["rev-list", "--count", "HEAD..@{u}"]:
            return subprocess.CompletedProcess(command, 0, stdout="ikke-et-tall\n")
        raise AssertionError(f"unexpected command: {command}")

    with patch("subprocess.run", side_effect=fake_run):
        status = check_launcher_update_status(tmp_path)

    assert status.status == "error"


def test_open_server_browser_window_opens_default_run_server_url() -> None:
    with patch("webbrowser.open", return_value=True) as open_browser:
        assert open_server_browser_window()

    assert server_browser_url() == "http://127.0.0.1:8765/"
    open_browser.assert_called_once_with("http://127.0.0.1:8765/", new=1)


def test_launcher_layout_source_defines_notebook_tabs_and_log_below_tabs() -> None:
    source = inspect.getsource(BildebankLauncher._build_gui)
    refresh_source = inspect.getsource(BildebankLauncher._refresh_state)

    assert "ttk.Notebook(outer)" in source
    assert 'self.notebook.add(self.main_tab, text="Bildebank")' in source
    assert 'self.notebook.add(self.import_tab, text="Import av bilder")' in source
    assert 'self.notebook.add(self.tools_tab, text="Verktøy")' in source
    assert 'self.notebook.add(self.setup_tab, text="Oppsett")' in source
    assert "insightface_frame = ttk.Frame(self.setup_tab)" in source
    assert 'ttk.Separator(self.setup_tab, orient="horizontal")' in source
    assert "openclip_frame = ttk.Frame(self.setup_tab)" in source
    assert "insightface_frame = ttk.Frame(self.tools_tab)" not in source
    assert "openclip_frame = ttk.Frame(self.tools_tab)" not in source
    assert "log_frame = ttk.Frame(outer)" in source
    assert "log_frame = ttk.Frame(self.notebook)" not in source
    assert "footer = ttk.Frame(outer)" in source
    assert "ttk.Style(self.root).configure(BUTTON_STYLE, padding=BUTTON_PADDING)" in source
    assert "self.cancel_command_button = self._button(" in source
    assert 'text="Avbryt jobb"' in source
    assert "self.exit_button = self._button(" in source
    assert 'text="Avslutt bildebank kontrollpanel"' in source
    assert 'text="Avslutt bildebank kontrollpanel"' not in refresh_source


def test_launcher_button_helper_uses_launcher_button_style() -> None:
    source = inspect.getsource(BildebankLauncher._button)

    assert 'kwargs.setdefault("style", BUTTON_STYLE)' in source
    assert "return self.ttk.Button(parent, **kwargs)" in source


def test_launcher_initializes_dependency_status_asynchronously() -> None:
    init_source = inspect.getsource(BildebankLauncher.__init__)
    refresh_source = inspect.getsource(BildebankLauncher._refresh_state)
    start_source = inspect.getsource(BildebankLauncher._start_dependency_status_refresh)
    worker_source = inspect.getsource(BildebankLauncher._dependency_status_worker)

    assert "self._start_dependency_status_refresh()" in init_source
    assert "insightface_dependency_status()" not in init_source
    assert "openclip_model_status()" not in init_source
    assert "insightface_dependency_status()" not in refresh_source
    assert "openclip_model_status()" not in refresh_source
    assert "threading.Thread" in start_source
    assert "self._post_to_tk(" in worker_source


def test_launcher_initializes_update_status_asynchronously() -> None:
    init_source = inspect.getsource(BildebankLauncher.__init__)
    start_source = inspect.getsource(BildebankLauncher._start_update_status_refresh)
    worker_source = inspect.getsource(BildebankLauncher._update_status_worker)

    assert "self._start_update_status_refresh()" in init_source
    assert "check_launcher_update_status()" not in init_source
    assert "threading.Thread" in start_source
    assert "self._post_to_tk(" in worker_source


class FakeButton:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def configure(self, **kwargs: object) -> None:
        self.options.update(kwargs)


def test_update_button_text_reflects_update_status() -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)

    launcher.update_status = LauncherUpdateStatus("checking")
    assert launcher._update_button_text() == "Ser etter oppdateringer ..."

    launcher.update_status = LauncherUpdateStatus("available", commits_behind=1)
    assert launcher._update_button_text() == "Installer oppdatering"

    launcher.update_status = LauncherUpdateStatus("current")
    assert launcher._update_button_text() == "Se etter oppdateringer"

    launcher.update_status = LauncherUpdateStatus("error", "nettverksfeil")
    assert launcher._update_button_text() == "Se etter oppdateringer"


def test_apply_update_button_state_updates_label_and_disables_while_checking() -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    button = FakeButton()
    launcher.update_button = button
    launcher.update_button_icons = {}
    launcher.busy = False
    launcher.update_status = LauncherUpdateStatus("checking")

    launcher._apply_update_button_state()

    assert button.options["text"] == "Ser etter oppdateringer ..."
    assert button.options["image"] == ""
    assert button.options["compound"] == "none"
    assert button.options["state"] == "disabled"


def test_apply_update_button_state_uses_icon_when_available() -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    button = FakeButton()
    search_icon = object()
    launcher.update_button = button
    launcher.update_button_icons = {"search": search_icon}
    launcher.busy = False
    launcher.update_status = LauncherUpdateStatus("current")

    launcher._apply_update_button_state()

    assert button.options["text"] == "Se etter oppdateringer"
    assert button.options["image"] is search_icon
    assert button.options["compound"] == "left"


def test_update_button_icon_uses_green_check_only_for_available() -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    search_icon = object()
    green_check_icon = object()
    launcher.update_button_icons = {"search": search_icon, "green-check": green_check_icon}

    launcher.update_status = LauncherUpdateStatus("checking")
    assert launcher._update_button_icon() is search_icon

    launcher.update_status = LauncherUpdateStatus("current")
    assert launcher._update_button_icon() is search_icon

    launcher.update_status = LauncherUpdateStatus("error")
    assert launcher._update_button_icon() is search_icon

    launcher.update_status = LauncherUpdateStatus("available")
    assert launcher._update_button_icon() is green_check_icon


def test_update_status_finished_shows_available_update_button() -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    button = FakeButton()
    logged: list[str] = []
    launcher.update_button = button
    launcher.update_button_icons = {}
    launcher.busy = False
    launcher.update_checking = True
    launcher._log = logged.append
    launcher._set_buttons_enabled = lambda enabled: None

    launcher._update_status_finished(LauncherUpdateStatus("available", commits_behind=3))

    assert launcher.update_checking is False
    assert button.options["text"] == "Installer oppdatering"
    assert logged == []


def test_update_status_finished_logs_error_and_returns_to_check_button() -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    button = FakeButton()
    logged: list[str] = []
    launcher.update_button = button
    launcher.update_button_icons = {}
    launcher.busy = False
    launcher.update_checking = True
    launcher._log = logged.append
    launcher._set_buttons_enabled = lambda enabled: None

    launcher._update_status_finished(LauncherUpdateStatus("error", "ingen upstream"))

    assert button.options["text"] == "Se etter oppdateringer"
    assert logged == ["Oppdateringssjekk feilet: ingen upstream"]


def test_pyproject_includes_launcher_button_icons_as_package_data() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    package_data = pyproject["tool"]["setuptools"]["package-data"]["bildebank"]

    assert "assets/icons/*.png" in package_data


def test_update_button_click_runs_update_only_when_update_is_available() -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    actions: list[str] = []
    launcher._run_update = lambda: actions.append("update")
    launcher._start_update_status_refresh = lambda: actions.append("check")

    launcher.update_status = LauncherUpdateStatus("available")
    launcher._on_update_button_clicked()
    launcher.update_status = LauncherUpdateStatus("current")
    launcher._on_update_button_clicked()
    launcher.update_status = LauncherUpdateStatus("error")
    launcher._on_update_button_clicked()

    assert actions == ["update", "check", "check"]


def test_post_to_tk_ignores_callbacks_after_close_started() -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher.closing = True

    class FakeRoot:
        def after(self, *_args: object) -> None:
            raise AssertionError("after should not be called while closing")

    launcher.root = FakeRoot()

    assert not launcher._post_to_tk(lambda: None)


def test_dependency_status_finished_logs_error_details() -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher.busy = False
    launcher.dependency_status_refreshing = True
    logged: list[str] = []
    launcher._log = logged.append
    launcher._apply_dependency_status_values = lambda: None
    launcher._set_buttons_enabled = lambda enabled: None

    launcher._dependency_status_finished(
        InsightFaceDependencyStatus("Feil", "runtime-feil"),
        InsightFaceModelStatus("buffalo_l", "Mangler", "forventet mangler"),
        "Feil: open_clip-feil",
        OpenClipModelStatus("ViT-B-32", "laion", "Feil", "modell-feil"),
    )

    assert logged == [
        "InsightFace-status feilet: runtime-feil",
        "OpenCLIP-modell-status feilet: modell-feil",
    ]

    logged.clear()
    launcher._dependency_status_finished(
        InsightFaceDependencyStatus("Feil"),
        InsightFaceModelStatus("buffalo_l", "Klar"),
        "Mangler",
        OpenClipModelStatus("ViT-B-32", "laion", "Mangler", "forventet mangler"),
    )

    assert logged == []


def test_load_dependency_status_calls_openclip_model_status() -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    expected_openclip_model_status = OpenClipModelStatus("ViT-B-32", "laion", "Tilgjengelig", "modellmappe")

    with (
        patch("bildebank.launcher.insightface_dependency_status", return_value=InsightFaceDependencyStatus("Klar")),
        patch("bildebank.launcher.insightface_model_status", return_value=InsightFaceModelStatus("buffalo_l", "Lastet ned")),
        patch("bildebank.launcher.openclip_dependency_status", return_value="Installert"),
        patch("bildebank.launcher.openclip_model_status", return_value=expected_openclip_model_status),
    ):
        status = launcher._load_dependency_status()

    assert status == (
        InsightFaceDependencyStatus("Klar"),
        InsightFaceModelStatus("buffalo_l", "Lastet ned"),
        "Installert",
        expected_openclip_model_status,
    )


def test_select_source_does_not_run_nested_tk_event_loop() -> None:
    source = inspect.getsource(BildebankLauncher._select_source)

    assert "self.root.update()" not in source
    assert "after_idle" in source


def test_select_person_does_not_run_nested_tk_event_loop() -> None:
    source = inspect.getsource(BildebankLauncher._select_person)

    assert "self.root.update()" not in source
    assert "after_idle" in source
    assert 'state="readonly"' in source


def test_launcher_source_exposes_export_person_dry_run_flow() -> None:
    refresh_source = inspect.getsource(BildebankLauncher._refresh_state)
    flow_source = inspect.getsource(BildebankLauncher._start_export_person_flow)
    dry_run_source = inspect.getsource(BildebankLauncher._run_export_person_dry_run)
    confirm_source = inspect.getsource(BildebankLauncher._confirm_export_person)

    assert 'text="Eksporter person"' in refresh_source
    assert "_start_export_person_flow" in refresh_source
    assert "Denne funksjonen eksporterer en kopi av alle bildene av en person" in flow_source
    assert "description: str" in inspect.getsource(BildebankLauncher._select_person)
    assert "_select_person(" in flow_source
    assert "dry_run=True" in dry_run_source
    assert "_confirm_export_person" in dry_run_source
    assert "messagebox.askyesno" in confirm_source


def test_launcher_source_exposes_static_browser_commands_and_shared_hide_checkbox() -> None:
    refresh_source = inspect.getsource(BildebankLauncher._refresh_state)
    make_browser_source = inspect.getsource(BildebankLauncher._run_make_browser)
    start_make_person_source = inspect.getsource(BildebankLauncher._start_make_person_browser_flow)
    make_person_source = inspect.getsource(BildebankLauncher._run_make_person_browser)
    make_people_source = inspect.getsource(BildebankLauncher._run_make_people_browser)

    assert 'text="Lag HTML-browser"' in refresh_source
    assert 'text="Lag personbrowser"' in refresh_source
    assert 'text="Lag alle personbrowsere"' in refresh_source
    assert 'text=\'Skjul "Ute av fokus"\'' in refresh_source
    assert "static_browser_hide_out_of_focus_var" in refresh_source
    assert "de statiske HTML-browserkommandoene" in refresh_source
    assert "_select_person(" in start_make_person_source
    assert "Velg personen det skal lages statisk HTML-browser for." in start_make_person_source
    assert "static_browser_hide_out_of_focus_var.get()" in make_browser_source
    assert "make_browser_command(" in make_browser_source
    assert "hide_out_of_focus=hide_out_of_focus" in make_browser_source
    assert "static_browser_hide_out_of_focus_var.get()" in make_person_source
    assert "make_person_browser_command(" in make_person_source
    assert "hide_out_of_focus=hide_out_of_focus" in make_person_source
    assert "static_browser_hide_out_of_focus_var.get()" in make_people_source
    assert "make_people_browser_command(" in make_people_source
    assert "hide_out_of_focus=hide_out_of_focus" in make_people_source


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
        patch("bildebank.launcher.program_repo_root", return_value=tmp_path),
        patch("bildebank.launcher.load_config") as load_config,
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


def test_close_is_blocked_while_command_is_running() -> None:
    assert close_blocked_by_running_command(True)
    assert not close_blocked_by_running_command(False)


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


def test_subprocess_output_encoding_uses_locale() -> None:
    with patch("locale.getpreferredencoding", return_value="cp1252"):
        assert subprocess_output_encoding() == "cp1252"


def test_interruptible_command_creationflags_are_zero_outside_windows() -> None:
    with patch("bildebank.launcher.os.name", "posix"):
        assert interruptible_command_creationflags() == 0


def test_interrupt_process_sends_sigint_outside_windows() -> None:
    class FakeProcess:
        signal_sent: object | None = None

        def send_signal(self, value: object) -> None:
            self.signal_sent = value

    process = FakeProcess()

    with patch("bildebank.launcher.os.name", "posix"):
        interrupt_process(process)  # type: ignore[arg-type]

    assert process.signal_sent is signal.SIGINT


def test_face_scan_is_cancellable_from_launcher() -> None:
    source = inspect.getsource(BildebankLauncher._run_face_scan)

    assert "cancellable=True" in source


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
