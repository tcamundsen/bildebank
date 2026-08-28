from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.launcher_status import (
    InsightFaceDependencyStatus,
    InsightFaceModelStatus,
    OpenClipModelStatus,
    RegisteredPerson,
)
from bildebank.launcher_tools_tab import (
    FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP,
    FACE_SCAN_TOOLTIP,
    IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP,
    IMAGE_SCAN_TOOLTIP,
    TOOLS_UNAVAILABLE_MESSAGE,
    ToolsTab,
)


def bare_tools_tab(tmp_path: Path, setup: object) -> ToolsTab:
    tab = ToolsTab.__new__(ToolsTab)
    tab.root = object()
    tab._get_collection_path = lambda: tmp_path / "samling"
    tab._get_setup = lambda: setup
    tab._log = lambda _message: None
    tab._refresh_launcher = lambda: None
    tab._show_error = lambda _message, _exc: None
    return tab


def ready_setup() -> SimpleNamespace:
    return SimpleNamespace(
        insightface_status=InsightFaceDependencyStatus("Klar"),
        face_model_status=InsightFaceModelStatus("buffalo_l", "Lastet ned"),
        openclip_status="Installert",
        openclip_model_status=OpenClipModelStatus("ViT-B-32", "laion", "Tilgjengelig"),
        status_refreshing=False,
    )


class FakeVariable:
    def __init__(self, value: object = None) -> None:
        self.value = value

    def get(self) -> object:
        return self.value


class FakeWidget:
    def __init__(self, parent: FakeWidget | None = None, **options: object) -> None:
        self.parent = parent
        self.options = options
        self.children: list[FakeWidget] = []
        if parent is not None:
            parent.children.append(self)

    def bind(self, *_args: object, **_kwargs: object) -> None:
        pass

    def columnconfigure(self, *_args: object, **_kwargs: object) -> None:
        pass

    def configure(self, **options: object) -> None:
        self.options.update(options)

    def destroy(self) -> None:
        if self.parent is not None:
            self.parent.children.remove(self)

    def grid(self, *_args: object, **_kwargs: object) -> None:
        pass

    def winfo_children(self) -> list[FakeWidget]:
        return list(self.children)


def test_tools_tab_builds_all_buttons_only_when_collection_is_available(tmp_path: Path) -> None:
    notebook = FakeWidget()
    ttk = SimpleNamespace(Frame=FakeWidget, Checkbutton=FakeWidget, Label=FakeWidget)
    tk = SimpleNamespace(BooleanVar=FakeVariable)
    tab = ToolsTab(
        tk=tk,
        ttk=ttk,
        notebook=notebook,
        root=FakeWidget(),
        button=FakeWidget,
        run_waiting_command=lambda *_args, **_kwargs: None,
        get_collection_path=lambda: tmp_path / "samling",
        get_setup=ready_setup,
        log=lambda _message: None,
        refresh_launcher=lambda: None,
        add_tooltip=lambda _widget, _text: None,
        ask_string=lambda *_args, **_kwargs: None,
        show_log_review_question=lambda *_args, **_kwargs: None,
        show_error=lambda _message, _exc: None,
        padding=12,
        padx=4,
        pady=4,
    )

    with (
        patch("bildebank.launcher_tools_tab.list_pending_deletes", return_value=[]),
        patch("bildebank.launcher_tools_tab.active_video_preview_candidates", return_value=[]),
        patch("bildebank.launcher_tools_tab.record_startup_event") as record,
    ):
        buttons = tab.refresh(available=True)

    assert [button.options["text"] for button in buttons] == [
        "Les GPS fra bilder",
        "Finn ansikter",
        "Klargjør bildesøk",
        "Lag miniatyrbilder",
        "Lag HTML-browser",
        "Lag personbrowser",
        "Lag alle personbrowsere",
        'Skjul "Ute av fokus"',
        "Sjekk bildebank",
        "Grundig sjekk",
        "Rydd databaser",
        "Ventende filsletting: OK",
        "Eksporter person",
        "Videoavspilling: OK",
        "Grupper bilder …",
    ]
    assert [call.args[0] for call in record.call_args_list] == [
        "tools_tab_refresh_enter",
        "tools_pending_deletes_status_complete",
        "tools_video_preview_status_complete",
        "tools_tab_widgets_complete",
    ]

    assert tab.refresh(available=False) == []
    assert [child.options.get("text") for child in tab.button_frame.winfo_children()] == [
        TOOLS_UNAVAILABLE_MESSAGE
    ]


