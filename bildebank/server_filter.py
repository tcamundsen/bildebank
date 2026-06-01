from __future__ import annotations

import datetime as dt
import html
import re
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
    date_source: str | None = None
    location: str | None = None
    size_gt: int | None = None
    size_lt: int | None = None


def parse_text_filter(query: str) -> BrowserTextFilter:
    clean_query = " ".join(query.strip().split())
    if not clean_query:
        raise ValueError("Filtersøk kan ikke være tomt.")
    after = None
    before = None
    date_source = None
    location = None
    size_gt = None
    size_lt = None
    for token in clean_query.split():
        size_operator = size_filter_operator(token)
        if size_operator is not None:
            operator, value = size_operator
            if operator == ">":
                if size_gt is not None:
                    raise ValueError("size> kan bare brukes én gang.")
                size_gt = parse_size_bytes(value)
            else:
                if size_lt is not None:
                    raise ValueError("size< kan bare brukes én gang.")
                size_lt = parse_size_bytes(value)
            continue
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
        elif key == "date":
            if date_source is not None:
                raise ValueError("date kan bare brukes én gang.")
            if value not in {"manual", "metadata", "filename", "mtime"}:
                raise ValueError("date må være manual, metadata, filename eller mtime.")
            date_source = value
        else:
            raise ValueError(f"Ukjent filter: {key}")
    return BrowserTextFilter(
        clean_query,
        after=after,
        before=before,
        date_source=date_source,
        location=location,
        size_gt=size_gt,
        size_lt=size_lt,
    )


def parse_filter_date(value: str, key: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{key} må være en dato på formen YYYY-MM-DD.") from exc


def size_filter_operator(token: str) -> tuple[str, str] | None:
    if token.startswith("size>"):
        return ">", token.removeprefix("size>")
    if token.startswith("size<"):
        return "<", token.removeprefix("size<")
    return None


def parse_size_bytes(value: str) -> int:
    match = re.fullmatch(r"(?i)(\d+(?:[.,]\d+)?)(B|KB|MB|GB|TB)?", value.strip())
    if match is None:
        raise ValueError("size må skrives som for eksempel size<300KB eller size>2MB.")
    number = float(match.group(1).replace(",", "."))
    if number < 0:
        raise ValueError("size kan ikke være negativ.")
    unit = (match.group(2) or "B").upper()
    multiplier = {
        "B": 1,
        "KB": 1024,
        "MB": 1024**2,
        "GB": 1024**3,
        "TB": 1024**4,
    }[unit]
    return int(number * multiplier)


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
    manual_date_sql = (
        f"COALESCE(manual_date_from GLOB {db.DATE_GLOB_SQL}, 0) "
        f"AND COALESCE(manual_date_to GLOB {db.DATE_GLOB_SQL}, 0)"
    )
    if text_filter.after is not None or text_filter.before is not None:
        where.append(f"{db.BROWSER_DATE_ORDER_SQL} GLOB {db.DATE_GLOB_SQL}")
    if text_filter.after is not None:
        where.append(f"{db.BROWSER_DATE_ORDER_SQL} > ?")
        params.append(text_filter.after.isoformat())
    if text_filter.before is not None:
        where.append(f"{db.BROWSER_DATE_ORDER_SQL} < ?")
        params.append(text_filter.before.isoformat())
    if text_filter.size_gt is not None:
        where.append("size_bytes > ?")
        params.append(text_filter.size_gt)
    if text_filter.size_lt is not None:
        where.append("size_bytes < ?")
        params.append(text_filter.size_lt)
    if text_filter.date_source == "manual":
        where.append(f"({manual_date_sql})")
    elif text_filter.date_source in {"metadata", "filename", "mtime"}:
        where.append(f"NOT ({manual_date_sql})")
        where.append("date_source = ?")
        params.append(text_filter.date_source)
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
        <p class="meta">Eksempler: after:2023-12-01 before:2024-12-12, date:manual, date:metadata, date:filename, date:mtime, location:gps, location:manual, size&lt;300KB, size&gt;2MB.</p>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def filter_form(query: str) -> str:
    return f"""
    <form action="/filter" method="get" class="search">
      <input name="q" value="{html.escape(query)}" placeholder="date:manual size&gt;2MB" autofocus>
      <button type="submit">Søk</button>
    </form>
    """
