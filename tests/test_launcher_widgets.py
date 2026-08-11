from __future__ import annotations

import inspect

from bildebank.launcher_import_tab import ImportTab
from bildebank.launcher_tools_tab import ToolsTab
from bildebank.launcher_widgets import (
    ImageClusteringDialogValues,
    ask_string_dialog,
    image_clustering_dialog,
    select_person_dialog,
    select_source_dialog,
    show_log_review_question,
)

def test_launcher_string_dialog_is_padded_and_replaces_simpledialog() -> None:
    source = inspect.getsource(ask_string_dialog)
    cleanup_source = inspect.getsource(ToolsTab._confirm_cleanup_pending_deletes)
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
    pending_source = inspect.getsource(ToolsTab._pending_deletes_list_finished)

    assert "Toplevel" in source
    assert "grab_set" not in source
    assert "wait_window" not in source
    assert "wait_variable" not in source
    assert "set_busy(True" in source
    assert "set_busy(False" in source
    assert "_show_log_review_question" in pending_source


def test_leiden_dialog_hides_fixed_technical_parameters() -> None:
    source = inspect.getsource(image_clustering_dialog)
    leiden_source = source.split('elif algorithm.get() == "Leiden":', 1)[1].split("        else:", 1)[0]
    values = ImageClusteringDialogValues(
        algorithm="leiden",
        query="",
        hide_out_of_focus=False,
        neighbor_count=20,
        resolution=0.2,
    )

    assert "Nabomodus:" not in source
    assert "Minste likhet" not in source
    assert "Kantvekter:" not in source
    assert "Detaljerte grupper: 20 naboer og 0,2" in source
    assert "Du kan skrive egne verdier i feltene over." in source
    assert "random_seed=" not in leiden_source
    assert "neighbor_mode=" not in leiden_source
    assert "minimum_similarity=" not in leiden_source
    assert "weight_mode=" not in leiden_source
    assert values.random_seed == 0
    assert values.neighbor_mode == "union"
    assert values.minimum_similarity == 0.0
    assert values.weight_mode == "cosine"
