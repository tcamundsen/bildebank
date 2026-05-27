from __future__ import annotations

import html
import re
import sqlite3
import urllib.parse
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import FaceRecognitionConfig
from .geo import H3_COLUMNS, h3_area_label
from .html_export import display_relative_path, format_bytes, month_key_from_path
from .media import camera_info
from .media_cache import cached_image_dimensions
from .openclip import relative_to_target
from .server_browser_sources import (
    BrowserSource,
    all_browser_source,
    imported_source_browser_source,
    is_filtered_source,
    person_browser_source,
    source_has_sql_filter,
    source_item_url,
    source_sql_filter,
)
from .thumbnails import existing_thumbnail_url


ShellPageRenderer = Callable[..., str]
PageRenderer = Callable[[str, str], str]
MONTH_PATH_RE = re.compile(r"(?:^|[\\/])(?P<year>\d{4})[\\/](?P<month>\d{2})(?:[\\/]|$)")
FILE_COLUMNS = (
    "id, target_path, target_path_key, stored_filename, taken_date, date_source, "
    "size_bytes, view_rotation_degrees, gps_lat, gps_lon, "
    "media_width, media_height, media_orientation, media_metadata_mtime_ns, "
    f"{db.H3_FILE_COLUMNS_SQL}"
)
ITEM_DATE_ORDER_SQL = db.BROWSER_DATE_ORDER_SQL
ITEM_ORDER_SQL = f"{ITEM_DATE_ORDER_SQL}, target_path_key"


def is_image_item(item: Any) -> bool:
    target_path = Path(str(item["target_path"]))
    return target_path.suffix.lower().lstrip(".") not in {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}


def item_view_rotation(item: Any) -> int:
    try:
        return db.normalize_view_rotation(item["view_rotation_degrees"])
    except (KeyError, IndexError):
        return 0


def rotation_style_attr(item: Any) -> str:
    rotation = item_view_rotation(item)
    if rotation == 0:
        return ""
    return f' style="transform: rotate({rotation}deg);" data-view-rotation="{rotation}"'


def first_browser_item(target: Path) -> Any | None:
    return first_source_item(target, all_browser_source())


def first_source_item(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
) -> Any | None:
    if source_has_sql_filter(source):
        return first_sql_filtered_source_item(target, source)
    if source.person_name is not None or source.source_id is not None:
        items = source_items(target, source, face_config)
        return items[0] if items else None
    if not is_filtered_source(source):
        return first_unfiltered_source_item(target)
    items = source_items(target, source, face_config)
    return items[0] if items else None


def first_sql_filtered_source_item(target: Path, source: BrowserSource) -> Any | None:
    where_sql, params = source_sql_filter(source)
    conn = db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({where_sql})
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            params,
        ).fetchone()
    finally:
        conn.close()


def first_unfiltered_source_item(target: Path) -> Any | None:
    conn = db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()


def sql_filtered_source_item_by_id(target: Path, source: BrowserSource, file_id: int) -> Any | None:
    where_sql, params = source_sql_filter(source)
    conn = db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND id = ?
              AND ({where_sql})
            """,
            (file_id, *params),
        ).fetchone()
    finally:
        conn.close()


def unfiltered_source_item_by_id(target: Path, file_id: int) -> Any | None:
    conn = db.connect(target)
    try:
        return conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL AND id = ?
            """,
            (file_id,),
        ).fetchone()
    finally:
        conn.close()


def browser_item_by_id(target: Path, file_id: int) -> Any | None:
    return source_item_by_id(target, all_browser_source(), file_id)


