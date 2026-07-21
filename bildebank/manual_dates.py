from __future__ import annotations

import datetime as dt


_UNIT_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyzæøåÆØÅ")


def date_range_from_uncertainty(center: dt.date, value: str) -> tuple[dt.date, dt.date]:
    stripped_value = value.strip()
    amount_end = 0
    while amount_end < len(stripped_value) and stripped_value[amount_end].isdecimal():
        amount_end += 1

    unit = stripped_value[amount_end:].lstrip()
    if (
        amount_end == 0
        or not unit
        or any(character not in _UNIT_CHARACTERS for character in unit)
    ):
        raise ValueError("Ugyldig usikkerhet. Bruk for eksempel 3d, 2w, 1m eller 1y.")
    amount = int(stripped_value[:amount_end])
    if amount < 1:
        raise ValueError("Usikkerhet må være minst 1.")
    unit = unit.lower()
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
