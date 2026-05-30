from __future__ import annotations

import html
import math
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import db
from .geo import (
    H3_COLUMNS,
    PREDEFINED_GEO_PLACES,
    PredefinedGeoPlace,
    h3_area_label,
    h3_column_for_resolution,
    h3_resolution_label,
    predefined_geo_place,
)

DEFAULT_GEO_RESOLUTION = 7
DEFAULT_GEO_MIN_COUNT = 2
DEFAULT_GEO_LIMIT = 100
ShellPageRenderer = Callable[..., str]


@dataclass(frozen=True)
class GeoMapCell:
    h3_cell: str
    count: int
    name: str | None
    x: float
    y: float


def geo_map_layout(rows: list[Any]) -> list[GeoMapCell]:
    import h3

    row_by_cell = {str(row["h3_cell"]): row for row in rows}
    remaining = set(row_by_cell)
    components: list[list[str]] = []
    while remaining:
        start = min(remaining)
        remaining.remove(start)
        component = [start]
        stack = [start]
        while stack:
            cell = stack.pop()
            for neighbor in sorted(set(h3.grid_disk(cell, 1)) & remaining):
                remaining.remove(neighbor)
                stack.append(neighbor)
                component.append(neighbor)
        components.append(sorted(component))

    size = 28.0
    component_gap = 120.0
    max_row_width = 980.0
    placed: list[GeoMapCell] = []
    offset_x = 0.0
    offset_y = 0.0
    row_height = 0.0
    for component in sorted(components, key=lambda cells: (-len(cells), cells[0])):
        coords = geo_component_pixel_coordinates(component, size)
        min_x = min(x for x, _ in coords.values())
        min_y = min(y for _, y in coords.values())
        max_x = max(x for x, _ in coords.values())
        max_y = max(y for _, y in coords.values())
        width = max_x - min_x + size * 3
        height = max_y - min_y + size * 3
        if offset_x > 0 and offset_x + width > max_row_width:
            offset_x = 0.0
            offset_y += row_height + component_gap
            row_height = 0.0
        for cell in component:
            row = row_by_cell[cell]
            pixel_x, pixel_y = coords[cell]
            placed.append(
                GeoMapCell(
                    h3_cell=cell,
                    count=int(row["count"]),
                    name=str(row["name"]) if "name" in row.keys() and row["name"] else None,
                    x=offset_x + (pixel_x - min_x) + size * 1.5,
                    y=offset_y + (pixel_y - min_y) + size * 1.5,
                )
            )
        offset_x += width + component_gap
        row_height = max(row_height, height)
    return placed


def geo_component_pixel_coordinates(cells: list[str], size: float) -> dict[str, tuple[float, float]]:
    coords = geo_component_grid_coordinates(cells)
    pixels = {cell: (x * size * math.sqrt(3), y * size * 1.5) for cell, (x, y) in coords.items()}
    if len(cells) < 2:
        return pixels
    return geo_oriented_component_pixels(cells, pixels)


def geo_component_grid_coordinates(cells: list[str]) -> dict[str, tuple[float, float]]:
    import h3

    origin = cells[0]
    coords: dict[str, tuple[float, float]] = {}
    try:
        for cell in cells:
            ij = h3.cell_to_local_ij(origin, cell)
            i = float(ij[0] if isinstance(ij, tuple) else ij["i"])
            j = float(ij[1] if isinstance(ij, tuple) else ij["j"])
            coords[cell] = ((i + j) * 0.5, -i + j)
    except Exception:  # noqa: BLE001 - H3 can fail for cells without a shared local IJ space
        coords = geo_component_fallback_coordinates(cells)
    return coords