def source_item_by_id(
    target: Path,
    source: BrowserSource,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> Any | None:
    if source_has_sql_filter(source):
        return sql_filtered_source_item_by_id(target, source, file_id)
    if source.person_name is not None or source.source_id is not None:
        return next((item for item in source_items(target, source, face_config) if int(item["id"]) == file_id), None)
    return unfiltered_source_item_by_id(target, file_id)


def item_order_key(item: Any) -> tuple[str, str]:
    taken_date = str(item["taken_date"] or "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}", taken_date):
        taken_date = "9999-99-99"
    return taken_date, str(item["target_path_key"])


def adjacent_items_from_list(items: list[Any], item: Any) -> tuple[Any | None, Any | None]:
    index = next((idx for idx, candidate in enumerate(items) if int(candidate["id"]) == int(item["id"])), -1)
    if index < 0:
        return None, None
    previous_item = items[index - 1] if index > 0 else None
    next_item = items[index + 1] if index < len(items) - 1 else None
    return previous_item, next_item


def adjacent_unfiltered_source_items(target: Path, item: Any) -> tuple[Any | None, Any | None]:
    order_key = item_order_key(item)
    conn = db.connect(target)
    try:
        previous_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({ITEM_DATE_ORDER_SQL}, target_path_key) < (?, ?)
            ORDER BY {ITEM_DATE_ORDER_SQL} DESC, target_path_key DESC
            LIMIT 1
            """,
            order_key,
        ).fetchone()
        next_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({ITEM_DATE_ORDER_SQL}, target_path_key) > (?, ?)
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            order_key,
        ).fetchone()
        return previous_item, next_item
    finally:
        conn.close()


def adjacent_sql_filtered_source_items(target: Path, source: BrowserSource, item: Any) -> tuple[Any | None, Any | None]:
    where_sql, params = source_sql_filter(source)
    order_key = item_order_key(item)
    conn = db.connect(target)
    try:
        previous_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({where_sql})
              AND ({ITEM_DATE_ORDER_SQL}, target_path_key) < (?, ?)
            ORDER BY {ITEM_DATE_ORDER_SQL} DESC, target_path_key DESC
            LIMIT 1
            """,
            (*params, *order_key),
        ).fetchone()
        next_item = conn.execute(
            f"""
            SELECT {FILE_COLUMNS}
            FROM files
            WHERE deleted_at IS NULL
              AND ({where_sql})
              AND ({ITEM_DATE_ORDER_SQL}, target_path_key) > (?, ?)
            ORDER BY {ITEM_ORDER_SQL}
            LIMIT 1
            """,
            (*params, *order_key),
        ).fetchone()
        return previous_item, next_item
    finally:
        conn.close()


def adjacent_browser_items(target: Path, item: Any) -> tuple[Any | None, Any | None]:
    return adjacent_source_items(target, all_browser_source(), item)


def adjacent_source_items(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> tuple[Any | None, Any | None]:
    if source_has_sql_filter(source):
        return adjacent_sql_filtered_source_items(target, source, item)
    if source.person_name is not None or source.source_id is not None:
        return adjacent_items_from_list(source_items(target, source, face_config), item)
    return adjacent_unfiltered_source_items(target, item)


def valid_month_key(value: str) -> bool:
    if len(value) != 7 or value[4] != "-":
        return False
    year, month = value.split("-", 1)
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= 12


@lru_cache(maxsize=8)
def cached_browser_month_keys(target_path: str, db_mtime_ns: int) -> tuple[str, ...]:
    target = Path(target_path)
    conn = db.connect(target)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT substr(target_path, 1, 4) || '-' || substr(target_path, 6, 2) AS month_key
            FROM files
            WHERE deleted_at IS NULL
              AND target_path GLOB '[0-9][0-9][0-9][0-9]/[0-9][0-9]/*'
            ORDER BY month_key
            """
        )
        return tuple(str(row["month_key"]) for row in rows if valid_month_key(str(row["month_key"])))
    finally:
        conn.close()


def sql_filtered_source_month_keys(target: Path, source: BrowserSource) -> list[str]:
    where_sql, params = source_sql_filter(source)
    conn = db.connect(target)
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT substr(target_path, 1, 4) || '-' || substr(target_path, 6, 2) AS month_key
            FROM files
            WHERE deleted_at IS NULL
              AND ({where_sql})
              AND target_path GLOB '[0-9][0-9][0-9][0-9]/[0-9][0-9]/*'
            ORDER BY month_key
            """,
            params,
        )
        return [str(row["month_key"]) for row in rows if valid_month_key(str(row["month_key"]))]
    finally:
        conn.close()


