from __future__ import annotations

import html
import importlib.util
import urllib.parse
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from . import __version__, db
from .config import (
    AppConfig,
    FaceRecognitionConfig,
    set_browser_hide_out_of_focus,
    set_browser_manual_h3_cell,
    set_face_recognition_enabled,
    set_face_recognition_model_name,
)
from .html_export import format_bytes


ShellPageRenderer = Callable[..., str]
ModuleAvailable = Callable[[str], bool]


def app_status_page_html(
    target: Path,
    config: AppConfig | None = None,
    *,
    shell_page_html: ShellPageRenderer,
    module_available_func: ModuleAvailable | None = None,
) -> str:
    if config is None:
        config = AppConfig()
    if module_available_func is None:
        module_available_func = module_available
    insightface_installed = module_available_func("insightface")
    rows = "\n".join(
        (
            app_status_row_html("Bildesamling", str(target)),
            app_status_hide_out_of_focus_row_html(config.browser.hide_out_of_focus),
            app_status_manual_h3_cell_row_html(config.browser.manual_h3_cell),
            app_status_row_html("Bildebank-versjon", __version__),
            app_status_face_config_row_html(config.face_recognition.enabled, insightface_installed=insightface_installed),
            app_status_face_model_row_html(config.face_recognition),
            app_status_row_html("InsightFace installert", yes_no(insightface_installed)),
            app_status_row_html("OpenCLIP tilgjengelig", yes_no(module_available_func("open_clip"))),
            app_status_row_html("OpenCLIP aktivert", yes_no(config.openclip.enabled)),
            app_status_row_html("OpenCLIP-modell", config.openclip.model_name),
            app_status_row_html("OpenCLIP-pretrained", config.openclip.pretrained),
            app_status_row_html("OpenCLIP-device", config.openclip.device),
        )
    )
    return shell_page_html(
        "Innstillinger",
        f"""
        <nav class="subnav">
          <a href="/settings/removed">Slettede bilder</a>
          <a href="/date-source/filename">Dato fra filnavn</a>
          <a href="/date-source/mtime">Dato fra mtime</a>
        </nav>
        <h1>Innstillinger</h1>
        <dl class="info-list app-status">
          {rows}
        </dl>
        """,
        face_enabled=config.face_recognition.enabled,
        openclip_enabled=config.openclip.enabled,
    )


