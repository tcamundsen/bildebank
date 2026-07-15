from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bildebank.launcher_advanced_start_tab import (
    AdvancedStartTab,
    LAN_SHARE_MODE,
    NORMAL_MODE,
    READ_ONLY_MODE,
    parse_server_port,
)


class FakeVariable:
    def __init__(self, *, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class FakeWidget:
    def __init__(self, _parent: object = None, **options: object) -> None:
        self.options = options

    def columnconfigure(self, *_args: object, **_kwargs: object) -> None:
        pass

    def configure(self, **options: object) -> None:
        self.options.update(options)

    def grid(self, **_options: object) -> None:
        pass


def make_tab() -> tuple[AdvancedStartTab, list[dict[str, object]], list[str]]:
    starts: list[dict[str, object]] = []
    logs: list[str] = []
    tab = AdvancedStartTab(
        tk=SimpleNamespace(StringVar=FakeVariable),
        ttk=SimpleNamespace(
            Frame=FakeWidget,
            Label=FakeWidget,
            Radiobutton=FakeWidget,
            Entry=FakeWidget,
        ),
        notebook=FakeWidget(),
        root=object(),
        button=FakeWidget,
        start_server=lambda **options: starts.append(options),
        log=logs.append,
        padding=12,
        padx=4,
        pady=4,
    )
    return tab, starts, logs


def test_advanced_start_defaults_and_availability() -> None:
    tab, _starts, _logs = make_tab()

    assert tab.mode.get() == NORMAL_MODE
    assert tab.port.get() == "8765"
    tab.set_available(False)
    assert tab.start_button.options["state"] == "disabled"
    tab.set_available(True)
    assert tab.start_button.options["state"] == "normal"


@pytest.mark.parametrize(
    ("mode", "read_only", "lan_share"),
    [
        (NORMAL_MODE, False, False),
        (READ_ONLY_MODE, True, False),
        (LAN_SHARE_MODE, False, True),
    ],
)
def test_advanced_start_passes_selected_mode(
    mode: str, read_only: bool, lan_share: bool
) -> None:
    tab, starts, _logs = make_tab()
    tab.mode.set(mode)
    tab.port.set("9000")

    tab._on_start()

    assert starts[0]["port"] == 9000
    assert starts[0]["read_only"] is read_only
    assert starts[0]["lan_share"] is lan_share
    assert (starts[0]["confirm_lan_start"] is not None) is lan_share


@pytest.mark.parametrize("value", ["", "abc", "1.5", "0", "65536"])
def test_parse_server_port_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="1 til 65535"):
        parse_server_port(value)


def test_invalid_port_shows_error_without_starting() -> None:
    tab, starts, _logs = make_tab()
    tab.port.set("0")

    with patch("tkinter.messagebox.showerror") as showerror:
        tab._on_start()

    showerror.assert_called_once()
    assert starts == []


def test_lan_confirmation_warns_and_logs_cancellation() -> None:
    tab, _starts, logs = make_tab()

    with patch("tkinter.messagebox.askokcancel", return_value=False) as askokcancel:
        assert not tab._confirm_lan_start()

    message = askokcancel.call_args.args[1]
    assert "Alle på samme LAN" in message
    assert "ingen innlogging" in message
    assert "skrivebeskyttet" in message
    assert "privat nettverk" in message
    assert logs == ["LAN-deling avbrutt."]