def browser_month_keys(target: Path) -> list[str]:
    return source_month_keys(target, all_browser_source())


def source_month_keys(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
) -> list[str]:
    if source_has_sql_filter(source):
        return sql_filtered_source_month_keys(target, source)
    if source.person_name is not None or source.source_id is not None:
        keys = {month_key_for_item(target, item) for item in source_items(target, source, face_config)}
        return sorted(key for key in keys if valid_month_key(key))
    db_path = db.db_path_for_target(target)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return list(cached_browser_month_keys(str(target.resolve()), mtime_ns))


def date_source_items(target: Path, date_source: str) -> list[Any]:
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                  AND date_source = ?
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (date_source,),
            )
        )
    finally:
        conn.close()


def imported_source_items(target: Path, source_id: int) -> list[Any]:
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT
                    files.id,
                    files.target_path,
                    files.target_path_key,
                    files.stored_filename,
                    files.taken_date,
                    files.date_source,
                    files.size_bytes,
                    files.view_rotation_degrees,
                    files.gps_lat,
                    files.gps_lon,
                    files.media_width,
                    files.media_height,
                    files.media_orientation,
                    files.media_metadata_mtime_ns,
                    {db.H3_FILE_COLUMNS_SQL}
                FROM files
                JOIN file_sources ON file_sources.file_id = files.id
                WHERE files.deleted_at IS NULL
                  AND file_sources.source_id = ?
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (source_id,),
            )
        )
    finally:
        conn.close()


def imported_source_by_id(target: Path, source_id: int) -> db.Source | None:
    conn = db.connect(target)
    try:
        try:
            return db.get_source(conn, source_id)
        except ValueError:
            return None
    finally:
        conn.close()


def source_summary_rows(target: Path) -> list[sqlite3.Row]:
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                """
                SELECT
                    sources.id,
                    sources.name,
                    sources.path,
                    sources.imported_at,
                    sources.status,
                    sources.superseded_by_source_id,
                    COUNT(file_sources.id) AS source_file_count,
                    COUNT(CASE WHEN files.deleted_at IS NULL THEN 1 END) AS active_file_count
                FROM sources
                LEFT JOIN file_sources ON file_sources.source_id = sources.id
                LEFT JOIN files ON files.id = file_sources.file_id
                GROUP BY sources.id
                ORDER BY sources.imported_at IS NULL, sources.imported_at, sources.id
                """
            )
        )
    finally:
        conn.close()


