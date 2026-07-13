from __future__ import annotations

import importlib
from importlib import resources
import json
import locale
import os
import signal
import subprocess
import threading
import tempfile
import webbrowser
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from PIL import Image, ImageTk

from . import db
from .config import (
    load_config,
    set_face_recognition_enabled,
    set_image_search_enabled,
)
from .launcher_commands import (
    backup_command,
    check_source_command,
    cleanup_pending_deletes_apply_command,
    cleanup_pending_deletes_list_command,
    create_command,
    deep_doctor_command,
    doctor_command,
    download_face_model_command,
    export_person_command,
    face_scan_command,
    geo_scan_command,
    image_scan_command,
    import_command,
    insightface_install_command,
    launcher_command,
    make_browser_command,
    make_people_browser_command,
    make_person_browser_command,
    make_thumbnails_command,
    migrate_command,
    openclip_install_command,
    read_unimport_target_change_report,
    rescan_source_command,
    run_server_command,
    server_browser_url,
    unimport_source_command,
    unimport_source_dry_run_command,
    update_command,
    vacuum_command,
)
from .launcher_status import (
    InsightFaceDependencyStatus,
    InsightFaceModelStatus,
    LauncherConfig,
    LauncherUpdateStatus,
    OpenClipModelStatus,
    RegisteredPerson,
    check_launcher_update_status,
    collection_needs_migration,
    dependency_setup_button_state,
    face_model_download_button_state,
    insightface_dependency_status,
    insightface_install_supported,
    insightface_model_status,
    is_collection_created,
    load_launcher_config,
    openclip_dependency_status,
    openclip_install_supported,
    openclip_model_status,
    program_repo_root,
    registered_persons,
    registered_sources,
    rescan_source_candidates,
    save_launcher_config,
)
from .pending_deletes import list_pending_deletes

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
FACE_SCAN_TOOLTIP = (
    "Kjører 'bildebank face-scan'. Denne kommandoen scanner bildene etter ansikter. "
    "Må kjøres på nytt når du legger til nye biler."
)
FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP = (
    "InsightFace må installeres og valgt ansiktsmodell må lastes ned på Oppsett-fanen "
    "for å slå på ansiktsgjenkjenning."
)
FACE_SCAN_SETUP_DOWNLOAD_MESSAGE = (
    "Ansiktsgjenkjenning krever InsightFace og en ansiktsmodell. "
    "Dette kan laste ned litt over 400 MB.\n\n"
    "Vil du installere det som mangler, slå på ansiktsgjenkjenning og søke etter ansikter nå?"
)
FACE_SCAN_ENABLE_MESSAGE = (
    "Ansiktsgjenkjenning er slått av i innstillingene.\n\n"
    "Vil du slå den på og søke etter ansikter nå?"
)
IMAGE_SCAN_TOOLTIP = (
    "Kjører 'bildebank image-scan'. Denne kommandoen gjør at du "
    "kan gjøre klikke Bildesøk i nettleseren og skrive søkeord der. "
    "Kommandoen må scanne nye bilder for at det kan søkes i dem."
)
IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP = (
    "Trykk knappen 'Installer OpenCLIP' på Oppsett-fanen for å slå på bildesøk."
)
IMAGE_SCAN_SETUP_DOWNLOAD_MESSAGE = (
    "Bildesøk krever OpenCLIP og en lokal AI-modell. "
    "Dette kan laste ned flere hundre MB.\n\n"
    "Vil du installere det som mangler, slå på bildesøk og klargjøre bildene nå?"
)
IMAGE_SCAN_ENABLE_MESSAGE = (
    "Bildesøk er slått av i innstillingene.\n\n"
    "Vil du slå det på og klargjøre bildene nå?"
)

PROGRESS_LOG_LABELS = (
    "Import",
    "Import dry-run",
    "Rescan-source",
    "Rescan-source dry-run",
    "Thumbnails",
    "Unimport",
    "Check-source",
    "geo-scan",
    "Doctor filer",
    "Doctor SHA-256",
    "Doctor orphan",
    "Image-scan",
    "Image-search",
    "Face-scan",
    "Face-suggest",
    "Refresh-metadata",
)


def suggest_import_name(source_folder: Path) -> str:
    raw_path = str(source_folder)
    if "\\" in raw_path:
        name = PureWindowsPath(raw_path).name.strip()
    else:
        name = source_folder.name.strip()
    if name:
        return name
    return str(source_folder).strip()


