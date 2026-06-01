from __future__ import annotations

import html
from typing import Any, Callable

from .server_browser_sources import (
    BrowserSource,
    all_browser_source,
    person_browser_source,
    source_item_url,
    source_month_url,
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
        '<a class="server-search-link" href="/geo">Steder</a>',
        '<a class="server-search-link" href="/sources">Kilder</a>',
        '<a class="server-search-link" href="/tags">Tagger</a>',
    ]
    if face_enabled:
        links.insert(0, '<a class="server-search-link" href="/people">Personer</a>')
    all_label = html.escape(all_items_label)
    if source == all_browser_source() and item is None:
        links.insert(0, f'<a class="server-search-link" href="/">{all_label}</a>')
    if source.date_source is not None or source.source_id is not None or source.geo_place_slug is not None or source.tag_name is not None:
        all_url = all_items_url or (source_item_url(all_browser_source(), int(item["id"])) if item is not None else "/")
        links.insert(0, f'<a class="server-search-link" href="{html.escape(all_url)}">{all_label}</a>')
    if source.person_name is not None and face_enabled:
        all_url = all_items_url or (source_item_url(all_browser_source(), int(item["id"])) if item is not None else "/")
        links.insert(0, f'<a class="server-search-link" href="{html.escape(all_url)}">{all_label}</a>')
        if source.show_faces:
            no_faces_source = person_browser_source(
                source.person_name,
                include_suggestions=source.include_suggestions,
                show_faces=False,
            )
            no_faces_url = source_item_url(no_faces_source, int(item["id"])) if item is not None else no_faces_source.root_url
            links.insert(
                1,
                f'<a class="server-search-link" href="{html.escape(no_faces_url)}">Uten ansiktsmarkering</a>',
            )
        else:
            faces_source = person_browser_source(
                source.person_name,
                include_suggestions=source.include_suggestions,
                show_faces=True,
            )
            faces_url = source_item_url(faces_source, int(item["id"])) if item is not None else faces_source.root_url
            links.insert(
                1,
                f'<a class="server-search-link" href="{html.escape(faces_url)}">Med ansiktsmarkering</a>',
            )
        if source.include_suggestions:
            links.insert(
                2,
                f'<a class="server-search-link" href="{html.escape(person_browser_source(source.person_name, include_suggestions=False).root_url)}">Bare bekreftede</a>',
            )
        else:
            links.insert(
                2,
                f'<a class="server-search-link" href="{html.escape(person_browser_source(source.person_name, include_suggestions=True).root_url)}">Med forslag</a>',
            )
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
    search_link = '<a class="server-search-link" href="/search">Bildesøk</a>' if openclip_enabled else ""
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


def nav_button(href: str, label: str, key_nav: str) -> str:
    return f'<a class="nav-button" href="{href}" data-key-nav="{html.escape(key_nav)}">{html.escape(label)}</a>'


def nav_disabled(label: str) -> str:
    return f'<span class="nav-button disabled">{html.escape(label)}</span>'


def source_controls_html(
    source: BrowserSource,
    month_nav: dict[str, str | None],
    previous_item: Any | None,
    next_item: Any | None,
    *,
    include_info_button: bool = False,
    info_button: str = "",
    rotation_buttons: str = "",
    manual_location_button: str = "",
    unconfirm_buttons: str = "",
    delete_button: str = "",
) -> str:
    return f"""
    <nav class="controls" aria-label="Navigering">
      {source_month_nav_link(source, month_nav["previous_year"], "Forrige år", "previous-year")}
      {source_month_nav_link(source, month_nav["next_year"], "Neste år", "next-year")}
      {source_month_nav_link(source, month_nav["previous_month"], "Forrige måned", "previous-month")}
      {source_month_nav_link(source, month_nav["next_month"], "Neste måned", "next-month")}
      {source_nav_link(source, previous_item, "Forrige bilde", "previous")}
      {source_nav_link(source, next_item, "Neste bilde", "next")}
      {rotation_buttons}
      {manual_location_button}
      {info_button if include_info_button else ""}
      {unconfirm_buttons}
      {delete_button}
    </nav>
    """


def source_nav_link(source: BrowserSource, item: Any | None, label: str, key_nav: str) -> str:
    if item is None:
        return nav_disabled(label)
    return nav_button(source_item_url(source, int(item["id"])), label, key_nav)


def source_month_nav_link(source: BrowserSource, month_key: str | None, label: str, key_nav: str) -> str:
    if month_key is None:
        return nav_disabled(label)
    return nav_button(source_month_url(source, month_key), label, key_nav)
