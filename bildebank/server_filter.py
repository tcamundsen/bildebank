from __future__ import annotations

import datetime as dt
import html
import re
import shlex
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import db
from .media import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from .server_shell import message_html


ShellPageRenderer = Callable[..., str]


@dataclass(frozen=True)
class BrowserTextFilter:
    query: str
    after: dt.date | None = None
    before: dt.date | None = None
    camera: str | None = None
    date_source: str | None = None
    deleted: bool = False
    rotated: bool = False
    extension: str | None = None
    filename: str | None = None
    location: str | None = None
    location_place_slug: str | None = None
    location_place_cells: tuple[tuple[str, str], ...] = ()
    h3res_operator: str | None = None
    h3res_value: int | None = None
    media_type: str | None = None
    missing: str | None = None
    orientation: str | None = None
    path: str | None = None
    person: str | None = None
    size_gt: int | None = None
    size_gte: int | None = None
    size_lt: int | None = None
    size_lte: int | None = None
    source: str | None = None
    tag: str | None = None
    width_gt: int | None = None
    width_gte: int | None = None
    width_lt: int | None = None
    width_lte: int | None = None
    width_eq: int | None = None
    height_gt: int | None = None
    height_gte: int | None = None
    height_lt: int | None = None
    height_lte: int | None = None
    height_eq: int | None = None
    year: int | None = None
    year_eq: int | None = None
    year_gt: int | None = None
    year_gte: int | None = None
    year_lt: int | None = None
    year_lte: int | None = None
    month: int | None = None
    month_eq: int | None = None
    month_gt: int | None = None
    month_gte: int | None = None
    month_lt: int | None = None
    month_lte: int | None = None
    day: int | None = None
    day_eq: int | None = None
    day_gt: int | None = None
    day_gte: int | None = None
    day_lt: int | None = None
    day_lte: int | None = None


@dataclass
class NumericComparison:
    gt: int | None = None
    gte: int | None = None
    lt: int | None = None
    lte: int | None = None
    eq: int | None = None

    def set_once(self, operator: str, value: int, label: str) -> None:
        attr = COMPARISON_ATTRS[operator]
        conflict_operator = COMPARISON_CONFLICTS.get(operator)
        if conflict_operator is not None and getattr(self, COMPARISON_ATTRS[conflict_operator]) is not None:
            raise ValueError(f"{label}{operator} kan ikke kombineres med {label}{conflict_operator}.")
        if operator == "=" and self.has_range():
            raise ValueError(f"{label}= kan ikke kombineres med range-filter.")
        if operator != "=" and self.eq is not None:
            raise ValueError(f"{label}{operator} kan ikke kombineres med {label}=.")
        if getattr(self, attr) is not None:
            raise ValueError(f"{label}{operator} kan bare brukes én gang.")
        setattr(self, attr, value)

    def has_range(self) -> bool:
        return self.gt is not None or self.gte is not None or self.lt is not None or self.lte is not None


