from __future__ import annotations

import html
import importlib.util
import urllib.parse
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from . import __version__, db
from .geo import h3_resolution
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
MAX_NAMED_H3_RESOLUTION = 11


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
    named_h3_cells = app_status_named_h3_cells(target)
    rows = "\n".join(
        (
            app_status_row_html("Bildesamling", str(target)),
            app_status_hide_out_of_focus_row_html(config.browser.hide_out_of_focus),
            app_status_manual_h3_cell_row_html(config.browser.manual_h3_cell, named_h3_cells),
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


def app_status_named_h3_cells(target: Path) -> list[Any]:
    if not db.db_path_for_target(target).is_file():
        return []
    conn = db.connect(target)
    try:
        return db.geo_place_names(conn)
    finally:
        conn.close()


def app_status_manual_h3_cell_row_html(h3_cell: str, named_h3_cells: list[Any]) -> str:
    clean_h3_cell = h3_cell.strip()
    has_selected_cell = False
    selected_name = ""
    options = [f'<option value=""{selected_attr(clean_h3_cell == "")}>Ingen celle valgt</option>']
    for row in named_h3_cells:
        cell = str(row["h3_cell"])
        name = str(row["name"])
        selected = cell == clean_h3_cell
        has_selected_cell = has_selected_cell or selected
        if selected:
            selected_name = name
        options.append(
            f'<option value="{html.escape(cell)}"{selected_attr(selected)}>'
            f'{html.escape(name)} ({html.escape(h3_resolution_option_label(cell))})'
            "</option>"
        )
    if clean_h3_cell and not has_selected_cell:
        options.append(
            f'<option value="{html.escape(clean_h3_cell)}" selected>'
            f'Ikke navngitt: {html.escape(clean_h3_cell)}'
            "</option>"
        )
    status = html.escape(selected_name or clean_h3_cell or "Ikke satt")
    maps_link = h3_cell_google_maps_link_html(clean_h3_cell)
    h3geo_link = h3_cell_h3geo_link_html(clean_h3_cell)
    return f"""
    <div class="info-row">
      <dt>Aktiv manuell H3-celle. Denne brukes til å sette plassering på bilder som mangler det.</dt>
      <dd>
        <form action="/settings/manual-h3-cell" method="post" class="app-toggle-form">
          <select name="h3_cell">
            {"".join(options)}
          </select>
          <button type="submit" class="nav-button">Lagre</button>
          <span class="app-toggle-status">{status}</span>
          {maps_link}
          {h3geo_link}
          <a href="/settings/h3-cells" class="app-toggle-note">Rediger H3-celler</a>
        </form>
      </dd>
    </div>
    """


def h3_cell_google_maps_link_html(h3_cell: str) -> str:
    if not h3_cell:
        return ""
    try:
        url = h3_cell_google_maps_url(h3_cell)
    except Exception:  # noqa: BLE001 - settings page should not fail if h3 is missing or a cell is invalid
        return ""
    return f'<a href="{html.escape(url)}" target="_blank" rel="noopener" class="app-toggle-note">Google Maps</a>'


def h3_cell_google_maps_url(h3_cell: str) -> str:
    import h3

    latitude, longitude = h3.cell_to_latlng(h3_cell)
    query = urllib.parse.quote(f"{latitude:.7f},{longitude:.7f}", safe=",")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


def h3_cell_h3geo_link_html(h3_cell: str) -> str:
    if not h3_cell:
        return ""
    url = h3_cell_h3geo_url(h3_cell)
    return f'<a href="{html.escape(url)}" target="_blank" rel="noopener" class="app-toggle-note">h3geo.org</a>'


def h3_cell_h3geo_url(h3_cell: str) -> str:
    return f"https://h3geo.org/#hex={urllib.parse.quote(h3_cell, safe='')}"


def h3_resolution_option_label(h3_cell: str) -> str:
    try:
        return f"H3-{h3_resolution(h3_cell)}"
    except Exception:  # noqa: BLE001 - settings page should not fail if h3 is missing or a cell is invalid
        return "H3-?"



def h3_resolution_value_label(h3_cell: str) -> str:
    try:
        return str(h3_resolution(h3_cell))
    except Exception:  # noqa: BLE001 - settings page should not fail if h3 is missing or a cell is invalid
        return "ukjent"


def h3_cells_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    named_h3_cells = app_status_named_h3_cells(target)
    return shell_page_html(
        "Rediger H3-celler",
        f"""
        <nav class="subnav"><a href="/settings">Innstillinger</a></nav>
        <h1>Rediger H3-celler</h1>
        <p class="meta">På denne siden gir du navn til H3-celler. Disse navnene brukes for
        å velge på siden <a href="/settings">innstillinger</a> hvilken celle som brukes til
        å sette sted for bilder som mangler GPS-lokasjon.
        </p>
        <section class="custom-geo-places">
          <h2>Legg til H3-celle</h2>
          <p>Bruk oppløsning 11 (høyeste oppløsning) for å markere et enkelt hus eller
          en veldig presis lokasjon, hvis du vet nøyaktig plassering. Hvis huset ligger på
          grensen mellom to H3-celler i oppløsning 11, så velger du bare den som
          dekker mest av huset. Det er ikke så viktig.</p>
          <p>Hvis du har mange bilder fra en gammel sydentur til Kreta, så kan du i
          første omgang finne en H3-celle som er så stor at den dekker hele Kreta og
          markere bildene med den. Og hvis du seinere ønsker å plassere mer presis
          for enkelte bilder, så gjør du det.</p>
          {h3_cell_form_html()}
        </section>
        <section class="custom-geo-places">
          <h2>Definerte steder</h2>
          {h3_cell_list_html(named_h3_cells)}
        </section>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def h3_cell_form_html() -> str:
    return f"""
    <form action="/settings/h3-cell" method="post" class="custom-place-form">
      <label>Navn <input name="name" autocomplete="off"></label>
      <label>H3-id <input name="h3_cell" autocomplete="off"></label>
      <p class="meta">Bildebank bruker H3-oppløsning 0 til {MAX_NAMED_H3_RESOLUTION}.</p>
      <div class="custom-place-actions">
        <button type="submit">Legg til H3-celle</button>
      </div>
    </form>
    """


def h3_cell_list_html(named_h3_cells: list[Any]) -> str:
    if not named_h3_cells:
        return '<p class="meta">Ingen H3-celler er navngitt ennå.</p>'
    rows = "\n".join(h3_cell_row_html(row) for row in named_h3_cells)
    return f"""
    <div class="geo-list">
      <div class="geo-row">
        <strong>Navn</strong>
        <strong>Resolution</strong>
        <strong>H3 hexagon id</strong>
      </div>
      {rows}
    </div>
    """


def h3_cell_row_html(row: Any) -> str:
    h3_cell = str(row["h3_cell"])
    name = str(row["name"])
    return f"""
    <div class="geo-row">
      <span>{html.escape(name)}</span>
      <span>{html.escape(h3_resolution_value_label(h3_cell))}</span>
      <code>{html.escape(h3_cell)}</code>
    </div>
    """


def h3_resolution_for_named_place(h3_cell: str) -> int:
    import h3

    if hasattr(h3, "is_valid_cell") and not h3.is_valid_cell(h3_cell):
        raise ValueError(f"Ugyldig H3-celle: {h3_cell}")
    try:
        resolution = int(h3.get_resolution(h3_cell))
    except Exception as exc:  # noqa: BLE001 - h3 raises library-specific exceptions
        raise ValueError(f"Ugyldig H3-celle: {h3_cell}") from exc
    if resolution > MAX_NAMED_H3_RESOLUTION:
        raise ValueError(f"Bildebank bruker bare H3-oppløsning 0 til {MAX_NAMED_H3_RESOLUTION}.")
    return h3_resolution(h3_cell)


def save_h3_cell_name(target: Path, *, h3_cell: str, name: str) -> None:
    clean_h3_cell = h3_cell.strip()
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Navn mangler.")
    h3_resolution_for_named_place(clean_h3_cell)
    conn = db.connect(target)
    try:
        db.set_geo_place_name(conn, clean_h3_cell, clean_name)
        conn.commit()
    finally:
        conn.close()


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
