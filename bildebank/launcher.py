from __future__ import annotations

import json
import locale
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable

from . import db


CONFIG_DIR_NAME = "Bildebank"
CONFIG_FILENAME = "launcher.json"


@dataclass(frozen=True)
class LauncherConfig:
    collection_path: Path


def default_collection_path() -> Path:
    return Path.home() / "kode" / "bilde-samling"


def default_config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / CONFIG_DIR_NAME / CONFIG_FILENAME
    return Path.home() / ".bildebank" / CONFIG_FILENAME


def load_launcher_config(config_path: Path | None = None) -> LauncherConfig:
    path = config_path or default_config_path()
    if not path.exists():
        return LauncherConfig(collection_path=default_collection_path())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return LauncherConfig(collection_path=default_collection_path())

    collection_path = data.get("collection_path")
    if not isinstance(collection_path, str) or not collection_path.strip():
        return LauncherConfig(collection_path=default_collection_path())
    return LauncherConfig(collection_path=Path(collection_path))


def save_launcher_config(config: LauncherConfig, config_path: Path | None = None) -> None:
    path = config_path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"collection_path": str(config.collection_path)}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def suggest_import_name(source_folder: Path) -> str:
    raw_path = str(source_folder)
    if "\\" in raw_path:
        name = PureWindowsPath(raw_path).name.strip()
    else:
        name = source_folder.name.strip()
    if name:
        return name
    return str(source_folder).strip()


def is_collection_created(collection_path: Path) -> bool:
    return db.db_path_for_target(collection_path).exists()


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


def bildebank_command(*args: str | Path) -> list[str]:
    return [sys.executable, "-m", "bildebank", *(str(arg) for arg in args)]


def create_command(collection_path: Path) -> list[str]:
    return bildebank_command("create", collection_path)


def import_command(collection_path: Path, source_folder: Path, import_name: str) -> list[str]:
    return bildebank_command("--target", collection_path, "import", "--name", import_name, source_folder)


def run_server_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "run-server")


def geo_scan_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "geo-scan")


def make_thumbnails_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "make-thumbnails")


def check_source_command(collection_path: Path, source_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "check-source", source_path)


def rescan_source_command(collection_path: Path, source_name: str) -> list[str]:
    return bildebank_command("--target", collection_path, "rescan-source", "--name", source_name)


def unimport_source_command(collection_path: Path, source_name: str) -> list[str]:
    return bildebank_command("--target", collection_path, "unimport", "--name", source_name)


def unimport_source_dry_run_command(collection_path: Path, source_name: str) -> list[str]:
    return bildebank_command("--target", collection_path, "unimport", "--dry-run", "--name", source_name)


def registered_sources(collection_path: Path) -> list[db.Source]:
    conn = db.connect(collection_path)
    try:
        return db.get_sources(conn)
    finally:
        conn.close()


def rescan_source_candidates(sources: list[db.Source]) -> list[db.Source]:
    return [
        source
        for source in sources
        if source.superseded_by_source_id is None and source.status != "superseded"
    ]


def subprocess_output_encoding() -> str:
    return locale.getpreferredencoding(False) or "utf-8"


