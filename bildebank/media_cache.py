from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Protocol

from . import db
from .media import ImageDimensions, image_dimensions, image_orientation
from .value_parsing import require_int


class MediaMetadataRow(Protocol):
    def __getitem__(self, key: str) -> object: ...


class MediaMetadataCache:
    def __init__(self, target: Path, conn: sqlite3.Connection | None = None) -> None:
        self.target = target
        self.conn = conn if conn is not None else db.connect(target)
        self._owns_conn = conn is None
        self._dirty = False
        self._rows = self._load_rows()

    def close(self) -> None:
        if self._dirty:
            self.conn.commit()
        if self._owns_conn:
            self.conn.close()

    def __enter__(self) -> "MediaMetadataCache":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None:
            if self._owns_conn:
                self.conn.rollback()
                self.conn.close()
            return
        self.close()

    def image_dimensions(self, path: Path) -> ImageDimensions | None:
        mtime_ns = media_mtime_ns(path)
        path_key = self._path_key(path)
        row = self._rows.get(path_key) if path_key is not None else None
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


def cached_image_dimensions(target: Path, path: Path) -> ImageDimensions | None:
    with MediaMetadataCache(target) as cache:
        return cache.image_dimensions(path)


def cached_image_orientation(target: Path, path: Path) -> int:
    with MediaMetadataCache(target) as cache:
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
