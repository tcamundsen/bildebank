from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Protocol
import webbrowser

from PIL import Image, ImageTk

from .config import load_config
from .formatting import format_bytes
from .launcher_commands import (
    backup_command,
    create_command,
    launcher_command,
    migrate_command,
    run_server_command,
    server_browser_url,
    update_command,
)
from .server_runtime import DEFAULT_PORT
from .server_slideshow import DEFAULT_SLIDESHOW_DELAY_SECONDS
from .launcher_status import (
    LauncherUpdateStatus,
    check_launcher_update_status,
    collection_needs_migration,
    is_collection_created,
    program_repo_root,
)
from .snapshot import MainDatabaseSourceError, SnapshotPlan, plan_snapshot
from .snapshot_check import (
    SnapshotCheckProgress,
    SnapshotCheckResult,
    check_snapshot_repository,
)
from .snapshot_create import (
    SnapshotCreationResult,
    create_snapshot,
    validate_existing_recovery_repository,
)
from .snapshot_progress import SnapshotCreateProgress, SnapshotCreateProgressCallback
from .launcher_widgets import Tooltip


def open_server_browser_window(port: int = DEFAULT_PORT) -> bool:
    return bool(webbrowser.open(server_browser_url(port), new=1))


@dataclass(frozen=True)
class LauncherRecoveryPlan:
    source_dir: Path
    repository_dir: Path
    database_error: str


def plan_launcher_snapshot(
    collection: Path,
    repository: Path,
) -> SnapshotPlan | LauncherRecoveryPlan:
    config = load_config(program_repo_root(), migrate_legacy=False)
    try:
        return plan_snapshot(
            collection,
            repository,
            configured_face_database_dir=config.face_recognition.database_dir,
        )
    except MainDatabaseSourceError as exc:
        validated_repository = validate_existing_recovery_repository(collection, repository)
        return LauncherRecoveryPlan(
            source_dir=collection.resolve(),
            repository_dir=validated_repository,
            database_error=str(exc),
        )


def create_launcher_snapshot(
    collection: Path,
    repository: Path,
    *,
    progress: SnapshotCreateProgressCallback | None = None,
) -> SnapshotCreationResult:
    config = load_config(program_repo_root(), migrate_legacy=False)
    if progress is None:
        return create_snapshot(
            collection,
            repository,
            face_config=config.face_recognition,
        )
    return create_snapshot(
        collection,
        repository,
        face_config=config.face_recognition,
        progress=progress,
    )


def snapshot_plan_log_lines(plan: SnapshotPlan | LauncherRecoveryPlan) -> tuple[str, ...]:
    if isinstance(plan, LauncherRecoveryPlan):
        return (
            "Plan for recovery-snapshot:",
            f"  Bildesamling: {plan.source_dir}",
            f"  Repository: {plan.repository_dir}",
            "  Hoveddatabasen kunne ikke valideres normalt.",
            f"  Databasefeil: {plan.database_error}",
            "  Repositorybindingen er kontrollert skrivefritt.",
            "  Reell kjøring vil sikre lesbare filer og rå databaser som recovery-data.",
        )
    state_text = {
        "missing": "mangler og vil bli opprettet",
        "empty": "er tomt og vil bli initialisert",
        "existing": "er et eksisterende, gyldig repository",
    }.get(plan.repository_state, plan.repository_state)
    lines = [
        "Plan for versjonert backup:",
        f"  Bildesamling: {plan.source_dir}",
        f"  Repository: {plan.repository_dir}",
        f"  Repositoryet {state_text}",
        f"  Filer i inventaret: {plan.inventory.total_files} "
        f"({format_bytes(plan.inventory.total_bytes)})",
        f"  Ekskludert: {plan.inventory.excluded_files} "
        f"({format_bytes(plan.inventory.excluded_bytes)})",
        f"  Estimerte nye objekter: {plan.storage.estimated_new_objects}",
        f"  Estimert ny datamengde: {format_bytes(plan.storage.estimated_new_bytes)}",
        f"  Ledig plass: {format_bytes(plan.storage.free_bytes)}",
        "  Estimert plass er tilstrekkelig: "
        + ("ja" if plan.storage.has_estimated_capacity else "nei"),
    ]
    lines.extend(f"  ADVARSEL: {warning}" for warning in plan.warnings)
    return tuple(lines)


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


