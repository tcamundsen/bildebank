from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .db_core import (
    absolute_target_path,
    path_key,
    target_relative_path,
    target_relative_path_key,
)
from .db_schema import VIDEO_EXTENSIONS
from .db_sources import Source


MAX_FILE_COMMENT_LENGTH = 2000


def normalize_file_comment(comment: str) -> str:
    normalized = comment.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("Kommentaren kan ikke være tom. Bruk fjern kommentar i stedet.")
    if len(normalized) > MAX_FILE_COMMENT_LENGTH:
        raise ValueError(f"Kommentaren kan ikke være lengre enn {MAX_FILE_COMMENT_LENGTH} tegn.")
    return normalized


def set_file_comment(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    comment: str | None,
) -> str | None:
    normalized = None if comment is None else normalize_file_comment(comment)
    cursor = conn.execute(
        "UPDATE files SET comment = ? WHERE id = ?",
        (normalized, file_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("Filen finnes ikke i importdatabasen.")
    return normalized


@dataclass(frozen=True)
class UnimportPlan:
    source: Source
    source_file_count: int
    active_remove_count: int
    active_keep_count: int
    file_ids_to_delete: tuple[int, ...]
    target_paths_to_delete: tuple[Path, ...]
    target_paths_to_keep: tuple[Path, ...]


def source_file_sources(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT
                file_sources.id,
                file_sources.file_id,
                file_sources.source_path,
                file_sources.source_path_key,
                file_sources.sha256,
                file_sources.size_bytes,
                files.target_path,
                files.deleted_at
            FROM file_sources
            JOIN files ON files.id = file_sources.file_id
            WHERE file_sources.source_id = ?
            ORDER BY file_sources.id
            """,
            (source_id,),
        )
    )


def files_by_hash(conn: sqlite3.Connection, sha256: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM files
            WHERE sha256 = ?
            ORDER BY deleted_at IS NOT NULL, id
            """,
            (sha256,),
        )
    )


def duplicate_active_sha256_files(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            WITH duplicate_hashes AS (
                SELECT sha256, COUNT(*) AS duplicate_count
                FROM files
                WHERE deleted_at IS NULL
                GROUP BY sha256
                HAVING COUNT(*) > 1
            )
            SELECT
                files.id,
                files.sha256,
                files.target_path,
                files.size_bytes,
                files.imported_at,
                duplicate_hashes.duplicate_count
            FROM files
            JOIN duplicate_hashes ON duplicate_hashes.sha256 = files.sha256
            WHERE files.deleted_at IS NULL
            ORDER BY files.sha256, files.id
            """
        )
    )


def active_file_integrity_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT id, target_path, sha256, size_bytes
            FROM files
            WHERE deleted_at IS NULL
            ORDER BY id
            """
        )
    )


def file_target_path_keys(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["target_path_key"])
        for row in conn.execute("SELECT target_path_key FROM files")
    }


def get_file_source_for_source_path(
    conn: sqlite3.Connection, source_id: int, source_path_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM file_sources WHERE source_id = ? AND source_path_key = ?",
        (source_id, source_path_key),
    ).fetchone()


