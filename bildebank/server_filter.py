from __future__ import annotations

import datetime as dt
import html
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from . import db
from .server_shell import message_html


ShellPageRenderer = Callable[..., str]


@dataclass(frozen=True)
class BrowserTextFilter:
    query: str
    after: dt.date | None = None
    before: dt.date | None = None
    location: str | None = None


def parse_text_filter(query: str) -> BrowserTextFilter:
    clean_query = " ".join(query.strip().split())
    if not clean_query:
        raise ValueError("Filtersøk kan ikke være tomt.")
    after = None
    before = None
    location = None
    for token in clean_query.split():
        if ":" not in token:
            raise ValueError(f"Ukjent filter: {token}")
        key, value = token.split(":", 1)
        key = key.strip().casefold()
        value = value.strip()
        if not value:
            raise ValueError(f"Filteret mangler verdi: {key}:")
        if key == "after":
            if after is not None:
                raise ValueError("after kan bare brukes én gang.")
            after = parse_filter_date(value, key)
        elif key == "before":
            if before is not None:
                raise ValueError("before kan bare brukes én gang.")
            before = parse_filter_date(value, key)
        elif key == "location":
            if location is not None:
                raise ValueError("location kan bare brukes én gang.")
            if value not in {"gps", "manual"}:
                raise ValueError("location må være gps eller manual.")
            location = value
        else:
            raise ValueError(f"Ukjent filter: {key}")
    return BrowserTextFilter(clean_query, after=after, before=before, location=location)


def parse_filter_date(value: str, key: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{key} må være en dato på formen YYYY-MM-DD.") from exc


def text_filter_url(query: str) -> str:
    return "/filter/" + urllib.parse.quote(parse_text_filter(query).query, safe="")


def text_filter_browser_source(query: str) -> Any:
    from .server_browser_sources import BrowserSource

    text_filter = parse_text_filter(query)
    return BrowserSource(
        f"Filtersøk: {text_filter.query}",
        text_filter_url(text_filter.query),
        text_filter=text_filter,
    )


def text_filter_where_clause(text_filter: BrowserTextFilter) -> tuple[str, tuple[object, ...]]:
    where: list[str] = []
    params: list[object] = []
    if text_filter.after is not None or text_filter.before is not None:
        where.append(f"{db.BROWSER_DATE_ORDER_SQL} GLOB {db.DATE_GLOB_SQL}")
    if text_filter.after is not None:
        where.append(f"{db.BROWSER_DATE_ORDER_SQL} > ?")
        params.append(text_filter.after.isoformat())
    if text_filter.before is not None:
        where.append(f"{db.BROWSER_DATE_ORDER_SQL} < ?")
        params.append(text_filter.before.isoformat())
    if text_filter.location == "gps":
        where.append("gps_lat IS NOT NULL")
        where.append("gps_lon IS NOT NULL")
        where.append("(gps_source IS NULL OR gps_source != 'manual-h3')")
    elif text_filter.location == "manual":
        where.append("gps_source = 'manual-h3'")
    if not where:
        raise ValueError("Filtersøket må ha minst ett kriterium.")
    return " AND ".join(where), tuple(params)


def filter_start_html(
    *,
    shell_page_html: ShellPageRenderer,
    query: str = "",
    message: str = "",
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    return shell_page_html(
        "Filtersøk",
        f"""
        <h1>Filtersøk</h1>
        {message_html(message)}
        {filter_form(query)}
        <p class="meta">Eksempler: after:2023-12-01 before:2024-12-12, location:gps, location:manual.</p>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def filter_form(query: str) -> str:
    return f"""
    <form action="/filter" method="get" class="search">
      <input name="q" value="{html.escape(query)}" placeholder="after:2023-12-01 location:gps" autofocus>
      <button type="submit">Søk</button>
    </form>
    """
