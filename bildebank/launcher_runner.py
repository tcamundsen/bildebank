from __future__ import annotations

import locale
import os
import signal
import subprocess
import threading
from collections.abc import Callable

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


def subprocess_output_encoding() -> str:
    return locale.getpreferredencoding(False) or "utf-8"


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


def progress_log_key(message: str) -> str | None:
    if _is_tqdm_progress_line(message):
        return "tqdm-progress"
    if message.startswith("Snapshot: "):
        return "Snapshot"
    if message.startswith("Snapshot dry-run: "):
        return "Snapshot dry-run"
    if message.startswith("Snapshot check: "):
        return "Snapshot check"
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


class CommandRunner:
    def __init__(
        self,
        *,
        post_to_ui: Callable[[Callable[[], None]], bool],
        on_output: Callable[[str], None],
    ) -> None:
        self._post_to_ui = post_to_ui
        self._on_output = on_output
        self.process: subprocess.Popen[str] | None = None
        self.cancel_requested = False
        self.cancellable = False

    def start(
        self,
        command: list[str],
        *,
        on_start: Callable[[], None],
        on_start_failed: Callable[[OSError], None],
        on_finished: Callable[[int, bool], None],
        stdin_text: str | None = None,
        cancellable: bool = False,
    ) -> None:
        self.process = None
        self.cancel_requested = False
        self.cancellable = cancellable
        on_start()

        def worker() -> None:
            try:
                process: subprocess.Popen[str] = subprocess.Popen(
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
                def report_start_failed(exc: OSError = exc) -> None:
                    self._report_start_failed(exc, on_start_failed)

                self._post_to_ui(report_start_failed)
                return

            self.process = process
            if self.cancel_requested:
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
                message = line.rstrip()

                def report_output(message: str = message) -> None:
                    self._on_output(message)

                self._post_to_ui(report_output)
            return_code = process.wait()
            cancel_requested = self.cancel_requested
            self._post_to_ui(
                lambda: self._report_finished(return_code, cancel_requested, on_finished)
            )

        threading.Thread(target=worker, daemon=True).start()

    def request_cancel(self) -> bool:
        if not self.cancellable:
            return False
        self.cancel_requested = True
        process = self.process
        if process is not None and process.poll() is None:
            interrupt_process(process)
        return True

    def _report_start_failed(self, exc: OSError, callback: Callable[[OSError], None]) -> None:
        self._reset()
        callback(exc)

    def _report_finished(
        self,
        return_code: int,
        cancel_requested: bool,
        callback: Callable[[int, bool], None],
    ) -> None:
        self._reset()
        callback(return_code, cancel_requested)

    def _reset(self) -> None:
        self.process = None
        self.cancel_requested = False
        self.cancellable = False