class BackgroundTaskRunner(Protocol):
    def __call__(
        self,
        task: Callable[[Callable[[], bool]], Any],
        *,
        running_message: str,
        failure_message: str,
        on_success: Callable[[Any], None],
        cancellable: bool = False,
    ) -> None: ...


class LogReviewQuestion(Protocol):
    def __call__(
        self,
        title: str,
        message: str,
        *,
        yes_text: str,
        no_text: str,
        on_yes: Callable[[], None],
        on_no: Callable[[], None],
    ) -> None: ...


@dataclass(frozen=True)
class MainTabRefresh:
    buttons: list[Any]
    collection_created: bool
    available: bool


@dataclass(frozen=True)
class ServerLaunchOptions:
    port: int
    read_only: bool
    lan_share: bool
    slideshow: bool
    delay: int | None
    filter: str | None


def normalize_server_launch_options(
    *,
    port: int,
    read_only: bool = False,
    lan_share: bool = False,
    slideshow: bool = False,
    delay: int = DEFAULT_SLIDESHOW_DELAY_SECONDS,
    filter: str | None = None,
) -> ServerLaunchOptions:
    normalized_filter = filter.strip() if slideshow and filter else None
    return ServerLaunchOptions(
        port=port,
        read_only=read_only if not slideshow else False,
        lan_share=lan_share if not slideshow else False,
        slideshow=slideshow,
        delay=delay if slideshow else None,
        filter=normalized_filter or None,
    )


