from __future__ import annotations

import inspect
from unittest.mock import patch

from bildebank.launcher_app import LauncherApp
from bildebank.launcher_setup_tab import SetupTab
from bildebank.launcher_status import (
    InsightFaceDependencyStatus,
    InsightFaceModelStatus,
    OpenClipModelStatus,
)


class FakeValue:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class FakeButton:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def configure(self, **kwargs: object) -> None:
        self.options.update(kwargs)


def bare_setup_tab() -> SetupTab:
    setup = SetupTab.__new__(SetupTab)
    setup.insightface_status = InsightFaceDependencyStatus("Klar")
    setup.face_model_status = InsightFaceModelStatus("buffalo_l", "Lastet ned")
    setup.openclip_status = "Installert"
    setup.openclip_model_status = OpenClipModelStatus("ViT-B-32", "laion", "Tilgjengelig")
    setup.status_refreshing = False
    setup.insightface_status_value = FakeValue()
    setup.insightface_model_status_value = FakeValue()
    setup.openclip_status_value = FakeValue()
    setup.openclip_model_status_value = FakeValue()
    setup.install_insightface_button = None
    setup.install_openclip_button = None
    setup.download_face_model_button = None
    setup._log = lambda _message: None
    setup._on_status_changed = lambda: None
    return setup


def test_setup_tab_builds_insightface_and_openclip_sections() -> None:
    source = inspect.getsource(SetupTab._build)

    assert "insightface_frame = ttk.Frame(self.frame)" in source
    assert 'text="Installer InsightFace"' in source
    assert 'text="Last ned modell"' in source
    assert 'ttk.Separator(self.frame, orient="horizontal")' in source
    assert "openclip_frame = ttk.Frame(self.frame)" in source
    assert 'text="Installer OpenCLIP"' in source


def test_launcher_starts_setup_status_asynchronously() -> None:
    init_source = inspect.getsource(LauncherApp.__init__)
    refresh_source = inspect.getsource(LauncherApp._refresh_state)
    start_source = inspect.getsource(SetupTab.start_status_refresh)
    worker_source = inspect.getsource(SetupTab._status_worker)

    assert "self.setup.start_status_refresh()" in init_source
    assert "insightface_dependency_status()" not in init_source
    assert "openclip_model_status()" not in init_source
    assert "insightface_dependency_status()" not in refresh_source
    assert "openclip_model_status()" not in refresh_source
    assert "threading.Thread" in start_source
    assert "self._post_to_ui(" in worker_source


def test_status_finished_updates_values_and_logs_error_details() -> None:
    setup = bare_setup_tab()
    logged: list[str] = []
    changes: list[str] = []
    setup.status_refreshing = True
    setup._log = logged.append
    setup._on_status_changed = lambda: changes.append("changed")

    setup._status_finished(
        InsightFaceDependencyStatus("Feil", "runtime-feil"),
        InsightFaceModelStatus("buffalo_l", "Mangler", "forventet mangler"),
        "Feil: open_clip-feil",
        OpenClipModelStatus("ViT-B-32", "laion", "Feil", "modell-feil"),
    )

    assert not setup.status_refreshing
    assert setup.insightface_status_value.value == "InsightFace: Feil"
    assert setup.openclip_model_status_value.value == "AI-modell: Feil"
    assert logged == [
        "InsightFace-status feilet: runtime-feil",
        "OpenCLIP-modell-status feilet: modell-feil",
    ]
    assert changes == ["changed"]


def test_load_status_calls_all_dependency_status_functions() -> None:
    setup = bare_setup_tab()
    expected_openclip_model_status = OpenClipModelStatus("ViT-B-32", "laion", "Tilgjengelig", "modellmappe")

    with (
        patch(
            "bildebank.launcher_setup_tab.insightface_dependency_status",
            return_value=InsightFaceDependencyStatus("Klar"),
        ),
        patch(
            "bildebank.launcher_setup_tab.insightface_model_status",
            return_value=InsightFaceModelStatus("buffalo_l", "Lastet ned"),
        ),
        patch("bildebank.launcher_setup_tab.openclip_dependency_status", return_value="Installert"),
        patch(
            "bildebank.launcher_setup_tab.openclip_model_status",
            return_value=expected_openclip_model_status,
        ),
    ):
        status = setup._load_status()

    assert status == (
        InsightFaceDependencyStatus("Klar"),
        InsightFaceModelStatus("buffalo_l", "Lastet ned"),
        "Installert",
        expected_openclip_model_status,
    )


def test_openclip_install_finish_refreshes_status_before_callback() -> None:
    setup = bare_setup_tab()
    actions: list[str] = []
    setup._on_status_changed = lambda: actions.append("status")

    with (
        patch("bildebank.launcher_setup_tab.importlib.invalidate_caches"),
        patch("bildebank.launcher_setup_tab.openclip_dependency_status", return_value="Installert"),
        patch(
            "bildebank.launcher_setup_tab.openclip_model_status",
            return_value=OpenClipModelStatus("ViT-B-32", "laion", "Tilgjengelig"),
        ),
    ):
        setup._openclip_install_finished(lambda: actions.append("next"))

    assert setup.openclip_status == "Installert"
    assert setup.openclip_model_status.status == "Tilgjengelig"
    assert actions == ["status", "next"]


def test_setup_buttons_are_disabled_while_status_refreshes() -> None:
    setup = bare_setup_tab()
    setup.status_refreshing = True
    setup.install_insightface_button = FakeButton()
    setup.install_openclip_button = FakeButton()
    setup.download_face_model_button = FakeButton()

    with (
        patch("bildebank.launcher_setup_tab.insightface_install_supported", return_value=True),
        patch("bildebank.launcher_setup_tab.openclip_install_supported", return_value=True),
    ):
        setup.set_buttons_enabled(
            True,
            migration_required=False,
            migration_status_error=None,
        )

    assert setup.install_insightface_button.options["state"] == "disabled"
    assert setup.install_openclip_button.options["state"] == "disabled"
    assert setup.download_face_model_button.options["state"] == "disabled"
