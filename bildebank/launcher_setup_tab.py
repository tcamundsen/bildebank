from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from typing import Any, Protocol

from .launcher_commands import (
    download_face_model_command,
    ffmpeg_install_command,
    insightface_install_command,
    openclip_install_command,
)
from .launcher_status import (
    InsightFaceDependencyStatus,
    InsightFaceModelStatus,
    OpenClipModelStatus,
    FFmpegDependencyStatus,
    dependency_setup_button_state,
    face_model_download_button_state,
    ffmpeg_dependency_status,
    ffmpeg_install_supported,
    insightface_dependency_status,
    insightface_install_supported,
    insightface_model_status,
    openclip_dependency_status,
    openclip_install_supported,
    openclip_model_status,
)


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


class SetupTab:
    def __init__(
        self,
        *,
        tk: Any,
        ttk: Any,
        notebook: Any,
        root: Any,
        button: ButtonFactory,
        run_waiting_command: WaitingCommandRunner,
        post_to_ui: Callable[[Callable[[], None]], bool],
        log: Callable[[str], None],
        confirm_rerun: Callable[[str, str], bool],
        on_status_changed: Callable[[], None],
        padding: int,
        pady: int,
    ) -> None:
        self.root = root
        self._run_waiting_command = run_waiting_command
        self._post_to_ui = post_to_ui
        self._log = log
        self._confirm_rerun = confirm_rerun
        self._on_status_changed = on_status_changed

        self.insightface_status = InsightFaceDependencyStatus("Sjekker")
        self.face_model_status = InsightFaceModelStatus("", "Sjekker")
        self.openclip_status = "Sjekker"
        self.openclip_model_status = OpenClipModelStatus("", "", "Sjekker")
        self.ffmpeg_status = FFmpegDependencyStatus("Sjekker")
        self.ffmpeg_auto_install_attempted = False
        self.status_refreshing = False

        self.insightface_status_value = tk.StringVar(value="")
        self.insightface_model_status_value = tk.StringVar(value="")
        self.openclip_status_value = tk.StringVar(value="")
        self.openclip_model_status_value = tk.StringVar(value="")
        self.ffmpeg_status_value = tk.StringVar(value="")
        self.frame = ttk.Frame(notebook, padding=padding)
        self.install_insightface_button: Any | None = None
        self.install_openclip_button: Any | None = None
        self.download_face_model_button: Any | None = None
        self.install_ffmpeg_button: Any | None = None

        self._set_status_placeholder()
        self._build(ttk=ttk, button=button, padding=padding, pady=pady)

    def _build(self, *, ttk: Any, button: ButtonFactory, padding: int, pady: int) -> None:
        self.frame.columnconfigure(0, weight=1)

        ffmpeg_frame = ttk.Frame(self.frame)
        ffmpeg_frame.grid(row=0, column=0, sticky="w")
        self.install_ffmpeg_button = button(
            ffmpeg_frame,
            text="Installer FFmpeg",
            command=self.install_ffmpeg,
        )
        self.install_ffmpeg_button.grid(row=0, column=0, sticky="w")
        ttk.Label(ffmpeg_frame, textvariable=self.ffmpeg_status_value).grid(
            row=0,
            column=1,
            sticky="w",
            padx=padding,
        )

        ffmpeg_separator = ttk.Separator(self.frame, orient="horizontal")
        ffmpeg_separator.grid(row=1, column=0, sticky="ew", pady=padding)

        insightface_frame = ttk.Frame(self.frame)
        insightface_frame.grid(row=2, column=0, sticky="w")
        ttk.Label(insightface_frame, textvariable=self.insightface_status_value).grid(
            row=0,
            column=0,
            sticky="e",
            padx=padding,
        )
        self.install_insightface_button = button(
            insightface_frame,
            text="Installer InsightFace",
            command=self.install_insightface,
        )
        self.install_insightface_button.grid(row=0, column=1, sticky="w", pady=pady)
        ttk.Label(insightface_frame, textvariable=self.insightface_model_status_value).grid(
            row=1,
            column=0,
            sticky="e",
            padx=(0, 12),
        )
        self.download_face_model_button = button(
            insightface_frame,
            text="Last ned modell",
            command=self.download_face_model,
        )
        self.download_face_model_button.grid(row=1, column=1, sticky="w", pady=pady)

        separator = ttk.Separator(self.frame, orient="horizontal")
        separator.grid(row=3, column=0, sticky="ew", pady=padding)

        openclip_frame = ttk.Frame(self.frame)
        openclip_frame.grid(row=4, column=0, sticky="w")
        self.install_openclip_button = button(
            openclip_frame,
            text="Installer OpenCLIP",
            command=self.install_openclip,
        )
        self.install_openclip_button.grid(row=0, column=0, sticky="w")
        ttk.Label(openclip_frame, textvariable=self.openclip_status_value).grid(
            row=0,
            column=1,
            sticky="w",
            padx=padding,
        )
        ttk.Label(openclip_frame, textvariable=self.openclip_model_status_value).grid(
            row=0,
            column=2,
            sticky="w",
        )

    def log_unsupported_installers(self) -> None:
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
        if not ffmpeg_install_supported():
            self._log("Installer FFmpeg-knappen er deaktivert: automatisk installasjon støttes bare på Windows.")

    def _set_status_placeholder(self) -> None:
        self.insightface_status_value.set("InsightFace: sjekker ...")
        self.insightface_model_status_value.set("Valgt modell: sjekker ...")
        self.openclip_status_value.set("open_clip: sjekker ...")
        self.openclip_model_status_value.set("AI-modell: sjekker ...")
        self.ffmpeg_status_value.set("FFmpeg: sjekker ...")

    def _apply_status_values(self) -> None:
        self.insightface_status_value.set(f"InsightFace: {self.insightface_status.status}")
        self.insightface_model_status_value.set(
            f"Valgt modell: {self.face_model_status.model_name} ({self.face_model_status.status})"
        )
        self.openclip_status_value.set(f"open_clip: {self.openclip_status}")
        self.openclip_model_status_value.set(f"AI-modell: {self.openclip_model_status.status}")
        self.ffmpeg_status_value.set(f"FFmpeg: {self.ffmpeg_status.status}")

    def _log_status_detail(self, label: str, status: str, detail: str) -> None:
        if status == "Feil" and detail:
            self._log(f"{label}-status feilet: {detail}")

    def start_status_refresh(self) -> None:
        if self.status_refreshing:
            return
        self.status_refreshing = True
        self._set_status_placeholder()
        self._on_status_changed()
        thread = threading.Thread(target=self._status_worker, daemon=True)
        thread.start()

    def _status_worker(self) -> None:
        statuses = self._load_status()
        self._post_to_ui(lambda: self._status_finished(*statuses))

    def _load_status(
        self,
    ) -> tuple[InsightFaceDependencyStatus, InsightFaceModelStatus, str, OpenClipModelStatus, FFmpegDependencyStatus]:
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
        try:
            ffmpeg_status = ffmpeg_dependency_status()
        except Exception as exc:  # noqa: BLE001 - setup status must not block launcher startup
            ffmpeg_status = FFmpegDependencyStatus("Feil", str(exc))
        return insightface_status, face_model_status, openclip_status, openclip_model_state, ffmpeg_status

    def _status_finished(
        self,
        insightface_status: InsightFaceDependencyStatus,
        face_model_status: InsightFaceModelStatus,
        openclip_status: str,
        openclip_model_status: OpenClipModelStatus,
        ffmpeg_status: FFmpegDependencyStatus,
    ) -> None:
        self.status_refreshing = False
        self.insightface_status = insightface_status
        self.face_model_status = face_model_status
        self.openclip_status = openclip_status
        self.openclip_model_status = openclip_model_status
        self.ffmpeg_status = ffmpeg_status
        self._log_status_detail("InsightFace", insightface_status.status, insightface_status.detail)
        self._log_status_detail("Ansiktsmodell", face_model_status.status, face_model_status.detail)
        self._log_status_detail("OpenCLIP-modell", openclip_model_status.status, openclip_model_status.detail)
        self._log_status_detail("FFmpeg", ffmpeg_status.status, ffmpeg_status.detail)
        self._apply_status_values()
        self._on_status_changed()
        if (
            ffmpeg_status.status != "Klar"
            and ffmpeg_install_supported()
            and not self.ffmpeg_auto_install_attempted
        ):
            self.ffmpeg_auto_install_attempted = True
            self._log("FFmpeg mangler. Prøver automatisk installasjon ...")
            self.run_ffmpeg_install(on_success=self.start_status_refresh)

    def set_buttons_enabled(
        self,
        enabled: bool,
        *,
        migration_required: bool,
        migration_status_error: str | None,
    ) -> None:
        setup_enabled = enabled and not self.status_refreshing
        if self.install_insightface_button is not None:
            self.install_insightface_button.configure(
                state=dependency_setup_button_state(
                    enabled=setup_enabled,
                    migration_required=migration_required,
                    migration_status_error=migration_status_error,
                    install_supported=insightface_install_supported(),
                )
            )
        if self.install_openclip_button is not None:
            self.install_openclip_button.configure(
                state=dependency_setup_button_state(
                    enabled=setup_enabled,
                    migration_required=migration_required,
                    migration_status_error=migration_status_error,
                    install_supported=openclip_install_supported(),
                )
            )
        if self.download_face_model_button is not None:
            self.download_face_model_button.configure(
                state=face_model_download_button_state(
                    enabled=setup_enabled,
                    migration_required=migration_required,
                    migration_status_error=migration_status_error,
                    insightface_status=self.insightface_status,
                )
            )
        if self.install_ffmpeg_button is not None:
            self.install_ffmpeg_button.configure(
                state=dependency_setup_button_state(
                    enabled=setup_enabled,
                    migration_required=migration_required,
                    migration_status_error=migration_status_error,
                    install_supported=ffmpeg_install_supported(),
                )
            )

    def install_ffmpeg(self) -> None:
        if not ffmpeg_install_supported():
            self._log("Kan ikke installere FFmpeg automatisk her.")
            return
        if self.ffmpeg_status.status == "Klar" and not self._confirm_rerun(
            "Installer FFmpeg på nytt?",
            "FFmpeg er allerede klart. Vil du kjøre installasjonen på nytt?",
        ):
            self._log("FFmpeg-installasjon avbrutt.")
            return
        self._log("Installerer FFmpeg ...")
        self.run_ffmpeg_install(on_success=self.start_status_refresh)

    def run_ffmpeg_install(self, *, on_success: Callable[[], None]) -> None:
        self._run_waiting_command(
            ffmpeg_install_command(),
            running_message="Installerer FFmpeg ...",
            success_message="FFmpeg-installasjon fullført.",
            failure_message="FFmpeg-installasjon feilet. Bildebank prøver igjen ved neste oppstart.",
            on_success=on_success,
        )

    def install_insightface(self) -> None:
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
        self.run_insightface_install(on_success=self.start_status_refresh)

    def run_insightface_install(self, *, on_success: Callable[[], None]) -> None:
        self._run_waiting_command(
            insightface_install_command(),
            running_message="Installerer InsightFace ...",
            success_message="InsightFace-installasjon fullført.",
            failure_message="InsightFace-installasjon feilet.",
            on_success=lambda: self._install_finished(on_success),
        )

    def install_openclip(self) -> None:
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
        self.run_openclip_install(on_success=self.start_status_refresh)

    def run_openclip_install(self, *, on_success: Callable[[], None]) -> None:
        self._run_waiting_command(
            openclip_install_command(),
            running_message="Installerer OpenCLIP ...",
            success_message="OpenCLIP-installasjon fullført.",
            failure_message="OpenCLIP-installasjon feilet.",
            on_success=lambda: self._openclip_install_finished(on_success),
        )

    def _install_finished(self, on_success: Callable[[], None]) -> None:
        importlib.invalidate_caches()
        on_success()

    def _openclip_install_finished(self, on_success: Callable[[], None]) -> None:
        importlib.invalidate_caches()
        self.refresh_openclip_status_after_install()
        on_success()

    def refresh_openclip_status_after_install(self) -> None:
        try:
            self.openclip_status = openclip_dependency_status()
        except Exception as exc:  # noqa: BLE001 - setup status must not block launcher flow
            self.openclip_status = f"Feil: {exc}"
        try:
            self.openclip_model_status = openclip_model_status()
        except Exception as exc:  # noqa: BLE001 - setup status must not block launcher flow
            self.openclip_model_status = OpenClipModelStatus("", "", "Feil", str(exc))
        self._log_status_detail("OpenCLIP", self.openclip_status, "")
        self._log_status_detail(
            "OpenCLIP-modell",
            self.openclip_model_status.status,
            self.openclip_model_status.detail,
        )
        self._apply_status_values()
        self._on_status_changed()

    def download_face_model(self) -> None:
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
        self.run_face_model_download(on_success=self.start_status_refresh)

    def run_face_model_download(self, *, on_success: Callable[[], None]) -> None:
        self._run_waiting_command(
            download_face_model_command(),
            running_message="Laster ned ansiktsmodell ...",
            success_message="Ansiktsmodell lastet ned.",
            failure_message="Nedlasting av ansiktsmodell feilet.",
            on_success=on_success,
        )
