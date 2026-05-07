from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


PROGRAM_DB_FILENAME = ".bildebank-program.sqlite3"


@dataclass(frozen=True)
class KnownTarget:
    path: Path
    created_at: str | None
    last_seen_at: str | None
    exists: bool


def program_db_path(program_root: Path) -> Path:
    return program_root / PROGRAM_DB_FILENAME


def path_key(path: Path) -> str:
    resolved = path.resolve()
    value = os.path.normpath(str(resolved))
    if os.name == "nt":
        value = value.lower()
    return value


def record_target_best_effort(program_root: Path, target: Path, *, created: bool = False) -> None:
    try:
        record_target(program_root, target, created=created)
    except Exception:
        return


def record_target(program_root: Path, target: Path, *, created: bool = False) -> None:
    db_path = program_db_path(program_root)
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        key = path_key(target)
        resolved = str(target.resolve())
        if created:
            conn.execute(
                """
                INSERT INTO targets(path, path_key, created_at, last_seen_at)
                VALUES(?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(path_key) DO UPDATE SET
                    path = excluded.path,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (resolved, key),
            )
        else:
            conn.execute(
                """
                INSERT INTO targets(path, path_key, last_seen_at)
                VALUES(?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(path_key) DO UPDATE SET
                    path = excluded.path,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (resolved, key),
            )
        conn.commit()
    finally:
        conn.close()


def known_targets(program_root: Path) -> list[KnownTarget]:
    db_path = program_db_path(program_root)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT path, created_at, last_seen_at
            FROM targets
            ORDER BY COALESCE(last_seen_at, created_at) DESC, path
            """
        ).fetchall()
        return [
            KnownTarget(
                path=Path(str(row["path"])),
                created_at=row["created_at"],
                last_seen_at=row["last_seen_at"],
                exists=Path(str(row["path"])).exists(),
            )
            for row in rows
        ]
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            path_key TEXT NOT NULL UNIQUE,
            created_at TEXT,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
