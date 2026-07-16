from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bildebank.launcher_advanced_start_tab import (
    AdvancedStartTab,
    LAN_SHARE_MODE,
    NORMAL_MODE,
    READ_ONLY_MODE,
    SLIDESHOW_MODE,
    parse_server_port,
    parse_slideshow_delay,
)


class FakeVariable:
    def __init__(self, *, value: str) -> None:
        self.value = value
        self.callbacks: list[Callable[..., None]] = []

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value
        for callback in self.callbacks:
            callback("variable", "", "write")

    def trace_add(self, _mode: str, callback: Callable[..., None]) -> None:
        self.callbacks.append(callback)


class FakeWidget:
    def __init__(self, _parent: object = None, **options: object) -> None:
        self.options = options
        self.visible = False

    def columnconfigure(self, *_args: object, **_kwargs: object) -> None:
        pass

    def configure(self, **options: object) -> None:
        self.options.update(options)

    def grid(self, **_options: object) -> None:
        self.visible = True

    def grid_remove(self) -> None:
        self.visible = False


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
    assert tab.slideshow_delay.get() == "10"
    assert tab.slideshow_filter.get() == ""
    assert not tab.slideshow_settings_frame.visible
    assert not tab.lan_address_frame.visible
    assert tab.start_button.options["text"] == "Start Bildebank i nettleser"
    tab.set_available(False)
    assert tab.start_button.options["state"] == "disabled"
    tab.set_available(True)
    assert tab.start_button.options["state"] == "normal"


def test_lan_mode_shows_detected_address_with_default_port() -> None:
    tab, _starts, _logs = make_tab()

    with patch(
        "bildebank.launcher_advanced_start_tab.lan_share_urls",
        return_value=["http://192.168.1.20:8765/"],
    ):
        tab.mode.set(LAN_SHARE_MODE)

    assert tab.lan_address_frame.visible
    assert tab.lan_addresses.get() == "http://192.168.1.20:8765/"


def test_lan_address_updates_for_custom_port() -> None:
    tab, _starts, _logs = make_tab()

    with patch(
        "bildebank.launcher_advanced_start_tab.lan_share_urls",
        side_effect=lambda port: [f"http://192.168.1.20:{port}/"],
    ):
        tab.mode.set(LAN_SHARE_MODE)
        tab.port.set("9000")

    assert tab.lan_addresses.get() == "http://192.168.1.20:9000/"


def test_slideshow_mode_shows_fields_and_lan_address_and_preserves_values() -> None:
    tab, _starts, _logs = make_tab()

    tab.slideshow_delay.set("17")
    tab.slideshow_filter.set("year=1999")
    with patch(
        "bildebank.launcher_advanced_start_tab.lan_share_urls",
        return_value=["http://192.168.1.20:8765/"],
    ):
        tab.mode.set(SLIDESHOW_MODE)

    assert tab.slideshow_settings_frame.visible
    assert tab.lan_address_frame.visible
    assert tab.start_button.options["text"] == "Start slideshow"
    assert tab.slideshow_delay.get() == "17"
    assert tab.slideshow_filter.get() == "year=1999"

    tab.mode.set(NORMAL_MODE)
    assert not tab.slideshow_settings_frame.visible
    assert not tab.lan_address_frame.visible
    assert tab.start_button.options["text"] == "Start Bildebank i nettleser"

    tab.mode.set(SLIDESHOW_MODE)
    assert tab.slideshow_delay.get() == "17"
    assert tab.slideshow_filter.get() == "year=1999"


def test_lan_mode_shows_all_detected_addresses() -> None:
    tab, _starts, _logs = make_tab()

    with patch(
        "bildebank.launcher_advanced_start_tab.lan_share_urls",
        return_value=[
            "http://192.168.1.20:8765/",
            "http://192.168.2.30:8765/",
        ],
    ):
        tab.mode.set(LAN_SHARE_MODE)

    assert tab.lan_addresses.get() == (
        "http://192.168.1.20:8765/\nhttp://192.168.2.30:8765/"
    )


