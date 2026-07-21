from __future__ import annotations

import html
import sqlite3
import urllib.parse
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import BrowserHotkeyConfig, FaceRecognitionConfig, HOTKEY_KEYS
from .db_schema import SchemaMigrationRequired
from .geo import h3_resolution
from .html_paths import display_relative_path
from .media import media_kind
from .media_cache import cached_image_dimensions
from .server_browser_queries import (
    browser_date_for_item,
    first_source_day_item,
    is_image_item,
    month_key_for_item,
    should_filter_out_of_focus,
    source_item_by_id,
    source_item_count,
    source_month_keys,
    valid_day_key,
    valid_month_key,
)
from .server_browser_sidecars import (
    hidden_sidecar_main_image,
    motion_video_for_image,
    raw_sidecar_for_image,
)
from .video_previews import VIDEO_PREVIEW_SOURCE_EXTENSIONS, existing_video_preview_path
from .server_browser_info_html import (
    display_short_date,
    gps_location_badge_html,
    gps_source_is_manual_h3,
    manual_date_text,
    manual_h3_badge_html,
    manual_h3_cell,
    manual_h3_cell_name,
    manual_h3_place_name,
)
from .server_browser_sources import (
    BrowserSource,
    all_browser_source,
    is_filtered_source,
    person_browser_source,
    source_includes_deleted,
    source_item_url,
    source_month_url,
    source_year_url,
)


PageRenderer = Callable[[str, str], str]
Breadcrumb = tuple[str, str | None] | tuple[str, str | None, str | None]
TAG_CONTROL_ROWS_CACHE_MAX_SIZE = 8
TAG_CONTROL_ROWS_CACHE: dict[tuple[str, int], tuple[tuple[str, str], ...]] = {}
MONTH_NAMES = {
    "01": "Januar",
    "02": "Februar",
    "03": "Mars",
    "04": "April",
    "05": "Mai",
    "06": "Juni",
    "07": "Juli",
    "08": "August",
    "09": "September",
    "10": "Oktober",
    "11": "November",
    "12": "Desember",
}


def clear_tag_control_rows_cache() -> None:
    TAG_CONTROL_ROWS_CACHE.clear()


def item_view_rotation(item: Any) -> int:
    try:
        return db.normalize_view_rotation(item["view_rotation_degrees"])
    except (KeyError, IndexError):
        return 0


def rotation_style_attr(
    item: Any,
    target: Path | None = None,
    *,
    write_metadata_cache: bool = True,
) -> str:
    rotation = item_view_rotation(item)
    if rotation == 0:
        return ""
    style = f"transform: rotate({rotation}deg);"
    if rotation in {90, 270}:
        ratio = item_media_width_height_ratio(item, target, write_metadata_cache=write_metadata_cache)
        if ratio is not None:
            style += f" --quarter-turn-width: {ratio * 100:.6f}%;"
    return f' style="{style}" data-view-rotation="{rotation}"'


def item_media_width_height_ratio(
    item: Any,
    target: Path | None = None,
    *,
    write_metadata_cache: bool = True,
) -> float | None:
    try:
        width = int(item["media_width"])
        height = int(item["media_height"])
    except (KeyError, IndexError, TypeError, ValueError):
        width = 0
        height = 0
    if (width <= 0 or height <= 0) and target is not None and write_metadata_cache:
        dimensions = cached_image_dimensions(target, db.absolute_target_path(target, Path(str(item["target_path"]))))
        if dimensions is not None:
            width = dimensions.width
            height = dimensions.height
    if width <= 0 or height <= 0:
        return None
    return width / height


def media_link_class_attr(item: Any) -> str:
    rotation = item_view_rotation(item)
    css_class = "media-link quarter-turn" if rotation in {90, 270} else "media-link"
    return f' class="{css_class}"'
def item_page_html(
    target: Path,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
    *,
    page_html: PageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
    manual_person_controls_enabled: bool = True,
    person_reference_links_enabled: bool = False,
    hotkey_hints_enabled: bool = False,
    hotkeys: Mapping[str, BrowserHotkeyConfig] | None = None,
    read_only: bool = False,
) -> str:
    return source_item_page_html(
        target,
        all_browser_source(),
        item,
        previous_item,
        next_item,
        month_nav,
        page_html=page_html,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        face_config=face_config,
        manual_person_controls_enabled=manual_person_controls_enabled,
        person_reference_links_enabled=person_reference_links_enabled,
        hotkey_hints_enabled=hotkey_hints_enabled,
        hotkeys=hotkeys,
        read_only=read_only,
    )


