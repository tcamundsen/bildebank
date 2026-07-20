from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

from . import db
from .launcher_commands import (
    check_source_command,
    import_command,
    read_unimport_target_change_report,
    rescan_source_command,
    unimport_source_command,
    unimport_source_dry_run_command,
)
from .launcher_status import registered_sources, rescan_source_candidates
from .launcher_widgets import select_source_dialog


class ButtonFactory(Protocol):
    def __call__(self, parent: Any, **kwargs: Any) -> Any: ...


class WaitingCommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        running_message: str,
        success_message: str,
        failure_message: str,
        on_success: Callable[[], None] | None = None,
        stdin_text: str | None = None,
        cancellable: bool = False,
    ) -> None: ...


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


class ImportTab:
    def __init__(
        self,
        *,
        tk: Any,
        ttk: Any,
        notebook: Any,
        root: Any,
        button: ButtonFactory,
        run_waiting_command: WaitingCommandRunner,
        get_collection_path: Callable[[], Path],
        log: Callable[[str], None],
        refresh_launcher: Callable[[], None],
        add_tooltip: Callable[[Any, str], None],
        ask_string: Callable[..., str | None],
        padding: int,
        padx: int,
        pady: int,
    ) -> None:
        self.tk = tk
        self.ttk = ttk
        self.root = root
        self._button = button
        self._run_waiting_command = run_waiting_command
        self._get_collection_path = get_collection_path
        self._log = log
        self._refresh_launcher = refresh_launcher
        self._add_tooltip = add_tooltip
        self._ask_string = ask_string
        self.padx = padx
        self.pady = pady

        self.frame = ttk.Frame(notebook, padding=padding)
        self.frame.columnconfigure(0, weight=1)
        self.button_frame = ttk.Frame(self.frame)
        self.button_frame.grid(row=0, column=0, sticky="w")

    @property
    def collection_path(self) -> Path:
        return self._get_collection_path()

    def refresh(self, *, available: bool) -> list[Any]:
        for child in self.button_frame.winfo_children():
            child.destroy()
        if not available:
            return []

        import_button = self._button(
            self.button_frame,
            text="Importer bilder",
            command=self._start_import_flow,
        )
        import_button.grid(row=0, column=0, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(
            import_button,
            "Registrerer og importerer bildene fra en mappe, USB-brikke, CD eller disk.",
        )
        unimport_button = self._button(
            self.button_frame,
            text="Angre import",
            command=self._start_unimport_source_flow,
        )
        unimport_button.grid(row=0, column=1, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(
            unimport_button,
            "Reverser en tidligere import. Kontrollerer først at alle registrerte originalfiler "
            "fortsatt finnes med samme innhold. Krever nøyaktig bekreftelse før noe endres.",
        )
        rescan_button = self._button(
            self.button_frame,
            text="Rescan kilde",
            command=self._start_rescan_source_flow,
        )
        rescan_button.grid(row=0, column=2, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(
            rescan_button,
            "Scan en mappe du har importert bilder fra en gang til. Bruk dette hvis bildebank "
            "har blitt forbedret, og nå støtter flere bildefiler.",
        )
        check_button = self._button(
            self.button_frame,
            text="Sjekk kilde",
            command=self._start_check_source_flow,
        )
        check_button.grid(row=0, column=3, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(
            check_button,
            "Sjekker at filene i en kildemappe finnes i bildesamlingen med samme SHA-256. "
            "Hvis alle filene i mappen du har importert fra finnes i bildesamlingen "
            "så er det i prinsippet trygt å slette mappen du importerte bildene fra.",
        )
        return [import_button, rescan_button, check_button, unimport_button]

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
            on_success=self._refresh_launcher,
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
            on_success=self._refresh_launcher,
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
            on_success=self._refresh_launcher,
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

        if not messagebox.askokcancel(
            "Unimport",
            (
                "Dry-run er fullført og planen står i loggen.\n\n"
                "Unimport kan fjerne filer fra den aktive bildesamlingen."
            ),
            parent=self.root,
            icon="warning",
        ):
            self._log(f'Unimport avbrutt for kilde "{source.name}" etter dry-run.')
            return
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
            on_success=self._refresh_launcher,
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
        select_source_dialog(
            sources,
            tk=self.tk,
            ttk=self.ttk,
            root=self.root,
            button=self._button,
            title=title,
            action_label=action_label,
            on_select=on_select,
            on_cancel=on_cancel,
        )
