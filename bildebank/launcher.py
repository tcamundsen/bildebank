from __future__ import annotations

import os
import signal
import subprocess
import sys

from .launcher_benchmark import record_startup_event, startup_benchmark_enabled


WINDOWS_LAUNCHER_CHILD_ENV = "BILDEBANK_WINDOWS_LAUNCHER_CHILD"
LAUNCHER_APP_IMPORT_MODULES = (
    "launcher_status",
    "launcher_runner",
    "launcher_import_tab",
    "launcher_advanced_start_tab",
    "launcher_main_tab",
    "launcher_snapshot_tab",
    "launcher_setup_tab",
    "launcher_tools_tab",
    "launcher_widgets",
)


def install_windows_interrupt_handler() -> None:
    if os.name != "nt":
        return
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, signal.default_int_handler)


def _record_launcher_app_import_breakdown() -> None:
    if not startup_benchmark_enabled():
        return

    from importlib import import_module

    for module_name in LAUNCHER_APP_IMPORT_MODULES:
        import_module(f".{module_name}", package=__package__)
        record_startup_event(f"{module_name}_imported")


def main() -> int:
    install_windows_interrupt_handler()
    child_process = os.environ.get(WINDOWS_LAUNCHER_CHILD_ENV) == "1"
    record_startup_event(
        "launcher_child_enter" if child_process else "launcher_parent_enter"
    )
    if (
        sys.platform == "win32"
        and not child_process
    ):
        child_environment = os.environ.copy()
        child_environment[WINDOWS_LAUNCHER_CHILD_ENV] = "1"
        record_startup_event("windows_child_spawn_start")
        subprocess.Popen(
            [sys.executable, "-m", "bildebank", "start"],
            env=child_environment,
        )
        record_startup_event("windows_child_spawned")
        print("Bildebank starter. Vennligst vent …")
        return 0

    record_startup_event("launcher_app_import_start")
    _record_launcher_app_import_breakdown()
    from .launcher_app import LauncherApp

    record_startup_event("launcher_app_imported")
    launcher = LauncherApp()
    record_startup_event("launcher_app_constructed")
    launcher.run()
    return 0
