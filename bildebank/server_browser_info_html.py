from __future__ import annotations

import html
import sqlite3
import urllib.parse
import datetime as dt
from pathlib import Path
from typing import Any

from . import db
from .formatting import format_bytes
from .geo import H3_COLUMNS, h3_area_label
from .html_paths import display_relative_path
from .media import ImageDimensions
from .media_cache import cached_image_dimensions
from .server_browser_queries import parse_iso_date


def display_short_date(value: str) -> str:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%d.%m.%y")
def manual_h3_cell_name(target: Path, h3_cell: str, *, conn: sqlite3.Connection | None = None) -> str | None:
    owned_conn = conn is None
    conn = conn or db.connect(target)
    try:
        return db.geo_place_name(conn, h3_cell)
    finally:
        if owned_conn:
            conn.close()
def image_info_content_html(target: Path, item: Any, *, read_only: bool = False) -> str:
    return "\n".join(image_info_rows(target, item, read_only=read_only))


def image_info_rows(target: Path, item: Any, *, read_only: bool = False) -> list[str]:
    target_path = Path(str(item["target_path"]))
    absolute_path = db.absolute_target_path(target, target_path)
    dimensions = cached_image_dimensions(target, absolute_path) if not read_only else cached_item_dimensions(item)
    rows = [
        info_row_html("Filnavn", display_relative_path(target, target_path)),
        info_row_html("Dato", image_date_text(item)),
        info_row_html("Filstørrelse", f"{format_bytes(int(item['size_bytes']))} ({int(item['size_bytes'])} bytes)"),
        info_row_html("Oppløsning", f"{dimensions.width} x {dimensions.height}" if dimensions else "-"),
        info_row_html("Kamera", camera_text_from_item(item)),
    ]
    if manual_date_text(item):
        rows.append(info_row_html("Opprinnelig dato", f"{item['taken_date'] or '-'} ({date_source_text(str(item['date_source'] or ''))})"))
        if item["manual_date_note"]:
            rows.append(info_row_html("Datonotat", str(item["manual_date_note"])))
    sources = image_source_rows(target, target_path)
    if sources:
        rows.append(info_row_html("Kilder", "\n\n".join(sources), multiline=True))
    else:
        rows.append(info_row_html("Kilder", "-"))
    tags = image_tag_links_html(target, int(item["id"]))
    if tags:
        rows.append(info_row_html("Tagger", tags, raw_html=True))
    manual_h3_label = manual_h3_label_html(target, item)
    if manual_h3_label:
        rows.append(info_row_html("", manual_h3_label, raw_html=True))
    maps_link = google_maps_link_html(item)
    if maps_link:
        rows.append(info_row_html("Kart", maps_link, raw_html=True))
    if gps_source_text(item) != "ukjent":
        rows.append(info_row_html("GPS-kilde", gps_source_text(item)))
    geo_links = image_geo_area_links_html(target, item)
    if geo_links:
        rows.append(info_row_html("Steder", geo_links, raw_html=True))
    return rows


