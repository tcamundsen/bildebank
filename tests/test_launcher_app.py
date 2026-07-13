from __future__ import annotations

import inspect

from bildebank.launcher_app import LauncherApp, close_blocked_by_running_command


def test_launcher_layout_source_defines_notebook_tabs_and_log_below_tabs() -> None:
    source = inspect.getsource(LauncherApp._build_gui)
    refresh_source = inspect.getsource(LauncherApp._refresh_state)

    assert "ttk.Notebook(outer)" in source
    assert "self.main_tab = MainTab(" in source
    assert 'self.notebook.add(self.main_tab.frame, text="Bildebank")' in source
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
    assert "self.import_tab.refresh(available=main_state.available)" in refresh_source
    assert "self.tools_tab.refresh(available=main_state.available)" in refresh_source


def test_launcher_button_helper_uses_launcher_button_style() -> None:
    source = inspect.getsource(LauncherApp._button)

    assert 'kwargs.setdefault("style", BUTTON_STYLE)' in source
    assert "return self.ttk.Button(parent, **kwargs)" in source


def test_post_to_tk_ignores_callbacks_after_close_started() -> None:
    launcher = LauncherApp.__new__(LauncherApp)
    launcher.closing = True

    class FakeRoot:
        def after(self, *_args: object) -> None:
            raise AssertionError("after should not be called while closing")

    launcher.root = FakeRoot()

    assert not launcher._post_to_tk(lambda: None)


def test_close_stops_server_owned_by_main_tab() -> None:
    launcher = LauncherApp.__new__(LauncherApp)
    actions: list[str] = []
    launcher.busy = False
    launcher.closing = False
    launcher.main_tab = type(
        "FakeMainTab",
        (),
        {"stop_server_process": lambda self: actions.append("stop-server")},
    )()
    launcher._destroy_root = lambda: actions.append("destroy-root")

    launcher._on_close()

    assert actions == ["stop-server", "destroy-root"]


def test_close_is_blocked_while_command_is_running() -> None:
    assert close_blocked_by_running_command(True)
    assert not close_blocked_by_running_command(False)
