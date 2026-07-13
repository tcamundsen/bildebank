from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from . import launcher_import_tab as _launcher_import_tab
from . import launcher_main_tab as _launcher_main_tab
from . import launcher_status as _launcher_status
from . import launcher_tools_tab as _launcher_tools_tab
from .launcher_status import (
    LauncherConfig,
    load_launcher_config,
    save_launcher_config,
)
from .launcher_runner import CommandRunner, progress_log_key
from .launcher_import_tab import ImportTab
from .launcher_main_tab import MainTab
from .launcher_setup_tab import SetupTab
from .launcher_tools_tab import ToolsTab
from .launcher_widgets import (
    Tooltip,
    ask_string_dialog,
    show_log_review_question,
)

# Midlertidige re-eksporter mens launcher-modulene deles opp.
InsightFaceDependencyStatus = _launcher_status.InsightFaceDependencyStatus
InsightFaceModelStatus = _launcher_status.InsightFaceModelStatus
OpenClipModelStatus = _launcher_status.OpenClipModelStatus
LauncherUpdateStatus = _launcher_status.LauncherUpdateStatus
suggest_import_name = _launcher_import_tab.suggest_import_name
source_is_collection_or_inside = _launcher_import_tab.source_is_collection_or_inside
open_server_browser_window = _launcher_main_tab.open_server_browser_window
server_browser_url = _launcher_main_tab.server_browser_url
FACE_SCAN_TOOLTIP = _launcher_tools_tab.FACE_SCAN_TOOLTIP
FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP = _launcher_tools_tab.FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP
IMAGE_SCAN_TOOLTIP = _launcher_tools_tab.IMAGE_SCAN_TOOLTIP
IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP = _launcher_tools_tab.IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP

if os.name == "nt":
    PADX = 2
    PADY = 2
    BUTTON_PADDING = (8, 4)
    PAD = 6
else:
    PADX = 4
    PADY = 4
    BUTTON_PADDING = (10, 6)
    PAD = 12

PAD_OUTER = 16
BUTTON_STYLE = "Launcher.TButton"


def close_blocked_by_running_command(busy: bool) -> bool:
    return busy


