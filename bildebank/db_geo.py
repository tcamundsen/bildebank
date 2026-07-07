from __future__ import annotations

import sqlite3

from .db_schema import (
    BROWSER_DATE_ORDER_SQL,
    H3_FILE_COLUMNS,
    H3_FILE_COLUMN_SET,
    H3_FILE_COLUMNS_SQL,
)


def geo_scan_files(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    only_missing: bool = False,
    override_manual_h3: bool = False,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    where = ["deleted_at IS NULL"]
    if not override_manual_h3:
        where.append("(gps_source IS NULL OR gps_source != 'manual-h3')")
    if only_missing:
        where.append("gps_lat IS NULL")
        where.append("gps_lon IS NULL")
        where.append("gps_scanned_at IS NULL")
        where.append("gps_error IS NULL")
    elif not force:
        missing_or_unscanned = "(gps_scanned_at IS NULL OR gps_lat IS NULL OR gps_lon IS NULL)"
        if override_manual_h3:
            where.append(f"({missing_or_unscanned} OR gps_source = 'manual-h3')")
        else:
            where.append(missing_or_unscanned)
    sql = """
        SELECT id, target_path
        FROM files
        WHERE {where_sql}
        ORDER BY target_path
    """.format(where_sql=" AND ".join(where))
    params: list[int] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def update_file_gps(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    gps_lat: float | None,
    gps_lon: float | None,
    gps_alt: float | None,
    h3_cells: dict[str, str | None] | None,
    gps_source: str | None,
    gps_error: str | None,
) -> None:
    cells = h3_cells or {}
    assignments = ",\n            ".join(f"{column} = ?" for column in H3_FILE_COLUMNS)
    conn.execute(
        f"""
        UPDATE files
        SET gps_lat = ?,
            gps_lon = ?,
            gps_alt = ?,
            {assignments},
            gps_source = ?,
            gps_scanned_at = CURRENT_TIMESTAMP,
            gps_error = ?
        WHERE id = ?
        """,
        (
            gps_lat,
            gps_lon,
            gps_alt,
            *(cells.get(column) for column in H3_FILE_COLUMNS),
            gps_source,
            gps_error,
            file_id,
        ),
    )


def set_file_manual_h3_location(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    h3_cells: dict[str, str | None],
) -> bool:
    row = conn.execute(
        """
        SELECT id, deleted_at, gps_lat, gps_lon
        FROM files
        WHERE id = ?
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        return False
    if row["deleted_at"] is not None:
        raise ValueError("Filen er markert som slettet.")
    if row["gps_lat"] is not None or row["gps_lon"] is not None:
        raise ValueError("Filen har GPS-lokasjon og kan ikke få manuell H3-celle.")
    cells = h3_cells or {}
    assignments = ",\n            ".join(f"{column} = ?" for column in H3_FILE_COLUMNS)
    conn.execute(
        f"""
        UPDATE files
        SET gps_lat = NULL,
            gps_lon = NULL,
            gps_alt = NULL,
            {assignments},
            gps_source = 'manual-h3',
            gps_scanned_at = CURRENT_TIMESTAMP,
            gps_error = NULL
        WHERE id = ?
        """,
        (
            *(cells.get(column) for column in H3_FILE_COLUMNS),
            file_id,
        ),
    )
    return True


def remove_file_manual_h3_location(
    conn: sqlite3.Connection,
    *,
    file_id: int,
) -> bool:
    row = conn.execute(
        """
        SELECT id, deleted_at, gps_source
        FROM files
        WHERE id = ?
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        return False
    if row["deleted_at"] is not None:
        raise ValueError("Filen er markert som slettet.")
    if str(row["gps_source"] or "") != "manual-h3":
        raise ValueError("Filen har ikke manuell H3-lokasjon.")
    assignments = ",\n            ".join(f"{column} = NULL" for column in H3_FILE_COLUMNS)
    conn.execute(
        f"""
        UPDATE files
        SET gps_lat = NULL,
            gps_lon = NULL,
            gps_alt = NULL,
            {assignments},
            gps_source = NULL,
            gps_scanned_at = NULL,
            gps_error = NULL
        WHERE id = ?
        """,
        (file_id,),
    )
    return True


def geo_stats(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN gps_scanned_at IS NOT NULL THEN 1 ELSE 0 END) AS scanned,
            SUM(CASE WHEN gps_lat IS NOT NULL AND gps_lon IS NOT NULL THEN 1 ELSE 0 END) AS with_gps,
            SUM(CASE WHEN gps_scanned_at IS NOT NULL
                      AND gps_lat IS NULL
                      AND gps_lon IS NULL
                      AND gps_error IS NULL THEN 1 ELSE 0 END) AS without_gps,
            SUM(CASE WHEN gps_source = 'manual-h3' THEN 1 ELSE 0 END) AS manual_h3,
            SUM(CASE WHEN gps_error IS NOT NULL THEN 1 ELSE 0 END) AS errors
        FROM files
        WHERE deleted_at IS NULL
        """
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "scanned": int(row["scanned"] or 0),
        "with_gps": int(row["with_gps"] or 0),
        "without_gps": int(row["without_gps"] or 0),
        "manual_h3": int(row["manual_h3"] or 0),
        "errors": int(row["errors"] or 0),
    }


def geo_areas(
    conn: sqlite3.Connection,
    *,
    column: str,
    min_count: int,
    limit: int,
) -> list[sqlite3.Row]:
    validate_h3_column(column)
    return list(
        conn.execute(
            f"""
            SELECT files.{column} AS h3_cell, COUNT(*) AS count, geo_place_names.name AS name
            FROM files
            LEFT JOIN geo_place_names ON geo_place_names.h3_cell = files.{column}
            WHERE files.{column} IS NOT NULL
              AND files.deleted_at IS NULL
            GROUP BY files.{column}
            HAVING COUNT(*) >= ?
            ORDER BY count DESC, geo_place_names.name, files.{column}
            LIMIT ?
            """,
            (min_count, limit),
        )
    )


GEO_FILE_COLUMNS = (
    "id, target_path, target_path_key, stored_filename, taken_date, date_source, "
    "manual_date_from, manual_date_to, manual_date_note, "
    "camera_make, camera_model, "
    "size_bytes, view_rotation_degrees, gps_lat, gps_lon, gps_alt, gps_source, "
    f"{H3_FILE_COLUMNS_SQL}"
)


def validate_h3_column(column: str) -> None:
    if column not in H3_FILE_COLUMN_SET:
        raise ValueError(f"Ustøttet H3-kolonne: {column}")


def geo_area_files(
    conn: sqlite3.Connection,
    *,
    column: str,
    h3_cell: str,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    validate_h3_column(column)
    sql = f"""
        SELECT {GEO_FILE_COLUMNS}
        FROM files
        WHERE {column} = ?
          AND deleted_at IS NULL
        ORDER BY {BROWSER_DATE_ORDER_SQL}, target_path_key
    """
    params: list[str | int] = [h3_cell]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def geo_place_count(conn: sqlite3.Connection, *, cells_by_column: list[tuple[str, str]]) -> int:
    conditions, params = geo_place_where_clause(cells_by_column)
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT id) AS count
        FROM files
        WHERE deleted_at IS NULL
          AND ({conditions})
        """,
        params,
    ).fetchone()
    return int(row["count"] or 0)


def geo_place_files(
    conn: sqlite3.Connection,
    *,
    cells_by_column: list[tuple[str, str]],
    limit: int | None = None,
) -> list[sqlite3.Row]:
    conditions, params = geo_place_where_clause(cells_by_column)
    sql = f"""
        SELECT DISTINCT {GEO_FILE_COLUMNS}
        FROM files
        WHERE deleted_at IS NULL
          AND ({conditions})
        ORDER BY {BROWSER_DATE_ORDER_SQL}, target_path_key
    """
    query_params: list[str | int] = list(params)
    if limit is not None:
        sql += " LIMIT ?"
        query_params.append(limit)
    return list(conn.execute(sql, query_params))


def geo_place_where_clause(cells_by_column: list[tuple[str, str]]) -> tuple[str, tuple[str, ...]]:
    clean_cells: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for column, h3_cell in cells_by_column:
        validate_h3_column(column)
        clean_cell = h3_cell.strip()
        if not clean_cell:
            continue
        key = (column, clean_cell)
        if key not in seen:
            clean_cells.append(key)
            seen.add(key)
    if not clean_cells:
        raise ValueError("Geo-sted mangler H3-celler.")
    conditions = " OR ".join(f"{column} = ?" for column, _h3_cell in clean_cells)
    return conditions, tuple(h3_cell for _column, h3_cell in clean_cells)


def geo_child_areas(
    conn: sqlite3.Connection,
    *,
    parent_column: str,
    parent_h3_cell: str,
    child_column: str,
) -> list[sqlite3.Row]:
    validate_h3_column(parent_column)
    validate_h3_column(child_column)
    return list(
        conn.execute(
            f"""
            SELECT files.{child_column} AS h3_cell, COUNT(*) AS count, geo_place_names.name AS name
            FROM files
            LEFT JOIN geo_place_names ON geo_place_names.h3_cell = files.{child_column}
            WHERE files.{parent_column} = ?
              AND files.{child_column} IS NOT NULL
              AND files.deleted_at IS NULL
            GROUP BY files.{child_column}
            ORDER BY count DESC, COALESCE(geo_place_names.name, files.{child_column}), files.{child_column}
            """,
            (parent_h3_cell,),
        )
    )


def geo_place_name(conn: sqlite3.Connection, h3_cell: str) -> str | None:
    row = conn.execute("SELECT name FROM geo_place_names WHERE h3_cell = ?", (h3_cell,)).fetchone()
    return None if row is None else str(row["name"])


def geo_place_names(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT h3_cell, name
            FROM geo_place_names
            ORDER BY name COLLATE NOCASE, h3_cell
            """
        )
    )


def set_geo_place_name(conn: sqlite3.Connection, h3_cell: str, name: str) -> str | None:
    clean_name = name.strip()
    if not h3_cell.strip():
        raise ValueError("H3-celle mangler.")
    if not clean_name:
        conn.execute("DELETE FROM geo_place_names WHERE h3_cell = ?", (h3_cell,))
        return None
    conn.execute(
        """
        INSERT INTO geo_place_names(h3_cell, name, updated_at)
        VALUES(?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(h3_cell) DO UPDATE SET
            name = excluded.name,
            updated_at = CURRENT_TIMESTAMP
        """,
        (h3_cell, clean_name),
    )
    return clean_name


def custom_geo_places(conn: sqlite3.Connection) -> list[dict[str, object]]:
    places = list(conn.execute("SELECT slug, name FROM geo_places ORDER BY name, slug"))
    if not places:
        return []
    cells_by_slug = custom_geo_place_cells_by_slug(conn)
    return [
        {
            "slug": str(row["slug"]),
            "name": str(row["name"]),
            "h3_cells": tuple(cells_by_slug.get(str(row["slug"]), ())),
        }
        for row in places
    ]


def custom_geo_place(conn: sqlite3.Connection, slug: str) -> dict[str, object] | None:
    clean_slug = slug.strip().lower()
    row = conn.execute("SELECT slug, name FROM geo_places WHERE slug = ?", (clean_slug,)).fetchone()
    if row is None:
        return None
    cells = tuple(row["h3_cell"] for row in custom_geo_place_cells(conn, clean_slug))
    return {"slug": str(row["slug"]), "name": str(row["name"]), "h3_cells": cells}


def custom_geo_place_cells_by_slug(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute(
        """
        SELECT slug, h3_cell
        FROM geo_place_cells
        ORDER BY slug, position, h3_cell
        """
    )
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(str(row["slug"]), []).append(str(row["h3_cell"]))
    return result


def custom_geo_place_cells(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT h3_cell
            FROM geo_place_cells
            WHERE slug = ?
            ORDER BY position, h3_cell
            """,
            (slug,),
        )
    )


def set_custom_geo_place(conn: sqlite3.Connection, *, slug: str, name: str, h3_cells: list[str]) -> None:
    clean_slug = slug.strip().lower()
    clean_name = name.strip()
    if not clean_slug:
        raise ValueError("Slug mangler.")
    if not clean_name:
        raise ValueError("Navn mangler.")
    clean_cells: list[str] = []
    seen: set[str] = set()
    for cell in h3_cells:
        clean_cell = cell.strip()
        if not clean_cell or clean_cell in seen:
            continue
        clean_cells.append(clean_cell)
        seen.add(clean_cell)
    if not clean_cells:
        raise ValueError("Stedet må ha minst én H3-celle.")
    conn.execute(
        """
        INSERT INTO geo_places(slug, name, updated_at)
        VALUES(?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(slug) DO UPDATE SET
            name = excluded.name,
            updated_at = CURRENT_TIMESTAMP
        """,
        (clean_slug, clean_name),
    )
    conn.execute("DELETE FROM geo_place_cells WHERE slug = ?", (clean_slug,))
    conn.executemany(
        """
        INSERT INTO geo_place_cells(slug, h3_cell, position)
        VALUES(?, ?, ?)
        """,
        [(clean_slug, cell, index) for index, cell in enumerate(clean_cells)],
    )


def rename_custom_geo_place(
    conn: sqlite3.Connection,
    *,
    old_slug: str,
    slug: str,
    name: str,
    h3_cells: list[str],
) -> None:
    clean_old_slug = old_slug.strip().lower()
    clean_slug = slug.strip().lower()
    if not clean_old_slug:
        set_custom_geo_place(conn, slug=slug, name=name, h3_cells=h3_cells)
        return
    if clean_old_slug == clean_slug:
        set_custom_geo_place(conn, slug=slug, name=name, h3_cells=h3_cells)
        return
    if conn.execute("SELECT 1 FROM geo_places WHERE slug = ?", (clean_slug,)).fetchone() is not None:
        raise ValueError("Slug er allerede i bruk.")
    if conn.execute("SELECT 1 FROM geo_places WHERE slug = ?", (clean_old_slug,)).fetchone() is None:
        raise ValueError("Stedet som skulle oppdateres finnes ikke.")
    conn.execute("DELETE FROM geo_places WHERE slug = ?", (clean_old_slug,))
    set_custom_geo_place(conn, slug=slug, name=name, h3_cells=h3_cells)


def delete_custom_geo_place(conn: sqlite3.Connection, slug: str) -> None:
    conn.execute("DELETE FROM geo_places WHERE slug = ?", (slug.strip().lower(),))
