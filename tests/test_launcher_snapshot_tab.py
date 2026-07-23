from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank import db
from bildebank.launcher_snapshot_tab import (
    LauncherRecoveryPlan,
    SnapshotTab,
    browse_path_parts,
    browse_snapshot_media,
    create_launcher_snapshot,
    default_download_export_directory,
    plan_launcher_snapshot,
    plan_single_file_choices,
    snapshot_dialog_initial_directory,
    snapshot_plan_log_lines,
)
from bildebank.program_state import KnownSnapshotRepository
from bildebank.snapshot import (
    MainDatabaseSourceError,
    REPOSITORY_LOCK_FILENAME,
    RepositoryBindingChange,
)
from bildebank.snapshot_create import create_snapshot
from bildebank.snapshot_progress import SnapshotCancelled, SnapshotPlanProgress
from bildebank.snapshot_repository import ObjectReference, SnapshotStorageError
from bildebank.snapshot_restore import RestoreEntry
from tests.test_snapshot_restore import degraded_snapshot, tree_file_bytes


class FakeButton:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def configure(self, **kwargs: object) -> None:
        self.options.update(kwargs)


class FakeStringVar:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


def bare_snapshot_tab(collection_path: Path, repository: Path | None = None) -> SnapshotTab:
    tab = SnapshotTab.__new__(SnapshotTab)
    tab._get_collection_path = lambda: collection_path
    tab.selected_repository = repository
    tab.repository_collection = collection_path
    tab.repository_value = FakeStringVar()
    tab.choose_repository_button = FakeButton()
    tab.create_button = FakeButton()
    tab.check_button = FakeButton()
    tab.restore_file_button = FakeButton()
    tab.create_available = True
    tab.root = object()
    tab._log_progress = lambda _message: None
    return tab


def restore_entry(
    path: str | None,
    *,
    entry_id: str = "e-000000000001",
    restore_kind: str = "normal",
    record_type: str = "file",
    integrity_status: str = "ok",
    expected: ObjectReference | None = None,
    observed: ObjectReference | None = None,
) -> RestoreEntry:
    return RestoreEntry(
        entry_id=entry_id,
        path=path,
        original_path_display=path or "utrygg sti",
        recovery_name=None if restore_kind == "normal" else f"{entry_id}.bin",
        restore_kind=restore_kind,
        integrity_status=integrity_status,
        object=observed,
        expected=expected,
        mtime_ns=123,
        record_type=record_type,
    )


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

    assert lines[0] == "Plan for snapshot:"
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
    progress_events: list[SnapshotPlanProgress] = []

    def cancel_requested() -> bool:
        return False

    with (
        patch("bildebank.launcher_snapshot_tab.load_config", return_value=config) as load,
        patch("bildebank.launcher_snapshot_tab.program_repo_root", return_value=tmp_path),
        patch("bildebank.launcher_snapshot_tab.plan_snapshot", return_value=expected_plan) as planner,
        patch("bildebank.launcher_snapshot_tab.create_snapshot", return_value=expected_result) as creator,
    ):
        assert plan_launcher_snapshot(collection, repository) is expected_plan
        assert (
            plan_launcher_snapshot(
                collection,
                repository,
                progress=progress_events.append,
                should_cancel=cancel_requested,
            )
            is expected_plan
        )
        assert create_launcher_snapshot(collection, repository) is expected_result

    assert load.call_count == 3
    assert planner.call_args_list[0].args == (collection, repository)
    planner.assert_called_with(
        collection,
        repository,
        configured_face_database_dir=face_config.database_dir,
        progress=progress_events.append,
        should_cancel=cancel_requested,
    )
    creator.assert_called_once_with(
        collection,
        repository,
        face_config=face_config,
        confirmed_binding_change=None,
        progress=None,
        should_cancel=None,
    )


