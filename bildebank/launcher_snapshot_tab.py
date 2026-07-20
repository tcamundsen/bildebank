from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol

from .config import load_config
from .formatting import format_bytes
from .launcher_status import program_repo_root
from .program_state import (
    known_snapshot_repositories_for_target,
    record_published_snapshot_best_effort,
)
from .snapshot import MainDatabaseSourceError, SnapshotPlan, plan_snapshot
from .snapshot_check import (
    SnapshotCheckProgress,
    SnapshotCheckResult,
    SnapshotSummary,
    check_snapshot_repository,
    list_repository_snapshots,
)
from .snapshot_create import (
    SnapshotCreationResult,
    create_snapshot,
    validate_existing_recovery_repository,
)
from .snapshot_progress import (
    SnapshotCancelCallback,
    SnapshotCancelled,
    SnapshotCreateProgress,
    SnapshotCreateProgressCallback,
    SnapshotPlanProgress,
    SnapshotPlanProgressCallback,
    raise_if_snapshot_cancelled,
)
from .snapshot_repository import SnapshotStorageError
from .snapshot_restore import (
    RestoreEntry,
    SingleFileRestorePlan,
    SingleFileRestoreResult,
    locked_snapshot,
    plan_single_file_restore,
    restore_single_file,
)


_YEAR_RE = re.compile(r"\d{4}")
_MONTH_RE = re.compile(r"(?:0[1-9]|1[0-2])")


class ButtonFactory(Protocol):
    def __call__(self, parent: Any, **kwargs: Any) -> Any: ...


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
class LauncherRecoveryPlan:
    source_dir: Path
    repository_dir: Path
    database_error: str


@dataclass(frozen=True)
class SnapshotBrowseResult:
    snapshot: SnapshotSummary
    entries: tuple[RestoreEntry, ...]


@dataclass(frozen=True)
class SingleFilePlanChoices:
    plans: tuple[SingleFileRestorePlan, ...]
    errors: tuple[str, ...]


def snapshot_dialog_initial_directory(collection: Path) -> Path:
    repository = latest_available_snapshot_repository(collection)
    return repository if repository is not None else collection.parent


def latest_available_snapshot_repository(collection: Path) -> Path | None:
    try:
        repositories = known_snapshot_repositories_for_target(program_repo_root(), collection)
    except Exception:
        repositories = []
    for repository in repositories:
        if repository.path.is_dir():
            return repository.path
    return None


def plan_launcher_snapshot(
    collection: Path,
    repository: Path,
    *,
    progress: SnapshotPlanProgressCallback | None = None,
    should_cancel: SnapshotCancelCallback | None = None,
) -> SnapshotPlan | LauncherRecoveryPlan:
    config = load_config(program_repo_root(), migrate_legacy=False)
    try:
        if progress is not None and should_cancel is None:
            return plan_snapshot(
                collection,
                repository,
                configured_face_database_dir=config.face_recognition.database_dir,
                progress=progress,
            )
        if progress is not None or should_cancel is not None:
            return plan_snapshot(
                collection,
                repository,
                configured_face_database_dir=config.face_recognition.database_dir,
                progress=progress,
                should_cancel=should_cancel,
            )
        return plan_snapshot(
            collection,
            repository,
            configured_face_database_dir=config.face_recognition.database_dir,
        )
    except MainDatabaseSourceError as exc:
        raise_if_snapshot_cancelled(should_cancel)
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
    should_cancel: SnapshotCancelCallback | None = None,
) -> SnapshotCreationResult:
    config = load_config(program_repo_root(), migrate_legacy=False)
    if progress is None and should_cancel is None:
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
        should_cancel=should_cancel,
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
        "Plan for snapshot:",
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


def browse_path_parts(entry: RestoreEntry) -> tuple[str, ...] | None:
    if entry.record_type != "file" or entry.restore_kind != "normal" or entry.path is None:
        return None
    parts = PurePosixPath(entry.path).parts
    if (
        len(parts) == 3
        and _YEAR_RE.fullmatch(parts[0])
        and _MONTH_RE.fullmatch(parts[1])
    ):
        return parts
    if len(parts) == 2 and parts[0] == "udatert":
        return parts
    if (
        len(parts) == 4
        and parts[0] == "deleted"
        and _YEAR_RE.fullmatch(parts[1])
        and _MONTH_RE.fullmatch(parts[2])
    ):
        return parts
    if len(parts) == 3 and parts[:2] == ("deleted", "udatert"):
        return parts
    return None


