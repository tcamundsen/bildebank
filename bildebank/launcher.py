from __future__ import annotations

import importlib
from importlib import resources
import importlib.util
import json
import locale
import os
import signal
import subprocess
import sys
import threading
import tempfile
import webbrowser
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from PIL import Image, ImageTk

from . import db
from .config import load_config, load_launcher_collection_path, set_launcher_collection_path
from .pending_deletes import list_pending_deletes
from .server import DEFAULT_HOST, DEFAULT_PORT

if os.name == "nt":
    PADX = 2
    PADY = 2
    BUTTON_PADDING = (8, 4)
    PAD = 6
else:
    PADX = 4
    PADY = 4
    BUTTON_PADDING = (10, 6)
    PAD = 12

PAD_OUTER = 16
BUTTON_STYLE = "Launcher.TButton"

PROGRESS_LOG_LABELS = (
    "Import",
    "Import dry-run",
    "Rescan-source",
    "Rescan-source dry-run",
    "Thumbnails",
    "Unimport",
    "Check-source",
    "geo-scan",
    "Doctor filer",
    "Doctor SHA-256",
    "Doctor orphan",
    "Image-scan",
    "Image-search",
    "Face-scan",
    "Face-suggest",
    "Refresh-metadata",
)


@dataclass(frozen=True)
class LauncherConfig:
    collection_path: Path


@dataclass(frozen=True)
class LauncherUpdateStatus:
    status: str
    detail: str = ""
    commits_behind: int = 0


@dataclass(frozen=True)
class RegisteredPerson:
    name: str
    confirmed_file_count: int
    face_count: int
    suggestion_count: int
    updated_at: str


@dataclass(frozen=True)
class InsightFaceDependencyStatus:
    status: str
    detail: str = ""


@dataclass(frozen=True)
class InsightFaceModelStatus:
    model_name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class OpenClipModelStatus:
    model_name: str
    pretrained: str
    status: str
    detail: str = ""


def default_collection_path() -> Path:
    return Path.home() / "kode" / "bilde-samling"


def load_launcher_config() -> LauncherConfig:
    collection_path = load_launcher_collection_path(program_repo_root())
    if collection_path is not None:
        return LauncherConfig(collection_path=collection_path)
    return LauncherConfig(collection_path=default_collection_path())


def save_launcher_config(config: LauncherConfig) -> None:
    set_launcher_collection_path(program_repo_root(), config.collection_path)


def suggest_import_name(source_folder: Path) -> str:
    raw_path = str(source_folder)
    if "\\" in raw_path:
        name = PureWindowsPath(raw_path).name.strip()
    else:
        name = source_folder.name.strip()
    if name:
        return name
    return str(source_folder).strip()


def is_collection_created(collection_path: Path) -> bool:
    return db.db_path_for_target(collection_path).exists()


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


def bildebank_command(*args: str | Path) -> list[str]:
    return [sys.executable, "-m", "bildebank", *(str(arg) for arg in args)]


def create_command(collection_path: Path) -> list[str]:
    return bildebank_command("create", collection_path)


def import_command(collection_path: Path, source_folder: Path, import_name: str) -> list[str]:
    return bildebank_command("--target", collection_path, "import", "--name", import_name, source_folder)


def run_server_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "run-server")


def server_browser_url() -> str:
    return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/"


def open_server_browser_window() -> bool:
    return bool(webbrowser.open(server_browser_url(), new=1))


def launcher_command() -> list[str]:
    return bildebank_command("launcher")


def update_command() -> list[str]:
    return bildebank_command("update")


def check_launcher_update_status(repo_root: Path | None = None) -> LauncherUpdateStatus:
    root = repo_root or program_repo_root()
    try:
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
        upstream = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root)
        remote_name = _upstream_remote_name(upstream)
        remote_url = _run_git(["remote", "get-url", remote_name], root)
        if _git_remote_url_uses_ssh(remote_url):
            return LauncherUpdateStatus(
                "skipped",
                f"hopper over automatisk oppdateringssjekk for SSH-remote {remote_name}",
            )
        _run_git(["fetch", "--quiet"], root)
        commit_count_text = _run_git(["rev-list", "--count", "HEAD..@{u}"], root)
        commit_count = int(commit_count_text.strip())
    except FileNotFoundError as exc:
        return LauncherUpdateStatus("error", f"git finnes ikke: {exc}")
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        return LauncherUpdateStatus("error", str(exc))
    if commit_count > 0:
        return LauncherUpdateStatus(
            "available",
            f"{branch} ligger {commit_count} commits bak {upstream}",
            commits_behind=commit_count,
        )
    return LauncherUpdateStatus("current", f"{branch} er oppdatert mot {upstream}")


def _upstream_remote_name(upstream: str) -> str:
    remote_name, separator, _branch_name = upstream.partition("/")
    if not separator or not remote_name:
        raise ValueError(f"uventet upstream-navn: {upstream}")
    return remote_name


def _git_remote_url_uses_ssh(remote_url: str) -> bool:
    url = remote_url.strip()
    if url.startswith(("ssh://", "git+ssh://")):
        return True
    if "://" in url or "@" not in url:
        return False
    _user, _separator, host_and_path = url.partition("@")
    return ":" in host_and_path


def _run_git(args: list[str], repo_root: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


def doctor_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "doctor")


def deep_doctor_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "doctor", "--deep")


def geo_scan_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "geo-scan")


def face_scan_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "face-scan")


def image_scan_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "image-scan")


def make_thumbnails_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "make-thumbnails")


def make_browser_command(collection_path: Path, *, hide_out_of_focus: bool = False) -> list[str]:
    command = bildebank_command("--target", collection_path, "make-browser")
    if hide_out_of_focus:
        command.append("--hide-out-of-focus")
    return command


def make_person_browser_command(
    collection_path: Path,
    person_name: str,
    *,
    hide_out_of_focus: bool = False,
) -> list[str]:
    command = bildebank_command("--target", collection_path, "make-person-browser", person_name)
    if hide_out_of_focus:
        command.append("--hide-out-of-focus")
    return command


def make_people_browser_command(collection_path: Path, *, hide_out_of_focus: bool = False) -> list[str]:
    command = bildebank_command("--target", collection_path, "make-people-browser")
    if hide_out_of_focus:
        command.append("--hide-out-of-focus")
    return command


def vacuum_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "vacuum")


def migrate_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "migrate")


def cleanup_pending_deletes_list_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "cleanup-pending-deletes", "--list")


def cleanup_pending_deletes_apply_command(collection_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "cleanup-pending-deletes", "--apply")


def check_source_command(collection_path: Path, source_path: Path) -> list[str]:
    return bildebank_command("--target", collection_path, "check-source", source_path)


def rescan_source_command(collection_path: Path, source_name: str) -> list[str]:
    return bildebank_command("--target", collection_path, "rescan-source", "--name", source_name)


def unimport_source_command(collection_path: Path, source_name: str) -> list[str]:
    return bildebank_command("--target", collection_path, "unimport", "--name", source_name)


def unimport_source_dry_run_command(
    collection_path: Path,
    source_name: str,
    *,
    target_change_report_json: Path | None = None,
) -> list[str]:
    command = bildebank_command("--target", collection_path, "unimport", "--dry-run", "--name", source_name)
    if target_change_report_json is not None:
        command.extend(["--target-change-report-json", str(target_change_report_json)])
    return command