def sources_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    sources = source_summary_rows(target)
    rows = "\n".join(source_row_html(source) for source in sources)
    content = (
        f'<div class="people-table">{rows}</div>'
        if rows
        else '<p class="meta">Ingen importerte kilder registrert.</p>'
    )
    return shell_page_html(
        "Kilder",
        f"""
        <h1>Kilder</h1>
        {content}
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def source_row_html(source: sqlite3.Row) -> str:
    name = str(source["name"])
    status = str(source["status"])
    active_file_count = int(source["active_file_count"])
    source_file_count = int(source["source_file_count"])
    imported_at = str(source["imported_at"] or "-")
    superseded_by = source["superseded_by_source_id"]
    superseded = f", erstattet av #{int(superseded_by)}" if superseded_by is not None else ""
    source_browser = imported_source_browser_source(source)
    return f"""
    <div class="people-row">
      <div class="people-name">{html.escape(name)}</div>
      <a class="person-link" href="{html.escape(source_browser.root_url)}">Vis bilder ({active_file_count})</a>
      <span class="status">filer fra kilde: {source_file_count}</span>
      <span class="status">status: {html.escape(status)}{html.escape(superseded)}</span>
      <span class="status">importert: {html.escape(imported_at)}</span>
      <div class="detail">{html.escape(str(source["path"]))}</div>
    </div>
    """


def empty_browser_html(
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    search_link = '<p><a href="/search">Bildesøk</a></p>' if openclip_enabled else ""
    return shell_page_html(
        "Bildebrowser",
        f"""
        <h1>Bildebrowser</h1>
        <p class="meta">Ingen filer i bildesamlingen.</p>
        {search_link}
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


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
    )


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
) -> str:
    from .server_faces import (
        confirmed_people_for_file,
        faces_button_html,
        faces_overlay_html,
        people_links_html,
        source_duplicate_confirmed_faces_warning_html,
        unconfirm_face_buttons_html,
        unconfirmed_face_count_for_item,
    )
    from .server_shell import app_header_html, source_controls_html

    target_path = Path(str(item["target_path"]))
    relative = display_relative_path(target, target_path)
    media = source_item_media_html(target, source, item, face_config)
    controls = source_controls_html(
        source,
        month_nav,
        previous_item,
        next_item,
        include_info_button=True,
        info_button=image_info_button_html(int(item["id"])),
        rotation_buttons=rotation_buttons_html(source, item),
        unconfirm_buttons=unconfirm_face_buttons_html(target, source, item, face_config) if face_enabled else "",
        delete_button=delete_button_html(source, item, previous_item, next_item),
    )
    people = people_links_html(confirmed_people_for_file(target, int(item["id"]), face_config)) if face_enabled else ""
    show_unconfirmed_faces = face_enabled and source.person_name is None
    unconfirmed_face_count = unconfirmed_face_count_for_item(target, int(item["id"]), face_config) if show_unconfirmed_faces else 0
    faces_button = faces_button_html(unconfirmed_face_count, int(item["id"])) if show_unconfirmed_faces else ""
    faces_overlay = faces_overlay_html(item) if unconfirmed_face_count > 0 else ""
    info_overlay = image_info_overlay_html()
    duplicate_warning = source_duplicate_confirmed_faces_warning_html(target, source, item, face_config) if face_enabled else ""
    return page_html(
        f"{source.title}: {target_path.name}",
        f"""
        <main class="server-browser">
          {app_header_html(
              source.title,
              source=source,
              item=item,
              extra_html=people + faces_button,
              controls=controls,
              message_html=duplicate_warning,
              face_enabled=face_enabled,
              openclip_enabled=openclip_enabled,
          )}
          <section class="stage">{media}</section>
          <footer class="browser-footer">
            <a class="filename" href="/file/{int(item["id"])}" target="_blank">{html.escape(relative)}</a>
          </footer>
        </main>
        {faces_overlay}
        {info_overlay}
        """,
    )


def source_item_media_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    if source.person_name is not None:
        if not source.show_faces:
            return item_media_html(item)
        from .server_faces import person_faces_for_item, person_item_media_html

        faces = person_faces_for_item(
            target,
            source.person_name,
            item,
            include_suggestions=source.include_suggestions,
            face_config=face_config,
        )
        return person_item_media_html(item, faces)
    return item_media_html(item)


