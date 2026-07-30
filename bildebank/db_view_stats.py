from __future__ import annotations

import math
import random
import sqlite3
from collections.abc import Callable, Collection, Sequence
from pathlib import Path

from .media import media_kind


def record_file_view(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    viewed_at: str,
) -> bool:
    cursor = conn.execute(
        """
        INSERT INTO file_view_stats(
            file_id, view_count, first_viewed_at, last_viewed_at
        )
        SELECT id, 1, ?, ?
        FROM files
        WHERE id = ?
          AND deleted_at IS NULL
        ON CONFLICT(file_id) DO UPDATE SET
            view_count = file_view_stats.view_count + 1,
            last_viewed_at = excluded.last_viewed_at
        """,
        (viewed_at, viewed_at, file_id),
    )
    return cursor.rowcount == 1


def random_view_candidate_file_id(
    conn: sqlite3.Connection,
    *,
    browser_file_ids: Collection[int] | None = None,
    choose: Callable[[Sequence[int]], int] = random.choice,
) -> int | None:
    candidates = eligible_view_candidates(conn, browser_file_ids=browser_file_ids)
    if not candidates:
        return None
    unseen_ids = [file_id for file_id, last_viewed_at in candidates if last_viewed_at is None]
    if unseen_ids:
        return choose(unseen_ids)
    candidates.sort(key=lambda candidate: (str(candidate[1]), candidate[0]))
    pool_size = min(len(candidates), max(20, math.ceil(len(candidates) * 0.05)))
    return choose([file_id for file_id, _ in candidates[:pool_size]])


def view_registration_counts(
    conn: sqlite3.Connection,
    *,
    browser_file_ids: Collection[int] | None = None,
) -> tuple[int, int]:
    candidates = eligible_view_candidates(conn, browser_file_ids=browser_file_ids)
    return (
        sum(last_viewed_at is not None for _, last_viewed_at in candidates),
        len(candidates),
    )


def eligible_view_candidates(
    conn: sqlite3.Connection,
    *,
    browser_file_ids: Collection[int] | None = None,
) -> list[tuple[int, str | None]]:
    browser_file_id_set = set(browser_file_ids) if browser_file_ids is not None else None
    rows = conn.execute(
        """
        SELECT files.id, files.target_path, file_view_stats.last_viewed_at
        FROM files
        LEFT JOIN file_view_stats ON file_view_stats.file_id = files.id
        WHERE files.deleted_at IS NULL
        """
    ).fetchall()
    candidates = [
        (int(row["id"]), row["last_viewed_at"])
        for row in rows
        if (
            (browser_file_id_set is None or int(row["id"]) in browser_file_id_set)
            and media_kind(Path(str(row["target_path"]))) in {"image", "video"}
        )
    ]
    return candidates
