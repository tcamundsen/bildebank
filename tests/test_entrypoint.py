from __future__ import annotations

import sys
from unittest.mock import patch

from bildebank import entrypoint


def test_exact_start_command_uses_lightweight_launcher_path() -> None:
    with (
        patch.object(sys, "argv", ["bildebank", "start"]),
        patch("bildebank.launcher.main", return_value=7) as launcher_main,
    ):
        assert entrypoint.main() == 7

    launcher_main.assert_called_once_with()


def test_launcher_alias_uses_lightweight_launcher_path() -> None:
    with (
        patch.object(sys, "argv", ["bildebank", "launcher"]),
        patch("bildebank.launcher.main", return_value=8) as launcher_main,
    ):
        assert entrypoint.main() == 8

    launcher_main.assert_called_once_with()


def test_start_with_options_keeps_full_cli_parsing() -> None:
    with (
        patch.object(sys, "argv", ["bildebank", "start", "--help"]),
        patch("bildebank.launcher.main") as launcher_main,
        patch("bildebank.cli.main", return_value=0) as cli_main,
    ):
        assert entrypoint.main() == 0

    launcher_main.assert_not_called()
    cli_main.assert_called_once_with()


def test_lightweight_launcher_path_reports_errors_without_full_cli(capsys) -> None:
    with (
        patch.object(sys, "argv", ["bildebank", "start"]),
        patch("bildebank.launcher.main", side_effect=ValueError("detalj")),
    ):
        assert entrypoint.main() == 1

    assert capsys.readouterr().err == "Feil: detalj\n"
