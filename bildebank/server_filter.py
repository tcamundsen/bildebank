from __future__ import annotations

import datetime as dt
import html
import re
import shlex
import urllib.parse
from dataclasses import dataclass
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
    extension: str | None = None
    filename: str | None = None
    location: str | None = None
    location_place_slug: str | None = None
    location_place_cells: tuple[tuple[str, str], ...] = ()
    media_type: str | None = None
    missing: str | None = None
    orientation: str | None = None
    path: str | None = None
    person: str | None = None
    size_gt: int | None = None
    size_lt: int | None = None
    source: str | None = None
    tag: str | None = None
    width_gt: int | None = None
    width_lt: int | None = None
    width_eq: int | None = None
    height_gt: int | None = None
    height_lt: int | None = None
    height_eq: int | None = None
    month: int | None = None
    day: int | None = None


def parse_text_filter(query: str) -> BrowserTextFilter:
    tokens, clean_query = tokenize_filter_query(query)
    if not tokens:
        raise ValueError("Filtersøk kan ikke være tomt.")
    after = None
    before = None
    camera = None
    date_source = None
    deleted = False
    extension = None
    filename = None
    location = None
    media_type = None
    missing = None
    orientation = None
    path = None
    person = None
    size_gt = None
    size_lt = None
    source = None
    tag = None
    width_gt = None
    width_lt = None
    width_eq = None
    height_gt = None
    height_lt = None
    height_eq = None
    month = None
    day = None
    for token in tokens:
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
        dimension_operator = dimension_filter_operator(token)
        if dimension_operator is not None:
            key, operator, value = dimension_operator
            pixels = parse_dimension_pixels(key, value)
            if key == "width" and operator == ">":
                if width_gt is not None:
                    raise ValueError("width> kan bare brukes én gang.")
                width_gt = pixels
            elif key == "width" and operator == "<":
                if width_lt is not None:
                    raise ValueError("width< kan bare brukes én gang.")
                width_lt = pixels
            elif key == "width" and operator == "=":
                if width_eq is not None:
                    raise ValueError("width= kan bare brukes én gang.")
                width_eq = pixels
            elif key == "height" and operator == ">":
                if height_gt is not None:
                    raise ValueError("height> kan bare brukes én gang.")
                height_gt = pixels
            elif key == "height" and operator == "<":
                if height_lt is not None:
                    raise ValueError("height< kan bare brukes én gang.")
                height_lt = pixels
            elif key == "height" and operator == "=":
                if height_eq is not None:
                    raise ValueError("height= kan bare brukes én gang.")
                height_eq = pixels
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
        elif key == "camera":
            if camera is not None:
                raise ValueError("camera kan bare brukes én gang.")
            camera = value
        elif key == "location":
            if location is not None:
                raise ValueError("location kan bare brukes én gang.")
            location = value
        elif key == "date":
            if date_source is not None:
                raise ValueError("date kan bare brukes én gang.")
            if value not in {"manual", "metadata", "filename", "mtime"}:
                raise ValueError("date må være manual, metadata, filename eller mtime.")
            date_source = value
        elif key == "month":
            if month is not None:
                raise ValueError("month kan bare brukes én gang.")
            month = parse_filter_integer_range(value, "month", 1, 12)
        elif key == "day":
            if day is not None:
                raise ValueError("day kan bare brukes én gang.")
            day = parse_filter_integer_range(value, "day", 1, 31)
        elif key == "deleted":
            if value not in {"true", "false"}:
                raise ValueError("deleted må være true eller false.")
            deleted = value == "true"
        elif key == "extension":
            if extension is not None:
                raise ValueError("extension kan bare brukes én gang.")
            extension = parse_extension(value)
        elif key == "filename":
            if filename is not None:
                raise ValueError("filename kan bare brukes én gang.")
            filename = value
        elif key == "missing":
            if missing is not None:
                raise ValueError("missing kan bare brukes én gang.")
            if value not in {"gps", "date", "metadata"}:
                raise ValueError("missing må være gps, date eller metadata.")
            missing = value
        elif key == "orientation":
            if orientation is not None:
                raise ValueError("orientation kan bare brukes én gang.")
            if value not in {"portrait", "landscape"}:
                raise ValueError("orientation må være portrait eller landscape.")
            orientation = value
        elif key == "path":
            if path is not None:
                raise ValueError("path kan bare brukes én gang.")
            path = value
        elif key == "person":
            if person is not None:
                raise ValueError("person kan bare brukes én gang.")
            person = value
        elif key == "source":
            if source is not None:
                raise ValueError("source kan bare brukes én gang.")
            source = value
        elif key == "tag":
            if tag is not None:
                raise ValueError("tag kan bare brukes én gang.")
            tag = value
        elif key == "type":
            if media_type is not None:
                raise ValueError("type kan bare brukes én gang.")
            if value not in {"image", "video", "file"}:
                raise ValueError("type må være image, video eller file.")
            media_type = value
        else:
            raise ValueError(f"Ukjent filter: {key}")
    return BrowserTextFilter(
        clean_query,
        after=after,
        before=before,
        camera=camera,
        date_source=date_source,
        deleted=deleted,
        extension=extension,
        filename=filename,
        location=location,
        media_type=media_type,
        missing=missing,
        orientation=orientation,
        path=path,
        person=person,
        size_gt=size_gt,
        size_lt=size_lt,
        source=source,
        tag=tag,
        width_gt=width_gt,
        width_lt=width_lt,
        width_eq=width_eq,
        height_gt=height_gt,
        height_lt=height_lt,
        height_eq=height_eq,
        month=month,
        day=day,
    )


