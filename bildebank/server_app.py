from __future__ import annotations

import html
import importlib.util
import urllib.parse
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from . import __version__, db
from .geo import h3_column_for_resolution, h3_resolution
from .config import (
    AppConfig,
    BrowserHotkeyConfig,
    FaceRecognitionConfig,
    HOTKEY_KEYS,
    set_browser_hide_out_of_focus,
    set_browser_hotkey_hints_enabled,
    set_browser_hotkey,
    set_browser_manual_person_controls_enabled,
    set_face_recognition_enabled,
    set_face_recognition_model_name,
)
from .formatting import format_bytes
from .target_lock import TargetLock


ShellPageRenderer = Callable[..., str]
ModuleAvailable = Callable[[str], bool]
MAX_NAMED_H3_RESOLUTION = 11


def app_status_page_html(
    target: Path,
    config: AppConfig | None = None,
    *,
    shell_page_html: ShellPageRenderer,
    module_available_func: ModuleAvailable | None = None,
    scroll_y: int | None = None,
) -> str:
    if config is None:
        config = AppConfig()
    if module_available_func is None:
        module_available_func = module_available
    insightface_installed = module_available_func("insightface")
    named_h3_cells = app_status_named_h3_cells(target)
    registered_people = app_status_registered_people(target, config.face_recognition)
    defined_tags = app_status_defined_tags(target)
    rows = "\n".join(
        (
            app_status_row_html("Bildesamling", str(target)),
            app_status_hide_out_of_focus_row_html(config.browser.hide_out_of_focus),
            app_status_hotkeys_row_html(
                config.browser.hotkeys or {},
                named_h3_cells,
                registered_people,
                defined_tags,
                hints_enabled=config.browser.hotkey_hints_enabled,
            ),
            app_status_row_html("Bildebank-versjon", __version__),
            app_status_face_config_row_html(config.face_recognition.enabled, insightface_installed=insightface_installed),
            app_status_face_model_row_html(config.face_recognition),
            app_status_manual_person_controls_row_html(config.browser.manual_person_controls_enabled),
            app_status_row_html("InsightFace installert", yes_no(insightface_installed)),
            app_status_row_html("OpenCLIP tilgjengelig", yes_no(module_available_func("open_clip"))),
            app_status_row_html("OpenCLIP aktivert", yes_no(config.openclip.enabled)),
            app_status_row_html("OpenCLIP-modell", config.openclip.model_name),
            app_status_row_html("OpenCLIP-pretrained", config.openclip.pretrained),
            app_status_row_html("OpenCLIP-device", config.openclip.device),
        )
    )
    scroll_restore = (
        f'<div data-settings-scroll-restore="{int(scroll_y)}" hidden></div>'
        if scroll_y is not None and scroll_y > 0
        else ""
    )
    return shell_page_html(
        "Innstillinger",
        f"""
        {scroll_restore}
        <nav class="subnav">
          <a href="/settings/removed">Slettede bilder</a>
          <a href="/sources" title='Lister output fra "bildebank list-sources"'>Importerte mapper</a>
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


def update_manual_person_controls_config(config: AppConfig, repo_root: Path, enabled: bool) -> AppConfig:
    set_browser_manual_person_controls_enabled(repo_root, enabled)
    return replace(
        config,
        browser=replace(config.browser, manual_person_controls_enabled=enabled),
    )


def update_hotkey_config(config: AppConfig, repo_root: Path, key: str, hotkey: BrowserHotkeyConfig) -> AppConfig:
    if hotkey.action == "tag":
        hotkey = replace(hotkey, tag_name=db.normalize_tag_name(hotkey.tag_name))
    set_browser_hotkey(repo_root, key, hotkey)
    hotkeys = dict(config.browser.hotkeys or {})
    hotkeys[key] = hotkey
    return replace(
        config,
        browser=replace(config.browser, hotkeys=hotkeys),
    )


def update_hotkey_hints_config(config: AppConfig, repo_root: Path, enabled: bool) -> AppConfig:
    set_browser_hotkey_hints_enabled(repo_root, enabled)
    return replace(
        config,
        browser=replace(config.browser, hotkey_hints_enabled=enabled),
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


def app_status_manual_person_controls_row_html(enabled: bool) -> str:
    checked = " checked" if enabled else ""
    status = "På" if enabled else "Av"
    return f"""
    <div class="info-row">
      <dt>GUI for manuell bekrefting av person i bildet</dt>
      <dd>
        <form action="/settings/manual-person-controls" method="post" class="app-toggle-form">
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


def app_status_registered_people(target: Path, face_config: FaceRecognitionConfig | None = None) -> list[dict[str, str]]:
    try:
        from .server_faces import registered_people

        return registered_people(target, face_config)
    except Exception:  # noqa: BLE001 - settings should still render without a valid face database
        return []


def app_status_defined_tags(target: Path) -> list[Any]:
    if not db.db_path_for_target(target).is_file():
        return []
    try:
        conn = db.connect(target)
        try:
            return list(db.tags(conn))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - settings should still render without a valid target database
        return []


def app_status_hotkeys_row_html(
    hotkeys: dict[str, BrowserHotkeyConfig],
    named_h3_cells: list[Any],
    registered_people: list[dict[str, str]],
    defined_tags: list[Any],
    *,
    hints_enabled: bool = False,
) -> str:
    rows = "\n".join(
        app_status_hotkey_form_html(
            key,
            hotkeys.get(key, BrowserHotkeyConfig()),
            named_h3_cells,
            registered_people,
            defined_tags,
        )
        for key in HOTKEY_KEYS
    )
    checked = " checked" if hints_enabled else ""
    status = "På" if hints_enabled else "Av"
    return f"""
    <div class="info-row">
      <dt>Hurtigtaster 1-5</dt>
      <dd>
        <div class="hotkey-settings">
          <form action="/settings/hotkey-hints" method="post" class="app-toggle-form">
            <input type="hidden" name="enabled" value="false">
            <label class="app-toggle">
              <input type="checkbox" name="enabled" value="true"{checked} onchange="this.form.submit()">
              <span class="app-toggle-track" aria-hidden="true"><span></span></span>
              <span class="app-toggle-status">Aktiver hurtigtaster 1-5: {status}</span>
            </label>
            <a href="/settings/h3-cells" class="app-toggle-note">Rediger H3-celler</a>
          </form>
          {rows}
        </div>
      </dd>
    </div>
    """


def app_status_hotkey_form_html(
    key: str,
    hotkey: BrowserHotkeyConfig,
    named_h3_cells: list[Any],
    registered_people: list[dict[str, str]],
    defined_tags: list[Any],
) -> str:
    return f"""
    <form action="/settings/hotkey" method="post" class="hotkey-form">
      <input type="hidden" name="key" value="{html.escape(key)}">
      <strong>{html.escape(key)}</strong>
      <select name="action" aria-label="Handling for hurtigtast {html.escape(key)}" data-hotkey-action>
        <option value=""{selected_attr(not hotkey.action)}>Av</option>
        <option value="h3"{selected_attr(hotkey.action == "h3")}>Sett H3</option>
        <option value="manual_date"{selected_attr(hotkey.action == "manual_date")}>Sett dato</option>
        <option value="person"{selected_attr(hotkey.action == "person")}>Legg til person</option>
        <option value="tag"{selected_attr(hotkey.action == "tag")}>Sett tagg</option>
      </select>
      <span class="hotkey-fields" data-hotkey-fields="h3">
        <select name="h3_cell" aria-label="H3-celle">{hotkey_h3_options_html(hotkey.h3_cell, named_h3_cells)}</select>
      </span>
      <span class="hotkey-fields" data-hotkey-fields="person">
        <select name="person_name" aria-label="Person">{hotkey_person_options_html(hotkey.person_name, registered_people)}</select>
      </span>
      <span class="hotkey-fields" data-hotkey-fields="tag">
        <select name="tag_name" aria-label="Tagg">{hotkey_tag_options_html(hotkey.tag_name, defined_tags)}</select>
      </span>
      <span class="hotkey-fields hotkey-date-fields" data-hotkey-fields="manual_date">
        <select name="mode" aria-label="Datotype">
          <option value="exact"{selected_attr(hotkey.mode == "exact")}>Eksakt</option>
          <option value="uncertain"{selected_attr(hotkey.mode == "uncertain")}>Usikker</option>
          <option value="between"{selected_attr(hotkey.mode == "between")}>Intervall</option>
        </select>
        <input name="date" value="{html.escape(hotkey.date)}" placeholder="YYYY-MM-DD" aria-label="Dato">
        <select name="uncertainty" aria-label="Usikkerhet">
          <option value="1d"{selected_attr(hotkey.uncertainty == "1d")}>±1 dag</option>
          <option value="1w"{selected_attr(hotkey.uncertainty == "1w")}>±1 uke</option>
          <option value="1m"{selected_attr(hotkey.uncertainty == "1m")}>±1 måned</option>
          <option value="1y"{selected_attr(hotkey.uncertainty == "1y")}>±1 år</option>
        </select>
        <input name="date_from" value="{html.escape(hotkey.date_from)}" placeholder="Fra YYYY-MM-DD" aria-label="Fra-dato">
        <input name="date_to" value="{html.escape(hotkey.date_to)}" placeholder="Til YYYY-MM-DD" aria-label="Til-dato">
        <input name="note" value="{html.escape(hotkey.note)}" placeholder="Notat" aria-label="Datonotat">
      </span>
      <span class="hotkey-fields hotkey-empty-fields" data-hotkey-fields=""></span>
      <button type="submit" class="nav-button">Lagre</button>
    </form>
    """


def hotkey_h3_options_html(selected_h3_cell: str, named_h3_cells: list[Any]) -> str:
    selected = selected_h3_cell.strip()
    options = [f'<option value=""{selected_attr(not selected)}>Velg H3-celle</option>']
    has_selected = False
    for row in named_h3_cells:
        cell = str(row["h3_cell"])
        name = str(row["name"])
        is_selected = cell == selected
        has_selected = has_selected or is_selected
        options.append(
            f'<option value="{html.escape(cell)}"{selected_attr(is_selected)}>'
            f'{html.escape(name)} ({html.escape(h3_resolution_option_label(cell))})'
            "</option>"
        )
    if selected and not has_selected:
        options.append(f'<option value="{html.escape(selected)}" selected>Ikke navngitt: {html.escape(selected)}</option>')
    return "".join(options)


def hotkey_person_options_html(selected_person: str, registered_people: list[dict[str, str]]) -> str:
    selected = selected_person.strip()
    options = [f'<option value=""{selected_attr(not selected)}>Velg person</option>']
    has_selected = False
    for person in registered_people:
        name = str(person["name"])
        is_selected = name == selected
        has_selected = has_selected or is_selected
        options.append(f'<option value="{html.escape(name)}"{selected_attr(is_selected)}>{html.escape(name)}</option>')
    if selected and not has_selected:
        options.append(f'<option value="{html.escape(selected)}" selected>{html.escape(selected)}</option>')
    return "".join(options)


def hotkey_tag_options_html(selected_tag: str, defined_tags: list[Any]) -> str:
    selected = selected_tag.strip()
    options = [f'<option value=""{selected_attr(not selected)}>Velg tagg</option>']
    has_selected = False
    for tag in defined_tags:
        name = str(tag["name"])
        is_selected = name == selected
        has_selected = has_selected or is_selected
        options.append(f'<option value="{html.escape(name)}"{selected_attr(is_selected)}>{html.escape(name)}</option>')
    if selected and not has_selected:
        options.append(f'<option value="{html.escape(selected)}" selected>{html.escape(selected)}</option>')
    return "".join(options)


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
    image_counts = h3_cell_image_counts(target, named_h3_cells)
    return shell_page_html(
        "Rediger H3-celler",
        f"""
        <h1>Navngi H3-celler</h1>
        <section class="custom-geo-places">
        <p>På denne siden gir du navn til H3-celler. Disse navnene brukes for
        å velge på siden <a href="/settings">innstillinger</a> hvilken celle som brukes til
        å sette sted for bilder som mangler GPS-lokasjon.
        </p>
          <p>Bruk oppløsning 11 (høyeste oppløsning) for å markere et enkelt hus eller
          en veldig presis lokasjon, hvis du vet nøyaktig plassering. Hvis huset ligger på
          grensen mellom to H3-celler i oppløsning 11, så velger du bare den som
          dekker mest av huset. Det er ikke så viktig.</p>
          <p>Hvis du har mange bilder fra en gammel sydentur til Kreta, så kan du i
          første omgang finne en H3-celle som er så stor at den dekker hele Kreta og
          markere bildene med den. Og hvis du seinere ønsker å plassere mer presis
          for enkelte bilder, så gjør du det.</p>
          <p>Sletting av registrerte navn og trygt. Ingen bilder eller registreringer
          av manuel h3-lokasjon fjernes.</p>
          {h3_cell_form_html()}
        </section>
        <section class="custom-geo-places">
          <h2>Registrerte navn</h2>
          {h3_cell_list_html(named_h3_cells, image_counts)}
        </section>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def h3_cell_form_html(row: Any | None = None) -> str:
    h3_cell = "" if row is None else str(row["h3_cell"])
    name = "" if row is None else str(row["name"])
    button_text = "Legg til H3-celle" if row is None else "Lagre"
    original_h3_cell_input = (
        f'<input type="hidden" name="original_h3_cell" value="{html.escape(h3_cell)}">' if row is not None else ""
    )
    delete_button = (
        '<button class="danger-button" type="submit" '
        'formaction="/settings/h3-cell-delete" formmethod="post" '
        'data-confirm-submit="Slette navn gitt til H3-celle?">Slett</button>'
        if row is not None
        else ""
    )
    return f"""
    <form action="/settings/h3-cell" method="post" class="custom-place-form">
      {original_h3_cell_input}
      <label>Navn <input name="name" value="{html.escape(name)}" autocomplete="off"></label>
      <label>H3-id. Bildebank bruker H3-oppløsning 0 til {MAX_NAMED_H3_RESOLUTION}. <input name="h3_cell" value="{html.escape(h3_cell)}" autocomplete="off"></label>
      <div class="custom-place-actions">
        <button type="submit">{button_text}</button>
        {delete_button}
      </div>
    </form>
    """


def h3_cell_image_counts(target: Path, named_h3_cells: list[Any]) -> dict[str, int | None]:
    if not named_h3_cells or not db.db_path_for_target(target).is_file():
        return {}
    conn = db.connect(target)
    try:
        counts: dict[str, int | None] = {}
        for row in named_h3_cells:
            h3_cell = str(row["h3_cell"])
            try:
                column = h3_column_for_resolution(h3_resolution(h3_cell))
                counts[h3_cell] = db.geo_place_count(conn, cells_by_column=[(column, h3_cell)])
            except Exception:  # noqa: BLE001 - settings page should still render if a saved cell is invalid
                counts[h3_cell] = None
        return counts
    finally:
        conn.close()


def h3_cell_list_html(named_h3_cells: list[Any], image_counts: dict[str, int | None] | None = None) -> str:
    if not named_h3_cells:
        return '<p class="meta">Ingen H3-celler er navngitt ennå.</p>'
    counts = image_counts or {}
    rows = "\n".join(h3_cell_edit_html(row, counts.get(str(row["h3_cell"]))) for row in named_h3_cells)
    return f"""
    <div class="custom-place-list h3-cell-list">
      {rows}
    </div>
    """


def h3_cell_edit_html(row: Any, image_count: int | None = None) -> str:
    h3_cell = str(row["h3_cell"])
    name = str(row["name"])
    count_text = "ukjent" if image_count is None else str(image_count)
    return f"""
    <details class="custom-place-edit">
      <summary>
        <span class="custom-place-name">{html.escape(name)}</span>
        <span class="status">{html.escape(count_text)} bilder</span>
        <span class="status">H3-{html.escape(h3_resolution_value_label(h3_cell))}</span>
        <span class="status"><a href="{html.escape(h3_cell_h3geo_url(h3_cell))}" target="_blank" rel="noopener">{html.escape(h3_cell)}</a></span>
      </summary>
      <div class="custom-place-edit-body">
        {h3_cell_form_html(row)}
      </div>
    </details>
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


def save_h3_cell_name(target: Path, *, h3_cell: str, name: str, original_h3_cell: str = "") -> None:
    clean_h3_cell = h3_cell.strip()
    clean_original_h3_cell = original_h3_cell.strip()
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Navn mangler.")
    h3_resolution_for_named_place(clean_h3_cell)
    with TargetLock(target, command="h3-cell-name-save"):
        conn = db.connect(target)
        try:
            if clean_original_h3_cell and clean_original_h3_cell != clean_h3_cell:
                h3_resolution_for_named_place(clean_original_h3_cell)
                if db.geo_place_name(conn, clean_original_h3_cell) is None:
                    raise ValueError("H3-cellen som skulle oppdateres finnes ikke.")
                if db.geo_place_name(conn, clean_h3_cell) is not None:
                    raise ValueError("H3-cellen er allerede navngitt.")
                db.set_geo_place_name(conn, clean_original_h3_cell, "")
            db.set_geo_place_name(conn, clean_h3_cell, clean_name)
            conn.commit()
        finally:
            conn.close()


def delete_h3_cell_name(target: Path, *, h3_cell: str) -> None:
    clean_h3_cell = h3_cell.strip()
    h3_resolution_for_named_place(clean_h3_cell)
    with TargetLock(target, command="h3-cell-name-delete"):
        conn = db.connect(target)
        try:
            db.set_geo_place_name(conn, clean_h3_cell, "")
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
