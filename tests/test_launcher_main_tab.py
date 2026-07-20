from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.launcher_main_tab import (
    MainTab,
    ServerLaunchOptions,
    normalize_server_launch_options,
    open_server_browser_window,
    server_browser_url,
)
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
        self.grid_options: dict[str, object] = {}
        self.children: list[FakeWidget] = []
        if parent is not None:
            parent.children.append(self)

    def destroy(self) -> None:
        if self.parent is not None:
            self.parent.children.remove(self)

    def grid(self, *_args: object, **kwargs: object) -> None:
        self.grid_options.update(kwargs)

    def winfo_children(self) -> list[FakeWidget]:
        return list(self.children)


def bare_main_tab(collection_path: Path) -> MainTab:
    tab = MainTab.__new__(MainTab)
    tab._get_collection_path = lambda: collection_path
    tab._is_busy = lambda: False
    tab.choose_collection_button = FakeButton()
    tab.create_collection_button = FakeButton()
    tab.start_server_button = FakeButton()
    tab.update_button = FakeButton()
    tab.update_button_icons = {}
    tab.update_status = LauncherUpdateStatus("current")
    tab.update_checking = False
    tab.migration_required = False
    tab.migration_status_error = None
    tab.server_port = 8765
    tab.server_launch_options = None
    tab.root = object()
    return tab