def test_dependency_tooltips_explain_missing_ai_components(tmp_path: Path) -> None:
    setup = ready_setup()
    tab = bare_tools_tab(tmp_path, setup)
    tab.face_scan_tooltip = SimpleNamespace(text=FACE_SCAN_TOOLTIP)
    tab.image_scan_tooltip = SimpleNamespace(text=IMAGE_SCAN_TOOLTIP)

    setup.insightface_status = InsightFaceDependencyStatus("Mangler")
    setup.openclip_status = "Mangler"
    tab.update_dependency_tooltips()

    assert tab.face_scan_tooltip.text == FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP
    assert tab.image_scan_tooltip.text == IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP

    setup.insightface_status = InsightFaceDependencyStatus("Klar")
    setup.openclip_status = "Installert"
    tab.update_dependency_tooltips()

    assert tab.face_scan_tooltip.text == FACE_SCAN_TOOLTIP
    assert tab.image_scan_tooltip.text == IMAGE_SCAN_TOOLTIP


def test_ai_tool_buttons_are_disabled_while_setup_status_refreshes(
    tmp_path: Path,
) -> None:
    setup = ready_setup()
    setup.status_refreshing = True
    notebook = FakeWidget()
    tab = ToolsTab(
        tk=SimpleNamespace(BooleanVar=FakeVariable),
        ttk=SimpleNamespace(Frame=FakeWidget, Checkbutton=FakeWidget),
        notebook=notebook,
        root=FakeWidget(),
        button=FakeWidget,
        run_waiting_command=lambda *_args, **_kwargs: None,
        get_collection_path=lambda: tmp_path / "samling",
        get_setup=lambda: setup,
        log=lambda _message: None,
        refresh_launcher=lambda: None,
        add_tooltip=lambda _widget, _text: None,
        ask_string=lambda *_args, **_kwargs: None,
        show_log_review_question=lambda *_args, **_kwargs: None,
        show_error=lambda _message, _exc: None,
        padding=12,
        padx=4,
        pady=4,
    )
    with (
        patch("bildebank.launcher_tools_tab.list_pending_deletes", return_value=[]),
        patch(
            "bildebank.launcher_tools_tab.active_video_preview_candidates",
            return_value=[],
        ),
    ):
        tab.refresh(available=True)

    tab.set_dependency_buttons_enabled(True)

    assert tab.face_scan_button.options["state"] == "disabled"
    assert tab.image_scan_button.options["state"] == "disabled"
    assert tab.clustering_button.options["state"] == "disabled"

    setup.status_refreshing = False
    tab.set_dependency_buttons_enabled(True)
    assert tab.clustering_button.options["state"] == "normal"


def test_clustering_waits_for_setup_status_instead_of_offering_install(
    tmp_path: Path,
) -> None:
    setup = ready_setup()
    setup.status_refreshing = True
    logs: list[str] = []
    tab = bare_tools_tab(tmp_path, setup)
    tab._log = logs.append

    with patch("tkinter.messagebox.askyesno") as ask_yes_no:
        tab._run_image_clustering()

    ask_yes_no.assert_not_called()
    assert logs == ["Venter på kontroll av OpenCLIP-status ..."]


def test_dependency_tooltips_explain_missing_models(tmp_path: Path) -> None:
    setup = ready_setup()
    tab = bare_tools_tab(tmp_path, setup)
    tab.face_scan_tooltip = SimpleNamespace(text=FACE_SCAN_TOOLTIP)
    tab.image_scan_tooltip = SimpleNamespace(text=IMAGE_SCAN_TOOLTIP)
    setup.face_model_status = InsightFaceModelStatus("buffalo_l", "Mangler")
    setup.openclip_model_status = OpenClipModelStatus("ViT-B-32", "laion", "Mangler")

    tab.update_dependency_tooltips()

    assert tab.face_scan_tooltip.text == FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP
    assert tab.image_scan_tooltip.text == IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP


