from __future__ import annotations

import signal
from unittest.mock import patch

from bildebank.launcher_runner import (
    CommandRunner,
    interrupt_process,
    interruptible_command_creationflags,
    progress_log_key,
    subprocess_output_encoding,
)

def test_subprocess_output_encoding_uses_locale() -> None:
    with patch("locale.getpreferredencoding", return_value="cp1252"):
        assert subprocess_output_encoding() == "cp1252"


def test_interruptible_command_creationflags_are_zero_outside_windows() -> None:
    with patch("bildebank.launcher_runner.os.name", "posix"):
        assert interruptible_command_creationflags() == 0


def test_interrupt_process_sends_sigint_outside_windows() -> None:
    class FakeProcess:
        signal_sent: object | None = None

        def send_signal(self, value: object) -> None:
            self.signal_sent = value

    process = FakeProcess()

    with patch("bildebank.launcher_runner.os.name", "posix"):
        interrupt_process(process)  # type: ignore[arg-type]

    assert process.signal_sent is signal.SIGINT


def test_progress_log_key_recognizes_progress_updates_only() -> None:
    assert progress_log_key("Thumbnails: kontrollert=25/84, sjekket=25") == "Thumbnails"
    assert progress_log_key("Check-source: kontrollert=4/10, filer=4") == "Check-source"
    assert progress_log_key("geo-scan: scannet=12/40, gps=3, feil=0") == "geo-scan"
    assert progress_log_key("Import: importert=2/10, duplikater=1") == "Import"
    assert progress_log_key("Rescan-source: kontrollert=2/10, nye=1") == "Rescan-source"
    assert progress_log_key("Snapshot: lager filinventar ...") == "Snapshot"
    assert progress_log_key("Snapshot: filer=25/100, lest=1.0 GB/4.0 GB") == "Snapshot"
    assert progress_log_key("Snapshot check: objekter=25/100, lest=1.0 GB/4.0 GB") == "Snapshot check"
    assert progress_log_key(" 59%|#####     | 206485/352210 [00:13<00:08, 16523.67KB/s]") == "tqdm-progress"
    assert progress_log_key("Thumbnails: 84 filer skal kontrolleres.") is None
    assert progress_log_key("Thumbnails: ferdig kontrollert 84/84 filer.") is None
    assert progress_log_key("Import fullført.") is None

def test_command_runner_owns_cancellation_state() -> None:
    runner = CommandRunner(post_to_ui=lambda callback: True, on_output=lambda _message: None)
    process = type("FakeProcess", (), {"poll": lambda self: None})()
    runner.process = process  # type: ignore[assignment]
    runner.cancellable = True

    with patch("bildebank.launcher_runner.interrupt_process") as interrupt:
        assert runner.request_cancel()

    assert runner.cancel_requested
    interrupt.assert_called_once_with(process)


def test_command_runner_reports_output_and_completion_on_ui_callback() -> None:
    events: list[object] = []
    runner = CommandRunner(
        post_to_ui=lambda callback: (callback(), True)[1],
        on_output=lambda message: events.append(("output", message)),
    )

    class FakeProcess:
        stdin = None
        stdout = ["første linje\n", "andre linje\n"]

        def wait(self) -> int:
            return 0

    class ImmediateThread:
        def __init__(self, *, target, daemon: bool) -> None:
            self.target = target
            assert daemon

        def start(self) -> None:
            self.target()

    with (
        patch("bildebank.launcher_runner.subprocess.Popen", return_value=FakeProcess()),
        patch("bildebank.launcher_runner.threading.Thread", ImmediateThread),
    ):
        runner.start(
            ["bildebank", "doctor"],
            on_start=lambda: events.append("start"),
            on_start_failed=lambda exc: events.append(("start-feil", exc)),
            on_finished=lambda return_code, cancelled: events.append(("ferdig", return_code, cancelled)),
        )

    assert events == [
        "start",
        ("output", "første linje"),
        ("output", "andre linje"),
        ("ferdig", 0, False),
    ]
    assert runner.process is None
    assert not runner.cancellable
