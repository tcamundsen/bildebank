from __future__ import annotations

from unittest.mock import Mock, patch

from bildebank import launcher


def test_launcher_module_is_a_thin_public_entrypoint() -> None:
    assert launcher.main.__module__ == "bildebank.launcher"
    assert not hasattr(launcher, "LauncherApp")
    assert not hasattr(launcher, "ImportTab")
    assert not hasattr(launcher, "ToolsTab")


def test_launcher_main_runs_launcher_app() -> None:
    app = Mock()

    with patch("bildebank.launcher_app.LauncherApp", return_value=app) as app_class:
        assert launcher.main() == 0

    app_class.assert_called_once_with()
    app.run.assert_called_once_with()