def test_main_tab_refresh_builds_normal_and_migration_actions(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.button_frame = FakeWidget()
    tab._button = FakeWidget
    tooltips: dict[FakeWidget, str] = {}
    tab._add_tooltip = lambda widget, tooltip_text: tooltips.update({widget: tooltip_text})
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


def test_open_server_browser_window_uses_selected_port() -> None:
    with patch("webbrowser.open", return_value=True) as open_browser:
        assert open_server_browser_window(9000)

    assert server_browser_url(9000) == "http://127.0.0.1:9000/"
    open_browser.assert_called_once_with("http://127.0.0.1:9000/", new=1)


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
    for button in (
        tab.start_server_button,
        tab.update_button,
    ):
        button.configure(state="normal")

    with patch("bildebank.launcher_main_tab.is_collection_created", return_value=False):
        tab.set_buttons_enabled(True)

    assert tab.choose_collection_button.options["state"] == "normal"
    assert tab.create_collection_button.options["state"] == "normal"
    assert tab.start_server_button.options["state"] == "disabled"
    assert tab.update_button.options["state"] == "normal"


def test_main_action_buttons_with_collection_disable_create(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    for button in (
        tab.start_server_button,
        tab.update_button,
    ):
        button.configure(state="normal")

    with patch("bildebank.launcher_main_tab.is_collection_created", return_value=True):
        tab.set_buttons_enabled(True)

    assert tab.choose_collection_button.options["state"] == "normal"
    assert tab.create_collection_button.options["state"] == "disabled"
    assert tab.start_server_button.options["state"] == "normal"
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


def test_start_server_uses_advanced_options(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.server_process = None
    tab.update_migration_status = lambda: None
    tab._log = lambda _message: None

    with patch("bildebank.launcher_main_tab.subprocess.Popen") as popen:
        tab.start_server(port=9000, read_only=True)

    assert popen.call_args.args[0][-3:] == ["--port", "9000", "--read-only"]
    assert tab.server_port == 9000
    assert tab.server_launch_options == ServerLaunchOptions(
        port=9000,
        read_only=True,
        lan_share=False,
        slideshow=False,
        delay=None,
        filter=None,
    )


def test_start_server_uses_normalized_slideshow_options(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.server_process = None
    tab.update_migration_status = lambda: None
    tab._log = lambda _message: None

    with patch("bildebank.launcher_main_tab.subprocess.Popen") as popen:
        tab.start_server(
            port=9000,
            slideshow=True,
            delay=20,
            filter="  year=1999  ",
            confirm_lan_start=lambda: True,
        )

    command = popen.call_args.args[0]
    assert command[-7:] == [
        "--port",
        "9000",
        "--slideshow",
        "--delay",
        "20",
        "--filter",
        "year=1999",
    ]
    assert "--lan-share" not in command
    assert "--read-only" not in command
    assert tab.server_launch_options == normalize_server_launch_options(
        port=9000,
        slideshow=True,
        delay=20,
        filter="year=1999",
    )


def test_lan_start_cancel_does_not_start_process(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.server_process = None
    tab.update_migration_status = lambda: None
    tab._log = lambda _message: None

    with patch("bildebank.launcher_main_tab.subprocess.Popen") as popen:
        tab.start_server(port=8765, lan_share=True, confirm_lan_start=lambda: False)

    popen.assert_not_called()


def test_lan_start_confirmation_starts_with_lan_share_only(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.server_process = None
    tab.update_migration_status = lambda: None
    tab._log = lambda _message: None

    with patch("bildebank.launcher_main_tab.subprocess.Popen") as popen:
        tab.start_server(port=8765, lan_share=True, confirm_lan_start=lambda: True)

    command = popen.call_args.args[0]
    assert command[-3:] == ["--port", "8765", "--lan-share"]
    assert "--read-only" not in command


def test_running_server_opens_recorded_port_without_confirmation_or_new_process(
    tmp_path: Path,
) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.server_process = SimpleNamespace(poll=lambda: None)
    tab.server_port = 9000
    tab.server_launch_options = normalize_server_launch_options(
        port=9000,
        slideshow=True,
        delay=20,
        filter="year=1999",
    )
    tab.update_migration_status = lambda: None
    tab._log = lambda _message: None
    confirmations: list[str] = []

    with (
        patch("bildebank.launcher_main_tab.subprocess.Popen") as popen,
        patch("bildebank.launcher_main_tab.open_server_browser_window") as open_browser,
    ):
        tab.start_server(
            port=9000,
            slideshow=True,
            delay=20,
            filter="  year=1999 ",
            confirm_lan_start=lambda: confirmations.append("confirm") or True,
        )

    popen.assert_not_called()
    open_browser.assert_called_once_with(9000)
    assert confirmations == []


def test_changed_running_configuration_can_keep_existing_process(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    events: list[str] = []
    process = SimpleNamespace(
        poll=lambda: None,
        terminate=lambda: events.append("terminate"),
        wait=lambda *, timeout: events.append(f"wait:{timeout}"),
    )
    tab.server_process = process
    tab.server_launch_options = normalize_server_launch_options(port=8765)
    tab.update_migration_status = lambda: None
    tab._log = events.append

    with (
        patch("tkinter.messagebox.askokcancel", return_value=False) as ask_restart,
        patch("bildebank.launcher_main_tab.subprocess.Popen") as popen,
    ):
        tab.start_server(port=9000, read_only=True)

    ask_restart.assert_called_once()
    popen.assert_not_called()
    assert tab.server_process is process
    assert tab.server_launch_options == normalize_server_launch_options(port=8765)
    assert "terminate" not in events


def test_changed_configuration_stops_before_starting_slideshow(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    events: list[str] = []
    process = SimpleNamespace(
        poll=lambda: None,
        terminate=lambda: events.append("terminate"),
        wait=lambda *, timeout: events.append(f"wait:{timeout}"),
    )
    tab.server_process = process
    tab.server_launch_options = normalize_server_launch_options(port=8765)
    tab.update_migration_status = lambda: None
    tab._log = events.append

    def start_process(command: list[str]) -> SimpleNamespace:
        events.append("popen")
        assert command[-5:] == [
            "--slideshow",
            "--delay",
            "10",
            "--filter",
            "year=1999",
        ]
        return SimpleNamespace()

    with (
        patch(
            "tkinter.messagebox.askokcancel",
            side_effect=lambda *_args, **_kwargs: events.append("restart-confirm") or True,
        ),
        patch("bildebank.launcher_main_tab.subprocess.Popen", side_effect=start_process),
    ):
        tab.start_server(
            port=8765,
            slideshow=True,
            filter="year=1999",
            confirm_lan_start=lambda: events.append("lan-confirm") or True,
        )

    assert events.index("restart-confirm") < events.index("lan-confirm")
    assert events.index("lan-confirm") < events.index("terminate")
    assert events.index("terminate") < events.index("popen")
    assert tab.server_process is not process
    assert tab.server_launch_options == normalize_server_launch_options(
        port=8765,
        slideshow=True,
        filter="year=1999",
    )


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