def insert_imported_file(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    source_path: Path,
    target_root: Path,
    target_path: Path,
    original_filename: str,
    stored_filename: str,
    sha256: str,
    size_bytes: int,
    taken_date: str | None,
    date_source: str,
    name_conflict: bool,
    camera_make: str | None = None,
    camera_model: str | None = None,
    metadata_datetime: str | None = None,
) -> int:
    try:
        cur = conn.execute(
            """
            INSERT INTO files(
                target_path, target_path_key, original_filename, stored_filename, sha256,
                size_bytes, taken_date, date_source, name_conflict, camera_make, camera_model,
                metadata_datetime
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                target_relative_path(target_root, target_path).as_posix(),
                target_relative_path_key(target_root, target_path),
                original_filename,
                stored_filename,
                sha256,
                size_bytes,
                taken_date,
                date_source,
                1 if name_conflict else 0,
                camera_make,
                camera_model,
                metadata_datetime,
            ),
        )
        file_id = int(cur.fetchone()["id"])
        insert_or_validate_file_source(
            conn,
            file_id=file_id,
            source_id=source_id,
            source_path=str(source_path.resolve()),
            source_path_key=path_key(source_path),
            sha256=sha256,
            size_bytes=size_bytes,
        )
        return file_id
    except sqlite3.IntegrityError as exc:
        raise sqlite3.IntegrityError(
            f"Kunne ikke registrere importert fil i databasen: {exc}"
        ) from exc


def insert_duplicate(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    source_path: Path,
    matched_file_id: int,
    sha256: str,
    size_bytes: int,
) -> None:
    insert_or_validate_file_source(
        conn,
        file_id=matched_file_id,
        source_id=source_id,
        source_path=str(source_path.resolve()),
        source_path_key=path_key(source_path),
        sha256=sha256,
        size_bytes=size_bytes,
    )


def insert_or_validate_file_source(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    source_id: int,
    source_path: str,
    source_path_key: str,
    sha256: str,
    size_bytes: int,
) -> None:
    existing = conn.execute(
        """
        SELECT file_id, source_path, sha256, size_bytes
        FROM file_sources
        WHERE source_id = ? AND source_path_key = ?
        """,
        (source_id, source_path_key),
    ).fetchone()
    if existing is not None:
        mismatches = []
        expected = {
            "file_id": file_id,
            "source_path": source_path,
            "sha256": sha256,
            "size_bytes": size_bytes,
        }
        for key, value in expected.items():
            if existing[key] != value:
                mismatches.append(f"{key}: eksisterende={existing[key]!r}, forventet={value!r}")
        if mismatches:
            raise ValueError(
                "Konflikt i file_sources for "
                f"source_id={source_id}, source_path_key={source_path_key}: "
                + "; ".join(mismatches)
            )
        return

    conn.execute(
        """
        INSERT INTO file_sources(
            file_id, source_id, source_path, source_path_key, sha256, size_bytes
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (file_id, source_id, source_path, source_path_key, sha256, size_bytes),
    )


def build_unimport_plan(conn: sqlite3.Connection, target: Path, source: Source) -> UnimportPlan:
    rows = source_file_sources(conn, source.id)
    source_counts: dict[int, int] = {}
    for row in rows:
        file_id = int(row["file_id"])
        source_counts[file_id] = source_counts.get(file_id, 0) + 1
    total_sources_by_file_id = unimport_total_sources_by_file_id(conn, source.id)

    active_remove_file_ids: set[int] = set()
    active_keep_file_ids: set[int] = set()
    target_paths_to_delete: list[Path] = []
    target_paths_to_keep: list[Path] = []
    seen_delete_file_ids: set[int] = set()
    seen_keep_file_ids: set[int] = set()

    for row in rows:
        file_id = int(row["file_id"])
        total_sources = total_sources_by_file_id[file_id]
        if total_sources == source_counts[file_id]:
            if row["deleted_at"] is None:
                active_remove_file_ids.add(file_id)
            if file_id not in seen_delete_file_ids:
                target_paths_to_delete.append(absolute_target_path(target, Path(str(row["target_path"]))))
                seen_delete_file_ids.add(file_id)
        else:
            if row["deleted_at"] is None:
                active_keep_file_ids.add(file_id)
            if file_id not in seen_keep_file_ids:
                target_paths_to_keep.append(absolute_target_path(target, Path(str(row["target_path"]))))
                seen_keep_file_ids.add(file_id)

    return UnimportPlan(
        source=source,
        source_file_count=len(rows),
        active_remove_count=len(active_remove_file_ids),
        active_keep_count=len(active_keep_file_ids),
        file_ids_to_delete=tuple(sorted(seen_delete_file_ids)),
        target_paths_to_delete=tuple(target_paths_to_delete),
        target_paths_to_keep=tuple(target_paths_to_keep),
    )


def unimport_total_sources_by_file_id(conn: sqlite3.Connection, source_id: int) -> dict[int, int]:
    return {
        int(row["file_id"]): int(row["total_sources"])
        for row in conn.execute(
            """
            SELECT file_id, COUNT(*) AS total_sources
            FROM file_sources
            WHERE file_id IN (
                SELECT file_id
                FROM file_sources
                WHERE source_id = ?
            )
            GROUP BY file_id
            """,
            (source_id,),
        )
    }


def apply_unimport(conn: sqlite3.Connection, plan: UnimportPlan) -> None:
    source_id = plan.source.id
    conn.execute("DELETE FROM errors WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM file_sources WHERE source_id = ?", (source_id,))
    if plan.file_ids_to_delete:
        placeholders = ",".join("?" for _ in plan.file_ids_to_delete)
        conn.execute(
            f"DELETE FROM file_tags WHERE file_id IN ({placeholders})",
            plan.file_ids_to_delete,
        )
        conn.execute(
            f"DELETE FROM files WHERE id IN ({placeholders})",
            plan.file_ids_to_delete,
        )
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


def duplicate_source_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COALESCE(SUM(source_count - 1), 0)
            FROM (
                SELECT COUNT(*) AS source_count
                FROM file_sources
                GROUP BY file_id
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
    )


def status_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int] | int]:
    media = {"bilder": 0, "videoer": 0}
    for row in conn.execute("SELECT stored_filename FROM files WHERE deleted_at IS NULL"):
        suffix = Path(str(row["stored_filename"])).suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            media["videoer"] += 1
        else:
            media["bilder"] += 1

    date_sources = {
        str(row["date_source"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT date_source, COUNT(*) AS count
            FROM files
            WHERE deleted_at IS NULL
            GROUP BY date_source
            """
        )
    }
    total = sum(media.values())
    return {"total": total, "media": media, "date_sources": date_sources}


def name_conflicts(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        WITH primary_sources AS (
            SELECT
                file_sources.*,
                ROW_NUMBER() OVER (
                    PARTITION BY file_id
                    ORDER BY id
                ) AS source_rank
            FROM file_sources
        )
        SELECT
            primary_sources.source_path,
            files.target_path,
            files.original_filename,
            files.stored_filename
        FROM files
        JOIN primary_sources ON primary_sources.file_id = files.id
            AND primary_sources.source_rank = 1
        WHERE files.name_conflict = 1
          AND files.deleted_at IS NULL
        ORDER BY files.imported_at, files.id
        """
    )


def file_by_target_path(conn: sqlite3.Connection, target: Path, target_path: Path) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM files WHERE target_path_key = ?",
        (target_relative_path_key(target, target_path),),
    ).fetchone()


def file_sources_by_target_path(conn: sqlite3.Connection, target: Path, target_path: Path) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
        SELECT
            files.id AS file_id,
            file_sources.source_id,
            file_sources.source_path,
            files.target_path,
            files.original_filename,
            files.stored_filename,
            files.sha256,
            files.size_bytes,
            files.taken_date,
            files.date_source,
            files.manual_date_from,
            files.manual_date_to,
            files.manual_date_note,
            files.name_conflict,
            files.imported_at AS file_imported_at,
            sources.path AS source_root,
            sources.name AS source_name,
            sources.status AS source_status,
            sources.imported_at AS source_imported_at
        FROM files
        JOIN file_sources ON file_sources.file_id = files.id
        JOIN sources ON sources.id = file_sources.source_id
        WHERE files.target_path_key = ?
        ORDER BY
            file_sources.id
        """,
            (target_relative_path_key(target, target_path),),
        )
    )


def files_by_original_filename(
    conn: sqlite3.Connection, original_filename: str
) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        WITH primary_sources AS (
            SELECT
                file_sources.*,
                ROW_NUMBER() OVER (
                    PARTITION BY file_id
                    ORDER BY id
                ) AS source_rank
            FROM file_sources
        )
        SELECT
            files.id,
            primary_sources.source_id,
            primary_sources.source_path,
            files.target_path,
            files.original_filename,
            files.stored_filename,
            files.sha256,
            files.size_bytes,
            files.taken_date,
            files.date_source,
            files.name_conflict,
            files.imported_at
        FROM files
        JOIN primary_sources ON primary_sources.file_id = files.id
            AND primary_sources.source_rank = 1
        WHERE original_filename = ?
          AND deleted_at IS NULL
        ORDER BY files.imported_at, files.id
        """,
        (original_filename,),
    )