def _source_item_face_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None,
    *,
    face_enabled: bool,
    manual_person_controls_enabled: bool,
    person_reference_links_enabled: bool = False,
    face_edit_controls_enabled: bool = True,
    timing_callback: Callable[[str, float], None] | None = None,
) -> tuple[str, str, str, bool]:
    if not face_enabled:
        return "", "", "", False

    from .server_faces import (
        confirmed_face_people_text_html,
        current_face_db_path_and_mtime,
        faces_button_html,
        faces_overlay_html,
        manual_person_file_controls_html,
        people_for_file_from_face_db,
        people_links_html,
        source_duplicate_confirmed_faces_warning_html,
        registered_people_options_html_from_face_db,
        unconfirmed_face_count_for_item_from_face_db,
    )

    face_db_path, face_db_mtime_ns = current_face_db_path_and_mtime(target, face_config)
    if face_db_mtime_ns is None:
        return "", "", "", False

    start = time.perf_counter()
    people_data, confirmed_face_people_data = people_for_file_from_face_db(
        face_db_path,
        face_db_mtime_ns,
        int(item["id"]),
        person_reference_links_enabled=person_reference_links_enabled,
    )
    person_has_confirmed_face = False
    if source.person_name is not None:
        person_has_confirmed_face = any(
            str(person.get("name")) == source.person_name
            for person in confirmed_face_people_data
        )
    if timing_callback is not None:
        timing_callback("html_people_for_file", start)

    manual_person_controls = ""
    if manual_person_controls_enabled:
        start = time.perf_counter()
        manual_person_controls = manual_person_file_controls_html(
            target,
            item,
            people_data,
            face_config,
            registered_options_html=registered_people_options_html_from_face_db(face_db_path, face_db_mtime_ns),
        )
        if timing_callback is not None:
            timing_callback("html_manual_person_controls", start)

    start = time.perf_counter()
    face_rail_html = people_links_html(
        people_data,
        "Personer i bildet",
        manual_person_controls=manual_person_controls,
        file_id=int(item["id"]),
        manual_remove_enabled=manual_person_controls_enabled,
    )
    show_unconfirmed_faces = source.person_name is None and face_edit_controls_enabled
    unconfirmed_face_count = (
        unconfirmed_face_count_for_item_from_face_db(face_db_path, face_db_mtime_ns, int(item["id"]))
        if show_unconfirmed_faces
        else 0
    )
    face_rail_html += faces_button_html(unconfirmed_face_count, int(item["id"])) if show_unconfirmed_faces else ""
    face_rail_html += confirmed_face_people_text_html(confirmed_face_people_data)
    faces_overlay = faces_overlay_html(item) if unconfirmed_face_count > 0 else ""
    duplicate_warning = source_duplicate_confirmed_faces_warning_html(target, source, item, face_config)
    if timing_callback is not None:
        timing_callback("html_face_rail", start)
    return face_rail_html, faces_overlay, duplicate_warning, person_has_confirmed_face


def _source_item_controls_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
    face_config: FaceRecognitionConfig | None,
    *,
    associated_file_buttons: str,
    manual_h3_button: str,
    face_enabled: bool,
    person_has_confirmed_face: bool,
    hide_out_of_focus: bool,
    conn: sqlite3.Connection | None,
    timing_callback: Callable[[str, float], None] | None = None,
    read_only: bool = False,
) -> str:
    from .server_shell import (
        face_matches_button_html,
        face_suggest_button_html,
        face_toggle_button_html,
        source_controls_html,
        suggestion_toggle_button_html,
    )

    start = time.perf_counter()
    controls = source_controls_html(
        source,
        month_nav,
        previous_item,
        next_item,
        rotation_buttons="" if read_only else rotation_buttons_html(source, item),
        manual_date_button="" if read_only else manual_date_button_html(item) + manual_h3_button,
        associated_file_buttons=associated_file_buttons,
        face_toggle_button=face_toggle_button_html(source, item, face_enabled=face_enabled),
        face_suggest_button="" if read_only else face_suggest_button_html(face_enabled=face_enabled),
        face_matches_button=face_matches_button_html(int(item["id"]), face_enabled=face_enabled),
        suggestion_toggle_button=suggestion_toggle_button_html(
            source,
            item,
            face_enabled=face_enabled,
            href=suggestion_toggle_href(
                target,
                source,
                item,
                face_config,
                person_has_confirmed_face=person_has_confirmed_face,
                hide_out_of_focus=hide_out_of_focus,
                conn=conn,
            ),
        ),
        unconfirm_buttons="",
        delete_button="" if read_only else delete_button_html(source, item, previous_item, next_item),
        year_links_to_year_pages=True,
        previous_year_fallback_url=previous_year_overview_url(
            target,
            source,
            month_key_for_item(target, item),
            month_nav,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
            conn=conn,
            current_key_in_source=True,
        ),
        previous_month_fallback_url=previous_month_overview_url(
            source,
            month_key_for_item(target, item),
            month_nav,
        ),
    )
    if timing_callback is not None:
        timing_callback("html_controls", start)
    return controls


def previous_month_overview_url(
    source: BrowserSource,
    current_key: str,
    month_nav: dict[str, str | None],
) -> str | None:
    if month_nav["previous_month"] is not None or not valid_month_key(current_key):
        return None
    return source.root_url if is_filtered_source(source) else "/years"


