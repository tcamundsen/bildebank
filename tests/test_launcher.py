from __future__ import annotations

import inspect
from unittest.mock import Mock, patch

from bildebank import launcher


def test_launcher_module_is_a_thin_public_entrypoint() -> None:
    source = inspect.getsource(launcher)

    assert "def main() -> int:" in source
    assert "from .launcher_app import LauncherApp" in source
    assert "tkinter" not in source
    assert "launcher_import_tab" not in source
    assert "launcher_tools_tab" not in source


def test_launcher_main_runs_launcher_app() -> None:
    app = Mock()

    with patch("bildebank.launcher_app.LauncherApp", return_value=app) as app_class:
        assert launcher.main() == 0

    app_class.assert_called_once_with()
    app.run.assert_called_once_with()
