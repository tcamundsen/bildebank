from __future__ import annotations


def main() -> int:
    from .launcher_app import LauncherApp

    launcher = LauncherApp()
    launcher.run()
    return 0
