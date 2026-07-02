from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import db


REPO_ROOT = Path(__file__).resolve().parents[1]
# "example-bilder.sqlite3" er en kopi av nåværende .bildebank.sqlite3 fra bildesamlingen
# min. Jeg kopierer jevnlig inn ny versjon.
EXAMPLE_DATABASE = REPO_ROOT / "example-bilder.sqlite3"

mcp = FastMCP("Bildebank dev")


def _connect_readonly(database_path: Path) -> sqlite3.Connection:
    if not database_path.is_file():
        raise FileNotFoundError(f"Fant ikke kopiert database: {database_path.name}")
    conn = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["name"])
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]


def _schema_version(conn: sqlite3.Connection, table_names: set[str]) -> int | None:
    if "meta" not in table_names:
        return None
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def _example_schema_version() -> int | None:
    try:
        with _connect_readonly(EXAMPLE_DATABASE) as conn:
            return _schema_version(conn, set(_table_names(conn)))
    except (OSError, sqlite3.Error):
        return None


@contextmanager
def _schema_database() -> Iterator[tuple[Path, dict[str, Any]]]:
    example_schema_version = _example_schema_version()
    metadata: dict[str, Any] = {
        "example_database": EXAMPLE_DATABASE.name,
        "example_schema_version": example_schema_version,
        "runtime_schema_version": db.SCHEMA_VERSION,
    }

    if example_schema_version == db.SCHEMA_VERSION:
        yield EXAMPLE_DATABASE, metadata | {"schema_source": "example_database"}
        return

    with tempfile.TemporaryDirectory(prefix="bildebank-schema-") as temporary_directory:
        target = Path(temporary_directory) / "collection"
        db.init_database(target)
        yield db.db_path_for_target(target), metadata | {"schema_source": "generated_runtime_schema"}


def _columns(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [
        {
            "name": str(row["name"]),
            "type": str(row["type"]),
            "not_null": bool(row["notnull"]),
            "default": row["dflt_value"],
            "primary_key": int(row["pk"]),
        }
        for row in conn.execute(f"PRAGMA table_info({table})")
    ]


def _indexes(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    indexes: list[dict[str, Any]] = []
    for row in conn.execute(f"PRAGMA index_list({table})"):
        index_name = str(row["name"])
        indexes.append(
            {
                "name": index_name,
                "unique": bool(row["unique"]),
                "origin": str(row["origin"]),
                "partial": bool(row["partial"]),
                "columns": [
                    str(column["name"])
                    for column in conn.execute(f"PRAGMA index_info({index_name})")
                ],
            }
        )
    return indexes


def _foreign_keys(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [
        {
            "from": str(row["from"]),
            "table": str(row["table"]),
            "to": str(row["to"]),
            "on_update": str(row["on_update"]),
            "on_delete": str(row["on_delete"]),
        }
        for row in conn.execute(f"PRAGMA foreign_key_list({table})")
    ]


@mcp.tool()
def get_schema_summary() -> dict[str, Any]:
    """Returner gjeldende schema uten å lese bildedata."""
    with _schema_database() as (database_path, metadata):
        with _connect_readonly(database_path) as conn:
            table_names = _table_names(conn)
            table_name_set = set(table_names)
            return {
                "database": database_path.name,
                **metadata,
                "schema_version": _schema_version(conn, table_name_set),
                "tables": [
                    {
                        "name": table,
                        "columns": _columns(conn, table),
                        "indexes": _indexes(conn, table),
                        "foreign_keys": _foreign_keys(conn, table),
                    }
                    for table in table_names
                ],
            }


if __name__ == "__main__":
    mcp.run()
