from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.launcher_main_tab import MainTab, open_server_browser_window, server_browser_url
from bildebank.launcher_status import LauncherUpdateStatus


class FakeButton:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def configure(self, **kwargs: object) -> None:
        self.options.update(kwargs)


class FakeWidget(FakeButton):
    def __init__(self, parent: FakeWidget | None = None, **options: object) -> None:
        super().__init__()
        self.parent = parent
        self.options.update(options)
        self.children: list[FakeWidget] = []
        if parent is not None:
            parent.children.append(self)

    def destroy(self) -> None:
        if self.parent is not None:
            self.parent.children.remove(self)

    def grid(self, *_args: object, **_kwargs: object) -> None:
        pass

    def winfo_children(self) -> list[FakeWidget]:
        return list(self.children)


def bare_main_tab(collection_path: Path) -> MainTab:
    tab = MainTab.__new__(MainTab)
    tab._get_collection_path = lambda: collection_path
    tab._is_busy = lambda: False
    tab.choose_collection_button = FakeButton()
    tab.create_collection_button = FakeButton()
    tab.start_server_button = FakeButton()
    tab.backup_button = FakeButton()
    tab.update_button = FakeButton()
    tab.update_button_icons = {}
    tab.update_status = LauncherUpdateStatus("current")
    tab.update_checking = False
    tab.migration_required = False
    tab.migration_status_error = None
    return tab


