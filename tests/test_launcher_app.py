from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from bildebank.launcher_app import BUTTON_STYLE, LauncherApp, close_blocked_by_running_command
from bildebank.launcher_status import LauncherConfig


class FakeWidget:
    def __init__(self, parent: FakeWidget | None = None, **options: object) -> None:
        self.parent = parent
        self.options = options
        self.children: list[FakeWidget] = []
        self.grid_options: dict[str, object] = {}
        if parent is not None:
            parent.children.append(self)

    def columnconfigure(self, *_args: object, **_kwargs: object) -> None:
        pass

    def configure(self, **options: object) -> None:
        self.options.update(options)

    def grid(self, **options: object) -> None:
        self.grid_options = options

    def rowconfigure(self, *_args: object, **_kwargs: object) -> None:
        pass

    def yview(self, *_args: object) -> None:
        pass

    def set(self, *_args: object) -> None:
        pass


class FakeNotebook(FakeWidget):
    def __init__(self, parent: FakeWidget | None = None, **options: object) -> None:
        super().__init__(parent, **options)
        self.tabs: list[tuple[FakeWidget, str]] = []

    def add(self, frame: FakeWidget, *, text: str) -> None:
        self.tabs.append((frame, text))


class FakeStyle:
    configured: list[tuple[str, dict[str, object]]] = []

    def __init__(self, _root: FakeWidget) -> None:
        pass

    def configure(self, name: str, **options: object) -> None:
        self.configured.append((name, options))


class FakeRoot(FakeWidget):
    def title(self, _title: str) -> None:
        pass

    def minsize(self, _width: int, _height: int) -> None:
        pass

    def protocol(self, _name: str, _callback: object) -> None:
        pass


