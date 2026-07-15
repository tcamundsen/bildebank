from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.launcher_main_tab import (
    LauncherRecoveryPlan,
    MainTab,
    create_launcher_snapshot,
    open_server_browser_window,
    plan_launcher_snapshot,
    server_browser_url,
    snapshot_plan_log_lines,
)
from bildebank.launcher_status import LauncherUpdateStatus
from bildebank.snapshot import MainDatabaseSourceError


class FakeButton:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def configure(self, **kwargs: object) -> None:
        self.options.update(kwargs)


class FakeWidget(FakeButton):
    def __init__(self, parent: FakeWidget | None = None, **options: object) -> None:
        super().__init__()
        self.parent = parent
        self.options.update(options)
        self.children: list[FakeWidget] = []
        if parent is not None:
            parent.children.append(self)

    def destroy(self) -> None:
        if self.parent is not None:
            self.parent.children.remove(self)

    def grid(self, *_args: object, **_kwargs: object) -> None:
        pass

    def winfo_children(self) -> list[FakeWidget]:
        return list(self.children)


def bare_main_tab(collection_path: Path) -> MainTab:
    tab = MainTab.__new__(MainTab)
    tab._get_collection_path = lambda: collection_path
    tab._is_busy = lambda: False
    tab.choose_collection_button = FakeButton()
    tab.create_collection_button = FakeButton()
    tab.start_server_button = FakeButton()
    tab.backup_button = FakeButton()
    tab.snapshot_button = FakeButton()
    tab.snapshot_check_button = FakeButton()
    tab.update_button = FakeButton()
    tab.update_button_icons = {}
    tab.update_status = LauncherUpdateStatus("current")
    tab.update_checking = False
    tab.migration_required = False
    tab.migration_status_error = None
    tab.server_port = 8765
    return tab