def browse_sort_key(entry: RestoreEntry) -> tuple[object, ...]:
    parts = browse_path_parts(entry)
    if parts is None:
        return (9, entry.original_path_display.casefold())
    if parts[0] == "deleted":
        if parts[1] == "udatert":
            return (2, 1, parts[-1].casefold())
        return (2, 0, -int(parts[1]), int(parts[2]), parts[-1].casefold())
    if parts[0] == "udatert":
        return (1, parts[-1].casefold())
    return (0, -int(parts[0]), int(parts[1]), parts[-1].casefold())


def browse_snapshot_media(repository: Path, snapshot_id: str) -> SnapshotBrowseResult:
    with locked_snapshot(repository, snapshot_id, command="launcher snapshot browse") as loaded:
        entries = tuple(
            sorted(
                (entry for entry in loaded.entries if browse_path_parts(entry) is not None),
                key=browse_sort_key,
            )
        )
        return SnapshotBrowseResult(snapshot=loaded.summary, entries=entries)


def plan_single_file_choices(
    repository: Path,
    snapshot_id: str,
    export_directory: Path,
    entry: RestoreEntry,
) -> SingleFilePlanChoices:
    assert entry.path is not None
    variants: tuple[str | None, ...] = (None,)
    if entry.expected is not None and entry.object is not None and entry.expected != entry.object:
        variants = ("expected", "observed")
    plans: list[SingleFileRestorePlan] = []
    errors: list[str] = []
    for variant in variants:
        try:
            plans.append(
                plan_single_file_restore(
                    repository,
                    snapshot_id,
                    export_directory,
                    path=entry.path,
                    variant=variant,
                )
            )
        except SnapshotStorageError as exc:
            label = variant or "automatisk variant"
            errors.append(f"{label}: {exc}")
    if not plans:
        raise SnapshotStorageError("Ingen restorerbar variant ble funnet. " + " ".join(errors))
    return SingleFilePlanChoices(plans=tuple(plans), errors=tuple(errors))


def default_download_export_directory() -> Path:
    home = Path.home()
    downloads = home / "Downloads"
    parent = downloads if downloads.is_dir() else home
    return parent / "Bildebank-gjenopprettet"


def snapshot_display_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone().strftime("%d.%m.%Y %H:%M")


def snapshot_status_label(status: str) -> str:
    return {
        "complete": "Fullført",
        "degraded": "Med avvik",
        "recovery": "Redning",
    }.get(status, status)


def integrity_status_label(status: str) -> str:
    return {
        "ok": "OK",
        "missing": "Mangler",
        "unreadable": "Kan ikke leses",
        "hash_mismatch": "Innholdsavvik",
        "size_mismatch": "Størrelsesavvik",
        "changed_during_snapshot": "Endret under snapshot",
    }.get(status, status)


