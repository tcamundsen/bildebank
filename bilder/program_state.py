from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


PROGRAM_DB_FILENAME = ".bildebank-program.sqlite3"


@dataclass(frozen=True)
class KnownTarget:
    path: Path
    collection_id: str | None
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
        collection_id = target_collection_id(target)
        if collection_id is not None:
            existing = conn.execute(
                "SELECT id FROM targets WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    """
                    UPDATE targets
                    SET path = ?,
                        path_key = ?,
                        last_seen_at = CURRENT_TIMESTAMP
                    WHERE collection_id = ?
                    """,
                    (resolved, key, collection_id),
                )
                conn.commit()
                return
        if created:
            conn.execute(
                """
                INSERT INTO targets(path, path_key, collection_id, created_at, last_seen_at)
                VALUES(?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(path_key) DO UPDATE SET
                    path = excluded.path,
                    collection_id = excluded.collection_id,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (resolved, key, collection_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO targets(path, path_key, collection_id, last_seen_at)
                VALUES(?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(path_key) DO UPDATE SET
                    path = excluded.path,
                    collection_id = excluded.collection_id,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (resolved, key, collection_id),
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
        conn.commit()
        rows = conn.execute(
            """
            SELECT path, collection_id, created_at, last_seen_at
            FROM targets
            ORDER BY COALESCE(last_seen_at, created_at) DESC, path
            """
        ).fetchall()
        return [
            KnownTarget(
                path=Path(str(row["path"])),
                collection_id=row["collection_id"],
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
            collection_id TEXT,
            created_at TEXT,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(targets)")}
    if "collection_id" not in columns:
        conn.execute("ALTER TABLE targets ADD COLUMN collection_id TEXT")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_targets_collection_id
        ON targets(collection_id)
        WHERE collection_id IS NOT NULL
        """
    )
    backfill_collection_ids(conn)


def backfill_collection_ids(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, path
        FROM targets
        WHERE collection_id IS NULL
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        collection_id = target_collection_id(Path(str(row[1])))
        if collection_id is None:
            continue
        duplicate = conn.execute(
            "SELECT id FROM targets WHERE collection_id = ? AND id != ?",
            (collection_id, int(row[0])),
        ).fetchone()
        if duplicate is not None:
            continue
        conn.execute(
            "UPDATE targets SET collection_id = ? WHERE id = ?",
            (collection_id, int(row[0])),
        )


def target_collection_id(target: Path) -> str | None:
    db_path = target / ".bilder.sqlite3"
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'collection_id'",
        ).fetchone()
        return None if row is None else str(row[0])
    except sqlite3.Error:
        return None
    finally:
        conn.close()