def item_media_html(item: Any) -> str:
    file_id = int(item["id"])
    target_path = Path(str(item["target_path"]))
    url = f"/file/{file_id}"
    name = html.escape(str(item["stored_filename"]))
    if target_path.suffix.lower().lstrip(".") in {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}:
        return f'<video src="{url}" controls></video>'
    return f'<a href="{url}" target="_blank"><img src="{url}" alt="{name}"{rotation_style_attr(item)}></a>'


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
      <button class="nav-button" type="button" data-rotate-item="{file_id}" data-rotate-direction="left">Roter venstre</button>
      <button class="nav-button" type="button" data-rotate-item="{file_id}" data-rotate-direction="right">Roter høyre</button>
    """


def delete_button_html(source: BrowserSource, item: Any, previous_item: Any | None, next_item: Any | None) -> str:
    redirect_url = source_item_url(source, int(next_item["id"])) if next_item is not None else ""
    if not redirect_url and previous_item is not None:
        redirect_url = source_item_url(source, int(previous_item["id"]))
    if not redirect_url:
        redirect_url = source.root_url
    relative = display_relative_path(Path("."), Path(str(item["target_path"])))
    return (
        f'<button class="nav-button danger-button" type="button" '
        f'data-delete-item="{int(item["id"])}" '
        f'data-delete-path="{html.escape(relative)}" '
        f'data-delete-redirect="{html.escape(redirect_url)}">Slett</button>'
    )


def image_info_button_html(file_id: int | None) -> str:
    file_attr = f' data-info-item="{file_id}"' if file_id is not None else ""
    return f'<button class="nav-button" type="button" data-open-info{file_attr}>Bildeinfo</button>'


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


def image_info_content_html(target: Path, item: Any) -> str:
    return "\n".join(image_info_rows(target, item))


def image_info_rows(target: Path, item: Any) -> list[str]:
    target_path = Path(str(item["target_path"]))
    absolute_path = db.absolute_target_path(target, target_path)
    dimensions = cached_image_dimensions(target, absolute_path)
    camera = camera_info(absolute_path)
    rows = [
        info_row_html("Filnavn", display_relative_path(target, target_path)),
        info_row_html("Dato", image_date_text(item)),
        info_row_html("Filstørrelse", f"{format_bytes(int(item['size_bytes']))} ({int(item['size_bytes'])} bytes)"),
        info_row_html("Oppløsning", f"{dimensions.width} x {dimensions.height}" if dimensions else "-"),
        info_row_html("Kamera", camera_text(camera)),
    ]
    sources = image_source_rows(target, target_path)
    if sources:
        rows.append(info_row_html("Kilder", "\n\n".join(sources), multiline=True))
    else:
        rows.append(info_row_html("Kilder", "-"))
    maps_link = google_maps_link_html(item)
    if maps_link:
        rows.append(info_row_html("Kart", maps_link, raw_html=True))
    geo_links = image_geo_area_links_html(target, item)
    if geo_links:
        rows.append(info_row_html("Steder", geo_links, raw_html=True))
    return rows


def image_date_text(item: Any) -> str:
    taken_date = str(item["taken_date"] or "-")
    source = str(item["date_source"] or "")
    return f"{taken_date} ({date_source_text(source)})"


def date_source_text(source: str) -> str:
    labels = {
        "metadata": "fra metadata",
        "filename": "fra filnavn",
        "mtime": "fra mtime",
        "unknown": "ukjent datokilde",
    }
    return labels.get(source, source or "ukjent datokilde")


def google_maps_link_html(item: Any) -> str:
    lat = item["gps_lat"]
    lon = item["gps_lon"]
    if lat is None or lon is None:
        return ""
    latitude = float(lat)
    longitude = float(lon)
    query = urllib.parse.quote(f"{latitude:.7f},{longitude:.7f}", safe=",")
    url = f"https://www.google.com/maps/search/?api=1&query={query}"
    label = f"Åpne i Google Maps ({latitude:.7f}, {longitude:.7f})"
    return f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(label)}</a>'


def camera_text(camera: Any | None) -> str:
    if camera is None:
        return "-"
    parts = [part for part in (camera.make, camera.model) if part]
    return " ".join(parts) if parts else "-"


def image_source_rows(target: Path, target_path: Path) -> list[str]:
    conn = db.connect(target)
    try:
        rows = db.file_sources_by_target_path(conn, target, db.absolute_target_path(target, target_path))
    finally:
        conn.close()
    result = []
    for row in rows:
        source_name = str(row["source_name"] or row["source_root"] or f"Kilde #{row['source_id']}")
        result.append(f"{source_name}: {row['source_path']}")
    return result


def image_geo_area_links_html(target: Path, item: Any) -> str:
    conn = db.connect(target)
    try:
        links = []
        for resolution, column in H3_COLUMNS.items():
            h3_cell = item[column]
            if not h3_cell:
                continue
            place_name = db.geo_place_name(conn, str(h3_cell))
            label = f"H3-{resolution}: {h3_cell} ({h3_area_label(resolution)})"
            if place_name:
                label += f" {place_name}"
            url = "/geo/area/" + urllib.parse.quote(str(h3_cell), safe="")
            links.append(f'<a href="{html.escape(url)}">{html.escape(label)}</a>')
        return "<br>".join(links)
    finally:
        conn.close()


def info_row_html(label: str, value: str, *, multiline: bool = False, raw_html: bool = False) -> str:
    escaped_value = value if raw_html else html.escape(value)
    if multiline and not raw_html:
        escaped_value = "<br>".join(escaped_value.splitlines())
    return f"""
    <div class="info-row">
      <dt>{html.escape(label)}</dt>
      <dd>{escaped_value}</dd>
    </div>
    """


def month_page_html(
    target: Path,
    month_key: str,
    items: list[Any],
    *,
    page_html: PageRenderer,
) -> str:
    return source_month_page_html(target, all_browser_source(), month_key, items, page_html=page_html)


def source_month_page_html(
    target: Path,
    source: BrowserSource,
    month_key: str,
    items: list[Any],
    *,
    page_html: PageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    from .server_shell import app_header_html, source_controls_html

    cards = "\n".join(source_month_item_html(target, source, item) for item in items)
    previous_item = items[-1] if items else None
    next_item = items[0] if items else None
    controls = source_controls_html(
        source,
        source_month_navigation_for_key(target, source, month_key, face_config),
        previous_item,
        next_item,
    )
    return page_html(
        f"{source.title}: {month_key}",
        f"""
        <main class="server-browser">
          {app_header_html(
              source.title,
              source=source,
              extra_html=f'<span class="status">Månedsoversikt: {html.escape(month_key)}</span>',
              controls=controls,
              face_enabled=face_enabled,
              openclip_enabled=openclip_enabled,
          )}
          <section class="month-grid-server">{cards}</section>
          <footer class="browser-footer">
            <span class="filename">Månedsoversikt: {html.escape(month_key)}</span>
          </footer>
        </main>
        """,
    )


def empty_person_browser_html(
    person: str | BrowserSource,
    *,
    shell_page_html: ShellPageRenderer,
    openclip_enabled: bool = True,
) -> str:
    source = person if isinstance(person, BrowserSource) else person_browser_source(person, include_suggestions=True)
    return empty_source_html(source, shell_page_html=shell_page_html, openclip_enabled=openclip_enabled)


def empty_source_html(
    source: BrowserSource,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    return shell_page_html(
        source.title,
        f"""
        <h1>{html.escape(source.title)}</h1>
        <p class="meta">{html.escape(empty_source_message(source))}</p>
        """,
        source=source,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def empty_source_message(source: BrowserSource) -> str:
    if source.person_name is None:
        if source.date_source == "filename":
            return "Ingen bilder med dato fra filnavn."
        if source.date_source == "mtime":
            return "Ingen bilder med dato fra mtime."
        if source.source_id is not None:
            return "Ingen aktive bilder for denne kilden."
        if source.geo_place_slug is not None:
            return "Ingen aktive bilder for dette stedet."
        return "Ingen filer i bildesamlingen."
    if source.include_suggestions:
        return "Ingen bekreftede ansikter eller forslag for denne personen ennå."
    return "Ingen bekreftede bilder for denne personen ennå."


def person_not_found_html(
    person_name: str,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    return shell_page_html(
        "Fant ikke person",
        f"""
        <h1>Fant ikke person</h1>
        <p class="error">{html.escape(person_name)}</p>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def person_month_page_html(
    target: Path,
    person_name: str,
    month_key: str,
    items: list[Any],
    *,
    page_html: PageRenderer,
) -> str:
    return source_month_page_html(
        target,
        person_browser_source(person_name, include_suggestions=True),
        month_key,
        items,
        page_html=page_html,
    )