def read_unimport_target_change_report(report_path: Path) -> list[str]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    changed_targets = payload.get("changed_targets", [])
    if not isinstance(changed_targets, list):
        raise ValueError("uventet target-change-rapport fra unimport dry-run")
    paths: list[str] = []
    for item in changed_targets:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("uventet target-change-rad fra unimport dry-run")
        paths.append(item["path"])
    return paths


def export_person_command(
    collection_path: Path,
    person_name: str,
    destination_root: Path,
    *,
    dry_run: bool = False,
) -> list[str]:
    command = bildebank_command("--target", collection_path, "export-person", person_name, "--dest", destination_root)
    if dry_run:
        command.append("--dry-run")
    return command


def program_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def insightface_install_command(repo_root: Path | None = None) -> list[str]:
    script_path = (repo_root or program_repo_root()) / "install-insightface.ps1"
    return ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]


def openclip_install_command(repo_root: Path | None = None) -> list[str]:
    script_path = (repo_root or program_repo_root()) / "install-openclip.ps1"
    return ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]


def download_face_model_command() -> list[str]:
    return bildebank_command("download-face-model")


def insightface_install_supported() -> bool:
    return os.name == "nt"


def openclip_install_supported() -> bool:
    return os.name == "nt"


def dependency_setup_button_state(
    *,
    enabled: bool,
    migration_required: bool,
    migration_status_error: str | None,
    install_supported: bool,
) -> str:
    if enabled and not migration_required and migration_status_error is None and install_supported:
        return "normal"
    return "disabled"


def face_model_download_button_state(
    *,
    enabled: bool,
    migration_required: bool,
    migration_status_error: str | None,
    insightface_status: InsightFaceDependencyStatus,
) -> str:
    if (
        enabled
        and not migration_required
        and migration_status_error is None
        and insightface_status.status == "Klar"
    ):
        return "normal"
    return "disabled"


def close_blocked_by_running_command(busy: bool) -> bool:
    return busy


