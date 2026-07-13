from __future__ import annotations

import inspect

from bildebank.launcher import BildebankLauncher
from bildebank.launcher_import_tab import ImportTab
from bildebank.launcher_widgets import (
    ask_string_dialog,
    select_person_dialog,
    select_source_dialog,
    show_log_review_question,
)

def test_launcher_string_dialog_is_padded_and_replaces_simpledialog() -> None:
    source = inspect.getsource(ask_string_dialog)
    cleanup_source = inspect.getsource(BildebankLauncher._confirm_cleanup_pending_deletes)
    unimport_source = inspect.getsource(ImportTab._confirm_unimport_source)
    import_source = inspect.getsource(ImportTab._start_import_flow)

    assert "Toplevel(root)" in source
    assert "ttk.Frame(dialog, padding=16)" in source
    assert "wraplength=460" in source
    assert "ttk.Entry" in source
    assert "button(button_frame" in source
    assert "grab_set" in source
    assert "wait_window" in source
    assert "_ask_string" in cleanup_source
    assert "_ask_string" in unimport_source
    assert "_ask_string" in import_source
    assert "simpledialog" not in cleanup_source
    assert "simpledialog" not in unimport_source
    assert "simpledialog" not in import_source


def test_select_source_does_not_run_nested_tk_event_loop() -> None:
    source = inspect.getsource(select_source_dialog)

    assert "self.root.update()" not in source
    assert "after_idle" in source


def test_select_person_does_not_run_nested_tk_event_loop() -> None:
    source = inspect.getsource(select_person_dialog)

    assert "self.root.update()" not in source
    assert "after_idle" in source
    assert 'state="readonly"' in source


def test_launcher_log_review_question_is_nonmodal() -> None:
    source = inspect.getsource(show_log_review_question)
    pending_source = inspect.getsource(BildebankLauncher._pending_deletes_list_finished)

    assert "Toplevel" in source
    assert "grab_set" not in source
    assert "wait_window" not in source
    assert "wait_variable" not in source
    assert "set_busy(True" in source
    assert "set_busy(False" in source
    assert "_show_log_review_question" in pending_source
