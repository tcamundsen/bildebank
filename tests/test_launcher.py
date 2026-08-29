from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import Mock, call, patch

from bildebank import launcher


def test_launcher_module_is_a_thin_public_entrypoint() -> None:
    assert launcher.main.__module__ == "bildebank.launcher"
    assert not hasattr(launcher, "LauncherApp")
    assert not hasattr(launcher, "ImportTab")
    assert not hasattr(launcher, "ToolsTab")


def test_launcher_app_import_does_not_load_deferred_modules() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import bildebank.launcher_app; "
            "print([module for module in "
            "('bildebank.server_runtime', 'bildebank.image_clustering', 'numpy') "
            "if module in sys.modules])",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "[]"


def test_launcher_main_runs_launcher_app() -> None:
    app = Mock()

    with (
        patch("bildebank.launcher.sys.platform", "linux"),
        patch("bildebank.launcher_app.LauncherApp", return_value=app) as app_class,
    ):
        assert launcher.main() == 0

    app_class.assert_called_once_with()
    app.run.assert_called_once_with()


def test_launcher_benchmark_records_direct_launcher_app_imports() -> None:
    with (
        patch.object(launcher, "startup_benchmark_enabled", return_value=True),
        patch.object(launcher, "record_startup_event") as record,
        patch("importlib.import_module") as import_module,
    ):
        launcher._record_launcher_app_import_breakdown()

    assert import_module.call_args_list == [
        call(f".{module_name}", package="bildebank")
        for module_name in launcher.LAUNCHER_APP_IMPORT_MODULES
    ]
    assert record.call_args_list == [
        call(f"{module_name}_imported")
        for module_name in launcher.LAUNCHER_APP_IMPORT_MODULES
    ]


def test_launcher_installs_windows_interrupt_handler() -> None:
    with (
        patch("bildebank.launcher.os.name", "nt"),
        patch.object(launcher.signal, "SIGBREAK", 21, create=True),
        patch("bildebank.launcher.signal.signal") as install_signal,
    ):
        launcher.install_windows_interrupt_handler()

    install_signal.assert_called_once_with(21, launcher.signal.default_int_handler)


def test_launcher_restarts_under_python_before_opening_window_on_windows(
    capsys,
) -> None:
    child = Mock()
    environment = {"PATH": "test-path"}

    with (
        patch("bildebank.launcher.sys.platform", "win32"),
        patch("bildebank.launcher.os.environ", environment),
        patch("bildebank.launcher.subprocess.Popen", return_value=child) as popen,
        patch("bildebank.launcher_app.LauncherApp") as app_class,
    ):
        assert launcher.main() == 0

    popen.assert_called_once_with(
        [sys.executable, "-m", "bildebank", "start"],
        env={
            **environment,
            launcher.WINDOWS_LAUNCHER_CHILD_ENV: "1",
        },
    )
    assert capsys.readouterr().out == "Bildebank starter. Vennligst vent …\n"
    app_class.assert_not_called()


def test_windows_launcher_child_opens_window_without_restart_loop() -> None:
    app = Mock()

    with (
        patch("bildebank.launcher.sys.platform", "win32"),
        patch.dict(
            os.environ,
            {launcher.WINDOWS_LAUNCHER_CHILD_ENV: "1"},
        ),
        patch("bildebank.launcher.subprocess.Popen") as popen,
        patch("bildebank.launcher_app.LauncherApp", return_value=app),
    ):
        assert launcher.main() == 0

    popen.assert_not_called()
    app.run.assert_called_once_with()