def cached_item_dimensions(item: Any) -> ImageDimensions | None:
    try:
        width = int(item["media_width"])
        height = int(item["media_height"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return ImageDimensions(width, height)


def image_tag_links_html(target: Path, file_id: int) -> str:
    conn = db.connect(target)
    try:
        rows = db.tags_for_file(conn, file_id)
    finally:
        conn.close()
    links = []
    for row in rows:
        name = str(row["name"])
        suffix = " (system)" if row["kind"] == db.TAG_KIND_SYSTEM else ""
        url = "/tag/" + urllib.parse.quote(name, safe="")
        links.append(f'<a href="{html.escape(url)}">{html.escape(name)}</a>{html.escape(suffix)}')
    return ", ".join(links)


def manual_h3_label_html(target: Path, item: Any) -> str:
    if not gps_source_is_manual_h3(item):
        return ""
    conn = db.connect(target)
    try:
        place_name = manual_h3_place_name(conn, item)
        h3_cell = manual_h3_cell(item)
    finally:
        conn.close()
    return manual_h3_status_html(place_name, h3_cell)


def gps_source_is_manual_h3(item: Any) -> bool:
    try:
        return str(item["gps_source"] or "") == "manual-h3"
    except (KeyError, IndexError):
        return False


def manual_h3_place_name(conn: sqlite3.Connection, item: Any) -> str | None:
    if not gps_source_is_manual_h3(item):
        return None
    for resolution in sorted(H3_COLUMNS, reverse=True):
        column = H3_COLUMNS[resolution]
        try:
            h3_cell = item[column]
        except (KeyError, IndexError):
            continue
        if not h3_cell:
            continue
        place_name = db.geo_place_name(conn, str(h3_cell))
        if place_name:
            return place_name
    return None


def manual_h3_cell(item: Any) -> str | None:
    if not gps_source_is_manual_h3(item):
        return None
    for resolution in sorted(H3_COLUMNS, reverse=True):
        column = H3_COLUMNS[resolution]
        try:
            h3_cell = item[column]
        except (KeyError, IndexError):
            continue
        if h3_cell:
            return str(h3_cell)
    return None


def manual_h3_link_html(label: str, h3_cell: str | None) -> str:
    if not h3_cell:
        return html.escape(label)
    url = "https://h3geo.org/#hex=" + urllib.parse.quote_plus(h3_cell)
    return f'<a href="{html.escape(url)}" target="_blank" ' \
           'title="Vis plasseringen på kartet på https://h3geo.org/" ' \
           f'rel="noopener">{html.escape(label)}</a>'


def manual_h3_status_html(place_name: str | None, h3_cell: str | None = None) -> str:
    label = f"Manuell H3: {place_name}" if place_name else "Manuell H3"
    return f'<span class="status">{manual_h3_link_html(label, h3_cell)}</span>'


def manual_h3_badge_html(place_name: str | None, h3_cell: str | None = None, *, extra_html: str = "") -> str:
    label = f"Manuell H3: {place_name}" if place_name else "Manuell H3"
    return f'<div class="location-status-badge">{manual_h3_link_html(label, h3_cell)}{extra_html}</div>'


def gps_location_badge_html(item: Any, *, extra_html: str = "") -> str:
    coordinates = gps_coordinate_pair(item)
    if coordinates is None:
        return f'<div class="location-status-badge">GPS mangler{extra_html}</div>'
    latitude, longitude = coordinates
    url = google_maps_url(latitude, longitude)
    return (
        '<div class="location-status-badge">'
        f'<a href="{html.escape(url)}" target="_blank" rel="noopener">GPS-lokalisert</a>'
        f'{extra_html}'
        '</div>'
    )


def image_date_text(item: Any) -> str:
    manual_text = manual_date_text(item)
    if manual_text:
        return manual_text
    taken_date = str(item["taken_date"] or "-")
    source = str(item["date_source"] or "")
    return f"{taken_date} ({date_source_text(source)})"


def manual_date_text(item: Any) -> str:
    try:
        date_from = parse_iso_date(str(item["manual_date_from"] or ""))
        date_to = parse_iso_date(str(item["manual_date_to"] or ""))
    except (KeyError, IndexError):
        return ""
    if date_from is None or date_to is None:
        return ""
    if date_from == date_to:
        return f"{date_from.isoformat()} (manuell dato)"
    midpoint = date_from + (date_to - date_from) // 2
    uncertainty_days = max((date_to - date_from).days // 2, 1)
    return f"ca. {midpoint.isoformat()} ± {format_uncertainty_days(uncertainty_days)} (manuell dato)"


def format_uncertainty_days(days: int) -> str:
    if days % 365 == 0:
        years = days // 365
        return f"{years} år" if years != 1 else "1 år"
    if days % 30 == 0:
        months = days // 30
        return f"{months} måneder" if months != 1 else "1 måned"
    if days % 7 == 0:
        weeks = days // 7
        return f"{weeks} uker" if weeks != 1 else "1 uke"
    return f"{days} dager" if days != 1 else "1 dag"


def date_source_text(source: str) -> str:
    labels = {
        "metadata": "fra metadata",
        "filename": "fra filnavn",
        "mtime": "fra mtime",
        "unknown": "ukjent datokilde",
    }
    return labels.get(source, source or "ukjent datokilde")


def google_maps_link_html(item: Any) -> str:
    coordinates = gps_coordinate_pair(item)
    if coordinates is None:
        return ""
    latitude, longitude = coordinates
    label = f"Åpne i Google Maps ({latitude:.7f}, {longitude:.7f})"
    url = google_maps_url(latitude, longitude)
    return f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(label)}</a>'


def gps_coordinate_pair(item: Any) -> tuple[float, float] | None:
    try:
        lat = item["gps_lat"]
        lon = item["gps_lon"]
    except (KeyError, IndexError):
        return None
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def google_maps_url(latitude: float, longitude: float) -> str:
    query = urllib.parse.quote(f"{latitude:.7f},{longitude:.7f}", safe=",")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


def gps_source_text(item: Any) -> str:
    try:
        source = str(item["gps_source"] or "")
    except (KeyError, IndexError):
        source = ""
    labels = {
        "exiftool": "fra metadata",
        "manual-h3": "satt manuelt",
    }
    return labels.get(source, source or "ukjent")


def camera_text_from_item(item: Any) -> str:
    parts = [
        str(part)
        for part in (optional_item_value(item, "camera_make"), optional_item_value(item, "camera_model"))
        if part
    ]
    return " ".join(parts) if parts else "-"


def optional_item_value(item: Any, key: str) -> Any | None:
    try:
        return item[key]
    except (KeyError, IndexError):
        return None


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