def tokenize_filter_query(query: str) -> tuple[list[str], str]:
    try:
        tokens = shlex.split(query.strip())
    except ValueError as exc:
        raise ValueError("Ugyldige anførselstegn i filtersøk.") from exc
    return tokens, " ".join(canonical_filter_token(token) for token in tokens)


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


def size_filter_operator(token: str) -> tuple[str, str] | None:
    if token.startswith("size>"):
        return ">", token.removeprefix("size>")
    if token.startswith("size<"):
        return "<", token.removeprefix("size<")
    return None


def dimension_filter_operator(token: str) -> tuple[str, str, str] | None:
    for key in ("width", "height"):
        for operator in (">", "<", "="):
            prefix = f"{key}{operator}"
            if token.startswith(prefix):
                return key, operator, token.removeprefix(prefix)
    return None


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
        extension=text_filter.extension,
        filename=text_filter.filename,
        location=text_filter.location,
        location_place_slug=place.slug,
        location_place_cells=tuple(geo_place_cells_by_column(place)),
        media_type=text_filter.media_type,
        missing=text_filter.missing,
        orientation=text_filter.orientation,
        path=text_filter.path,
        person=text_filter.person,
        size_gt=text_filter.size_gt,
        size_lt=text_filter.size_lt,
        source=text_filter.source,
        tag=text_filter.tag,
        width_gt=text_filter.width_gt,
        width_lt=text_filter.width_lt,
        width_eq=text_filter.width_eq,
        height_gt=text_filter.height_gt,
        height_lt=text_filter.height_lt,
        height_eq=text_filter.height_eq,
        month=text_filter.month,
        day=text_filter.day,
    )