def metadata_refresh_files(conn: sqlite3.Connection, *, rescan: bool = False) -> Iterable[sqlite3.Row]:
    date_filter = "" if rescan else "AND date_source != 'metadata'"
    return conn.execute(
        f"""
        WITH primary_sources AS (
            SELECT
                file_sources.*,
                ROW_NUMBER() OVER (
                    PARTITION BY file_id
                    ORDER BY id
                ) AS source_rank
            FROM file_sources
        )
        SELECT
            files.id,
            primary_sources.source_path,
            files.target_path,
            files.taken_date,
            files.date_source,
            files.sha256,
            files.stored_filename
        FROM files
        JOIN primary_sources ON primary_sources.file_id = files.id
            AND primary_sources.source_rank = 1
        WHERE deleted_at IS NULL
          {date_filter}
        ORDER BY files.date_source, files.taken_date, files.target_path
        """
    )


def non_metadata_files(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return metadata_refresh_files(conn)


def deleted_files(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        WITH primary_sources AS (
            SELECT
                file_sources.*,
                ROW_NUMBER() OVER (
                    PARTITION BY file_id
                    ORDER BY id
                ) AS source_rank
            FROM file_sources
        )
        SELECT
            files.id,
            primary_sources.source_path,
            files.target_path,
            files.deleted_original_target_path,
            files.deleted_at,
            files.stored_filename,
            files.size_bytes,
            files.taken_date,
            files.date_source,
            files.sha256
        FROM files
        JOIN primary_sources ON primary_sources.file_id = files.id
            AND primary_sources.source_rank = 1
        WHERE deleted_at IS NOT NULL
        ORDER BY files.deleted_at, files.deleted_original_target_path, files.target_path
        """
    )
