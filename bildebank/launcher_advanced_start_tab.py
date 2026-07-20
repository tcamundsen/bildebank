from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .cli_server import lan_share_urls
from .server_slideshow import DEFAULT_SLIDESHOW_DELAY_SECONDS
from .server_runtime import DEFAULT_PORT

NORMAL_MODE = "normal"
READ_ONLY_MODE = "read-only"
LAN_SHARE_MODE = "lan-share"
SLIDESHOW_MODE = "slideshow"


class ServerStarter(Protocol):
    def __call__(
        self,
        *,
        port: int,
        read_only: bool = False,
        lan_share: bool = False,
        slideshow: bool = False,
        delay: int = DEFAULT_SLIDESHOW_DELAY_SECONDS,
        filter: str | None = None,
        confirm_lan_start: Callable[[], bool] | None = None,
    ) -> None: ...


def parse_server_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("Port må være et heltall fra 1 til 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Port må være et heltall fra 1 til 65535.")
    return port


def parse_slideshow_delay(value: str) -> int:
    try:
        delay = int(value)
    except ValueError as exc:
        raise ValueError("Sekunder per bilde må være et positivt heltall.") from exc
    if delay <= 0:
        raise ValueError("Sekunder per bilde må være et positivt heltall.")
    return delay


class AdvancedStartTab:
    def __init__(
        self,
        *,
        tk: Any,
        ttk: Any,
        notebook: Any,
        root: Any,
        button: Callable[..., Any],
        start_server: ServerStarter,
        log: Callable[[str], None],
        padding: int,
        padx: int,
        pady: int,
    ) -> None:
        self.root = root
        self._button = button
        self._start_server = start_server
        self._log = log
        self.mode = tk.StringVar(value=NORMAL_MODE)
        self.port = tk.StringVar(value=str(DEFAULT_PORT))
        self.slideshow_delay = tk.StringVar(
            value=str(DEFAULT_SLIDESHOW_DELAY_SECONDS)
        )
        self.slideshow_filter = tk.StringVar(value="")
        self.lan_addresses = tk.StringVar(value="")

        self.frame = ttk.Frame(notebook, padding=padding)
        self.frame.columnconfigure(0, weight=1)
        ttk.Label(self.frame, text="Oppstartsmodus:").grid(
            row=0, column=0, sticky="w", padx=padx, pady=pady
        )
        modes = (
            ("Vanlig", NORMAL_MODE),
            ("Skrivebeskyttet", READ_ONLY_MODE),
            ("Del skrivebeskyttet på LAN", LAN_SHARE_MODE),
            ("Slideshow på LAN", SLIDESHOW_MODE),
        )
        for row, (text, value) in enumerate(modes, start=1):
            ttk.Radiobutton(
                self.frame,
                text=text,
                variable=self.mode,
                value=value,
            ).grid(row=row, column=0, sticky="w", padx=padx, pady=pady)

        port_frame = ttk.Frame(self.frame)
        port_frame.grid(row=5, column=0, sticky="w", padx=padx, pady=(padding, pady))
        ttk.Label(port_frame, text="Port:").grid(row=0, column=0, sticky="w")
        ttk.Entry(port_frame, textvariable=self.port, width=8).grid(
            row=0, column=1, sticky="w", padx=(padx, 0)
        )
        self.slideshow_settings_frame = ttk.Frame(self.frame)
        self.slideshow_settings_frame.grid(
            row=6, column=0, sticky="w", padx=padx, pady=pady
        )
        ttk.Label(
            self.slideshow_settings_frame,
            text="Sekunder per bilde:",
        ).grid(row=0, column=0, sticky="w")
        ttk.Entry(
            self.slideshow_settings_frame,
            textvariable=self.slideshow_delay,
            width=8,
        ).grid(row=0, column=1, sticky="w", padx=(padx, 0))
        ttk.Label(
            self.slideshow_settings_frame,
            text="Filter (valgfritt):",
        ).grid(row=1, column=0, sticky="w", pady=(pady, 0))
        ttk.Entry(
            self.slideshow_settings_frame,
            textvariable=self.slideshow_filter,
            width=48,
        ).grid(row=1, column=1, sticky="w", padx=(padx, 0), pady=(pady, 0))

        self.lan_address_frame = ttk.Frame(self.frame)
        self.lan_address_frame.grid(row=7, column=0, sticky="ew", padx=padx, pady=pady)
        ttk.Label(
            self.lan_address_frame,
            text="Adresse som andre skal åpne:",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            self.lan_address_frame,
            textvariable=self.lan_addresses,
            justify="left",
            wraplength=640,
        ).grid(row=1, column=0, sticky="w")
        self.start_button = self._button(
            self.frame,
            text="Start Bildebank i nettleser",
            command=self._on_start,
        )
        self.start_button.grid(row=8, column=0, sticky="w", padx=padx, pady=(padding, pady))

        self.mode.trace_add("write", self._on_settings_changed)
        self.port.trace_add("write", self._on_settings_changed)
        self._update_mode_settings()

    def set_available(self, available: bool) -> None:
        self.start_button.configure(state="normal" if available else "disabled")

    def _on_settings_changed(self, *_args: object) -> None:
        self._update_mode_settings()

    def _update_mode_settings(self) -> None:
        slideshow = self.mode.get() == SLIDESHOW_MODE
        if slideshow:
            self.slideshow_settings_frame.grid()
            self.start_button.configure(text="Start slideshow")
        else:
            self.slideshow_settings_frame.grid_remove()
            self.start_button.configure(text="Start Bildebank i nettleser")
        self._update_lan_addresses()

    def _update_lan_addresses(self) -> None:
        if self.mode.get() not in {LAN_SHARE_MODE, SLIDESHOW_MODE}:
            self.lan_address_frame.grid_remove()
            return

        self.lan_address_frame.grid()
        try:
            port = parse_server_port(self.port.get())
        except ValueError:
            self.lan_addresses.set("Angi en gyldig port fra 1 til 65535.")
            return

        urls = lan_share_urls(port)
        if urls:
            self.lan_addresses.set("\n".join(urls))
            return
        self.lan_addresses.set(
            "Fant ikke lokal LAN-adresse automatisk. Finn IPv4-adressen med "
            f"ipconfig og bruk http://<IP-adresse>:{port}/."
        )

    def _on_start(self) -> None:
        from tkinter import messagebox

        try:
            port = parse_server_port(self.port.get())
        except ValueError as exc:
            messagebox.showerror("Ugyldig port", str(exc), parent=self.root)
            return

        mode = self.mode.get()
        slideshow = mode == SLIDESHOW_MODE
        if slideshow:
            try:
                delay = parse_slideshow_delay(self.slideshow_delay.get())
            except ValueError as exc:
                messagebox.showerror("Ugyldig visningstid", str(exc), parent=self.root)
                return
        else:
            delay = DEFAULT_SLIDESHOW_DELAY_SECONDS

        if mode in {LAN_SHARE_MODE, SLIDESHOW_MODE}:
            self._update_lan_addresses()
        self._start_server(
            port=port,
            read_only=mode == READ_ONLY_MODE,
            lan_share=mode == LAN_SHARE_MODE,
            slideshow=slideshow,
            delay=delay,
            filter=(self.slideshow_filter.get().strip() or None) if slideshow else None,
            confirm_lan_start=(
                self._confirm_lan_start
                if mode in {LAN_SHARE_MODE, SLIDESHOW_MODE}
                else None
            ),
        )

    def _confirm_lan_start(self) -> bool:
        from tkinter import messagebox

        confirmed = bool(
            messagebox.askokcancel(
                "Dele bilder på LAN?",
                (
                    "Alle på samme LAN kan nå serveren og se bildene. Det finnes ingen "
                    "innlogging.\n\nModusen er skrivebeskyttet, men bildene er fortsatt "
                    "eksponert.\n\nBruk dette bare på et privat nettverk du stoler på."
                ),
                parent=self.root,
            )
        )
        if not confirmed:
            if self.mode.get() == SLIDESHOW_MODE:
                self._log("Slideshow på LAN avbrutt.")
            else:
                self._log("LAN-deling avbrutt.")
        return confirmed