def test_face_scan_preflight_installs_downloads_enables_and_scans(tmp_path: Path) -> None:
    tab = bare_tools_tab(
        tmp_path,
        SimpleNamespace(
            insightface_status=InsightFaceDependencyStatus("Mangler"),
            face_model_status=InsightFaceModelStatus("buffalo_l", "Mangler"),
        ),
    )
    actions: list[str] = []
    tab._face_recognition_enabled = lambda: False
    tab._run_face_scan_insightface_install_step = lambda on_success: (actions.append("install"), on_success())
    tab._run_face_scan_model_download_step = lambda on_success: (actions.append("download"), on_success())
    tab._run_face_scan_enable_step = lambda on_success: (actions.append("enable"), on_success())
    tab._start_face_scan_command = lambda: actions.append("scan")
    tab._log = actions.append

    with (
        patch("tkinter.messagebox.askyesno", return_value=True) as askyesno,
        patch("bildebank.launcher_tools_tab.insightface_install_supported", return_value=True),
    ):
        tab._run_face_scan()

    askyesno.assert_called_once()
    assert actions == ["install", "download", "enable", "scan"]


def test_face_scan_preflight_enables_disabled_config_before_scan(tmp_path: Path) -> None:
    tab = bare_tools_tab(tmp_path, ready_setup())
    actions: list[str] = []
    tab._face_recognition_enabled = lambda: False
    tab._run_face_scan_enable_step = lambda on_success: (actions.append("enable"), on_success())
    tab._start_face_scan_command = lambda: actions.append("scan")
    tab._log = actions.append

    with patch("tkinter.messagebox.askyesno", return_value=True) as askyesno:
        tab._run_face_scan()

    askyesno.assert_called_once()
    assert actions == ["enable", "scan"]


def test_face_scan_enable_step_turns_on_face_recognition(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "bildebank-config.toml").write_text("[face_recognition]\nenabled = false\n", encoding="utf-8")
    tab = bare_tools_tab(tmp_path, ready_setup())
    actions: list[str] = []

    with patch("bildebank.launcher_tools_tab.program_repo_root", return_value=repo_root):
        tab._run_face_scan_enable_step(lambda: actions.append("next"))

    assert actions == ["next"]
    assert "enabled = true" in (repo_root / "bildebank-config.toml").read_text(encoding="utf-8")


def test_face_scan_preflight_can_be_cancelled(tmp_path: Path) -> None:
    tab = bare_tools_tab(tmp_path, ready_setup())
    actions: list[str] = []
    tab._face_recognition_enabled = lambda: False
    tab._start_face_scan_command = lambda: actions.append("scan")
    tab._log = actions.append

    with patch("tkinter.messagebox.askyesno", return_value=False):
        tab._run_face_scan()

    assert actions == ["Ansiktsscan avbrutt."]


def test_image_scan_preflight_installs_enables_and_scans(tmp_path: Path) -> None:
    tab = bare_tools_tab(
        tmp_path,
        SimpleNamespace(
            openclip_status="Mangler",
            openclip_model_status=OpenClipModelStatus("ViT-B-32", "laion", "Mangler"),
        ),
    )
    actions: list[str] = []
    tab._image_search_enabled = lambda: False
    tab._run_image_scan_openclip_install_step = lambda on_success: (actions.append("install"), on_success())
    tab._run_image_scan_openclip_model_download_step = lambda on_success: (actions.append("download"), on_success())
    tab._run_image_scan_enable_step = lambda on_success: (actions.append("enable"), on_success())
    tab._start_image_scan_command = lambda: actions.append("scan")
    tab._log = actions.append

    with (
        patch("tkinter.messagebox.askyesno", return_value=True) as askyesno,
        patch("bildebank.launcher_tools_tab.openclip_install_supported", return_value=True),
    ):
        tab._run_image_scan()

    askyesno.assert_called_once()
    assert actions == ["install", "download", "enable", "scan"]


def test_image_scan_preflight_enables_disabled_config_before_scan(tmp_path: Path) -> None:
    tab = bare_tools_tab(tmp_path, ready_setup())
    actions: list[str] = []
    tab._image_search_enabled = lambda: False
    tab._run_image_scan_enable_step = lambda on_success: (actions.append("enable"), on_success())
    tab._start_image_scan_command = lambda: actions.append("scan")
    tab._log = actions.append

    with patch("tkinter.messagebox.askyesno", return_value=True) as askyesno:
        tab._run_image_scan()

    askyesno.assert_called_once()
    assert actions == ["enable", "scan"]


