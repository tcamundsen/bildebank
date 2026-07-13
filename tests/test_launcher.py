from __future__ import annotations

import inspect
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.launcher import (
    BildebankLauncher,
    FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP,
    FACE_SCAN_TOOLTIP,
    IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP,
    IMAGE_SCAN_TOOLTIP,
    InsightFaceDependencyStatus,
    InsightFaceModelStatus,
    LauncherUpdateStatus,
    OpenClipModelStatus,
    close_blocked_by_running_command,
    open_server_browser_window,
    server_browser_url,
    source_is_collection_or_inside,
    suggest_import_name,
)


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
    assert 'text="Avslutt Bildebank"' in source
    assert 'text="Avslutt Bildebank"' not in refresh_source


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


def test_update_status_finished_logs_skipped_update_check_without_error() -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    button = FakeButton()
    logged: list[str] = []
    launcher.update_button = button
    launcher.update_button_icons = {}
    launcher.busy = False
    launcher.update_checking = True
    launcher._log = logged.append
    launcher._set_buttons_enabled = lambda enabled: None

    launcher._update_status_finished(LauncherUpdateStatus("skipped", "SSH-remote"))

    assert button.options["text"] == "Se etter oppdateringer"
    assert logged == ["Oppdateringssjekk hoppet over: SSH-remote"]


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


def launcher_with_main_action_buttons(collection_path: Path) -> BildebankLauncher:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher.collection_path = collection_path
    launcher.buttons = []
    launcher.choose_collection_button = FakeButton()
    launcher.create_collection_button = FakeButton()
    launcher.start_server_button = FakeButton()
    launcher.backup_button = FakeButton()
    launcher.face_scan_button = FakeButton()
    launcher.face_scan_tooltip = type("FakeTooltip", (), {"text": FACE_SCAN_TOOLTIP})()
    launcher.image_scan_button = FakeButton()
    launcher.image_scan_tooltip = type("FakeTooltip", (), {"text": IMAGE_SCAN_TOOLTIP})()
    launcher.update_button = FakeButton()
    launcher.buttons.extend(
        [
            launcher.start_server_button,
            launcher.backup_button,
            launcher.face_scan_button,
            launcher.image_scan_button,
            launcher.update_button,
        ]
    )
    launcher.update_button_icons = {}
    launcher.update_status = LauncherUpdateStatus("current")
    launcher.busy = False
    launcher.migration_required = False
    launcher.migration_status_error = None
    launcher.dependency_status_refreshing = False
    launcher.install_insightface_button = None
    launcher.install_openclip_button = None
    launcher.download_face_model_button = None
    launcher.exit_button = None
    launcher.cancel_command_button = None
    launcher.command_runner = SimpleNamespace(cancellable=False, cancel_requested=False)
    launcher.insightface_status = InsightFaceDependencyStatus("Klar")
    launcher.face_model_status = InsightFaceModelStatus("buffalo_l", "Lastet ned")
    launcher.openclip_status = "Installert"
    launcher.openclip_model_status = OpenClipModelStatus("ViT-B-32", "laion", "Tilgjengelig")
    return launcher


def test_main_action_buttons_without_collection_keep_update_available(tmp_path: Path) -> None:
    launcher = launcher_with_main_action_buttons(tmp_path / "samling")

    with patch("bildebank.launcher.is_collection_created", return_value=False):
        launcher._set_buttons_enabled(True)

    assert launcher.choose_collection_button.options["state"] == "normal"
    assert launcher.create_collection_button.options["state"] == "normal"
    assert launcher.start_server_button.options["state"] == "disabled"
    assert launcher.backup_button.options["state"] == "disabled"
    assert launcher.update_button.options.get("state", "normal") == "normal"


def test_main_action_buttons_with_collection_disable_create(tmp_path: Path) -> None:
    launcher = launcher_with_main_action_buttons(tmp_path / "samling")

    with patch("bildebank.launcher.is_collection_created", return_value=True):
        launcher._set_buttons_enabled(True)

    assert launcher.choose_collection_button.options["state"] == "normal"
    assert launcher.create_collection_button.options["state"] == "disabled"
    assert launcher.start_server_button.options["state"] == "normal"
    assert launcher.backup_button.options["state"] == "normal"
    assert launcher.update_button.options.get("state", "normal") == "normal"