def test_launcher_snapshot_plan_uses_read_only_recovery_preflight(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    repository = tmp_path / "repository"
    config = SimpleNamespace(
        face_recognition=SimpleNamespace(database_dir=Path(".bildebank-faces"))
    )

    with (
        patch("bildebank.launcher_snapshot_tab.load_config", return_value=config),
        patch("bildebank.launcher_snapshot_tab.program_repo_root", return_value=tmp_path),
        patch(
            "bildebank.launcher_snapshot_tab.plan_snapshot",
            side_effect=MainDatabaseSourceError("integrity_check feilet"),
        ),
        patch(
            "bildebank.launcher_snapshot_tab.validate_existing_recovery_repository",
            return_value=repository.resolve(),
        ) as validate_recovery,
    ):
        plan = plan_launcher_snapshot(collection, repository)

    assert isinstance(plan, LauncherRecoveryPlan)
    assert plan.database_error == "integrity_check feilet"
    validate_recovery.assert_called_once_with(collection, repository)


def test_launcher_snapshot_plan_accepts_missing_main_database_for_bound_repository(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "samling"
    repository = tmp_path / "repository"
    db.init_database(collection)
    create_snapshot(collection, repository)
    (collection / db.DB_FILENAME).unlink()
    repository_before = tree_file_bytes(repository)
    config = SimpleNamespace(
        face_recognition=SimpleNamespace(database_dir=Path(".bildebank-faces"))
    )

    with (
        patch("bildebank.launcher_snapshot_tab.load_config", return_value=config),
        patch(
            "bildebank.launcher_snapshot_tab.program_repo_root",
            return_value=tmp_path,
        ),
    ):
        plan = plan_launcher_snapshot(collection, repository)

    assert isinstance(plan, LauncherRecoveryPlan)
    assert "hoveddatabasen" in plan.database_error.lower()
    assert tree_file_bytes(repository) == repository_before


def test_snapshot_tab_buttons_require_repository_and_collection_for_create(tmp_path: Path) -> None:
    tab = bare_snapshot_tab(tmp_path / "samling")

    tab.set_buttons_enabled(True)

    assert tab.choose_repository_button.options["state"] == "normal"
    assert tab.create_button.options["state"] == "disabled"
    assert tab.check_button.options["state"] == "disabled"
    assert tab.restore_file_button.options["state"] == "disabled"

    tab.selected_repository = tmp_path / "repository"
    tab.create_available = False
    tab.set_buttons_enabled(True)

    assert tab.create_button.options["state"] == "disabled"
    assert tab.check_button.options["state"] == "normal"
    assert tab.restore_file_button.options["state"] == "normal"

    tab.create_available = True
    tab.set_buttons_enabled(True)
    assert tab.create_button.options["state"] == "normal"


def test_snapshot_dialog_uses_latest_available_repository(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    repository = tmp_path / "usb" / "Familiebilder"
    repository.mkdir(parents=True)
    latest = KnownSnapshotRepository(
        collection_id="collection-id",
        repository_id="repository-id",
        path=repository,
        last_snapshot_id="snapshot-id",
        last_snapshot_status="complete",
        last_snapshot_at="2026-07-19T12:00:00Z",
    )

    with patch(
        "bildebank.launcher_snapshot_tab.known_snapshot_repositories_for_target",
        return_value=[latest],
    ):
        assert snapshot_dialog_initial_directory(collection) == repository


def test_snapshot_dialog_uses_newest_available_and_falls_back(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    available = tmp_path / "tilkoblet-disk" / "Familiebilder"
    available.mkdir(parents=True)
    repositories = [
        KnownSnapshotRepository(
            collection_id="collection-id",
            repository_id="nyeste-id",
            path=tmp_path / "frakoblet-disk" / "Familiebilder",
            last_snapshot_id="nyeste-snapshot",
            last_snapshot_status="complete",
            last_snapshot_at="2026-07-19T12:00:00Z",
        ),
        KnownSnapshotRepository(
            collection_id="collection-id",
            repository_id="eldre-id",
            path=available,
            last_snapshot_id="eldre-snapshot",
            last_snapshot_status="complete",
            last_snapshot_at="2026-07-18T12:00:00Z",
        ),
    ]

    with patch(
        "bildebank.launcher_snapshot_tab.known_snapshot_repositories_for_target",
        return_value=repositories,
    ):
        assert snapshot_dialog_initial_directory(collection) == available

    with patch(
        "bildebank.launcher_snapshot_tab.known_snapshot_repositories_for_target",
        return_value=repositories[:1],
    ):
        assert snapshot_dialog_initial_directory(collection) == collection.parent


def test_snapshot_tab_uses_selected_repository_for_create_and_check(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    repository = tmp_path / "repository"
    tab = bare_snapshot_tab(collection, repository)
    jobs: list[tuple[object, dict[str, object]]] = []
    questions: list[dict[str, object]] = []
    progress_messages: list[str] = []
    logged: list[str] = []
    tab._run_background_task = lambda task, **options: jobs.append((task, options))
    tab._show_log_review_question = (
        lambda _title, _message, **options: questions.append(options)
    )
    tab._log = logged.append
    tab._log_progress = progress_messages.append
    tab._post_to_ui = lambda callback: (callback(), True)[1]
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

    tab._start_snapshot_flow()
    plan_task = jobs[0][0]
    assert callable(plan_task)
    assert jobs[0][1]["cancellable"] is True

    with patch("bildebank.launcher_snapshot_tab.plan_launcher_snapshot", return_value=plan) as planner:
        assert plan_task(lambda: False) is plan
    assert planner.call_args.args == (collection, repository)
    progress = planner.call_args.kwargs["progress"]
    progress(SnapshotPlanProgress(stage="database"))
    assert progress_messages == ["Snapshot dry-run: leser og kontrollerer hoveddatabasen ..."]

    success = jobs[0][1]["on_success"]
    success(plan)
    assert questions[0]["yes_text"] == "Opprett snapshot"

    tab._start_snapshot_check_flow()
    assert questions[1]["yes_text"] == "Start full kontroll"
    questions[1]["on_yes"]()
    check_task = jobs[1][0]
    expected = object()
    with patch(
        "bildebank.launcher_snapshot_tab.check_snapshot_repository",
        return_value=expected,
    ) as check:
        assert check_task(lambda: False) is expected
    assert check.call_args.args == (repository,)
    assert check.call_args.kwargs["full"] is True


def test_launcher_requires_specific_confirmation_for_moved_collection(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "flyttet-samling"
    previous_collection = tmp_path / "opprinnelig-samling"
    repository = tmp_path / "repository"
    binding_change = RepositoryBindingChange(
        previous_collection_path=str(previous_collection),
        current_collection_path=str(collection),
        previous_machine_name="GAMMEL-PC",
        current_machine_name="NY-PC",
    )
    tab = bare_snapshot_tab(collection, repository)
    jobs: list[tuple[object, dict[str, object]]] = []
    questions: list[dict[str, object]] = []
    tab._run_background_task = lambda task, **options: jobs.append((task, options))
    tab._show_log_review_question = (
        lambda title, message, **options: questions.append(
            {"title": title, "message": message, **options}
        )
    )
    tab._log = lambda _message: None
    tab._post_to_ui = lambda callback: (callback(), True)[1]
    plan = SimpleNamespace(
        source_dir=collection,
        repository_dir=repository,
        repository_state="existing",
        binding_change=binding_change,
        inventory=SimpleNamespace(
            total_files=1,
            total_bytes=100,
            excluded_files=0,
            excluded_bytes=0,
        ),
        storage=SimpleNamespace(
            estimated_new_objects=0,
            estimated_new_bytes=0,
            free_bytes=1000,
            has_estimated_capacity=True,
        ),
        warnings=(),
    )

    tab._snapshot_plan_task_finished(repository, plan)

    question = questions[0]
    self_message = str(question["message"])
    assert question["title"] == "Bekreft flytting av bildesamlingen"
    assert question["yes_text"] == "Bekreft flytting og opprett"
    assert str(previous_collection) in self_message
    assert str(collection) in self_message
    assert "to uavhengige kopier" in self_message

    question["on_yes"]()
    create_task = jobs[0][0]
    expected_result = object()
    with patch(
        "bildebank.launcher_snapshot_tab.create_launcher_snapshot",
        return_value=expected_result,
    ) as create:
        assert create_task(lambda: False) is expected_result
    create.assert_called_once_with(
        collection,
        repository,
        confirmed_binding_change=binding_change,
        progress=create.call_args.kwargs["progress"],
        should_cancel=create.call_args.kwargs["should_cancel"],
    )


def test_launcher_reports_controlled_snapshot_cancellation(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    tab = bare_snapshot_tab(tmp_path / "samling", repository)
    logged: list[str] = []
    refreshed: list[bool] = []
    tab._log = logged.append
    tab._refresh_launcher = lambda: refreshed.append(True)

    tab._snapshot_plan_task_finished(repository, None)
    with patch("tkinter.messagebox.showinfo") as showinfo:
        tab._snapshot_creation_task_finished(repository, None)

    assert "Ingen endringer ble gjort" in logged[0]
    assert "Ingen nytt snapshot ble publisert" in logged[1]
    assert str(repository / "incomplete") in logged[1]
    showinfo.assert_called_once()
    assert refreshed == [True]


def test_create_task_treats_snapshot_cancel_as_controlled(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    tab = bare_snapshot_tab(tmp_path / "samling", repository)
    jobs: list[tuple[object, dict[str, object]]] = []
    tab._run_background_task = lambda task, **options: jobs.append((task, options))
    tab._post_to_ui = lambda callback: True

    tab._run_snapshot_create(repository)
    task = jobs[0][0]
    with patch(
        "bildebank.launcher_snapshot_tab.create_launcher_snapshot",
        side_effect=SnapshotCancelled("avbrutt"),
    ):
        assert task(lambda: True) is None


def test_browse_paths_include_year_undated_and_deleted_only() -> None:
    assert browse_path_parts(restore_entry("2024/01/IMG.jpg")) == (
        "2024",
        "01",
        "IMG.jpg",
    )
    assert browse_path_parts(restore_entry("udatert/video.mp4")) == (
        "udatert",
        "video.mp4",
    )
    assert browse_path_parts(restore_entry("deleted/2023/12/gammel.jpg")) == (
        "deleted",
        "2023",
        "12",
        "gammel.jpg",
    )
    assert browse_path_parts(restore_entry("deleted/udatert/gammel.mov")) == (
        "deleted",
        "udatert",
        "gammel.mov",
    )
    assert browse_path_parts(restore_entry("notater/familie.txt")) is None
    assert browse_path_parts(restore_entry(".bilder.sqlite3")) is None
    assert browse_path_parts(
        restore_entry(None, restore_kind="recovery_only")
    ) is None


def test_browse_snapshot_uses_locked_validated_entries(tmp_path: Path) -> None:
    summary = SimpleNamespace(snapshot_id="snapshot-id")
    entries = (
        restore_entry("2023/01/eldre.jpg", entry_id="e-000000000001"),
        restore_entry("2025/02/nyere.jpg", entry_id="e-000000000002"),
        restore_entry("notater/familie.txt", entry_id="e-000000000003"),
    )
    loaded = SimpleNamespace(summary=summary, entries=entries)

    class Manager:
        def __enter__(self) -> SimpleNamespace:
            return loaded

        def __exit__(self, *_args: object) -> None:
            pass

    manager = Manager()

    with patch("bildebank.launcher_snapshot_tab.locked_snapshot", return_value=manager) as lock:
        result = browse_snapshot_media(tmp_path / "repository", "snapshot-id")

    lock.assert_called_once_with(
        tmp_path / "repository",
        "snapshot-id",
        command="launcher snapshot browse",
    )
    assert [entry.path for entry in result.entries] == [
        "2025/02/nyere.jpg",
        "2023/01/eldre.jpg",
    ]


def test_single_file_plan_choices_offer_expected_and_observed(tmp_path: Path) -> None:
    expected = ObjectReference("a" * 64, 10)
    observed = ObjectReference("b" * 64, 11)
    entry = restore_entry(
        "2024/01/IMG.jpg",
        expected=expected,
        observed=observed,
        integrity_status="hash_mismatch",
    )
    expected_plan = SimpleNamespace(output=SimpleNamespace(variant="expected"))
    observed_plan = SimpleNamespace(output=SimpleNamespace(variant="observed"))

    with patch(
        "bildebank.launcher_snapshot_tab.plan_single_file_restore",
        side_effect=[expected_plan, observed_plan],
    ) as planner:
        choices = plan_single_file_choices(
            tmp_path / "repository",
            "snapshot-id",
            tmp_path / "export",
            entry,
        )

    assert choices.plans == (expected_plan, observed_plan)
    assert [call.kwargs["variant"] for call in planner.call_args_list] == [
        "expected",
        "observed",
    ]


def test_real_degraded_snapshot_can_be_browsed_and_planned_read_only() -> None:
    with degraded_snapshot() as (root, _target, repository, snapshot_id, _expected, _observed):
        repository_before = tree_file_bytes(repository)

        browse = browse_snapshot_media(repository, snapshot_id)
        entry = browse.entries[0]
        (root / "Downloads").mkdir()
        choices = plan_single_file_choices(
            repository,
            snapshot_id,
            root / "Downloads" / "Bildebank-gjenopprettet",
            entry,
        )

        assert entry.path == "2026/07/familie.jpg"
        assert {plan.output.variant for plan in choices.plans} == {"expected", "observed"}
        assert tree_file_bytes(repository) == repository_before
        assert not (repository / REPOSITORY_LOCK_FILENAME).exists()


def test_single_file_plan_choices_keep_available_variant(tmp_path: Path) -> None:
    entry = restore_entry(
        "2024/01/IMG.jpg",
        expected=ObjectReference("a" * 64, 10),
        observed=ObjectReference("b" * 64, 11),
        integrity_status="hash_mismatch",
    )
    observed_plan = SimpleNamespace(output=SimpleNamespace(variant="observed"))

    with patch(
        "bildebank.launcher_snapshot_tab.plan_single_file_restore",
        side_effect=[SnapshotStorageError("mangler"), observed_plan],
    ):
        choices = plan_single_file_choices(
            tmp_path / "repository",
            "snapshot-id",
            tmp_path / "export",
            entry,
        )

    assert choices.plans == (observed_plan,)
    assert choices.errors and "expected" in choices.errors[0]


def test_default_export_directory_prefers_downloads(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    with patch("bildebank.launcher_snapshot_tab.Path.home", return_value=tmp_path):
        assert default_download_export_directory() == downloads / "Bildebank-gjenopprettet"


def test_file_restore_runs_existing_core_without_cancellation(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    tab = bare_snapshot_tab(tmp_path / "samling", repository)
    jobs: list[tuple[object, dict[str, object]]] = []
    tab._run_background_task = lambda task, **options: jobs.append((task, options))
    entry = restore_entry("deleted/2024/01/IMG.jpg")
    plan = SimpleNamespace(
        snapshot=SimpleNamespace(snapshot_id="snapshot-id"),
        export_directory=tmp_path / "Downloads" / "Bildebank-gjenopprettet",
        output=SimpleNamespace(variant="expected"),
    )
    expected_result = object()

    tab._run_file_restore(repository, entry, plan)

    task, options = jobs[0]
    assert options.get("cancellable", False) is False
    with patch(
        "bildebank.launcher_snapshot_tab.restore_single_file",
        return_value=expected_result,
    ) as restore:
        assert task(lambda: False) is expected_result
    restore.assert_called_once_with(
        repository,
        "snapshot-id",
        plan.export_directory,
        path="deleted/2024/01/IMG.jpg",
        variant="expected",
    )