def fake_tab(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(
        frame=FakeWidget(kwargs["notebook"]),
        start_server=lambda **_options: None,
    )


def test_launcher_app_builds_tabs_log_and_footer_outside_notebook(tmp_path: Path) -> None:
    app = LauncherApp.__new__(LauncherApp)
    app.tk = SimpleNamespace(Text=FakeWidget)
    app.ttk = SimpleNamespace(
        Style=FakeStyle,
        Frame=FakeWidget,
        Label=FakeWidget,
        Notebook=FakeNotebook,
        Scrollbar=FakeWidget,
        Button=FakeWidget,
    )
    app.root = FakeRoot()
    app.status_value = object()
    app.collection_path = tmp_path / "samling"
    app.busy = False
    app.tooltips = []

    with (
        patch("bildebank.launcher_app.MainTab", side_effect=fake_tab),
        patch("bildebank.launcher_app.AdvancedStartTab", side_effect=fake_tab),
        patch("bildebank.launcher_app.ImportTab", side_effect=fake_tab),
        patch("bildebank.launcher_app.ToolsTab", side_effect=fake_tab),
        patch("bildebank.launcher_app.SetupTab", side_effect=fake_tab),
    ):
        app._build_gui()

    assert [text for _frame, text in app.notebook.tabs] == [
        "Bildebank",
        "Nettleser og deling",
        "Import av bilder",
        "Verktøy",
        "Oppsett",
    ]
    outer = app.notebook.parent
    assert outer is not None
    assert app.log_text.parent is not app.notebook
    assert app.log_text.parent is not None
    assert app.log_text.parent.parent is outer
    assert app.cancel_command_button.options["text"] == "Avbryt jobb"
    assert app.exit_button.options["text"] == "Avslutt Bildebank"
    assert app.cancel_command_button.options["style"] == BUTTON_STYLE


def test_launcher_app_starts_tab_status_refreshes(tmp_path: Path) -> None:
    actions: list[str] = []
    fake_tkinter = ModuleType("tkinter")
    fake_tkinter.Tk = FakeRoot
    fake_tkinter.StringVar = lambda **kwargs: SimpleNamespace(**kwargs)
    fake_tkinter.ttk = SimpleNamespace()

    def build_gui(app: LauncherApp) -> None:
        app.main_tab = SimpleNamespace(
            update_migration_status=lambda: actions.append("migration"),
            start_update_status_refresh=lambda: actions.append("update-status"),
            show_initial_migration_status=lambda: actions.append("migration-dialog"),
        )
        app.setup = SimpleNamespace(
            start_status_refresh=lambda: actions.append("setup-status"),
            log_unsupported_installers=lambda: actions.append("installer-info"),
        )

    with (
        patch.dict(sys.modules, {"tkinter": fake_tkinter}),
        patch(
            "bildebank.launcher_app.load_launcher_config",
            return_value=LauncherConfig(tmp_path / "samling"),
        ),
        patch("bildebank.launcher_app.CommandRunner"),
        patch.object(LauncherApp, "_build_gui", build_gui),
        patch.object(LauncherApp, "_refresh_state", lambda self: actions.append("refresh")),
        patch.object(LauncherApp, "_log", lambda self, message: actions.append(message)),
    ):
        LauncherApp()

    assert actions == [
        "migration",
        "refresh",
        "update-status",
        "setup-status",
        f"Valgt bildesamling: {tmp_path / 'samling'}",
        "migration-dialog",
        "installer-info",
    ]


def test_refresh_state_applies_main_availability_to_advanced_start() -> None:
    app = LauncherApp.__new__(LauncherApp)
    availability: list[bool] = []
    app.main_tab = SimpleNamespace(
        refresh=lambda: SimpleNamespace(available=False, buttons=[])
    )
    app.advanced_start_tab = SimpleNamespace(set_available=availability.append)
    app.import_tab = SimpleNamespace(refresh=lambda *, available: [])
    app.tools_tab = SimpleNamespace(refresh=lambda *, available: [])
    app.tooltips = []
    app.busy = False
    app._set_buttons_enabled = lambda _enabled: None

    app._refresh_state()

    assert availability == [False]


def test_post_to_tk_ignores_callbacks_after_close_started() -> None:
    app = LauncherApp.__new__(LauncherApp)
    app.closing = True

    class RootThatMustNotSchedule:
        def after(self, *_args: object) -> None:
            raise AssertionError("after should not be called while closing")

    app.root = RootThatMustNotSchedule()

    assert not app._post_to_tk(lambda: None)


def test_close_stops_server_owned_by_main_tab() -> None:
    app = LauncherApp.__new__(LauncherApp)
    actions: list[str] = []
    app.busy = False
    app.closing = False
    app.main_tab = SimpleNamespace(
        stop_server_process=lambda: actions.append("stop-server")
    )
    app._destroy_root = lambda: actions.append("destroy-root")

    app._on_close()

    assert actions == ["stop-server", "destroy-root"]


def test_close_is_blocked_while_command_is_running() -> None:
    assert close_blocked_by_running_command(True)
    assert not close_blocked_by_running_command(False)


def test_background_task_runs_work_off_ui_path_and_reports_success() -> None:
    app = LauncherApp.__new__(LauncherApp)
    events: list[object] = []
    app.busy = False
    app._set_busy = lambda busy, message="": (
        setattr(app, "busy", busy),
        events.append(("busy", busy, message)),
    )
    app._clear_active_progress_log = lambda: None
    app._log = lambda message: events.append(("log", message))
    app._post_to_tk = lambda callback: (callback(), True)[1]

    class ImmediateThread:
        def __init__(self, *, target, daemon: bool) -> None:
            self.target = target
            assert daemon

        def start(self) -> None:
            self.target()

    with patch("bildebank.launcher_app.threading.Thread", ImmediateThread):
        app._run_background_task(
            lambda: "resultat",
            running_message="Jobber ...",
            failure_message="Feilet.",
            on_success=lambda result: events.append(("resultat", result)),
        )

    assert events == [
        ("busy", True, "Jobber ..."),
        ("log", "Jobber ..."),
        ("busy", False, ""),
        ("resultat", "resultat"),
    ]


def test_background_task_reports_exception_and_unlocks_launcher() -> None:
    app = LauncherApp.__new__(LauncherApp)
    app.busy = False
    app.root = object()
    logged: list[str] = []
    app._set_busy = lambda busy, message="": setattr(app, "busy", busy)
    app._clear_active_progress_log = lambda: None
    app._log = logged.append
    app._post_to_tk = lambda callback: (callback(), True)[1]

    class ImmediateThread:
        def __init__(self, *, target, daemon: bool) -> None:
            self.target = target
            assert daemon

        def start(self) -> None:
            self.target()

    def fail() -> object:
        raise ValueError("detalj")

    with (
        patch("bildebank.launcher_app.threading.Thread", ImmediateThread),
        patch("tkinter.messagebox.showerror") as showerror,
    ):
        app._run_background_task(
            fail,
            running_message="Jobber ...",
            failure_message="Snapshot feilet.",
            on_success=lambda _result: None,
        )

    assert not app.busy
    assert logged[-1] == "Snapshot feilet. detalj"
    showerror.assert_called_once_with(
        "Feil",
        "Snapshot feilet.\n\ndetalj",
        parent=app.root,
    )