def test_face_scan_button_enabled_and_tooltip_explains_missing_insightface(tmp_path: Path) -> None:
    launcher = launcher_with_main_action_buttons(tmp_path / "samling")
    launcher.insightface_status = InsightFaceDependencyStatus("Mangler", "mangler insightface")

    with patch("bildebank.launcher.is_collection_created", return_value=True):
        launcher._set_buttons_enabled(True)

    assert launcher.face_scan_button.options["state"] == "normal"
    assert launcher.face_scan_tooltip.text == FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP

    launcher.insightface_status = InsightFaceDependencyStatus("Klar")
    launcher._set_buttons_enabled(True)

    assert launcher.face_scan_button.options["state"] == "normal"
    assert launcher.face_scan_tooltip.text == FACE_SCAN_TOOLTIP


def test_face_scan_button_enabled_and_tooltip_explains_missing_face_model(tmp_path: Path) -> None:
    launcher = launcher_with_main_action_buttons(tmp_path / "samling")
    launcher.face_model_status = InsightFaceModelStatus("buffalo_l", "Mangler")

    with patch("bildebank.launcher.is_collection_created", return_value=True):
        launcher._set_buttons_enabled(True)

    assert launcher.face_scan_button.options["state"] == "normal"
    assert launcher.face_scan_tooltip.text == FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP


def test_image_scan_button_enabled_and_tooltip_explains_missing_openclip(tmp_path: Path) -> None:
    launcher = launcher_with_main_action_buttons(tmp_path / "samling")
    launcher.openclip_status = "Mangler"

    with patch("bildebank.launcher.is_collection_created", return_value=True):
        launcher._set_buttons_enabled(True)

    assert launcher.image_scan_button.options["state"] == "normal"
    assert launcher.image_scan_tooltip.text == IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP

    launcher.openclip_status = "Installert"
    launcher._set_buttons_enabled(True)

    assert launcher.image_scan_button.options["state"] == "normal"
    assert launcher.image_scan_tooltip.text == IMAGE_SCAN_TOOLTIP


def test_image_scan_button_enabled_and_tooltip_explains_missing_openclip_model(tmp_path: Path) -> None:
    launcher = launcher_with_main_action_buttons(tmp_path / "samling")
    launcher.openclip_model_status = OpenClipModelStatus("ViT-B-32", "laion", "Mangler")

    with patch("bildebank.launcher.is_collection_created", return_value=True):
        launcher._set_buttons_enabled(True)

    assert launcher.image_scan_button.options["state"] == "normal"
    assert launcher.image_scan_tooltip.text == IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP


def test_face_scan_preflight_installs_downloads_enables_and_scans(tmp_path: Path) -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher.collection_path = tmp_path / "samling"
    launcher.root = object()
    launcher.insightface_status = InsightFaceDependencyStatus("Mangler")
    launcher.face_model_status = InsightFaceModelStatus("buffalo_l", "Mangler")
    actions: list[str] = []
    launcher._face_recognition_enabled = lambda: False
    launcher._run_face_scan_insightface_install_step = lambda on_success: (actions.append("install"), on_success())
    launcher._run_face_scan_model_download_step = lambda on_success: (actions.append("download"), on_success())
    launcher._run_face_scan_enable_step = lambda on_success: (actions.append("enable"), on_success())
    launcher._start_face_scan_command = lambda: actions.append("scan")
    launcher._log = actions.append

    with (
        patch("tkinter.messagebox.askyesno", return_value=True) as askyesno,
        patch("bildebank.launcher.insightface_install_supported", return_value=True),
    ):
        launcher._run_face_scan()

    askyesno.assert_called_once()
    assert actions == ["install", "download", "enable", "scan"]


def test_face_scan_preflight_enables_disabled_config_before_scan(tmp_path: Path) -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher.collection_path = tmp_path / "samling"
    launcher.root = object()
    launcher.insightface_status = InsightFaceDependencyStatus("Klar")
    launcher.face_model_status = InsightFaceModelStatus("buffalo_l", "Lastet ned")
    actions: list[str] = []
    launcher._face_recognition_enabled = lambda: False
    launcher._run_face_scan_enable_step = lambda on_success: (actions.append("enable"), on_success())
    launcher._start_face_scan_command = lambda: actions.append("scan")
    launcher._log = actions.append

    with patch("tkinter.messagebox.askyesno", return_value=True) as askyesno:
        launcher._run_face_scan()

    askyesno.assert_called_once()
    assert actions == ["enable", "scan"]