def source_month_item_html(target: Path, source: BrowserSource, item: Any) -> str:
    target_path = Path(str(item["target_path"]))
    label = html.escape(display_relative_path(target, target_path))
    media = thumbnail_media_html(target, item)
    return f"""
    <article class="item">
      <a class="thumb-link" href="{source_item_url(source, int(item["id"]))}">{media}</a>
      <div class="text">
        <div class="path">{label}</div>
        <div class="score">{html.escape(format_bytes(int(item["size_bytes"])))}</div>
      </div>
    </article>
    """


def thumbnail_media_html(target: Path, item: Any) -> str:
    target_path = Path(str(item["target_path"]))
    name = html.escape(str(item["stored_filename"]))
    if target_path.suffix.lower().lstrip(".") in {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}:
        return f'<div class="video-thumb">Video<br>{name}</div>'
    relative_path = db.target_relative_path(target, target_path)
    thumbnail_src = "/file/" + existing_thumbnail_url(target, relative_path)
    return f'<img src="{html.escape(thumbnail_src)}" alt="{name}" loading="lazy"{rotation_style_attr(item)}>'


def all_source_items(target: Path) -> list[Any]:
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                ORDER BY {ITEM_ORDER_SQL}
                """
            )
        )
    finally:
        conn.close()


def items_by_file_ids(target: Path, file_ids: list[int]) -> list[Any]:
    if not file_ids:
        return []
    placeholders = ",".join("?" for _ in file_ids)
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                  AND id IN ({placeholders})
                ORDER BY {ITEM_ORDER_SQL}
                """,
                tuple(file_ids),
            )
        )
    finally:
        conn.close()


