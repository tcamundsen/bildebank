from __future__ import annotations

from importlib import resources
import os
import subprocess
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageTk

from . import launcher_import_tab as _launcher_import_tab
from . import launcher_status as _launcher_status
from . import launcher_tools_tab as _launcher_tools_tab
from .launcher_commands import (
    backup_command,
    create_command,
    launcher_command,
    migrate_command,
    run_server_command,
    server_browser_url,
    update_command,
)
from .launcher_status import (
    LauncherConfig,
    LauncherUpdateStatus,
    check_launcher_update_status,
    collection_needs_migration,
    is_collection_created,
    load_launcher_config,
    save_launcher_config,
)
from .launcher_runner import CommandRunner, progress_log_key
from .launcher_import_tab import ImportTab
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
suggest_import_name = _launcher_import_tab.suggest_import_name
source_is_collection_or_inside = _launcher_import_tab.source_is_collection_or_inside
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
def open_server_browser_window() -> bool:
    return bool(webbrowser.open(server_browser_url(), new=1))


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
        self.server_process: subprocess.Popen[Any] | None = None
        self.closing = False

        self.root = tk.Tk()
        self.root.title("Bildebank")
        self.root.minsize(640, 460)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.collection_value: tk.StringVar = tk.StringVar(value="Bildesamling: " + str(self.collection_path))
        self.status_value: tk.StringVar = tk.StringVar(value="")
        self.notebook: ttk.Notebook | None = None
        self.main_tab: ttk.Frame | None = None
        self.import_tab: ImportTab | None = None
        self.tools_tab: ToolsTab | None = None
        self.setup: SetupTab | None = None
        self.main_button_frame: ttk.Frame | None = None
        self.log_text: tk.Text | None = None
        self.buttons: list[Any] = []
        self.choose_collection_button: ttk.Button | None = None
        self.create_collection_button: ttk.Button | None = None
        self.create_collection_tooltip: Tooltip | None = None
        self.start_server_button: ttk.Button | None = None
        self.backup_button: ttk.Button | None = None
        self.update_button: ttk.Button | None = None
        self.update_button_icons: dict[str, ImageTk.PhotoImage] = {}
        self.cancel_command_button: ttk.Button | None = None
        self.exit_button: ttk.Button | None = None
        self.tooltips: list[Tooltip] = []
        self.migration_required = False
        self.migration_status_error: str | None = None
        self.migration_dialog_shown = False
        self.update_status = LauncherUpdateStatus("checking")
        self.update_checking = False
        self.active_progress_log_key: str | None = None
        self.active_progress_log_range: tuple[str, str] | None = None
        self.command_runner = CommandRunner(
            post_to_ui=self._post_to_tk,
            on_output=self._log_process_output,
        )
        self.update_button_icons = self._load_update_button_icons()

        self._build_gui()
        self._update_migration_status()
        self._refresh_state()
        self._start_update_status_refresh()
        assert self.setup is not None
        self.setup.start_status_refresh()
        self._log(f"Valgt bildesamling: {self.collection_path}")
        if self.migration_required:
            self.root.after(0, self._show_migration_required_dialog)
        elif self.migration_status_error is not None:
            self.root.after(0, self._show_migration_status_error)
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
        self._stop_server_process()
        self._destroy_root()

    def _stop_server_process(self) -> None:
        process = self.server_process
        if process is None:
            return
        if process.poll() is not None:
            self.server_process = None
            return
        self._log("Stopper Bildebank-server ...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._log("Bildebank-serveren svarte ikke på stopp, avslutter hardt ...")
            process.kill()
            process.wait(timeout=5)
        self.server_process = None
        self._log("Bildebank-server stoppet.")

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
        self.main_tab = ttk.Frame(self.notebook, padding=PAD)
        self.notebook.add(self.main_tab, text="Bildebank")
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

        self.main_tab.columnconfigure(0, weight=1)

        collection_frame = ttk.Frame(self.main_tab)
        collection_frame.grid(row=0, column=0, sticky="w")

        collection_label = ttk.Label(collection_frame, textvariable=self.collection_value, wraplength=560)
        collection_label.grid(row=0, column=0, sticky="w", padx=PADX, pady=PADY, columnspan=2)

        self.choose_collection_button = self._button(
            collection_frame,
            text="Velg annen plassering",
            command=self._choose_collection,
        )
        self.choose_collection_button.grid(row=1, column=0, sticky="w", padx=PADX, pady=PADY)
        self.create_collection_button = self._button(
            collection_frame,
            text="Opprett bildesamling",
            command=self._create_collection,
        )
        self.create_collection_button.grid(row=1, column=1, sticky="w", padx=PADX, pady=PADY)
        self.create_collection_tooltip = Tooltip(
            self.create_collection_button,
            self._create_collection_tooltip(False),
        )

        separator = ttk.Separator(self.main_tab, orient="horizontal")
        separator.grid(row=1, column=0, sticky="ew", pady=PAD)

        self.main_button_frame = ttk.Frame(self.main_tab)
        self.main_button_frame.grid(row=2, column=0, sticky="w")

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
        assert self.main_button_frame is not None
        assert self.import_tab is not None
        assert self.tools_tab is not None

        for tooltip in self.tooltips:
            tooltip.hide()
        if self.create_collection_tooltip is not None:
            self.create_collection_tooltip.hide()
        self.tooltips = []
        for frame in (self.main_button_frame,):
            for child in frame.winfo_children():
                child.destroy()
        self.buttons = []
        self.start_server_button = None
        self.backup_button = None
        self.update_button = None
        collection_created = is_collection_created(self.collection_path)
        self.buttons.extend(
            self.import_tab.refresh(
                available=(
                    collection_created
                    and not self.migration_required
                    and self.migration_status_error is None
                )
            )
        )
        self.buttons.extend(
            self.tools_tab.refresh(
                available=(
                    collection_created
                    and not self.migration_required
                    and self.migration_status_error is None
                )
            )
        )
        if self.create_collection_tooltip is not None:
            self.create_collection_tooltip.text = self._create_collection_tooltip(collection_created)

        if collection_created:
            if self.migration_required:
                migrate_button = self._button(self.main_button_frame, text="Migrer", command=self._run_migrate)
                migrate_button.grid(row=0, column=0, padx=PADX, pady=PADY, sticky="ew")
                exit_button = self._button(
                    self.main_button_frame,
                    text="Avslutt uten å migrere",
                    command=self._on_close,
                )
                exit_button.grid(row=0, column=1, padx=PADX, pady=PADY, sticky="ew")
                self.buttons.extend([migrate_button, exit_button])
            elif self.migration_status_error is not None:
                pass
            else:
                start_button = self._button(
                    self.main_button_frame,
                    text="Start Bildebank i nettleser",
                    command=self._start_server,
                )
                self.start_server_button = start_button
                start_button.grid(row=0, column=0, padx=PADX, pady=PADY, columnspan=2, sticky="ew")
                update_button = self._button(
                    self.main_button_frame,
                    text=self._update_button_text(),
                    command=self._on_update_button_clicked,
                )
                update_button.grid(row=0, column=2, padx=PADX, pady=PADY, sticky="ew")
                self.update_button = update_button
                self._add_tooltip(
                    update_button,
                    "Oppdaterer Bildebank til siste utgave. "
                    "Dette tilsvarer kommandoen 'bildebank update' ",
                )
                backup_button = self._button(
                    self.main_button_frame,
                    text="Ta backup",
                    command=self._start_backup_flow,
                )
                self.backup_button = backup_button
                backup_button.grid(row=0, column=3, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    backup_button,
                    "Kjører først backup dry-run og viser planen i loggen. "
                    "Faktisk backup speiler bildesamlingen og kan slette filer i backupmålet.",
                )

                self.buttons.extend(
                    [
                        start_button,
                        update_button,
                        backup_button,
                    ]
                )
        else:
            start_button = self._button(
                self.main_button_frame,
                text="Start Bildebank i nettleser",
                command=self._start_server,
            )
            self.start_server_button = start_button
            start_button.grid(row=0, column=0, padx=PADX, pady=PADY, columnspan=2, sticky="ew")
            update_button = self._button(
                self.main_button_frame,
                text=self._update_button_text(),
                command=self._on_update_button_clicked,
            )
            update_button.grid(row=0, column=2, padx=PADX, pady=PADY, sticky="ew")
            self.update_button = update_button
            self._add_tooltip(
                update_button,
                "Oppdaterer Bildebank til siste utgave. "
                "Dette tilsvarer kommandoen 'bildebank update' ",
            )
            backup_button = self._button(
                self.main_button_frame,
                text="Ta backup",
                command=self._start_backup_flow,
            )
            self.backup_button = backup_button
            backup_button.grid(row=0, column=3, padx=PADX, pady=PADY, sticky="ew")
            self._add_tooltip(
                backup_button,
                "Backup kan brukes etter at bildesamlingen er opprettet.",
            )
            self.buttons.extend([start_button, update_button, backup_button])

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

    def _create_collection_tooltip(self, collection_created: bool) -> str:
        if collection_created:
            return "Mappen er allerede en bildesamling."
        return (
            "Lag en bildesamling på stedet vist til venstre. Klikk 'Velg annen plassering' "
            "for å finne bildesamlingen din eller opprette en ny et annet sted."
        )

    def _update_button_text(self) -> str:
        if self.update_status.status == "checking":
            return "Ser etter oppdateringer ..."
        if self.update_status.status == "available":
            return "Installer oppdatering"
        return "Se etter oppdateringer"

    def _load_update_button_icons(self) -> dict[str, ImageTk.PhotoImage]:
        icons: dict[str, ImageTk.PhotoImage] = {}
        try:
            icon_root = resources.files("bildebank").joinpath("assets", "icons")
            for key, filename in {
                "search": "search.png",
                "green-check": "green-check.png",
            }.items():
                with icon_root.joinpath(filename).open("rb") as icon_file:
                    image = Image.open(icon_file)
                    resized = image.resize((18, 18), Image.Resampling.LANCZOS)
                    icons[key] = ImageTk.PhotoImage(resized)
        except Exception as exc:  # noqa: BLE001 - launcher must work without button icons
            self._log(f"Kunne ikke laste ikon for oppdateringsknapp: {exc}")
            return {}
        return icons

    def _update_button_icon(self) -> ImageTk.PhotoImage | None:
        icon_key = "green-check" if self.update_status.status == "available" else "search"
        return self.update_button_icons.get(icon_key)

    def _apply_update_button_state(self) -> None:
        if self.update_button is None:
            return
        icon = self._update_button_icon()
        if icon is None:
            self.update_button.configure(text=self._update_button_text(), image="", compound="none")
        else:
            self.update_button.configure(text=self._update_button_text(), image=icon, compound="left")
        if self.update_status.status == "checking" or self.busy:
            self.update_button.configure(state="disabled")

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.buttons:
            button.configure(state=state)
        collection_created = is_collection_created(self.collection_path)
        dependency_buttons_enabled = enabled and not self.migration_required and self.migration_status_error is None
        if self.choose_collection_button is not None:
            collection_state = "normal" if dependency_buttons_enabled else "disabled"
            self.choose_collection_button.configure(state=collection_state)
        if self.create_collection_button is not None:
            create_state = "normal" if dependency_buttons_enabled and not collection_created else "disabled"
            self.create_collection_button.configure(state=create_state)
        if self.start_server_button is not None and not collection_created:
            self.start_server_button.configure(state="disabled")
        if self.backup_button is not None and not collection_created:
            self.backup_button.configure(state="disabled")
        if self.tools_tab is not None:
            self.tools_tab.update_dependency_tooltips()
        if self.update_button is not None and (self.update_status.status == "checking" or not enabled):
            self.update_button.configure(state="disabled")
        if self.setup is not None:
            self.setup.set_buttons_enabled(
                enabled,
                migration_required=self.migration_required,
                migration_status_error=self.migration_status_error,
            )
        self._apply_update_button_state()
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

    def _update_migration_status(self) -> None:
        self.migration_required = False
        self.migration_status_error = None
        if not is_collection_created(self.collection_path):
            return
        try:
            self.migration_required = collection_needs_migration(self.collection_path)
        except Exception as exc:  # noqa: BLE001 - launcher must show a controlled startup error
            self.migration_status_error = str(exc)

    def _migration_required_message(self) -> str:
        return (
            "Bildebank-databasen må oppdateres før programmet kan brukes.\n\n"
            "Velg Migrer for å kjøre samme migrering som kommandoen "
            "`bildebank migrate`, eller avslutt uten å gjøre endringer."
        )

    def _show_migration_required_dialog(self) -> None:
        if self.migration_dialog_shown or not self.migration_required:
            return
        self.migration_dialog_shown = True

        dialog = self.tk.Toplevel(self.root)
        dialog.title("Migrering kreves")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", self._on_close)

        frame = self.ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        self.ttk.Label(frame, text="Migrering kreves", font=("", 12, "bold")).grid(row=0, column=0, sticky="w")
        self.ttk.Label(frame, text=self._migration_required_message(), wraplength=420).grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(10, 16),
        )
        self._button(frame, text="Migrer", command=lambda: self._migrate_from_dialog(dialog)).grid(
            row=2,
            column=0,
            sticky="e",
            padx=(0, 8),
        )
        self._button(frame, text="Avslutt uten å migrere", command=self._on_close).grid(
            row=2,
            column=1,
            sticky="w",
        )

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - dialog.winfo_width()) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - dialog.winfo_height()) // 2, 0)
        dialog.geometry(f"+{x}+{y}")

    def _migrate_from_dialog(self, dialog: Any) -> None:
        dialog.destroy()
        self._run_migrate()

    def _show_migration_status_error(self) -> None:
        if self.migration_status_error is None:
            return
        from tkinter import messagebox

        messagebox.showerror(
            "Kan ikke starte Bildebank",
            "Databasestatus kunne ikke kontrolleres.\n\n" + self.migration_status_error,
            parent=self.root,
        )

    def _choose_collection(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(
            title="Velg plassering for bildesamling",
            initialdir=str(self.collection_path.parent),
        )
        if not selected:
            self._log("Valg av bildesamling avbrutt.")
            return
        self._stop_server_process()
        self.collection_path = Path(selected)
        self.collection_value.set("Bildesamling: " + str(self.collection_path))
        self.config = LauncherConfig(collection_path=self.collection_path)
        try:
            save_launcher_config(self.config)
        except OSError as exc:
            self._show_error("Kunne ikke lagre valgt plassering.", exc)
        self._log(f"Valgt bildesamling: {self.collection_path}")
        self.migration_dialog_shown = False
        self._update_migration_status()
        self._refresh_state()
        if self.migration_required:
            self._show_migration_required_dialog()
        elif self.migration_status_error is not None:
            self._show_migration_status_error()

    def _create_collection(self) -> None:
        self._log("Oppretter bildesamling ...")
        self._run_waiting_command(
            create_command(self.collection_path),
            running_message="Oppretter bildesamling ...",
            success_message="Bildesamling opprettet.",
            failure_message="Kunne ikke opprette bildesamlingen.",
            on_success=self._refresh_state,
        )

    def _start_backup_flow(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(
            title="Velg backup-plassering",
            initialdir=str(self.collection_path.parent),
        )
        if not selected:
            self._log("Backup avbrutt: ingen plassering valgt.")
            return
        self._run_backup_dry_run(Path(selected))

    def _run_backup_dry_run(self, backup_parent: Path) -> None:
        self._log(f"Kontrollerer backup til {backup_parent} ...")
        self._run_waiting_command(
            backup_command(self.collection_path, backup_parent, dry_run=True),
            running_message="Kontrollerer backup ...",
            success_message="Backup dry-run fullført. Se planen i loggen.",
            failure_message="Backup dry-run feilet.",
            on_success=lambda: self._confirm_backup(backup_parent),
            cancellable=True,
        )

    def _confirm_backup(self, backup_parent: Path) -> None:
        backup_dir = backup_parent / self.collection_path.name
        self._show_log_review_question(
            "Ta backup?",
            (
                "Dry-run er fullført og planen står i loggen.\n\n"
                "Faktisk backup speiler bildesamlingen. Det kan slette filer fra "
                "backupen som ikke finnes i bildesamlingen.\n\n"
                f"Backupmappe:\n{backup_dir}\n\n"
                "Vil du kjøre faktisk backup nå?"
            ),
            yes_text="Kjør backup",
            no_text="Avbryt",
            on_yes=lambda: self._run_backup(backup_parent),
            on_no=lambda: self._log("Backup avbrutt etter dry-run."),
        )

    def _run_backup(self, backup_parent: Path) -> None:
        self._log(f"Tar backup til {backup_parent} ...")
        self._run_waiting_command(
            backup_command(self.collection_path, backup_parent),
            running_message="Tar backup ...",
            success_message="Backup fullført.",
            failure_message="Backup feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _run_migrate(self) -> None:
        self._log("Migrerer database ...")
        self._run_waiting_command(
            migrate_command(self.collection_path),
            running_message="Migrerer database ...",
            success_message="Migrering fullført.",
            failure_message="Migrering feilet.",
            on_success=self._migration_finished,
        )

    def _migration_finished(self) -> None:
        self._update_migration_status()
        self.migration_dialog_shown = False
        self._refresh_state()
        if self.migration_required:
            self._show_migration_required_dialog()
        elif self.migration_status_error is not None:
            self._show_migration_status_error()

    def _run_update(self) -> None:
        from tkinter import messagebox

        if not messagebox.askyesno(
            "Oppdater Bildebank?",
            (
                "Bildebank-serveren stoppes hvis den kjører. Etter oppdateringen "
                "starter Bildebank-vinduet på nytt."
            ),
            parent=self.root,
        ):
            self._log("Oppdatering avbrutt.")
            return
        self._stop_server_process()
        self._log("Oppdaterer Bildebank ...")
        self._run_waiting_command(
            update_command(),
            running_message="Oppdaterer Bildebank ...",
            success_message="Bildebank er oppdatert. Starter Bildebank-vinduet på nytt ...",
            failure_message="Oppdatering feilet.",
            on_success=self._restart_launcher,
        )

    def _on_update_button_clicked(self) -> None:
        if self.update_status.status == "available":
            self._run_update()
            return
        self._start_update_status_refresh()

    def _start_update_status_refresh(self) -> None:
        if self.update_checking:
            return
        self.update_checking = True
        self.update_status = LauncherUpdateStatus("checking")
        self._apply_update_button_state()
        self._set_buttons_enabled(not self.busy)
        thread = threading.Thread(target=self._update_status_worker, daemon=True)
        thread.start()

    def _update_status_worker(self) -> None:
        status = check_launcher_update_status()
        self._post_to_tk(lambda: self._update_status_finished(status))

    def _update_status_finished(self, status: LauncherUpdateStatus) -> None:
        self.update_checking = False
        self.update_status = status
        if status.status == "error" and status.detail:
            self._log(f"Oppdateringssjekk feilet: {status.detail}")
        elif status.status == "skipped" and status.detail:
            self._log(f"Oppdateringssjekk hoppet over: {status.detail}")
        self._apply_update_button_state()
        self._set_buttons_enabled(not self.busy)

    def _restart_launcher(self) -> None:
        from tkinter import messagebox

        try:
            subprocess.Popen(launcher_command())
        except OSError as exc:
            messagebox.showerror("Kunne ikke starte Bildebank", "Bildebank-vinduet kunne ikke startes på nytt.")
            self._log(f"Kunne ikke starte Bildebank-vinduet på nytt: {exc}")
            return
        self._log("Nytt Bildebank-vindu startet. Lukker dette vinduet.")
        self._destroy_root()

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

    def _start_server(self) -> None:
        from tkinter import messagebox

        self._update_migration_status()
        if self.migration_required:
            self._refresh_state()
            self._show_migration_required_dialog()
            return
        if self.migration_status_error is not None:
            self._refresh_state()
            self._show_migration_status_error()
            return

        if self.server_process is not None:
            if self.server_process.poll() is None:
                self._log("Bildebank-server kjører allerede. Åpner nytt vindu.")
                if not open_server_browser_window():
                    self._log(f"Kunne ikke åpne nettleser automatisk. Åpne {server_browser_url()} manuelt.")
                return
            self.server_process = None

        self._log("Starter Bildebank ...")
        try:
            self.server_process = subprocess.Popen(run_server_command(self.collection_path))
        except OSError as exc:
            messagebox.showerror("Kunne ikke starte Bildebank", "Bildebank-serveren kunne ikke startes.")
            self._log(f"Kunne ikke starte Bildebank: {exc}")
            return
        self._log("Bildebank-serveren starter. Nettleseren åpnes av Bildebank når serveren er klar.")

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