def test_face_scan_enable_step_turns_on_face_recognition(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "bildebank-config.toml").write_text("[face_recognition]\nenabled = false\n", encoding="utf-8")
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher._log = lambda _message: None
    launcher._show_error = lambda _message, _exc: None
    actions: list[str] = []

    with patch("bildebank.launcher.program_repo_root", return_value=repo_root):
        launcher._run_face_scan_enable_step(lambda: actions.append("next"))

    assert actions == ["next"]
    assert "enabled = true" in (repo_root / "bildebank-config.toml").read_text(encoding="utf-8")


def test_face_scan_preflight_can_be_cancelled(tmp_path: Path) -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher.collection_path = tmp_path / "samling"
    launcher.root = object()
    launcher.insightface_status = InsightFaceDependencyStatus("Klar")
    launcher.face_model_status = InsightFaceModelStatus("buffalo_l", "Lastet ned")
    actions: list[str] = []
    launcher._face_recognition_enabled = lambda: False
    launcher._start_face_scan_command = lambda: actions.append("scan")
    launcher._log = actions.append

    with patch("tkinter.messagebox.askyesno", return_value=False):
        launcher._run_face_scan()

    assert actions == ["Ansiktsscan avbrutt."]


def test_image_scan_preflight_installs_enables_and_scans(tmp_path: Path) -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher.collection_path = tmp_path / "samling"
    launcher.root = object()
    launcher.openclip_status = "Mangler"
    launcher.openclip_model_status = OpenClipModelStatus("ViT-B-32", "laion", "Mangler")
    actions: list[str] = []
    launcher._image_search_enabled = lambda: False
    launcher._run_image_scan_openclip_install_step = lambda on_success: (actions.append("install"), on_success())
    launcher._run_image_scan_enable_step = lambda on_success: (actions.append("enable"), on_success())
    launcher._start_image_scan_command = lambda: actions.append("scan")
    launcher._log = actions.append

    with (
        patch("tkinter.messagebox.askyesno", return_value=True) as askyesno,
        patch("bildebank.launcher.openclip_install_supported", return_value=True),
    ):
        launcher._run_image_scan()

    askyesno.assert_called_once()
    assert actions == ["install", "enable", "scan"]


def test_image_scan_preflight_enables_disabled_config_before_scan(tmp_path: Path) -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher.collection_path = tmp_path / "samling"
    launcher.root = object()
    launcher.openclip_status = "Installert"
    launcher.openclip_model_status = OpenClipModelStatus("ViT-B-32", "laion", "Tilgjengelig")
    actions: list[str] = []
    launcher._image_search_enabled = lambda: False
    launcher._run_image_scan_enable_step = lambda on_success: (actions.append("enable"), on_success())
    launcher._start_image_scan_command = lambda: actions.append("scan")
    launcher._log = actions.append

    with patch("tkinter.messagebox.askyesno", return_value=True) as askyesno:
        launcher._run_image_scan()

    askyesno.assert_called_once()
    assert actions == ["enable", "scan"]


def test_image_scan_enable_step_turns_on_image_search(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "bildebank-config.toml").write_text("[image_search]\nenabled = false\n", encoding="utf-8")
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher._log = lambda _message: None
    launcher._show_error = lambda _message, _exc: None
    actions: list[str] = []

    with patch("bildebank.launcher.program_repo_root", return_value=repo_root):
        launcher._run_image_scan_enable_step(lambda: actions.append("next"))

    assert actions == ["next"]
    assert "enabled = true" in (repo_root / "bildebank-config.toml").read_text(encoding="utf-8")


