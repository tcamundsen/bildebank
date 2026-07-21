from __future__ import annotations

import html
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .config import FaceRecognitionConfig
from .formatting import format_bytes
from .html_paths import display_relative_path
from .media import media_kind
from .server_browser_item_html import (
    breadcrumb_html,
    previous_month_overview_url,
    previous_year_overview_url,
    rotation_style_attr,
    source_breadcrumb_label,
)
from .server_browser_queries import (
    browser_year_cards,
    browser_year_month_cards,
    first_month_in_year,
    last_month_before_year,
    source_item_count,
    source_month_navigation_for_key,
    source_month_keys,
    source_year_cards,
    source_year_month_cards,
    valid_month_key,
    valid_year_key,
)
from .server_browser_sources import (
    BrowserSource,
    all_browser_source,
    is_filtered_source,
    person_browser_source,
    source_item_url,
    source_month_url,
    source_year_url,
)
ShellPageRenderer = Callable[..., str]
PageRenderer = Callable[[str, str], str]
Breadcrumb = tuple[str, str | None] | tuple[str, str | None, str | None]
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


def empty_browser_html(
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    search_link = '<p><a href="/search" data-search-preload>Bildesøk</a></p>' if openclip_enabled else ""
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




def month_page_html(
    target: Path,
    month_key: str,
    items: list[Any],
    *,
    page_html: PageRenderer,
) -> str:
    return source_month_page_html(target, all_browser_source(), month_key, items, page_html=page_html)


def years_page_html(
    target: Path,
    *,
    page_html: PageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    hide_out_of_focus: bool = False,
) -> str:
    from .server_shell import app_header_html

    year_cards = browser_year_cards(target, hide_out_of_focus=hide_out_of_focus)
    cards = "\n".join(year_card_html(target, card) for card in year_cards)
    content = cards if cards else '<p class="meta">Ingen filer i bildesamlingen.</p>'
    controls = years_navigation_controls_html(year_cards)
    return page_html(
        "År",
        f"""
        <main class="server-browser years-browser">
          {app_header_html(
              "År",
              controls=controls,
              face_enabled=face_enabled,
              openclip_enabled=openclip_enabled,
          )}
          <section class="month-grid-server years-grid-server">{content}</section>
        </main>
        """,
    )


def years_navigation_controls_html(year_cards: list[dict[str, Any]]) -> str:
    from .server_shell import nav_button, nav_disabled

    first_card = year_cards[0] if year_cards else None
    first_month = str(first_card["first_month"]) if first_card is not None else None
    first_year = str(first_card["year"]) if first_card is not None else None
    first_item = first_card["item"] if first_card is not None else None

    def year_button(target_year: str | None, label: str, key_nav: str, tooltip: str) -> str:
        if target_year is None:
            return nav_disabled(label)
        return nav_button(source_year_url(all_browser_source(), target_year), label, key_nav, tooltip)

    def month_button(month_key: str | None, label: str, key_nav: str, tooltip: str) -> str:
        if month_key is None:
            return nav_disabled(label)
        return nav_button(source_month_url(all_browser_source(), month_key), label, key_nav, tooltip)

    return f"""
    <nav class="controls" aria-label="Navigering">
      <span class="nav-button-pair" data-nav-button-pair="year">
        {year_button(None, "◀ Å", "previous-year", "Forrige år")}
        {year_button(first_year, "r ▶", "next-year", "Neste år")}
      </span>
      <span class="nav-button-pair" data-nav-button-pair="month">
        {month_button(None, "◀ Mån", "previous-month", "Forrige måned")}
        {month_button(first_month, "ed ▶", "next-month", "Neste måned")}
      </span>
      <span class="nav-button-pair" data-nav-button-pair="item">
        {nav_disabled("◀ Bil")}
        {item_button(all_browser_source(), first_item, "de ▶", "next", "Neste bilde")}
      </span>
    </nav>
    """


def source_years_navigation_controls_html(source: BrowserSource, year_cards: list[dict[str, Any]]) -> str:
    from .server_shell import nav_button, nav_disabled

    first_card = year_cards[0] if year_cards else None
    first_month = str(first_card["first_month"]) if first_card is not None else None
    first_year = str(first_card["year"]) if first_card is not None else None
    first_item = first_card["item"] if first_card is not None else None

    def year_button(target_year: str | None, label: str, key_nav: str, tooltip: str) -> str:
        if target_year is None:
            return nav_disabled(label)
        return nav_button(source_year_url(source, target_year), label, key_nav, tooltip)

    def month_button(month_key: str | None, label: str, key_nav: str, tooltip: str) -> str:
        if month_key is None:
            return nav_disabled(label)
        return nav_button(source_month_url(source, month_key), label, key_nav, tooltip)

    return f"""
    <nav class="controls" aria-label="Navigering">
      <span class="nav-button-pair" data-nav-button-pair="year">
        {year_button(None, "◀ Å", "previous-year", "Forrige år")}
        {year_button(first_year, "r ▶", "next-year", "Neste år")}
      </span>
      <span class="nav-button-pair" data-nav-button-pair="month">
        {month_button(None, "◀ Mån", "previous-month", "Forrige måned")}
        {month_button(first_month, "ed ▶", "next-month", "Neste måned")}
      </span>
      <span class="nav-button-pair" data-nav-button-pair="item">
        {nav_disabled("◀ Bil")}
        {item_button(source, first_item, "de ▶", "next", "Neste bilde")}
      </span>
    </nav>
    """


def year_months_page_html(
    target: Path,
    year: str,
    *,
    page_html: PageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    hide_out_of_focus: bool = False,
) -> str:
    from .server_shell import app_header_html

    cards = "\n".join(
        year_month_card_html(target, all_browser_source(), card)
        for card in browser_year_month_cards(target, year, hide_out_of_focus=hide_out_of_focus)
    )
    content = cards if cards else '<p class="meta">Ingen bilder dette året.</p>'
    escaped_year = html.escape(year)
    controls = year_navigation_controls_html(target, year, hide_out_of_focus=hide_out_of_focus)
    return page_html(
        escaped_year,
        f"""
        <main class="server-browser">
          {app_header_html(
              escaped_year,
              title_html=breadcrumb_html([("År", "/years")], escaped_year),
              controls=controls,
              face_enabled=face_enabled,
              openclip_enabled=openclip_enabled,
          )}
          <section class="month-grid-server year-month-grid-server">{content}</section>
        </main>
        """,
    )


def year_navigation_controls_html(target: Path, year: str, *, hide_out_of_focus: bool = False) -> str:
    return source_year_navigation_controls_html(
        target,
        all_browser_source(),
        year,
        hide_out_of_focus=hide_out_of_focus,
    )


def source_year_navigation_controls_html(
    target: Path,
    source: BrowserSource,
    year: str,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
) -> str:
    from .server_shell import nav_button, nav_disabled

    month_keys = source_month_keys(target, source, face_config, hide_out_of_focus=hide_out_of_focus)
    years = sorted({key[:4] for key in month_keys})
    previous_year = next((candidate for candidate in reversed(years) if candidate < year), None)
    next_year = next((candidate for candidate in years if candidate > year), None)
    previous_month = last_month_before_year(month_keys, year)
    next_month = first_month_in_year(month_keys, year)
    month_cards = source_year_month_cards(
        target,
        source,
        year,
        face_config,
        hide_out_of_focus=hide_out_of_focus,
    )
    first_item = month_cards[0]["item"] if month_cards else None
    last_item = month_cards[-1]["item"] if month_cards else None
    previous_overview_url = None
    if previous_year is None and years and year == years[0]:
        previous_overview_url = source.root_url if is_filtered_source(source) else "/years"

    def year_button(target_year: str | None, label: str, key_nav: str, tooltip: str) -> str:
        if target_year is None:
            return nav_disabled(label)
        return nav_button(source_year_url(source, target_year), label, key_nav, tooltip)

    def previous_year_button() -> str:
        if previous_year is not None:
            return year_button(previous_year, "◀ Å", "previous-year", "Forrige år")
        if previous_overview_url is not None:
            return nav_button(previous_overview_url, "◀ Å", "previous-year", "Forrige år")
        return nav_disabled("◀ Å")

    def month_button(month_key: str | None, label: str, key_nav: str, tooltip: str) -> str:
        if month_key is None:
            return nav_disabled(label)
        return nav_button(source_month_url(source, month_key), label, key_nav, tooltip)

    def previous_month_button() -> str:
        if previous_month is not None:
            return month_button(previous_month, "◀ Mån", "previous-month", "Forrige måned")
        if previous_overview_url is not None:
            return nav_button(previous_overview_url, "◀ Mån", "previous-month", "Forrige måned")
        return nav_disabled("◀ Mån")

    return f"""
    <nav class="controls" aria-label="Navigering">
      <span class="nav-button-pair" data-nav-button-pair="year">
        {previous_year_button()}
        {year_button(next_year, "r ▶", "next-year", "Neste år")}
      </span>
      <span class="nav-button-pair" data-nav-button-pair="month">
        {previous_month_button()}
        {month_button(next_month, "ed ▶", "next-month", "Neste måned")}
      </span>
      <span class="nav-button-pair" data-nav-button-pair="item">
        {item_button(source, last_item, "◀ Bil", "previous", "Forrige bilde")}
        {item_button(source, first_item, "de ▶", "next", "Neste bilde")}
      </span>
    </nav>
    """


def item_button(source: BrowserSource, item: Any | None, label: str, key_nav: str, tooltip: str) -> str:
    from .server_shell import nav_button, nav_disabled

    if item is None:
        return nav_disabled(label)
    return nav_button(source_item_url(source, int(item["id"])), label, key_nav, tooltip)


def source_year_months_page_html(
    target: Path,
    source: BrowserSource,
    year: str,
    *,
    page_html: PageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
    hide_out_of_focus: bool = False,
) -> str:
    from .server_shell import app_header_html

    cards = "\n".join(
        year_month_card_html(target, source, card)
        for card in source_year_month_cards(
            target,
            source,
            year,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
        )
    )
    content = cards if cards else '<p class="meta">Ingen bilder dette året.</p>'
    controls = source_year_navigation_controls_html(
        target,
        source,
        year,
        face_config,
        hide_out_of_focus=hide_out_of_focus,
    )
    return page_html(
        f"{source.title}: {year}",
        f"""
        <main class="server-browser">
          {app_header_html(
              source.title,
              source=source,
              title_html=source_year_breadcrumb_html(
                  target,
                  source,
                  year,
                  face_config,
                  hide_out_of_focus=hide_out_of_focus,
              ),
              controls=controls,
              face_enabled=face_enabled,
              openclip_enabled=openclip_enabled,
          )}
          <section class="month-grid-server year-month-grid-server">{content}</section>
        </main>
        """,
    )


def source_years_page_html(
    target: Path,
    source: BrowserSource,
    *,
    page_html: PageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    face_config: FaceRecognitionConfig | None = None,
    hide_out_of_focus: bool = False,
) -> str:
    from .server_shell import app_header_html

    year_cards = source_year_cards(
        target,
        source,
        face_config,
        hide_out_of_focus=hide_out_of_focus,
    )
    cards = "\n".join(source_year_card_html(target, source, card) for card in year_cards)
    if cards:
        content = cards
    elif source_item_count(target, source, face_config, hide_out_of_focus=hide_out_of_focus) > 0:
        content = '<p class="meta">Ingen daterte bilder matcher denne visningen.</p>'
    else:
        content = f'<p class="meta">{html.escape(empty_source_message(source))}</p>'
    controls = source_years_navigation_controls_html(source, year_cards)
    if source.text_filter is None:
        source_label = source.title
        source_title = None
    else:
        source_label, source_title = source_breadcrumb_label(
            target,
            source,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
        )
    title_attr = f' title="{html.escape(source_title)}"' if source_title else ""
    title_html = f"<span{title_attr}>{html.escape(source_label)}</span>" if source_title else html.escape(source_label)
    return page_html(
        source_label,
        f"""
        <main class="server-browser years-browser">
          {app_header_html(
              source_label,
              source=source,
              title_html=title_html,
              controls=controls,
              face_enabled=face_enabled,
              openclip_enabled=openclip_enabled,
          )}
          <section class="month-grid-server years-grid-server">{content}</section>
        </main>
        """,
    )


def source_year_breadcrumb_html(
    target: Path,
    source: BrowserSource,
    year: str,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> str:
    if not valid_year_key(year):
        return html.escape(source.title)
    source_label, source_title = source_breadcrumb_label(
        target,
        source,
        face_config,
        hide_out_of_focus=hide_out_of_focus,
        conn=conn,
    )
    return breadcrumb_html([(source_label, source.root_url, source_title)], html.escape(year))


def year_card_html(target: Path, card: dict[str, Any]) -> str:
    return source_year_card_html(target, all_browser_source(), card)


def source_year_card_html(target: Path, source: BrowserSource, card: dict[str, Any]) -> str:
    year = str(card["year"])
    month_count = int(card["month_count"])
    item_count = int(card["item_count"])
    item = card["item"]
    month_label = "måned" if month_count == 1 else "måneder"
    image_label = "bilde" if item_count == 1 else "bilder"
    media = thumbnail_media_html(target, item)
    return f"""
    <article class="item">
      <a class="thumb-link" href="{html.escape(source_year_url(source, year))}">{media}</a>
      <div class="text">
        <div class="path">{html.escape(year)}</div>
        <div class="score">{month_count} {month_label}, {item_count} {image_label}</div>
      </div>
    </article>
    """


def year_month_card_html(target: Path, source: BrowserSource, card: dict[str, Any]) -> str:
    month_key = str(card["month_key"])
    item_count = int(card["item_count"])
    item = card["item"]
    image_label = "bilde" if item_count == 1 else "bilder"
    media = thumbnail_media_html(target, item)
    return f"""
    <article class="item">
      <a class="thumb-link" href="{html.escape(source_month_url(source, month_key))}">{media}</a>
      <div class="text">
        <div class="path">{html.escape(month_key)}</div>
        <div class="score">{item_count} {image_label}</div>
      </div>
    </article>
    """


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
    hide_out_of_focus: bool = False,
) -> str:
    from .server_shell import app_header_html, source_controls_html, suggestion_toggle_button_html

    cards = "\n".join(source_month_item_html(target, source, item) for item in items)
    previous_item = items[-1] if items else None
    next_item = items[0] if items else None
    month_nav = source_month_navigation_for_key(
        target,
        source,
        month_key,
        face_config,
        hide_out_of_focus=hide_out_of_focus,
    )
    controls = source_controls_html(
        source,
        month_nav,
        previous_item,
        next_item,
        suggestion_toggle_button=suggestion_toggle_button_html(source, None, face_enabled=face_enabled),
        year_links_to_year_pages=True,
        previous_year_fallback_url=previous_year_overview_url(
            target,
            source,
            month_key,
            month_nav,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
        ),
        previous_month_fallback_url=(
            previous_month_overview_url(source, month_key, month_nav) if items else None
        ),
    )
    return page_html(
        f"{source.title}: {month_key}",
        f"""
        <main class="server-browser month-browser">
          {app_header_html(
              source.title,
              source=source,
              title_html=source_month_breadcrumb_html(
                  target,
                  source,
                  month_key,
                  face_config,
                  hide_out_of_focus=hide_out_of_focus,
              ),
              controls=controls,
              face_enabled=face_enabled,
              openclip_enabled=openclip_enabled,
          )}
          <section class="month-grid-server">{cards}</section>
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
        if source.reference_suggestions_person_name is not None:
            return "Ingen aktive foreslåtte bilder for dette referansebildet."
        if source.source_id is not None:
            return "Ingen aktive bilder for denne kilden."
        if source.geo_place_slug is not None:
            return "Ingen aktive bilder for dette stedet."
        if source.text_filter is not None:
            return "Ingen aktive bilder matcher filtersøket."
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


def source_month_breadcrumb_html(
    target: Path,
    source: BrowserSource,
    month_key: str,
    face_config: FaceRecognitionConfig | None = None,
    *,
    hide_out_of_focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> str:
    if not valid_month_key(month_key):
        return html.escape(source.title)
    year, month = month_key.split("-", 1)
    month_name = MONTH_NAMES.get(month, month_key)
    crumbs: list[Breadcrumb]
    if source == all_browser_source():
        crumbs = [
            ("År", "/years"),
            (year, source_year_url(source, year)),
        ]
    else:
        source_label, source_title = source_breadcrumb_label(
            target,
            source,
            face_config,
            hide_out_of_focus=hide_out_of_focus,
            conn=conn,
        )
        crumbs = [
            (source_label, source.root_url, source_title),
            (year, source_year_url(source, year)),
        ]
    return breadcrumb_html(crumbs, html.escape(month_name))


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
    kind = media_kind(target_path)
    if kind == "video":
        return f'<div class="video-thumb">Video<br>{name}</div>'
    if kind != "image":
        return f'<div class="video-thumb">Fil<br>{name}</div>'
    thumbnail_src = f"/thumbnail/{int(item['id'])}"
    return f'<img src="{html.escape(thumbnail_src)}" alt="{name}" loading="lazy"{rotation_style_attr(item)}>'