def test_lan_addresses_are_refreshed_immediately_before_start() -> None:
    tab, starts, _logs = make_tab()

    with patch(
        "bildebank.launcher_advanced_start_tab.lan_share_urls",
        side_effect=[
            ["http://192.168.1.20:8765/"],
            ["http://192.168.1.21:8765/"],
        ],
    ):
        tab.mode.set(LAN_SHARE_MODE)
        tab._on_start()

    assert tab.lan_addresses.get() == "http://192.168.1.21:8765/"
    assert starts[0]["lan_share"] is True


def test_lan_mode_shows_help_for_invalid_port_and_missing_address() -> None:
    tab, _starts, _logs = make_tab()

    with patch(
        "bildebank.launcher_advanced_start_tab.lan_share_urls",
        return_value=[],
    ):
        tab.mode.set(LAN_SHARE_MODE)
        assert "ipconfig" in tab.lan_addresses.get()
        assert "http://<IP-adresse>:8765/" in tab.lan_addresses.get()

        tab.port.set("invalid")

    assert "gyldig port" in tab.lan_addresses.get()


@pytest.mark.parametrize("mode", [NORMAL_MODE, READ_ONLY_MODE])
def test_lan_address_field_is_hidden_outside_lan_mode(mode: str) -> None:
    tab, _starts, _logs = make_tab()
    tab.mode.set(LAN_SHARE_MODE)

    tab.mode.set(mode)

    assert not tab.lan_address_frame.visible


@pytest.mark.parametrize(
    ("mode", "read_only", "lan_share", "slideshow"),
    [
        (NORMAL_MODE, False, False, False),
        (READ_ONLY_MODE, True, False, False),
        (LAN_SHARE_MODE, False, True, False),
        (SLIDESHOW_MODE, False, False, True),
    ],
)
def test_advanced_start_passes_selected_mode(
    mode: str, read_only: bool, lan_share: bool, slideshow: bool
) -> None:
    tab, starts, _logs = make_tab()
    tab.mode.set(mode)
    tab.port.set("9000")

    tab._on_start()

    assert starts[0]["port"] == 9000
    assert starts[0]["read_only"] is read_only
    assert starts[0]["lan_share"] is lan_share
    assert starts[0]["slideshow"] is slideshow
    assert (starts[0]["confirm_lan_start"] is not None) is (lan_share or slideshow)


def test_slideshow_start_passes_normalized_delay_and_optional_filter() -> None:
    tab, starts, _logs = make_tab()
    tab.mode.set(SLIDESHOW_MODE)
    tab.slideshow_delay.set("20")
    tab.slideshow_filter.set("  person:Ola year=1999  ")

    tab._on_start()

    assert starts[0]["delay"] == 20
    assert starts[0]["filter"] == "person:Ola year=1999"


def test_empty_slideshow_filter_is_passed_as_none() -> None:
    tab, starts, _logs = make_tab()
    tab.mode.set(SLIDESHOW_MODE)
    tab.slideshow_filter.set("   ")

    tab._on_start()

    assert starts[0]["filter"] is None


@pytest.mark.parametrize("value", ["", "abc", "1.5", "0", "65536"])
def test_parse_server_port_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="1 til 65535"):
        parse_server_port(value)


@pytest.mark.parametrize("value", ["", "abc", "1.5", "0", "-1"])
def test_parse_slideshow_delay_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="positivt heltall"):
        parse_slideshow_delay(value)


def test_invalid_port_shows_error_without_starting() -> None:
    tab, starts, _logs = make_tab()
    tab.port.set("0")

    with patch("tkinter.messagebox.showerror") as showerror:
        tab._on_start()

    showerror.assert_called_once()
    assert starts == []


def test_invalid_slideshow_delay_shows_error_without_starting() -> None:
    tab, starts, _logs = make_tab()
    tab.mode.set(SLIDESHOW_MODE)
    tab.slideshow_delay.set("0")

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