def test_image_search_enabled_reads_openclip_config_field(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = repo_root / "bildebank-config.toml"
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher._log = lambda _message: None

    with patch("bildebank.launcher.program_repo_root", return_value=repo_root):
        config_path.write_text("[image_search]\nenabled = true\n", encoding="utf-8")
        assert launcher._image_search_enabled()
        config_path.write_text("[image_search]\nenabled = false\n", encoding="utf-8")
        assert not launcher._image_search_enabled()


def test_image_scan_openclip_install_finish_refreshes_launcher_status(tmp_path: Path) -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher.collection_path = tmp_path / "samling"
    launcher.busy = False
    launcher.migration_required = False
    launcher.migration_status_error = None
    launcher.dependency_status_refreshing = False
    launcher.buttons = []
    launcher.choose_collection_button = None
    launcher.create_collection_button = None
    launcher.start_server_button = None
    launcher.backup_button = None
    launcher.face_scan_button = None
    launcher.image_scan_button = None
    launcher.update_button = None
    launcher.install_insightface_button = None
    launcher.install_openclip_button = None
    launcher.download_face_model_button = None
    launcher.exit_button = None
    launcher.cancel_command_button = None
    launcher.command_runner = SimpleNamespace(cancellable=False, cancel_requested=False)
    launcher.update_status = LauncherUpdateStatus("current")
    launcher.update_button_icons = {}
    launcher._apply_update_button_state = lambda: None
    launcher._log = lambda _message: None
    launcher._apply_dependency_status_values = lambda: None
    actions: list[str] = []

    with (
        patch("bildebank.launcher.openclip_dependency_status", return_value="Installert"),
        patch(
            "bildebank.launcher.openclip_model_status",
            return_value=OpenClipModelStatus("ViT-B-32", "laion", "Tilgjengelig"),
        ),
        patch("bildebank.launcher.is_collection_created", return_value=True),
    ):
        launcher._image_scan_openclip_install_finished(lambda: actions.append("next"))

    assert launcher.openclip_status == "Installert"
    assert launcher.openclip_model_status.status == "Tilgjengelig"
    assert actions == ["next"]


def test_image_scan_preflight_can_be_cancelled(tmp_path: Path) -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)
    launcher.collection_path = tmp_path / "samling"
    launcher.root = object()
    launcher.openclip_status = "Installert"
    launcher.openclip_model_status = OpenClipModelStatus("ViT-B-32", "laion", "Tilgjengelig")
    actions: list[str] = []
    launcher._image_search_enabled = lambda: False
    launcher._start_image_scan_command = lambda: actions.append("scan")
    launcher._log = actions.append

    with patch("tkinter.messagebox.askyesno", return_value=False):
        launcher._run_image_scan()

    assert actions == ["Bildesøk-scan avbrutt."]


def test_create_collection_tooltip_explains_disabled_existing_collection() -> None:
    launcher = BildebankLauncher.__new__(BildebankLauncher)

    assert launcher._create_collection_tooltip(True) == "Mappen er allerede en bildesamling."
    assert "Lag en bildesamling" in launcher._create_collection_tooltip(False)


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
    assert "_show_log_review_question" in confirm_source
    assert "_run_export_person(person, destination_root)" in confirm_source


def test_launcher_source_exposes_backup_dry_run_flow() -> None:
    refresh_source = inspect.getsource(BildebankLauncher._refresh_state)
    flow_source = inspect.getsource(BildebankLauncher._start_backup_flow)
    dry_run_source = inspect.getsource(BildebankLauncher._run_backup_dry_run)
    confirm_source = inspect.getsource(BildebankLauncher._confirm_backup)
    backup_source = inspect.getsource(BildebankLauncher._run_backup)

    assert 'text="Ta backup"' in refresh_source
    assert "_start_backup_flow" in refresh_source
    assert "askdirectory" in flow_source
    assert 'title="Velg backup-plassering"' in flow_source
    assert "_run_backup_dry_run" in flow_source
    assert "dry_run=True" in dry_run_source
    assert "_confirm_backup" in dry_run_source
    assert "_show_log_review_question" in confirm_source
    assert "_run_backup(backup_parent)" in confirm_source
    assert "cancellable=True" in backup_source


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


def test_close_is_blocked_while_command_is_running() -> None:
    assert close_blocked_by_running_command(True)
    assert not close_blocked_by_running_command(False)


def test_face_scan_is_cancellable_from_launcher() -> None:
    source = inspect.getsource(BildebankLauncher._start_face_scan_command)

    assert "cancellable=True" in source
