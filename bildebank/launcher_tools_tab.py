from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .config import (
    load_config,
    set_face_recognition_enabled,
    set_image_search_enabled,
)
from .launcher_commands import (
    cleanup_pending_deletes_apply_command,
    cleanup_pending_deletes_list_command,
    deep_doctor_command,
    doctor_command,
    export_person_command,
    face_scan_command,
    geo_scan_command,
    image_scan_command,
    make_browser_command,
    make_people_browser_command,
    make_person_browser_command,
    make_thumbnails_command,
    make_video_previews_command,
    vacuum_command,
)
from .launcher_status import (
    InsightFaceDependencyStatus,
    InsightFaceModelStatus,
    OpenClipModelStatus,
    RegisteredPerson,
    insightface_install_supported,
    openclip_install_supported,
    program_repo_root,
    registered_persons,
)
from .launcher_widgets import Tooltip, select_person_dialog
from .pending_deletes import list_pending_deletes
from .video_previews import active_video_preview_candidates, existing_video_preview_path

FACE_SCAN_TOOLTIP = (
    "Kjører 'bildebank face-scan'. Denne kommandoen scanner bildene etter ansikter. "
    "Må kjøres på nytt når du legger til nye biler."
)
FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP = (
    "InsightFace må installeres og valgt ansiktsmodell må lastes ned på Oppsett-fanen "
    "for å slå på ansiktsgjenkjenning."
)
FACE_SCAN_SETUP_DOWNLOAD_MESSAGE = (
    "Ansiktsgjenkjenning krever InsightFace og en ansiktsmodell. "
    "Dette kan laste ned litt over 400 MB.\n\n"
    "Vil du installere det som mangler, slå på ansiktsgjenkjenning og søke etter ansikter nå?"
)
FACE_SCAN_ENABLE_MESSAGE = (
    "Ansiktsgjenkjenning er slått av i innstillingene.\n\n"
    "Vil du slå den på og søke etter ansikter nå?"
)
IMAGE_SCAN_TOOLTIP = (
    "Kjører 'bildebank image-scan'. Denne kommandoen gjør at du "
    "kan gjøre klikke Bildesøk i nettleseren og skrive søkeord der. "
    "Kommandoen må scanne nye bilder for at det kan søkes i dem."
)
IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP = (
    "Trykk knappen 'Installer OpenCLIP' på Oppsett-fanen for å slå på bildesøk."
)
IMAGE_SCAN_SETUP_DOWNLOAD_MESSAGE = (
    "Bildesøk krever OpenCLIP og en lokal AI-modell. "
    "Dette kan laste ned flere hundre MB.\n\n"
    "Vil du installere det som mangler, slå på bildesøk og klargjøre bildene nå?"
)
IMAGE_SCAN_ENABLE_MESSAGE = (
    "Bildesøk er slått av i innstillingene.\n\n"
    "Vil du slå det på og klargjøre bildene nå?"
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


class ToolsSetup(Protocol):
    insightface_status: InsightFaceDependencyStatus
    face_model_status: InsightFaceModelStatus
    openclip_status: str
    openclip_model_status: OpenClipModelStatus

    def run_insightface_install(self, *, on_success: Callable[[], None]) -> None: ...

    def run_face_model_download(self, *, on_success: Callable[[], None]) -> None: ...

    def run_openclip_install(self, *, on_success: Callable[[], None]) -> None: ...


class ToolsTab:
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
        get_setup: Callable[[], ToolsSetup],
        log: Callable[[str], None],
        refresh_launcher: Callable[[], None],
        add_tooltip: Callable[[Any, str], None],
        ask_string: Callable[..., str | None],
        show_log_review_question: Callable[..., None],
        show_error: Callable[[str, BaseException], None],
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
        self._get_setup = get_setup
        self._log = log
        self._refresh_launcher = refresh_launcher
        self._add_tooltip = add_tooltip
        self._ask_string = ask_string
        self._show_log_review_question = show_log_review_question
        self._show_error = show_error
        self.padx = padx
        self.pady = pady

        self.frame = ttk.Frame(notebook, padding=padding)
        self.frame.columnconfigure(0, weight=1)
        self.button_frame = ttk.Frame(self.frame)
        self.button_frame.grid(row=2, column=0, sticky="w")
        self.static_browser_hide_out_of_focus_var = tk.BooleanVar(value=False)
        self.face_scan_button: Any | None = None
        self.face_scan_tooltip: Tooltip | None = None
        self.image_scan_button: Any | None = None
        self.image_scan_tooltip: Tooltip | None = None
        self.pending_deletes_status = "Ukjent"
        self.pending_deletes_count: int | None = None
        self.video_preview_missing_count: int | None = None

    @property
    def collection_path(self) -> Path:
        return self._get_collection_path()

    @property
    def setup(self) -> ToolsSetup:
        return self._get_setup()

    def refresh(self, *, available: bool) -> list[Any]:
        if self.face_scan_tooltip is not None:
            self.face_scan_tooltip.hide()
        if self.image_scan_tooltip is not None:
            self.image_scan_tooltip.hide()
        self.face_scan_button = None
        self.face_scan_tooltip = None
        self.image_scan_button = None
        self.image_scan_tooltip = None
        for child in self.button_frame.winfo_children():
            child.destroy()
        if not available:
            self.pending_deletes_status = "Ukjent"
            self.pending_deletes_count = None
            return []

        self._refresh_pending_deletes_status()
        self._refresh_video_preview_status()
        geo_button = self._button(
            self.button_frame,
            text="Les GPS fra bilder",
            command=self._run_geo_scan,
        )
        geo_button.grid(row=0, column=0, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(geo_button, "Scann bildene med exiftool for å finne ut hvor bildene ble tatt.")

        thumbs_button = self._button(
            self.button_frame,
            text="Lag miniatyrbilder",
            command=self._run_make_thumbnails,
        )
        thumbs_button.grid(row=0, column=1, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(
            thumbs_button,
            "Lag småbilder av alle bildene som kan brukes for at månedsvisning skal laste raskere.",
        )

        face_button = self._button(
            self.button_frame,
            text="Finn ansikter",
            command=self._run_face_scan,
        )
        self.face_scan_button = face_button
        face_button.grid(row=0, column=2, padx=self.padx, pady=self.pady, sticky="ew")
        self.face_scan_tooltip = Tooltip(face_button, FACE_SCAN_TOOLTIP)

        image_scan_button = self._button(
            self.button_frame,
            text="Klargjør bildesøk",
            command=self._run_image_scan,
        )
        self.image_scan_button = image_scan_button
        image_scan_button.grid(row=0, column=3, padx=self.padx, pady=self.pady, sticky="ew")
        self.image_scan_tooltip = Tooltip(image_scan_button, IMAGE_SCAN_TOOLTIP)

        doctor_button = self._button(
            self.button_frame,
            text="Sjekk bildebank",
            command=self._run_doctor,
        )
        doctor_button.grid(row=1, column=0, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(
            doctor_button,
            "Kjør en status-sjekk av Bildebank og bildesamlingen. "
            "Du kan få forslag til tiltak som må gjøres.",
        )

        deep_doctor_button = self._button(
            self.button_frame,
            text="Grundig sjekk",
            command=self._run_deep_doctor,
        )
        deep_doctor_button.grid(row=1, column=1, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(
            deep_doctor_button,
            "Kjør en status-sjekk av Bildebank og bildesamlingen. "
            "Denne kjører en enda grundigere sjekk, og kan ta litt tid å fullføre. "
            "Du kan få forslag til tiltak som må gjøres. ",
        )

        vacuum_button = self._button(
            self.button_frame,
            text="Rydd databaser",
            command=self._run_vacuum,
        )
        vacuum_button.grid(row=1, column=2, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(vacuum_button, "Bildebank reduserer størrelsen på databasene, hvis mulig.")

        pending_button = self._button(
            self.button_frame,
            text=self._pending_deletes_button_text(),
            command=self._show_pending_deletes,
        )
        self._add_tooltip(
            pending_button,
            "Hvis det finnes filer her, så har en jobb som skulle flytte eller slette "
            "blitt avbrutt. Knappen brukes til å fullføre jobben på en trygg måte.",
        )
        pending_button.grid(row=1, column=3, padx=self.padx, pady=self.pady, sticky="ew")

        export_person_button = self._button(
            self.button_frame,
            text="Eksporter person",
            command=self._start_export_person_flow,
        )
        export_person_button.grid(row=2, column=0, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(
            export_person_button,
            "Eksporter en kopi av alle bildene som vises på siden til en person i bildebrowseren.",
        )

        static_browser_button = self._button(
            self.button_frame,
            text="Lag HTML-browser",
            command=self._run_make_browser,
        )
        static_browser_button.grid(row=2, column=1, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(
            static_browser_button,
            "Lag en statisk index.html i bildesamlingen som kan åpnes uten Bildebank-server.",
        )

        static_person_browser_button = self._button(
            self.button_frame,
            text="Lag personbrowser",
            command=self._start_make_person_browser_flow,
        )
        static_person_browser_button.grid(row=2, column=2, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(
            static_person_browser_button,
            "Lag en statisk HTML-browser for en valgt person.",
        )

        static_people_browser_button = self._button(
            self.button_frame,
            text="Lag alle personbrowsere",
            command=self._run_make_people_browser,
        )
        static_people_browser_button.grid(row=2, column=3, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(
            static_people_browser_button,
            "Lag statiske HTML-browsere for alle registrerte personer.",
        )

        static_browser_hide_checkbox = self.ttk.Checkbutton(
            self.button_frame,
            text='Skjul "Ute av fokus"',
            variable=self.static_browser_hide_out_of_focus_var,
        )
        static_browser_hide_checkbox.grid(
            row=3,
            column=1,
            columnspan=3,
            padx=self.padx,
            pady=self.pady,
            sticky="w",
        )
        video_preview_button = self._button(
            self.button_frame,
            text=self._video_preview_button_text(),
            command=self._run_make_video_previews,
        )
        video_preview_button.grid(row=3, column=0, padx=self.padx, pady=self.pady, sticky="ew")
        self._add_tooltip(
            video_preview_button,
            "Lag MP4-avspillingskopier av AVI- og 3GP-videoer. Originalene endres ikke.",
        )
        self._add_tooltip(
            static_browser_hide_checkbox,
            "Når dette er valgt, får de statiske HTML-browserkommandoene "
            "flagget --hide-out-of-focus.",
        )
        self.update_dependency_tooltips()
        return [
            geo_button,
            face_button,
            image_scan_button,
            thumbs_button,
            static_browser_button,
            static_person_browser_button,
            static_people_browser_button,
            static_browser_hide_checkbox,
            doctor_button,
            deep_doctor_button,
            vacuum_button,
            pending_button,
            export_person_button,
            video_preview_button,
        ]

    def update_dependency_tooltips(self) -> None:
        if self.face_scan_tooltip is not None:
            self.face_scan_tooltip.text = (
                FACE_SCAN_TOOLTIP
                if self.setup.insightface_status.status == "Klar"
                and self.setup.face_model_status.status == "Lastet ned"
                else FACE_SCAN_DEPENDENCY_MISSING_TOOLTIP
            )
        if self.image_scan_tooltip is not None:
            self.image_scan_tooltip.text = (
                IMAGE_SCAN_TOOLTIP
                if self.setup.openclip_status == "Installert"
                and self.setup.openclip_model_status.status == "Tilgjengelig"
                else IMAGE_SCAN_OPENCLIP_MISSING_TOOLTIP
            )

    def _refresh_pending_deletes_status(self) -> None:
        try:
            rows = list_pending_deletes(self.collection_path)
        except Exception as exc:  # noqa: BLE001 - launcher should remain usable
            self.pending_deletes_status = "Ukjent"
            self.pending_deletes_count = None
            self._log(f"Kunne ikke lese ventende filsletting-status: {exc}")
            return
        self.pending_deletes_count = len(rows)
        self.pending_deletes_status = "OK" if not rows else "Trenger opprydding"

    def _pending_deletes_button_text(self) -> str:
        if self.pending_deletes_status == "OK":
            return "Ventende filsletting: OK"
        if self.pending_deletes_status == "Trenger opprydding":
            assert self.pending_deletes_count is not None
            return f"Ventende filsletting: ! {self.pending_deletes_count}"
        return "Ventende filsletting: ukjent"

    def _refresh_video_preview_status(self) -> None:
        try:
            candidates = active_video_preview_candidates(self.collection_path)
            self.video_preview_missing_count = sum(
                existing_video_preview_path(self.collection_path, item) is None
                for item in candidates
            )
        except Exception as exc:  # noqa: BLE001 - launcher should remain usable
            self.video_preview_missing_count = None
            self._log(f"Kunne ikke lese status for videoavspillingskopier: {exc}")

    def _video_preview_button_text(self) -> str:
        if self.video_preview_missing_count is None:
            return "Videoavspilling: ukjent"
        if self.video_preview_missing_count == 0:
            return "Videoavspilling: OK"
        return f"Lag videoavspilling: {self.video_preview_missing_count} mangler"

    def _run_geo_scan(self) -> None:
        self._log("Scanner GPS-metadata ...")
        self._run_waiting_command(
            geo_scan_command(self.collection_path),
            running_message="Scanner GPS-metadata ...",
            success_message="GPS-scan fullført.",
            failure_message="GPS-scan feilet.",
            on_success=self._refresh_launcher,
            cancellable=True,
        )

    def _run_face_scan(self) -> None:
        from tkinter import messagebox

        insightface_missing = self.setup.insightface_status.status != "Klar"
        model_missing = self.setup.face_model_status.status != "Lastet ned"
        face_recognition_disabled = not self._face_recognition_enabled()
        if not insightface_missing and not model_missing and not face_recognition_disabled:
            self._start_face_scan_command()
            return

        if (insightface_missing or model_missing) and not insightface_install_supported():
            messagebox.showerror(
                "Ansiktsgjenkjenning mangler",
                "Ansiktsgjenkjenning kan ikke klargjøres automatisk her. "
                "Installer InsightFace og last ned ansiktsmodellen fra Oppsett-fanen på Windows.",
                parent=self.root,
            )
            self._log("Ansiktsscan avbrutt: InsightFace-oppsett kan ikke kjøres automatisk her.")
            return

        question = FACE_SCAN_SETUP_DOWNLOAD_MESSAGE if insightface_missing or model_missing else FACE_SCAN_ENABLE_MESSAGE
        if not messagebox.askyesno("Klargjør ansiktsgjenkjenning?", question, parent=self.root):
            self._log("Ansiktsscan avbrutt.")
            return

        steps: list[Callable[[Callable[[], None]], None]] = []
        if insightface_missing:
            steps.append(self._run_face_scan_insightface_install_step)
        if model_missing:
            steps.append(self._run_face_scan_model_download_step)
        if face_recognition_disabled:
            steps.append(self._run_face_scan_enable_step)
        self._run_face_scan_setup_steps(steps)

    def _start_face_scan_command(self) -> None:
        self._log("Scanner ansikter ...")
        self._run_waiting_command(
            face_scan_command(self.collection_path),
            running_message="Scanner ansikter ...",
            success_message="Ansiktsscan fullført.",
            failure_message="Ansiktsscan feilet.",
            on_success=self._refresh_launcher,
            cancellable=True,
        )

    def _face_recognition_enabled(self) -> bool:
        try:
            return bool(load_config(program_repo_root()).face_recognition.enabled)
        except (OSError, ValueError) as exc:
            self._log(f"Kunne ikke lese innstilling for ansiktsgjenkjenning: {exc}")
            return False

    def _run_face_scan_setup_steps(self, steps: list[Callable[[Callable[[], None]], None]]) -> None:
        if not steps:
            self._start_face_scan_command()
            return
        step = steps[0]
        remaining = steps[1:]
        step(lambda: self._run_face_scan_setup_steps(remaining))

    def _run_face_scan_insightface_install_step(self, on_success: Callable[[], None]) -> None:
        self._log("Installerer InsightFace før ansiktsscan ...")
        self.setup.run_insightface_install(on_success=on_success)

    def _run_face_scan_model_download_step(self, on_success: Callable[[], None]) -> None:
        self._log(f"Laster ned ansiktsmodell {self.setup.face_model_status.model_name} før ansiktsscan ...")
        self.setup.run_face_model_download(on_success=on_success)

    def _run_face_scan_enable_step(self, on_success: Callable[[], None]) -> None:
        try:
            set_face_recognition_enabled(program_repo_root(), True)
        except (OSError, ValueError) as exc:
            self._show_error("Kunne ikke slå på ansiktsgjenkjenning.", exc)
            return
        self._log("Ansiktsgjenkjenning er slått på.")
        on_success()

    def _run_image_scan(self) -> None:
        from tkinter import messagebox

        openclip_missing = self.setup.openclip_status != "Installert"
        model_missing = self.setup.openclip_model_status.status != "Tilgjengelig"
        image_search_disabled = not self._image_search_enabled()
        if not openclip_missing and not model_missing and not image_search_disabled:
            self._start_image_scan_command()
            return

        if (openclip_missing or model_missing) and not openclip_install_supported():
            messagebox.showerror(
                "Bildesøk mangler",
                "Bildesøk kan ikke klargjøres automatisk her. "
                "Installer OpenCLIP og AI-modellen fra Oppsett-fanen på Windows.",
                parent=self.root,
            )
            self._log("Bildesøk-scan avbrutt: OpenCLIP-oppsett kan ikke kjøres automatisk her.")
            return

        question = IMAGE_SCAN_SETUP_DOWNLOAD_MESSAGE if openclip_missing or model_missing else IMAGE_SCAN_ENABLE_MESSAGE
        if not messagebox.askyesno("Klargjør bildesøk?", question, parent=self.root):
            self._log("Bildesøk-scan avbrutt.")
            return

        steps: list[Callable[[Callable[[], None]], None]] = []
        if openclip_missing or model_missing:
            steps.append(self._run_image_scan_openclip_install_step)
        if image_search_disabled:
            steps.append(self._run_image_scan_enable_step)
        self._run_image_scan_setup_steps(steps)

    def _start_image_scan_command(self) -> None:
        self._log("Scanner bilder for bildesøk ...")
        self._run_waiting_command(
            image_scan_command(self.collection_path),
            running_message="Scanner bilder for bildesøk ...",
            success_message="Bildesøk-scan fullført.",
            failure_message="Bildesøk-scan feilet.",
            on_success=self._refresh_launcher,
            cancellable=True,
        )

    def _image_search_enabled(self) -> bool:
        try:
            return bool(load_config(program_repo_root()).openclip.enabled)
        except (OSError, ValueError) as exc:
            self._log(f"Kunne ikke lese innstilling for bildesøk: {exc}")
            return False

    def _run_image_scan_setup_steps(self, steps: list[Callable[[Callable[[], None]], None]]) -> None:
        if not steps:
            self._start_image_scan_command()
            return
        step = steps[0]
        remaining = steps[1:]
        step(lambda: self._run_image_scan_setup_steps(remaining))

    def _run_image_scan_openclip_install_step(self, on_success: Callable[[], None]) -> None:
        self._log("Installerer OpenCLIP før bildesøk-scan ...")
        self.setup.run_openclip_install(on_success=on_success)

    def _run_image_scan_enable_step(self, on_success: Callable[[], None]) -> None:
        try:
            set_image_search_enabled(program_repo_root(), True)
        except (OSError, ValueError) as exc:
            self._show_error("Kunne ikke slå på bildesøk.", exc)
            return
        self._log("Bildesøk er slått på.")
        on_success()

    def _run_make_thumbnails(self) -> None:
        self._log("Lager thumbnails ...")
        self._run_waiting_command(
            make_thumbnails_command(self.collection_path),
            running_message="Lager thumbnails ...",
            success_message="Thumbnails fullført.",
            failure_message="Thumbnail-jobb feilet.",
            on_success=self._refresh_launcher,
            cancellable=True,
        )

    def _run_make_video_previews(self) -> None:
        self._log("Lager videoavspillingskopier ...")
        self._run_waiting_command(
            make_video_previews_command(self.collection_path),
            running_message="Lager videoavspillingskopier ...",
            success_message="Videoavspillingskopier fullført.",
            failure_message="Videoavspillingskopier feilet.",
            on_success=self._refresh_launcher,
            cancellable=True,
        )

    def _run_make_browser(self) -> None:
        hide_out_of_focus = bool(self.static_browser_hide_out_of_focus_var.get())
        self._log("Lager statisk HTML-browser ...")
        self._run_waiting_command(
            make_browser_command(self.collection_path, hide_out_of_focus=hide_out_of_focus),
            running_message="Lager statisk HTML-browser ...",
            success_message="Statisk HTML-browser fullført.",
            failure_message="Statisk HTML-browser feilet.",
            on_success=self._refresh_launcher,
            cancellable=True,
        )

    def _start_make_person_browser_flow(self) -> None:
        from tkinter import messagebox

        persons = self._load_registered_persons()
        if persons is None:
            return
        if not persons:
            messagebox.showinfo("Ingen personer", "Fant ingen registrerte personer.")
            self._log("Personbrowser avbrutt: fant ingen registrerte personer.")
            return
        self._select_person(
            persons,
            title="Lag personbrowser",
            description="Velg personen det skal lages statisk HTML-browser for.",
            action_label="Lag HTML-browser",
            on_cancel=lambda: self._log("Personbrowser avbrutt: ingen person valgt."),
            on_select=self._run_make_person_browser,
        )

    def _run_make_person_browser(self, person: RegisteredPerson) -> None:
        hide_out_of_focus = bool(self.static_browser_hide_out_of_focus_var.get())
        self._log(f'Lager statisk HTML-browser for "{person.name}" ...')
        self._run_waiting_command(
            make_person_browser_command(
                self.collection_path,
                person.name,
                hide_out_of_focus=hide_out_of_focus,
            ),
            running_message="Lager statisk personbrowser ...",
            success_message="Statisk personbrowser fullført.",
            failure_message="Statisk personbrowser feilet.",
            on_success=self._refresh_launcher,
            cancellable=True,
        )

    def _run_make_people_browser(self) -> None:
        hide_out_of_focus = bool(self.static_browser_hide_out_of_focus_var.get())
        self._log("Lager statiske personbrowsere ...")
        self._run_waiting_command(
            make_people_browser_command(self.collection_path, hide_out_of_focus=hide_out_of_focus),
            running_message="Lager statiske personbrowsere ...",
            success_message="Statiske personbrowsere fullført.",
            failure_message="Statiske personbrowsere feilet.",
            on_success=self._refresh_launcher,
            cancellable=True,
        )

    def _run_doctor(self) -> None:
        self._log("Kjører doctor ...")
        self._run_waiting_command(
            doctor_command(self.collection_path),
            running_message="Kjører doctor ...",
            success_message="Doctor fullført.",
            failure_message="Doctor feilet.",
            on_success=self._refresh_launcher,
            cancellable=True,
        )

    def _run_deep_doctor(self) -> None:
        self._log("Kjører grundig doctor ...")
        self._run_waiting_command(
            deep_doctor_command(self.collection_path),
            running_message="Kjører grundig doctor ...",
            success_message="Grundig doctor fullført.",
            failure_message="Grundig doctor feilet.",
            on_success=self._refresh_launcher,
            cancellable=True,
        )

    def _run_vacuum(self) -> None:
        self._log("Pakker Bildebank-databaser ...")
        self._run_waiting_command(
            vacuum_command(self.collection_path),
            running_message="Pakker Bildebank-databaser ...",
            success_message="Vacuum fullført.",
            failure_message="Vacuum feilet.",
            on_success=self._refresh_launcher,
        )

    def _show_pending_deletes(self) -> None:
        self._log("Kontrollerer ventende filsletting ...")
        self._run_waiting_command(
            cleanup_pending_deletes_list_command(self.collection_path),
            running_message="Kontrollerer ventende filsletting ...",
            success_message="Kontroll av ventende filsletting fullført. Se listen i loggen.",
            failure_message="Kontroll av ventende filsletting feilet.",
            on_success=self._pending_deletes_list_finished,
        )

    def _pending_deletes_list_finished(self) -> None:
        from tkinter import messagebox

        self._refresh_pending_deletes_status()
        self._refresh_launcher()
        if not self.pending_deletes_count:
            messagebox.showinfo(
                "Ventende filsletting",
                "Ingen ventende filslettinger.",
                parent=self.root,
            )
            return
        self._show_log_review_question(
            "Ventende filsletting",
            (
                "Listen over ventende filslettinger står i loggen.\n\n"
                "Vil du prøve å rydde opp nå?"
            ),
            yes_text="Rydd opp",
            no_text="Avbryt",
            on_yes=self._confirm_cleanup_pending_deletes,
            on_no=lambda: self._log("Opprydding av ventende filsletting avbrutt."),
        )

    def _confirm_cleanup_pending_deletes(self) -> None:
        confirmation = self._ask_string(
            "Bekreft ventende filsletting",
            'Skriv "ja, rydd opp" for å gjennomføre opprydding.',
        )
        if confirmation != "ja, rydd opp":
            self._log("Opprydding av ventende filsletting avbrutt.")
            return
        self._run_cleanup_pending_deletes()

    def _run_cleanup_pending_deletes(self) -> None:
        self._log("Rydder opp ventende filsletting ...")
        self._run_waiting_command(
            cleanup_pending_deletes_apply_command(self.collection_path),
            running_message="Rydder opp ventende filsletting ...",
            success_message="Opprydding av ventende filsletting fullført.",
            failure_message="Opprydding av ventende filsletting feilet.",
            on_success=self._refresh_launcher,
        )

    def _load_registered_persons(self) -> list[RegisteredPerson] | None:
        from tkinter import messagebox

        try:
            return registered_persons(self.collection_path)
        except Exception as exc:  # noqa: BLE001 - GUI should show readable errors
            messagebox.showerror("Kunne ikke lese personer", "Kunne ikke lese registrerte personer.")
            self._log(f"Kunne ikke lese registrerte personer: {exc}")
            return None

    def _start_export_person_flow(self) -> None:
        from tkinter import filedialog, messagebox

        persons = self._load_registered_persons()
        if persons is None:
            return
        if not persons:
            messagebox.showinfo("Ingen personer", "Fant ingen registrerte personer.")
            self._log("Personeksport avbrutt: fant ingen registrerte personer.")
            return
        self._select_person(
            persons,
            title="Eksporter person",
            description=(
                "Denne funksjonen eksporterer en kopi av alle bildene av en person. "
                "Velg personen du vil eksportere, og deretter mappen der personmappen skal opprettes."
            ),
            action_label="Velg mappe",
            on_cancel=lambda: self._log("Personeksport avbrutt: ingen person valgt."),
            on_select=lambda person: self._choose_export_person_destination(person, filedialog=filedialog),
        )

    def _choose_export_person_destination(self, person: RegisteredPerson, *, filedialog: Any) -> None:
        selected = filedialog.askdirectory(
            title=f"Velg hvor personmappen for {person.name} skal opprettes",
            initialdir=str(self.collection_path.parent),
        )
        if not selected:
            self._log(f'Personeksport avbrutt for "{person.name}": ingen mappe valgt.')
            return
        self._run_export_person_dry_run(person, Path(selected))

    def _select_person(
        self,
        persons: list[RegisteredPerson],
        *,
        title: str,
        description: str,
        action_label: str,
        on_select: Callable[[RegisteredPerson], None],
        on_cancel: Callable[[], None],
    ) -> None:
        select_person_dialog(
            persons,
            tk=self.tk,
            ttk=self.ttk,
            root=self.root,
            button=self._button,
            title=title,
            description=description,
            action_label=action_label,
            on_select=on_select,
            on_cancel=on_cancel,
        )

    def _run_export_person_dry_run(self, person: RegisteredPerson, destination_root: Path) -> None:
        self._log(f'Kontrollerer personeksport for "{person.name}" til {destination_root} ...')
        self._run_waiting_command(
            export_person_command(self.collection_path, person.name, destination_root, dry_run=True),
            running_message="Kontrollerer personeksport ...",
            success_message="Eksport dry-run fullført. Se planen i loggen.",
            failure_message="Eksport dry-run feilet.",
            on_success=lambda: self._confirm_export_person(person, destination_root),
        )

    def _confirm_export_person(self, person: RegisteredPerson, destination_root: Path) -> None:
        self._show_log_review_question(
            "Eksporter person?",
            (
                "Dry-run er fullført og planen står i loggen.\n\n"
                f'Vil du eksportere bildene av "{person.name}" nå?\n\n'
                f"Personmappen opprettes under:\n{destination_root}"
            ),
            yes_text="Eksporter",
            no_text="Avbryt",
            on_yes=lambda: self._run_export_person(person, destination_root),
            on_no=lambda: self._log(f'Personeksport avbrutt for "{person.name}".'),
        )

    def _run_export_person(self, person: RegisteredPerson, destination_root: Path) -> None:
        self._log(f'Eksporterer bilder av "{person.name}" til {destination_root} ...')
        self._run_waiting_command(
            export_person_command(self.collection_path, person.name, destination_root),
            running_message="Eksporterer person ...",
            success_message="Personeksport fullført.",
            failure_message="Personeksport feilet.",
            on_success=self._refresh_launcher,
            cancellable=True,
        )