def browser_month_navigation(target: Path, item: Any) -> dict[str, str | None]:
    current_key = month_key_for_item(target, item)
    return browser_month_navigation_for_key(target, current_key)


def month_key_for_item(target: Path, item: Any) -> str:
    stored_key = month_key_from_stored_path(str(item["target_path"]))
    if stored_key is not None:
        return stored_key
    return month_key_from_path(relative_to_target(target, Path(str(item["target_path"]))))


def month_key_from_stored_path(path: str) -> str | None:
    match = MONTH_PATH_RE.search(path.replace("\\\\", "\\"))
    if match is None:
        return None
    month_key = f"{match.group('year')}-{match.group('month')}"
    return month_key if valid_month_key(month_key) else None


def source_items(
    target: Path,
    source: BrowserSource,
    face_config: FaceRecognitionConfig | None = None,
) -> list[Any]:
    if source.person_name is not None:
        from .server_faces import person_items

        return person_items(
            target,
            source.person_name,
            include_suggestions=source.include_suggestions,
            face_config=face_config,
        )
    if source.geo_place_slug is not None:
        from .server_geo import geo_place_items

        return geo_place_items(target, source.geo_place_slug)
    if source.date_source is not None:
        return date_source_items(target, source.date_source)
    if source.source_id is not None:
        return imported_source_items(target, source.source_id)
    return all_source_items(target)


def person_item_by_id(target: Path, person_name: str, file_id: int) -> Any | None:
    return source_item_by_id(target, person_browser_source(person_name, include_suggestions=True), file_id)


def adjacent_person_items(target: Path, person_name: str, item: Any) -> tuple[Any | None, Any | None]:
    return adjacent_source_items(target, person_browser_source(person_name, include_suggestions=True), item)


def person_month_navigation(target: Path, person_name: str, item: Any) -> dict[str, str | None]:
    return source_month_navigation(target, person_browser_source(person_name, include_suggestions=True), item)


