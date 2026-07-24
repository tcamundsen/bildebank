from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Protocol


DB_FILENAME = ".bilder.sqlite3"
COLLECTION_ID_META_KEY = "collection_id"
_PREPARED_TARGETS: set[str] = set()


class SchemaValidator(Protocol):
    def __call__(self, conn: sqlite3.Connection, *, full: bool = True) -> None: ...


def path_key(path: Path) -> str:
    resolved = path.resolve()
    value = os.path.normpath(str(resolved))
    if os.name == "nt":
        value = value.lower()
    return value


def relative_path(path: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    normalized = os.path.normpath(str(candidate))
    return Path(normalized)


def relative_path_key(path: Path) -> str:
    value = os.path.normpath(str(relative_path(path))).replace("\\", "/")
    if value in {".", ""}:
        return ""
    if os.name == "nt":
        value = value.lower()
    return value


def target_relative_path(target: Path, path: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(target.resolve())
        except ValueError as exc:
            raise ValueError(f"Filen ligger utenfor bildesamlingen: {path}") from exc
    return relative_path(candidate)


def target_relative_path_key(target: Path, path: Path) -> str:
    return relative_path_key(target_relative_path(target, path))


def absolute_target_path(target: Path, path: Path | str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else target / candidate


def db_path_for_target(target: Path) -> Path:
    return target / DB_FILENAME


def connect_database_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    except Exception:
        conn.close()
        raise


def find_target(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if db_path_for_target(candidate).exists():
            return candidate
    return None


def connect(
    target: Path,
    *,
    require_current: bool = True,
    schema_validator: SchemaValidator | None = None,
) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path_for_target(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        if require_current:
            if schema_validator is None:
                raise ValueError("schema_validator kreves når require_current=True")
            schema_validator(conn, full=str(target.resolve()) not in _PREPARED_TARGETS)
        return conn
    except Exception:
        conn.close()
        raise


def prepare_database(target: Path, *, schema_validator: SchemaValidator) -> None:
    conn = connect(target, require_current=False)
    try:
        schema_validator(conn, full=True)
        _PREPARED_TARGETS.add(str(target.resolve()))
    finally:
        conn.close()


def execute_sql_statements(conn: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            conn.execute(statement)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = table_columns(conn, table)
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_collection_id(conn: sqlite3.Connection) -> str:
    value = get_meta(conn, COLLECTION_ID_META_KEY)
    if value is None:
        value = str(uuid.uuid4())
        set_meta(conn, COLLECTION_ID_META_KEY, value)
        return value
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"Ugyldig collection_id i databasen: {value}") from exc
    if normalized != value:
        set_meta(conn, COLLECTION_ID_META_KEY, normalized)
    return normalized


def log_command(conn: sqlite3.Connection, command: str, args: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO command_log(command, args_json) VALUES(?, ?)",
        (command, json.dumps(args, ensure_ascii=False, sort_keys=True)),
    )
