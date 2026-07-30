from __future__ import annotations

import math
import random
import sqlite3
from collections.abc import Callable, Sequence
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
    choose: Callable[[Sequence[int]], int] = random.choice,
) -> int | None:
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
        if media_kind(Path(str(row["target_path"]))) in {"image", "video"}
    ]
    if not candidates:
        return None
    unseen_ids = [file_id for file_id, last_viewed_at in candidates if last_viewed_at is None]
    if unseen_ids:
        return choose(unseen_ids)
    candidates.sort(key=lambda candidate: (str(candidate[1]), candidate[0]))
    pool_size = min(len(candidates), max(20, math.ceil(len(candidates) * 0.05)))
    return choose([file_id for file_id, _ in candidates[:pool_size]])
