from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .db_core import path_key


@dataclass(frozen=True)
class Source:
    id: int
    path: Path
    path_key: str | None
    name: str
    imported_at: str | None
    status: str
    superseded_by_source_id: int | None


def row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=int(row["id"]),
        path=Path(str(row["path"])),
        path_key=row["path_key"],
        name=str(row["name"]),
        imported_at=row["imported_at"],
        status=row["status"],
        superseded_by_source_id=row["superseded_by_source_id"],
    )


def get_sources(conn: sqlite3.Connection) -> list[Source]:
    return [row_to_source(row) for row in conn.execute("SELECT * FROM sources ORDER BY id")]


def get_source(conn: sqlite3.Connection, source_id: int) -> Source:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"Fant ikke kilde #{source_id}")
    return row_to_source(row)


def find_source_by_name(conn: sqlite3.Connection, name: str) -> Source | None:
    row = conn.execute(
        "SELECT * FROM sources WHERE name = ?",
        (name,),
    ).fetchone()
    return row_to_source(row) if row is not None else None


def add_named_source(conn: sqlite3.Connection, path: Path, name: str) -> int:
    existing = conn.execute(
        "SELECT id, path, imported_at FROM sources WHERE name = ?",
        (name,),
    ).fetchone()
    if existing is not None and existing["imported_at"] is not None:
        raise ValueError(
            f"Kilde med navn {name!r} er allerede importert som "
            f"{existing['path']}. Bruk et nytt --name hvis dette er en annen mappe/import."
        )
    cur = conn.execute(
        """
        INSERT INTO sources(path, path_key, name)
        VALUES(?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET path = excluded.path, path_key = excluded.path_key
        RETURNING id
        """,
        (str(path.resolve()), path_key(path), name),
    )
    return int(cur.fetchone()["id"])


def mark_source_imported(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute(
        "UPDATE sources SET imported_at = CURRENT_TIMESTAMP, status = 'imported' WHERE id = ?",
        (source_id,),
    )


def mark_source_error(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("UPDATE sources SET status = 'error' WHERE id = ?", (source_id,))


def mark_sources_superseded(
    conn: sqlite3.Connection, *, source_ids: Iterable[int], superseded_by_source_id: int
) -> None:
    ids = list(source_ids)
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"""
        UPDATE sources
        SET status = 'superseded',
            superseded_by_source_id = ?
        WHERE id IN ({placeholders})
        """,
        [superseded_by_source_id, *ids],
    )
