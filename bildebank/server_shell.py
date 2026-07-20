from __future__ import annotations

import html
from typing import Any, Callable

from .server_browser_sources import (
    BrowserSource,
    all_browser_source,
    person_browser_source,
    source_item_url,
    source_month_url,
    source_year_url,
)


PageRenderer = Callable[[str, str], str]


def shell_page_html(
    title: str,
    content: str,
    *,
    page_html: PageRenderer,
    main_class: str = "shell",
    source: BrowserSource | None = None,
    item: Any | None = None,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    all_items_url: str | None = None,
    all_items_label: str = "Alle bilder",
    title_html: str | None = None,
) -> str:
    return page_html(
        title,
        f"""
        {app_header_html(
            title,
            source=source,
            item=item,
            title_html=title_html,
            face_enabled=face_enabled,
            openclip_enabled=openclip_enabled,
            all_items_url=all_items_url,
            all_items_label=all_items_label,
        )}
        <main class="{html.escape(main_class)}">
          {content}
        </main>
        """,
    )


def error_html(
    exc: Exception,
    *,
    shell_page_html: Callable[..., str],
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    return shell_page_html(
        "Feil",
        f"""
        <h1>Feil</h1>
        <p class="error">{html.escape(str(exc))}</p>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def message_html(message: str) -> str:
    if not message:
        return ""
    return f'<p class="message">{html.escape(message)}</p>'


def source_top_links_html(
    source: BrowserSource,
    item: Any | None = None,
    *,
    face_enabled: bool = True,
    all_items_url: str | None = None,
    all_items_label: str = "Alle bilder",
) -> str:
    links = [
        '<a class="server-search-link" href="/filter">Filtersøk</a>',
        '<a class="server-search-link" href="/geo">Steder</a>',
        '<a class="server-search-link" href="/tags">Tagger</a>',
    ]
    if face_enabled:
        links.insert(0, '<a class="server-search-link" href="/people">Personer</a>')
    all_label = html.escape(all_items_label)
    if source == all_browser_source() and item is None:
        links.insert(0, f'<a class="server-search-link" href="/">{all_label}</a>')
    if (
        source.source_id is not None
        or source.geo_place_slug is not None
        or source.tag_name is not None
        or source.text_filter is not None
    ):
        all_url = all_items_url or (source_item_url(all_browser_source(), int(item["id"])) if item is not None else "/")
        links.insert(0, f'<a class="server-search-link" href="{html.escape(all_url)}">{all_label}</a>')
    if source.person_name is not None and face_enabled:
        all_url = all_items_url or (source_item_url(all_browser_source(), int(item["id"])) if item is not None else "/")
        links.insert(0, f'<a class="server-search-link" href="{html.escape(all_url)}">{all_label}</a>')
    return "\n".join(links)


def source_action_links_html(
    source: BrowserSource,
    item: Any | None = None,
    *,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    all_items_url: str | None = None,
    all_items_label: str = "Alle bilder",
) -> str:
    search_link = (
        '<a class="server-search-link" href="/search" data-search-preload>Bildesøk</a>'
        if openclip_enabled
        else ""
    )
    return f"""
    <div class="top-actions">
      {source_top_links_html(
          source,
          item,
          face_enabled=face_enabled,
          all_items_url=all_items_url,
          all_items_label=all_items_label,
      )}
      {search_link}
      <a class="server-search-link" href="/dashboard">Dashboard</a>
      <a class="server-search-link" href="/help/web/bildebrowser">Hjelp</a>
      <a class="server-search-link" href="/settings">Innstillinger</a>
    </div>
    """


def app_topline_html(
    title: str,
    *,
    source: BrowserSource | None = None,
    item: Any | None = None,
    extra_html: str = "",
    title_html: str | None = None,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    all_items_url: str | None = None,
    all_items_label: str = "Alle bilder",
) -> str:
    rendered_title = title_html if title_html is not None else html.escape(title)
    return f"""
    <div class="topline">
      <div class="title">{rendered_title}</div>
      {extra_html}
      {source_action_links_html(
          source or all_browser_source(),
          item,
          face_enabled=face_enabled,
          openclip_enabled=openclip_enabled,
          all_items_url=all_items_url,
          all_items_label=all_items_label,
      )}
    </div>
    """


def app_header_html(
    title: str,
    *,
    source: BrowserSource | None = None,
    item: Any | None = None,
    extra_html: str = "",
    title_html: str | None = None,
    controls: str = "",
    message_html: str = "",
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    all_items_url: str | None = None,
    all_items_label: str = "Alle bilder",
) -> str:
    return f"""
    <header class="browser-header">
      {app_topline_html(
          title,
          source=source,
          item=item,
          extra_html=extra_html,
          title_html=title_html,
          face_enabled=face_enabled,
          openclip_enabled=openclip_enabled,
          all_items_url=all_items_url,
          all_items_label=all_items_label,
      )}
      {controls}
      {message_html}
    </header>
    """


def nav_button(href: str, label: str, key_nav: str, tooltip: str = "") -> str:
    return f'<a class="nav-button" href="{href}" title="{tooltip}" data-key-nav="{html.escape(key_nav)}">{html.escape(label)}</a>'


def nav_disabled(label: str) -> str:
    return f'<span class="nav-button disabled">{html.escape(label)}</span>'


def face_toggle_button_html(source: BrowserSource, item: Any | None, *, face_enabled: bool = True) -> str:
    if not face_enabled or source.person_name is None or item is None:
        return ""
    target_source = person_browser_source(
        source.person_name,
        include_suggestions=source.include_suggestions,
        show_faces=not source.show_faces,
    )
    href = source_item_url(target_source, int(item["id"]))
    title = "Skjul ansiktsmarkering" if source.show_faces else "Vis ansiktsmarkering"
    icon_class = "face-toggle-icon face-toggle-icon-active" if source.show_faces else "face-toggle-icon"
    return f'<a class="nav-button" href="{html.escape(href)}" title="{title}"><span class="{icon_class}">👤</span></a>'


def suggestion_toggle_button_html(
    source: BrowserSource,
    item: Any | None,
    *,
    face_enabled: bool = True,
    href: str | None = None,
) -> str:
    if not face_enabled or source.person_name is None:
        return ""
    target_source = person_browser_source(
        source.person_name,
        include_suggestions=not source.include_suggestions,
        show_faces=source.show_faces,
    )
    href = href or (source_item_url(target_source, int(item["id"])) if item is not None else target_source.root_url)
    box = "[✓]" if source.include_suggestions else "[&nbsp;&nbsp;&nbsp;]"
    return f'<a class="nav-button" href="{html.escape(href)}">{box} Ta med forslag</a>'


def face_suggest_button_html(*, face_enabled: bool = True) -> str:
    if not face_enabled:
        return ""
    return (
        '<button class="nav-button" type="button" data-open-face-suggest '
        'title="Kjør face-suggest for å finne ansikter">👥✨</button>'
    )


def face_matches_button_html(file_id: int, *, face_enabled: bool = True) -> str:
    if not face_enabled:
        return ""
    return (
        '<button class="nav-button" type="button" data-open-face-matches '
        f'data-face-matches-item="{file_id}" title="Vis mulige ansiktstreff">Ansiktstreff</button>'
    )


def source_controls_html(
    source: BrowserSource,
    month_nav: dict[str, str | None],
    previous_item: Any | None,
    next_item: Any | None,
    *,
    include_info_button: bool = False,
    info_button: str = "",
    rotation_buttons: str = "",
    manual_date_button: str = "",
    associated_file_buttons: str = "",
    manual_person_controls: str = "",
    manual_location_button: str = "",
    face_toggle_button: str = "",
    face_suggest_button: str = "",
    face_matches_button: str = "",
    suggestion_toggle_button: str = "",
    unconfirm_buttons: str = "",
    delete_button: str = "",
    year_links_to_year_pages: bool = False,
    previous_year_fallback_url: str | None = None,
    previous_month_fallback_url: str | None = None,
) -> str:
    previous_year_link = source_year_nav_link if year_links_to_year_pages else source_month_nav_link
    next_year_link = source_year_nav_link if year_links_to_year_pages else source_month_nav_link
    previous_month_link = source_month_nav_link(
        source,
        month_nav["previous_month"],
        "◀ Mån",
        "previous-month",
        "Forrige måned",
        fallback_url=previous_month_fallback_url,
    )
    return f"""
    <nav class="controls" aria-label="Navigering">
      <span class="nav-button-pair" data-nav-button-pair="year">
        {previous_year_link(
            source,
            month_nav["previous_year"],
            "◀ Å",
            "previous-year",
            "Forrige år",
            fallback_url=previous_year_fallback_url,
        )}
        {next_year_link(source, month_nav["next_year"], "r ▶", "next-year", "Neste år")}
      </span>
      <span class="nav-button-pair" data-nav-button-pair="month">
        {previous_month_link}
        {source_month_nav_link(source, month_nav["next_month"], "ed ▶", "next-month", "Neste måned")}
      </span>
      <span class="nav-button-pair" data-nav-button-pair="item">
        {source_nav_link(source, previous_item, "◀ Bil", "previous", "Forrige bilde")}
        {source_nav_link(source, next_item, "de ▶", "next", "Neste bilde")}
      </span>
      {rotation_buttons}
      {manual_date_button}
      {associated_file_buttons}
      {manual_person_controls}
      {manual_location_button}
      {face_toggle_button}
      {face_suggest_button}
      {face_matches_button}
      {suggestion_toggle_button}
      {info_button if include_info_button else ""}
      {unconfirm_buttons}
      {delete_button}
    </nav>
    """


def source_nav_link(source: BrowserSource, item: Any | None, label: str, key_nav: str, tooltip: str) -> str:
    if item is None:
        return nav_disabled(label)
    return nav_button(source_item_url(source, int(item["id"])), label, key_nav, tooltip)


def source_month_nav_link(
    source: BrowserSource,
    month_key: str | None,
    label: str,
    key_nav: str,
    tooltip: str,
    *,
    fallback_url: str | None = None,
) -> str:
    if month_key is None:
        if fallback_url is not None:
            return nav_button(fallback_url, label, key_nav, tooltip)
        return nav_disabled(label)
    return nav_button(source_month_url(source, month_key), label, key_nav, tooltip)


def source_year_nav_link(
    source: BrowserSource,
    month_key: str | None,
    label: str,
    key_nav: str,
    tooltip: str,
    *,
    fallback_url: str | None = None,
) -> str:
    if month_key is None:
        if fallback_url is not None:
            return nav_button(fallback_url, label, key_nav, tooltip)
        return nav_disabled(label)
    return nav_button(source_year_url(source, month_key[:4]), label, key_nav, tooltip)
