from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .db_core import path_key


@dataclass(frozen=True)
class Source:
    id: int
    path: Path
    path_key: str | None
    name: str
    imported_at: str | None
    status: str


def row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=int(row["id"]),
        path=Path(str(row["path"])),
        path_key=row["path_key"],
        name=str(row["name"]),
        imported_at=row["imported_at"],
        status=row["status"],
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
        "SELECT id, path, path_key, imported_at FROM sources WHERE name = ?",
        (name,),
    ).fetchone()
    if existing is not None and existing["imported_at"] is not None:
        raise ValueError(
            f"Kilde med navn {name!r} er allerede importert som "
            f"{existing['path']}. Bruk et nytt --name hvis dette er en annen mappe/import."
        )
    new_path_key = path_key(path)
    if existing is not None:
        existing_path_key = existing["path_key"] or path_key(Path(str(existing["path"])))
        if existing_path_key != new_path_key:
            recorded_files = int(
                conn.execute(
                    "SELECT COUNT(*) FROM file_sources WHERE source_id = ?",
                    (int(existing["id"]),),
                ).fetchone()[0]
            )
            if recorded_files:
                raise ValueError(
                    f"Kilde med navn {name!r} har en delvis import fra {existing['path']}. "
                    "Kilden kan ikke flyttes eller få ny stasjon før den delvise importen "
                    "er fullført fra den opprinnelige plasseringen."
                )
    cur = conn.execute(
        """
        INSERT INTO sources(path, path_key, name)
        VALUES(?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET path = excluded.path, path_key = excluded.path_key
        RETURNING id
        """,
        (str(path.resolve()), new_path_key, name),
    )
    return int(cur.fetchone()["id"])


def mark_source_imported(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute(
        "UPDATE sources SET imported_at = CURRENT_TIMESTAMP, status = 'imported' WHERE id = ?",
        (source_id,),
    )


def mark_source_error(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("UPDATE sources SET status = 'error' WHERE id = ?", (source_id,))
