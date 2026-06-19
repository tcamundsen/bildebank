from __future__ import annotations

import datetime as dt
import re


def date_range_from_uncertainty(center: dt.date, value: str) -> tuple[dt.date, dt.date]:
    match = re.fullmatch(r"\s*(\d+)\s*([A-Za-zæøåÆØÅ]+)\s*", value)
    if match is None:
        raise ValueError("Ugyldig usikkerhet. Bruk for eksempel 3d, 2w, 1m eller 1y.")
    amount = int(match.group(1))
    if amount < 1:
        raise ValueError("Usikkerhet må være minst 1.")
    unit = match.group(2).lower()
    if unit in {"d", "day", "days", "dag", "dager"}:
        delta = dt.timedelta(days=amount)
        return center - delta, center + delta
    if unit in {"w", "week", "weeks", "uke", "uker"}:
        delta = dt.timedelta(weeks=amount)
        return center - delta, center + delta
    if unit in {"m", "month", "months", "måned", "måneder"}:
        return add_months(center, -amount), add_months(center, amount)
    if unit in {"y", "year", "years", "år"}:
        return add_months(center, -12 * amount), add_months(center, 12 * amount)
    raise ValueError("Ugyldig usikkerhetsenhet. Bruk d, w, m eller y.")


def add_months(value: dt.date, months: int) -> dt.date:
    month_index = value.year * 12 + value.month - 1 + months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, days_in_month(year, month))
    return dt.date(year, month, day)


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (dt.date(year, month + 1, 1) - dt.timedelta(days=1)).day