def geo_oriented_component_pixels(
    cells: list[str],
    pixels: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    import h3

    lat_lon = {cell: h3.cell_to_latlng(cell) for cell in cells}
    center_lat = sum(float(lat) for lat, _ in lat_lon.values()) / len(lat_lon)
    lon_scale = max(0.2, math.cos(math.radians(center_lat)))
    geo_points = {
        cell: (float(lon) * lon_scale, -float(lat))
        for cell, (lat, lon) in lat_lon.items()
    }
    candidates = []
    for reflect in (False, True):
        reflected = {
            cell: ((-x if reflect else x), y)
            for cell, (x, y) in pixels.items()
        }
        for step in range(6):
            angle = step * math.pi / 3
            candidates.append(geo_rotate_points(reflected, angle))
    return max(candidates, key=lambda candidate: geo_orientation_score(cells, candidate, geo_points))


def geo_rotate_points(points: dict[str, tuple[float, float]], angle: float) -> dict[str, tuple[float, float]]:
    cos_angle = math.cos(angle)
    sin_angle = math.sin(angle)
    return {
        cell: (x * cos_angle - y * sin_angle, x * sin_angle + y * cos_angle)
        for cell, (x, y) in points.items()
    }


def geo_orientation_score(
    cells: list[str],
    layout_points: dict[str, tuple[float, float]],
    geo_points: dict[str, tuple[float, float]],
) -> float:
    score = 0.0
    for index, first in enumerate(cells):
        for second in cells[index + 1 :]:
            layout_dx = layout_points[second][0] - layout_points[first][0]
            layout_dy = layout_points[second][1] - layout_points[first][1]
            geo_dx = geo_points[second][0] - geo_points[first][0]
            geo_dy = geo_points[second][1] - geo_points[first][1]
            layout_length = math.hypot(layout_dx, layout_dy)
            geo_length = math.hypot(geo_dx, geo_dy)
            if layout_length == 0 or geo_length == 0:
                continue
            score += (layout_dx / layout_length) * (geo_dx / geo_length)
            score += (layout_dy / layout_length) * (geo_dy / geo_length)
    return score


def geo_component_fallback_coordinates(cells: list[str]) -> dict[str, tuple[float, float]]:
    import h3

    directions = [(1.0, 0.0), (0.5, 1.0), (-0.5, 1.0), (-1.0, 0.0), (-0.5, -1.0), (0.5, -1.0)]
    cell_set = set(cells)
    coords = {cells[0]: (0.0, 0.0)}
    queue = [cells[0]]
    while queue:
        cell = queue.pop(0)
        x, y = coords[cell]
        for index, neighbor in enumerate(sorted(set(h3.grid_disk(cell, 1)) & cell_set)):
            if neighbor in coords or neighbor == cell:
                continue
            dx, dy = directions[index % len(directions)]
            coords[neighbor] = (x + dx, y + dy)
            queue.append(neighbor)
    for cell in cells:
        coords.setdefault(cell, (float(len(coords)), 0.0))
    return coords


def geo_map_svg_html(cells: list[GeoMapCell]) -> str:
    size = 28.0
    max_x = max(cell.x for cell in cells) + size * 2
    max_y = max(cell.y for cell in cells) + size * 2
    shapes = "\n".join(geo_map_cell_svg(cell, size=size) for cell in cells)
    return f"""
    <div class="geo-map-wrap">
      <svg class="geo-map" viewBox="0 0 {max_x:.0f} {max_y:.0f}" role="img" aria-label="H3-heksagonkart">
        {shapes}
      </svg>
    </div>
    """


def geo_map_cell_svg(cell: GeoMapCell, *, size: float) -> str:
    points = []
    for index in range(6):
        angle = math.pi / 6 + index * math.pi / 3
        points.append(f"{cell.x + size * math.cos(angle):.1f},{cell.y + size * math.sin(angle):.1f}")
    label = cell.name or cell.h3_cell
    url = "/geo/area/" + urllib.parse.quote(cell.h3_cell, safe="")
    return f"""
    <a class="geo-hex-link" href="{html.escape(url)}">
      <polygon class="geo-hex" points="{' '.join(points)}"></polygon>
      <text class="geo-hex-count" x="{cell.x:.1f}" y="{cell.y + 4:.1f}" text-anchor="middle">{cell.count}</text>
      <title>{html.escape(label)} - {cell.count} bilder</title>
    </a>
    """


def geo_filter_form_html(action: str, *, resolution: int, min_count: int, limit: int) -> str:
    return f"""
          <form action="{html.escape(action)}" method="get" class="geo-filter">
            <label>H3-oppløsning {h3_resolution_select_html(resolution)}</label>
            <label>Minst antall <input name="min_count" value="{min_count}" inputmode="numeric"></label>
            <label>Maks steder <input name="limit" value="{limit}" inputmode="numeric"></label>
            <button type="submit">Vis</button>
          </form>
    """


def h3_resolution_select_html(selected_resolution: int) -> str:
    options = "\n".join(
        f'<option value="{resolution}"{" selected" if resolution == selected_resolution else ""}>H3-{resolution} ({html.escape(h3_area_label(resolution))})</option>'
        for resolution in sorted(H3_COLUMNS)
    )
    return f'<select name="resolution">{options}</select>'


def geo_parent_area_link_html(target: Path, h3_cell: str, resolution: int) -> str:
    if resolution <= min(H3_COLUMNS):
        return ""
    import h3

    parent_resolution = resolution - 1
    parent_cell = h3.cell_to_parent(h3_cell, parent_resolution)
    conn = db.connect(target)
    try:
        parent_name = db.geo_place_name(conn, parent_cell)
    finally:
        conn.close()
    url = "/geo/area/" + urllib.parse.quote(parent_cell, safe="")
    label = parent_name or parent_cell
    return (
        " "
        f'<a href="{html.escape(url)}">Større område: H3-{parent_resolution} '
        f"{html.escape(label)}</a>"
    )


def geo_child_areas_section_html(rows: list[Any], *, resolution: int, inherited_name: str | None = None) -> str:
    if not rows:
        return ""
    links = "\n".join(geo_area_row_html(row, resolution=resolution, inherited_name=inherited_name) for row in rows)
    return f"""
    <section class="geo-child-areas">
      <h2>Inneholder</h2>
      <p class="meta">Understeder på H3-{h3_resolution_label(resolution)}.</p>
      <div class="geo-list">{links}</div>
    </section>
    """


def geo_stats_summary_html(stats: dict[str, int]) -> str:
    rows = "\n".join(
        f"<div><strong>{label}</strong><span>{stats[key]}</span></div>"
        for label, key in (
            ("Aktive bilder", "total"),
            ("Scannet", "scanned"),
            ("Med GPS", "with_gps"),
            ("Uten GPS", "without_gps"),
            ("Feil", "errors"),
        )
    )
    return f'<div class="geo-stats">{rows}</div>'


def geo_area_row_html(row: Any, *, resolution: int, inherited_name: str | None = None) -> str:
    h3_cell = str(row["h3_cell"])
    count = int(row["count"])
    name = row["name"] if "name" in row.keys() else None
    if name:
        label = str(name)
        detail = h3_cell
    elif inherited_name:
        label = f"{inherited_name} (arvet)"
        detail = h3_cell
    else:
        label = h3_cell
        detail = h3_resolution_label(resolution)
    url = "/geo/area/" + urllib.parse.quote(h3_cell, safe="")
    return f"""
    <a class="geo-row" href="{html.escape(url)}">
      <span>{html.escape(label)}</span>
      <span>{html.escape(detail)}</span>
      <strong>{count} bilder</strong>
    </a>
    """


def geo_places_section_html(rows: list[dict[str, object]]) -> str:
    if not rows:
        return ""
    links = "\n".join(geo_place_row_html(row) for row in rows)
    return f"""
          <section class="geo-predefined">
            <h2>Definerte steder</h2>
            <a href="/geo/custom-places">Rediger egendefinerte steder</a>
            <div class="geo-list">{links}</div>
          </section>
    """


def geo_place_row_html(row: dict[str, object]) -> str:
    slug = str(row["slug"])
    name = str(row["name"])
    count = int(row["count"])
    h3_cells = tuple(str(cell) for cell in row["h3_cells"])
    url = "/geo/place/" + urllib.parse.quote(slug, safe="")
    h3geo_url = h3geo_place_url(h3_cells)
    if row["kind"] == 'system':
        icon = "system"
    else:
        icon = "user"
    return f"""
    <div class="geo-row">
      <span><a href="{html.escape(url)}">{html.escape(name)}</a> <a href="{html.escape(h3geo_url)}" target="_blank" rel="noopener">H3Geo</a></span>
      <span class="status">{icon}</span>
      <strong>{count} bilder</strong>
    </div>
    """


def h3geo_place_url(h3_cells: tuple[str, ...]) -> str:
    return "https://h3geo.org/#hex=" + urllib.parse.quote_plus(", ".join(h3_cells))


def custom_geo_places_admin_html(places: list[PredefinedGeoPlace]) -> str:
    existing = "\n".join(custom_geo_place_edit_html(place) for place in places)
    existing_section = (
        f"""
            <h2>Lagrede steder</h2>
            <div class="custom-place-list">{existing}</div>
        """
        if existing
        else '<p class="meta">Ingen egne steder er lagret ennå.</p>'
    )
    return f"""
          <section class="custom-geo-places">
            <h2>Legg til sted</h2>
            {custom_geo_place_form_html()}
            {existing_section}
          </section>
    """


def custom_geo_place_form_html(place: PredefinedGeoPlace | None = None) -> str:
    slug = place.slug if place is not None else ""
    name = place.name if place is not None else ""
    cells = "\n".join(place.h3_cells) if place is not None else ""
    button_text = "Oppdater sted" if place is not None else "Legg til sted"
    delete_button = (
        '<button class="danger-button" type="submit" '
        'formaction="/geo/custom-place-delete" formmethod="post">Slett sted</button>'
        if place is not None
        else ""
    )
    original_slug_input = (
        f'<input type="hidden" name="original_slug" value="{html.escape(slug)}">' if place is not None else ""
    )
    return f"""
    <form action="/geo/custom-place" method="post" class="custom-place-form">
      {original_slug_input}
      <div class="custom-place-identity">
        <label>Slug <input name="slug" value="{html.escape(slug)}" autocomplete="off"></label>
        <label>Navn <input name="name" value="{html.escape(name)}" autocomplete="off"></label>
      </div>
      <label class="custom-place-cells">H3-celler <textarea name="h3_cells" rows="4">{html.escape(cells)}</textarea></label>
      <div class="custom-place-actions">
        <button type="submit">{button_text}</button>
        {delete_button}
      </div>
    </form>
    """


def custom_geo_place_edit_html(place: PredefinedGeoPlace) -> str:
    cell_count = len(place.h3_cells)
    cell_label = "1 H3-celle" if cell_count == 1 else f"{cell_count} H3-celler"
    return f"""
    <details class="custom-place-edit">
      <summary>
        <span class="custom-place-name">{html.escape(place.name)}</span>
        <span class="status">{html.escape(place.slug)}</span>
        <span class="status">{cell_label}</span>
      </summary>
      <div class="custom-place-edit-body">
        {custom_geo_place_form_html(place)}
      </div>
    </details>
    """


def geo_index_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    resolution: int = DEFAULT_GEO_RESOLUTION,
    min_count: int = DEFAULT_GEO_MIN_COUNT,
    limit: int = DEFAULT_GEO_LIMIT,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    column = h3_column_for_resolution(resolution)
    conn = db.connect(target)
    try:
        stats = db.geo_stats(conn)
        areas = db.geo_areas(conn, column=column, min_count=min_count, limit=limit)
        geo_places = geo_place_rows(conn)
    finally:
        conn.close()
    return shell_page_html(
        "Steder",
        f"""
        <nav class="subnav">
          <a href="/geo/map?resolution={resolution}&min_count={min_count}&limit={limit}">Heksagonkart</a>
          <a href="/geo/stats">Geo-statistikk</a>
          <a href="/geo/missing">Bilder uten GPS</a>
          <a href="/help/web/steder">Hjelp</a>
        </nav>
        <h2>Statistikk over bilder med GPS-posisjon</h2>
        {geo_stats_summary_html(stats)}
        {geo_places_section_html(geo_places)}
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def custom_geo_places_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    conn = db.connect(target)
    try:
        places = custom_geo_places(conn)
    finally:
        conn.close()
    return shell_page_html(
        "Egendefinerte steder",
        f"""
        <nav class="subnav">
          <a href="/geo">Steder</a>
          <a href="/help/web/egendefinerte-steder.md">Hjelp</a>
        </nav>
        <h1>Egne steder</h1>
        {custom_geo_places_admin_html(places)}
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def geo_map_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    resolution: int = DEFAULT_GEO_RESOLUTION,
    min_count: int = DEFAULT_GEO_MIN_COUNT,
    limit: int = DEFAULT_GEO_LIMIT,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    column = h3_column_for_resolution(resolution)
    conn = db.connect(target)
    try:
        areas = db.geo_areas(conn, column=column, min_count=min_count, limit=limit)
    finally:
        conn.close()
    cells = geo_map_layout(areas)
    content = geo_map_svg_html(cells) if cells else '<p class="meta">Ingen steder med nok bilder. Kjør bildebank geo-scan, eller senk min_count.</p>'
    return shell_page_html(
        "Heksagonkart",
        f"""
        <nav class="subnav">
          <a href="/geo?resolution={resolution}&min_count={min_count}&limit={limit}">Steder</a>
        </nav>
        <h1>Heksagonkart</h1>
        {geo_filter_form_html("/geo/map", resolution=resolution, min_count=min_count, limit=limit)}
        <p class="meta">Viser H3-{h3_resolution_label(resolution)}. Heksagoner som er H3-naboer legges sammen i klynger. Hver klynge orienteres etter faktiske GPS-retninger, men klyngene er ikke plassert med geografisk avstand.</p>
        {content}
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def geo_stats_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    conn = db.connect(target)
    try:
        stats = db.geo_stats(conn)
    finally:
        conn.close()
    return shell_page_html(
        "Geo-statistikk",
        f"""
        <nav class="subnav">
          <a href="/geo">Steder</a>
          <a href="/geo/missing">Bilder uten GPS</a>
        </nav>
        <h1>Geo-statistikk</h1>
        {geo_stats_summary_html(stats)}
        <p class="meta">Geo-data leses fra databasen. Kjør bildebank geo-scan for å fylle inn GPS og H3-celler.</p>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def geo_area_page_html(
    target: Path,
    h3_cell: str,
    *,
    shell_page_html: ShellPageRenderer,
    resolution: int,
    limit: int = DEFAULT_GEO_LIMIT,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    hide_out_of_focus: bool = False,
) -> str:
    from .server_browser import google_maps_link_html, source_month_item_html
    from .server_browser_sources import all_browser_source

    conn = db.connect(target)
    try:
        place_name = db.geo_place_name(conn, h3_cell)
    finally:
        conn.close()
    items = geo_area_items(target, h3_cell=h3_cell, resolution=resolution, limit=limit)
    if hide_out_of_focus:
        from .server_browser import filter_out_of_focus_items

        items = filter_out_of_focus_items(target, all_browser_source(), items, hide_out_of_focus)
    child_areas = geo_child_area_items(target, h3_cell=h3_cell, resolution=resolution)
    cards = "\n".join(source_month_item_html(target, all_browser_source(), item) for item in items)
    content = cards if cards else '<p class="meta">Ingen aktive bilder i dette området.</p>'
    maps_link = google_maps_link_html(items[0]) if items else ""
    maps_paragraph = f'<p class="meta">{maps_link}</p>' if maps_link else ""
    parent_link = geo_parent_area_link_html(target, h3_cell, resolution)
    quoted = urllib.parse.quote(h3_cell, safe="")
    title = place_name or "Sted"
    escaped_name = html.escape(place_name or "")
    child_area_section = geo_child_areas_section_html(
        child_areas,
        resolution=resolution + 1,
        inherited_name=place_name,
    )
    return shell_page_html(
        f"{title} {h3_cell}",
        f"""
        <nav class="subnav"><a href="/geo">Steder</a></nav>
        <h1>{html.escape(title)}</h1>
        <p class="meta">H3-celle {html.escape(h3_cell)}, {h3_resolution_label(resolution)}. Viser opptil {limit} bilder.{parent_link}</p>
        {maps_paragraph}
        <form action="/geo/place-name" method="post" class="geo-filter geo-name-form">
          <input type="hidden" name="h3_cell" value="{html.escape(h3_cell)}">
          <input type="hidden" name="limit" value="{limit}">
          <label>Stedsnavn <input name="name" value="{escaped_name}" autocomplete="off"></label>
          <button type="submit">Lagre navn</button>
        </form>
        {child_area_section}
        <form action="/geo/area/{html.escape(quoted)}" method="get" class="geo-filter">
          <label>Maks bilder <input name="limit" value="{limit}" inputmode="numeric"></label>
          <button type="submit">Vis</button>
        </form>
        <section class="month-grid-server">{content}</section>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def geo_missing_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    limit: int = DEFAULT_GEO_LIMIT,
    offset: int = 0,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    hide_out_of_focus: bool = False,
) -> str:
    from .server_browser import filter_out_of_focus_items, source_month_item_html
    from .server_browser_sources import all_browser_source

    items = geo_missing_items(target, limit=limit, offset=offset)
    if hide_out_of_focus:
        items = filter_out_of_focus_items(target, all_browser_source(), items, hide_out_of_focus)
    cards = "\n".join(source_month_item_html(target, all_browser_source(), item) for item in items)
    previous_offset = max(0, offset - limit)
    next_offset = offset + limit
    previous_link = (
        f'<a class="server-search-link" href="/geo/missing?limit={limit}&offset={previous_offset}">Forrige side</a>'
        if offset > 0
        else '<span class="nav-button disabled">Forrige side</span>'
    )
    next_link = (
        f'<a class="server-search-link" href="/geo/missing?limit={limit}&offset={next_offset}">Neste side</a>'
        if len(items) == limit
        else '<span class="nav-button disabled">Neste side</span>'
    )
    content = cards if cards else '<p class="meta">Ingen aktive bilder mangler GPS.</p>'
    return shell_page_html(
        "Bilder uten GPS",
        f"""
        <nav class="subnav">
          <a href="/geo">Steder</a>
          <a href="/geo/stats">Geo-statistikk</a>
        </nav>
        <h1>Bilder uten GPS</h1>
        <p class="meta">Viser {len(items)} bilder fra offset {offset}.</p>
        <nav class="controls">{previous_link}{next_link}</nav>
        <section class="month-grid-server">{content}</section>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def geo_area_items(target: Path, *, h3_cell: str, resolution: int, limit: int) -> list[Any]:
    column = h3_column_for_resolution(resolution)
    conn = db.connect(target)
    try:
        return db.geo_area_files(conn, column=column, h3_cell=h3_cell, limit=limit)
    finally:
        conn.close()


def geo_place_items(target: Path, slug: str, *, limit: int | None = None) -> list[Any]:
    place = geo_place_by_slug(target, slug)
    if place is None:
        return []
    conn = db.connect(target)
    try:
        return db.geo_place_files(conn, cells_by_column=geo_place_cells_by_column(place), limit=limit)
    finally:
        conn.close()


def geo_place_by_slug(target: Path, slug: str) -> PredefinedGeoPlace | None:
    clean_slug = slug.strip().lower()
    place = predefined_geo_place(clean_slug)
    if place is not None:
        return place
    conn = db.connect(target)
    try:
        custom = db.custom_geo_place(conn, clean_slug)
    finally:
        conn.close()
    return geo_place_from_row(custom) if custom is not None else None


def geo_place_from_row(row: dict[str, object]) -> PredefinedGeoPlace:
    return PredefinedGeoPlace(
        slug=str(row["slug"]),
        name=str(row["name"]),
        h3_cells=tuple(str(cell) for cell in row["h3_cells"]),
    )


def custom_geo_places(conn: sqlite3.Connection) -> list[PredefinedGeoPlace]:
    return [geo_place_from_row(row) for row in db.custom_geo_places(conn)]


def geo_place_cells_by_column(place: PredefinedGeoPlace) -> list[tuple[str, str]]:
    import h3

    cells_by_column: list[tuple[str, str]] = []
    max_resolution = max(H3_COLUMNS)
    for cell in place.h3_cells:
        resolution = h3_resolution_any(cell)
        query_cell = h3.cell_to_parent(cell, max_resolution) if resolution > max_resolution else cell
        query_resolution = min(resolution, max_resolution)
        cells_by_column.append((h3_column_for_resolution(query_resolution), query_cell))
    return cells_by_column


def h3_resolution_any(h3_cell: str) -> int:
    import h3

    if hasattr(h3, "is_valid_cell") and not h3.is_valid_cell(h3_cell):
        raise ValueError(f"Ugyldig H3-celle: {h3_cell}")
    try:
        return int(h3.get_resolution(h3_cell))
    except Exception as exc:  # noqa: BLE001 - h3 raises library-specific exceptions
        raise ValueError(f"Ugyldig H3-celle: {h3_cell}") from exc


def normalize_geo_place_slug(value: str) -> str:
    slug = re.sub(r"\s+", "_", value.strip().lower())
    if not slug:
        raise ValueError("Slug mangler.")
    if any(char in slug for char in "/?#"):
        raise ValueError("Slug kan ikke inneholde /, ? eller #.")
    if len(slug) > 120:
        raise ValueError("Slug er for lang.")
    return slug


def parse_geo_place_cells(value: str) -> list[str]:
    cells = [cell.strip() for cell in re.split(r"[\s,]+", value) if cell.strip()]
    clean_cells: list[str] = []
    seen: set[str] = set()
    for cell in cells:
        h3_resolution_any(cell)
        if cell not in seen:
            clean_cells.append(cell)
            seen.add(cell)
    if not clean_cells:
        raise ValueError("Stedet må ha minst én H3-celle.")
    return clean_cells


def set_geo_place_name(target: Path, h3_cell: str, name: str) -> None:
    h3_resolution_any(h3_cell)
    conn = db.connect(target)
    try:
        db.set_geo_place_name(conn, h3_cell, name)
        conn.commit()
    finally:
        conn.close()


def save_custom_geo_place(
    target: Path,
    *,
    raw_original_slug: str,
    raw_slug: str,
    name: str,
    raw_h3_cells: str,
) -> None:
    original_slug = normalize_geo_place_slug(raw_original_slug) if raw_original_slug else ""
    if predefined_geo_place(original_slug) is not None:
        raise ValueError("Innebygde steder kan ikke endres.")
    slug = normalize_geo_place_slug(raw_slug)
    if predefined_geo_place(slug) is not None:
        raise ValueError("Slug er reservert for et innebygd sted.")
    h3_cells = parse_geo_place_cells(raw_h3_cells)
    conn = db.connect(target)
    try:
        db.rename_custom_geo_place(
            conn,
            old_slug=original_slug,
            slug=slug,
            name=name,
            h3_cells=h3_cells,
        )
        conn.commit()
    finally:
        conn.close()


def delete_custom_geo_place(target: Path, raw_slug: str) -> None:
    slug = normalize_geo_place_slug(raw_slug)
    if predefined_geo_place(slug) is not None:
        raise ValueError("Innebygde steder kan ikke slettes.")
    conn = db.connect(target)
    try:
        db.delete_custom_geo_place(conn, slug)
        conn.commit()
    finally:
        conn.close()


def geo_child_area_items(target: Path, *, h3_cell: str, resolution: int) -> list[Any]:
    if resolution >= max(H3_COLUMNS):
        return []
    parent_column = h3_column_for_resolution(resolution)
    child_column = h3_column_for_resolution(resolution + 1)
    conn = db.connect(target)
    try:
        return db.geo_child_areas(
            conn,
            parent_column=parent_column,
            parent_h3_cell=h3_cell,
            child_column=child_column,
        )
    finally:
        conn.close()


def geo_missing_items(target: Path, *, limit: int, offset: int) -> list[Any]:
    conn = db.connect(target)
    try:
        return db.geo_missing_files(conn, limit=limit, offset=offset)
    finally:
        conn.close()


def geo_place_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    return [
        {
            "slug": place.slug,
            "name": place.name,
            "kind": "system" if place in PREDEFINED_GEO_PLACES else "user",
            "h3_cells": place.h3_cells,
            "count": db.geo_place_count(conn, cells_by_column=geo_place_cells_by_column(place)),
        }
        for place in sorted((*PREDEFINED_GEO_PLACES, *custom_geo_places(conn)), key=lambda item: (item.name, item.slug))
    ]
