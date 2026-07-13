from __future__ import annotations

import inspect
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.launcher import (
    BildebankLauncher,
    LauncherUpdateStatus,
    close_blocked_by_running_command,
    open_server_browser_window,
    server_browser_url,
)


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
    assert "self.import_tab = ImportTab(" in source
    assert 'self.notebook.add(self.import_tab.frame, text="Import av bilder")' in source
    assert "self.tools_tab = ToolsTab(" in source
    assert 'self.notebook.add(self.tools_tab.frame, text="Verktøy")' in source
    assert "self.setup = SetupTab(" in source
    assert 'self.notebook.add(self.setup.frame, text="Oppsett")' in source
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
    launcher.update_button = FakeButton()
    launcher.buttons.extend(
        [
            launcher.start_server_button,
            launcher.backup_button,
            launcher.update_button,
        ]
    )
    launcher.update_button_icons = {}
    launcher.update_status = LauncherUpdateStatus("current")
    launcher.busy = False
    launcher.migration_required = False
    launcher.migration_status_error = None
    launcher.tools_tab = None
    launcher.setup = SimpleNamespace(set_buttons_enabled=lambda *_args, **_kwargs: None)
    launcher.exit_button = None
    launcher.cancel_command_button = None
    launcher.command_runner = SimpleNamespace(cancellable=False, cancel_requested=False)
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


def test_close_is_blocked_while_command_is_running() -> None:
    assert close_blocked_by_running_command(True)
    assert not close_blocked_by_running_command(False)
