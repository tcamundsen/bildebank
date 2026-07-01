from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from .html_paths import relative_to_target


UNKNOWN_BROWSER_DATE = "9999-99-99"
BROWSER_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
MONTH_PATH_RE = re.compile(r"(?:^|[\\/])(?P<year>\d{4})[\\/](?P<month>\d{2})(?:[\\/]|$)")


def item_value(item: Any, key: str, default: object = None) -> object:
    try:
        return item[key]
    except (KeyError, IndexError, TypeError):
        return default


def parse_iso_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def manual_date_midpoint(date_from: object, date_to: object) -> dt.date | None:
    start = parse_iso_date(str(date_from or ""))
    end = parse_iso_date(str(date_to or ""))
    if start is None or end is None:
        return None
    return start + (end - start) // 2


def manual_date_midpoint_for_item(item: Any) -> dt.date | None:
    return manual_date_midpoint(
        item_value(item, "manual_date_from"),
        item_value(item, "manual_date_to"),
    )


def browser_date_from_item(item: Any) -> str:
    manual_midpoint = manual_date_midpoint_for_item(item)
    if manual_midpoint is not None:
        return manual_midpoint.isoformat()
    taken_date = str(item_value(item, "taken_date", "") or "")
    if BROWSER_DATE_RE.match(taken_date):
        return taken_date[:10]
    return UNKNOWN_BROWSER_DATE


def browser_date_text(item: Any) -> str:
    manual_from = str(item_value(item, "manual_date_from", "") or "")
    manual_to = str(item_value(item, "manual_date_to", "") or "")
    if manual_from and manual_to:
        if manual_from == manual_to:
            return f"{manual_from} (manuell dato)"
        midpoint = manual_date_midpoint(manual_from, manual_to)
        if midpoint is not None:
            return f"ca. {midpoint.isoformat()} (manuell dato)"
    taken_date = str(item_value(item, "taken_date", "-") or "-")
    date_source = item_value(item, "date_source", "")
    return f"{taken_date} ({date_source})"


def month_key_from_browser_date_value(browser_date: str) -> str | None:
    if browser_date == UNKNOWN_BROWSER_DATE:
        return None
    if BROWSER_DATE_RE.fullmatch(browser_date):
        return browser_date[:7]
    return None


def valid_month_key(value: str) -> bool:
    if len(value) != 7 or value[4] != "-":
        return False
    year, month = value.split("-", 1)
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= 12


def next_month_key(month_key: str) -> str | None:
    if not valid_month_key(month_key):
        return None
    year, month = (int(part) for part in month_key.split("-", 1))
    if month == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{month + 1:02d}"


def valid_day_key(value: str) -> bool:
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def month_key_from_path(path: Path) -> str:
    parts = path.parts
    if len(parts) >= 3 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].isdigit():
        return f"{parts[0]}-{parts[1]}"
    if parts and parts[0] == "udatert":
        return "udatert"
    return "ukjent"


def month_key_from_stored_path(path: str) -> str | None:
    match = MONTH_PATH_RE.search(path.replace("\\\\", "\\"))
    if match is None:
        return None
    month_key = f"{match.group('year')}-{match.group('month')}"
    return month_key if valid_month_key(month_key) else None


def month_key_for_item(target: Path, item: Any) -> str:
    browser_date = browser_date_from_item(item)
    month_key = month_key_from_browser_date_value(browser_date)
    if month_key is not None:
        return month_key
    target_path = Path(str(item_value(item, "target_path", "")))
    stored_key = month_key_from_stored_path(str(target_path))
    return stored_key or month_key_from_path(relative_to_target(target, target_path))