def previous_year_overview_url(
    target: Path,
    source: BrowserSource,
    current_key: str,
    month_nav: dict[str, str | None],
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
    current_key_in_source: bool = False,
) -> str | None:
    if month_nav["previous_year"] is not None or not valid_month_key(current_key):
        return None
    if current_key_in_source:
        return source.root_url if is_filtered_source(source) else "/years"
    month_keys = source_month_keys(
        target,
        source,
        face_config,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
    )
    if current_key not in month_keys:
        return None
    first_year = min(key[:4] for key in month_keys)
    if current_key[:4] != first_year:
        return None
    return source.root_url if is_filtered_source(source) else "/years"


def _source_item_side_panel_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    *,
    hide_out_of_focus: bool,
    face_rail_html: str,
    suffix_html: str,
    conn: sqlite3.Connection | None,
    timing_start: float,
    timing_callback: Callable[[str, float], None] | None = None,
    read_only: bool = False,
) -> str:
    out_of_focus_redirect_url = hidden_after_out_of_focus_tag_redirect_url(
        source,
        previous_item,
        next_item,
        hide_out_of_focus=hide_out_of_focus,
    )
    side_panel = item_side_panel_html(
        target,
        item,
        out_of_focus_redirect_url=out_of_focus_redirect_url,
        extra_html=face_rail_html,
        suffix_html=suffix_html,
        conn=conn,
        read_only=read_only,
    )
    if timing_callback is not None:
        timing_callback("html_tag_controls", timing_start)
    return side_panel


def _source_item_header_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    controls: str,
    duplicate_warning: str,
    face_config: FaceRecognitionConfig | None,
    *,
    face_enabled: bool,
    openclip_enabled: bool,
    all_items_url: str | None,
    all_items_label: str,
    hide_out_of_focus: bool,
    conn: sqlite3.Connection | None,
    source_item_count_value: int | None,
    first_day_item_id: int | None = None,
    timing_callback: Callable[[str, float], None] | None = None,
    read_only: bool = False,
) -> str:
    from .server_shell import app_header_html

    start = time.perf_counter()
    title_html = source_item_breadcrumb_html(
        target,
        source,
        item,
        face_config=face_config,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
        source_item_count_value=source_item_count_value,
        first_day_item_id=first_day_item_id,
    )
    if timing_callback is not None:
        timing_callback("html_breadcrumb", start)

    start = time.perf_counter()
    header_html = app_header_html(
        source.title,
        source=source,
        item=item,
        title_html=title_html,
        controls=controls,
        message_html=duplicate_warning,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        all_items_url=all_items_url,
        all_items_label=all_items_label,
    )
    if timing_callback is not None:
        timing_callback("html_app_header", start)
    return header_html


def source_item_page_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
    *,
    page_html: PageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
    manual_person_controls_enabled: bool = True,
    person_reference_links_enabled: bool = False,
    hotkey_hints_enabled: bool = False,
    hotkeys: Mapping[str, BrowserHotkeyConfig] | None = None,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
    source_item_count_value: int | None = None,
    first_day_item_id: int | None = None,
    timing_callback: Callable[[str, float], None] | None = None,
    read_only: bool = False,
) -> str:
    target_path = Path(str(item["target_path"]))
    start = time.perf_counter()
    media = source_item_media_html(target, source, item, face_config, read_only=read_only)
    if timing_callback is not None:
        timing_callback("html_media", start)
    face_rail_html, faces_overlay, duplicate_warning, person_has_confirmed_face = _source_item_face_html(
        target,
        source,
        item,
        face_config,
        face_enabled=face_enabled,
        manual_person_controls_enabled=manual_person_controls_enabled and not read_only,
        person_reference_links_enabled=person_reference_links_enabled,
        face_edit_controls_enabled=not read_only,
        timing_callback=timing_callback,
    )
    start = time.perf_counter()
    hotkey_hints_html = (
        hotkey_hints_panel_html(target, hotkeys or {}, conn=conn)
        if hotkey_hints_enabled and not read_only
        else ""
    )
    if timing_callback is not None:
        timing_callback("html_hotkey_hints", start)
    start = time.perf_counter()
    motion_video, raw_sidecar = associated_files_for_item(target, item, conn=conn)
    associated_file_buttons = associated_file_buttons_html(motion_video, raw_sidecar)
    if timing_callback is not None:
        timing_callback("html_associated_files", start)
    named_h3_cells = named_manual_h3_cells(target, conn=conn) if not read_only else []
    controls = _source_item_controls_html(
        target,
        source,
        item,
        previous_item,
        next_item,
        month_nav,
        face_config,
        associated_file_buttons=associated_file_buttons,
        face_enabled=face_enabled,
        person_has_confirmed_face=person_has_confirmed_face,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
        timing_callback=timing_callback,
        read_only=read_only,
        manual_h3_button=manual_h3_button_html(item, named_h3_cells) if not read_only else "",
    )
    start = time.perf_counter()
    info_overlay = image_info_overlay_html()
    manual_date_overlay = "" if read_only else manual_date_overlay_html()
    manual_h3_overlay = "" if read_only else manual_h3_overlay_html(item, named_h3_cells)
    comment_dialog = "" if read_only else comment_dialog_html(item)
    face_suggest_dialog = ""
    if face_enabled and not read_only:
        from .server_faces import face_suggest_dialog_html

        threshold = face_config.suggest_threshold if face_config is not None else 0.6
        face_suggest_dialog = face_suggest_dialog_html(
            threshold,
            return_url=source_item_url(source, int(item["id"])),
        )
    side_panel = _source_item_side_panel_html(
        target,
        source,
        item,
        previous_item,
        next_item,
        hide_out_of_focus=hide_out_of_focus,
        face_rail_html=face_rail_html,
        suffix_html=hotkey_hints_html,
        conn=conn,
        timing_start=start,
        timing_callback=timing_callback,
        read_only=read_only,
    )
    start = time.perf_counter()
    all_items_link = all_browser_item_link(target, source, item, hide_out_of_focus=hide_out_of_focus, conn=conn)
    if timing_callback is not None:
        timing_callback("html_all_items_link", start)
    all_items_url = all_items_link[0] if all_items_link is not None else None
    all_items_label = all_items_link[1] if all_items_link is not None else "Åpne i alle bilder"
    source_url_attr = ""
    if source.text_filter is not None:
        source_url_attr = f' data-browser-source-url="{html.escape(source.root_url)}"'
    hotkeys_enabled_attr = ' data-browser-hotkeys-enabled="true"' if hotkey_hints_enabled and not read_only else ""
    header_html = _source_item_header_html(
        target,
        source,
        item,
        controls,
        duplicate_warning,
        face_config=face_config,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
        all_items_url=all_items_url,
        all_items_label=all_items_label,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
        source_item_count_value=source_item_count_value,
        first_day_item_id=first_day_item_id,
        timing_callback=timing_callback,
    )
    start = time.perf_counter()
    result = page_html(
        f"{source.title}: {target_path.name}",
        f"""
        <main class="server-browser" data-browser-item-id="{int(item["id"])}"{source_url_attr}{hotkeys_enabled_attr}>
          {header_html}
          <div class="stage-shell">
            {side_panel}
            <section class="stage">
              {media}
              {comment_overlay_html(item)}
            </section>
          </div>
        </main>
        {faces_overlay}
        {info_overlay}
        {manual_date_overlay}
        {manual_h3_overlay}
        {comment_dialog}
        {face_suggest_dialog}
        """,
    )
    if timing_callback is not None:
        timing_callback("html_page", start)
    return result