def source_month_navigation(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> dict[str, str | None]:
    return source_month_navigation_for_key(target, source, month_key_for_item(target, item), face_config)


def source_month_navigation_for_key(
    target: Path,
    source: BrowserSource,
    current_key: str,
    face_config: FaceRecognitionConfig | None = None,
) -> dict[str, str | None]:
    if not valid_month_key(current_key):
        return {"previous_year": None, "next_year": None, "previous_month": None, "next_month": None}
    keys = source_month_keys(target, source, face_config)
    if not keys:
        return {"previous_year": None, "next_year": None, "previous_month": None, "next_month": None}
    years = sorted({key[:4] for key in keys})
    current_year = current_key[:4]
    current_year_index = years.index(current_year) if current_year in years else -1
    previous_year = years[current_year_index - 1] if current_year_index > 0 else None
    next_year = years[current_year_index + 1] if current_year_index < len(years) - 1 else None
    return {
        "previous_year": first_month_in_year(keys, previous_year),
        "next_year": first_month_in_year(keys, next_year),
        "previous_month": next((key for key in reversed(keys) if key < current_key), None),
        "next_month": next((key for key in keys if key > current_key), None),
    }


def person_month_items(target: Path, person_name: str, month_key: str) -> list[Any]:
    return source_month_items(target, person_browser_source(person_name, include_suggestions=True), month_key)


def source_month_items(
    target: Path,
    source: BrowserSource,
    month_key: str,
    face_config: FaceRecognitionConfig | None = None,
) -> list[Any]:
    if source_has_sql_filter(source):
        return sql_filtered_source_month_items(target, source, month_key)
    if source.person_name is not None or source.source_id is not None:
        return [
            item
            for item in source_items(target, source, face_config)
            if month_key_for_item(target, item) == month_key
        ]
    return browser_month_items(target, month_key)


def sql_filtered_source_month_items(target: Path, source: BrowserSource, month_key: str) -> list[Any]:
    if not valid_month_key(month_key):
        return []
    where_sql, params = source_sql_filter(source)
    year, month = month_key.split("-", 1)
    path_glob = f"{year}/{month}/*"
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                  AND ({where_sql})
                  AND target_path GLOB ?
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (*params, path_glob),
            )
        )
    finally:
        conn.close()


def browser_month_navigation_for_key(target: Path, current_key: str) -> dict[str, str | None]:
    if not valid_month_key(current_key):
        return {
            "previous_year": None,
            "next_year": None,
            "previous_month": None,
            "next_month": None,
        }
    keys = browser_month_keys(target)
    if not keys:
        return {
            "previous_year": None,
            "next_year": None,
            "previous_month": None,
            "next_month": None,
        }
    years = sorted({key[:4] for key in keys})
    current_year = current_key[:4]
    current_year_index = years.index(current_year) if current_year in years else -1
    previous_year = years[current_year_index - 1] if current_year_index > 0 else None
    next_year = years[current_year_index + 1] if current_year_index < len(years) - 1 else None
    previous_month = next((key for key in reversed(keys) if key < current_key), None)
    next_month = next((key for key in keys if key > current_key), None)
    return {
        "previous_year": first_month_in_year(keys, previous_year),
        "next_year": first_month_in_year(keys, next_year),
        "previous_month": previous_month,
        "next_month": next_month,
    }


def first_month_in_year(keys: list[str], year: str | None) -> str | None:
    if year is None:
        return None
    return next((key for key in keys if key.startswith(year)), None)


def browser_month_items(target: Path, month_key: str) -> list[Any]:
    year, month = month_key.split("-", 1)
    prefix = db.relative_path_key(Path(year) / month) + "/"
    conn = db.connect(target)
    try:
        rows = list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                  AND target_path_key LIKE ?
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (prefix + "%",),
            )
        )
        if rows:
            return rows
        return [
            row
            for row in conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE deleted_at IS NULL
                ORDER BY {ITEM_ORDER_SQL}
                """
            )
            if month_key_from_stored_path(str(row["target_path"])) == month_key
        ]
    finally:
        conn.close()