def text_filter_where_clause(text_filter: BrowserTextFilter) -> tuple[str, tuple[object, ...]]:
    where: list[str] = []
    params: list[object] = []
    manual_date_sql = (
        f"COALESCE(manual_date_from GLOB {db.DATE_GLOB_SQL}, 0) "
        f"AND COALESCE(manual_date_to GLOB {db.DATE_GLOB_SQL}, 0)"
    )
    taken_date_sql = f"COALESCE(taken_date GLOB {db.DATE_GLOB_SQL}, 0)"
    browser_has_date_sql = f"(({manual_date_sql}) OR {taken_date_sql})"
    if text_filter.deleted:
        where.append("deleted_at IS NOT NULL")
    if (
        text_filter.after is not None
        or text_filter.before is not None
        or text_filter.month is not None
        or text_filter.day is not None
    ):
        where.append(browser_has_date_sql)
    if text_filter.after is not None:
        where.append(f"{db.BROWSER_DATE_ORDER_SQL} > ?")
        params.append(text_filter.after.isoformat())
    if text_filter.before is not None:
        where.append(f"{db.BROWSER_DATE_ORDER_SQL} < ?")
        params.append(text_filter.before.isoformat())
    if text_filter.month is not None:
        where.append(f"CAST(substr({db.BROWSER_DATE_ORDER_SQL}, 6, 2) AS INTEGER) = ?")
        params.append(text_filter.month)
    if text_filter.day is not None:
        where.append(f"CAST(substr({db.BROWSER_DATE_ORDER_SQL}, 9, 2) AS INTEGER) = ?")
        params.append(text_filter.day)
    if text_filter.size_gt is not None:
        where.append("size_bytes > ?")
        params.append(text_filter.size_gt)
    if text_filter.size_lt is not None:
        where.append("size_bytes < ?")
        params.append(text_filter.size_lt)
    if text_filter.width_gt is not None:
        where.append("media_width > ?")
        params.append(text_filter.width_gt)
    if text_filter.width_lt is not None:
        where.append("media_width < ?")
        params.append(text_filter.width_lt)
    if text_filter.width_eq is not None:
        where.append("media_width = ?")
        params.append(text_filter.width_eq)
    if text_filter.height_gt is not None:
        where.append("media_height > ?")
        params.append(text_filter.height_gt)
    if text_filter.height_lt is not None:
        where.append("media_height < ?")
        params.append(text_filter.height_lt)
    if text_filter.height_eq is not None:
        where.append("media_height = ?")
        params.append(text_filter.height_eq)
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
    if not where:
        raise ValueError("Filtersøket må ha minst ett kriterium.")
    return " AND ".join(where), tuple(params)


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
      <p class="meta">Kriterier kan kombineres. Bruk anførselstegn når tekst inneholder mellomrom, for eksempel camera:"iPhone" eller tag:"Ute av fokus".</p>
      <dl class="info-list">
        <div class="info-row"><dt>Dato</dt><dd><code>after:2023-12-01</code>, <code>before:2024-12-12</code>, <code>month:12</code>, <code>day:24</code>, <code>month:12 day:24</code>, <code>date:manual</code>, <code>date:metadata</code>, <code>date:filename</code>, <code>date:mtime</code></dd></div>
        <div class="info-row"><dt>Sted</dt><dd><code>location:gps</code>, <code>location:manual</code>, <code>location:slug</code>. Slug er samme tekst som i <code>/geo/place/slug</code>.</dd></div>
        <div class="info-row"><dt>Fil</dt><dd><code>type:image</code>, <code>type:video</code>, <code>type:file</code>, <code>extension:jpg</code>, <code>size&lt;300KB</code>, <code>size&gt;2MB</code></dd></div>
        <div class="info-row"><dt>Tekst</dt><dd><code>filename:IMG</code>, <code>path:2024/01</code>, <code>camera:"iPhone"</code></dd></div>
        <div class="info-row"><dt>Organisering</dt><dd><code>source:1</code>, <code>source:"mobil 2024"</code>, <code>tag:"Ute av fokus"</code>, <code>person:Viljar</code>, <code>deleted:true</code></dd></div>
        <div class="info-row"><dt>Mangler</dt><dd><code>missing:gps</code>, <code>missing:date</code>, <code>missing:metadata</code></dd></div>
        <div class="info-row"><dt>Form</dt><dd><code>orientation:portrait</code>, <code>orientation:landscape</code>, <code>width&gt;1024</code>, <code>width=1024</code>, <code>width&lt;2000</code>, <code>height&gt;1024</code>, <code>height=1024</code>, <code>height&lt;2000</code>. Bredde og høyde er piksler uten enhet.</dd></div>
      </dl>
      <h3>Eksempler</h3>
       <dl class="info-list">
         <div class="info-row">
           <dt>Alle julaftener</dt>
           <dd><a href="/filter/day:24 month:12"><code>day:24 month:12</code></a></dd></div>
         <div class="info-row">
           <dt>Alle 17. mai</dt>
           <dd><a href="/filter/day:17 month:5"><code>day:17 month:5</code></a></dd></div>
         <div class="info-row">
           <dt>Alle julidager på Kreta</dt>
           <dd><a href="/filter/month:7 location:kreta"><code>month:7 location:kreta</code></a></dd></div>
       </dl>
    </section>
    """