def removed_files_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    conn = db.connect(target)
    try:
        rows = list(db.deleted_files(conn))
    finally:
        conn.close()
    items = "\n".join(removed_file_row_html(target, row) for row in rows)
    content = (
        f'<div class="removed-list">{items}</div>'
        if items
        else '<p class="meta">Ingen bilder er flyttet til deleted/.</p>'
    )
    return shell_page_html(
        "Slettede bilder",
        f"""
        <nav class="subnav"><a href="/settings">Innstillinger</a></nav>
        <h1>Slettede bilder</h1>
        <p class="meta">{len(rows)} bilder flyttet til deleted/.</p>
        {content}
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def update_face_enabled_config(config: AppConfig, repo_root: Path, enabled: bool) -> AppConfig:
    set_face_recognition_enabled(repo_root, enabled)
    return replace(
        config,
        face_recognition=replace(config.face_recognition, enabled=enabled),
    )


def update_hide_out_of_focus_config(config: AppConfig, repo_root: Path, enabled: bool) -> AppConfig:
    set_browser_hide_out_of_focus(repo_root, enabled)
    return replace(
        config,
        browser=replace(config.browser, hide_out_of_focus=enabled),
    )


def update_manual_h3_cell_config(config: AppConfig, repo_root: Path, h3_cell: str) -> AppConfig:
    clean_h3_cell = h3_cell.strip()
    set_browser_manual_h3_cell(repo_root, clean_h3_cell)
    return replace(
        config,
        browser=replace(config.browser, manual_h3_cell=clean_h3_cell),
    )


def update_face_model_config(config: AppConfig, repo_root: Path, model_name: str) -> AppConfig:
    face_config = config.face_recognition
    installed_models = installed_insightface_models(face_config)
    if model_name not in installed_models:
        raise ValueError(f"InsightFace-modellen er ikke installert: {model_name}")
    set_face_recognition_model_name(repo_root, model_name)
    return replace(
        config,
        face_recognition=replace(face_config, model_name=model_name),
    )


def removed_file_row_html(target: Path, row: Any) -> str:
    deleted_path = Path(str(row["target_path"]))
    original_path = row["deleted_original_target_path"] or row["target_path"]
    link = "/file/" + urllib.parse.quote(deleted_path.as_posix())
    exists = "finnes" if db.absolute_target_path(target, deleted_path).is_file() else "mangler"
    taken_date = str(row["taken_date"] or "ukjent dato")
    size = format_bytes(int(row["size_bytes"])) if row["size_bytes"] is not None else "ukjent størrelse"
    deleted_at = str(row["deleted_at"] or "")
    return f"""
    <div class="removed-row">
      <a href="{html.escape(link)}" target="_blank">{html.escape(str(original_path))}</a>
      <span>{html.escape(deleted_at)}</span>
      <span>{html.escape(taken_date)}</span>
      <span>{html.escape(size)}</span>
      <span>{exists}</span>
      <button class="nav-button" type="button" data-undelete-item="{int(row["id"])}" data-undelete-path="{html.escape(str(original_path))}">Undelete</button>
    </div>
    """


def app_status_row_html(label: str, value: str) -> str:
    return f"""
    <div class="info-row">
      <dt>{html.escape(label)}</dt>
      <dd>{html.escape(value)}</dd>
    </div>
    """


def app_status_face_config_row_html(enabled: bool, *, insightface_installed: bool = True) -> str:
    checked = " checked" if enabled else ""
    status = "På" if enabled else "Av"
    install_note = (
        ""
        if insightface_installed
        else '<span class="app-toggle-note"><a href="/help/insightface">InsightFace</a> må installeres for å scanne ansikter i nye bilder.</span>'
    )
    return f"""
    <div class="info-row">
      <dt>InsightFace aktivert</dt>
      <dd>
        <form action="/settings/face-config" method="post" class="app-toggle-form">
          <input type="hidden" name="enabled" value="false">
          <label class="app-toggle">
            <input type="checkbox" name="enabled" value="true"{checked} onchange="this.form.submit()">
            <span class="app-toggle-track" aria-hidden="true"><span></span></span>
            <span class="app-toggle-status">{status}</span>
          </label>
          {install_note}
        </form>
      </dd>
    </div>
    """


def app_status_hide_out_of_focus_row_html(enabled: bool) -> str:
    checked = " checked" if enabled else ""
    status = "På" if enabled else "Av"
    return f"""
    <div class="info-row">
      <dt>Skjul bilder tagget “Ute av fokus”</dt>
      <dd>
        <form action="/settings/hide-out-of-focus" method="post" class="app-toggle-form">
          <input type="hidden" name="enabled" value="false">
          <label class="app-toggle">
            <input type="checkbox" name="enabled" value="true"{checked} onchange="this.form.submit()">
            <span class="app-toggle-track" aria-hidden="true"><span></span></span>
            <span class="app-toggle-status">{status}</span>
          </label>
        </form>
      </dd>
    </div>
    """


def app_status_manual_h3_cell_row_html(h3_cell: str) -> str:
    value = html.escape(h3_cell)
    status = html.escape(h3_cell or "Ikke satt")
    return f"""
    <div class="info-row">
      <dt>Aktiv manuell H3-celle</dt>
      <dd>
        <form action="/settings/manual-h3-cell" method="post" class="app-toggle-form">
          <input type="text" name="h3_cell" value="{value}" placeholder="H3-celle">
          <button type="submit" class="nav-button">Lagre</button>
          <span class="app-toggle-status">{status}</span>
        </form>
      </dd>
    </div>
    """


def app_status_face_model_row_html(config: FaceRecognitionConfig) -> str:
    installed_models = installed_insightface_models(config)
    if not installed_models:
        return app_status_row_html("InsightFace-modell", f"{config.model_name} (ingen installerte modeller funnet)")
    options = "\n".join(
        f'<option value="{html.escape(model)}"{selected_attr(model == config.model_name)}>{html.escape(model)}</option>'
        for model in installed_models
    )
    note = (
        ""
        if config.model_name in installed_models
        else f'<span class="app-toggle-note">Aktiv config er {html.escape(config.model_name)}, men modellen finnes ikke i modellmappen.</span>'
    )
    return f"""
    <div class="info-row">
      <dt>InsightFace-modell</dt>
      <dd>
        <form action="/settings/face-model" method="post" class="app-toggle-form">
          <select name="model_name" onchange="this.form.submit()">
            {options}
          </select>
          {note}
        </form>
      </dd>
    </div>
    """


def selected_attr(selected: bool) -> str:
    return " selected" if selected else ""


def installed_insightface_models(config: FaceRecognitionConfig) -> list[str]:
    models_dir = config.model_root / "models"
    try:
        children = list(models_dir.iterdir())
    except OSError:
        return []
    models: list[str] = []
    for child in children:
        if not child.is_dir():
            continue
        if list(child.glob("*.onnx")) or list((child / child.name).glob("*.onnx")):
            models.append(child.name)
    return sorted(models, key=str.lower)


def yes_no(value: bool) -> str:
    return "ja" if value else "nei"


def module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def server_program_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
