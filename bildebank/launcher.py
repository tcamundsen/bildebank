from __future__ import annotations

import os
import subprocess
import sys


WINDOWS_LAUNCHER_CHILD_ENV = "BILDEBANK_WINDOWS_LAUNCHER_CHILD"


def main() -> int:
    if (
        sys.platform == "win32"
        and os.environ.get(WINDOWS_LAUNCHER_CHILD_ENV) != "1"
    ):
        child_environment = os.environ.copy()
        child_environment[WINDOWS_LAUNCHER_CHILD_ENV] = "1"
        subprocess.Popen(
            [sys.executable, "-m", "bildebank", "start"],
            env=child_environment,
        )
        print("Bildebank starter. Vinduet åpnes om 3–10 sekunder. Vennligst vent …")
        return 0

    from .launcher_app import LauncherApp

    launcher = LauncherApp()
    launcher.run()
    return 0
