from __future__ import annotations

import os
import signal
import subprocess
import sys

from .launcher_benchmark import record_startup_event


WINDOWS_LAUNCHER_CHILD_ENV = "BILDEBANK_WINDOWS_LAUNCHER_CHILD"


def install_windows_interrupt_handler() -> None:
    if os.name != "nt":
        return
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, signal.default_int_handler)


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
        print("Bildebank starter. Vinduet åpnes om 3–10 sekunder. Vennligst vent …")
        return 0

    record_startup_event("launcher_app_import_start")
    from .launcher_app import LauncherApp

    record_startup_event("launcher_app_imported")
    launcher = LauncherApp()
    record_startup_event("launcher_app_constructed")
    launcher.run()
    return 0