def _resolved_path(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _path_key(path: Path) -> str:
    value = os.path.normpath(str(_resolved_path(path)))
    if os.name == "nt":
        value = value.lower()
    return value


def source_is_collection_or_inside(source_folder: Path, collection_path: Path) -> bool:
    source = _resolved_path(source_folder)
    collection = _resolved_path(collection_path)
    if _path_key(source) == _path_key(collection):
        return True
    try:
        source.relative_to(collection)
    except ValueError:
        return False
    return True


def open_server_browser_window() -> bool:
    return bool(webbrowser.open(server_browser_url(), new=1))


def close_blocked_by_running_command(busy: bool) -> bool:
    return busy


def interruptible_command_creationflags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def interrupt_process(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        ctrl_break_event = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break_event is not None:
            process.send_signal(ctrl_break_event)
            return
    else:
        process.send_signal(signal.SIGINT)
        return
    process.terminate()


def subprocess_output_encoding() -> str:
    return locale.getpreferredencoding(False) or "utf-8"


def progress_log_key(message: str) -> str | None:
    if _is_tqdm_progress_line(message):
        return "tqdm-progress"
    for label in PROGRESS_LOG_LABELS:
        prefix = f"{label}:"
        rest = message[len(prefix):] if message.startswith(prefix) else ""
        if rest and not rest.lstrip().startswith("ferdig") and ("=" in rest or _contains_progress_count(rest)):
            return label
    return None


def _is_tqdm_progress_line(message: str) -> bool:
    stripped = message.lstrip()
    percent, separator, rest = stripped.partition("%|")
    return bool(separator) and percent.isdigit() and _contains_progress_count(rest)


def _contains_progress_count(message: str) -> bool:
    parts = message.replace(",", " ").split()
    return any(_is_progress_count(part) for part in parts)


def _is_progress_count(part: str) -> bool:
    current, separator, total = part.partition("/")
    return bool(separator) and current.isdigit() and total.isdigit()


class Tooltip:
    def __init__(self, widget: Any, text: str, *, delay_ms: int = 500) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.after_id: str | None = None
        self.window: Any | None = None

        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        self._cancel()
        self.after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self.after_id is None:
            return
        self.widget.after_cancel(self.after_id)
        self.after_id = None

    def _show(self) -> None:
        import tkinter as tk

        self.after_id = None
        if self.window is not None or not self.widget.winfo_exists():
            return

        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{x}+{y}")

        label = tk.Label(
            window,
            text=self.text,
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=3,
            justify="left",
            wraplength=360,
        )
        label.pack()
        self.window = window

    def hide(self, _event: Any = None) -> None:
        self._cancel()
        if self.window is None:
            return
        self.window.destroy()
        self.window = None


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
        self.active_command_process: subprocess.Popen[str] | None = None
        self.active_command_cancel_requested = False
        self.active_command_cancellable = False
        self.closing = False

        self.root = tk.Tk()
        self.root.title("Bildebank")
        self.root.minsize(640, 460)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.collection_value: tk.StringVar = tk.StringVar(value="Bildesamling: " + str(self.collection_path))
        self.status_value: tk.StringVar = tk.StringVar(value="")
        self.insightface_status_value: tk.StringVar = tk.StringVar(value="")
        self.insightface_model_status_value: tk.StringVar = tk.StringVar(value="")
        self.openclip_status_value: tk.StringVar = tk.StringVar(value="")
        self.openclip_model_status_value: tk.StringVar = tk.StringVar(value="")
        self.static_browser_hide_out_of_focus_var: tk.BooleanVar = tk.BooleanVar(value=False)
        self.notebook: ttk.Notebook | None = None
        self.main_tab: ttk.Frame | None = None
        self.import_tab: ttk.Frame | None = None
        self.tools_tab: ttk.Frame | None = None
        self.setup_tab: ttk.Frame | None = None
        self.main_button_frame: ttk.Frame | None = None
        self.import_button_frame: ttk.Frame | None = None
        self.tools_button_frame: ttk.Frame | None = None
        self.log_text: tk.Text | None = None
        self.buttons: list[Any] = []
        self.choose_collection_button: ttk.Button | None = None
        self.create_collection_button: ttk.Button | None = None
        self.create_collection_tooltip: Tooltip | None = None
        self.start_server_button: ttk.Button | None = None
        self.backup_button: ttk.Button | None = None
        self.face_scan_button: ttk.Button | None = None
        self.face_scan_tooltip: Tooltip | None = None
        self.image_scan_button: ttk.Button | None = None
        self.image_scan_tooltip: Tooltip | None = None
        self.install_insightface_button: ttk.Button | None = None
        self.install_openclip_button: ttk.Button | None = None
        self.download_face_model_button: ttk.Button | None = None
        self.update_button: ttk.Button | None = None
        self.update_button_icons: dict[str, ImageTk.PhotoImage] = {}
        self.cancel_command_button: ttk.Button | None = None
        self.exit_button: ttk.Button | None = None
        self.tooltips: list[Tooltip] = []
        self.pending_deletes_status: str = "Ukjent"
        self.pending_deletes_count: int | None = None
        self.migration_required = False
        self.migration_status_error: str | None = None
        self.migration_dialog_shown = False
        self.update_status = LauncherUpdateStatus("checking")
        self.update_checking = False
        self.dependency_status_refreshing = False
        self.insightface_status = InsightFaceDependencyStatus("Sjekker")
        self.face_model_status = InsightFaceModelStatus("", "Sjekker")
        self.openclip_status = "Sjekker"
        self.openclip_model_status = OpenClipModelStatus("", "", "Sjekker")
        self.active_progress_log_key: str | None = None
        self.active_progress_log_range: tuple[str, str] | None = None
        self._set_dependency_status_placeholder()
        self.update_button_icons = self._load_update_button_icons()

        self._build_gui()
        self._update_migration_status()
        self._refresh_state()
        self._start_update_status_refresh()
        self._start_dependency_status_refresh()
        self._log(f"Valgt bildesamling: {self.collection_path}")
        if self.migration_required:
            self.root.after(0, self._show_migration_required_dialog)
        elif self.migration_status_error is not None:
            self.root.after(0, self._show_migration_status_error)
        if not insightface_install_supported():
            self._log(
                "Installer InsightFace-knappen er deaktivert: "
                "install-insightface.ps1 er Windows-installasjonsflyt."
            )
        if not openclip_install_supported():
            self._log(
                "Installer OpenCLIP-knappen er deaktivert: "
                "install-openclip.ps1 er Windows-installasjonsflyt."
            )

    def run(self) -> None:
        self.root.mainloop()

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
        self.import_tab = ttk.Frame(self.notebook, padding=PAD)
        self.tools_tab = ttk.Frame(self.notebook, padding=PAD)
        self.setup_tab = ttk.Frame(self.notebook, padding=PAD)
        self.notebook.add(self.main_tab, text="Bildebank")
        self.notebook.add(self.import_tab, text="Import av bilder")
        self.notebook.add(self.tools_tab, text="Verktøy")
        self.notebook.add(self.setup_tab, text="Oppsett")

        self.main_tab.columnconfigure(0, weight=1)
        self.import_tab.columnconfigure(0, weight=1)
        self.tools_tab.columnconfigure(0, weight=1)
        self.setup_tab.columnconfigure(0, weight=1)

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

        self.import_button_frame = ttk.Frame(self.import_tab)
        self.import_button_frame.grid(row=0, column=0, sticky="w")

        insightface_frame = ttk.Frame(self.setup_tab)
        insightface_frame.grid(row=0, column=0, sticky="w")
        ttk.Label(insightface_frame, textvariable=self.insightface_status_value).grid(
            row=0,
            column=0,
            sticky="e",
            padx=PAD,
        )
        self.install_insightface_button = self._button(
            insightface_frame,
            text="Installer InsightFace",
            command=self._install_insightface,
        )
        self.install_insightface_button.grid(row=0, column=1, sticky="w", pady=PADY)
        ttk.Label(insightface_frame, textvariable=self.insightface_model_status_value).grid(
            row=1,
            column=0,
            sticky="e",
            padx=(0, 12),
        )
        self.download_face_model_button = self._button(
            insightface_frame,
            text="Last ned modell",
            command=self._download_face_model,
        )
        self.download_face_model_button.grid(row=1, column=1, sticky="w", pady=PADY)

        setup_separator = ttk.Separator(self.setup_tab, orient="horizontal")
        setup_separator.grid(row=1, column=0, sticky="ew", pady=PAD)

        openclip_frame = ttk.Frame(self.setup_tab)
        openclip_frame.grid(row=2, column=0, sticky="w")
        self.install_openclip_button = self._button(
            openclip_frame,
            text="Installer OpenCLIP",
            command=self._install_openclip,
        )
        self.install_openclip_button.grid(row=0, column=0, sticky="w")
        ttk.Label(openclip_frame, textvariable=self.openclip_status_value).grid(
            row=0,
            column=1,
            sticky="w",
            padx=PAD,
        )
        ttk.Label(openclip_frame, textvariable=self.openclip_model_status_value).grid(
            row=0,
            column=2,
            sticky="w",
        )

        self.tools_button_frame = ttk.Frame(self.tools_tab)
        self.tools_button_frame.grid(row=2, column=0, sticky="w")

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
        assert self.import_button_frame is not None
        assert self.tools_button_frame is not None

        for tooltip in self.tooltips:
            tooltip.hide()
        if self.create_collection_tooltip is not None:
            self.create_collection_tooltip.hide()
        self.tooltips = []
        for frame in (self.main_button_frame, self.import_button_frame, self.tools_button_frame):
            for child in frame.winfo_children():
                child.destroy()
        self.buttons = []
        self.start_server_button = None
        self.backup_button = None
        self.update_button = None
        collection_created = is_collection_created(self.collection_path)
        if self.create_collection_tooltip is not None:
            self.create_collection_tooltip.text = self._create_collection_tooltip(collection_created)

        if collection_created:
            if self.migration_required:
                self.pending_deletes_status = "Ukjent"
                self.pending_deletes_count = None
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
                self.pending_deletes_status = "Ukjent"
                self.pending_deletes_count = None
            else:
                self._refresh_pending_deletes_status()
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

                import_button = self._button(
                    self.import_button_frame,
                    text="Importer bilder",
                    command=self._start_import_flow,
                )
                import_button.grid(row=0, column=0, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    import_button,
                    "Registrerer og importerer bildene fra en mappe, USB-brikke, CD eller disk."
                )
                unimport_button = self._button(
                    self.import_button_frame,
                    text="Angre import",
                    command=self._start_unimport_source_flow,
                )
                unimport_button.grid(row=0, column=1, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    unimport_button,
                    "Reverser en tidligere import. Kontrollerer først at alle registrerte originalfiler "
                    "fortsatt finnes med samme innhold. Krever nøyaktig bekreftelse før noe endres."
                )
                rescan_button = self._button(
                    self.import_button_frame,
                    text="Rescan kilde",
                    command=self._start_rescan_source_flow,
                )
                rescan_button.grid(row=0, column=2, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    rescan_button,
                    "Scan en mappe du har importert bilder fra en gang til. Bruk dette hvis bildebank "
                    "har blitt forbedret, og nå støtter flere bildefiler."
                )
                check_button = self._button(
                    self.import_button_frame,
                    text="Sjekk kilde",
                    command=self._start_check_source_flow,
                )
                check_button.grid(row=0, column=3, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    check_button,
                    "Sjekker at filene i en kildemappe finnes i bildesamlingen med samme SHA-256. "
                    "Hvis alle filene i mappen du har importert fra finnes i bildesamlingen "
                    "så er det i prinsippet trygt å slette mappen du importerte bildene fra.",
                )
                geo_button = self._button(
                    self.tools_button_frame,
                    text="Les GPS fra bilder",
                    command=self._run_geo_scan,
                )
                geo_button.grid(row=0, column=0, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    geo_button,
                    "Scann bildene med exiftool for å finne ut hvor bildene ble tatt."
                )
                thumbs_button = self._button(
                    self.tools_button_frame,
                    text="Lag miniatyrbilder",
                    command=self._run_make_thumbnails,
                )
                thumbs_button.grid(row=0, column=1, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    thumbs_button,
                    "Lag småbilder av alle bildene som kan brukes for at månedsvisning skal laste raskere."
                )
                static_browser_button = self._button(
                    self.tools_button_frame,
                    text="Lag HTML-browser",
                    command=self._run_make_browser,
                )
                static_browser_button.grid(row=2, column=1, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    static_browser_button,
                    "Lag en statisk index.html i bildesamlingen som kan åpnes uten Bildebank-server.",
                )
                static_person_browser_button = self._button(
                    self.tools_button_frame,
                    text="Lag personbrowser",
                    command=self._start_make_person_browser_flow,
                )
                static_person_browser_button.grid(row=2, column=2, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    static_person_browser_button,
                    "Lag en statisk HTML-browser for en valgt person.",
                )
                static_people_browser_button = self._button(
                    self.tools_button_frame,
                    text="Lag alle personbrowsere",
                    command=self._run_make_people_browser,
                )
                static_people_browser_button.grid(row=2, column=3, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    static_people_browser_button,
                    "Lag statiske HTML-browsere for alle registrerte personer.",
                )
                static_browser_hide_checkbox = self.ttk.Checkbutton(
                    self.tools_button_frame,
                    text='Skjul "Ute av fokus"',
                    variable=self.static_browser_hide_out_of_focus_var,
                )
                static_browser_hide_checkbox.grid(
                    row=3,
                    column=1,
                    columnspan=3,
                    padx=PADX,
                    pady=PADY,
                    sticky="w",
                )
                self._add_tooltip(
                    static_browser_hide_checkbox,
                    "Når dette er valgt, får de statiske HTML-browserkommandoene "
                    "flagget --hide-out-of-focus.",
                )
                face_button = self._button(
                    self.tools_button_frame,
                    text="Finn ansikter",
                    command=self._run_face_scan,
                )
                self.face_scan_button = face_button
                face_button.grid(row=0, column=2, padx=PADX, pady=PADY, sticky="ew")
                self.face_scan_tooltip = Tooltip(face_button, FACE_SCAN_TOOLTIP)
                self.tooltips.append(self.face_scan_tooltip)
                image_scan_button = self._button(
                    self.tools_button_frame,
                    text="Klargjør bildesøk",
                    command=self._run_image_scan,
                )
                self.image_scan_button = image_scan_button
                image_scan_button.grid(row=0, column=3, padx=PADX, pady=PADY, sticky="ew")
                self.image_scan_tooltip = Tooltip(image_scan_button, IMAGE_SCAN_TOOLTIP)
                self.tooltips.append(self.image_scan_tooltip)
                doctor_button = self._button(
                    self.tools_button_frame,
                    text="Sjekk bildebank",
                    command=self._run_doctor,
                )
                doctor_button.grid(row=1, column=0, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    doctor_button,
                    "Kjør en status-sjekk av Bildebank og bildesamlingen. "
                    "Du kan få forslag til tiltak som må gjøres."
                )
                deep_doctor_button = self._button(
                    self.tools_button_frame,
                    text="Grundig sjekk",
                    command=self._run_deep_doctor,
                )
                deep_doctor_button.grid(row=1, column=1, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    deep_doctor_button,
                    "Kjør en status-sjekk av Bildebank og bildesamlingen. "
                    "Denne kjører en enda grundigere sjekk, og kan ta litt tid å fullføre. "
                    "Du kan få forslag til tiltak som må gjøres. "
                )
                vacuum_button = self._button(
                    self.tools_button_frame,
                    text="Rydd databaser",
                    command=self._run_vacuum,
                )
                vacuum_button.grid(row=1, column=2, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    vacuum_button,
                    "Bildebank reduserer størrelsen på databasene, hvis mulig."
                )
                pending_button = self._button(
                    self.tools_button_frame,
                    text=self._pending_deletes_button_text(),
                    command=self._show_pending_deletes,
                )
                self._add_tooltip(
                    pending_button,
                    "Hvis det finnes filer her, så har en jobb som skulle flytte eller slette "
                    "blitt avbrutt. Knappen brukes til å fullføre jobben på en trygg måte.",
                )
                pending_button.grid(row=1, column=3, padx=PADX, pady=PADY, sticky="ew")
                export_person_button = self._button(
                    self.tools_button_frame,
                    text="Eksporter person",
                    command=self._start_export_person_flow,
                )
                export_person_button.grid(row=2, column=0, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    export_person_button,
                    "Eksporter en kopi av alle bildene som vises på siden til en person i bildebrowseren.",
                )
                self.buttons.extend(
                    [
                        start_button,
                        update_button,
                        backup_button,
                        import_button,
                        rescan_button,
                        check_button,
                        unimport_button,
                        geo_button,
                        face_button,
                        image_scan_button,
                        thumbs_button,
                        static_browser_button,
                        static_person_browser_button,
                        static_people_browser_button,
                        static_browser_hide_checkbox,
                        doctor_button,
                        deep_doctor_button,
                        vacuum_button,
                        pending_button,
                        export_person_button,
                    ]
                )
        else:
            self.pending_deletes_status = "Ukjent"
            self.pending_deletes_count = None
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
        tk = self.tk
        ttk = self.ttk

        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text=message, wraplength=460, justify="left").grid(row=0, column=0, sticky="w")
        value = tk.StringVar(value=initialvalue)
        entry = ttk.Entry(frame, textvariable=value, width=48)
        entry.grid(row=1, column=0, sticky="ew", pady=(10, 16))
        entry.focus_set()
        entry.selection_range(0, tk.END)

        result: str | None = None

        def accept() -> None:
            nonlocal result
            result = value.get()
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=2, column=0, sticky="e")
        self._button(button_frame, text="Avbryt", command=cancel).grid(row=0, column=0, padx=(0, 8))
        self._button(button_frame, text="OK", command=accept).grid(row=0, column=1)

        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - dialog.winfo_width()) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - dialog.winfo_height()) // 2, 0)
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        self.root.wait_window(dialog)
        return result

    def _add_tooltip(self, widget: Any, text: str) -> None:
        self.tooltips.append(Tooltip(widget, text))

    def _create_collection_tooltip(self, collection_created: bool) -> str:
        if collection_created:
            return "Mappen er allerede en bildesamling."
        return (
            "Lag en bildesamling på stedet vist til venstre. Klikk 'Velg annen plassering' "
            "for å finne bildesamlingen din eller opprette en ny et annet sted."
        )

    def _refresh_pending_deletes_status(self) -> None:
        try:
            rows = list_pending_deletes(self.collection_path)
        except Exception as exc:  # noqa: BLE001 - launcher status must not block other buttons
            self.pending_deletes_status = "Ukjent"
            self.pending_deletes_count = None
            self._log(f"Kunne ikke lese ventende filsletting-status: {exc}")
            return
        self.pending_deletes_count = len(rows)
        self.pending_deletes_status = "OK" if not rows else "Trenger opprydding"

    def _pending_deletes_button_text(self) -> str:
        if self.pending_deletes_status == "OK":
            return "Ventende filsletting: OK"
        if self.pending_deletes_status == "Trenger opprydding":
            assert self.pending_deletes_count is not None
            return f"Ventende filsletting: ! {self.pending_deletes_count}"
        return "Ventende filsletting: ukjent"

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

    def _set_dependency_status_placeholder(self) -> None:
        self.insightface_status_value.set("InsightFace: sjekker ...")
        self.insightface_model_status_value.set("Valgt modell: sjekker ...")
        self.openclip_status_value.set("open_clip: sjekker ...")
        self.openclip_model_status_value.set("AI-modell: sjekker ...")

    def _apply_dependency_status_values(self) -> None:
        self.insightface_status_value.set(f"InsightFace: {self.insightface_status.status}")
        self.insightface_model_status_value.set(
            f"Valgt modell: {self.face_model_status.model_name} ({self.face_model_status.status})"
        )
        self.openclip_status_value.set(f"open_clip: {self.openclip_status}")
        self.openclip_model_status_value.set(f"AI-modell: {self.openclip_model_status.status}")

    def _log_dependency_status_detail(self, label: str, status: str, detail: str) -> None:
        if status == "Feil" and detail:
            self._log(f"{label}-status feilet: {detail}")

    def _start_dependency_status_refresh(self) -> None:
        if self.dependency_status_refreshing:
            return
        self.dependency_status_refreshing = True
        self._set_dependency_status_placeholder()
        self._set_buttons_enabled(not self.busy)
        thread = threading.Thread(target=self._dependency_status_worker, daemon=True)
        thread.start()

    def _dependency_status_worker(self) -> None:
        insightface_status, face_model_status, openclip_status, openclip_model_status = self._load_dependency_status()
        self._post_to_tk(
            lambda: self._dependency_status_finished(
                insightface_status,
                face_model_status,
                openclip_status,
                openclip_model_status,
            )
        )

    def _load_dependency_status(
        self,
    ) -> tuple[InsightFaceDependencyStatus, InsightFaceModelStatus, str, OpenClipModelStatus]:
        try:
            insightface_status = insightface_dependency_status()
        except Exception as exc:  # noqa: BLE001 - setup status must not block launcher startup
            insightface_status = InsightFaceDependencyStatus("Feil", str(exc))
        try:
            face_model_status = insightface_model_status()
        except Exception as exc:  # noqa: BLE001 - setup status must not block launcher startup
            face_model_status = InsightFaceModelStatus("", "Feil", str(exc))
        try:
            openclip_status = openclip_dependency_status()
        except Exception as exc:  # noqa: BLE001 - setup status must not block launcher startup
            openclip_status = f"Feil: {exc}"
        try:
            openclip_model_state = openclip_model_status()
        except Exception as exc:  # noqa: BLE001 - setup status must not block launcher startup
            openclip_model_state = OpenClipModelStatus("", "", "Feil", str(exc))
        return insightface_status, face_model_status, openclip_status, openclip_model_state

    def _dependency_status_finished(
        self,
        insightface_status: InsightFaceDependencyStatus,
        face_model_status: InsightFaceModelStatus,
        openclip_status: str,
        openclip_model_status: OpenClipModelStatus,
    ) -> None:
        self.dependency_status_refreshing = False
        self.insightface_status = insightface_status
        self.face_model_status = face_model_status
        self.openclip_status = openclip_status
        self.openclip_model_status = openclip_model_status
        self._log_dependency_status_detail("InsightFace", insightface_status.status, insightface_status.detail)
        self._log_dependency_status_detail("Ansiktsmodell", face_model_status.status, face_model_status.detail)
        self._log_dependency_status_detail("OpenCLIP-modell", openclip_model_status.status, openclip_model_status.detail)
        self._apply_dependency_status_values()
        self._set_buttons_enabled(not self.busy)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.buttons:
            button.configure(state=state)
        collection_created = is_collection_created(self.collection_path)
        dependency_buttons_enabled = enabled and not self.migration_required and self.migration_status_error is None
        setup_buttons_enabled = enabled and not self.dependency_status_refreshing
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
        if self.face_scan_button is not None:
            self.face_scan_button.configure(state=state)
            if self.face_scan_tooltip is not None:
                self.face_scan_tooltip.text = (
                    FACE_SCAN_TOOLTIP
                    if self.insightface_status.status == "Klar" and self.face_model_status.status == "Lastet ned"
                    else FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP
                )
        if self.image_scan_button is not None:
            self.image_scan_button.configure(state=state)
            if self.image_scan_tooltip is not None:
                self.image_scan_tooltip.text = (
                    IMAGE_SCAN_TOOLTIP
                    if self.openclip_status == "Installert" and self.openclip_model_status.status == "Tilgjengelig"
                    else IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP
                )
        if self.update_button is not None and (self.update_status.status == "checking" or not enabled):
            self.update_button.configure(state="disabled")
        if self.install_insightface_button is not None:
            self.install_insightface_button.configure(
                state=dependency_setup_button_state(
                    enabled=setup_buttons_enabled,
                    migration_required=self.migration_required,
                    migration_status_error=self.migration_status_error,
                    install_supported=insightface_install_supported(),
                )
            )
        if self.install_openclip_button is not None:
            self.install_openclip_button.configure(
                state=dependency_setup_button_state(
                    enabled=setup_buttons_enabled,
                    migration_required=self.migration_required,
                    migration_status_error=self.migration_status_error,
                    install_supported=openclip_install_supported(),
                )
            )
        if self.download_face_model_button is not None:
            self.download_face_model_button.configure(
                state=face_model_download_button_state(
                    enabled=setup_buttons_enabled,
                    migration_required=self.migration_required,
                    migration_status_error=self.migration_status_error,
                    insightface_status=self.insightface_status,
                )
            )
        self._apply_update_button_state()
        if self.exit_button is not None:
            self.exit_button.configure(state=state)
        if self.cancel_command_button is not None:
            cancel_state = (
                "normal"
                if self.busy and self.active_command_cancellable and not self.active_command_cancel_requested
                else "disabled"
            )
            self.cancel_command_button.configure(state=cancel_state)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        self.status_value.set(message)
        self._set_buttons_enabled(not busy)

    def _cancel_active_command(self) -> None:
        process = self.active_command_process
        if process is None and self.busy and self.active_command_cancellable:
            self.active_command_cancel_requested = True
            self._set_buttons_enabled(False)
            self.status_value.set("Avbryter jobb ...")
            self._log("Ber jobben avbryte kontrollert ...")
            return
        if process is None or process.poll() is not None:
            return
        self.active_command_cancel_requested = True
        self._set_buttons_enabled(False)
        self.status_value.set("Avbryter jobb ...")
        self._log("Ber jobben avbryte kontrollert ...")
        try:
            interrupt_process(process)
        except OSError as exc:
            self._log(f"Kunne ikke avbryte jobben: {exc}")

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

    def _run_geo_scan(self) -> None:
        self._log("Scanner GPS-metadata ...")
        self._run_waiting_command(
            geo_scan_command(self.collection_path),
            running_message="Scanner GPS-metadata ...",
            success_message="GPS-scan fullført.",
            failure_message="GPS-scan feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _run_face_scan(self) -> None:
        from tkinter import messagebox

        insightface_missing = self.insightface_status.status != "Klar"
        model_missing = self.face_model_status.status != "Lastet ned"
        face_recognition_disabled = not self._face_recognition_enabled()
        if not insightface_missing and not model_missing and not face_recognition_disabled:
            self._start_face_scan_command()
            return

        if (insightface_missing or model_missing) and not insightface_install_supported():
            messagebox.showerror(
                "Ansiktsgjenkjenning mangler",
                "Ansiktsgjenkjenning kan ikke klargjøres automatisk her. "
                "Installer InsightFace og last ned ansiktsmodellen fra Oppsett-fanen på Windows.",
                parent=self.root,
            )
            self._log("Ansiktsscan avbrutt: InsightFace-oppsett kan ikke kjøres automatisk her.")
            return

        question = FACE_SCAN_SETUP_DOWNLOAD_MESSAGE if insightface_missing or model_missing else FACE_SCAN_ENABLE_MESSAGE
        if not messagebox.askyesno("Klargjør ansiktsgjenkjenning?", question, parent=self.root):
            self._log("Ansiktsscan avbrutt.")
            return

        steps: list[Callable[[Callable[[], None]], None]] = []
        if insightface_missing:
            steps.append(self._run_face_scan_insightface_install_step)
        if model_missing:
            steps.append(self._run_face_scan_model_download_step)
        if face_recognition_disabled:
            steps.append(self._run_face_scan_enable_step)
        self._run_face_scan_setup_steps(steps)

    def _start_face_scan_command(self) -> None:
        self._log("Scanner ansikter ...")
        self._run_waiting_command(
            face_scan_command(self.collection_path),
            running_message="Scanner ansikter ...",
            success_message="Ansiktsscan fullført.",
            failure_message="Ansiktsscan feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _face_recognition_enabled(self) -> bool:
        try:
            return bool(load_config(program_repo_root()).face_recognition.enabled)
        except (OSError, ValueError) as exc:
            self._log(f"Kunne ikke lese innstilling for ansiktsgjenkjenning: {exc}")
            return False

    def _run_face_scan_setup_steps(self, steps: list[Callable[[Callable[[], None]], None]]) -> None:
        if not steps:
            self._start_face_scan_command()
            return
        step = steps[0]
        remaining = steps[1:]
        step(lambda: self._run_face_scan_setup_steps(remaining))

    def _run_face_scan_insightface_install_step(self, on_success: Callable[[], None]) -> None:
        self._log("Installerer InsightFace før ansiktsscan ...")
        self._run_waiting_command(
            insightface_install_command(),
            running_message="Installerer InsightFace ...",
            success_message="InsightFace-installasjon fullført.",
            failure_message="InsightFace-installasjon feilet.",
            on_success=lambda: self._face_scan_insightface_install_finished(on_success),
        )

    def _face_scan_insightface_install_finished(self, on_success: Callable[[], None]) -> None:
        importlib.invalidate_caches()
        on_success()

    def _run_face_scan_model_download_step(self, on_success: Callable[[], None]) -> None:
        self._log(f"Laster ned ansiktsmodell {self.face_model_status.model_name} før ansiktsscan ...")
        self._run_waiting_command(
            download_face_model_command(),
            running_message="Laster ned ansiktsmodell ...",
            success_message="Ansiktsmodell lastet ned.",
            failure_message="Nedlasting av ansiktsmodell feilet.",
            on_success=on_success,
        )

    def _run_face_scan_enable_step(self, on_success: Callable[[], None]) -> None:
        try:
            set_face_recognition_enabled(program_repo_root(), True)
        except (OSError, ValueError) as exc:
            self._show_error("Kunne ikke slå på ansiktsgjenkjenning.", exc)
            return
        self._log("Ansiktsgjenkjenning er slått på.")
        on_success()

    def _run_image_scan(self) -> None:
        from tkinter import messagebox

        openclip_missing = self.openclip_status != "Installert"
        model_missing = self.openclip_model_status.status != "Tilgjengelig"
        image_search_disabled = not self._image_search_enabled()
        if not openclip_missing and not model_missing and not image_search_disabled:
            self._start_image_scan_command()
            return

        if (openclip_missing or model_missing) and not openclip_install_supported():
            messagebox.showerror(
                "Bildesøk mangler",
                "Bildesøk kan ikke klargjøres automatisk her. "
                "Installer OpenCLIP og AI-modellen fra Oppsett-fanen på Windows.",
                parent=self.root,
            )
            self._log("Bildesøk-scan avbrutt: OpenCLIP-oppsett kan ikke kjøres automatisk her.")
            return

        question = IMAGE_SCAN_SETUP_DOWNLOAD_MESSAGE if openclip_missing or model_missing else IMAGE_SCAN_ENABLE_MESSAGE
        if not messagebox.askyesno("Klargjør bildesøk?", question, parent=self.root):
            self._log("Bildesøk-scan avbrutt.")
            return

        steps: list[Callable[[Callable[[], None]], None]] = []
        if openclip_missing or model_missing:
            steps.append(self._run_image_scan_openclip_install_step)
        if image_search_disabled:
            steps.append(self._run_image_scan_enable_step)
        self._run_image_scan_setup_steps(steps)

    def _start_image_scan_command(self) -> None:
        self._log("Scanner bilder for bildesøk ...")
        self._run_waiting_command(
            image_scan_command(self.collection_path),
            running_message="Scanner bilder for bildesøk ...",
            success_message="Bildesøk-scan fullført.",
            failure_message="Bildesøk-scan feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _image_search_enabled(self) -> bool:
        try:
            return bool(load_config(program_repo_root()).openclip.enabled)
        except (OSError, ValueError) as exc:
            self._log(f"Kunne ikke lese innstilling for bildesøk: {exc}")
            return False

    def _run_image_scan_setup_steps(self, steps: list[Callable[[Callable[[], None]], None]]) -> None:
        if not steps:
            self._start_image_scan_command()
            return
        step = steps[0]
        remaining = steps[1:]
        step(lambda: self._run_image_scan_setup_steps(remaining))

    def _run_image_scan_openclip_install_step(self, on_success: Callable[[], None]) -> None:
        self._log("Installerer OpenCLIP før bildesøk-scan ...")
        self._run_waiting_command(
            openclip_install_command(),
            running_message="Installerer OpenCLIP ...",
            success_message="OpenCLIP-installasjon fullført.",
            failure_message="OpenCLIP-installasjon feilet.",
            on_success=lambda: self._image_scan_openclip_install_finished(on_success),
        )

    def _image_scan_openclip_install_finished(self, on_success: Callable[[], None]) -> None:
        importlib.invalidate_caches()
        self._refresh_openclip_status_after_install()
        on_success()

    def _refresh_openclip_status_after_install(self) -> None:
        try:
            self.openclip_status = openclip_dependency_status()
        except Exception as exc:  # noqa: BLE001 - setup status must not block launcher flow
            self.openclip_status = f"Feil: {exc}"
        try:
            self.openclip_model_status = openclip_model_status()
        except Exception as exc:  # noqa: BLE001 - setup status must not block launcher flow
            self.openclip_model_status = OpenClipModelStatus("", "", "Feil", str(exc))
        self._log_dependency_status_detail("OpenCLIP", self.openclip_status, "")
        self._log_dependency_status_detail(
            "OpenCLIP-modell",
            self.openclip_model_status.status,
            self.openclip_model_status.detail,
        )
        self._apply_dependency_status_values()
        self._set_buttons_enabled(not self.busy)

    def _run_image_scan_enable_step(self, on_success: Callable[[], None]) -> None:
        try:
            set_image_search_enabled(program_repo_root(), True)
        except (OSError, ValueError) as exc:
            self._show_error("Kunne ikke slå på bildesøk.", exc)
            return
        self._log("Bildesøk er slått på.")
        on_success()

    def _run_make_thumbnails(self) -> None:
        self._log("Lager thumbnails ...")
        self._run_waiting_command(
            make_thumbnails_command(self.collection_path),
            running_message="Lager thumbnails ...",
            success_message="Thumbnails fullført.",
            failure_message="Thumbnail-jobb feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _run_make_browser(self) -> None:
        hide_out_of_focus = bool(self.static_browser_hide_out_of_focus_var.get())
        self._log("Lager statisk HTML-browser ...")
        self._run_waiting_command(
            make_browser_command(self.collection_path, hide_out_of_focus=hide_out_of_focus),
            running_message="Lager statisk HTML-browser ...",
            success_message="Statisk HTML-browser fullført.",
            failure_message="Statisk HTML-browser feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _start_make_person_browser_flow(self) -> None:
        from tkinter import messagebox

        persons = self._load_registered_persons()
        if persons is None:
            return
        if not persons:
            messagebox.showinfo("Ingen personer", "Fant ingen registrerte personer.")
            self._log("Personbrowser avbrutt: fant ingen registrerte personer.")
            return

        self._select_person(
            persons,
            title="Lag personbrowser",
            description="Velg personen det skal lages statisk HTML-browser for.",
            action_label="Lag HTML-browser",
            on_cancel=lambda: self._log("Personbrowser avbrutt: ingen person valgt."),
            on_select=self._run_make_person_browser,
        )

    def _run_make_person_browser(self, person: RegisteredPerson) -> None:
        hide_out_of_focus = bool(self.static_browser_hide_out_of_focus_var.get())
        self._log(f'Lager statisk HTML-browser for "{person.name}" ...')
        self._run_waiting_command(
            make_person_browser_command(
                self.collection_path,
                person.name,
                hide_out_of_focus=hide_out_of_focus,
            ),
            running_message="Lager statisk personbrowser ...",
            success_message="Statisk personbrowser fullført.",
            failure_message="Statisk personbrowser feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _run_make_people_browser(self) -> None:
        hide_out_of_focus = bool(self.static_browser_hide_out_of_focus_var.get())
        self._log("Lager statiske personbrowsere ...")
        self._run_waiting_command(
            make_people_browser_command(self.collection_path, hide_out_of_focus=hide_out_of_focus),
            running_message="Lager statiske personbrowsere ...",
            success_message="Statiske personbrowsere fullført.",
            failure_message="Statiske personbrowsere feilet.",
            on_success=self._refresh_state,
            cancellable=True,
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

    def _run_doctor(self) -> None:
        self._log("Kjører doctor ...")
        self._run_waiting_command(
            doctor_command(self.collection_path),
            running_message="Kjører doctor ...",
            success_message="Doctor fullført.",
            failure_message="Doctor feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _run_deep_doctor(self) -> None:
        self._log("Kjører grundig doctor ...")
        self._run_waiting_command(
            deep_doctor_command(self.collection_path),
            running_message="Kjører grundig doctor ...",
            success_message="Grundig doctor fullført.",
            failure_message="Grundig doctor feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _run_vacuum(self) -> None:
        self._log("Pakker Bildebank-databaser ...")
        self._run_waiting_command(
            vacuum_command(self.collection_path),
            running_message="Pakker Bildebank-databaser ...",
            success_message="Vacuum fullført.",
            failure_message="Vacuum feilet.",
            on_success=self._refresh_state,
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

    def _show_pending_deletes(self) -> None:
        self._log("Kontrollerer ventende filsletting ...")
        self._run_waiting_command(
            cleanup_pending_deletes_list_command(self.collection_path),
            running_message="Kontrollerer ventende filsletting ...",
            success_message="Kontroll av ventende filsletting fullført. Se listen i loggen.",
            failure_message="Kontroll av ventende filsletting feilet.",
            on_success=self._pending_deletes_list_finished,
        )

    def _pending_deletes_list_finished(self) -> None:
        from tkinter import messagebox

        self._refresh_pending_deletes_status()
        self._refresh_state()
        if not self.pending_deletes_count:
            messagebox.showinfo(
                "Ventende filsletting",
                "Ingen ventende filslettinger.",
                parent=self.root,
            )
            return
        self._show_log_review_question(
            "Ventende filsletting",
            (
                "Listen over ventende filslettinger står i loggen.\n\n"
                "Vil du prøve å rydde opp nå?"
            ),
            yes_text="Rydd opp",
            no_text="Avbryt",
            on_yes=self._confirm_cleanup_pending_deletes,
            on_no=lambda: self._log("Opprydding av ventende filsletting avbrutt."),
        )

    def _confirm_cleanup_pending_deletes(self) -> None:
        confirmation = self._ask_string(
            "Bekreft ventende filsletting",
            'Skriv "ja, rydd opp" for å gjennomføre opprydding.',
        )
        if confirmation != "ja, rydd opp":
            self._log("Opprydding av ventende filsletting avbrutt.")
            return
        self._run_cleanup_pending_deletes()

    def _run_cleanup_pending_deletes(self) -> None:
        self._log("Rydder opp ventende filsletting ...")
        self._run_waiting_command(
            cleanup_pending_deletes_apply_command(self.collection_path),
            running_message="Rydder opp ventende filsletting ...",
            success_message="Opprydding av ventende filsletting fullført.",
            failure_message="Opprydding av ventende filsletting feilet.",
            on_success=self._refresh_state,
        )

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

    def _install_insightface(self) -> None:
        if not insightface_install_supported():
            self._log("Kan ikke installere InsightFace her: install-insightface.ps1 er Windows-installasjonsflyt.")
            return
        if self.insightface_status.status == "Klar" and not self._confirm_rerun(
            "Installer InsightFace på nytt?",
            "InsightFace-avhengighetene er allerede klare. Vil du kjøre installasjonen på nytt?",
        ):
            self._log("InsightFace-installasjon avbrutt.")
            return
        self._log("Installerer InsightFace ...")
        self._run_waiting_command(
            insightface_install_command(),
            running_message="Installerer InsightFace ...",
            success_message="InsightFace-installasjon fullført.",
            failure_message="InsightFace-installasjon feilet.",
            on_success=self._insightface_install_finished,
        )

    def _insightface_install_finished(self) -> None:
        importlib.invalidate_caches()
        self._start_dependency_status_refresh()

    def _install_openclip(self) -> None:
        if not openclip_install_supported():
            self._log("Kan ikke installere OpenCLIP her: install-openclip.ps1 er Windows-installasjonsflyt.")
            return
        if (
            self.openclip_status == "Installert"
            or self.openclip_model_status.status == "Tilgjengelig"
        ) and not self._confirm_rerun(
            "Installer OpenCLIP på nytt?",
            "OpenCLIP ser allerede ut til å være installert eller ha lokal AI-modell. Vil du kjøre installasjonen på nytt?",
        ):
            self._log("OpenCLIP-installasjon avbrutt.")
            return
        self._log("Installerer OpenCLIP ...")
        self._run_waiting_command(
            openclip_install_command(),
            running_message="Installerer OpenCLIP ...",
            success_message="OpenCLIP-installasjon fullført.",
            failure_message="OpenCLIP-installasjon feilet.",
            on_success=self._openclip_install_finished,
        )

    def _openclip_install_finished(self) -> None:
        importlib.invalidate_caches()
        self._refresh_openclip_status_after_install()
        self._start_dependency_status_refresh()

    def _download_face_model(self) -> None:
        if self.insightface_status.status != "Klar":
            self._log("Kan ikke laste ned ansiktsmodell før InsightFace-avhengighetene er klare.")
            return
        if self.face_model_status.status == "Lastet ned" and not self._confirm_rerun(
            "Last ned ansiktsmodell på nytt?",
            (
                f"Ansiktsmodellen {self.face_model_status.model_name} er allerede lastet ned. "
                "Vil du kjøre modellnedlastingen på nytt?"
            ),
        ):
            self._log("Nedlasting av ansiktsmodell avbrutt.")
            return
        self._log(f"Laster ned ansiktsmodell {self.face_model_status.model_name} ...")
        self._run_waiting_command(
            download_face_model_command(),
            running_message="Laster ned ansiktsmodell ...",
            success_message="Ansiktsmodell lastet ned.",
            failure_message="Nedlasting av ansiktsmodell feilet.",
            on_success=self._start_dependency_status_refresh,
        )

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
        dialog = self.tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)

        self._set_busy(True, "Venter på bekreftelse ...")

        frame = self.ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        self.ttk.Label(frame, text=title, font=("", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        self.ttk.Label(frame, text=message, wraplength=460).grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(10, 16),
        )

        finished = False

        def finish(answer: bool) -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            try:
                dialog.destroy()
            except self.tk.TclError:
                pass
            self._set_busy(False)
            if answer:
                on_yes()
            else:
                on_no()

        button_frame = self.ttk.Frame(frame)
        button_frame.grid(row=2, column=0, columnspan=2, sticky="e")
        self._button(button_frame, text=no_text, command=lambda: finish(False)).grid(row=0, column=0, padx=(0, 8))
        self._button(button_frame, text=yes_text, command=lambda: finish(True)).grid(row=0, column=1)

        dialog.bind("<Escape>", lambda _event: finish(False))
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - dialog.winfo_width()) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - dialog.winfo_height()) // 2, 0)
        dialog.geometry(f"+{x}+{y}")

    def _start_rescan_source_flow(self) -> None:
        from tkinter import messagebox

        sources = self._load_registered_sources()
        if sources is None:
            return
        candidates = rescan_source_candidates(sources)
        if not candidates:
            messagebox.showinfo("Ingen kilder", "Fant ingen aktive kilder som kan rescannes.")
            self._log("Rescan avbrutt: fant ingen aktive kilder.")
            return
        self._select_source(
            candidates,
            title="Velg kilde for rescan",
            action_label="Rescan",
            on_cancel=lambda: self._log("Rescan avbrutt: ingen kilde valgt."),
            on_select=self._run_rescan_source,
        )

    def _run_rescan_source(self, source: db.Source) -> None:
        self._log(f'Rescanner kilde "{source.name}" fra {source.path} ...')
        self._run_waiting_command(
            rescan_source_command(self.collection_path, source.name),
            running_message="Scanner kilde på nytt ...",
            success_message="Rescan fullført.",
            failure_message="Rescan feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _start_check_source_flow(self) -> None:
        from tkinter import messagebox

        sources = self._load_registered_sources()
        if sources is None:
            return
        if not sources:
            messagebox.showinfo("Ingen kilder", "Fant ingen registrerte kilder.")
            self._log("Sjekk kilde avbrutt: fant ingen registrerte kilder.")
            return
        self._select_source(
            sources,
            title="Velg kilde som skal sjekkes",
            action_label="Sjekk kilde",
            on_cancel=lambda: self._log("Sjekk kilde avbrutt: ingen kilde valgt."),
            on_select=self._run_check_source,
        )

    def _run_check_source(self, source: db.Source) -> None:
        self._log(f'Sjekker kilde "{source.name}" fra {source.path} ...')
        self._run_waiting_command(
            check_source_command(self.collection_path, source.path),
            running_message="Sjekker kilde ...",
            success_message="Kildesjekk fullført.",
            failure_message="Kildesjekk feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _start_unimport_source_flow(self) -> None:
        from tkinter import messagebox

        sources = self._load_registered_sources()
        if sources is None:
            return
        candidates = rescan_source_candidates(sources)
        if not candidates:
            messagebox.showinfo("Ingen kilder", "Fant ingen aktive kilder som kan unimporteres.")
            self._log("Unimport avbrutt: fant ingen aktive kilder.")
            return
        self._select_source(
            candidates,
            title="Velg kilde for unimport",
            action_label="Unimport",
            on_cancel=lambda: self._log("Unimport avbrutt: ingen kilde valgt."),
            on_select=self._run_unimport_source_dry_run,
        )

    def _run_unimport_source_dry_run(self, source: db.Source) -> None:
        report_file = tempfile.NamedTemporaryFile(
            prefix="bildebank-unimport-",
            suffix=".json",
            delete=False,
        )
        report_path = Path(report_file.name)
        report_file.close()
        self._log(f'Kontrollerer unimport for kilde "{source.name}" fra {source.path} ...')
        self._run_waiting_command(
            unimport_source_dry_run_command(
                self.collection_path,
                source.name,
                target_change_report_json=report_path,
            ),
            running_message="Kontrollerer unimport ...",
            success_message="Unimport dry-run fullført. Se planen i loggen.",
            failure_message="Unimport dry-run feilet.",
            on_success=lambda: self._confirm_unimport_source(source, report_path),
        )

    def _confirm_unimport_source(self, source: db.Source, report_path: Path) -> None:
        from tkinter import messagebox

        try:
            changed_targets = read_unimport_target_change_report(report_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._log(f'Unimport avbrutt for kilde "{source.name}": kunne ikke lese dry-run-rapport: {exc}')
            messagebox.showerror("Unimport", "Kunne ikke lese dry-run-rapporten for unimport.")
            return
        finally:
            try:
                report_path.unlink(missing_ok=True)
            except OSError:
                pass

        messagebox.showwarning(
            "Unimport",
            (
                "Dry-run er fullført og planen står i loggen.\n\n"
                "Unimport kan fjerne filer fra den aktive bildesamlingen."
            ),
        )
        confirmation = self._ask_string(
            "Bekreft unimport",
            f'Skriv "ja, det vil jeg" for å unimporte kilden:\n{source.name}',
        )
        if confirmation != "ja, det vil jeg":
            self._log(f'Unimport avbrutt for kilde "{source.name}".')
            return
        target_change_answer = "nei"
        if changed_targets:
            preview = "\n".join(f"  {path}" for path in changed_targets[:10])
            if len(changed_targets) > 10:
                preview += f"\n  ... og {len(changed_targets) - 10} til"
            if not messagebox.askyesno(
                "Endrede filer",
                (
                    "Noen fil(er) i bildesamlingen er endret siden import.\n\n"
                    "Filene i kilden er verifisert, men disse filene matcher ikke "
                    "lenger databaseført størrelse/SHA-256 og kan inneholde "
                    f"manuelle endringer:\n\n{preview}\n\n"
                    "Fortsette unimport og la disse filene slettes?"
                ),
                parent=self.root,
            ):
                self._log(f'Unimport avbrutt for kilde "{source.name}": endrede filer.')
                return
            target_change_answer = "ja"
        self._run_unimport_source(source, target_change_answer=target_change_answer)

    def _run_unimport_source(self, source: db.Source, *, target_change_answer: str = "nei") -> None:
        self._log(f'Unimporterer kilde "{source.name}" fra {source.path} ...')
        self._run_waiting_command(
            unimport_source_command(self.collection_path, source.name),
            running_message="Kjører unimport ...",
            success_message="Unimport-kommando avsluttet. Se loggen for resultat.",
            failure_message="Unimport feilet.",
            stdin_text=f"ja, det vil jeg\n{target_change_answer}\n",
            on_success=self._refresh_state,
        )

    def _load_registered_sources(self) -> list[db.Source] | None:
        from tkinter import messagebox

        try:
            return registered_sources(self.collection_path)
        except Exception as exc:  # noqa: BLE001 - GUI should show readable errors
            messagebox.showerror("Kunne ikke lese kilder", "Kunne ikke lese registrerte kilder.")
            self._log(f"Kunne ikke lese registrerte kilder: {exc}")
            return None

    def _load_registered_persons(self) -> list[RegisteredPerson] | None:
        from tkinter import messagebox

        try:
            return registered_persons(self.collection_path)
        except Exception as exc:  # noqa: BLE001 - GUI should show readable errors
            messagebox.showerror("Kunne ikke lese personer", "Kunne ikke lese registrerte personer.")
            self._log(f"Kunne ikke lese registrerte personer: {exc}")
            return None

    def _select_source(
        self,
        sources: list[db.Source],
        *,
        title: str,
        action_label: str,
        on_select: Callable[[db.Source], None],
        on_cancel: Callable[[], None],
    ) -> None:
        tk = self.tk
        ttk = self.ttk

        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.minsize(760, 320)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)

        frame = ttk.Frame(dialog, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        columns = ("id", "status", "name", "path")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", height=10)
        tree.heading("id", text="ID")
        tree.heading("status", text="Status")
        tree.heading("name", text="Navn")
        tree.heading("path", text="Mappe")
        tree.column("id", width=55, stretch=False, anchor="e")
        tree.column("status", width=105, stretch=False)
        tree.column("name", width=180, stretch=True)
        tree.column("path", width=380, stretch=True)

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        source_by_item: dict[str, db.Source] = {}
        for source in sources:
            item_id = tree.insert(
                "",
                "end",
                values=(source.id, source.status, source.name, str(source.path)),
            )
            source_by_item[item_id] = source
        first_item = tree.get_children()
        if first_item:
            tree.selection_set(first_item[0])
            tree.focus(first_item[0])

        def accept() -> None:
            selected = tree.selection()
            if not selected:
                return
            source = source_by_item[selected[0]]
            dialog.withdraw()
            dialog.destroy()
            self.root.lift()
            self.root.focus_force()
            self.root.after_idle(lambda: on_select(source))

        def cancel() -> None:
            dialog.withdraw()
            dialog.destroy()
            self.root.lift()
            self.root.focus_force()
            self.root.after_idle(on_cancel)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self._button(button_frame, text="Avbryt", command=cancel).grid(row=0, column=0, padx=(0, 8))
        self._button(button_frame, text=action_label, command=accept).grid(row=0, column=1)

        tree.bind("<Double-1>", lambda _event: accept())
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.protocol("WM_DELETE_WINDOW", cancel)

    def _start_export_person_flow(self) -> None:
        from tkinter import filedialog, messagebox

        persons = self._load_registered_persons()
        if persons is None:
            return
        if not persons:
            messagebox.showinfo("Ingen personer", "Fant ingen registrerte personer.")
            self._log("Personeksport avbrutt: fant ingen registrerte personer.")
            return

        self._select_person(
            persons,
            title="Eksporter person",
            description=(
                "Denne funksjonen eksporterer en kopi av alle bildene av en person. "
                "Velg personen du vil eksportere, og deretter mappen der personmappen skal opprettes."
            ),
            action_label="Velg mappe",
            on_cancel=lambda: self._log("Personeksport avbrutt: ingen person valgt."),
            on_select=lambda person: self._choose_export_person_destination(person, filedialog=filedialog),
        )

    def _choose_export_person_destination(self, person: RegisteredPerson, *, filedialog: Any) -> None:
        selected = filedialog.askdirectory(
            title=f"Velg hvor personmappen for {person.name} skal opprettes",
            initialdir=str(self.collection_path.parent),
        )
        if not selected:
            self._log(f'Personeksport avbrutt for "{person.name}": ingen mappe valgt.')
            return
        self._run_export_person_dry_run(person, Path(selected))

    def _select_person(
        self,
        persons: list[RegisteredPerson],
        *,
        title: str,
        description: str,
        action_label: str,
        on_select: Callable[[RegisteredPerson], None],
        on_cancel: Callable[[], None],
    ) -> None:
        tk = self.tk
        ttk = self.ttk

        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)

        ttk.Label(
            frame,
            text=description,
            wraplength=460,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(frame, text="Person:").grid(row=1, column=0, sticky="w", pady=(0, 4))

        person_names = [person.name for person in persons]
        selected_name = tk.StringVar(value=person_names[0])
        combobox = ttk.Combobox(
            frame,
            textvariable=selected_name,
            values=person_names,
            state="readonly",
            width=42,
        )
        combobox.grid(row=2, column=0, columnspan=2, sticky="ew")
        combobox.focus_set()

        person_by_name = {person.name: person for person in persons}

        def accept() -> None:
            person = person_by_name.get(selected_name.get())
            if person is None:
                return
            dialog.withdraw()
            dialog.destroy()
            self.root.lift()
            self.root.focus_force()
            self.root.after_idle(lambda: on_select(person))

        def cancel() -> None:
            dialog.withdraw()
            dialog.destroy()
            self.root.lift()
            self.root.focus_force()
            self.root.after_idle(on_cancel)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self._button(button_frame, text="Avbryt", command=cancel).grid(row=0, column=0, padx=(0, 8))
        self._button(button_frame, text=action_label, command=accept).grid(row=0, column=1)

        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.protocol("WM_DELETE_WINDOW", cancel)

    def _run_export_person_dry_run(self, person: RegisteredPerson, destination_root: Path) -> None:
        self._log(f'Kontrollerer personeksport for "{person.name}" til {destination_root} ...')
        self._run_waiting_command(
            export_person_command(self.collection_path, person.name, destination_root, dry_run=True),
            running_message="Kontrollerer personeksport ...",
            success_message="Eksport dry-run fullført. Se planen i loggen.",
            failure_message="Eksport dry-run feilet.",
            on_success=lambda: self._confirm_export_person(person, destination_root),
        )

    def _confirm_export_person(self, person: RegisteredPerson, destination_root: Path) -> None:
        self._show_log_review_question(
            "Eksporter person?",
            (
                "Dry-run er fullført og planen står i loggen.\n\n"
                f'Vil du eksportere bildene av "{person.name}" nå?\n\n'
                f"Personmappen opprettes under:\n{destination_root}"
            ),
            yes_text="Eksporter",
            no_text="Avbryt",
            on_yes=lambda: self._run_export_person(person, destination_root),
            on_no=lambda: self._log(f'Personeksport avbrutt for "{person.name}".'),
        )

    def _run_export_person(self, person: RegisteredPerson, destination_root: Path) -> None:
        self._log(f'Eksporterer bilder av "{person.name}" til {destination_root} ...')
        self._run_waiting_command(
            export_person_command(self.collection_path, person.name, destination_root),
            running_message="Eksporterer person ...",
            success_message="Personeksport fullført.",
            failure_message="Personeksport feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _start_import_flow(self) -> None:
        from tkinter import filedialog, messagebox

        selected = filedialog.askdirectory(title="Velg mappen som skal importeres")
        if not selected:
            self._log("Import avbrutt: ingen mappe valgt.")
            return

        source_folder = Path(selected)
        if source_is_collection_or_inside(source_folder, self.collection_path):
            message = "Du kan ikke importere selve bildesamlingen eller en mappe inni den."
            messagebox.showerror("Kan ikke importere", message)
            self._log(f"Import avvist: {source_folder} ligger i bildesamlingen {self.collection_path}")
            return

        proposed_name = suggest_import_name(source_folder)
        while True:
            import_name = self._ask_string(
                "Importnavn",
                "Navn på importen:",
                initialvalue=proposed_name,
            )
            if import_name is None:
                self._log("Import avbrutt: importnavn ikke valgt.")
                return
            import_name = import_name.strip()
            if import_name:
                break
            messagebox.showerror("Importnavn mangler", "Importnavn kan ikke være tomt.")

        self._log(f'Importerer bilder fra {source_folder} med navn "{import_name}" ...')
        self._run_waiting_command(
            import_command(self.collection_path, source_folder, import_name),
            running_message="Importerer bilder ...",
            success_message="Import fullført.",
            failure_message="Import feilet.",
            on_success=self._refresh_state,
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
        self.active_command_process = None
        self.active_command_cancel_requested = False
        self.active_command_cancellable = cancellable
        self._set_busy(True, running_message)
        self._clear_active_progress_log()
        self._log("$ " + " ".join(command))

        def worker() -> None:
            try:
                process: subprocess.Popen[str] = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE if stdin_text is not None else None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding=subprocess_output_encoding(),
                    errors="replace",
                    bufsize=1,
                    creationflags=interruptible_command_creationflags() if cancellable else 0,
                )
            except OSError as exc:
                def report_start_failed(exc: OSError = exc) -> None:
                    self._command_start_failed(failure_message, exc)

                self._post_to_tk(report_start_failed)
                return
            self.active_command_process = process
            if self.active_command_cancel_requested:
                try:
                    interrupt_process(process)
                except OSError:
                    pass

            if stdin_text is not None:
                assert process.stdin is not None
                process.stdin.write(stdin_text)
                process.stdin.flush()
                process.stdin.close()

            assert process.stdout is not None
            for line in process.stdout:
                message = line.rstrip()

                def log_message(message: str = message) -> None:
                    self._log_process_output(message)

                self._post_to_tk(log_message)
            return_code = process.wait()
            cancel_requested = self.active_command_cancel_requested
            def report_finished() -> None:
                self._command_finished(
                    return_code,
                    success_message=success_message,
                    failure_message=failure_message,
                    on_success=on_success,
                    cancel_requested=cancel_requested,
                )

            self._post_to_tk(report_finished)

        threading.Thread(target=worker, daemon=True).start()

    def _command_start_failed(self, failure_message: str, exc: OSError) -> None:
        from tkinter import messagebox

        self.active_command_process = None
        self.active_command_cancel_requested = False
        self.active_command_cancellable = False
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

        self.active_command_process = None
        self.active_command_cancel_requested = False
        self.active_command_cancellable = False
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
