from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.launcher_status import (
    InsightFaceDependencyStatus,
    InsightFaceModelStatus,
    OpenClipModelStatus,
)
from bildebank.launcher_tools_tab import (
    FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP,
    FACE_SCAN_TOOLTIP,
    IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP,
    IMAGE_SCAN_TOOLTIP,
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

    def destroy(self) -> None:
        if self.parent is not None:
            self.parent.children.remove(self)

    def grid(self, *_args: object, **_kwargs: object) -> None:
        pass

    def winfo_children(self) -> list[FakeWidget]:
        return list(self.children)


def test_tools_tab_builds_all_buttons_only_when_collection_is_available(tmp_path: Path) -> None:
    notebook = FakeWidget()
    ttk = SimpleNamespace(Frame=FakeWidget, Checkbutton=FakeWidget)
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

    with patch("bildebank.launcher_tools_tab.list_pending_deletes", return_value=[]):
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
    ]

    assert tab.refresh(available=False) == []
    assert tab.button_frame.winfo_children() == []


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
    tab._run_image_scan_enable_step = lambda on_success: (actions.append("enable"), on_success())
    tab._start_image_scan_command = lambda: actions.append("scan")
    tab._log = actions.append

    with (
        patch("tkinter.messagebox.askyesno", return_value=True) as askyesno,
        patch("bildebank.launcher_tools_tab.openclip_install_supported", return_value=True),
    ):
        tab._run_image_scan()

    askyesno.assert_called_once()
    assert actions == ["install", "enable", "scan"]


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


def test_tools_tab_exposes_export_person_dry_run_flow() -> None:
    refresh_source = inspect.getsource(ToolsTab.refresh)
    flow_source = inspect.getsource(ToolsTab._start_export_person_flow)
    dry_run_source = inspect.getsource(ToolsTab._run_export_person_dry_run)
    confirm_source = inspect.getsource(ToolsTab._confirm_export_person)

    assert 'text="Eksporter person"' in refresh_source
    assert "_start_export_person_flow" in refresh_source
    assert "Denne funksjonen eksporterer en kopi av alle bildene av en person" in flow_source
    assert "description: str" in inspect.getsource(ToolsTab._select_person)
    assert "dry_run=True" in dry_run_source
    assert "_confirm_export_person" in dry_run_source
    assert "_show_log_review_question" in confirm_source
    assert "_run_export_person(person, destination_root)" in confirm_source


def test_tools_tab_exposes_static_browser_commands_and_shared_hide_checkbox() -> None:
    refresh_source = inspect.getsource(ToolsTab.refresh)
    make_browser_source = inspect.getsource(ToolsTab._run_make_browser)
    start_make_person_source = inspect.getsource(ToolsTab._start_make_person_browser_flow)
    make_person_source = inspect.getsource(ToolsTab._run_make_person_browser)
    make_people_source = inspect.getsource(ToolsTab._run_make_people_browser)

    assert 'text="Lag HTML-browser"' in refresh_source
    assert 'text="Lag personbrowser"' in refresh_source
    assert 'text="Lag alle personbrowsere"' in refresh_source
    assert 'text=\'Skjul "Ute av fokus"\'' in refresh_source
    assert "static_browser_hide_out_of_focus_var" in refresh_source
    assert "de statiske HTML-browserkommandoene" in refresh_source
    assert "Velg personen det skal lages statisk HTML-browser for." in start_make_person_source
    for source_code in (make_browser_source, make_person_source, make_people_source):
        assert "static_browser_hide_out_of_focus_var.get()" in source_code
        assert "hide_out_of_focus=hide_out_of_focus" in source_code


def test_face_and_image_scan_commands_are_cancellable() -> None:
    assert "cancellable=True" in inspect.getsource(ToolsTab._start_face_scan_command)
    assert "cancellable=True" in inspect.getsource(ToolsTab._start_image_scan_command)


def test_pending_delete_cleanup_requires_exact_confirmation(tmp_path: Path) -> None:
    tab = bare_tools_tab(tmp_path, ready_setup())
    actions: list[str] = []
    tab._ask_string = lambda *_args, **_kwargs: "ja"
    tab._run_cleanup_pending_deletes = lambda: actions.append("cleanup")

    tab._confirm_cleanup_pending_deletes()

    assert actions == []
