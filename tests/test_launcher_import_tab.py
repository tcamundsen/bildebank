from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bildebank import db
from bildebank.launcher_import_tab import (
    ImportTab,
    source_is_collection_or_inside,
    suggest_import_name,
)


def source(path: Path, *, name: str = "Sommer 2024") -> db.Source:
    return db.Source(
        id=1,
        path=path,
        path_key=str(path),
        name=name,
        imported_at="2026-01-01",
        status="completed",
    )


def bare_import_tab(collection_path: Path) -> ImportTab:
    tab = ImportTab.__new__(ImportTab)
    tab.root = object()
    tab._get_collection_path = lambda: collection_path
    tab._log = lambda _message: None
    tab._refresh_launcher = lambda: None
    tab._ask_string = lambda *_args, **_kwargs: None
    return tab


def test_import_helpers_handle_windows_names_and_reject_collection_paths(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    other_source = tmp_path / "bilder"
    child = collection / "2024"
    collection.mkdir()
    other_source.mkdir()
    child.mkdir()

    assert suggest_import_name(Path(r"D:\Bilder\Sommer 2024")) == "Sommer 2024"
    assert source_is_collection_or_inside(collection, collection)
    assert source_is_collection_or_inside(child, collection)
    assert not source_is_collection_or_inside(other_source, collection)


def test_import_tab_defines_all_four_actions_and_safety_tooltips() -> None:
    source_code = inspect.getsource(ImportTab.refresh)

    assert 'text="Importer bilder"' in source_code
    assert 'text="Angre import"' in source_code
    assert 'text="Rescan kilde"' in source_code
    assert 'text="Sjekk kilde"' in source_code
    assert "Krever nøyaktig bekreftelse" in source_code
    assert "samme SHA-256" in source_code


def test_import_flow_runs_import_with_selected_folder_and_trimmed_name(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    source_folder = tmp_path / "bilder"
    collection.mkdir()
    source_folder.mkdir()
    tab = bare_import_tab(collection)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    tab._ask_string = lambda *_args, **_kwargs: "  Feriebilder  "
    tab._run_waiting_command = lambda command, **kwargs: calls.append((command, kwargs))

    with patch("tkinter.filedialog.askdirectory", return_value=str(source_folder)):
        tab._start_import_flow()

    command, options = calls[0]
    assert command[-6:] == [
        "--target",
        str(collection),
        "import",
        "--name",
        "Feriebilder",
        str(source_folder),
    ]
    assert options["running_message"] == "Importerer bilder ..."
    assert options["on_success"] is tab._refresh_launcher


def test_import_flow_rejects_collection_as_source(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    collection.mkdir()
    tab = bare_import_tab(collection)
    logged: list[str] = []
    tab._log = logged.append
    tab._run_waiting_command = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("import command should not run")
    )

    with (
        patch("tkinter.filedialog.askdirectory", return_value=str(collection)),
        patch("tkinter.messagebox.showerror") as showerror,
    ):
        tab._start_import_flow()

    showerror.assert_called_once()
    assert logged == [f"Import avvist: {collection} ligger i bildesamlingen {collection}"]


def test_rescan_and_check_source_remain_cancellable(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    source_folder = tmp_path / "bilder"
    tab = bare_import_tab(collection)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    tab._run_waiting_command = lambda command, **kwargs: calls.append((command, kwargs))
    registered_source = source(source_folder)

    tab._run_rescan_source(registered_source)
    tab._run_check_source(registered_source)

    assert calls[0][0][-5:] == ["--target", str(collection), "rescan-source", "--name", "Sommer 2024"]
    assert calls[0][1]["cancellable"] is True
    assert calls[1][0][-4:] == ["--target", str(collection), "check-source", str(source_folder)]
    assert calls[1][1]["cancellable"] is True


def test_unimport_starts_with_dry_run_and_target_change_report(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    tab = bare_import_tab(collection)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    tab._run_waiting_command = lambda command, **kwargs: calls.append((command, kwargs))

    tab._run_unimport_source_dry_run(source(tmp_path / "bilder"))

    command, options = calls[0]
    assert command[-8:-1] == [
        "--target",
        str(collection),
        "unimport",
        "--dry-run",
        "--name",
        "Sommer 2024",
        "--target-change-report-json",
    ]
    report_path = Path(command[-1])
    assert report_path.name.startswith("bildebank-unimport-")
    assert options["success_message"] == "Unimport dry-run fullført. Se planen i loggen."
    assert callable(options["on_success"])
    report_path.unlink(missing_ok=True)


def test_unimport_requires_exact_confirmation_before_running(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text('{"changed_targets": []}', encoding="utf-8")
    tab = bare_import_tab(tmp_path / "samling")
    tab._ask_string = lambda *_args, **_kwargs: "ja"
    tab._run_unimport_source = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("unimport should not run without exact confirmation")
    )

    with patch("tkinter.messagebox.askokcancel", return_value=True):
        tab._confirm_unimport_source(source(tmp_path / "bilder"), report_path)

    assert not report_path.exists()


def test_unimport_can_be_cancelled_after_dry_run(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text('{"changed_targets": []}', encoding="utf-8")
    tab = bare_import_tab(tmp_path / "samling")
    logged: list[str] = []
    tab._log = logged.append
    tab._ask_string = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("text confirmation should not open after cancellation")
    )
    tab._run_unimport_source = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("unimport should not run after cancellation")
    )
    selected_source = source(tmp_path / "bilder")

    with patch("tkinter.messagebox.askokcancel", return_value=False) as askokcancel:
        tab._confirm_unimport_source(selected_source, report_path)

    askokcancel.assert_called_once_with(
        "Unimport",
        (
            "Dry-run er fullført og planen står i loggen.\n\n"
            "Unimport kan fjerne filer fra den aktive bildesamlingen."
        ),
        parent=tab.root,
        icon="warning",
    )
    assert logged == ['Unimport avbrutt for kilde "Sommer 2024" etter dry-run.']
    assert not report_path.exists()


def test_unimport_changed_target_requires_extra_confirmation(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        '{"changed_targets": [{"path": "2024/01/IMG.jpg"}]}',
        encoding="utf-8",
    )
    tab = bare_import_tab(tmp_path / "samling")
    tab._ask_string = lambda *_args, **_kwargs: "ja, det vil jeg"
    calls: list[tuple[db.Source, str]] = []
    tab._run_unimport_source = lambda selected, *, target_change_answer: calls.append(
        (selected, target_change_answer)
    )
    selected_source = source(tmp_path / "bilder")

    with (
        patch("tkinter.messagebox.askokcancel", return_value=True),
        patch("tkinter.messagebox.askyesno", return_value=True) as askyesno,
    ):
        tab._confirm_unimport_source(selected_source, report_path)

    askyesno.assert_called_once()
    assert calls == [(selected_source, "ja")]
    assert not report_path.exists()


def test_unimport_passes_both_cli_confirmations_on_stdin(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    tab = bare_import_tab(collection)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    tab._run_waiting_command = lambda command, **kwargs: calls.append((command, kwargs))

    tab._run_unimport_source(source(tmp_path / "bilder"), target_change_answer="ja")

    command, options = calls[0]
    assert command[-5:] == ["--target", str(collection), "unimport", "--name", "Sommer 2024"]
    assert options["stdin_text"] == "ja, det vil jeg\nja\n"
    assert options["on_success"] is tab._refresh_launcher