@dataclass
class FilterParseState:
    query: str
    after: dt.date | None = None
    before: dt.date | None = None
    camera: str | None = None
    date_source: str | None = None
    deleted: bool = False
    rotated: bool = False
    extension: str | None = None
    filename: str | None = None
    location: str | None = None
    h3res_operator: str | None = None
    h3res_value: int | None = None
    media_type: str | None = None
    missing: str | None = None
    orientation: str | None = None
    path: str | None = None
    person: str | None = None
    size: NumericComparison = field(default_factory=NumericComparison)
    source: str | None = None
    tag: str | None = None
    width: NumericComparison = field(default_factory=NumericComparison)
    height: NumericComparison = field(default_factory=NumericComparison)
    year: NumericComparison = field(default_factory=NumericComparison)
    month: NumericComparison = field(default_factory=NumericComparison)
    day: NumericComparison = field(default_factory=NumericComparison)

    def set_once(self, name: str, value: object, message_name: str | None = None) -> None:
        if getattr(self, name) is not None:
            raise ValueError(f"{message_name or name} kan bare brukes én gang.")
        setattr(self, name, value)

    def set_h3res_once(self, operator: str, value: int) -> None:
        if self.h3res_operator is not None:
            raise ValueError("h3res kan bare brukes én gang.")
        self.h3res_operator = operator
        self.h3res_value = value

    def validate(self) -> None:
        if self.h3res_operator is not None and self.location != "manual":
            raise ValueError("h3res kan bare brukes sammen med location:manual.")

    def to_filter(self) -> BrowserTextFilter:
        self.validate()
        return BrowserTextFilter(
            self.query,
            after=self.after,
            before=self.before,
            camera=self.camera,
            date_source=self.date_source,
            deleted=self.deleted,
            rotated=self.rotated,
            extension=self.extension,
            filename=self.filename,
            location=self.location,
            h3res_operator=self.h3res_operator,
            h3res_value=self.h3res_value,
            media_type=self.media_type,
            missing=self.missing,
            orientation=self.orientation,
            path=self.path,
            person=self.person,
            size_gt=self.size.gt,
            size_gte=self.size.gte,
            size_lt=self.size.lt,
            size_lte=self.size.lte,
            source=self.source,
            tag=self.tag,
            width_gt=self.width.gt,
            width_gte=self.width.gte,
            width_lt=self.width.lt,
            width_lte=self.width.lte,
            width_eq=self.width.eq,
            height_gt=self.height.gt,
            height_gte=self.height.gte,
            height_lt=self.height.lt,
            height_lte=self.height.lte,
            height_eq=self.height.eq,
            year=self.year.eq,
            year_eq=self.year.eq,
            year_gt=self.year.gt,
            year_gte=self.year.gte,
            year_lt=self.year.lt,
            year_lte=self.year.lte,
            month=self.month.eq,
            month_eq=self.month.eq,
            month_gt=self.month.gt,
            month_gte=self.month.gte,
            month_lt=self.month.lt,
            month_lte=self.month.lte,
            day=self.day.eq,
            day_eq=self.day.eq,
            day_gt=self.day.gt,
            day_gte=self.day.gte,
            day_lt=self.day.lt,
            day_lte=self.day.lte,
        )


@dataclass(frozen=True)
class OperatorFilterSpec:
    operators: tuple[str, ...]
    parse_value: Callable[[str], int]
    apply: Callable[[FilterParseState, str, int], None]