def associated_files_for_item(
    target: Path,
    item: Any,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[Any | None, Any | None]:
    if not is_image_item(item):
        return None, None
    try:
        folded_filename = str(item["original_filename"]).casefold()
    except (KeyError, IndexError):
        folded_filename = ""
    motion_video = motion_video_for_image(target, item, conn=conn) if folded_filename.endswith(".mp.jpg") else None
    raw_sidecar = raw_sidecar_for_image(target, item, conn=conn)
    return motion_video, raw_sidecar


def suggestion_toggle_href(
    target: Path,
    source: BrowserSource,
    item: Any | None,
    face_config: FaceRecognitionConfig | None = None,
    *,
    person_has_confirmed_face: bool | None = None,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    if source.person_name is None:
        return None
    target_source = person_browser_source(
        source.person_name,
        include_suggestions=not source.include_suggestions,
        show_faces=source.show_faces,
    )
    if item is None:
        return target_source.root_url
    file_id = int(item["id"])
    if source.person_name is not None and not source.include_suggestions and target_source.include_suggestions:
        return source_item_url(target_source, file_id)
    if source.person_name is not None and source.include_suggestions and not target_source.include_suggestions:
        return source_item_url(target_source, file_id) if person_has_confirmed_face else target_source.root_url
    target_item = source_item_by_id(
        target,
        target_source,
        file_id,
        face_config,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
    )
    if target_item is None:
        return target_source.root_url
    return source_item_url(target_source, file_id)




def associated_file_buttons_html(*associated_files: Any | None) -> str:
    return "\n".join(
        associated_file_button_html(associated_file)
        for associated_file in associated_files
        if associated_file is not None
    )


def associated_file_button_html(associated_file: Any) -> str:
    filename = str(associated_file["stored_filename"])
    extension = Path(filename).suffix.upper() or "Fil"
    url = "/filter/" + urllib.parse.quote(f"filename:{filename}", safe="") + f"/item/{int(associated_file['id'])}"
    return (
        f'<a class="nav-button associated-file-button" href="{html.escape(url)}" '
        f'title="Åpne tilknyttet fil {html.escape(filename)}">{html.escape(extension)}</a>'
    )


def source_item_breadcrumb_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
    source_item_count_value: int | None = None,
    first_day_item_id: int | None = None,
) -> str:
    month_key = month_key_for_item(target, item)
    filename = html.escape(str(item["stored_filename"]))
    file_id = int(item["id"])
    filename_link = (
        f'<a href="#" data-open-info data-info-item="{file_id}" '
        f'title="Vis detaljer om bildet" '
        f'aria-label="Åpne bildeinfo for {filename}">{filename}</a>'
    )
    source_label, source_title = source_breadcrumb_label(
        target,
        source,
        face_config,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
        source_item_count_value=source_item_count_value,
    )
    if not valid_month_key(month_key):
        return breadcrumb_html([(source_label, source.root_url, source_title)], filename_link)
    year, month = month_key.split("-", 1)
    month_name = MONTH_NAMES.get(month, month_key)
    browser_date = browser_date_for_item(item)
    day_crumb: Breadcrumb | None = None
    if valid_day_key(browser_date):
        if first_day_item_id is None:
            first_day_item = first_source_day_item(
                target,
                source,
                browser_date,
                face_config,
                hide_out_of_focus=hide_out_of_focus,
                conn=conn,
            )
            first_day_item_id = int(first_day_item["id"]) if first_day_item is not None else None
        if first_day_item_id is not None:
            day_crumb = (str(int(browser_date[8:10])), source_item_url(source, first_day_item_id))
    crumbs: list[Breadcrumb]
    if source == all_browser_source():
        crumbs = [
            ("År", "/years"),
            (year, source_year_url(source, year)),
            (month_name, source_month_url(source, month_key)),
        ]
    else:
        crumbs = [
            (source_label, source.root_url, source_title),
            (year, source_year_url(source, year)),
            (month_name, source_month_url(source, month_key)),
        ]
    if day_crumb is not None:
        crumbs.append(day_crumb)
    return breadcrumb_html(crumbs, filename_link)


def source_breadcrumb_label(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
    source_item_count_value: int | None = None,
) -> tuple[str, str | None]:
    label = source.person_name if source.person_name is not None else source.title
    if source.text_filter is None:
        return label, None
    count = (
        source_item_count_value
        if source_item_count_value is not None
        else source_item_count(target, source, face_config, hide_out_of_focus=hide_out_of_focus, conn=conn)
    )
    match_text = "1 treff" if count == 1 else f"{count} treff"
    return f"{label} ({match_text})", f"{match_text} i filtersøket"


def breadcrumb_html(
    crumbs: Sequence[Breadcrumb],
    final_html: str,
) -> str:
    parts = []
    for crumb in crumbs:
        label, url = crumb[0], crumb[1]
        title = crumb[2] if len(crumb) > 2 else None
        title_attr = f' title="{html.escape(title)}"' if title else ""
        parts.append(
            f'<a href="{html.escape(url)}"{title_attr}>{html.escape(label)}</a>'
            if url is not None
            else html.escape(label)
        )
    parts.append(final_html)
    return '<nav class="breadcrumb" aria-label="Plassering">' + '<span class="sep">/</span>'.join(parts) + "</nav>"


def all_browser_item_link(
    target: Path,
    source: BrowserSource,
    item: Any,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> tuple[str, str] | None:
    if source == all_browser_source():
        return None
    main_image = hidden_sidecar_main_image(target, item, conn=conn)
    if main_image is not None:
        return source_item_url(all_browser_source(), int(main_image["id"])), "Vis JPG-bildet"
    if not hide_out_of_focus:
        return None
    if should_filter_out_of_focus(source, hide_out_of_focus) and not source_includes_deleted(source):
        return source_item_url(all_browser_source(), int(item["id"])), "Åpne i alle bilder"
    visible_item = source_item_by_id(
        target,
        all_browser_source(),
        int(item["id"]),
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
    )
    if visible_item is None:
        return "/", "Åpne i alle bilder"
    return source_item_url(all_browser_source(), int(item["id"])), "Åpne i alle bilder"




def hidden_after_out_of_focus_tag_redirect_url(
    source: BrowserSource,
    previous_item: Any | None,
    next_item: Any | None,
    *,
    hide_out_of_focus: bool = False,
) -> str:
    if not should_filter_out_of_focus(source, hide_out_of_focus):
        return ""
    if next_item is not None:
        return source_item_url(source, int(next_item["id"]))
    if previous_item is not None:
        return source_item_url(source, int(previous_item["id"]))
    return source.root_url


def item_side_panel_html(
    target: Path,
    item: Any,
    *,
    out_of_focus_redirect_url: str = "",
    extra_html: str = "",
    suffix_html: str = "",
    conn: sqlite3.Connection | None = None,
    read_only: bool = False,
) -> str:
    file_id = int(item["id"])
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        defined_tags = () if read_only else tag_control_rows(target, conn)
        active_names = active_tag_name_keys_for_file(conn, file_id)
        manual_h3_name = manual_h3_place_name(conn, item)
        manual_h3_cell_value = manual_h3_cell(item)
        location_controls = "" if read_only else remove_manual_location_button_html(item)
    finally:
        if owned_conn:
            conn.close()
    buttons = []
    if not read_only:
        comment = item_string_value(item, "comment")
        active_class = " active" if comment else ""
        pressed = "true" if comment else "false"
        buttons.append(
            f'<button class="comment-button{active_class}" type="button" '
            f'data-open-item-comment data-comment-item="{file_id}" '
            f'aria-pressed="{pressed}">Kommentar</button>'
        )
    for tag in defined_tags:
        tag_name, tag_name_key = tag
        active = tag_name_key in active_names
        pressed = "true" if active else "false"
        active_class = " active" if active else ""
        redirect_attr = ""
        if tag_name == db.SYSTEM_TAG_OUT_OF_FOCUS and out_of_focus_redirect_url:
            redirect_attr = f' data-tag-hide-redirect="{html.escape(out_of_focus_redirect_url)}"'
        buttons.append(
            f'<button class="tag-toggle{active_class}" type="button" '
            f'title="Klikk for å legge til eller fjerne taggen fra bildet" '
            f'data-tag-toggle="{file_id}" data-tag-name="{html.escape(tag_name)}" '
            f'aria-pressed="{pressed}"{redirect_attr}>{html.escape(tag_name)}</button>'
        )
    location_status = (
        manual_h3_badge_html(manual_h3_name, manual_h3_cell_value, extra_html=location_controls)
        if gps_source_is_manual_h3(item)
        else gps_location_badge_html(item, extra_html=location_controls)
    )
    return f'<aside class="tag-rail" aria-label="Tagger">{"".join(buttons)}{location_status}{extra_html}{suffix_html}</aside>'


def hotkey_hints_panel_html(
    target: Path,
    hotkeys: Mapping[str, BrowserHotkeyConfig],
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    rows = []
    for key in HOTKEY_KEYS:
        label = hotkey_hint_label(target, hotkeys.get(key, BrowserHotkeyConfig()), conn=conn)
        if label:
            rows.append(f'<div class="hotkey-hint"><span>{html.escape(key)}:</span> {html.escape(label)}</div>')
    if not rows:
        return ""
    heading = '<div class="hotkey-hints-heading">Hurtigtaster aktivert:</div>'
    return '<section class="hotkey-hints" aria-label="Hurtigtaster">' + heading + "".join(rows) + "</section>"


def hotkey_hint_label(
    target: Path,
    hotkey: BrowserHotkeyConfig,
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    if hotkey.action == "h3" and hotkey.h3_cell:
        name = manual_h3_cell_name(target, hotkey.h3_cell, conn=conn) or hotkey.h3_cell
        return f"Sett H3 til {name}"
    if hotkey.action == "person" and hotkey.person_name:
        return f"Legg til {hotkey.person_name}"
    if hotkey.action == "tag" and hotkey.tag_name:
        return f"Sett tagg {db.normalize_tag_name(hotkey.tag_name)}"
    if hotkey.action == "manual_date":
        text = hotkey_date_hint_text(hotkey)
        return f"Sett dato til {text}" if text else ""
    return ""


def hotkey_date_hint_text(hotkey: BrowserHotkeyConfig) -> str:
    if hotkey.mode == "exact":
        return display_short_date(hotkey.date)
    if hotkey.mode == "uncertain":
        date_text = display_short_date(hotkey.date)
        return f"{date_text} ±{hotkey.uncertainty}" if date_text and hotkey.uncertainty else date_text
    if hotkey.mode == "between":
        start = display_short_date(hotkey.date_from)
        end = display_short_date(hotkey.date_to)
        if start and end:
            return f"{start}-{end}"
    return ""




def tag_control_rows(target: Path, conn: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    db_path = db.db_path_for_target(target)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    cache_key = (str(target.resolve()), mtime_ns)
    cached = TAG_CONTROL_ROWS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    rows = conn.execute(
        """
        SELECT name, name_key
        FROM tags
        ORDER BY CASE kind WHEN 'system' THEN 0 ELSE 1 END, name_key
        """
    )
    cached = tuple((str(row["name"]), str(row["name_key"])) for row in rows)
    if len(TAG_CONTROL_ROWS_CACHE) >= TAG_CONTROL_ROWS_CACHE_MAX_SIZE:
        TAG_CONTROL_ROWS_CACHE.pop(next(iter(TAG_CONTROL_ROWS_CACHE)))
    TAG_CONTROL_ROWS_CACHE[cache_key] = cached
    return cached


def active_tag_name_keys_for_file(conn: sqlite3.Connection, file_id: int) -> set[str]:
    rows = conn.execute(
        """
        SELECT tags.name_key
        FROM tags
        JOIN file_tags ON file_tags.tag_id = tags.id
        WHERE file_tags.file_id = ?
        """,
        (file_id,),
    )
    return {str(row["name_key"]) for row in rows}


def source_item_media_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
    *,
    read_only: bool = False,
) -> str:
    if source.person_name is not None:
        if not source.show_faces:
            return item_media_html(target, item, read_only=read_only)
        from .server_faces import person_faces_for_item, person_item_media_html

        faces = person_faces_for_item(
            target,
            source.person_name,
            item,
            include_suggestions=source.include_suggestions,
            face_config=face_config,
            read_only=read_only,
        )
        return person_item_media_html(target, item, faces)
    return item_media_html(target, item, read_only=read_only)


def item_media_html(target: Path, item: Any, *, read_only: bool = False) -> str:
    file_id = int(item["id"])
    target_path = Path(str(item["target_path"]))
    url = f"/file/{file_id}"
    preview_url = f"/preview/{file_id}"
    display_url = f"/display/{file_id}"
    name = html.escape(str(item["stored_filename"]))
    kind = media_kind(target_path)
    if kind == "video":
        return video_item_html(target, item)
    if kind != "image":
        return f'<a class="file-card" href="{url}" target="_blank">Fil<br>{name}</a>'
    return f'<a href="{display_url}" target="_blank"{media_link_class_attr(item)}><img src="{preview_url}" alt="{name}"{rotation_style_attr(item, target, write_metadata_cache=not read_only)}></a>'


def video_item_html(target: Path, item: Any) -> str:
    file_id = int(item["id"])
    target_path = Path(str(item["target_path"]))
    original_url = f"/file/{file_id}"
    name = html.escape(str(item["stored_filename"]))
    suffix = target_path.suffix.casefold()
    if suffix not in VIDEO_PREVIEW_SOURCE_EXTENSIONS:
        return f'<video src="{original_url}" controls></video>'
    format_name = suffix.removeprefix(".").upper()
    if existing_video_preview_path(target, item) is None:
        return (
            f'<a class="file-card" href="{original_url}" target="_blank" download>'
            f'{format_name}-videoen mangler MP4-avspillingskopi<br>{name}<br>'
            '<span>Kjør «Lag videoavspillingskopier» i Bildebank.</span></a>'
        )
    return (
        '<div class="video-with-original">'
        f'<video src="/video-preview/{file_id}" controls></video>'
        f'<a class="video-original-link" href="{original_url}" download>Åpne original {format_name}</a>'
        '</div>'
    )


def comment_overlay_html(item: Any) -> str:
    comment = item_string_value(item, "comment")
    hidden = "" if comment else " hidden"
    return (
        f'<div class="item-comment-overlay" data-item-comment-overlay{hidden}>'
        f'{html.escape(comment)}</div>'
    )


def person_item_page_html(
    target: Path,
    person_name: str,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
    *,
    page_html: PageRenderer,
) -> str:
    return source_item_page_html(
        target,
        person_browser_source(person_name, include_suggestions=True),
        item,
        previous_item,
        next_item,
        month_nav,
        page_html=page_html,
    )


def rotation_buttons_html(source: BrowserSource, item: Any) -> str:
    if not is_image_item(item):
        return ""
    file_id = int(item["id"])
    return f"""
      <button class="nav-button" type="button" title="Roter bildet til venstre" data-rotate-item="{file_id}" data-rotate-direction="left">↺</button>
      <button class="nav-button" type="button" title="Roter bildet til høyre" data-rotate-item="{file_id}" data-rotate-direction="right">↻</button>
    """


def manual_date_button_html(item: Any) -> str:
    file_id = int(item["id"])
    manual_from = item_string_value(item, "manual_date_from")
    manual_to = item_string_value(item, "manual_date_to")
    manual_note = item_string_value(item, "manual_date_note")
    title = "Endre manuell dato" if manual_date_text(item) else "Sett manuell dato"
    return (
        f'<button class="nav-button" type="button" '
        f'title="{title}" '
        f'data-open-manual-date '
        f'data-manual-date-item="{file_id}" '
        f'data-manual-date-from="{html.escape(manual_from)}" '
        f'data-manual-date-to="{html.escape(manual_to)}" '
        f'data-manual-date-note="{html.escape(manual_note)}">'
        f'📅</button>'
    )


def named_manual_h3_cells(target: Path, *, conn: sqlite3.Connection | None = None) -> list[Any]:
    owned_conn = conn is None
    if owned_conn and not (target / db.DB_FILENAME).exists():
        return []
    try:
        conn = conn or db.connect(target)
    except SchemaMigrationRequired:
        return []
    try:
        return db.geo_place_names(conn)
    finally:
        if owned_conn:
            conn.close()


def item_has_real_gps(item: Any) -> bool:
    return item_string_value(item, "gps_lat") != "" or item_string_value(item, "gps_lon") != ""


def manual_h3_button_html(item: Any, named_h3_cells: Sequence[Any]) -> str:
    if not named_h3_cells or item_has_real_gps(item):
        return ""
    file_id = int(item["id"])
    title = "Endre manuelt sted" if gps_source_is_manual_h3(item) else "Sett manuelt sted"
    return (
        f'<button class="nav-button" type="button" '
        f'title="{title}" '
        f'data-open-manual-h3 '
        f'data-manual-h3-item="{file_id}">🌐</button>'
    )


def item_string_value(item: Any, key: str) -> str:
    try:
        return str(item[key] or "")
    except (KeyError, IndexError):
        return ""


def remove_manual_location_button_html(item: Any) -> str:
    if not gps_source_is_manual_h3(item):
        return ""
    file_id = int(item["id"])
    return (
        f'<span class="manual-location-remove">(<button class="inline-link danger-inline-link" type="button" '
        'title="Fjern manuell angitt sted bildet er tatt" '
        f'data-remove-manual-location-item="{file_id}">'
        f'fjern</button>)</span>'
    )




def delete_button_html(source: BrowserSource, item: Any, previous_item: Any | None, next_item: Any | None) -> str:
    redirect_url = source_item_url(source, int(next_item["id"])) if next_item is not None else ""
    if not redirect_url and previous_item is not None:
        redirect_url = source_item_url(source, int(previous_item["id"]))
    if not redirect_url:
        redirect_url = source.root_url
    relative = display_relative_path(Path("."), Path(str(item["target_path"])))
    return (
        f'<button class="nav-button danger-button delete-button" type="button" '
        f'title="Flytt bildet til papirkurven" '
        f'data-delete-item="{int(item["id"])}" '
        f'data-delete-path="{html.escape(relative)}" '
        f'data-delete-redirect="{html.escape(redirect_url)}">Slett</button>'
    )


def image_info_overlay_html() -> str:
    return """
    <div id="infoOverlay" class="info-overlay" hidden>
      <div class="lightbox-bar">
        <div class="lightbox-title">Bildeinfo</div>
        <button class="lightbox-close" type="button" data-close-info>Lukk</button>
      </div>
      <div class="info-panel">
        <h2>Bildeinfo</h2>
        <dl class="info-list" data-info-list></dl>
      </div>
    </div>
    """


def manual_date_overlay_html() -> str:
    return """
    <div id="manualDateOverlay" class="info-overlay" hidden>
      <div class="lightbox-bar">
        <div class="lightbox-title">Manuell dato</div>
        <button class="lightbox-close" type="button" data-close-manual-date>Lukk</button>
      </div>
      <form class="modal-panel manual-date-panel" data-manual-date-form>
        <h2>Manuell dato</h2>
        <fieldset class="manual-date-modes">
          <label><input type="radio" name="mode" value="exact" checked> Eksakt dato</label>
          <label><input type="radio" name="mode" value="uncertain"> Usikker dato</label>
          <label><input type="radio" name="mode" value="between"> Intervall</label>
        </fieldset>
        <label data-manual-date-field="date">Dato
          <input type="date" name="date">
        </label>
        <label data-manual-date-field="uncertainty">Usikkerhet (d=dag, w=uke, m=måned, y=år)
          <input type="text" name="uncertainty" placeholder="1m">
        </label>
        <label data-manual-date-field="date_from">Fra-dato
          <input type="date" name="date_from">
        </label>
        <label data-manual-date-field="date_to">Til-dato
          <input type="date" name="date_to">
        </label>
        <label>Notat
          <input type="text" name="note">
        </label>
        <p class="assign-status" data-manual-date-status></p>
        <div class="modal-actions">
          <button class="danger-button" type="button" data-clear-manual-date hidden>Fjern manuell dato</button>
          <button type="button" data-close-manual-date>Avbryt</button>
          <button type="submit">Lagre</button>
        </div>
      </form>
    </div>
    """


def comment_dialog_html(item: Any) -> str:
    file_id = int(item["id"])
    comment = item_string_value(item, "comment")
    remove_hidden = "" if comment else " hidden"
    return f"""
    <div id="itemCommentDialog" class="modal-overlay" hidden>
      <form class="modal-panel item-comment-panel" data-item-comment-form data-file-id="{file_id}">
        <h2>Kommentar</h2>
        <label>Kommentar til bildet eller filen
          <textarea name="comment" maxlength="{db.MAX_FILE_COMMENT_LENGTH}" rows="8">{html.escape(comment)}</textarea>
        </label>
        <p class="assign-status" data-item-comment-status></p>
        <div class="modal-actions">
          <button class="danger-button" type="button" data-remove-item-comment{remove_hidden}>Fjern kommentar</button>
          <button type="button" data-close-item-comment>Avbryt</button>
          <button type="submit">Lagre</button>
        </div>
      </form>
    </div>
    """


def manual_h3_overlay_html(item: Any, named_h3_cells: Sequence[Any]) -> str:
    if not named_h3_cells or item_has_real_gps(item):
        return ""
    file_id = int(item["id"])
    current_cell = manual_h3_cell(item) if gps_source_is_manual_h3(item) else ""
    rows = []
    for row in named_h3_cells:
        h3_cell = str(row["h3_cell"])
        name = str(row["name"])
        resolution = str(h3_resolution(h3_cell))
        active = h3_cell == current_cell
        active_class = " active" if active else ""
        current_text = '<span class="manual-h3-current">valgt</span>' if active else ""
        rows.append(
            f'<button class="manual-h3-option{active_class}" type="button" '
            f'data-manual-h3-cell="{html.escape(h3_cell)}">'
            f'<span class="manual-h3-name">{html.escape(name)}</span>'
            f'<span class="manual-h3-meta">res {html.escape(resolution)} · {html.escape(h3_cell)}</span>'
            f'{current_text}</button>'
        )
    return f"""
    <div id="manualH3Overlay" class="info-overlay" hidden>
      <div class="lightbox-bar">
        <div class="lightbox-title">Manuelt sted</div>
        <button class="lightbox-close" type="button" data-close-manual-h3>Lukk</button>
      </div>
      <section class="modal-panel manual-h3-panel" data-manual-h3-panel data-file-id="{file_id}">
        <h2>Manuelt sted</h2>
        <div class="manual-h3-list">
          {"".join(rows)}
        </div>
        <p class="assign-status" data-manual-h3-status></p>
        <div class="modal-actions">
          <button type="button" data-close-manual-h3>Avbryt</button>
        </div>
      </section>
    </div>
    """