def test_main_tab_refresh_builds_normal_and_migration_actions(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.button_frame = FakeWidget()
    tab._button = FakeWidget
    tab._add_tooltip = lambda _widget, _text: None
    tab._on_close = lambda: None
    tab.create_collection_tooltip = SimpleNamespace(text="", hide=lambda: None)
    tab.padx = 4
    tab.pady = 4

    with patch("bildebank.launcher_main_tab.is_collection_created", return_value=True):
        state = tab.refresh()
        assert state.available
        assert [button.options["text"] for button in state.buttons] == [
            "Start Bildebank i nettleser",
            "Se etter oppdateringer",
            "Ta backup",
        ]

        tab.migration_required = True
        state = tab.refresh()
        assert not state.available
        assert [button.options["text"] for button in state.buttons] == [
            "Migrer",
            "Avslutt uten å migrere",
        ]


def test_open_server_browser_window_opens_default_run_server_url() -> None:
    with patch("webbrowser.open", return_value=True) as open_browser:
        assert open_server_browser_window()

    assert server_browser_url() == "http://127.0.0.1:8765/"
    open_browser.assert_called_once_with("http://127.0.0.1:8765/", new=1)


def test_update_status_refresh_starts_background_worker(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab._set_launcher_buttons_enabled = lambda _enabled: None

    with patch("bildebank.launcher_main_tab.threading.Thread") as thread:
        tab.start_update_status_refresh()

    thread.assert_called_once_with(target=tab._update_status_worker, daemon=True)
    thread.return_value.start.assert_called_once_with()
    assert tab.update_checking
    assert tab.update_status.status == "checking"


def test_update_status_worker_posts_result_to_ui(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    callbacks: list[object] = []
    finished: list[LauncherUpdateStatus] = []
    expected = LauncherUpdateStatus("available", commits_behind=2)
    tab._post_to_ui = lambda callback: callbacks.append(callback) or True
    tab._update_status_finished = finished.append

    with patch(
        "bildebank.launcher_main_tab.check_launcher_update_status",
        return_value=expected,
    ):
        tab._update_status_worker()

    assert len(callbacks) == 1
    callback = callbacks[0]
    assert callable(callback)
    callback()
    assert finished == [expected]


def test_update_button_text_reflects_update_status(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")

    tab.update_status = LauncherUpdateStatus("checking")
    assert tab._update_button_text() == "Ser etter oppdateringer ..."

    tab.update_status = LauncherUpdateStatus("available", commits_behind=1)
    assert tab._update_button_text() == "Installer oppdatering"

    tab.update_status = LauncherUpdateStatus("current")
    assert tab._update_button_text() == "Se etter oppdateringer"

    tab.update_status = LauncherUpdateStatus("error", "nettverksfeil")
    assert tab._update_button_text() == "Se etter oppdateringer"


def test_apply_update_button_state_updates_label_and_disables_while_checking(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.update_status = LauncherUpdateStatus("checking")

    tab._apply_update_button_state()

    assert tab.update_button.options["text"] == "Ser etter oppdateringer ..."
    assert tab.update_button.options["image"] == ""
    assert tab.update_button.options["compound"] == "none"
    assert tab.update_button.options["state"] == "disabled"


def test_apply_update_button_state_uses_icon_when_available(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    search_icon = object()
    tab.update_button_icons = {"search": search_icon}

    tab._apply_update_button_state()

    assert tab.update_button.options["text"] == "Se etter oppdateringer"
    assert tab.update_button.options["image"] is search_icon
    assert tab.update_button.options["compound"] == "left"


def test_update_button_icon_uses_green_check_only_for_available(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    search_icon = object()
    green_check_icon = object()
    tab.update_button_icons = {"search": search_icon, "green-check": green_check_icon}

    tab.update_status = LauncherUpdateStatus("checking")
    assert tab._update_button_icon() is search_icon
    tab.update_status = LauncherUpdateStatus("current")
    assert tab._update_button_icon() is search_icon
    tab.update_status = LauncherUpdateStatus("error")
    assert tab._update_button_icon() is search_icon
    tab.update_status = LauncherUpdateStatus("available")
    assert tab._update_button_icon() is green_check_icon


def test_update_status_finished_shows_available_update_button(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    logged: list[str] = []
    tab.update_checking = True
    tab._log = logged.append
    tab._set_launcher_buttons_enabled = lambda enabled: None

    tab._update_status_finished(LauncherUpdateStatus("available", commits_behind=3))

    assert tab.update_checking is False
    assert tab.update_button.options["text"] == "Installer oppdatering"
    assert logged == []


def test_update_status_finished_logs_error_and_returns_to_check_button(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    logged: list[str] = []
    tab.update_checking = True
    tab._log = logged.append
    tab._set_launcher_buttons_enabled = lambda enabled: None

    tab._update_status_finished(LauncherUpdateStatus("error", "ingen upstream"))

    assert tab.update_button.options["text"] == "Se etter oppdateringer"
    assert logged == ["Oppdateringssjekk feilet: ingen upstream"]


def test_update_status_finished_logs_skipped_update_check_without_error(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    logged: list[str] = []
    tab.update_checking = True
    tab._log = logged.append
    tab._set_launcher_buttons_enabled = lambda enabled: None

    tab._update_status_finished(LauncherUpdateStatus("skipped", "SSH-remote"))

    assert tab.update_button.options["text"] == "Se etter oppdateringer"
    assert logged == ["Oppdateringssjekk hoppet over: SSH-remote"]


def test_pyproject_includes_launcher_button_icons_as_package_data() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    package_data = pyproject["tool"]["setuptools"]["package-data"]["bildebank"]

    assert "assets/icons/*.png" in package_data


def test_update_button_click_runs_update_only_when_update_is_available(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    actions: list[str] = []
    tab._run_update = lambda: actions.append("update")
    tab.start_update_status_refresh = lambda: actions.append("check")

    tab.update_status = LauncherUpdateStatus("available")
    tab._on_update_button_clicked()
    tab.update_status = LauncherUpdateStatus("current")
    tab._on_update_button_clicked()
    tab.update_status = LauncherUpdateStatus("error")
    tab._on_update_button_clicked()

    assert actions == ["update", "check", "check"]


def test_main_action_buttons_without_collection_keep_update_available(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    for button in (tab.start_server_button, tab.backup_button, tab.update_button):
        button.configure(state="normal")

    with patch("bildebank.launcher_main_tab.is_collection_created", return_value=False):
        tab.set_buttons_enabled(True)

    assert tab.choose_collection_button.options["state"] == "normal"
    assert tab.create_collection_button.options["state"] == "normal"
    assert tab.start_server_button.options["state"] == "disabled"
    assert tab.backup_button.options["state"] == "disabled"
    assert tab.update_button.options["state"] == "normal"


def test_main_action_buttons_with_collection_disable_create(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    for button in (tab.start_server_button, tab.backup_button, tab.update_button):
        button.configure(state="normal")

    with patch("bildebank.launcher_main_tab.is_collection_created", return_value=True):
        tab.set_buttons_enabled(True)

    assert tab.choose_collection_button.options["state"] == "normal"
    assert tab.create_collection_button.options["state"] == "disabled"
    assert tab.start_server_button.options["state"] == "normal"
    assert tab.backup_button.options["state"] == "normal"
    assert tab.update_button.options["state"] == "normal"


def test_migration_requirement_disables_collection_controls(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.migration_required = True

    with patch("bildebank.launcher_main_tab.is_collection_created", return_value=True):
        tab.set_buttons_enabled(True)

    assert tab.choose_collection_button.options["state"] == "disabled"
    assert tab.create_collection_button.options["state"] == "disabled"


def test_create_collection_tooltip_explains_disabled_existing_collection(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")

    assert tab._create_collection_tooltip(True) == "Mappen er allerede en bildesamling."
    assert "Lag en bildesamling" in tab._create_collection_tooltip(False)


def test_main_tab_backup_runs_dry_run_before_confirmed_backup(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    backup_parent = tmp_path / "backup"
    tab = bare_main_tab(collection)
    calls: list[tuple[list[str], dict[str, object]]] = []
    questions: list[dict[str, object]] = []
    tab._log = lambda _message: None
    tab._refresh_launcher = lambda: None
    tab._run_waiting_command = lambda command, **options: calls.append((command, options))
    tab._show_log_review_question = (
        lambda _title, _message, **options: questions.append(options)
    )

    with patch("tkinter.filedialog.askdirectory", return_value=str(backup_parent)) as chooser:
        tab._start_backup_flow()

    chooser.assert_called_once_with(
        title="Velg backup-plassering",
        initialdir=str(collection.parent),
    )
    assert calls[0][0][-3:] == ["backup", "--dry-run", str(backup_parent)]
    assert calls[0][1]["cancellable"] is True

    on_dry_run_success = calls[0][1]["on_success"]
    assert callable(on_dry_run_success)
    on_dry_run_success()
    assert questions[0]["yes_text"] == "Kjør backup"

    on_yes = questions[0]["on_yes"]
    assert callable(on_yes)
    on_yes()
    assert "--dry-run" not in calls[1][0]
    assert calls[1][0][-2:] == ["backup", str(backup_parent)]
    assert calls[1][1]["cancellable"] is True


def test_start_server_stops_when_migration_is_required(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    actions: list[str] = []
    tab.server_process = None
    tab.update_migration_status = lambda: setattr(tab, "migration_required", True)
    tab._refresh_launcher = lambda: actions.append("refresh")
    tab._show_migration_required_dialog = lambda: actions.append("dialog")

    with patch("bildebank.launcher_main_tab.subprocess.Popen") as popen:
        tab._start_server()

    popen.assert_not_called()
    assert actions == ["refresh", "dialog"]


def test_stop_server_process_terminates_running_server(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    logged: list[str] = []
    process = SimpleNamespace(
        poll=lambda: None,
        terminate=lambda: logged.append("terminate"),
        wait=lambda *, timeout: logged.append(f"wait:{timeout}"),
    )
    tab.server_process = process
    tab._log = logged.append

    tab.stop_server_process()

    assert tab.server_process is None
    assert logged == [
        "Stopper Bildebank-server ...",
        "terminate",
        "wait:5",
        "Bildebank-server stoppet.",
    ]