class SnapshotTab:
    def __init__(
        self,
        *,
        tk: Any,
        ttk: Any,
        notebook: Any,
        root: Any,
        button: ButtonFactory,
        run_background_task: BackgroundTaskRunner,
        get_collection_path: Callable[[], Path],
        post_to_ui: Callable[[Callable[[], None]], bool],
        log: Callable[[str], None],
        log_progress: Callable[[str], None],
        refresh_launcher: Callable[[], None],
        add_tooltip: Callable[[Any, str], None],
        show_log_review_question: LogReviewQuestion,
        padding: int,
        padx: int,
        pady: int,
    ) -> None:
        self.tk = tk
        self.ttk = ttk
        self.root = root
        self._button = button
        self._run_background_task = run_background_task
        self._get_collection_path = get_collection_path
        self._post_to_ui = post_to_ui
        self._log = log
        self._log_progress = log_progress
        self._refresh_launcher = refresh_launcher
        self._add_tooltip = add_tooltip
        self._show_log_review_question = show_log_review_question
        self.padx = padx
        self.pady = pady
        self.selected_repository: Path | None = None
        self.repository_collection: Path | None = None
        self.create_available = False

        self.frame = ttk.Frame(notebook, padding=padding)
        self.frame.columnconfigure(0, weight=1)
        repository_frame = ttk.Frame(self.frame)
        repository_frame.grid(row=0, column=0, sticky="ew")
        repository_frame.columnconfigure(0, weight=1)
        self.repository_value = tk.StringVar(value="Snapshot-repository: ikke valgt")
        ttk.Label(repository_frame, textvariable=self.repository_value, wraplength=560).grid(
            row=0, column=0, sticky="w", padx=padx, pady=pady
        )
        self.choose_repository_button = self._button(
            repository_frame,
            text="Velg repository",
            command=self._choose_repository,
        )
        self.choose_repository_button.grid(row=1, column=0, sticky="w", padx=padx, pady=pady)

        ttk.Separator(self.frame, orient="horizontal").grid(
            row=1, column=0, sticky="ew", pady=padding
        )
        action_frame = ttk.Frame(self.frame)
        action_frame.grid(row=2, column=0, sticky="w")
        self.create_button = self._button(
            action_frame,
            text="Opprett snapshot",
            command=self._start_snapshot_flow,
        )
        self.create_button.grid(row=0, column=0, padx=padx, pady=pady, sticky="ew")
        self.check_button = self._button(
            action_frame,
            text="Kontroller snapshots",
            command=self._start_snapshot_check_flow,
        )
        self.check_button.grid(row=0, column=1, padx=padx, pady=pady, sticky="ew")
        self.restore_file_button = self._button(
            action_frame,
            text="Gjenopprett fil",
            command=self._start_restore_file_flow,
        )
        self.restore_file_button.grid(row=0, column=2, padx=padx, pady=pady, sticky="ew")

    @property
    def collection_path(self) -> Path:
        return self._get_collection_path()

    def refresh(self, *, create_available: bool) -> list[Any]:
        self.create_available = create_available
        collection = self.collection_path
        if self.repository_collection != collection:
            self.repository_collection = collection
            self._set_repository(latest_available_snapshot_repository(collection))
        self._add_tooltip(
            self.create_button,
            "Lager et nytt, uforanderlig snapshot uten å slette eldre snapshots. "
            "En skrivefri plan vises før du bekrefter.",
        )
        self._add_tooltip(
            self.check_button,
            "Leser og SHA-256-kontrollerer alle objekter i repositoryet uten å endre det.",
        )
        self._add_tooltip(
            self.restore_file_button,
            "Henter en kontrollert kopi av én fil til en mappe utenfor bildesamlingen.",
        )
        self.set_buttons_enabled(True)
        return [
            self.choose_repository_button,
            self.create_button,
            self.check_button,
            self.restore_file_button,
        ]

    def set_buttons_enabled(self, enabled: bool) -> None:
        repository_selected = self.selected_repository is not None
        self.choose_repository_button.configure(state="normal" if enabled else "disabled")
        self.create_button.configure(
            state="normal" if enabled and repository_selected and self.create_available else "disabled"
        )
        repository_state = "normal" if enabled and repository_selected else "disabled"
        self.check_button.configure(state=repository_state)
        self.restore_file_button.configure(state=repository_state)

    def _set_repository(self, repository: Path | None) -> None:
        self.selected_repository = repository
        value = "ikke valgt" if repository is None else str(repository)
        self.repository_value.set(f"Snapshot-repository: {value}")

    def _choose_repository(self) -> None:
        from tkinter import filedialog

        initial = self.selected_repository or snapshot_dialog_initial_directory(self.collection_path)
        selected = filedialog.askdirectory(
            title="Velg snapshot-repository",
            initialdir=str(initial),
            mustexist=False,
        )
        if not selected:
            self._log("Valg av snapshot-repository avbrutt.")
            return
        self._set_repository(Path(selected))
        self._log(f"Valgt snapshot-repository: {self.selected_repository}")
        self._refresh_launcher()

    def _required_repository(self) -> Path:
        if self.selected_repository is None:
            raise ValueError("Velg et snapshot-repository først.")
        return self.selected_repository

    def _start_snapshot_flow(self) -> None:
        self._run_snapshot_plan(self._required_repository())

    def _run_snapshot_plan(self, repository: Path) -> None:
        last_progress_at = [0.0]
        last_stage: list[str | None] = [None]
        last_objects = [-1]

        def report_progress(progress: SnapshotPlanProgress) -> None:
            now = time.monotonic()
            stage_changed = progress.stage != last_stage[0]
            finished = (
                progress.total_objects > 0
                and progress.completed_objects >= progress.total_objects
            )
            if (
                not stage_changed
                and progress.completed_objects == last_objects[0]
                and not finished
                and now - last_progress_at[0] < 1.0
            ):
                return
            if (
                not stage_changed
                and progress.completed_objects not in {0, progress.total_objects}
                and progress.completed_objects % 1_000 != 0
                and now - last_progress_at[0] < 1.0
            ):
                return
            last_progress_at[0] = now
            last_stage[0] = progress.stage
            last_objects[0] = progress.completed_objects

            if progress.stage == "database":
                message = "Snapshot dry-run: leser og kontrollerer hoveddatabasen ..."
            elif progress.stage == "database_complete":
                message = (
                    f"Snapshot dry-run: hoveddatabase={progress.completed_objects} filposter"
                )
            elif progress.stage == "inventory":
                if progress.completed_objects == 0:
                    message = "Snapshot dry-run: bygger filinventar ..."
                elif progress.total_objects == 0:
                    message = (
                        f"Snapshot dry-run: filer funnet={progress.completed_objects}, "
                        f"registrert={format_bytes(progress.completed_bytes)}"
                    )
                else:
                    message = (
                        f"Snapshot dry-run: filinventar={progress.completed_objects} filer "
                        f"({format_bytes(progress.completed_bytes)})"
                    )
            elif progress.stage == "files":
                if progress.total_objects == 0:
                    message = "Snapshot dry-run: ingen databaseførte filer å sammenligne."
                else:
                    message = (
                        "Snapshot dry-run: databaseførte filer kontrollert="
                        f"{progress.completed_objects}/{progress.total_objects}"
                    )
            else:
                message = "Snapshot dry-run: beregner plassbehov ..."
            self._post_to_ui(lambda: self._log_progress(message))

        def run_plan(should_cancel: Callable[[], bool]) -> SnapshotPlan | LauncherRecoveryPlan | None:
            try:
                return plan_launcher_snapshot(
                    self.collection_path,
                    repository,
                    progress=report_progress,
                    should_cancel=should_cancel,
                )
            except SnapshotCancelled:
                return None

        self._run_background_task(
            run_plan,
            running_message=f"Kontrollerer snapshot-plan for {repository} ...",
            failure_message="Kontroll av snapshot-plan feilet.",
            on_success=lambda plan: self._snapshot_plan_task_finished(repository, plan),
            cancellable=True,
        )

    def _snapshot_plan_task_finished(
        self,
        repository: Path,
        plan: SnapshotPlan | LauncherRecoveryPlan | None,
    ) -> None:
        if plan is None:
            self._log("Kontroll av snapshot-plan ble avbrutt. Ingen endringer ble gjort.")
            return
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
                "Et nytt snapshot legges til. Eldre snapshots og tilhørende objekter "
                "blir ikke slettet eller overskrevet."
            )
            estimated_size = (
                f"\n\nEstimert ny datamengde: {format_bytes(plan.storage.estimated_new_bytes)}"
            )
        self._show_log_review_question(
            "Opprett snapshot?",
            (
                "Den skrivefrie kontrollen er fullført og planen står i loggen.\n\n"
                f"{explanation}\n\nRepository:\n{repository}"
                f"{estimated_size}\n\nVil du opprette snapshotet nå?"
            ),
            yes_text="Opprett snapshot",
            no_text="Avbryt",
            on_yes=lambda: self._run_snapshot_create(repository),
            on_no=lambda: self._log("Snapshot avbrutt etter kontrollen."),
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
            self._post_to_ui(lambda: self._log_progress(message))

        def run_create(should_cancel: Callable[[], bool]) -> SnapshotCreationResult | None:
            try:
                return create_launcher_snapshot(
                    self.collection_path,
                    repository,
                    progress=report_progress,
                    should_cancel=should_cancel,
                )
            except SnapshotCancelled:
                return None

        self._run_background_task(
            run_create,
            running_message=f"Oppretter snapshot i {repository} ...",
            failure_message="Snapshot feilet. Ingen snapshot ble publisert.",
            on_success=lambda result: self._snapshot_creation_task_finished(repository, result),
            cancellable=True,
        )

    def _snapshot_creation_task_finished(
        self,
        repository: Path,
        result: SnapshotCreationResult | None,
    ) -> None:
        if result is None:
            from tkinter import messagebox

            self._log(
                "Snapshot ble avbrutt kontrollert. Ingen nytt snapshot ble publisert. "
                "Tidligere snapshots er uendret, og eventuelle ufullstendige data er beholdt "
                f"under {repository / 'incomplete'}."
            )
            messagebox.showinfo(
                "Snapshot avbrutt",
                "Snapshotet ble avbrutt kontrollert. Ingen nytt snapshot ble publisert.\n\n"
                "Tidligere snapshots er uendret. Ufullstendige data er beholdt for kontroll.",
                parent=self.root,
            )
            self._refresh_launcher()
            return
        self._snapshot_creation_finished(result)

    def _snapshot_creation_finished(self, result: SnapshotCreationResult) -> None:
        from tkinter import messagebox

        self._log(f"Snapshot opprettet med status {result.status}.")
        self._log(f"Snapshot-ID: {result.published.snapshot_id}")
        self._log(f"Snapshotmappe: {result.published.snapshot_dir}")
        for warning in result.build.warnings:
            self._log(f"ADVARSEL: {warning}")

        state_error = record_published_snapshot_best_effort(
            program_repo_root(),
            collection_id=result.build.collection_id,
            repository_id=result.build.repository_id,
            repository_path=result.repository,
            snapshot_id=result.published.snapshot_id,
            status=result.status,
        )
        state_warning = ""
        if state_error is not None:
            warning = (
                "Snapshotet er publisert, men Bildebank klarte ikke å huske "
                f"repositoryet lokalt: {state_error}"
            )
            self._log(f"ADVARSEL: {warning}")
            state_warning = f"\n\nADVARSEL: {warning}"

        details = (
            f"Snapshot-ID:\n{result.published.snapshot_id}\n\n"
            f"Snapshotmappe:\n{result.published.snapshot_dir}{state_warning}"
        )
        if result.status == "complete":
            messagebox.showinfo(
                "Snapshot fullført",
                "Snapshotet ble opprettet uten kjente avvik.\n\n" + details,
                parent=self.root,
            )
        elif result.status == "degraded":
            messagebox.showwarning(
                "Snapshot opprettet med problemer",
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
        self._set_repository(result.repository)
        self._refresh_launcher()

    def _start_snapshot_check_flow(self) -> None:
        repository = self._required_repository()
        self._show_log_review_question(
            "Kontroller hele snapshot-repositoryet?",
            (
                "Bildebank vil lese og beregne SHA-256 for alle objekter, "
                "også objekter som ikke lenger refereres av et snapshot.\n\n"
                "Dette kan ta lang tid, men endrer ikke snapshots eller objekter. "
                "Kontrollen kan avbrytes kontrollert.\n\n"
                f"Repository:\n{repository}"
            ),
            yes_text="Start full kontroll",
            no_text="Avbryt",
            on_yes=lambda: self._run_snapshot_check(repository),
            on_no=lambda: self._log("Full snapshotkontroll avbrutt."),
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
            failure_message="Full snapshotkontroll feilet.",
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
                "Snapshot-repositoryet har integritetsavvik",
                (
                    f"Kontrollen fant {len(result.issues)} repositoryavvik. "
                    "Berørte snapshots og stier står i loggen.\n\n"
                    "Ikke bruk dette repositoryet som eneste kopi."
                ),
                parent=self.root,
            )
        elif result.incomplete_runs:
            messagebox.showwarning(
                "Repositoryet er kontrollert med advarsler",
                (
                    "Alle publiserte data besto kontrollen, men repositoryet inneholder "
                    "ufullstendige kjøringer. Se loggen."
                ),
                parent=self.root,
            )
        else:
            messagebox.showinfo(
                "Snapshot-repository kontrollert",
                (
                    f"Alle {result.checked_objects} objekter besto full SHA-256-kontroll.\n\n"
                    f"Kontrollert datamengde: {format_bytes(result.checked_bytes)}"
                ),
                parent=self.root,
            )

    def _start_restore_file_flow(self) -> None:
        repository = self._required_repository()
        self._run_background_task(
            lambda _cancel_requested: list_repository_snapshots(repository),
            running_message=f"Leser snapshots i {repository} ...",
            failure_message="Kunne ikke lese snapshotlisten.",
            on_success=self._snapshot_list_finished,
        )

    def _snapshot_list_finished(self, result: SnapshotCheckResult) -> None:
        from tkinter import messagebox

        for issue in result.issues:
            self._log(f"ADVARSEL: {issue.message}")
        if result.issues:
            messagebox.showwarning(
                "Noen snapshots kunne ikke leses",
                "Ugyldige snapshots er utelatt fra listen. Detaljene står i loggen.",
                parent=self.root,
            )
        if not result.snapshots:
            messagebox.showinfo(
                "Ingen snapshots",
                "Repositoryet inneholder ingen gyldige, publiserte snapshots.",
                parent=self.root,
            )
            return
        self._show_snapshot_dialog(result.repository, result.snapshots)

    def _show_snapshot_dialog(
        self,
        repository: Path,
        snapshots: tuple[SnapshotSummary, ...],
    ) -> None:
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Velg snapshot")
        dialog.transient(self.root)
        dialog.minsize(760, 340)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        frame = self.ttk.Frame(dialog, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        self.ttk.Label(
            frame,
            text="Velg tidspunktet filen skal hentes fra.",
            wraplength=680,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        columns = ("date", "status", "files", "note")
        tree = self.ttk.Treeview(
            frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=10,
        )
        for key, label in (
            ("date", "Dato"),
            ("status", "Status"),
            ("files", "Filposter"),
            ("note", "Kommentar"),
        ):
            tree.heading(key, text=label)
        tree.column("date", width=135, stretch=False)
        tree.column("status", width=90, stretch=False)
        tree.column("files", width=75, stretch=False, anchor="e")
        tree.column("note", width=390, stretch=True)
        scrollbar = self.ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")
        snapshot_by_item: dict[str, SnapshotSummary] = {}
        for snapshot in reversed(snapshots):
            item = tree.insert(
                "",
                "end",
                values=(
                    snapshot_display_time(snapshot.completed_at),
                    snapshot_status_label(snapshot.status),
                    snapshot.entry_count,
                    snapshot.note or "",
                ),
            )
            snapshot_by_item[item] = snapshot
        first = tree.get_children()
        if first:
            tree.selection_set(first[0])
            tree.focus(first[0])

        def close(callback: Callable[[], None]) -> None:
            dialog.withdraw()
            dialog.destroy()
            self.root.after_idle(callback)

        def accept() -> None:
            selected = tree.selection()
            if not selected:
                return
            snapshot = snapshot_by_item[selected[0]]
            close(lambda: self._load_snapshot_files(repository, snapshot))

        button_frame = self.ttk.Frame(frame)
        button_frame.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self._button(button_frame, text="Avbryt", command=lambda: close(lambda: None)).grid(
            row=0, column=0, padx=(0, 8)
        )
        self._button(button_frame, text="Velg snapshot", command=accept).grid(
            row=0, column=1
        )
        tree.bind("<Double-1>", lambda _event: accept())
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: close(lambda: None))
        dialog.protocol("WM_DELETE_WINDOW", lambda: close(lambda: None))

    def _load_snapshot_files(self, repository: Path, snapshot: SnapshotSummary) -> None:
        self._run_background_task(
            lambda _cancel_requested: browse_snapshot_media(repository, snapshot.snapshot_id),
            running_message=f"Leser filer i snapshot {snapshot.snapshot_id} ...",
            failure_message="Kunne ikke lese filene i snapshotet.",
            on_success=lambda result: self._snapshot_files_finished(repository, result),
        )

    def _snapshot_files_finished(
        self,
        repository: Path,
        result: SnapshotBrowseResult,
    ) -> None:
        from tkinter import messagebox

        if not result.entries:
            messagebox.showinfo(
                "Ingen filer",
                "Snapshotet har ingen filer i år/måned, udatert eller deleted som kan vises her.",
                parent=self.root,
            )
            return
        self._show_file_dialog(repository, result)

    def _show_file_dialog(self, repository: Path, result: SnapshotBrowseResult) -> None:
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Velg fil")
        dialog.transient(self.root)
        dialog.minsize(700, 430)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        frame = self.ttk.Frame(dialog, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        self.ttk.Label(
            frame,
            text=(
                f"Snapshot fra {snapshot_display_time(result.snapshot.completed_at)}. "
                "Bla fra år til måned og velg en fil."
            ),
            wraplength=640,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        tree = self.ttk.Treeview(
            frame,
            columns=("status",),
            show="tree headings",
            selectmode="browse",
            height=14,
        )
        tree.heading("#0", text="Mappe eller fil")
        tree.heading("status", text="Snapshotstatus")
        tree.column("#0", width=500, stretch=True)
        tree.column("status", width=125, stretch=False)
        scrollbar = self.ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")

        node_by_parts: dict[tuple[str, ...], str] = {}
        entry_by_item: dict[str, RestoreEntry] = {}
        for entry in result.entries:
            parts = browse_path_parts(entry)
            assert parts is not None
            parent = ""
            for index, part in enumerate(parts[:-1], start=1):
                key = parts[:index]
                node = node_by_parts.get(key)
                if node is None:
                    if part == "udatert":
                        label = "Udatert"
                    elif part == "deleted":
                        label = "deleted/"
                    else:
                        label = part
                    node = tree.insert(parent, "end", text=label, open=False)
                    node_by_parts[key] = node
                parent = node
            leaf = tree.insert(
                parent,
                "end",
                text=parts[-1],
                values=(integrity_status_label(entry.integrity_status),),
            )
            entry_by_item[leaf] = entry

        def close(callback: Callable[[], None]) -> None:
            dialog.withdraw()
            dialog.destroy()
            self.root.after_idle(callback)

        button_frame = self.ttk.Frame(frame)
        select_button = self._button(
            button_frame,
            text="Gjenopprett valgt fil",
            state="disabled",
        )

        def accept() -> None:
            selected = tree.selection()
            if not selected or selected[0] not in entry_by_item:
                return
            entry = entry_by_item[selected[0]]
            close(lambda: self._show_export_directory_dialog(repository, result.snapshot, entry))

        def selection_changed(_event: Any = None) -> None:
            selected = tree.selection()
            state = "normal" if selected and selected[0] in entry_by_item else "disabled"
            select_button.configure(state=state)

        button_frame.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self._button(button_frame, text="Avbryt", command=lambda: close(lambda: None)).grid(
            row=0, column=0, padx=(0, 8)
        )
        select_button.configure(command=accept)
        select_button.grid(row=0, column=1)
        tree.bind("<<TreeviewSelect>>", selection_changed)
        tree.bind("<Double-1>", lambda _event: accept())
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: close(lambda: None))
        dialog.protocol("WM_DELETE_WINDOW", lambda: close(lambda: None))

    def _show_export_directory_dialog(
        self,
        repository: Path,
        snapshot: SnapshotSummary,
        entry: RestoreEntry,
    ) -> None:
        from tkinter import filedialog

        dialog = self.tk.Toplevel(self.root)
        dialog.title("Velg eksportmappe")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        frame = self.ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        self.ttk.Label(
            frame,
            text=(
                "Filen eksporteres som en kopi utenfor bildesamlingen. "
                "Den opprinnelige år/måned-stien beholdes under eksportmappen."
            ),
            wraplength=520,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        value = self.tk.StringVar(value=str(default_download_export_directory()))
        entry_widget = self.ttk.Entry(frame, textvariable=value, width=62)
        entry_widget.grid(row=1, column=0, sticky="ew", pady=(12, 16), padx=(0, 8))

        def browse() -> None:
            candidate = Path(value.get()).expanduser()
            initial = candidate if candidate.is_dir() else candidate.parent
            if not initial.is_dir():
                initial = Path.home()
            selected = filedialog.askdirectory(
                title="Velg eksportmappe",
                initialdir=str(initial),
                mustexist=False,
            )
            if selected:
                value.set(selected)

        self._button(frame, text="Velg annen mappe", command=browse).grid(
            row=1, column=1, sticky="e", pady=(12, 16)
        )

        def close(callback: Callable[[], None]) -> None:
            dialog.withdraw()
            dialog.destroy()
            self.root.after_idle(callback)

        def accept() -> None:
            raw = value.get().strip()
            if not raw:
                return
            export_directory = Path(raw).expanduser()
            close(
                lambda: self._plan_file_restore(
                    repository,
                    snapshot,
                    entry,
                    export_directory,
                )
            )

        button_frame = self.ttk.Frame(frame)
        button_frame.grid(row=2, column=0, columnspan=2, sticky="e")
        self._button(button_frame, text="Avbryt", command=lambda: close(lambda: None)).grid(
            row=0, column=0, padx=(0, 8)
        )
        self._button(button_frame, text="Kontroller eksport", command=accept).grid(
            row=0, column=1
        )
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: close(lambda: None))
        dialog.protocol("WM_DELETE_WINDOW", lambda: close(lambda: None))

    def _plan_file_restore(
        self,
        repository: Path,
        snapshot: SnapshotSummary,
        entry: RestoreEntry,
        export_directory: Path,
    ) -> None:
        self._run_background_task(
            lambda _cancel_requested: plan_single_file_choices(
                repository,
                snapshot.snapshot_id,
                export_directory,
                entry,
            ),
            running_message=f"Kontrollerer eksportplan for {entry.original_path_display} ...",
            failure_message="Kunne ikke planlegge filgjenopprettingen.",
            on_success=lambda choices: self._file_restore_plan_finished(
                repository,
                entry,
                choices,
            ),
        )

    def _file_restore_plan_finished(
        self,
        repository: Path,
        entry: RestoreEntry,
        choices: SingleFilePlanChoices,
    ) -> None:
        for error in choices.errors:
            self._log(f"ADVARSEL: Variant kan ikke gjenopprettes: {error}")
        if len(choices.plans) == 1:
            self._confirm_file_restore(repository, entry, choices.plans[0])
            return
        self._show_variant_dialog(repository, entry, choices.plans)

    def _show_variant_dialog(
        self,
        repository: Path,
        entry: RestoreEntry,
        plans: tuple[SingleFileRestorePlan, ...],
    ) -> None:
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Velg filvariant")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        frame = self.ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        self.ttk.Label(
            frame,
            text=(
                "Snapshotet inneholder både den databaseførte varianten og innholdet som "
                "faktisk ble observert. Velg hvilken kopi som skal eksporteres."
            ),
            wraplength=520,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        def choose(plan: SingleFileRestorePlan) -> None:
            dialog.withdraw()
            dialog.destroy()
            self.root.after_idle(lambda: self._confirm_file_restore(repository, entry, plan))

        plan_by_variant = {plan.output.variant: plan for plan in plans}
        expected = plan_by_variant.get("expected")
        observed = plan_by_variant.get("observed")
        if expected is not None:
            self._button(
                frame,
                text="Forventet (databaseført)",
                command=lambda: choose(expected),
            ).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        if observed is not None:
            self._button(
                frame,
                text="Observert ved snapshot",
                command=lambda: choose(observed),
            ).grid(row=1, column=1, sticky="ew")
        self._button(frame, text="Avbryt", command=dialog.destroy).grid(
            row=2, column=0, columnspan=2, sticky="e", pady=(12, 0)
        )
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    def _confirm_file_restore(
        self,
        repository: Path,
        entry: RestoreEntry,
        plan: SingleFileRestorePlan,
    ) -> None:
        variant_label = {
            "expected": "forventet (databaseført)",
            "observed": "observert ved snapshot",
        }.get(plan.output.variant, plan.output.variant)
        self._log("Plan for gjenoppretting av fil:")
        self._log(f"  Snapshot-ID: {plan.snapshot.snapshot_id}")
        self._log(f"  Fil: {entry.original_path_display}")
        self._log(f"  Variant: {variant_label}")
        self._log(f"  Eksportmål: {plan.output_path}")
        self._show_log_review_question(
            "Gjenopprett fil?",
            (
                f"Snapshot: {snapshot_display_time(plan.snapshot.completed_at)}\n"
                f"Fil: {entry.original_path_display}\n"
                f"Variant: {variant_label}\n"
                f"Størrelse: {format_bytes(plan.output.object.size_bytes)}\n\n"
                f"Filen eksporteres til:\n{plan.output_path}\n\n"
                "En eksisterende fil blir aldri overskrevet."
            ),
            yes_text="Gjenopprett fil",
            no_text="Avbryt",
            on_yes=lambda: self._run_file_restore(repository, entry, plan),
            on_no=lambda: self._log("Gjenoppretting av fil avbrutt."),
        )

    def _run_file_restore(
        self,
        repository: Path,
        entry: RestoreEntry,
        plan: SingleFileRestorePlan,
    ) -> None:
        assert entry.path is not None
        self._run_background_task(
            lambda _cancel_requested: restore_single_file(
                repository,
                plan.snapshot.snapshot_id,
                plan.export_directory,
                path=entry.path,
                variant=plan.output.variant,
            ),
            running_message=f"Gjenoppretter {entry.original_path_display} ...",
            failure_message=(
                "Gjenoppretting av fil feilet. Eventuell ufullstendig utdata er bevart."
            ),
            on_success=self._file_restore_finished,
        )

    def _file_restore_finished(self, result: SingleFileRestoreResult) -> None:
        from tkinter import messagebox

        self._log(f"Fil gjenopprettet: {result.output_path}")
        messagebox.showinfo(
            "Fil gjenopprettet",
            f"Filen er kontrollert og eksportert til:\n\n{result.output_path}",
            parent=self.root,
        )