class BildebankLauncher:
    def __init__(self, config_path: Path | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.config_path = config_path
        self.config = load_launcher_config(config_path)
        self.collection_path = self.config.collection_path
        self.busy = False
        self.server_process: subprocess.Popen[str] | None = None

        self.root = tk.Tk()
        self.root.title("Bildebank kontrollpanel")
        self.root.minsize(640, 460)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.collection_value: tk.StringVar = tk.StringVar(value=str(self.collection_path))
        self.status_value: tk.StringVar = tk.StringVar(value="")
        self.button_frame: ttk.Frame | None = None
        self.log_text: tk.Text | None = None
        self.buttons: list[ttk.Button] = []

        self._build_gui()
        self._refresh_state()
        self._log(f"Valgt bildesamling: {self.collection_path}")

    def run(self) -> None:
        self.root.mainloop()

    def _on_close(self) -> None:
        self._stop_server_process()
        self.root.destroy()

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

        outer = ttk.Frame(self.root, padding=16)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        title = ttk.Label(outer, text="Bildebank kontrollpanel", font=("", 15, "bold"))
        title.grid(row=0, column=0, sticky="w")

        ttk.Label(outer, text="Bildesamling:").grid(row=1, column=0, sticky="w", pady=(18, 2))
        collection_label = ttk.Label(outer, textvariable=self.collection_value, wraplength=580)
        collection_label.grid(row=2, column=0, sticky="we")

        self.button_frame = ttk.Frame(outer)
        self.button_frame.grid(row=3, column=0, sticky="w", pady=(14, 18))

        log_frame = ttk.Frame(outer)
        log_frame.grid(row=4, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        ttk.Label(log_frame, text="Logg:").grid(row=0, column=0, sticky="w")

        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")

        status = ttk.Label(outer, textvariable=self.status_value)
        status.grid(row=5, column=0, sticky="w", pady=(10, 0))

    def _refresh_state(self) -> None:
        ttk = self.ttk
        assert self.button_frame is not None

        for child in self.button_frame.winfo_children():
            child.destroy()
        self.buttons = []

        choose = ttk.Button(
            self.button_frame,
            text="Velg annen plassering",
            command=self._choose_collection,
        )
        choose.grid(row=0, column=0, padx=(0, 8), pady=4)
        self.buttons.append(choose)

        if is_collection_created(self.collection_path):
            import_button = ttk.Button(self.button_frame, text="Importer bilder", command=self._start_import_flow)
            import_button.grid(row=0, column=1, padx=(0, 8), pady=4)
            rescan_button = ttk.Button(self.button_frame, text="Rescan kilde", command=self._start_rescan_source_flow)
            rescan_button.grid(row=0, column=2, padx=(0, 8), pady=4)
            check_button = ttk.Button(self.button_frame, text="Sjekk kilde", command=self._start_check_source_flow)
            check_button.grid(row=0, column=3, padx=(0, 8), pady=4)
            unimport_button = ttk.Button(self.button_frame, text="Unimport", command=self._start_unimport_source_flow)
            unimport_button.grid(row=0, column=4, padx=(0, 8), pady=4)
            geo_button = ttk.Button(self.button_frame, text="Scan GPS", command=self._run_geo_scan)
            geo_button.grid(row=1, column=1, padx=(0, 8), pady=4)
            thumbs_button = ttk.Button(
                self.button_frame,
                text="Lag thumbnails",
                command=self._run_make_thumbnails,
            )
            thumbs_button.grid(row=1, column=2, padx=(0, 8), pady=4)
            start_button = ttk.Button(self.button_frame, text="Start Bildebank", command=self._start_server)
            start_button.grid(row=1, column=3, padx=(0, 8), pady=4)
            open_button = ttk.Button(self.button_frame, text="Åpne bildesamling", command=self._open_collection)
            open_button.grid(row=1, column=4, padx=(0, 8), pady=4)
            self.buttons.extend(
                [
                    import_button,
                    rescan_button,
                    check_button,
                    unimport_button,
                    geo_button,
                    thumbs_button,
                    start_button,
                    open_button,
                ]
            )
        else:
            create_button = ttk.Button(
                self.button_frame,
                text="Opprett bildesamling",
                command=self._create_collection,
            )
            create_button.grid(row=0, column=1, padx=(0, 8), pady=4)
            self.buttons.append(create_button)

        self._set_buttons_enabled(not self.busy)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.buttons:
            button.configure(state=state)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        self.status_value.set(message)
        self._set_buttons_enabled(not busy)

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
        self.collection_value.set(str(self.collection_path))
        self.config = LauncherConfig(collection_path=self.collection_path)
        try:
            save_launcher_config(self.config, self.config_path)
        except OSError as exc:
            self._show_error("Kunne ikke lagre valgt plassering.", exc)
        self._log(f"Valgt bildesamling: {self.collection_path}")
        self._refresh_state()

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
        )

    def _run_make_thumbnails(self) -> None:
        self._log("Lager thumbnails ...")
        self._run_waiting_command(
            make_thumbnails_command(self.collection_path),
            running_message="Lager thumbnails ...",
            success_message="Thumbnails fullført.",
            failure_message="Thumbnail-jobb feilet.",
            on_success=self._refresh_state,
        )

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
        self._log(f'Kontrollerer unimport for kilde "{source.name}" fra {source.path} ...')
        self._run_waiting_command(
            unimport_source_dry_run_command(self.collection_path, source.name),
            running_message="Kontrollerer unimport ...",
            success_message="Unimport dry-run fullført. Se planen i loggen.",
            failure_message="Unimport dry-run feilet.",
            on_success=lambda: self._confirm_unimport_source(source),
        )

    def _confirm_unimport_source(self, source: db.Source) -> None:
        from tkinter import messagebox, simpledialog

        messagebox.showwarning(
            "Unimport",
            (
                "Dry-run er fullført og planen står i loggen.\n\n"
                "Unimport kan fjerne filer fra den aktive bildesamlingen."
            ),
        )
        confirmation = simpledialog.askstring(
            "Bekreft unimport",
            f'Skriv "ja, det vil jeg" for å unimporte kilden:\n{source.name}',
            parent=self.root,
        )
        if confirmation != "ja, det vil jeg":
            self._log(f'Unimport avbrutt for kilde "{source.name}".')
            return
        self._run_unimport_source(source)

    def _run_unimport_source(self, source: db.Source) -> None:
        self._log(f'Unimporterer kilde "{source.name}" fra {source.path} ...')
        self._run_waiting_command(
            unimport_source_command(self.collection_path, source.name),
            running_message="Kjører unimport ...",
            success_message="Unimport fullført.",
            failure_message="Unimport feilet.",
            stdin_text="ja, det vil jeg\n",
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
            self.root.update_idletasks()
            self.root.update()
            self.root.after(300, lambda: on_select(source))

        def cancel() -> None:
            dialog.withdraw()
            dialog.destroy()
            self.root.lift()
            self.root.focus_force()
            self.root.update_idletasks()
            self.root.update()
            self.root.after(0, on_cancel)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(button_frame, text="Avbryt", command=cancel).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(button_frame, text=action_label, command=accept).grid(row=0, column=1)

        tree.bind("<Double-1>", lambda _event: accept())
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.protocol("WM_DELETE_WINDOW", cancel)

    def _start_import_flow(self) -> None:
        from tkinter import filedialog, messagebox, simpledialog

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
            import_name = simpledialog.askstring(
                "Importnavn",
                "Navn på importen:",
                initialvalue=proposed_name,
                parent=self.root,
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

        self._log("Starter Bildebank ...")
        try:
            self.server_process = subprocess.Popen(run_server_command(self.collection_path))
        except OSError as exc:
            messagebox.showerror("Kunne ikke starte Bildebank", "Bildebank-serveren kunne ikke startes.")
            self._log(f"Kunne ikke starte Bildebank: {exc}")
            return
        self._log("Bildebank-serveren starter. Nettleseren åpnes av Bildebank når serveren er klar.")

    def _open_collection(self) -> None:
        from tkinter import messagebox

        try:
            open_folder(self.collection_path)
        except OSError as exc:
            messagebox.showerror("Kunne ikke åpne bildesamling", "Mappen kunne ikke åpnes.")
            self._log(f"Kunne ikke åpne bildesamling: {exc}")

    def _run_waiting_command(
        self,
        command: list[str],
        *,
        running_message: str,
        success_message: str,
        failure_message: str,
        on_success: Callable[[], None] | None = None,
        stdin_text: str | None = None,
    ) -> None:
        from tkinter import messagebox

        self._set_busy(True, running_message)
        self._log("$ " + " ".join(command))

        def worker() -> None:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE if stdin_text is not None else None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding=subprocess_output_encoding(),
                    errors="replace",
                    bufsize=1,
                )
            except OSError as exc:
                self.root.after(
                    0,
                    lambda: self._command_start_failed(failure_message, exc),
                )
                return

            if stdin_text is not None:
                assert process.stdin is not None
                process.stdin.write(stdin_text)
                process.stdin.flush()
                process.stdin.close()

            assert process.stdout is not None
            for line in process.stdout:
                self.root.after(0, self._log, line.rstrip())
            return_code = process.wait()
            self.root.after(
                0,
                lambda: self._command_finished(
                    return_code,
                    success_message=success_message,
                    failure_message=failure_message,
                    on_success=on_success,
                    messagebox=messagebox,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _command_start_failed(self, failure_message: str, exc: OSError) -> None:
        from tkinter import messagebox

        self._set_busy(False)
        self._log(f"{failure_message} {exc}")
        messagebox.showerror("Feil", failure_message)

    def _command_finished(
        self,
        return_code: int,
        *,
        success_message: str,
        failure_message: str,
        on_success: Callable[[], None] | None,
        messagebox: object,
    ) -> None:
        self._set_busy(False)
        if return_code == 0:
            self._log(success_message)
            if on_success is not None:
                on_success()
            return
        self._log(f"{failure_message} Avsluttet med kode {return_code}.")
        messagebox.showerror("Feil", failure_message)

    def _show_error(self, message: str, exc: BaseException) -> None:
        from tkinter import messagebox

        messagebox.showerror("Feil", message)
        self._log(f"{message} {exc}")

    def _log(self, message: str) -> None:
        if not message:
            return
        assert self.log_text is not None
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def open_folder(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)])


def main() -> int:
    launcher = BildebankLauncher()
    launcher.run()
    return 0