def interruptible_command_creationflags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def interrupt_process(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        ctrl_break_event = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break_event is not None:
            process.send_signal(ctrl_break_event)
            return
    else:
        process.send_signal(signal.SIGINT)
        return
    process.terminate()


def openclip_dependency_status() -> str:
    if importlib.util.find_spec("open_clip") is not None:
        return "Installert"
    return "Mangler"


def openclip_model_status(repo_root: Path | None = None) -> OpenClipModelStatus:
    from .config import load_config

    config = load_config(repo_root or program_repo_root()).openclip
    if _openclip_model_files_exist(config.model_root):
        return OpenClipModelStatus(config.model_name, config.pretrained, "Tilgjengelig", str(config.model_root))
    return OpenClipModelStatus(config.model_name, config.pretrained, "Mangler", str(config.model_root))


def _openclip_model_files_exist(model_root: Path) -> bool:
    if not model_root.is_dir():
        return False
    model_extensions = {".bin", ".pt", ".pth", ".safetensors"}
    return any(path.is_file() and path.suffix.lower() in model_extensions for path in model_root.rglob("*"))


def insightface_dependency_status() -> InsightFaceDependencyStatus:
    from .face import insightface_runtime_error

    insightface_error = insightface_runtime_error()
    onnxruntime_available = importlib.util.find_spec("onnxruntime") is not None

    if insightface_error is None and onnxruntime_available:
        return InsightFaceDependencyStatus("Klar")

    if insightface_error is not None and not _insightface_error_means_missing(insightface_error):
        return InsightFaceDependencyStatus("Feil", insightface_error)

    missing = []
    if insightface_error is not None:
        missing.append("insightface")
    if not onnxruntime_available:
        missing.append("onnxruntime")
    return InsightFaceDependencyStatus("Mangler", "Mangler: " + ", ".join(missing))


def _insightface_error_means_missing(message: str) -> bool:
    return (
        "InsightFace er ikke installert" in message
        or "No module named 'insightface" in message
    )


def insightface_model_status(repo_root: Path | None = None) -> InsightFaceModelStatus:
    from .config import load_config
    from .face import insightface_model_files_exist

    config = load_config(repo_root or program_repo_root()).face_recognition
    if insightface_model_files_exist(config):
        return InsightFaceModelStatus(config.model_name, "Lastet ned", str(config.model_root))
    return InsightFaceModelStatus(config.model_name, "Mangler", str(config.model_root))


def registered_sources(collection_path: Path) -> list[db.Source]:
    conn = db.connect(collection_path)
    try:
        return db.get_sources(conn)
    finally:
        conn.close()


def registered_persons(collection_path: Path) -> list[RegisteredPerson]:
    from .face import list_persons

    config = load_config(program_repo_root()).face_recognition
    return [
        RegisteredPerson(
            name=str(row["name"]),
            confirmed_file_count=int(row["confirmed_file_count"]),
            face_count=int(row["face_count"]),
            suggestion_count=int(row["suggestion_count"]),
            updated_at=str(row["updated_at"]),
        )
        for row in list_persons(collection_path, config)
    ]


def migration_plan_needs_action(plan: db.MigrationPlan) -> bool:
    return (
        plan.current_version != plan.target_version
        or plan.refreshes_performance_indexes
        or bool(plan.internal_repairs)
    )


def collection_needs_migration(collection_path: Path) -> bool:
    return migration_plan_needs_action(db.migration_plan(collection_path))


def rescan_source_candidates(sources: list[db.Source]) -> list[db.Source]:
    return list(sources)


def subprocess_output_encoding() -> str:
    return locale.getpreferredencoding(False) or "utf-8"


def progress_log_key(message: str) -> str | None:
    if _is_tqdm_progress_line(message):
        return "tqdm-progress"
    for label in PROGRESS_LOG_LABELS:
        prefix = f"{label}:"
        rest = message[len(prefix):] if message.startswith(prefix) else ""
        if rest and not rest.lstrip().startswith("ferdig") and ("=" in rest or _contains_progress_count(rest)):
            return label
    return None


def _is_tqdm_progress_line(message: str) -> bool:
    stripped = message.lstrip()
    percent, separator, rest = stripped.partition("%|")
    return bool(separator) and percent.isdigit() and _contains_progress_count(rest)


def _contains_progress_count(message: str) -> bool:
    parts = message.replace(",", " ").split()
    return any(_is_progress_count(part) for part in parts)


def _is_progress_count(part: str) -> bool:
    current, separator, total = part.partition("/")
    return bool(separator) and current.isdigit() and total.isdigit()


class Tooltip:
    def __init__(self, widget: Any, text: str, *, delay_ms: int = 500) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.after_id: str | None = None
        self.window: Any | None = None

        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        self._cancel()
        self.after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self.after_id is None:
            return
        self.widget.after_cancel(self.after_id)
        self.after_id = None

    def _show(self) -> None:
        import tkinter as tk

        self.after_id = None
        if self.window is not None or not self.widget.winfo_exists():
            return

        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{x}+{y}")

        label = tk.Label(
            window,
            text=self.text,
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=3,
            justify="left",
            wraplength=360,
        )
        label.pack()
        self.window = window

    def hide(self, _event: Any = None) -> None:
        self._cancel()
        if self.window is None:
            return
        self.window.destroy()
        self.window = None


class BildebankLauncher:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.config = load_launcher_config()
        self.collection_path = self.config.collection_path
        self.busy = False
        self.server_process: subprocess.Popen[str] | None = None
        self.active_command_process: subprocess.Popen[str] | None = None
        self.active_command_cancel_requested = False
        self.active_command_cancellable = False
        self.closing = False

        self.root = tk.Tk()
        self.root.title("Bildebank kontrollpanel")
        self.root.minsize(640, 460)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.collection_value: tk.StringVar = tk.StringVar(value="Bildesamling: " + str(self.collection_path))
        self.status_value: tk.StringVar = tk.StringVar(value="")
        self.insightface_status_value: tk.StringVar = tk.StringVar(value="")
        self.insightface_model_status_value: tk.StringVar = tk.StringVar(value="")
        self.openclip_status_value: tk.StringVar = tk.StringVar(value="")
        self.openclip_model_status_value: tk.StringVar = tk.StringVar(value="")
        self.static_browser_hide_out_of_focus_var: tk.BooleanVar = tk.BooleanVar(value=False)
        self.notebook: ttk.Notebook | None = None
        self.main_tab: ttk.Frame | None = None
        self.import_tab: ttk.Frame | None = None
        self.tools_tab: ttk.Frame | None = None
        self.setup_tab: ttk.Frame | None = None
        self.main_button_frame: ttk.Frame | None = None
        self.import_button_frame: ttk.Frame | None = None
        self.tools_button_frame: ttk.Frame | None = None
        self.log_text: tk.Text | None = None
        self.buttons: list[Any] = []
        self.choose_collection_button: ttk.Button | None = None
        self.create_collection_button: ttk.Button | None = None
        self.install_insightface_button: ttk.Button | None = None
        self.install_openclip_button: ttk.Button | None = None
        self.download_face_model_button: ttk.Button | None = None
        self.update_button: ttk.Button | None = None
        self.update_button_icons: dict[str, ImageTk.PhotoImage] = {}
        self.cancel_command_button: ttk.Button | None = None
        self.exit_button: ttk.Button | None = None
        self.tooltips: list[Tooltip] = []
        self.pending_deletes_status: str = "Ukjent"
        self.pending_deletes_count: int | None = None
        self.migration_required = False
        self.migration_status_error: str | None = None
        self.migration_dialog_shown = False
        self.update_status = LauncherUpdateStatus("checking")
        self.update_checking = False
        self.dependency_status_refreshing = False
        self.insightface_status = InsightFaceDependencyStatus("Sjekker")
        self.face_model_status = InsightFaceModelStatus("", "Sjekker")
        self.openclip_status = "Sjekker"
        self.openclip_model_status = OpenClipModelStatus("", "", "Sjekker")
        self.active_progress_log_key: str | None = None
        self.active_progress_log_range: tuple[str, str] | None = None
        self._set_dependency_status_placeholder()
        self.update_button_icons = self._load_update_button_icons()

        self._build_gui()
        self._update_migration_status()
        self._refresh_state()
        self._start_update_status_refresh()
        self._start_dependency_status_refresh()
        self._log(f"Valgt bildesamling: {self.collection_path}")
        if self.migration_required:
            self.root.after(0, self._show_migration_required_dialog)
        elif self.migration_status_error is not None:
            self.root.after(0, self._show_migration_status_error)
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

    def run(self) -> None:
        self.root.mainloop()

    def _post_to_tk(self, callback: Callable[[], None]) -> bool:
        if self.closing:
            return False

        def guarded_callback() -> None:
            if self.closing:
                return
            try:
                if not self.root.winfo_exists():
                    return
            except self.tk.TclError:
                return
            callback()

        try:
            self.root.after(0, guarded_callback)
        except (RuntimeError, self.tk.TclError):
            return False
        return True

    def _destroy_root(self) -> None:
        self.closing = True
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass

    def _on_close(self) -> None:
        if close_blocked_by_running_command(self.busy):
            from tkinter import messagebox

            message = "Vent til jobben som kjører er ferdig før du lukker kontrollpanelet."
            self._log(message)
            messagebox.showinfo("Bildebank jobber", message, parent=self.root)
            return
        self.closing = True
        self._stop_server_process()
        self._destroy_root()

    def _stop_server_process(self) -> None:
        process = self.server_process
        if process is None:
            return
        if process.poll() is not None:
            self.server_process = None
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
        self._log("Bildebank-server stoppet.")

    def _build_gui(self) -> None:
        tk = self.tk
        ttk = self.ttk

        ttk.Style(self.root).configure(BUTTON_STYLE, padding=BUTTON_PADDING)

        # Ytterste padding i vinduet.
        outer = ttk.Frame(self.root, padding=PAD_OUTER)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        title = ttk.Label(outer, text="Bildebank kontrollpanel", font=("", 15, "bold"))
        title.grid(row=0, column=0, sticky="w")

        self.notebook = ttk.Notebook(outer)
        # pady er padding over og under notebooken.
        self.notebook.grid(row=1, column=0, sticky="ew", pady=(PAD))

        # padding er paddingen inni hver side av notebook
        self.main_tab = ttk.Frame(self.notebook, padding=PAD)
        self.import_tab = ttk.Frame(self.notebook, padding=PAD)
        self.tools_tab = ttk.Frame(self.notebook, padding=PAD)
        self.setup_tab = ttk.Frame(self.notebook, padding=PAD)
        self.notebook.add(self.main_tab, text="Bildebank")
        self.notebook.add(self.import_tab, text="Import av bilder")
        self.notebook.add(self.tools_tab, text="Verktøy")
        self.notebook.add(self.setup_tab, text="Oppsett")

        self.main_tab.columnconfigure(0, weight=1)
        self.import_tab.columnconfigure(0, weight=1)
        self.tools_tab.columnconfigure(0, weight=1)
        self.setup_tab.columnconfigure(0, weight=1)

        collection_frame = ttk.Frame(self.main_tab)
        collection_frame.grid(row=0, column=0, sticky="w")

        collection_label = ttk.Label(collection_frame, textvariable=self.collection_value, wraplength=560)
        collection_label.grid(row=0, column=0, sticky="w", padx=PADX, pady=PADY, columnspan=2)

        self.choose_collection_button = self._button(
            collection_frame,
            text="Velg annen plassering",
            command=self._choose_collection,
        )
        self.choose_collection_button.grid(row=1, column=0, sticky="w", padx=PADX, pady=PADY)
        self.create_collection_button = self._button(
            collection_frame,
            text="Opprett bildesamling",
            command=self._create_collection,
        )
        self._add_tooltip(
            self.create_collection_button,
            "Lag en bildesamling på stedet vist til venstre. Klikk 'Velg annen plassering' "
            "for å finne bildesamlingen din eller opprette en ny et annet sted."
        )
        self.create_collection_button.grid(row=1, column=1, sticky="w", padx=PADX, pady=PADY)

        separator = ttk.Separator(self.main_tab, orient="horizontal")
        separator.grid(row=1, column=0, sticky="ew", pady=PAD)

        self.main_button_frame = ttk.Frame(self.main_tab)
        self.main_button_frame.grid(row=2, column=0, sticky="w")

        self.import_button_frame = ttk.Frame(self.import_tab)
        self.import_button_frame.grid(row=0, column=0, sticky="w")

        insightface_frame = ttk.Frame(self.setup_tab)
        insightface_frame.grid(row=0, column=0, sticky="w")
        ttk.Label(insightface_frame, textvariable=self.insightface_status_value).grid(
            row=0,
            column=0,
            sticky="e",
            padx=PAD,
        )
        self.install_insightface_button = self._button(
            insightface_frame,
            text="Installer InsightFace",
            command=self._install_insightface,
        )
        self.install_insightface_button.grid(row=0, column=1, sticky="w", pady=PADY)
        ttk.Label(insightface_frame, textvariable=self.insightface_model_status_value).grid(
            row=1,
            column=0,
            sticky="e",
            padx=(0, 12),
        )
        self.download_face_model_button = self._button(
            insightface_frame,
            text="Last ned modell",
            command=self._download_face_model,
        )
        self.download_face_model_button.grid(row=1, column=1, sticky="w", pady=PADY)

        setup_separator = ttk.Separator(self.setup_tab, orient="horizontal")
        setup_separator.grid(row=1, column=0, sticky="ew", pady=PAD)

        openclip_frame = ttk.Frame(self.setup_tab)
        openclip_frame.grid(row=2, column=0, sticky="w")
        self.install_openclip_button = self._button(
            openclip_frame,
            text="Installer OpenCLIP",
            command=self._install_openclip,
        )
        self.install_openclip_button.grid(row=0, column=0, sticky="w")
        ttk.Label(openclip_frame, textvariable=self.openclip_status_value).grid(
            row=0,
            column=1,
            sticky="w",
            padx=PAD,
        )
        ttk.Label(openclip_frame, textvariable=self.openclip_model_status_value).grid(
            row=0,
            column=2,
            sticky="w",
        )

        self.tools_button_frame = ttk.Frame(self.tools_tab)
        self.tools_button_frame.grid(row=2, column=0, sticky="w")

        log_frame = ttk.Frame(outer)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        ttk.Label(log_frame, text="Logg:").grid(row=0, column=0, sticky="w")

        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")

        footer = ttk.Frame(outer)
        footer.grid(row=3, column=0, sticky="ew", pady=(PAD, 0))
        footer.columnconfigure(0, weight=1)
        status = ttk.Label(footer, textvariable=self.status_value)
        status.grid(row=0, column=0, sticky="w")
        self.cancel_command_button = self._button(
            footer,
            text="Avbryt jobb",
            command=self._cancel_active_command,
        )
        self.cancel_command_button.grid(row=0, column=1, sticky="e", padx=(0, PADX))
        self.exit_button = self._button(
            footer,
            text="Avslutt bildebank kontrollpanel",
            command=self._on_close,
        )
        self.exit_button.grid(row=0, column=2, sticky="e")

    def _refresh_state(self) -> None:
        assert self.main_button_frame is not None
        assert self.import_button_frame is not None
        assert self.tools_button_frame is not None

        for tooltip in self.tooltips:
            tooltip.hide()
        self.tooltips = []
        for frame in (self.main_button_frame, self.import_button_frame, self.tools_button_frame):
            for child in frame.winfo_children():
                child.destroy()
        self.buttons = []
        self.update_button = None
        if self.create_collection_button is not None:
            self.create_collection_button.grid_remove()

        if is_collection_created(self.collection_path):
            if self.migration_required:
                self.pending_deletes_status = "Ukjent"
                self.pending_deletes_count = None
                migrate_button = self._button(self.main_button_frame, text="Migrer", command=self._run_migrate)
                migrate_button.grid(row=0, column=0, padx=PADX, pady=PADY, sticky="ew")
                exit_button = self._button(
                    self.main_button_frame,
                    text="Avslutt uten å migrere",
                    command=self._on_close,
                )
                exit_button.grid(row=0, column=1, padx=PADX, pady=PADY, sticky="ew")
                self.buttons.extend([migrate_button, exit_button])
            elif self.migration_status_error is not None:
                self.pending_deletes_status = "Ukjent"
                self.pending_deletes_count = None
            else:
                self._refresh_pending_deletes_status()
                start_button = self._button(
                    self.main_button_frame,
                    text="Start Bildebank i nettleser",
                    command=self._start_server,
                )
                start_button.grid(row=0, column=0, padx=PADX, pady=PADY, columnspan=2, sticky="ew")
                update_button = self._button(
                    self.main_button_frame,
                    text=self._update_button_text(),
                    command=self._on_update_button_clicked,
                )
                update_button.grid(row=0, column=2, padx=PADX, pady=PADY, sticky="ew")
                self.update_button = update_button
                self._add_tooltip(
                    update_button,
                    "Oppdaterer Bildebank til siste utgave. "
                    "Dette tilsvarer kommandoen 'bildebank update' ",
                )

                import_button = self._button(
                    self.import_button_frame,
                    text="Importer bilder",
                    command=self._start_import_flow,
                )
                import_button.grid(row=0, column=0, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    import_button,
                    "Registrerer og importerer bildene fra en mappe, USB-brikke, CD eller disk."
                )
                unimport_button = self._button(
                    self.import_button_frame,
                    text="Angre import",
                    command=self._start_unimport_source_flow,
                )
                unimport_button.grid(row=0, column=1, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    unimport_button,
                    "Reverser en tidligere import. Kontrollerer først at alle registrerte kildefiler "
                    "fortsatt finnes med samme innhold. Krever nøyaktig bekreftelse før noe endres."
                )
                rescan_button = self._button(
                    self.import_button_frame,
                    text="Rescan kilde",
                    command=self._start_rescan_source_flow,
                )
                rescan_button.grid(row=0, column=2, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    rescan_button,
                    "Scan en mappe du har importert bilder fra en gang til. Bruk dette hvis bildebank "
                    "har blitt forbedret, og nå støtter flere bildefiler."
                )
                check_button = self._button(
                    self.import_button_frame,
                    text="Sjekk kilde",
                    command=self._start_check_source_flow,
                )
                check_button.grid(row=0, column=3, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    check_button,
                    "Sjekker at filene i en kildemappe finnes i bildesamlingen med samme SHA-256. "
                    "Hvis alle filene i mappen du har importert fra finnes i bildesamlingen "
                    "så er det i prinsippet trygt å slette mappen du importerte bildene fra.",
                )
                geo_button = self._button(
                    self.tools_button_frame,
                    text="Les GPS fra bilder",
                    command=self._run_geo_scan,
                )
                geo_button.grid(row=0, column=0, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    geo_button,
                    "Scann bildene med exiftool for å finne ut hvor bildene ble tatt."
                )
                thumbs_button = self._button(
                    self.tools_button_frame,
                    text="Lag miniatyrbilder",
                    command=self._run_make_thumbnails,
                )
                thumbs_button.grid(row=0, column=1, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    thumbs_button,
                    "Lag småbilder av alle bildene som kan brukes for at månedsvisning skal laste raskere."
                )
                static_browser_button = self._button(
                    self.tools_button_frame,
                    text="Lag HTML-browser",
                    command=self._run_make_browser,
                )
                static_browser_button.grid(row=2, column=1, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    static_browser_button,
                    "Lag en statisk index.html i bildesamlingen som kan åpnes uten Bildebank-server.",
                )
                static_person_browser_button = self._button(
                    self.tools_button_frame,
                    text="Lag personbrowser",
                    command=self._start_make_person_browser_flow,
                )
                static_person_browser_button.grid(row=2, column=2, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    static_person_browser_button,
                    "Lag en statisk HTML-browser for en valgt person.",
                )
                static_people_browser_button = self._button(
                    self.tools_button_frame,
                    text="Lag alle personbrowsere",
                    command=self._run_make_people_browser,
                )
                static_people_browser_button.grid(row=2, column=3, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    static_people_browser_button,
                    "Lag statiske HTML-browsere for alle registrerte personer.",
                )
                static_browser_hide_checkbox = self.ttk.Checkbutton(
                    self.tools_button_frame,
                    text='Skjul "Ute av fokus"',
                    variable=self.static_browser_hide_out_of_focus_var,
                )
                static_browser_hide_checkbox.grid(
                    row=3,
                    column=1,
                    columnspan=3,
                    padx=PADX,
                    pady=PADY,
                    sticky="w",
                )
                self._add_tooltip(
                    static_browser_hide_checkbox,
                    "Når dette er valgt, får de statiske HTML-browserkommandoene "
                    "flagget --hide-out-of-focus.",
                )
                face_button = self._button(
                    self.tools_button_frame,
                    text="Finn ansikter",
                    command=self._run_face_scan,
                )
                face_button.grid(row=0, column=2, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    face_button,
                    "Kjører 'bildebank face-scan'. Denne kommandoen scanner bildene etter ansikter. "
                    "Må kjøres på nytt når du legger til nye biler."
                )
                image_scan_button = self._button(
                    self.tools_button_frame,
                    text="Klargjør bildesøk",
                    command=self._run_image_scan,
                )
                image_scan_button.grid(row=0, column=3, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    image_scan_button,
                    "Kjører 'bildebank image-scan'. Denne kommandoen gjør at du "
                    "kan gjøre klikke Bildesøk i nettleseren og skrive søkeord der. "
                    "Kommandoen må scanne nye bilder for at det kan søkes i dem."
                )
                doctor_button = self._button(
                    self.tools_button_frame,
                    text="Sjekk bildebank",
                    command=self._run_doctor,
                )
                doctor_button.grid(row=1, column=0, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    doctor_button,
                    "Kjør en status-sjekk av Bildebank og bildesamlingen. "
                    "Du kan få forslag til tiltak som må gjøres."
                )
                deep_doctor_button = self._button(
                    self.tools_button_frame,
                    text="Grundig sjekk",
                    command=self._run_deep_doctor,
                )
                deep_doctor_button.grid(row=1, column=1, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    deep_doctor_button,
                    "Kjør en status-sjekk av Bildebank og bildesamlingen. "
                    "Denne kjører en enda grundigere sjekk, og kan ta litt tid å fullføre. "
                    "Du kan få forslag til tiltak som må gjøres. "
                )
                vacuum_button = self._button(
                    self.tools_button_frame,
                    text="Rydd databaser",
                    command=self._run_vacuum,
                )
                vacuum_button.grid(row=1, column=2, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    vacuum_button,
                    "Bildebank reduserer størrelsen på databasene, hvis mulig."
                )
                pending_button = self._button(
                    self.tools_button_frame,
                    text=self._pending_deletes_button_text(),
                    command=self._show_pending_deletes,
                )
                self._add_tooltip(
                    pending_button,
                    "Hvis det finnes filer her, så har en jobb som skulle flytte eller slette "
                    "blitt avbrutt. Knappen brukes til å fullføre jobben på en trygg måte.",
                )
                pending_button.grid(row=1, column=3, padx=PADX, pady=PADY, sticky="ew")
                export_person_button = self._button(
                    self.tools_button_frame,
                    text="Eksporter person",
                    command=self._start_export_person_flow,
                )
                export_person_button.grid(row=2, column=0, padx=PADX, pady=PADY, sticky="ew")
                self._add_tooltip(
                    export_person_button,
                    "Eksporter en kopi av alle bildene som vises på siden til en person i bildebrowseren.",
                )
                self.buttons.extend(
                    [
                        start_button,
                        update_button,
                        import_button,
                        rescan_button,
                        check_button,
                        unimport_button,
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
                    ]
                )
        else:
            self.pending_deletes_status = "Ukjent"
            self.pending_deletes_count = None
            assert self.create_collection_button is not None
            self.create_collection_button.grid()
            self.buttons.append(self.create_collection_button)

        self._set_buttons_enabled(not self.busy)

    def _button(self, parent: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("style", BUTTON_STYLE)
        return self.ttk.Button(parent, **kwargs)

    def _add_tooltip(self, widget: Any, text: str) -> None:
        self.tooltips.append(Tooltip(widget, text))

    def _refresh_pending_deletes_status(self) -> None:
        try:
            rows = list_pending_deletes(self.collection_path)
        except Exception as exc:  # noqa: BLE001 - launcher status must not block other buttons
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
                with Image.open(icon_root.joinpath(filename)) as image:
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
        if self.update_status.status == "checking" or self.busy:
            self.update_button.configure(state="disabled")

    def _set_dependency_status_placeholder(self) -> None:
        self.insightface_status_value.set("InsightFace: sjekker ...")
        self.insightface_model_status_value.set("Valgt modell: sjekker ...")
        self.openclip_status_value.set("open_clip: sjekker ...")
        self.openclip_model_status_value.set("AI-modell: sjekker ...")

    def _apply_dependency_status_values(self) -> None:
        self.insightface_status_value.set(f"InsightFace: {self.insightface_status.status}")
        self.insightface_model_status_value.set(
            f"Valgt modell: {self.face_model_status.model_name} ({self.face_model_status.status})"
        )
        self.openclip_status_value.set(f"open_clip: {self.openclip_status}")
        self.openclip_model_status_value.set(f"AI-modell: {self.openclip_model_status.status}")

    def _log_dependency_status_detail(self, label: str, status: str, detail: str) -> None:
        if status == "Feil" and detail:
            self._log(f"{label}-status feilet: {detail}")

    def _start_dependency_status_refresh(self) -> None:
        if self.dependency_status_refreshing:
            return
        self.dependency_status_refreshing = True
        self._set_dependency_status_placeholder()
        self._set_buttons_enabled(not self.busy)
        thread = threading.Thread(target=self._dependency_status_worker, daemon=True)
        thread.start()

    def _dependency_status_worker(self) -> None:
        insightface_status, face_model_status, openclip_status, openclip_model_status = self._load_dependency_status()
        self._post_to_tk(
            lambda: self._dependency_status_finished(
                insightface_status,
                face_model_status,
                openclip_status,
                openclip_model_status,
            )
        )

    def _load_dependency_status(
        self,
    ) -> tuple[InsightFaceDependencyStatus, InsightFaceModelStatus, str, OpenClipModelStatus]:
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
        return insightface_status, face_model_status, openclip_status, openclip_model_state

    def _dependency_status_finished(
        self,
        insightface_status: InsightFaceDependencyStatus,
        face_model_status: InsightFaceModelStatus,
        openclip_status: str,
        openclip_model_status: OpenClipModelStatus,
    ) -> None:
        self.dependency_status_refreshing = False
        self.insightface_status = insightface_status
        self.face_model_status = face_model_status
        self.openclip_status = openclip_status
        self.openclip_model_status = openclip_model_status
        self._log_dependency_status_detail("InsightFace", insightface_status.status, insightface_status.detail)
        self._log_dependency_status_detail("Ansiktsmodell", face_model_status.status, face_model_status.detail)
        self._log_dependency_status_detail("OpenCLIP-modell", openclip_model_status.status, openclip_model_status.detail)
        self._apply_dependency_status_values()
        self._set_buttons_enabled(not self.busy)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.buttons:
            button.configure(state=state)
        dependency_buttons_enabled = enabled and not self.migration_required and self.migration_status_error is None
        setup_buttons_enabled = enabled and not self.dependency_status_refreshing
        if self.choose_collection_button is not None:
            collection_state = "normal" if dependency_buttons_enabled else "disabled"
            self.choose_collection_button.configure(state=collection_state)
        if self.create_collection_button is not None and self.create_collection_button not in self.buttons:
            self.create_collection_button.configure(state="disabled")
        if self.install_insightface_button is not None:
            self.install_insightface_button.configure(
                state=dependency_setup_button_state(
                    enabled=setup_buttons_enabled,
                    migration_required=self.migration_required,
                    migration_status_error=self.migration_status_error,
                    install_supported=insightface_install_supported(),
                )
            )
        if self.install_openclip_button is not None:
            self.install_openclip_button.configure(
                state=dependency_setup_button_state(
                    enabled=setup_buttons_enabled,
                    migration_required=self.migration_required,
                    migration_status_error=self.migration_status_error,
                    install_supported=openclip_install_supported(),
                )
            )
        if self.download_face_model_button is not None:
            self.download_face_model_button.configure(
                state=face_model_download_button_state(
                    enabled=setup_buttons_enabled,
                    migration_required=self.migration_required,
                    migration_status_error=self.migration_status_error,
                    insightface_status=self.insightface_status,
                )
            )
        self._apply_update_button_state()
        if self.exit_button is not None:
            self.exit_button.configure(state=state)
        if self.cancel_command_button is not None:
            cancel_state = (
                "normal"
                if self.busy and self.active_command_cancellable and not self.active_command_cancel_requested
                else "disabled"
            )
            self.cancel_command_button.configure(state=cancel_state)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        self.status_value.set(message)
        self._set_buttons_enabled(not busy)

    def _cancel_active_command(self) -> None:
        process = self.active_command_process
        if process is None and self.busy and self.active_command_cancellable:
            self.active_command_cancel_requested = True
            self._set_buttons_enabled(False)
            self.status_value.set("Avbryter jobb ...")
            self._log("Ber jobben avbryte kontrollert ...")
            return
        if process is None or process.poll() is not None:
            return
        self.active_command_cancel_requested = True
        self._set_buttons_enabled(False)
        self.status_value.set("Avbryter jobb ...")
        self._log("Ber jobben avbryte kontrollert ...")
        try:
            interrupt_process(process)
        except OSError as exc:
            self._log(f"Kunne ikke avbryte jobben: {exc}")

    def _update_migration_status(self) -> None:
        self.migration_required = False
        self.migration_status_error = None
        if not is_collection_created(self.collection_path):
            return
        try:
            self.migration_required = collection_needs_migration(self.collection_path)
        except Exception as exc:  # noqa: BLE001 - launcher must show a controlled startup error
            self.migration_status_error = str(exc)

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
        self.ttk.Label(frame, text="Migrering kreves", font=("", 12, "bold")).grid(row=0, column=0, sticky="w")
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
        self._stop_server_process()
        self.collection_path = Path(selected)
        self.collection_value.set("Bildesamling: " + str(self.collection_path))
        self.config = LauncherConfig(collection_path=self.collection_path)
        try:
            save_launcher_config(self.config)
        except OSError as exc:
            self._show_error("Kunne ikke lagre valgt plassering.", exc)
        self._log(f"Valgt bildesamling: {self.collection_path}")
        self.migration_dialog_shown = False
        self._update_migration_status()
        self._refresh_state()
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
            on_success=self._refresh_state,
        )

    def _run_geo_scan(self) -> None:
        self._log("Scanner GPS-metadata ...")
        self._run_waiting_command(
            geo_scan_command(self.collection_path),
            running_message="Scanner GPS-metadata ...",
            success_message="GPS-scan fullført.",
            failure_message="GPS-scan feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _run_face_scan(self) -> None:
        self._log("Scanner ansikter ...")
        self._run_waiting_command(
            face_scan_command(self.collection_path),
            running_message="Scanner ansikter ...",
            success_message="Ansiktsscan fullført.",
            failure_message="Ansiktsscan feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _run_image_scan(self) -> None:
        self._log("Scanner bilder for bildesøk ...")
        self._run_waiting_command(
            image_scan_command(self.collection_path),
            running_message="Scanner bilder for bildesøk ...",
            success_message="Bildesøk-scan fullført.",
            failure_message="Bildesøk-scan feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _run_make_thumbnails(self) -> None:
        self._log("Lager thumbnails ...")
        self._run_waiting_command(
            make_thumbnails_command(self.collection_path),
            running_message="Lager thumbnails ...",
            success_message="Thumbnails fullført.",
            failure_message="Thumbnail-jobb feilet.",
            on_success=self._refresh_state,
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
            on_success=self._refresh_state,
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
            on_success=self._refresh_state,
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
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _run_doctor(self) -> None:
        self._log("Kjører doctor ...")
        self._run_waiting_command(
            doctor_command(self.collection_path),
            running_message="Kjører doctor ...",
            success_message="Doctor fullført.",
            failure_message="Doctor feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _run_deep_doctor(self) -> None:
        self._log("Kjører grundig doctor ...")
        self._run_waiting_command(
            deep_doctor_command(self.collection_path),
            running_message="Kjører grundig doctor ...",
            success_message="Grundig doctor fullført.",
            failure_message="Grundig doctor feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _run_vacuum(self) -> None:
        self._log("Pakker Bildebank-databaser ...")
        self._run_waiting_command(
            vacuum_command(self.collection_path),
            running_message="Pakker Bildebank-databaser ...",
            success_message="Vacuum fullført.",
            failure_message="Vacuum feilet.",
            on_success=self._refresh_state,
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
        self._update_migration_status()
        self.migration_dialog_shown = False
        self._refresh_state()
        if self.migration_required:
            self._show_migration_required_dialog()
        elif self.migration_status_error is not None:
            self._show_migration_status_error()

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
        self._refresh_state()
        if not self.pending_deletes_count:
            messagebox.showinfo(
                "Ventende filsletting",
                "Ingen ventende filslettinger.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Ventende filsletting",
            (
                "Listen over ventende filslettinger står i loggen.\n\n"
                "Vil du prøve å rydde opp nå?"
            ),
            parent=self.root,
        ):
            self._log("Opprydding av ventende filsletting avbrutt.")
            return
        self._confirm_cleanup_pending_deletes()

    def _confirm_cleanup_pending_deletes(self) -> None:
        from tkinter import simpledialog

        confirmation = simpledialog.askstring(
            "Bekreft ventende filsletting",
            'Skriv "ja, rydd opp" for å gjennomføre opprydding.',
            parent=self.root,
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
            on_success=self._refresh_state,
        )

    def _run_update(self) -> None:
        from tkinter import messagebox

        if not messagebox.askyesno(
            "Oppdater Bildebank?",
            (
                "Bildebank-serveren stoppes hvis den kjører. Etter oppdateringen "
                "starter kontrollpanelet på nytt."
            ),
            parent=self.root,
        ):
            self._log("Oppdatering avbrutt.")
            return
        self._stop_server_process()
        self._log("Oppdaterer Bildebank ...")
        self._run_waiting_command(
            update_command(),
            running_message="Oppdaterer Bildebank ...",
            success_message="Bildebank er oppdatert. Starter kontrollpanelet på nytt ...",
            failure_message="Oppdatering feilet.",
            on_success=self._restart_launcher,
        )

    def _on_update_button_clicked(self) -> None:
        if self.update_status.status == "available":
            self._run_update()
            return
        self._start_update_status_refresh()

    def _start_update_status_refresh(self) -> None:
        if self.update_checking:
            return
        self.update_checking = True
        self.update_status = LauncherUpdateStatus("checking")
        self._apply_update_button_state()
        self._set_buttons_enabled(not self.busy)
        thread = threading.Thread(target=self._update_status_worker, daemon=True)
        thread.start()

    def _update_status_worker(self) -> None:
        status = check_launcher_update_status()
        self._post_to_tk(lambda: self._update_status_finished(status))

    def _update_status_finished(self, status: LauncherUpdateStatus) -> None:
        self.update_checking = False
        self.update_status = status
        if status.status == "error" and status.detail:
            self._log(f"Oppdateringssjekk feilet: {status.detail}")
        elif status.status == "skipped" and status.detail:
            self._log(f"Oppdateringssjekk hoppet over: {status.detail}")
        self._apply_update_button_state()
        self._set_buttons_enabled(not self.busy)

    def _restart_launcher(self) -> None:
        from tkinter import messagebox

        try:
            subprocess.Popen(launcher_command())
        except OSError as exc:
            messagebox.showerror("Kunne ikke starte kontrollpanelet", "Kontrollpanelet kunne ikke startes på nytt.")
            self._log(f"Kunne ikke starte kontrollpanelet på nytt: {exc}")
            return
        self._log("Nytt kontrollpanel startet. Lukker dette vinduet.")
        self._destroy_root()

    def _install_insightface(self) -> None:
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
        self._run_waiting_command(
            insightface_install_command(),
            running_message="Installerer InsightFace ...",
            success_message="InsightFace-installasjon fullført.",
            failure_message="InsightFace-installasjon feilet.",
            on_success=self._insightface_install_finished,
        )

    def _insightface_install_finished(self) -> None:
        importlib.invalidate_caches()
        self._start_dependency_status_refresh()

    def _install_openclip(self) -> None:
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
        self._run_waiting_command(
            openclip_install_command(),
            running_message="Installerer OpenCLIP ...",
            success_message="OpenCLIP-installasjon fullført.",
            failure_message="OpenCLIP-installasjon feilet.",
            on_success=self._openclip_install_finished,
        )

    def _openclip_install_finished(self) -> None:
        importlib.invalidate_caches()
        self._start_dependency_status_refresh()

    def _download_face_model(self) -> None:
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
        self._run_waiting_command(
            download_face_model_command(),
            running_message="Laster ned ansiktsmodell ...",
            success_message="Ansiktsmodell lastet ned.",
            failure_message="Nedlasting av ansiktsmodell feilet.",
            on_success=self._start_dependency_status_refresh,
        )

    def _confirm_rerun(self, title: str, message: str) -> bool:
        from tkinter import messagebox

        return bool(messagebox.askyesno(title, message, parent=self.root))

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
            on_success=self._refresh_state,
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
            on_success=self._refresh_state,
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
        from tkinter import messagebox, simpledialog

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

        messagebox.showwarning(
            "Unimport",
            (
                "Dry-run er fullført og planen står i loggen.\n\n"
                "Unimport kan fjerne filer fra den aktive bildesamlingen."
            ),
        )
        confirmation = simpledialog.askstring(
            "Bekreft unimport",
            f'Skriv "ja, det vil jeg" for å unimporte kilden:\n{source.name}',
            parent=self.root,
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
                "Endrede målfiler",
                (
                    "Noen målfil(er) i bildebanken er endret siden import.\n\n"
                    "Kildefilene er verifisert, men disse målfilene matcher ikke "
                    "lenger databaseført størrelse/SHA-256 og kan inneholde "
                    f"manuelle endringer:\n\n{preview}\n\n"
                    "Fortsette unimport og la disse filene slettes?"
                ),
                parent=self.root,
            ):
                self._log(f'Unimport avbrutt for kilde "{source.name}": endrede målfiler.')
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
            on_success=self._refresh_state,
        )

    def _load_registered_sources(self) -> list[db.Source] | None:
        from tkinter import messagebox

        try:
            return registered_sources(self.collection_path)
        except Exception as exc:  # noqa: BLE001 - GUI should show readable errors
            messagebox.showerror("Kunne ikke lese kilder", "Kunne ikke lese registrerte kilder.")
            self._log(f"Kunne ikke lese registrerte kilder: {exc}")
            return None

    def _load_registered_persons(self) -> list[RegisteredPerson] | None:
        from tkinter import messagebox

        try:
            return registered_persons(self.collection_path)
        except Exception as exc:  # noqa: BLE001 - GUI should show readable errors
            messagebox.showerror("Kunne ikke lese personer", "Kunne ikke lese registrerte personer.")
            self._log(f"Kunne ikke lese registrerte personer: {exc}")
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
        tk = self.tk
        ttk = self.ttk

        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.minsize(760, 320)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)

        frame = ttk.Frame(dialog, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        columns = ("id", "status", "name", "path")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", height=10)
        tree.heading("id", text="ID")
        tree.heading("status", text="Status")
        tree.heading("name", text="Navn")
        tree.heading("path", text="Mappe")
        tree.column("id", width=55, stretch=False, anchor="e")
        tree.column("status", width=105, stretch=False)
        tree.column("name", width=180, stretch=True)
        tree.column("path", width=380, stretch=True)

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        source_by_item: dict[str, db.Source] = {}
        for source in sources:
            item_id = tree.insert(
                "",
                "end",
                values=(source.id, source.status, source.name, str(source.path)),
            )
            source_by_item[item_id] = source
        first_item = tree.get_children()
        if first_item:
            tree.selection_set(first_item[0])
            tree.focus(first_item[0])

        def accept() -> None:
            selected = tree.selection()
            if not selected:
                return
            source = source_by_item[selected[0]]
            dialog.withdraw()
            dialog.destroy()
            self.root.lift()
            self.root.focus_force()
            self.root.after_idle(lambda: on_select(source))

        def cancel() -> None:
            dialog.withdraw()
            dialog.destroy()
            self.root.lift()
            self.root.focus_force()
            self.root.after_idle(on_cancel)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self._button(button_frame, text="Avbryt", command=cancel).grid(row=0, column=0, padx=(0, 8))
        self._button(button_frame, text=action_label, command=accept).grid(row=0, column=1)

        tree.bind("<Double-1>", lambda _event: accept())
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.protocol("WM_DELETE_WINDOW", cancel)

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
        tk = self.tk
        ttk = self.ttk

        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)

        ttk.Label(
            frame,
            text=description,
            wraplength=460,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(frame, text="Person:").grid(row=1, column=0, sticky="w", pady=(0, 4))

        person_names = [person.name for person in persons]
        selected_name = tk.StringVar(value=person_names[0])
        combobox = ttk.Combobox(
            frame,
            textvariable=selected_name,
            values=person_names,
            state="readonly",
            width=42,
        )
        combobox.grid(row=2, column=0, columnspan=2, sticky="ew")
        combobox.focus_set()

        person_by_name = {person.name: person for person in persons}

        def accept() -> None:
            person = person_by_name.get(selected_name.get())
            if person is None:
                return
            dialog.withdraw()
            dialog.destroy()
            self.root.lift()
            self.root.focus_force()
            self.root.after_idle(lambda: on_select(person))

        def cancel() -> None:
            dialog.withdraw()
            dialog.destroy()
            self.root.lift()
            self.root.focus_force()
            self.root.after_idle(on_cancel)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self._button(button_frame, text="Avbryt", command=cancel).grid(row=0, column=0, padx=(0, 8))
        self._button(button_frame, text=action_label, command=accept).grid(row=0, column=1)

        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.protocol("WM_DELETE_WINDOW", cancel)

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
        from tkinter import messagebox

        if not messagebox.askyesno(
            "Eksporter person?",
            (
                "Dry-run er fullført og planen står i loggen.\n\n"
                f'Vil du eksportere bildene av "{person.name}" nå?\n\n'
                f"Personmappen opprettes under:\n{destination_root}"
            ),
            parent=self.root,
        ):
            self._log(f'Personeksport avbrutt for "{person.name}".')
            return
        self._run_export_person(person, destination_root)

    def _run_export_person(self, person: RegisteredPerson, destination_root: Path) -> None:
        self._log(f'Eksporterer bilder av "{person.name}" til {destination_root} ...')
        self._run_waiting_command(
            export_person_command(self.collection_path, person.name, destination_root),
            running_message="Eksporterer person ...",
            success_message="Personeksport fullført.",
            failure_message="Personeksport feilet.",
            on_success=self._refresh_state,
            cancellable=True,
        )

    def _start_import_flow(self) -> None:
        from tkinter import filedialog, messagebox, simpledialog

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
            import_name = simpledialog.askstring(
                "Importnavn",
                "Navn på importen:",
                initialvalue=proposed_name,
                parent=self.root,
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
            on_success=self._refresh_state,
        )

    def _start_server(self) -> None:
        from tkinter import messagebox

        self._update_migration_status()
        if self.migration_required:
            self._refresh_state()
            self._show_migration_required_dialog()
            return
        if self.migration_status_error is not None:
            self._refresh_state()
            self._show_migration_status_error()
            return

        if self.server_process is not None:
            if self.server_process.poll() is None:
                self._log("Bildebank-server kjører allerede. Åpner nytt vindu.")
                if not open_server_browser_window():
                    self._log(f"Kunne ikke åpne nettleser automatisk. Åpne {server_browser_url()} manuelt.")
                return
            self.server_process = None

        self._log("Starter Bildebank ...")
        try:
            self.server_process = subprocess.Popen(run_server_command(self.collection_path))
        except OSError as exc:
            messagebox.showerror("Kunne ikke starte Bildebank", "Bildebank-serveren kunne ikke startes.")
            self._log(f"Kunne ikke starte Bildebank: {exc}")
            return
        self._log("Bildebank-serveren starter. Nettleseren åpnes av Bildebank når serveren er klar.")

    def _run_waiting_command(
        self,
        command: list[str],
        *,
        running_message: str,
        success_message: str,
        failure_message: str,
        on_success: Callable[[], None] | None = None,
        stdin_text: str | None = None,
        cancellable: bool = False,
    ) -> None:
        from tkinter import messagebox

        self.active_command_process = None
        self.active_command_cancel_requested = False
        self.active_command_cancellable = cancellable
        self._set_busy(True, running_message)
        self._clear_active_progress_log()
        self._log("$ " + " ".join(command))

        def worker() -> None:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE if stdin_text is not None else None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding=subprocess_output_encoding(),
                    errors="replace",
                    bufsize=1,
                    creationflags=interruptible_command_creationflags() if cancellable else 0,
                )
            except OSError as exc:
                self._post_to_tk(lambda exc=exc: self._command_start_failed(failure_message, exc))
                return
            self.active_command_process = process
            if self.active_command_cancel_requested:
                try:
                    interrupt_process(process)
                except OSError:
                    pass

            if stdin_text is not None:
                assert process.stdin is not None
                process.stdin.write(stdin_text)
                process.stdin.flush()
                process.stdin.close()

            assert process.stdout is not None
            for line in process.stdout:
                self._post_to_tk(lambda message=line.rstrip(): self._log_process_output(message))
            return_code = process.wait()
            cancel_requested = self.active_command_cancel_requested
            self._post_to_tk(
                lambda: self._command_finished(
                    return_code,
                    success_message=success_message,
                    failure_message=failure_message,
                    on_success=on_success,
                    messagebox=messagebox,
                    cancel_requested=cancel_requested,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _command_start_failed(self, failure_message: str, exc: OSError) -> None:
        from tkinter import messagebox

        self.active_command_process = None
        self.active_command_cancel_requested = False
        self.active_command_cancellable = False
        self._set_busy(False)
        self._clear_active_progress_log()
        self._log(f"{failure_message} {exc}")
        messagebox.showerror("Feil", failure_message)

    def _command_finished(
        self,
        return_code: int,
        *,
        success_message: str,
        failure_message: str,
        on_success: Callable[[], None] | None,
        messagebox: object,
        cancel_requested: bool = False,
    ) -> None:
        self.active_command_process = None
        self.active_command_cancel_requested = False
        self.active_command_cancellable = False
        self._set_busy(False)
        self._clear_active_progress_log()
        if return_code == 0:
            self._log(success_message)
            if on_success is not None:
                on_success()
            return
        if cancel_requested:
            self._log(f"Jobben ble avbrutt. Avsluttet med kode {return_code}.")
            return
        self._log(f"{failure_message} Avsluttet med kode {return_code}.")
        messagebox.showerror("Feil", failure_message)

    def _show_error(self, message: str, exc: BaseException) -> None:
        from tkinter import messagebox

        messagebox.showerror("Feil", message)
        self._log(f"{message} {exc}")

    def _log_process_output(self, message: str) -> None:
        self._log(message, progress_key=progress_log_key(message))

    def _clear_active_progress_log(self) -> None:
        self.active_progress_log_key = None
        self.active_progress_log_range = None

    def _log(self, message: str, *, progress_key: str | None = None) -> None:
        if not message:
            return
        assert self.log_text is not None
        self.log_text.configure(state="normal")
        if (
            progress_key is not None
            and progress_key == self.active_progress_log_key
            and self.active_progress_log_range is not None
        ):
            start, end = self.active_progress_log_range
            self.log_text.delete(start, end)
            self.log_text.insert(start, message + "\n")
        else:
            start = self.log_text.index("end-1c")
            self.log_text.insert("end", message + "\n")
        if progress_key is not None:
            self.active_progress_log_key = progress_key
            self.active_progress_log_range = (start, f"{start} + {len(message) + 1} chars")
        else:
            self._clear_active_progress_log()
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> int:
    launcher = BildebankLauncher()
    launcher.run()
    return 0