def test_main_tab_refresh_builds_normal_and_migration_actions(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.button_frame = FakeWidget()
    tab._button = FakeWidget
    tab._add_tooltip = lambda _widget, _text: None
    tab._on_close = lambda: None
    tab.create_collection_tooltip = SimpleNamespace(text="", hide=lambda: None)
    tab.padx = 4
    tab.pady = 4

    with patch("bildebank.launcher_main_tab.is_collection_created", return_value=True):
        state = tab.refresh()
        assert state.available
        assert [button.options["text"] for button in state.buttons] == [
            "Start Bildebank i nettleser",
            "Se etter oppdateringer",
            "Ta backup",
            "Ta versjonert backup",
            "Kontroller versjonert backup",
        ]

        tab.migration_required = True
        state = tab.refresh()
        assert not state.available
        assert [button.options["text"] for button in state.buttons] == [
            "Migrer",
            "Avslutt uten å migrere",
        ]


def test_open_server_browser_window_opens_default_run_server_url() -> None:
    with patch("webbrowser.open", return_value=True) as open_browser:
        assert open_server_browser_window()

    assert server_browser_url() == "http://127.0.0.1:8765/"
    open_browser.assert_called_once_with("http://127.0.0.1:8765/", new=1)


def test_open_server_browser_window_uses_selected_port() -> None:
    with patch("webbrowser.open", return_value=True) as open_browser:
        assert open_server_browser_window(9000)

    assert server_browser_url(9000) == "http://127.0.0.1:9000/"
    open_browser.assert_called_once_with("http://127.0.0.1:9000/", new=1)


def test_update_status_refresh_starts_background_worker(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab._set_launcher_buttons_enabled = lambda _enabled: None

    with patch("bildebank.launcher_main_tab.threading.Thread") as thread:
        tab.start_update_status_refresh()

    thread.assert_called_once_with(target=tab._update_status_worker, daemon=True)
    thread.return_value.start.assert_called_once_with()
    assert tab.update_checking
    assert tab.update_status.status == "checking"


def test_update_status_worker_posts_result_to_ui(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    callbacks: list[object] = []
    finished: list[LauncherUpdateStatus] = []
    expected = LauncherUpdateStatus("available", commits_behind=2)
    tab._post_to_ui = lambda callback: callbacks.append(callback) or True
    tab._update_status_finished = finished.append

    with patch(
        "bildebank.launcher_main_tab.check_launcher_update_status",
        return_value=expected,
    ):
        tab._update_status_worker()

    assert len(callbacks) == 1
    callback = callbacks[0]
    assert callable(callback)
    callback()
    assert finished == [expected]


def test_update_button_text_reflects_update_status(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")

    tab.update_status = LauncherUpdateStatus("checking")
    assert tab._update_button_text() == "Ser etter oppdateringer ..."

    tab.update_status = LauncherUpdateStatus("available", commits_behind=1)
    assert tab._update_button_text() == "Installer oppdatering"

    tab.update_status = LauncherUpdateStatus("current")
    assert tab._update_button_text() == "Se etter oppdateringer"

    tab.update_status = LauncherUpdateStatus("error", "nettverksfeil")
    assert tab._update_button_text() == "Se etter oppdateringer"


def test_apply_update_button_state_updates_label_and_disables_while_checking(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.update_status = LauncherUpdateStatus("checking")

    tab._apply_update_button_state()

    assert tab.update_button.options["text"] == "Ser etter oppdateringer ..."
    assert tab.update_button.options["image"] == ""
    assert tab.update_button.options["compound"] == "none"
    assert tab.update_button.options["state"] == "disabled"


def test_apply_update_button_state_uses_icon_when_available(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    search_icon = object()
    tab.update_button_icons = {"search": search_icon}

    tab._apply_update_button_state()

    assert tab.update_button.options["text"] == "Se etter oppdateringer"
    assert tab.update_button.options["image"] is search_icon
    assert tab.update_button.options["compound"] == "left"


def test_update_button_icon_uses_green_check_only_for_available(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    search_icon = object()
    green_check_icon = object()
    tab.update_button_icons = {"search": search_icon, "green-check": green_check_icon}

    tab.update_status = LauncherUpdateStatus("checking")
    assert tab._update_button_icon() is search_icon
    tab.update_status = LauncherUpdateStatus("current")
    assert tab._update_button_icon() is search_icon
    tab.update_status = LauncherUpdateStatus("error")
    assert tab._update_button_icon() is search_icon
    tab.update_status = LauncherUpdateStatus("available")
    assert tab._update_button_icon() is green_check_icon


def test_update_status_finished_shows_available_update_button(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    logged: list[str] = []
    tab.update_checking = True
    tab._log = logged.append
    tab._set_launcher_buttons_enabled = lambda enabled: None

    tab._update_status_finished(LauncherUpdateStatus("available", commits_behind=3))

    assert tab.update_checking is False
    assert tab.update_button.options["text"] == "Installer oppdatering"
    assert logged == []


def test_update_status_finished_logs_error_and_returns_to_check_button(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    logged: list[str] = []
    tab.update_checking = True
    tab._log = logged.append
    tab._set_launcher_buttons_enabled = lambda enabled: None

    tab._update_status_finished(LauncherUpdateStatus("error", "ingen upstream"))

    assert tab.update_button.options["text"] == "Se etter oppdateringer"
    assert logged == ["Oppdateringssjekk feilet: ingen upstream"]


def test_update_status_finished_logs_skipped_update_check_without_error(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    logged: list[str] = []
    tab.update_checking = True
    tab._log = logged.append
    tab._set_launcher_buttons_enabled = lambda enabled: None

    tab._update_status_finished(LauncherUpdateStatus("skipped", "SSH-remote"))

    assert tab.update_button.options["text"] == "Se etter oppdateringer"
    assert logged == ["Oppdateringssjekk hoppet over: SSH-remote"]


def test_pyproject_includes_launcher_button_icons_as_package_data() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    package_data = pyproject["tool"]["setuptools"]["package-data"]["bildebank"]

    assert "assets/icons/*.png" in package_data


def test_update_button_click_runs_update_only_when_update_is_available(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    actions: list[str] = []
    tab._run_update = lambda: actions.append("update")
    tab.start_update_status_refresh = lambda: actions.append("check")

    tab.update_status = LauncherUpdateStatus("available")
    tab._on_update_button_clicked()
    tab.update_status = LauncherUpdateStatus("current")
    tab._on_update_button_clicked()
    tab.update_status = LauncherUpdateStatus("error")
    tab._on_update_button_clicked()

    assert actions == ["update", "check", "check"]


def test_main_action_buttons_without_collection_keep_update_available(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    for button in (
        tab.start_server_button,
        tab.backup_button,
        tab.snapshot_button,
        tab.snapshot_check_button,
        tab.update_button,
    ):
        button.configure(state="normal")

    with patch("bildebank.launcher_main_tab.is_collection_created", return_value=False):
        tab.set_buttons_enabled(True)

    assert tab.choose_collection_button.options["state"] == "normal"
    assert tab.create_collection_button.options["state"] == "normal"
    assert tab.start_server_button.options["state"] == "disabled"
    assert tab.backup_button.options["state"] == "disabled"
    assert tab.snapshot_button.options["state"] == "disabled"
    assert tab.snapshot_check_button.options["state"] == "normal"
    assert tab.update_button.options["state"] == "normal"


def test_main_action_buttons_with_collection_disable_create(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    for button in (
        tab.start_server_button,
        tab.backup_button,
        tab.snapshot_button,
        tab.snapshot_check_button,
        tab.update_button,
    ):
        button.configure(state="normal")

    with patch("bildebank.launcher_main_tab.is_collection_created", return_value=True):
        tab.set_buttons_enabled(True)

    assert tab.choose_collection_button.options["state"] == "normal"
    assert tab.create_collection_button.options["state"] == "disabled"
    assert tab.start_server_button.options["state"] == "normal"
    assert tab.backup_button.options["state"] == "normal"
    assert tab.snapshot_button.options["state"] == "normal"
    assert tab.snapshot_check_button.options["state"] == "normal"
    assert tab.update_button.options["state"] == "normal"


def test_migration_requirement_disables_collection_controls(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.migration_required = True

    with patch("bildebank.launcher_main_tab.is_collection_created", return_value=True):
        tab.set_buttons_enabled(True)

    assert tab.choose_collection_button.options["state"] == "disabled"
    assert tab.create_collection_button.options["state"] == "disabled"


def test_create_collection_tooltip_explains_disabled_existing_collection(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")

    assert tab._create_collection_tooltip(True) == "Mappen er allerede en bildesamling."
    assert "Lag en bildesamling" in tab._create_collection_tooltip(False)


def test_main_tab_backup_runs_dry_run_before_confirmed_backup(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    backup_parent = tmp_path / "backup"
    tab = bare_main_tab(collection)
    calls: list[tuple[list[str], dict[str, object]]] = []
    questions: list[dict[str, object]] = []
    tab._log = lambda _message: None
    tab._refresh_launcher = lambda: None
    tab._run_waiting_command = lambda command, **options: calls.append((command, options))
    tab._show_log_review_question = (
        lambda _title, _message, **options: questions.append(options)
    )

    with patch("tkinter.filedialog.askdirectory", return_value=str(backup_parent)) as chooser:
        tab._start_backup_flow()

    chooser.assert_called_once_with(
        title="Velg backup-plassering",
        initialdir=str(collection.parent),
    )
    assert calls[0][0][-3:] == ["backup", "--dry-run", str(backup_parent)]
    assert calls[0][1]["cancellable"] is True

    on_dry_run_success = calls[0][1]["on_success"]
    assert callable(on_dry_run_success)
    on_dry_run_success()
    assert questions[0]["yes_text"] == "Kjør backup"

    on_yes = questions[0]["on_yes"]
    assert callable(on_yes)
    on_yes()
    assert "--dry-run" not in calls[1][0]
    assert calls[1][0][-2:] == ["backup", str(backup_parent)]
    assert calls[1][1]["cancellable"] is True


def test_snapshot_plan_log_lines_show_capacity_and_warnings(tmp_path: Path) -> None:
    plan = SimpleNamespace(
        source_dir=tmp_path / "samling",
        repository_dir=tmp_path / "repository",
        repository_state="missing",
        inventory=SimpleNamespace(
            total_files=12,
            total_bytes=2048,
            excluded_files=2,
            excluded_bytes=128,
        ),
        storage=SimpleNamespace(
            estimated_new_objects=10,
            estimated_new_bytes=1920,
            free_bytes=4096,
            has_estimated_capacity=True,
        ),
        warnings=("Dry-run beregner ikke SHA-256.",),
    )

    lines = snapshot_plan_log_lines(plan)

    assert lines[0] == "Plan for versjonert backup:"
    assert any("vil bli opprettet" in line for line in lines)
    assert any("Estimert plass er tilstrekkelig: ja" in line for line in lines)
    assert any("ADVARSEL: Dry-run beregner ikke SHA-256" in line for line in lines)

    recovery_lines = snapshot_plan_log_lines(
        LauncherRecoveryPlan(
            source_dir=tmp_path / "samling",
            repository_dir=tmp_path / "repository",
            database_error="database disk image is malformed",
        )
    )
    assert recovery_lines[0] == "Plan for recovery-snapshot:"
    assert any("database disk image is malformed" in line for line in recovery_lines)


def test_launcher_snapshot_helpers_use_shared_plan_and_create_functions(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    repository = tmp_path / "repository"
    face_config = SimpleNamespace(database_dir=Path(".bildebank-faces"))
    config = SimpleNamespace(face_recognition=face_config)
    expected_plan = object()
    expected_result = object()

    with (
        patch("bildebank.launcher_main_tab.load_config", return_value=config) as load,
        patch("bildebank.launcher_main_tab.program_repo_root", return_value=tmp_path),
        patch("bildebank.launcher_main_tab.plan_snapshot", return_value=expected_plan) as planner,
        patch("bildebank.launcher_main_tab.create_snapshot", return_value=expected_result) as creator,
    ):
        assert plan_launcher_snapshot(collection, repository) is expected_plan
        assert create_launcher_snapshot(collection, repository) is expected_result

    assert load.call_count == 2
    planner.assert_called_once_with(
        collection,
        repository,
        configured_face_database_dir=face_config.database_dir,
    )
    creator.assert_called_once_with(collection, repository, face_config=face_config)


def test_launcher_snapshot_plan_uses_read_only_recovery_preflight_for_damaged_main_database(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "samling"
    repository = tmp_path / "repository"
    config = SimpleNamespace(
        face_recognition=SimpleNamespace(database_dir=Path(".bildebank-faces"))
    )

    with (
        patch("bildebank.launcher_main_tab.load_config", return_value=config),
        patch("bildebank.launcher_main_tab.program_repo_root", return_value=tmp_path),
        patch(
            "bildebank.launcher_main_tab.plan_snapshot",
            side_effect=MainDatabaseSourceError("integrity_check feilet"),
        ),
        patch(
            "bildebank.launcher_main_tab.validate_existing_recovery_repository",
            return_value=repository.resolve(),
        ) as validate_recovery,
    ):
        plan = plan_launcher_snapshot(collection, repository)

    assert isinstance(plan, LauncherRecoveryPlan)
    assert plan.database_error == "integrity_check feilet"
    validate_recovery.assert_called_once_with(collection, repository)


def test_recovery_plan_confirmation_does_not_claim_normal_size_estimate(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    tab = bare_main_tab(tmp_path / "samling")
    logged: list[str] = []
    questions: list[tuple[str, dict[str, object]]] = []
    tab._log = logged.append
    tab._show_log_review_question = (
        lambda _title, message, **options: questions.append((message, options))
    )
    plan = LauncherRecoveryPlan(
        source_dir=tmp_path / "samling",
        repository_dir=repository,
        database_error="integritetsfeil",
    )

    tab._snapshot_plan_finished(repository, plan)

    assert logged[0] == "Plan for recovery-snapshot:"
    assert "normal plassberegning er ikke mulig" in questions[0][0]
    assert "Estimert ny datamengde" not in questions[0][0]
    assert questions[0][1]["yes_text"] == "Opprett snapshot"


def test_main_tab_snapshot_uses_internal_plan_then_internal_create(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    repository = tmp_path / "repository"
    tab = bare_main_tab(collection)
    jobs: list[tuple[object, dict[str, object]]] = []
    questions: list[dict[str, object]] = []
    logged: list[str] = []
    tab._run_background_task = lambda task, **options: jobs.append((task, options))
    tab._show_log_review_question = (
        lambda _title, _message, **options: questions.append(options)
    )
    tab._log = logged.append
    plan = SimpleNamespace(
        source_dir=collection,
        repository_dir=repository,
        repository_state="missing",
        inventory=SimpleNamespace(
            total_files=1,
            total_bytes=100,
            excluded_files=0,
            excluded_bytes=0,
        ),
        storage=SimpleNamespace(
            estimated_new_objects=1,
            estimated_new_bytes=100,
            free_bytes=1000,
            has_estimated_capacity=True,
        ),
        warnings=(),
    )

    with patch("tkinter.filedialog.askdirectory", return_value=str(repository)) as chooser:
        tab._start_snapshot_flow()

    chooser.assert_called_once_with(
        title="Velg mappe for versjonert backup",
        initialdir=str(collection.parent),
        mustexist=False,
    )
    plan_task = jobs[0][0]
    assert callable(plan_task)
    with patch("bildebank.launcher_main_tab.plan_launcher_snapshot", return_value=plan) as planner:
        assert plan_task(lambda: False) is plan
    planner.assert_called_once_with(collection, repository)

    plan_success = jobs[0][1]["on_success"]
    assert callable(plan_success)
    plan_success(plan)
    assert logged[0] == "Plan for versjonert backup:"
    assert questions[0]["yes_text"] == "Opprett snapshot"

    on_yes = questions[0]["on_yes"]
    assert callable(on_yes)
    on_yes()
    create_task = jobs[1][0]
    assert callable(create_task)
    expected_result = object()
    with patch(
        "bildebank.launcher_main_tab.create_launcher_snapshot",
        return_value=expected_result,
    ) as creator:
        assert create_task(lambda: False) is expected_result
    creator.assert_called_once_with(collection, repository)


def test_snapshot_result_distinguishes_complete_degraded_and_recovery(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.root = object()
    tab._log = lambda _message: None
    tab._refresh_launcher = lambda: None
    published = SimpleNamespace(snapshot_id="snapshot-id", snapshot_dir=tmp_path / "snapshot")

    with (
        patch("tkinter.messagebox.showinfo") as showinfo,
        patch("tkinter.messagebox.showwarning") as showwarning,
    ):
        tab._snapshot_creation_finished(
            SimpleNamespace(status="complete", published=published, build=SimpleNamespace(warnings=()))
        )
        tab._snapshot_creation_finished(
            SimpleNamespace(status="degraded", published=published, build=SimpleNamespace(warnings=("avvik",)))
        )
        tab._snapshot_creation_finished(
            SimpleNamespace(status="recovery", published=published, build=SimpleNamespace(warnings=("db-feil",)))
        )

    showinfo.assert_called_once()
    assert showwarning.call_count == 2
    assert showwarning.call_args_list[0].args[0] == "Versjonert backup opprettet med problemer"
    assert showwarning.call_args_list[1].args[0] == "Recovery-snapshot opprettet"


def test_launcher_full_snapshot_check_uses_shared_cancellable_control(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    repository = tmp_path / "repository"
    tab = bare_main_tab(collection)
    questions: list[dict[str, object]] = []
    jobs: list[tuple[object, dict[str, object]]] = []
    tab._log = lambda _message: None
    tab._post_to_ui = lambda callback: (callback(), True)[1]
    tab._show_log_review_question = (
        lambda _title, _message, **options: questions.append(options)
    )
    tab._run_background_task = lambda task, **options: jobs.append((task, options))

    with patch("tkinter.filedialog.askdirectory", return_value=str(repository)) as chooser:
        tab._start_snapshot_check_flow()

    chooser.assert_called_once_with(
        title="Velg versjonert backup som skal kontrolleres",
        initialdir=str(collection.parent),
        mustexist=True,
    )
    assert questions[0]["yes_text"] == "Start full kontroll"
    on_yes = questions[0]["on_yes"]
    assert callable(on_yes)
    on_yes()
    assert jobs[0][1]["cancellable"] is True

    task = jobs[0][0]
    expected = object()

    def cancel_requested() -> bool:
        return False

    assert callable(task)
    with patch(
        "bildebank.launcher_main_tab.check_snapshot_repository",
        return_value=expected,
    ) as check:
        assert task(cancel_requested) is expected
    assert check.call_args.args == (repository,)
    assert check.call_args.kwargs["full"] is True
    assert check.call_args.kwargs["should_cancel"] is cancel_requested
    assert callable(check.call_args.kwargs["progress"])


def test_launcher_snapshot_check_result_distinguishes_success_damage_and_cancel(
    tmp_path: Path,
) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.root = object()
    tab._log = lambda _message: None

    def result(*, cancelled: bool = False, issues: tuple[object, ...] = ()) -> SimpleNamespace:
        return SimpleNamespace(
            cancelled=cancelled,
            repository=tmp_path / "repository",
            checked_objects=3,
            total_objects=3,
            checked_bytes=100,
            issues=issues,
            incomplete_runs=(),
        )

    issue = SimpleNamespace(message="korrupt objekt", affected=())
    with (
        patch("tkinter.messagebox.showinfo") as showinfo,
        patch("tkinter.messagebox.showwarning") as showwarning,
    ):
        tab._snapshot_check_finished(result())
        tab._snapshot_check_finished(result(issues=(issue,)))
        tab._snapshot_check_finished(result(cancelled=True))

    showinfo.assert_called_once()
    assert [call.args[0] for call in showwarning.call_args_list] == [
        "Backupen har integritetsavvik",
        "Kontroll avbrutt",
    ]


def test_start_server_stops_when_migration_is_required(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    actions: list[str] = []
    tab.server_process = None
    tab.update_migration_status = lambda: setattr(tab, "migration_required", True)
    tab._refresh_launcher = lambda: actions.append("refresh")
    tab._show_migration_required_dialog = lambda: actions.append("dialog")

    with patch("bildebank.launcher_main_tab.subprocess.Popen") as popen:
        tab._start_server()

    popen.assert_not_called()
    assert actions == ["refresh", "dialog"]


def test_start_server_uses_advanced_options(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.server_process = None
    tab.update_migration_status = lambda: None
    tab._log = lambda _message: None

    with patch("bildebank.launcher_main_tab.subprocess.Popen") as popen:
        tab.start_server(port=9000, read_only=True)

    assert popen.call_args.args[0][-3:] == ["--port", "9000", "--read-only"]
    assert tab.server_port == 9000


def test_lan_start_cancel_does_not_start_process(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.server_process = None
    tab.update_migration_status = lambda: None
    tab._log = lambda _message: None

    with patch("bildebank.launcher_main_tab.subprocess.Popen") as popen:
        tab.start_server(port=8765, lan_share=True, confirm_lan_start=lambda: False)

    popen.assert_not_called()


def test_lan_start_confirmation_starts_with_lan_share_only(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.server_process = None
    tab.update_migration_status = lambda: None
    tab._log = lambda _message: None

    with patch("bildebank.launcher_main_tab.subprocess.Popen") as popen:
        tab.start_server(port=8765, lan_share=True, confirm_lan_start=lambda: True)

    command = popen.call_args.args[0]
    assert command[-3:] == ["--port", "8765", "--lan-share"]
    assert "--read-only" not in command


def test_running_server_opens_recorded_port_without_confirmation_or_new_process(
    tmp_path: Path,
) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    tab.server_process = SimpleNamespace(poll=lambda: None)
    tab.server_port = 9000
    tab.update_migration_status = lambda: None
    tab._log = lambda _message: None
    confirmations: list[str] = []

    with (
        patch("bildebank.launcher_main_tab.subprocess.Popen") as popen,
        patch("bildebank.launcher_main_tab.open_server_browser_window") as open_browser,
    ):
        tab.start_server(
            port=8766,
            lan_share=True,
            confirm_lan_start=lambda: confirmations.append("confirm") or True,
        )

    popen.assert_not_called()
    open_browser.assert_called_once_with(9000)
    assert confirmations == []


def test_stop_server_process_terminates_running_server(tmp_path: Path) -> None:
    tab = bare_main_tab(tmp_path / "samling")
    logged: list[str] = []
    process = SimpleNamespace(
        poll=lambda: None,
        terminate=lambda: logged.append("terminate"),
        wait=lambda *, timeout: logged.append(f"wait:{timeout}"),
    )
    tab.server_process = process
    tab._log = logged.append

    tab.stop_server_process()

    assert tab.server_process is None
    assert logged == [
        "Stopper Bildebank-server ...",
        "terminate",
        "wait:5",
        "Bildebank-server stoppet.",
    ]
