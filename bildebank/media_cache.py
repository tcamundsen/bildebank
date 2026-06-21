from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Protocol

from . import db
from .media import ImageDimensions, image_dimensions, image_orientation
from .target_lock import TargetLock
from .value_parsing import require_int


class MediaMetadataRow(Protocol):
    def __getitem__(self, key: str) -> object: ...


class MediaMetadataCache:
    def __init__(
        self,
        target: Path,
        conn: sqlite3.Connection | None = None,
        *,
        target_locked: bool = False,
    ) -> None:
        self.target = target
        self.conn = conn if conn is not None else db.connect(target)
        self._owns_conn = conn is None
        self._target_locked = target_locked
        self._target_lock: TargetLock | None = None
        self._dirty = False
        self._rows = self._load_rows()

    def close(self) -> None:
        try:
            if self._dirty:
                self.conn.commit()
            if self._owns_conn:
                self.conn.close()
        finally:
            if self._target_lock is not None:
                self._target_lock.__exit__(None, None, None)
                self._target_lock = None

    def __enter__(self) -> "MediaMetadataCache":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None:
            try:
                if self._owns_conn:
                    self.conn.rollback()
                    self.conn.close()
            finally:
                if self._target_lock is not None:
                    self._target_lock.__exit__(exc_type, exc, traceback)
                    self._target_lock = None
            return
        self.close()

    def image_dimensions(self, path: Path) -> ImageDimensions | None:
        mtime_ns = media_mtime_ns(path)
        path_key = self._path_key(path)
        row = self._rows.get(path_key) if path_key is not None else None
        if row is not None and cached_mtime_matches(row, mtime_ns):
            return dimensions_from_row(row)

        self._ensure_target_lock()
        mtime_ns = media_mtime_ns(path)
        row = self._reload_row(path_key)
        if row is not None and cached_mtime_matches(row, mtime_ns):
            return dimensions_from_row(row)
        dimensions = image_dimensions(path)
        if row is not None:
            self.conn.execute(
                """
                UPDATE files
                SET media_width = ?,
                    media_height = ?,
                    media_metadata_mtime_ns = ?
                WHERE id = ?
                """,
                (
                    dimensions.width if dimensions is not None else None,
                    dimensions.height if dimensions is not None else None,
                    mtime_ns,
                    int(row["id"]),
                ),
            )
            self._dirty = True
            row["media_width"] = dimensions.width if dimensions is not None else None
            row["media_height"] = dimensions.height if dimensions is not None else None
            row["media_metadata_mtime_ns"] = mtime_ns
        return dimensions

    def image_orientation(self, path: Path) -> int:
        mtime_ns = media_mtime_ns(path)
        path_key = self._path_key(path)
        row = self._rows.get(path_key) if path_key is not None else None
        if row is not None and cached_mtime_matches(row, mtime_ns) and row["media_orientation"] is not None:
            return int(row["media_orientation"])

        self._ensure_target_lock()
        mtime_ns = media_mtime_ns(path)
        row = self._reload_row(path_key)
        if row is not None and cached_mtime_matches(row, mtime_ns) and row["media_orientation"] is not None:
            return int(row["media_orientation"])
        orientation = image_orientation(path)
        if row is not None:
            self.conn.execute(
                """
                UPDATE files
                SET media_orientation = ?,
                    media_metadata_mtime_ns = ?
                WHERE id = ?
                """,
                (orientation, mtime_ns, int(row["id"])),
            )
            self._dirty = True
            row["media_orientation"] = orientation
            row["media_metadata_mtime_ns"] = mtime_ns
        return orientation

    def _load_rows(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
                id,
                target_path_key,
                media_width,
                media_height,
                media_orientation,
                media_metadata_mtime_ns
            FROM files
            WHERE deleted_at IS NULL
            """
        )
        return {str(row["target_path_key"]): dict(row) for row in rows}

    def _path_key(self, path: Path) -> str | None:
        try:
            return db.target_relative_path_key(self.target, path)
        except ValueError:
            return None

    def _ensure_target_lock(self) -> None:
        if self._target_locked or self._target_lock is not None:
            return
        target_lock = TargetLock(self.target, command="media-metadata-cache")
        target_lock.__enter__()
        self._target_lock = target_lock

    def _reload_row(self, path_key: str | None) -> dict[str, Any] | None:
        if path_key is None:
            return None
        row = self.conn.execute(
            """
            SELECT
                id,
                target_path_key,
                media_width,
                media_height,
                media_orientation,
                media_metadata_mtime_ns
            FROM files
            WHERE target_path_key = ?
              AND deleted_at IS NULL
            """,
            (path_key,),
        ).fetchone()
        if row is None:
            self._rows.pop(path_key, None)
            return None
        loaded = dict(row)
        self._rows[path_key] = loaded
        return loaded


def cached_image_dimensions(
    target: Path,
    path: Path,
    *,
    target_locked: bool = False,
) -> ImageDimensions | None:
    with MediaMetadataCache(target, target_locked=target_locked) as cache:
        return cache.image_dimensions(path)


def cached_image_orientation(
    target: Path,
    path: Path,
    *,
    target_locked: bool = False,
) -> int:
    with MediaMetadataCache(target, target_locked=target_locked) as cache:
        return cache.image_orientation(path)


def media_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def cached_mtime_matches(row: MediaMetadataRow, mtime_ns: int | None) -> bool:
    cached_mtime_ns = row["media_metadata_mtime_ns"]
    return cached_mtime_ns is not None and require_int(cached_mtime_ns, "media_metadata_mtime_ns") == mtime_ns


def dimensions_from_row(row: MediaMetadataRow) -> ImageDimensions | None:
    width = row["media_width"]
    height = row["media_height"]
    if width is None or height is None:
        return None
    width_int = require_int(width, "media_width")
    height_int = require_int(height, "media_height")
    if width_int <= 0 or height_int <= 0:
        return None
    return ImageDimensions(width_int, height_int)