def test_image_scan_preflight_downloads_only_model_when_dependency_is_ready(
    tmp_path: Path,
) -> None:
    setup = ready_setup()
    setup.openclip_model_status = OpenClipModelStatus(
        "ViT-B-32",
        "laion",
        "Mangler",
        size_bytes=605_143_316,
    )
    tab = bare_tools_tab(tmp_path, setup)
    actions: list[str] = []
    tab._image_search_enabled = lambda: True
    tab._run_image_scan_openclip_install_step = lambda _on_success: actions.append("install")
    tab._run_image_scan_openclip_model_download_step = lambda on_success: (
        actions.append("download"),
        on_success(),
    )
    tab._start_image_scan_command = lambda: actions.append("scan")

    with patch("tkinter.messagebox.askyesno", return_value=True):
        tab._run_image_scan()

    assert actions == ["download", "scan"]


def test_image_scan_enable_step_turns_on_image_search(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "bildebank-config.toml").write_text("[image_search]\nenabled = false\n", encoding="utf-8")
    tab = bare_tools_tab(tmp_path, ready_setup())
    actions: list[str] = []

    with patch("bildebank.launcher_tools_tab.program_repo_root", return_value=repo_root):
        tab._run_image_scan_enable_step(lambda: actions.append("next"))

    assert actions == ["next"]
    assert "enabled = true" in (repo_root / "bildebank-config.toml").read_text(encoding="utf-8")