class BildebankLauncher:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.config = load_launcher_config()
        self.collection_path = self.config.collection_path
        self.busy = False
        self.closing = False

        self.root = tk.Tk()
        self.root.title("Bildebank")
        self.root.minsize(640, 460)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.status_value: tk.StringVar = tk.StringVar(value="")
        self.notebook: ttk.Notebook | None = None
        self.main_tab: MainTab | None = None
        self.import_tab: ImportTab | None = None
        self.tools_tab: ToolsTab | None = None
        self.setup: SetupTab | None = None
        self.log_text: tk.Text | None = None
        self.buttons: list[Any] = []
        self.cancel_command_button: ttk.Button | None = None
        self.exit_button: ttk.Button | None = None
        self.tooltips: list[Tooltip] = []
        self.active_progress_log_key: str | None = None
        self.active_progress_log_range: tuple[str, str] | None = None
        self.command_runner = CommandRunner(
            post_to_ui=self._post_to_tk,
            on_output=self._log_process_output,
        )
        self._build_gui()
        assert self.main_tab is not None
        self.main_tab.update_migration_status()
        self._refresh_state()
        self.main_tab.start_update_status_refresh()
        assert self.setup is not None
        self.setup.start_status_refresh()
        self._log(f"Valgt bildesamling: {self.collection_path}")
        self.main_tab.show_initial_migration_status()
        self.setup.log_unsupported_installers()

    def run(self) -> None:
        self.root.mainloop()

    def _require_setup(self) -> SetupTab:
        assert self.setup is not None
        return self.setup

    def _post_to_tk(self, callback: Callable[[], None]) -> bool:
        if self.closing:
            return False

        def guarded_callback() -> None:
            if self.closing:
                return
            try:
                if not self.root.winfo_exists():
                    return
            except self.tk.TclError:
                return
            callback()

        try:
            self.root.after(0, guarded_callback)
        except (RuntimeError, self.tk.TclError):
            return False
        return True

    def _destroy_root(self) -> None:
        self.closing = True
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass

    def _on_close(self) -> None:
        if close_blocked_by_running_command(self.busy):
            from tkinter import messagebox

            message = "Vent til jobben som kjører er ferdig før du lukker Bildebank-vinduet."
            self._log(message)
            messagebox.showinfo("Bildebank jobber", message, parent=self.root)
            return
        self.closing = True
        if self.main_tab is not None:
            self.main_tab.stop_server_process()
        self._destroy_root()

    def _set_collection_path(self, collection_path: Path) -> None:
        self.collection_path = collection_path
        self.config = LauncherConfig(collection_path=collection_path)
        save_launcher_config(self.config)

    def _build_gui(self) -> None:
        tk = self.tk
        ttk = self.ttk

        ttk.Style(self.root).configure(BUTTON_STYLE, padding=BUTTON_PADDING)

        # Ytterste padding i vinduet.
        outer = ttk.Frame(self.root, padding=PAD_OUTER)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        title = ttk.Label(outer, text="Bildebank", font=("", 15, "bold"))
        title.grid(row=0, column=0, sticky="w")

        self.notebook = ttk.Notebook(outer)
        # pady er padding over og under notebooken.
        self.notebook.grid(row=1, column=0, sticky="ew", pady=(PAD))

        # padding er paddingen inni hver side av notebook
        self.main_tab = MainTab(
            tk=tk,
            ttk=ttk,
            notebook=self.notebook,
            root=self.root,
            button=self._button,
            run_waiting_command=self._run_waiting_command,
            get_collection_path=lambda: self.collection_path,
            set_collection_path=self._set_collection_path,
            is_busy=lambda: self.busy,
            post_to_ui=self._post_to_tk,
            log=self._log,
            refresh_launcher=self._refresh_state,
            set_launcher_buttons_enabled=self._set_buttons_enabled,
            add_tooltip=self._add_tooltip,
            show_log_review_question=self._show_log_review_question,
            show_error=self._show_error,
            on_close=self._on_close,
            destroy_root=self._destroy_root,
            padding=PAD,
            padx=PADX,
            pady=PADY,
        )
        self.notebook.add(self.main_tab.frame, text="Bildebank")
        self.import_tab = ImportTab(
            tk=tk,
            ttk=ttk,
            notebook=self.notebook,
            root=self.root,
            button=self._button,
            run_waiting_command=self._run_waiting_command,
            get_collection_path=lambda: self.collection_path,
            log=self._log,
            refresh_launcher=self._refresh_state,
            add_tooltip=self._add_tooltip,
            ask_string=self._ask_string,
            padding=PAD,
            padx=PADX,
            pady=PADY,
        )
        self.notebook.add(self.import_tab.frame, text="Import av bilder")
        self.tools_tab = ToolsTab(
            tk=tk,
            ttk=ttk,
            notebook=self.notebook,
            root=self.root,
            button=self._button,
            run_waiting_command=self._run_waiting_command,
            get_collection_path=lambda: self.collection_path,
            get_setup=self._require_setup,
            log=self._log,
            refresh_launcher=self._refresh_state,
            add_tooltip=self._add_tooltip,
            ask_string=self._ask_string,
            show_log_review_question=self._show_log_review_question,
            show_error=self._show_error,
            padding=PAD,
            padx=PADX,
            pady=PADY,
        )
        self.notebook.add(self.tools_tab.frame, text="Verktøy")
        self.setup = SetupTab(
            tk=tk,
            ttk=ttk,
            notebook=self.notebook,
            root=self.root,
            button=self._button,
            run_waiting_command=self._run_waiting_command,
            post_to_ui=self._post_to_tk,
            log=self._log,
            confirm_rerun=self._confirm_rerun,
            on_status_changed=lambda: self._set_buttons_enabled(not self.busy),
            padding=PAD,
            pady=PADY,
        )
        self.notebook.add(self.setup.frame, text="Oppsett")

        log_frame = ttk.Frame(outer)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        ttk.Label(log_frame, text="Logg:").grid(row=0, column=0, sticky="w")

        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")

        footer = ttk.Frame(outer)
        footer.grid(row=3, column=0, sticky="ew", pady=(PAD, 0))
        footer.columnconfigure(0, weight=1)
        status = ttk.Label(footer, textvariable=self.status_value)
        status.grid(row=0, column=0, sticky="w")
        self.cancel_command_button = self._button(
            footer,
            text="Avbryt jobb",
            command=self._cancel_active_command,
        )
        self.cancel_command_button.grid(row=0, column=1, sticky="e", padx=(0, PADX))
        self.exit_button = self._button(
            footer,
            text="Avslutt Bildebank",
            command=self._on_close,
        )
        self.exit_button.grid(row=0, column=2, sticky="e")

    def _refresh_state(self) -> None:
        assert self.main_tab is not None
        assert self.import_tab is not None
        assert self.tools_tab is not None

        for tooltip in self.tooltips:
            tooltip.hide()
        self.tooltips = []
        self.buttons = []
        main_state = self.main_tab.refresh()
        self.buttons.extend(
            self.import_tab.refresh(available=main_state.available)
        )
        self.buttons.extend(
            self.tools_tab.refresh(available=main_state.available)
        )
        self.buttons.extend(main_state.buttons)

        self._set_buttons_enabled(not self.busy)

    def _button(self, parent: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("style", BUTTON_STYLE)
        return self.ttk.Button(parent, **kwargs)

    def _ask_string(
        self,
        title: str,
        message: str,
        *,
        initialvalue: str = "",
    ) -> str | None:
        return ask_string_dialog(
            tk=self.tk,
            ttk=self.ttk,
            root=self.root,
            button=self._button,
            title=title,
            message=message,
            initialvalue=initialvalue,
        )

    def _add_tooltip(self, widget: Any, text: str) -> None:
        self.tooltips.append(Tooltip(widget, text))

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.buttons:
            button.configure(state=state)
        if self.main_tab is not None:
            self.main_tab.set_buttons_enabled(enabled)
        if self.tools_tab is not None:
            self.tools_tab.update_dependency_tooltips()
        if self.setup is not None:
            assert self.main_tab is not None
            self.setup.set_buttons_enabled(
                enabled,
                migration_required=self.main_tab.migration_required,
                migration_status_error=self.main_tab.migration_status_error,
            )
        if self.exit_button is not None:
            self.exit_button.configure(state=state)
        if self.cancel_command_button is not None:
            cancel_state = (
                "normal"
                if self.busy and self.command_runner.cancellable and not self.command_runner.cancel_requested
                else "disabled"
            )
            self.cancel_command_button.configure(state=cancel_state)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        self.status_value.set(message)
        self._set_buttons_enabled(not busy)

    def _cancel_active_command(self) -> None:
        try:
            cancel_requested = self.command_runner.request_cancel()
        except OSError as exc:
            self._log(f"Kunne ikke avbryte jobben: {exc}")
            return
        if not cancel_requested:
            return
        self._set_buttons_enabled(False)
        self.status_value.set("Avbryter jobb ...")
        self._log("Ber jobben avbryte kontrollert ...")

    def _confirm_rerun(self, title: str, message: str) -> bool:
        from tkinter import messagebox

        return bool(messagebox.askyesno(title, message, parent=self.root))

    def _show_log_review_question(
        self,
        title: str,
        message: str,
        *,
        yes_text: str,
        no_text: str,
        on_yes: Callable[[], None],
        on_no: Callable[[], None],
    ) -> None:
        show_log_review_question(
            tk=self.tk,
            ttk=self.ttk,
            root=self.root,
            button=self._button,
            set_busy=self._set_busy,
            title=title,
            message=message,
            yes_text=yes_text,
            no_text=no_text,
            on_yes=on_yes,
            on_no=on_no,
        )

    def _run_waiting_command(
        self,
        command: list[str],
        *,
        running_message: str,
        success_message: str,
        failure_message: str,
        on_success: Callable[[], None] | None = None,
        stdin_text: str | None = None,
        cancellable: bool = False,
    ) -> None:
        def on_start() -> None:
            self._set_busy(True, running_message)
            self._clear_active_progress_log()
            self._log("$ " + " ".join(command))

        self.command_runner.start(
            command,
            on_start=on_start,
            on_start_failed=lambda exc: self._command_start_failed(failure_message, exc),
            on_finished=lambda return_code, cancel_requested: self._command_finished(
                return_code,
                success_message=success_message,
                failure_message=failure_message,
                on_success=on_success,
                cancel_requested=cancel_requested,
            ),
            stdin_text=stdin_text,
            cancellable=cancellable,
        )

    def _command_start_failed(self, failure_message: str, exc: OSError) -> None:
        from tkinter import messagebox

        self._set_busy(False)
        self._clear_active_progress_log()
        self._log(f"{failure_message} {exc}")
        messagebox.showerror("Feil", failure_message)

    def _command_finished(
        self,
        return_code: int,
        *,
        success_message: str,
        failure_message: str,
        on_success: Callable[[], None] | None,
        cancel_requested: bool = False,
    ) -> None:
        from tkinter import messagebox

        self._set_busy(False)
        self._clear_active_progress_log()
        if return_code == 0:
            self._log(success_message)
            if on_success is not None:
                on_success()
            return
        if cancel_requested:
            self._log(f"Jobben ble avbrutt. Avsluttet med kode {return_code}.")
            return
        self._log(f"{failure_message} Avsluttet med kode {return_code}.")
        messagebox.showerror("Feil", failure_message)

    def _show_error(self, message: str, exc: BaseException) -> None:
        from tkinter import messagebox

        messagebox.showerror("Feil", message)
        self._log(f"{message} {exc}")

    def _log_process_output(self, message: str) -> None:
        self._log(message, progress_key=progress_log_key(message))

    def _clear_active_progress_log(self) -> None:
        self.active_progress_log_key = None
        self.active_progress_log_range = None

    def _log(self, message: str, *, progress_key: str | None = None) -> None:
        if not message:
            return
        assert self.log_text is not None
        self.log_text.configure(state="normal")
        if (
            progress_key is not None
            and progress_key == self.active_progress_log_key
            and self.active_progress_log_range is not None
        ):
            start, end = self.active_progress_log_range
            self.log_text.delete(start, end)
            self.log_text.insert(start, message + "\n")
        else:
            start = self.log_text.index("end-1c")
            self.log_text.insert("end", message + "\n")
        if progress_key is not None:
            self.active_progress_log_key = progress_key
            self.active_progress_log_range = (start, f"{start} + {len(message) + 1} chars")
        else:
            self._clear_active_progress_log()
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> int:
    launcher = BildebankLauncher()
    launcher.run()
    return 0