class MainTab:
    def __init__(
        self,
        *,
        tk: Any,
        ttk: Any,
        notebook: Any,
        root: Any,
        button: ButtonFactory,
        run_waiting_command: WaitingCommandRunner,
        run_background_task: BackgroundTaskRunner,
        get_collection_path: Callable[[], Path],
        set_collection_path: Callable[[Path], None],
        is_busy: Callable[[], bool],
        post_to_ui: Callable[[Callable[[], None]], bool],
        log: Callable[[str], None],
        log_progress: Callable[[str], None],
        refresh_launcher: Callable[[], None],
        set_launcher_buttons_enabled: Callable[[bool], None],
        add_tooltip: Callable[[Any, str], None],
        show_log_review_question: LogReviewQuestion,
        show_error: Callable[[str, BaseException], None],
        on_close: Callable[[], None],
        destroy_root: Callable[[], None],
        padding: int,
        padx: int,
        pady: int,
    ) -> None:
        self.tk = tk
        self.ttk = ttk
        self.root = root
        self._button = button
        self._run_waiting_command = run_waiting_command
        self._run_background_task = run_background_task
        self._get_collection_path = get_collection_path
        self._set_collection_path = set_collection_path
        self._is_busy = is_busy
        self._post_to_ui = post_to_ui
        self._log = log
        self._log_progress = log_progress
        self._refresh_launcher = refresh_launcher
        self._set_launcher_buttons_enabled = set_launcher_buttons_enabled
        self._add_tooltip = add_tooltip
        self._show_log_review_question = show_log_review_question
        self._show_error = show_error
        self._on_close = on_close
        self._destroy_root = destroy_root
        self.padx = padx
        self.pady = pady

        self.frame = ttk.Frame(notebook, padding=padding)
        self.frame.columnconfigure(0, weight=1)
        self.collection_value = tk.StringVar(value="Bildesamling: " + str(self.collection_path))
        self.server_process: subprocess.Popen[Any] | None = None
        self.server_port = DEFAULT_PORT
        self.server_launch_options: ServerLaunchOptions | None = None
        self.migration_required = False
        self.migration_status_error: str | None = None
        self.migration_dialog_shown = False
        self.update_status = LauncherUpdateStatus("checking")
        self.update_checking = False
        self.update_button_icons = self._load_update_button_icons()
        self.start_server_button: Any | None = None
        self.backup_button: Any | None = None
        self.snapshot_button: Any | None = None
        self.snapshot_check_button: Any | None = None
        self.update_button: Any | None = None

        collection_frame = ttk.Frame(self.frame)
        collection_frame.grid(row=0, column=0, sticky="w")
        collection_label = ttk.Label(collection_frame, textvariable=self.collection_value, wraplength=560)
        collection_label.grid(row=0, column=0, sticky="w", padx=padx, pady=pady, columnspan=2)
        self.choose_collection_button = self._button(
            collection_frame,
            text="Velg annen plassering",
            command=self._choose_collection,
        )
        self.choose_collection_button.grid(row=1, column=0, sticky="w", padx=padx, pady=pady)
        self.create_collection_button = self._button(
            collection_frame,
            text="Opprett bildesamling",
            command=self._create_collection,
        )
        self.create_collection_button.grid(row=1, column=1, sticky="w", padx=padx, pady=pady)
        self.create_collection_tooltip = Tooltip(
            self.create_collection_button,
            self._create_collection_tooltip(False),
        )

        separator = ttk.Separator(self.frame, orient="horizontal")
        separator.grid(row=1, column=0, sticky="ew", pady=padding)
        self.button_frame = ttk.Frame(self.frame)
        self.button_frame.grid(row=2, column=0, sticky="w")

    @property
    def collection_path(self) -> Path:
        return self._get_collection_path()

    def refresh(self) -> MainTabRefresh:
        self.create_collection_tooltip.hide()
        for child in self.button_frame.winfo_children():
            child.destroy()
        self.start_server_button = None
        self.backup_button = None
        self.snapshot_button = None
        self.snapshot_check_button = None
        self.update_button = None
        collection_created = is_collection_created(self.collection_path)
        available = (
            collection_created
            and not self.migration_required
            and self.migration_status_error is None
        )
        self.create_collection_tooltip.text = self._create_collection_tooltip(collection_created)

        if collection_created and self.migration_required:
            migrate_button = self._button(self.button_frame, text="Migrer", command=self._run_migrate)
            migrate_button.grid(row=0, column=0, padx=self.padx, pady=self.pady, sticky="ew")
            exit_button = self._button(
                self.button_frame,
                text="Avslutt uten å migrere",
                command=self._on_close,
            )
            exit_button.grid(row=0, column=1, padx=self.padx, pady=self.pady, sticky="ew")
            return MainTabRefresh([migrate_button, exit_button], collection_created, available)

        if collection_created and self.migration_status_error is not None:
            return MainTabRefresh([], collection_created, available)

        start_button = self._button(
            self.button_frame,
            text="Start Bildebank i nettleser",
            command=self._start_server,
        )
        self.start_server_button = start_button
        start_button.grid(row=0, column=0, padx=self.padx, pady=self.pady, columnspan=2, sticky="ew")
        update_button = self._button(
            self.button_frame,
            text=self._update_button_text(),
            command=self._on_update_button_clicked,
        )
        update_button.grid(row=0, column=2, padx=self.padx, pady=self.pady, sticky="ew")
        self.update_button = update_button
        self._add_tooltip(
            update_button,
            "Oppdaterer Bildebank til siste utgave. "
            "Dette tilsvarer kommandoen 'bildebank update' ",
        )
        backup_button = self._button(
            self.button_frame,
            text="Ta backup",
            command=self._start_backup_flow,
        )
        self.backup_button = backup_button
        backup_button.grid(row=0, column=3, padx=self.padx, pady=self.pady, sticky="ew")
        if collection_created:
            self._add_tooltip(
                backup_button,
                "Eldre backupfunksjon som skal fjernes. IKKE BRUK. "
                "Kjører først backup dry-run og viser planen i loggen. "
                "Faktisk backup speiler bildesamlingen og kan slette filer i backupmålet.",
            )
        else:
            self._add_tooltip(
                backup_button,
                "Eldre backupfunksjon som skal fjernes. IKKE BRUK. "
                "Backup kan brukes etter at bildesamlingen er opprettet.",
            )
        snapshot_button = self._button(
            self.button_frame,
            text="Ta versjonert backup",
            command=self._start_snapshot_flow,
        )
        self.snapshot_button = snapshot_button
        snapshot_button.grid(
            row=1,
            column=0,
            columnspan=2,
            padx=self.padx,
            pady=self.pady,
            sticky="ew",
        )
        if collection_created:
            self._add_tooltip(
                snapshot_button,
                "Lager et nytt, uforanderlig snapshot uten å slette eldre snapshots. "
                "En skrivefri plan vises før du bekrefter.",
            )
        else:
            self._add_tooltip(
                snapshot_button,
                "Versjonert backup kan brukes etter at bildesamlingen er opprettet.",
            )
        snapshot_check_button = self._button(
            self.button_frame,
            text="Kontroller versjonert backup",
            command=self._start_snapshot_check_flow,
        )
        self.snapshot_check_button = snapshot_check_button
        snapshot_check_button.grid(
            row=1,
            column=2,
            columnspan=2,
            padx=self.padx,
            pady=self.pady,
            sticky="ew",
        )
        self._add_tooltip(
            snapshot_check_button,
            "Leser og SHA-256-kontrollerer alle objekter i et eksisterende repository. "
            "Kontrollen endrer ikke snapshots eller backupobjekter.",
        )
        return MainTabRefresh(
            [
                start_button,
                update_button,
                backup_button,
                snapshot_button,
                snapshot_check_button,
            ],
            collection_created,
            available,
        )

    def set_buttons_enabled(self, enabled: bool) -> None:
        collection_created = is_collection_created(self.collection_path)
        dependency_buttons_enabled = (
            enabled
            and not self.migration_required
            and self.migration_status_error is None
        )
        collection_state = "normal" if dependency_buttons_enabled else "disabled"
        self.choose_collection_button.configure(state=collection_state)
        create_state = (
            "normal"
            if dependency_buttons_enabled and not collection_created
            else "disabled"
        )
        self.create_collection_button.configure(state=create_state)
        if self.start_server_button is not None and not collection_created:
            self.start_server_button.configure(state="disabled")
        if self.backup_button is not None and not collection_created:
            self.backup_button.configure(state="disabled")
        if self.snapshot_button is not None and not collection_created:
            self.snapshot_button.configure(state="disabled")
        if self.update_button is not None and (
            self.update_status.status == "checking" or not enabled
        ):
            self.update_button.configure(state="disabled")
        self._apply_update_button_state()

    def update_migration_status(self) -> None:
        self.migration_required = False
        self.migration_status_error = None
        if not is_collection_created(self.collection_path):
            return
        try:
            self.migration_required = collection_needs_migration(self.collection_path)
        except Exception as exc:  # noqa: BLE001 - launcher must show a controlled startup error
            self.migration_status_error = str(exc)

    def show_initial_migration_status(self) -> None:
        if self.migration_required:
            self.root.after(0, self._show_migration_required_dialog)
        elif self.migration_status_error is not None:
            self.root.after(0, self._show_migration_status_error)

    def stop_server_process(self) -> None:
        process = self.server_process
        if process is None:
            self.server_launch_options = None
            return
        if process.poll() is not None:
            self.server_process = None
            self.server_launch_options = None
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
        self.server_launch_options = None
        self._log("Bildebank-server stoppet.")

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
        if self.update_status.status == "checking" or self._is_busy():
            self.update_button.configure(state="disabled")

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
        self.ttk.Label(frame, text="Migrering kreves", font=("", 12, "bold")).grid(
            row=0,
            column=0,
            sticky="w",
        )
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
        self.stop_server_process()
        selected_path = Path(selected)
        try:
            self._set_collection_path(selected_path)
        except OSError as exc:
            self._show_error("Kunne ikke lagre valgt plassering.", exc)
        self.collection_value.set("Bildesamling: " + str(self.collection_path))
        self._log(f"Valgt bildesamling: {self.collection_path}")
        self.migration_dialog_shown = False
        self.update_migration_status()
        self._refresh_launcher()
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
            on_success=self._refresh_launcher,
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

    def _start_snapshot_flow(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(
            title="Velg mappe for versjonert backup",
            initialdir=str(self.collection_path.parent),
            mustexist=False,
        )
        if not selected:
            self._log("Versjonert backup avbrutt: ingen mappe valgt.")
            return
        self._run_snapshot_plan(Path(selected))

    def _run_snapshot_plan(self, repository: Path) -> None:
        self._run_background_task(
            lambda _cancel_requested: plan_launcher_snapshot(self.collection_path, repository),
            running_message=f"Kontrollerer versjonert backup til {repository} ...",
            failure_message="Kontroll av versjonert backup feilet.",
            on_success=lambda plan: self._snapshot_plan_finished(repository, plan),
        )

    def _snapshot_plan_finished(
        self,
        repository: Path,
        plan: SnapshotPlan | LauncherRecoveryPlan,
    ) -> None:
        for line in snapshot_plan_log_lines(plan):
            self._log(line)
        if isinstance(plan, LauncherRecoveryPlan):
            explanation = (
                "Hoveddatabasen kunne ikke valideres. Repositorybindingen er kontrollert, "
                "men normal plassberegning er ikke mulig.\n\n"
                "Hvis du fortsetter, vil Bildebank forsøke å publisere et recovery-snapshot "
                "med alle lesbare filer og rå databasefiler."
            )
            estimated_size = ""
        else:
            explanation = (
                "Et nytt snapshot legges til. Eldre snapshots og backupobjekter "
                "blir ikke slettet eller overskrevet."
            )
            estimated_size = (
                f"\n\nEstimert ny datamengde: {format_bytes(plan.storage.estimated_new_bytes)}"
            )
        self._show_log_review_question(
            "Opprett versjonert backup?",
            (
                "Den skrivefrie kontrollen er fullført og planen står i loggen.\n\n"
                f"{explanation}\n\n"
                f"Repository:\n{repository}"
                f"{estimated_size}\n\n"
                "Vil du opprette snapshotet nå?"
            ),
            yes_text="Opprett snapshot",
            no_text="Avbryt",
            on_yes=lambda: self._run_snapshot_create(repository),
            on_no=lambda: self._log("Versjonert backup avbrutt etter kontrollen."),
        )

    def _run_snapshot_create(self, repository: Path) -> None:
        last_progress_at = [0.0]
        last_stage: list[str | None] = [None]
        last_objects = [-1]

        def report_progress(progress: SnapshotCreateProgress) -> None:
            now = time.monotonic()
            stage_changed = progress.stage != last_stage[0]
            finished = progress.completed_objects >= progress.total_objects
            if (
                not stage_changed
                and progress.completed_objects == last_objects[0]
                and not finished
                and now - last_progress_at[0] < 0.5
            ):
                return
            if (
                not stage_changed
                and progress.completed_objects not in {0, progress.total_objects}
                and progress.completed_objects % 25 != 0
                and now - last_progress_at[0] < 0.5
            ):
                return
            last_progress_at[0] = now
            last_stage[0] = progress.stage
            last_objects[0] = progress.completed_objects
            if progress.stage == "inventory":
                message = (
                    "Snapshot: lager filinventar ..."
                    if progress.total_objects == 0
                    else "Snapshot: filinventar="
                    f"{progress.completed_objects} filer "
                    f"({format_bytes(progress.completed_bytes)})"
                )
            elif progress.stage in {"files", "databases"}:
                label = "filer" if progress.stage == "files" else "databaser"
                message = (
                    f"Snapshot: {label}={progress.completed_objects}/{progress.total_objects}, "
                    f"lest={format_bytes(progress.completed_bytes)}/"
                    f"{format_bytes(progress.total_bytes)}"
                )
            else:
                if progress.completed_objects > 0:
                    return
                message = "Snapshot: publiserer manifest ..."

            def log_progress() -> None:
                self._log_progress(message)

            self._post_to_ui(log_progress)

        self._run_background_task(
            lambda _cancel_requested: create_launcher_snapshot(
                self.collection_path,
                repository,
                progress=report_progress,
            ),
            running_message=f"Oppretter versjonert backup i {repository} ...",
            failure_message="Versjonert backup feilet. Ingen snapshot ble publisert.",
            on_success=self._snapshot_creation_finished,
        )

    def _snapshot_creation_finished(self, result: SnapshotCreationResult) -> None:
        from tkinter import messagebox

        self._log(f"Snapshot opprettet med status {result.status}.")
        self._log(f"Snapshot-ID: {result.published.snapshot_id}")
        self._log(f"Snapshotmappe: {result.published.snapshot_dir}")
        for warning in result.build.warnings:
            self._log(f"ADVARSEL: {warning}")

        details = (
            f"Snapshot-ID:\n{result.published.snapshot_id}\n\n"
            f"Snapshotmappe:\n{result.published.snapshot_dir}"
        )
        if result.status == "complete":
            messagebox.showinfo(
                "Versjonert backup fullført",
                "Snapshotet ble opprettet uten kjente avvik.\n\n" + details,
                parent=self.root,
            )
        elif result.status == "degraded":
            messagebox.showwarning(
                "Versjonert backup opprettet med problemer",
                "Snapshotet ble publisert, men har avvik som må kontrolleres.\n\n" + details,
                parent=self.root,
            )
        else:
            messagebox.showwarning(
                "Recovery-snapshot opprettet",
                "Hoveddatabasen kunne ikke sikres normalt. Et recovery-snapshot ble publisert.\n\n"
                + details,
                parent=self.root,
            )
        self._refresh_launcher()

    def _start_snapshot_check_flow(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(
            title="Velg versjonert backup som skal kontrolleres",
            initialdir=str(self.collection_path.parent),
            mustexist=True,
        )
        if not selected:
            self._log("Full kontroll av versjonert backup avbrutt: ingen mappe valgt.")
            return
        repository = Path(selected)
        self._show_log_review_question(
            "Kontroller hele den versjonerte backupen?",
            (
                "Bildebank vil lese og beregne SHA-256 for alle backupobjekter, "
                "også objekter som ikke lenger refereres av et snapshot.\n\n"
                "Dette kan ta lang tid, men endrer ikke snapshots eller objekter. "
                "Kontrollen kan avbrytes kontrollert.\n\n"
                f"Repository:\n{repository}"
            ),
            yes_text="Start full kontroll",
            no_text="Avbryt",
            on_yes=lambda: self._run_snapshot_check(repository),
            on_no=lambda: self._log("Full kontroll av versjonert backup avbrutt."),
        )

    def _run_snapshot_check(self, repository: Path) -> None:
        last_progress_at = [0.0]
        last_objects = [-1]

        def report_progress(progress: SnapshotCheckProgress) -> None:
            now = time.monotonic()
            finished = progress.checked_objects >= progress.total_objects
            if (
                progress.checked_objects == last_objects[0]
                and not finished
                and now - last_progress_at[0] < 0.5
            ):
                return
            if (
                progress.checked_objects not in {0, progress.total_objects}
                and progress.checked_objects % 25 != 0
                and now - last_progress_at[0] < 0.5
            ):
                return
            last_progress_at[0] = now
            last_objects[0] = progress.checked_objects
            message = (
                "Snapshot check: "
                f"objekter={progress.checked_objects}/{progress.total_objects}, "
                f"lest={format_bytes(progress.checked_bytes)}/{format_bytes(progress.total_bytes)}"
            )
            self._post_to_ui(lambda: self._log_progress(message))

        self._run_background_task(
            lambda cancel_requested: check_snapshot_repository(
                repository,
                full=True,
                progress=report_progress,
                should_cancel=cancel_requested,
            ),
            running_message=f"Kontrollerer alle objekter i {repository} ...",
            failure_message="Full kontroll av versjonert backup feilet.",
            on_success=self._snapshot_check_finished,
            cancellable=True,
        )

    def _snapshot_check_finished(self, result: SnapshotCheckResult) -> None:
        from tkinter import messagebox

        self._log("Full snapshotkontroll " + ("avbrutt." if result.cancelled else "fullført."))
        self._log(f"Repository: {result.repository}")
        self._log(f"Kontrollerte objekter: {result.checked_objects}/{result.total_objects}")
        self._log(f"Kontrollerte byte: {format_bytes(result.checked_bytes)}")
        self._log(f"Repositoryavvik: {len(result.issues)}")
        for run in result.incomplete_runs:
            self._log(
                f"ADVARSEL: Ufullstendig kjøring {run.run_id}: {format_bytes(run.size_bytes)}"
            )
        for issue in result.issues:
            self._log(f"FEIL: {issue.message}")
            for affected in issue.affected:
                entry = f", entry_id={affected.entry_id}" if affected.entry_id else ""
                self._log(
                    f"  Berørt: snapshot={affected.snapshot_id}{entry}, "
                    f"sti={affected.logical_path}"
                )

        if result.cancelled:
            messagebox.showwarning(
                "Kontroll avbrutt",
                "Kontrollen ble avbrutt før alle objekter var lest. Repositoryet ble ikke endret.",
                parent=self.root,
            )
        elif result.issues:
            messagebox.showwarning(
                "Backupen har integritetsavvik",
                (
                    f"Kontrollen fant {len(result.issues)} repositoryavvik. "
                    "Berørte snapshots og stier står i loggen.\n\n"
                    "Ikke bruk denne backupen som eneste kopi."
                ),
                parent=self.root,
            )
        elif result.incomplete_runs:
            messagebox.showwarning(
                "Backupen er kontrollert med advarsler",
                (
                    "Alle publiserte data besto kontrollen, men repositoryet inneholder "
                    "ufullstendige kjøringer. Se loggen."
                ),
                parent=self.root,
            )
        else:
            messagebox.showinfo(
                "Versjonert backup kontrollert",
                (
                    f"Alle {result.checked_objects} objekter besto full SHA-256-kontroll.\n\n"
                    f"Kontrollert datamengde: {format_bytes(result.checked_bytes)}"
                ),
                parent=self.root,
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
            on_success=self._refresh_launcher,
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
        self.update_migration_status()
        self.migration_dialog_shown = False
        self._refresh_launcher()
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
        self.stop_server_process()
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
        self.start_update_status_refresh()

    def start_update_status_refresh(self) -> None:
        if self.update_checking:
            return
        self.update_checking = True
        self.update_status = LauncherUpdateStatus("checking")
        self._apply_update_button_state()
        self._set_launcher_buttons_enabled(not self._is_busy())
        thread = threading.Thread(target=self._update_status_worker, daemon=True)
        thread.start()

    def _update_status_worker(self) -> None:
        status = check_launcher_update_status()
        self._post_to_ui(lambda: self._update_status_finished(status))

    def _update_status_finished(self, status: LauncherUpdateStatus) -> None:
        self.update_checking = False
        self.update_status = status
        if status.status == "error" and status.detail:
            self._log(f"Oppdateringssjekk feilet: {status.detail}")
        elif status.status == "skipped" and status.detail:
            self._log(f"Oppdateringssjekk hoppet over: {status.detail}")
        self._apply_update_button_state()
        self._set_launcher_buttons_enabled(not self._is_busy())

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

    def _start_server(self) -> None:
        self.start_server()

    def start_server(
        self,
        *,
        port: int = DEFAULT_PORT,
        read_only: bool = False,
        lan_share: bool = False,
        slideshow: bool = False,
        delay: int = DEFAULT_SLIDESHOW_DELAY_SECONDS,
        filter: str | None = None,
        confirm_lan_start: Callable[[], bool] | None = None,
    ) -> None:
        from tkinter import messagebox

        options = normalize_server_launch_options(
            port=port,
            read_only=read_only,
            lan_share=lan_share,
            slideshow=slideshow,
            delay=delay,
            filter=filter,
        )

        self.update_migration_status()
        if self.migration_required:
            self._refresh_launcher()
            self._show_migration_required_dialog()
            return
        if self.migration_status_error is not None:
            self._refresh_launcher()
            self._show_migration_status_error()
            return

        lan_confirmed = False
        if self.server_process is not None:
            if self.server_process.poll() is None:
                if self.server_launch_options == options:
                    self._log("Bildebank-server kjører allerede. Åpner nytt vindu.")
                    if not open_server_browser_window(options.port):
                        self._log(
                            "Kunne ikke åpne nettleser automatisk. "
                            f"Åpne {server_browser_url(options.port)} manuelt."
                        )
                    return
                if not messagebox.askokcancel(
                    "Starte serveren på nytt?",
                    (
                        "Bildebank-serveren kjører med andre oppstartsvalg.\n\n"
                        "Vil du stoppe den og starte på nytt med de valgte innstillingene?"
                    ),
                    parent=self.root,
                ):
                    self._log("Omstart av Bildebank-server avbrutt.")
                    return
                if (
                    (options.lan_share or options.slideshow)
                    and confirm_lan_start is not None
                    and not confirm_lan_start()
                ):
                    return
                lan_confirmed = options.lan_share or options.slideshow
                self.stop_server_process()
            else:
                self.server_process = None
                self.server_launch_options = None

        if (
            (options.lan_share or options.slideshow)
            and not lan_confirmed
            and confirm_lan_start is not None
            and not confirm_lan_start()
        ):
            return

        self._log("Starter Bildebank ...")
        try:
            self.server_process = subprocess.Popen(
                run_server_command(
                    self.collection_path,
                    port=(
                        None
                        if options.port == DEFAULT_PORT
                        and not options.read_only
                        and not options.lan_share
                        and not options.slideshow
                        else options.port
                    ),
                    read_only=options.read_only,
                    lan_share=options.lan_share,
                    slideshow=options.slideshow,
                    delay=(
                        options.delay
                        if options.delay is not None
                        else DEFAULT_SLIDESHOW_DELAY_SECONDS
                    ),
                    filter=options.filter,
                )
            )
        except OSError as exc:
            messagebox.showerror("Kunne ikke starte Bildebank", "Bildebank-serveren kunne ikke startes.")
            self._log(f"Kunne ikke starte Bildebank: {exc}")
            return
        self.server_port = options.port
        self.server_launch_options = options
        self._log("Bildebank-serveren starter. Nettleseren åpnes av Bildebank når serveren er klar.")
