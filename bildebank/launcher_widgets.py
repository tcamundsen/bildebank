from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import db
from .launcher_status import RegisteredPerson


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


def center_dialog(dialog: Any, root: Any) -> None:
    dialog.update_idletasks()
    x = root.winfo_rootx() + max((root.winfo_width() - dialog.winfo_width()) // 2, 0)
    y = root.winfo_rooty() + max((root.winfo_height() - dialog.winfo_height()) // 2, 0)
    dialog.geometry(f"+{x}+{y}")


def ask_string_dialog(
    *,
    tk: Any,
    ttk: Any,
    root: Any,
    button: Callable[..., Any],
    title: str,
    message: str,
    initialvalue: str = "",
) -> str | None:
    dialog = tk.Toplevel(root)
    dialog.title(title)
    dialog.transient(root)
    dialog.resizable(False, False)
    frame = ttk.Frame(dialog, padding=16)
    frame.grid(row=0, column=0, sticky="nsew")
    frame.columnconfigure(0, weight=1)
    ttk.Label(frame, text=message, wraplength=460, justify="left").grid(row=0, column=0, sticky="w")
    value = tk.StringVar(value=initialvalue)
    entry = ttk.Entry(frame, textvariable=value, width=48)
    entry.grid(row=1, column=0, sticky="ew", pady=(10, 16))
    entry.focus_set()
    entry.selection_range(0, tk.END)
    result: str | None = None

    def accept() -> None:
        nonlocal result
        result = value.get()
        dialog.destroy()

    def cancel() -> None:
        dialog.destroy()

    button_frame = ttk.Frame(frame)
    button_frame.grid(row=2, column=0, sticky="e")
    button(button_frame, text="Avbryt", command=cancel).grid(row=0, column=0, padx=(0, 8))
    button(button_frame, text="OK", command=accept).grid(row=0, column=1)
    dialog.bind("<Return>", lambda _event: accept())
    dialog.bind("<Escape>", lambda _event: cancel())
    dialog.protocol("WM_DELETE_WINDOW", cancel)
    center_dialog(dialog, root)
    dialog.grab_set()
    root.wait_window(dialog)
    return result


def show_log_review_question(
    *,
    tk: Any,
    ttk: Any,
    root: Any,
    button: Callable[..., Any],
    set_busy: Callable[[bool, str], None],
    title: str,
    message: str,
    yes_text: str,
    no_text: str,
    on_yes: Callable[[], None],
    on_no: Callable[[], None],
) -> None:
    dialog = tk.Toplevel(root)
    dialog.title(title)
    dialog.transient(root)
    dialog.resizable(False, False)
    set_busy(True, "Venter på bekreftelse ...")
    frame = ttk.Frame(dialog, padding=16)
    frame.grid(row=0, column=0, sticky="nsew")
    ttk.Label(frame, text=title, font=("", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(frame, text=message, wraplength=460).grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(10, 16)
    )
    finished = False

    def finish(answer: bool) -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        try:
            dialog.destroy()
        except tk.TclError:
            pass
        set_busy(False, "")
        on_yes() if answer else on_no()

    button_frame = ttk.Frame(frame)
    button_frame.grid(row=2, column=0, columnspan=2, sticky="e")
    button(button_frame, text=no_text, command=lambda: finish(False)).grid(row=0, column=0, padx=(0, 8))
    button(button_frame, text=yes_text, command=lambda: finish(True)).grid(row=0, column=1)
    dialog.bind("<Escape>", lambda _event: finish(False))
    dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    center_dialog(dialog, root)


def select_source_dialog(
    sources: list[db.Source], *, tk: Any, ttk: Any, root: Any, button: Callable[..., Any],
    title: str, action_label: str, on_select: Callable[[db.Source], None], on_cancel: Callable[[], None],
) -> None:
    dialog = tk.Toplevel(root)
    dialog.title(title)
    dialog.transient(root)
    dialog.minsize(760, 320)
    dialog.columnconfigure(0, weight=1)
    dialog.rowconfigure(0, weight=1)
    frame = ttk.Frame(dialog, padding=12)
    frame.grid(row=0, column=0, sticky="nsew")
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    columns = ("id", "status", "name", "path")
    tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", height=10)
    for key, text in (("id", "ID"), ("status", "Status"), ("name", "Navn"), ("path", "Mappe")):
        tree.heading(key, text=text)
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
        item_id = tree.insert("", "end", values=(source.id, source.status, source.name, str(source.path)))
        source_by_item[item_id] = source
    children = tree.get_children()
    if children:
        tree.selection_set(children[0])
        tree.focus(children[0])

    def close(callback: Callable[[], None]) -> None:
        dialog.withdraw()
        dialog.destroy()
        root.lift()
        root.focus_force()
        root.after_idle(callback)

    def accept() -> None:
        selected = tree.selection()
        if selected:
            source = source_by_item[selected[0]]
            close(lambda: on_select(source))

    button_frame = ttk.Frame(frame)
    button_frame.grid(row=1, column=0, columnspan=2, sticky="e", pady=(12, 0))
    button(button_frame, text="Avbryt", command=lambda: close(on_cancel)).grid(row=0, column=0, padx=(0, 8))
    button(button_frame, text=action_label, command=accept).grid(row=0, column=1)
    tree.bind("<Double-1>", lambda _event: accept())
    dialog.bind("<Return>", lambda _event: accept())
    dialog.bind("<Escape>", lambda _event: close(on_cancel))
    dialog.protocol("WM_DELETE_WINDOW", lambda: close(on_cancel))


def select_person_dialog(
    persons: list[RegisteredPerson], *, tk: Any, ttk: Any, root: Any, button: Callable[..., Any],
    title: str, description: str, action_label: str,
    on_select: Callable[[RegisteredPerson], None], on_cancel: Callable[[], None],
) -> None:
    dialog = tk.Toplevel(root)
    dialog.title(title)
    dialog.transient(root)
    dialog.resizable(False, False)
    frame = ttk.Frame(dialog, padding=12)
    frame.grid(row=0, column=0, sticky="nsew")
    frame.columnconfigure(0, weight=1)
    ttk.Label(frame, text=description, wraplength=460).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
    ttk.Label(frame, text="Person:").grid(row=1, column=0, sticky="w", pady=(0, 4))
    person_names = [person.name for person in persons]
    selected_name = tk.StringVar(value=person_names[0])
    combobox = ttk.Combobox(frame, textvariable=selected_name, values=person_names, state="readonly", width=42)
    combobox.grid(row=2, column=0, columnspan=2, sticky="ew")
    combobox.focus_set()
    person_by_name = {person.name: person for person in persons}

    def close(callback: Callable[[], None]) -> None:
        dialog.withdraw()
        dialog.destroy()
        root.lift()
        root.focus_force()
        root.after_idle(callback)

    def accept() -> None:
        person = person_by_name.get(selected_name.get())
        if person is not None:
            close(lambda: on_select(person))

    button_frame = ttk.Frame(frame)
    button_frame.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))
    button(button_frame, text="Avbryt", command=lambda: close(on_cancel)).grid(row=0, column=0, padx=(0, 8))
    button(button_frame, text=action_label, command=accept).grid(row=0, column=1)
    dialog.bind("<Return>", lambda _event: accept())
    dialog.bind("<Escape>", lambda _event: close(on_cancel))
    dialog.protocol("WM_DELETE_WINDOW", lambda: close(on_cancel))