COMPARISON_ATTRS = {">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "=": "eq"}
COMPARISON_CONFLICTS = {">": ">=", ">=": ">", "<": "<=", "<=": "<"}


def parse_text_filter(query: str) -> BrowserTextFilter:
    tokens, clean_query = tokenize_filter_query(query)
    if not tokens:
        raise ValueError("Filtersøk kan ikke være tomt.")
    state = FilterParseState(clean_query)
    for token in tokens:
        if parse_operator_filter_token(state, token):
            continue
        parse_key_value_filter_token(state, token)
    return state.to_filter()


def parse_operator_filter_token(state: FilterParseState, token: str) -> bool:
    operator_filter = operator_filter_token(token)
    if operator_filter is None:
        return False
    key, operator, raw_value = operator_filter
    spec = OPERATOR_FILTERS[key]
    value = spec.parse_value(raw_value)
    spec.apply(state, operator, value)
    return True


def operator_filter_token(token: str) -> tuple[str, str, str] | None:
    for key, spec in OPERATOR_FILTERS.items():
        for operator in sorted(spec.operators, key=len, reverse=True):
            prefix = f"{key}{operator}"
            if token.startswith(prefix):
                if len(operator) == 1 and token.startswith(f"{prefix}="):
                    return None
                return key, operator, token.removeprefix(prefix)
    return None


def parse_key_value_filter_token(state: FilterParseState, token: str) -> None:
    if ":" not in token:
        raise ValueError(f"Ukjent filter: {token}")
    key, value = token.split(":", 1)
    key = key.strip().casefold()
    value = value.strip()
    if not value:
        raise ValueError(f"Filteret mangler verdi: {key}:")
    parser = KEY_VALUE_FILTERS.get(key)
    if parser is None:
        raise ValueError(f"Ukjent filter: {key}")
    parser(state, key, value)


def parse_date_filter(state: FilterParseState, key: str, value: str) -> None:
    state.set_once(key, parse_filter_date(value, key))


def parse_simple_text_filter(state: FilterParseState, key: str, value: str) -> None:
    state.set_once(key, value)


def parse_location_filter(state: FilterParseState, _key: str, value: str) -> None:
    state.set_once("location", value)


def parse_h3res_key_value_filter(state: FilterParseState, _key: str, value: str) -> None:
    state.set_h3res_once("=", parse_filter_integer_range(value, "h3res", 0, 11))


def parse_date_source_filter(state: FilterParseState, _key: str, value: str) -> None:
    if value not in {"manual", "metadata", "filename", "mtime"}:
        raise ValueError("date må være manual, metadata, filename eller mtime.")
    state.set_once("date_source", value, "date")


def parse_month_filter(state: FilterParseState, key: str, value: str) -> None:
    if state.month.eq is not None:
        raise ValueError("month kan bare brukes én gang.")
    state.month.set_once("=", parse_filter_integer_range(value, key, 1, 12), key)


def parse_year_filter(state: FilterParseState, key: str, value: str) -> None:
    if state.year.eq is not None:
        raise ValueError("year kan bare brukes én gang.")
    state.year.set_once("=", parse_filter_integer_range(value, key, 1, 9999), key)


def parse_day_filter(state: FilterParseState, key: str, value: str) -> None:
    if state.day.eq is not None:
        raise ValueError("day kan bare brukes én gang.")
    state.day.set_once("=", parse_filter_integer_range(value, key, 1, 31), key)


def parse_is_filter(state: FilterParseState, _key: str, value: str) -> None:
    if value == "deleted":
        if state.deleted:
            raise ValueError("is:deleted kan bare brukes én gang.")
        state.deleted = True
        return
    if value == "rotated":
        if state.rotated:
            raise ValueError("is:rotated kan bare brukes én gang.")
        state.rotated = True
        return
    raise ValueError(f"Ukjent is-filter: {value}. Gyldige verdier er deleted og rotated.")


def parse_extension_filter(state: FilterParseState, key: str, value: str) -> None:
    state.set_once(key, parse_extension(value))


def parse_missing_filter(state: FilterParseState, key: str, value: str) -> None:
    if value not in {"gps", "date", "metadata"}:
        raise ValueError("missing må være gps, date eller metadata.")
    state.set_once(key, value)


def parse_orientation_filter(state: FilterParseState, key: str, value: str) -> None:
    if value not in {"portrait", "landscape"}:
        raise ValueError("orientation må være portrait eller landscape.")
    state.set_once(key, value)


def parse_type_filter(state: FilterParseState, _key: str, value: str) -> None:
    if value not in {"image", "video", "file"}:
        raise ValueError("type må være image, video eller file.")
    state.set_once("media_type", value, "type")


def apply_size_operator(state: FilterParseState, operator: str, value: int) -> None:
    state.size.set_once(operator, value, "size")


def apply_h3res_operator(state: FilterParseState, operator: str, value: int) -> None:
    state.set_h3res_once(operator, value)


def apply_width_operator(state: FilterParseState, operator: str, value: int) -> None:
    state.width.set_once(operator, value, "width")


def apply_height_operator(state: FilterParseState, operator: str, value: int) -> None:
    state.height.set_once(operator, value, "height")


def apply_month_operator(state: FilterParseState, operator: str, value: int) -> None:
    state.month.set_once(operator, value, "month")


def apply_year_operator(state: FilterParseState, operator: str, value: int) -> None:
    state.year.set_once(operator, value, "year")


def apply_day_operator(state: FilterParseState, operator: str, value: int) -> None:
    state.day.set_once(operator, value, "day")


def parse_h3res_integer(value: str) -> int:
    return parse_filter_integer_range(value, "h3res", 0, 11)


def parse_month_integer(value: str) -> int:
    return parse_filter_integer_range(value, "month", 1, 12)


def parse_year_integer(value: str) -> int:
    return parse_filter_integer_range(value, "year", 1, 9999)


def parse_day_integer(value: str) -> int:
    return parse_filter_integer_range(value, "day", 1, 31)


def parse_width_pixels(value: str) -> int:
    return parse_dimension_pixels("width", value)


def parse_height_pixels(value: str) -> int:
    return parse_dimension_pixels("height", value)


def tokenize_filter_query(query: str) -> tuple[list[str], str]:
    try:
        raw_tokens = shlex.split(query.strip())
    except ValueError as exc:
        raise ValueError("Ugyldige anførselstegn i filtersøk.") from exc
    tokens = normalize_filter_tokens(raw_tokens)
    return tokens, " ".join(canonical_filter_token(token) for token in tokens)


def normalize_filter_tokens(tokens: list[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        normalized_token: str | None = None
        consumed = 1

        for key, spec in OPERATOR_FILTERS.items():
            operators = sorted(spec.operators, key=len, reverse=True)
            if token == key and index + 1 < len(tokens):
                next_token = tokens[index + 1]
                for operator in operators:
                    if not next_token.startswith(operator):
                        continue
                    raw_value = next_token.removeprefix(operator)
                    if raw_value:
                        normalized_token = f"{key}{operator}{raw_value}"
                        consumed = 2
                        break
                    if index + 2 >= len(tokens):
                        raise ValueError(f"Filteret mangler verdi: {key}{operator}")
                    normalized_token = f"{key}{operator}{tokens[index + 2]}"
                    consumed = 3
                    break
            else:
                for operator in operators:
                    prefix = f"{key}{operator}"
                    if not token.startswith(prefix):
                        continue
                    if token.removeprefix(prefix):
                        break
                    if index + 1 >= len(tokens):
                        raise ValueError(f"Filteret mangler verdi: {prefix}")
                    normalized_token = f"{token}{tokens[index + 1]}"
                    consumed = 2
                    break

            if normalized_token is not None:
                break

        normalized.append(normalized_token or token)
        index += consumed
    return normalized


def canonical_filter_token(token: str) -> str:
    if not any(char.isspace() for char in token):
        return token
    if ":" not in token:
        return token
    key, value = token.split(":", 1)
    escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}:"{escaped_value}"'


def parse_filter_date(value: str, key: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{key} må være en dato på formen YYYY-MM-DD.") from exc


def parse_filter_integer_range(value: str, key: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"{key} må være et heltall fra {minimum} til {maximum}.") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{key} må være et heltall fra {minimum} til {maximum}.")
    return number


def parse_extension(value: str) -> str:
    clean_value = value.strip().lower().removeprefix(".")
    if not clean_value or not re.fullmatch(r"[a-z0-9]+", clean_value):
        raise ValueError("extension må være en filendelse, for eksempel jpg eller mp4.")
    return clean_value


def parse_dimension_pixels(key: str, value: str) -> int:
    if not re.fullmatch(r"\d+", value.strip()):
        raise ValueError(
            f"{key} må være et heltall i piksler uten enhet, "
            "for eksempel width>1024, width=1024 eller height<2000."
        )
    return int(value)


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


KEY_VALUE_FILTERS: dict[str, Callable[[FilterParseState, str, str], None]] = {
    "after": parse_date_filter,
    "before": parse_date_filter,
    "camera": parse_simple_text_filter,
    "location": parse_location_filter,
    "h3res": parse_h3res_key_value_filter,
    "date": parse_date_source_filter,
    "year": parse_year_filter,
    "month": parse_month_filter,
    "day": parse_day_filter,
    "is": parse_is_filter,
    "extension": parse_extension_filter,
    "filename": parse_simple_text_filter,
    "missing": parse_missing_filter,
    "orientation": parse_orientation_filter,
    "path": parse_simple_text_filter,
    "person": parse_simple_text_filter,
    "source": parse_simple_text_filter,
    "tag": parse_simple_text_filter,
    "type": parse_type_filter,
}


OPERATOR_FILTERS: dict[str, OperatorFilterSpec] = {
    "size": OperatorFilterSpec((">=", "<=", ">", "<"), parse_size_bytes, apply_size_operator),
    "h3res": OperatorFilterSpec((">=", "<=", ">", "<", "="), parse_h3res_integer, apply_h3res_operator),
    "width": OperatorFilterSpec((">=", "<=", ">", "<", "="), parse_width_pixels, apply_width_operator),
    "height": OperatorFilterSpec((">=", "<=", ">", "<", "="), parse_height_pixels, apply_height_operator),
    "year": OperatorFilterSpec((">=", "<=", ">", "<", "="), parse_year_integer, apply_year_operator),
    "month": OperatorFilterSpec((">=", "<=", ">", "<", "="), parse_month_integer, apply_month_operator),
    "day": OperatorFilterSpec((">=", "<=", ">", "<", "="), parse_day_integer, apply_day_operator),
}


def text_filter_url(query: str) -> str:
    return "/filter/" + urllib.parse.quote(parse_text_filter(query).query, safe="")


def text_filter_browser_source(query: str, target: Path | None = None) -> Any:
    from .server_browser_sources import BrowserSource

    text_filter = parse_text_filter(query)
    if text_filter.location not in {None, "gps", "manual"}:
        text_filter = resolve_location_place(text_filter, target)
    return BrowserSource(
        f"Filtersøk: {text_filter.query}",
        text_filter_url(text_filter.query),
        text_filter=text_filter,
    )


def resolve_location_place(text_filter: BrowserTextFilter, target: Path | None) -> BrowserTextFilter:
    slug = str(text_filter.location or "").strip().lower()
    from .geo import predefined_geo_place
    from .server_geo import geo_place_by_slug, geo_place_cells_by_column

    place = geo_place_by_slug(target, slug) if target is not None else predefined_geo_place(slug)
    if place is None:
        raise ValueError(f"Ukjent sted: {slug}")
    return BrowserTextFilter(
        text_filter.query,
        after=text_filter.after,
        before=text_filter.before,
        camera=text_filter.camera,
        date_source=text_filter.date_source,
        deleted=text_filter.deleted,
        rotated=text_filter.rotated,
        extension=text_filter.extension,
        filename=text_filter.filename,
        location=text_filter.location,
        location_place_slug=place.slug,
        location_place_cells=tuple(geo_place_cells_by_column(place)),
        h3res_operator=text_filter.h3res_operator,
        h3res_value=text_filter.h3res_value,
        media_type=text_filter.media_type,
        missing=text_filter.missing,
        orientation=text_filter.orientation,
        path=text_filter.path,
        person=text_filter.person,
        size_gt=text_filter.size_gt,
        size_gte=text_filter.size_gte,
        size_lt=text_filter.size_lt,
        size_lte=text_filter.size_lte,
        source=text_filter.source,
        tag=text_filter.tag,
        width_gt=text_filter.width_gt,
        width_gte=text_filter.width_gte,
        width_lt=text_filter.width_lt,
        width_lte=text_filter.width_lte,
        width_eq=text_filter.width_eq,
        height_gt=text_filter.height_gt,
        height_gte=text_filter.height_gte,
        height_lt=text_filter.height_lt,
        height_lte=text_filter.height_lte,
        height_eq=text_filter.height_eq,
        year=text_filter.year,
        year_eq=text_filter.year_eq,
        year_gt=text_filter.year_gt,
        year_gte=text_filter.year_gte,
        year_lt=text_filter.year_lt,
        year_lte=text_filter.year_lte,
        month=text_filter.month,
        month_eq=text_filter.month_eq,
        month_gt=text_filter.month_gt,
        month_gte=text_filter.month_gte,
        month_lt=text_filter.month_lt,
        month_lte=text_filter.month_lte,
        day=text_filter.day,
        day_eq=text_filter.day_eq,
        day_gt=text_filter.day_gt,
        day_gte=text_filter.day_gte,
        day_lt=text_filter.day_lt,
        day_lte=text_filter.day_lte,
    )


def text_filter_has_date_criteria(
    text_filter: BrowserTextFilter,
    year_eq: int | None,
    month_eq: int | None,
    day_eq: int | None,
) -> bool:
    return any(
        value is not None
        for value in (
            text_filter.after,
            text_filter.before,
            year_eq,
            text_filter.year_gt,
            text_filter.year_gte,
            text_filter.year_lt,
            text_filter.year_lte,
            month_eq,
            text_filter.month_gt,
            text_filter.month_gte,
            text_filter.month_lt,
            text_filter.month_lte,
            day_eq,
            text_filter.day_gt,
            text_filter.day_gte,
            text_filter.day_lt,
            text_filter.day_lte,
        )
    )


def text_filter_where_clause(text_filter: BrowserTextFilter) -> tuple[str, tuple[object, ...]]:
    where: list[str] = []
    params: list[object] = []
    year_eq = text_filter.year_eq if text_filter.year_eq is not None else text_filter.year
    month_eq = text_filter.month_eq if text_filter.month_eq is not None else text_filter.month
    day_eq = text_filter.day_eq if text_filter.day_eq is not None else text_filter.day
    manual_date_sql = (
        f"COALESCE(manual_date_from GLOB {db.DATE_GLOB_SQL}, 0) "
        f"AND COALESCE(manual_date_to GLOB {db.DATE_GLOB_SQL}, 0)"
    )
    taken_date_sql = f"COALESCE(taken_date GLOB {db.DATE_GLOB_SQL}, 0)"
    browser_has_date_sql = f"(({manual_date_sql}) OR {taken_date_sql})"
    if text_filter.deleted:
        where.append("deleted_at IS NOT NULL")
    if text_filter.rotated:
        where.append("view_rotation_degrees IN (90, 180, 270)")
    if text_filter_has_date_criteria(text_filter, year_eq, month_eq, day_eq):
        where.append(browser_has_date_sql)
    if text_filter.after is not None:
        where.append(f"{db.BROWSER_DATE_ORDER_SQL} > ?")
        params.append(text_filter.after.isoformat())
    if text_filter.before is not None:
        where.append(f"{db.BROWSER_DATE_ORDER_SQL} < ?")
        params.append(text_filter.before.isoformat())
    add_date_part_conditions(
        where,
        params,
        date_part_sql(1, 4),
        eq=year_eq,
        gt=text_filter.year_gt,
        gte=text_filter.year_gte,
        lt=text_filter.year_lt,
        lte=text_filter.year_lte,
    )
    add_date_part_conditions(
        where,
        params,
        date_part_sql(6),
        eq=month_eq,
        gt=text_filter.month_gt,
        gte=text_filter.month_gte,
        lt=text_filter.month_lt,
        lte=text_filter.month_lte,
    )
    add_date_part_conditions(
        where,
        params,
        date_part_sql(9),
        eq=day_eq,
        gt=text_filter.day_gt,
        gte=text_filter.day_gte,
        lt=text_filter.day_lt,
        lte=text_filter.day_lte,
    )
    add_numeric_conditions(
        where,
        params,
        "size_bytes",
        gt=text_filter.size_gt,
        gte=text_filter.size_gte,
        lt=text_filter.size_lt,
        lte=text_filter.size_lte,
    )
    add_numeric_conditions(
        where,
        params,
        "media_width",
        gt=text_filter.width_gt,
        gte=text_filter.width_gte,
        lt=text_filter.width_lt,
        lte=text_filter.width_lte,
        eq=text_filter.width_eq,
    )
    add_numeric_conditions(
        where,
        params,
        "media_height",
        gt=text_filter.height_gt,
        gte=text_filter.height_gte,
        lt=text_filter.height_lt,
        lte=text_filter.height_lte,
        eq=text_filter.height_eq,
    )
    if text_filter.date_source == "manual":
        where.append(f"({manual_date_sql})")
    elif text_filter.date_source in {"metadata", "filename", "mtime"}:
        where.append(f"NOT ({manual_date_sql})")
        where.append("date_source = ?")
        params.append(text_filter.date_source)
    if text_filter.camera is not None:
        where.append("lower(coalesce(camera_make, '') || ' ' || coalesce(camera_model, '')) LIKE ?")
        params.append(like_contains_param(text_filter.camera))
    if text_filter.extension is not None:
        where.append("lower(stored_filename) LIKE ?")
        params.append(f"%.{text_filter.extension}")
    if text_filter.filename is not None:
        where.append("lower(stored_filename) LIKE ?")
        params.append(like_contains_param(text_filter.filename))
    if text_filter.media_type in {"image", "video"}:
        where.append(extension_condition_for_type(text_filter.media_type))
        params.extend(extension_params_for_type(text_filter.media_type))
    elif text_filter.media_type == "file":
        where.append(f"NOT {extension_condition_for_type('image')}")
        params.extend(extension_params_for_type("image"))
        where.append(f"NOT {extension_condition_for_type('video')}")
        params.extend(extension_params_for_type("video"))
    if text_filter.missing == "gps":
        where.append("gps_lat IS NULL")
        where.append("gps_lon IS NULL")
        where.append("(gps_source IS NULL OR gps_source != 'manual-h3')")
    elif text_filter.missing == "date":
        where.append(f"NOT {browser_has_date_sql}")
    elif text_filter.missing == "metadata":
        where.append("date_source != 'metadata'")
    if text_filter.orientation == "portrait":
        where.append("media_width IS NOT NULL AND media_height IS NOT NULL AND media_height > media_width")
    elif text_filter.orientation == "landscape":
        where.append("media_width IS NOT NULL AND media_height IS NOT NULL AND media_width > media_height")
    if text_filter.path is not None:
        where.append("lower(target_path) LIKE ?")
        params.append(like_contains_param(text_filter.path.replace("\\", "/")))
    if text_filter.person is not None:
        where.append(person_where_clause())
        params.append(text_filter.person.strip().lower())
    if text_filter.source is not None:
        source_filter, source_params = source_where_clause(text_filter.source)
        where.append(source_filter)
        params.extend(source_params)
    if text_filter.tag is not None:
        where.append(tag_where_clause())
        params.extend(tag_params(text_filter.tag))
    if text_filter.location == "gps":
        where.append("gps_lat IS NOT NULL")
        where.append("gps_lon IS NOT NULL")
        where.append("(gps_source IS NULL OR gps_source != 'manual-h3')")
    elif text_filter.location == "manual":
        where.append("gps_source = 'manual-h3'")
    elif text_filter.location_place_cells:
        place_where, place_params = db.geo_place_where_clause(list(text_filter.location_place_cells))
        where.append(f"({place_where})")
        params.extend(place_params)
    if text_filter.h3res_operator is not None and text_filter.h3res_value is not None:
        add_numeric_condition(
            where,
            params,
            manual_h3_resolution_sql(),
            text_filter.h3res_operator,
            text_filter.h3res_value,
        )
    if not where:
        raise ValueError("Filtersøket må ha minst ett kriterium.")
    return " AND ".join(where), tuple(params)


def date_part_sql(start: int, length: int = 2) -> str:
    return f"CAST(substr({db.BROWSER_DATE_ORDER_SQL}, {start}, {length}) AS INTEGER)"


def add_date_part_conditions(
    where: list[str],
    params: list[object],
    sql_expression: str,
    *,
    eq: int | None = None,
    gt: int | None = None,
    gte: int | None = None,
    lt: int | None = None,
    lte: int | None = None,
) -> None:
    add_numeric_conditions(where, params, sql_expression, gt=gt, gte=gte, lt=lt, lte=lte, eq=eq)


def add_numeric_conditions(
    where: list[str],
    params: list[object],
    sql_expression: str,
    *,
    gt: int | None = None,
    gte: int | None = None,
    lt: int | None = None,
    lte: int | None = None,
    eq: int | None = None,
) -> None:
    for operator, value in ((">", gt), (">=", gte), ("<", lt), ("<=", lte), ("=", eq)):
        if value is not None:
            add_numeric_condition(where, params, sql_expression, operator, value)


def add_numeric_condition(
    where: list[str],
    params: list[object],
    sql_expression: str,
    operator: str,
    value: int,
) -> None:
    where.append(f"{sql_expression} {operator} ?")
    params.append(value)


def manual_h3_resolution_sql() -> str:
    parts = [f"WHEN h3_res{resolution} IS NOT NULL THEN {resolution}" for resolution in range(11, -1, -1)]
    return "CASE " + " ".join(parts) + " END"


def extension_condition_for_type(media_type: str) -> str:
    extensions = IMAGE_EXTENSIONS if media_type == "image" else VIDEO_EXTENSIONS
    return "(" + " OR ".join("lower(stored_filename) LIKE ?" for _ext in extensions) + ")"


def extension_params_for_type(media_type: str) -> tuple[str, ...]:
    extensions = IMAGE_EXTENSIONS if media_type == "image" else VIDEO_EXTENSIONS
    return tuple(f"%{extension}" for extension in sorted(extensions))


def source_where_clause(value: str) -> tuple[str, tuple[object, ...]]:
    clean_value = value.strip()
    if clean_value.isdigit():
        return (
            "EXISTS (SELECT 1 FROM file_sources WHERE file_sources.file_id = files.id AND file_sources.source_id = ?)",
            (int(clean_value),),
        )
    return (
        """
        EXISTS (
            SELECT 1
            FROM file_sources
            JOIN sources ON sources.id = file_sources.source_id
            WHERE file_sources.file_id = files.id
              AND lower(sources.name) LIKE ?
        )
        """,
        (like_contains_param(clean_value),),
    )


def tag_where_clause() -> str:
    return """
    EXISTS (
        SELECT 1
        FROM file_tags
        JOIN tags ON tags.id = file_tags.tag_id
        WHERE file_tags.file_id = files.id
          AND tags.name_key IN (?, ?)
    )
    """


def tag_params(value: str) -> tuple[str, str]:
    clean_value = value.strip().casefold()
    return clean_value, re.sub(r"[-_]+", " ", clean_value)


def person_where_clause() -> str:
    return """
    EXISTS (
        SELECT 1
        FROM face_db.persons
        JOIN (
            SELECT person_id, face_id FROM face_db.person_faces
            UNION ALL
            SELECT person_id, face_id FROM face_db.face_suggestions
        ) person_matches ON person_matches.person_id = face_db.persons.id
        JOIN face_db.faces ON face_db.faces.id = person_matches.face_id
        WHERE face_db.faces.file_id = files.id
          AND lower(face_db.persons.name) = ?
    )
    """


def like_contains_param(value: str) -> str:
    return f"%{value.strip().lower()}%"


def text_filter_has_runtime_filter(text_filter: BrowserTextFilter) -> bool:
    return False


def text_filter_shows_motion_videos(text_filter: BrowserTextFilter) -> bool:
    return (
        text_filter.media_type == "video"
        or text_filter.extension is not None
        or text_filter.filename is not None
    )


def attach_text_filter_databases(conn: Any, target: Path, text_filter: BrowserTextFilter) -> None:
    if text_filter.person is None:
        return
    from .face import connect_face_db, face_db_path

    if any(str(row["name"]) == "face_db" for row in conn.execute("PRAGMA database_list")):
        return
    face_conn = connect_face_db(target)
    face_conn.close()
    conn.execute("ATTACH DATABASE ? AS face_db", (str(face_db_path(target)),))


def text_filter_items(target: Path, text_filter: BrowserTextFilter, *, hide_out_of_focus: bool = False) -> list[Any]:
    from .server_browser import (
        FILE_COLUMNS,
        ITEM_ORDER_SQL,
        OUT_OF_FOCUS_FILTER_PARAMS,
        OUT_OF_FOCUS_FILTER_SQL,
        with_motion_video_filter,
    )

    where_sql, params = text_filter_where_clause(text_filter)
    where_sql, params = with_motion_video_filter(
        target,
        where_sql,
        params,
        include_motion=text_filter_shows_motion_videos(text_filter),
    )
    deleted_sql = "1 = 1" if text_filter.deleted else "deleted_at IS NULL"
    focus_sql = f"AND {OUT_OF_FOCUS_FILTER_SQL}" if hide_out_of_focus else ""
    focus_params = OUT_OF_FOCUS_FILTER_PARAMS if hide_out_of_focus else ()
    conn = db.connect(target)
    try:
        attach_text_filter_databases(conn, target, text_filter)
        return list(
            conn.execute(
                f"""
                SELECT {FILE_COLUMNS}
                FROM files
                WHERE {deleted_sql}
                  AND ({where_sql})
                  {focus_sql}
                ORDER BY {ITEM_ORDER_SQL}
                """,
                (*params, *focus_params),
            )
        )
    finally:
        conn.close()


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
        {filter_help_html()}
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


def filter_help_html() -> str:
    return """
    <section class="filter-help">
      <h2>Søkekriterier</h2>
      <p>Komplett referanse over søkekriterier <a href="/help/web/filtersok.md">finner du her</a>.
      <h3>Eksempelsøk</h3>
      <dl class="info-list">
        <div class="info-row">
          <dt>Bilder fra 2024</dt>
          <dd><a href="/filter/year:2024"><code>year:2024</code></a>></dd>
        </div>
        <div class="info-row">
          <dt>Alle julaftener</dt>
          <dd><a href="/filter/month:12 day:24"><code>month:12 day:24</code></a></dd>
        </div>
        <div class="info-row">
          <dt>Adventstiden</dt>
          <dd><a href="/filter/month:12 day>=1 day<=25"><code>month:12 day>=1 day<=25</code></a></dd>
        </div>
        <div class="info-row">
          <dt>Sommerbilder</dt>
          <dd><a href="/filter/month>=6 month<=8"><code>month>=6 month<=8</code></a></dd>
        </div>
        <div class="info-row">
          <dt>Bilder fra juli på Kreta</dt>
          <dd><a href="/filter/month:7 location:kreta"><code>month:7 location:kreta</code></a></dd>
        </div>
        <div class="info-row">
          <dt>Bilder uten GPS</dt>
          <dd><a href="/filter/missing:gps"><code>missing:gps</code></a></dd>
        </div>
        <div class="info-row">
          <dt> Manuelt plasserte bilder</dt>
          <dd><a href="/filter/location:manual"><code>location:manual</code></a></dd>
        </div>
        <div class="info-row">
          <dt>Detaljert manuell plassering</dt>
          <dd><a href="/filter/location:manual h3res>=9"><code>location:manual h3res>=9</code></a></dd>
        </div>
        <div class="info-row">
          <dt>Store bilder</dt>
          <dd><a href="/filter/width>=3000 height>=2000"><code>width>=3000 height>=2000</code></a></dd>
        </div>
        <div class="info-row">
          <dt>Små filer</dt>
          <dd><a href="/filter/size<300KB"><code>size<300KB</code></a></dd>
        </div>
        <div class="info-row">
          <dt>Bilder med tagg</dt>
          <dd><a href='/filter/tag:"Ute av fokus"'><code>tag:"Ute av fokus"</code></a></dd>
        </div>
        <div class="info-row">
          <dt>Et bestemt tidsrom</dt>
          <dd><a href="/filter/after:2023-12-01 before:2023-12-23"><code>after:2023-12-01 before:2023-12-23</code></a></dd>
        </div>
      </dl>
    </section>
    """