def test_image_search_enabled_reads_openclip_config_field(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = repo_root / "bildebank-config.toml"
    tab = bare_tools_tab(tmp_path, ready_setup())

    with patch("bildebank.launcher_tools_tab.program_repo_root", return_value=repo_root):
        config_path.write_text("[image_search]\nenabled = true\n", encoding="utf-8")
        assert tab._image_search_enabled()
        config_path.write_text("[image_search]\nenabled = false\n", encoding="utf-8")
        assert not tab._image_search_enabled()


def test_image_scan_preflight_can_be_cancelled(tmp_path: Path) -> None:
    tab = bare_tools_tab(tmp_path, ready_setup())
    actions: list[str] = []
    tab._image_search_enabled = lambda: False
    tab._start_image_scan_command = lambda: actions.append("scan")
    tab._log = actions.append

    with patch("tkinter.messagebox.askyesno", return_value=False):
        tab._run_image_scan()

    assert actions == ["Bildesøk-scan avbrutt."]


def test_thumbnail_generation_offers_safe_cleanup_of_legacy_files(
    tmp_path: Path,
) -> None:
    tab = bare_tools_tab(tmp_path, ready_setup())
    old_thumbnail = (
        tab.collection_path / "thumbs" / "2024" / "01" / "gammel.jpg"
    )
    old_thumbnail.parent.mkdir(parents=True)
    old_thumbnail.write_bytes(b"old thumbnail")
    (
        tab.collection_path / "thumbs" / "2024" / "01" / "gammel-2.jpg"
    ).write_bytes(b"another old thumbnail")
    calls: list[tuple[list[str], dict[str, object]]] = []
    questions: list[tuple[str, str, dict[str, object]]] = []
    tab._run_waiting_command = (
        lambda command, **options: calls.append((command, options))
    )
    tab._show_log_review_question = (
        lambda title, message, **options: questions.append(
            (title, message, options)
        )
    )

    tab._run_make_thumbnails()

    assert calls[0][0][-1] == "make-thumbnails"
    on_generation_success = calls[0][1]["on_success"]
    assert callable(on_generation_success)
    on_generation_success()

    title, message, options = questions[0]
    assert title == "Slett gamle miniatyrbilder?"
    assert "2 gamle miniatyrbilder" in message
    assert "bruker dem ikke lenger" in message
    assert "kommer ikke til å bli brukt igjen" in message
    assert "trygt å slette dem" in message
    assert r"thumbs\v2" in message
    assert "originalbildene dine blir ikke berørt" in message
    assert options["yes_text"] == "Slett gamle miniatyrbilder"
    assert options["no_text"] == "Behold dem"

    on_yes = options["on_yes"]
    assert callable(on_yes)
    on_yes()

    assert calls[1][0][-4:] == [
        "--target",
        str(tab.collection_path),
        "cleanup-thumbnails",
        "--apply",
    ]


def test_thumbnail_generation_does_not_prompt_without_legacy_files(
    tmp_path: Path,
) -> None:
    tab = bare_tools_tab(tmp_path, ready_setup())
    (tab.collection_path / "thumbs" / "v2").mkdir(parents=True)
    questions: list[object] = []
    refreshed: list[bool] = []
    tab._refresh_launcher = lambda: refreshed.append(True)
    tab._show_log_review_question = lambda *_args, **_kwargs: questions.append(
        True
    )

    tab._thumbnail_generation_finished()

    assert refreshed == [True]
    assert questions == []


def test_export_person_runs_dry_run_before_confirmed_export(tmp_path: Path) -> None:
    tab = bare_tools_tab(tmp_path, ready_setup())
    person = RegisteredPerson("Kari", 3, 4, 1, "2026-07-13")
    destination = tmp_path / "eksport"
    selections: list[dict[str, object]] = []
    calls: list[tuple[list[str], dict[str, object]]] = []
    questions: list[dict[str, object]] = []
    tab._load_registered_persons = lambda: [person]
    tab._select_person = lambda _persons, **options: selections.append(options)
    tab._run_waiting_command = lambda command, **options: calls.append((command, options))
    tab._show_log_review_question = (
        lambda _title, _message, **options: questions.append(options)
    )

    tab._start_export_person_flow()

    assert "eksporterer en kopi" in str(selections[0]["description"])
    assert selections[0]["action_label"] == "Velg mappe"

    tab._run_export_person_dry_run(person, destination)
    assert calls[0][0][-5:] == ["export-person", "Kari", "--dest", str(destination), "--dry-run"]

    on_dry_run_success = calls[0][1]["on_success"]
    assert callable(on_dry_run_success)
    on_dry_run_success()
    assert questions[0]["yes_text"] == "Eksporter"

    on_yes = questions[0]["on_yes"]
    assert callable(on_yes)
    on_yes()
    assert calls[1][0][-4:] == ["export-person", "Kari", "--dest", str(destination)]
    assert calls[1][1]["cancellable"] is True


def test_static_browser_commands_share_hide_out_of_focus_option(tmp_path: Path) -> None:
    tab = bare_tools_tab(tmp_path, ready_setup())
    person = RegisteredPerson("Kari", 3, 4, 1, "2026-07-13")
    calls: list[tuple[list[str], dict[str, object]]] = []
    selections: list[dict[str, object]] = []
    tab.static_browser_hide_out_of_focus_var = FakeVariable(True)
    tab._run_waiting_command = lambda command, **options: calls.append((command, options))
    tab._load_registered_persons = lambda: [person]
    tab._select_person = lambda _persons, **options: selections.append(options)

    tab._run_make_browser()
    tab._run_make_person_browser(person)
    tab._run_make_people_browser()
    tab._start_make_person_browser_flow()

    assert [call[0][-1] for call in calls] == [
        "--hide-out-of-focus",
        "--hide-out-of-focus",
        "--hide-out-of-focus",
    ]
    assert all(call[1]["cancellable"] is True for call in calls)
    assert "Kari" in calls[1][0]
    assert selections[0]["description"] == (
        "Velg personen det skal lages statisk HTML-browser for."
    )


def test_face_and_image_scan_commands_are_cancellable(tmp_path: Path) -> None:
    tab = bare_tools_tab(tmp_path, ready_setup())
    calls: list[tuple[list[str], dict[str, object]]] = []
    tab._run_waiting_command = lambda command, **options: calls.append((command, options))

    tab._start_face_scan_command()
    tab._start_image_scan_command()

    assert calls[0][0][-1] == "face-scan"
    assert calls[1][0][-1] == "image-scan"
    assert all(call[1]["cancellable"] is True for call in calls)


def test_pending_delete_cleanup_requires_exact_confirmation(tmp_path: Path) -> None:
    tab = bare_tools_tab(tmp_path, ready_setup())
    actions: list[str] = []
    tab._ask_string = lambda *_args, **_kwargs: "ja"
    tab._run_cleanup_pending_deletes = lambda: actions.append("cleanup")

    tab._confirm_cleanup_pending_deletes()

    assert actions == []
