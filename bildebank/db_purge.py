from __future__ import annotations

import sqlite3

from .value_parsing import optional_int


def deleted_file_purge_rows(
    conn: sqlite3.Connection,
    *,
    file_id: int | None = None,
) -> list[sqlite3.Row]:
    where = "AND files.id = ?" if file_id is not None else ""
    parameters: tuple[object, ...] = (file_id,) if file_id is not None else ()
    return list(
        conn.execute(
            f"""
            SELECT
                files.id,
                files.target_path,
                files.original_filename,
                files.stored_filename,
                files.sha256,
                files.size_bytes,
                files.deleted_at,
                files.deleted_original_target_path,
                pending_file_purges.id AS purge_id
            FROM files
            LEFT JOIN pending_file_purges
              ON pending_file_purges.file_id = files.id
            WHERE files.deleted_at IS NOT NULL
              {where}
            ORDER BY files.id
            """,
            parameters,
        )
    )


def file_tombstones(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT id, sha256, size_bytes, original_filename,
                   former_target_path, purged_at
            FROM file_tombstones
            ORDER BY purged_at DESC, id DESC
            """
        )
    )


def file_tombstone(
    conn: sqlite3.Connection,
    *,
    tombstone_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, sha256, size_bytes, original_filename,
               former_target_path, purged_at
        FROM file_tombstones
        WHERE id = ?
        """,
        (tombstone_id,),
    ).fetchone()


def file_tombstone_by_sha256(
    conn: sqlite3.Connection,
    *,
    sha256: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, sha256, size_bytes, original_filename,
               former_target_path, purged_at
        FROM file_tombstones
        WHERE sha256 = ?
        """,
        (sha256,),
    ).fetchone()


def remove_file_tombstone(
    conn: sqlite3.Connection,
    *,
    tombstone_id: int,
) -> None:
    cursor = conn.execute(
        "DELETE FROM file_tombstones WHERE id = ?",
        (tombstone_id,),
    )
    if cursor.rowcount != 1:
        raise ValueError("Tombstonen finnes ikke.")


def pending_file_purges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT id, file_id, expected_path, expected_sha256,
                   expected_size_bytes, expected_deleted_at,
                   original_filename, former_target_path, attempts,
                   last_error, created_at, updated_at
            FROM pending_file_purges
            ORDER BY created_at, id
            """
        )
    )


def pending_file_purge(
    conn: sqlite3.Connection,
    *,
    purge_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, file_id, expected_path, expected_sha256,
               expected_size_bytes, expected_deleted_at,
               original_filename, former_target_path, attempts,
               last_error, created_at, updated_at
        FROM pending_file_purges
        WHERE id = ?
        """,
        (purge_id,),
    ).fetchone()


def pending_file_purge_for_file(
    conn: sqlite3.Connection,
    *,
    file_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, file_id, expected_path, expected_sha256,
               expected_size_bytes, expected_deleted_at,
               original_filename, former_target_path, attempts,
               last_error, created_at, updated_at
        FROM pending_file_purges
        WHERE file_id = ?
        """,
        (file_id,),
    ).fetchone()


def pending_file_purge_for_sha256(
    conn: sqlite3.Connection,
    *,
    sha256: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            pending_file_purges.id,
            pending_file_purges.file_id,
            pending_file_purges.expected_path,
            pending_file_purges.expected_sha256,
            pending_file_purges.expected_size_bytes,
            pending_file_purges.expected_deleted_at,
            pending_file_purges.original_filename,
            pending_file_purges.former_target_path,
            pending_file_purges.attempts,
            pending_file_purges.last_error,
            pending_file_purges.created_at,
            pending_file_purges.updated_at
        FROM pending_file_purges
        WHERE expected_sha256 = ?
        """,
        (sha256,),
    ).fetchone()


def require_no_pending_file_purge(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    operation: str,
) -> None:
    pending = pending_file_purge_for_file(conn, file_id=file_id)
    if pending is not None:
        raise ValueError(
            f"Kan ikke {operation}; filen har ventende permanent sletting "
            f"(purge #{int(pending['id'])})."
        )


def pending_file_purge_identity(
    conn: sqlite3.Connection,
    *,
    purge_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            pending_file_purges.id,
            pending_file_purges.file_id,
            pending_file_purges.expected_path,
            pending_file_purges.expected_sha256,
            pending_file_purges.expected_size_bytes,
            pending_file_purges.expected_deleted_at,
            pending_file_purges.original_filename,
            pending_file_purges.former_target_path,
            pending_file_purges.attempts,
            pending_file_purges.last_error,
            pending_file_purges.created_at,
            pending_file_purges.updated_at,
            files.target_path AS file_target_path,
            files.stored_filename AS file_stored_filename,
            files.sha256 AS file_sha256,
            files.size_bytes AS file_size_bytes,
            files.deleted_at AS file_deleted_at,
            files.original_filename AS file_original_filename,
            files.deleted_original_target_path AS file_former_target_path
        FROM pending_file_purges
        LEFT JOIN files ON files.id = pending_file_purges.file_id
        WHERE pending_file_purges.id = ?
        """,
        (purge_id,),
    ).fetchone()


def create_pending_file_purge(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    expected_path: str,
    expected_sha256: str,
    expected_size_bytes: int,
    expected_deleted_at: str,
) -> int:
    file_row = conn.execute(
        """
        SELECT target_path, sha256, size_bytes, deleted_at,
               original_filename, deleted_original_target_path
        FROM files
        WHERE id = ?
        """,
        (file_id,),
    ).fetchone()
    if file_row is None:
        raise ValueError("Kan ikke journalføre purge; files-raden finnes ikke.")
    expected = (
        expected_path,
        expected_sha256,
        expected_size_bytes,
        expected_deleted_at,
    )
    actual = (
        file_row["target_path"],
        file_row["sha256"],
        file_row["size_bytes"],
        file_row["deleted_at"],
    )
    if actual != expected:
        raise ValueError(
            "Kan ikke journalføre purge; filidentiteten er endret."
        )
    former_target_path = file_row["deleted_original_target_path"]
    if file_row["deleted_at"] is None or former_target_path is None:
        raise ValueError(
            "Kan ikke journalføre purge; filen er ikke korrekt markert som slettet."
        )
    if (
        conn.execute(
            "SELECT 1 FROM file_tombstones WHERE sha256 = ?",
            (expected_sha256,),
        ).fetchone()
        is not None
    ):
        raise ValueError(
            "Kan ikke journalføre purge; SHA-256 finnes allerede som tombstone."
        )
    cursor = conn.execute(
        """
        INSERT INTO pending_file_purges(
            file_id, expected_path, expected_sha256, expected_size_bytes,
            expected_deleted_at, original_filename, former_target_path
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            expected_path,
            expected_sha256,
            expected_size_bytes,
            expected_deleted_at,
            str(file_row["original_filename"]),
            str(former_target_path),
        ),
    )
    purge_id = optional_int(cursor.lastrowid, "purge-id")
    if purge_id is None:
        raise ValueError("Databasen returnerte ikke id for purge-posten.")
    return purge_id


def update_pending_file_purge_error(
    conn: sqlite3.Connection,
    *,
    purge_id: int,
    error: str,
) -> None:
    cursor = conn.execute(
        """
        UPDATE pending_file_purges
        SET attempts = attempts + 1,
            last_error = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (error, purge_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("Purge-posten finnes ikke.")


def remove_pending_file_purge(
    conn: sqlite3.Connection,
    *,
    purge_id: int,
) -> None:
    cursor = conn.execute(
        "DELETE FROM pending_file_purges WHERE id = ?",
        (purge_id,),
    )
    if cursor.rowcount != 1:
        raise ValueError("Purge-posten finnes ikke.")


def complete_pending_file_purge(
    conn: sqlite3.Connection,
    *,
    purge_id: int,
) -> int:
    savepoint = "complete_pending_file_purge"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        identity = pending_file_purge_identity(conn, purge_id=purge_id)
        if identity is None:
            raise ValueError("Purge-posten finnes ikke.")
        _validate_pending_file_purge_identity(identity)
        if (
            conn.execute(
                "SELECT 1 FROM file_tombstones WHERE sha256 = ?",
                (identity["expected_sha256"],),
            ).fetchone()
            is not None
        ):
            raise ValueError(
                "Kan ikke fullføre purge; SHA-256 finnes allerede som tombstone."
            )

        cursor = conn.execute(
            "DELETE FROM pending_file_purges WHERE id = ?",
            (purge_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("Purge-posten forsvant under fullføring.")
        conn.execute(
            "DELETE FROM file_sources WHERE file_id = ?",
            (identity["file_id"],),
        )
        cursor = conn.execute(
            "DELETE FROM files WHERE id = ?",
            (identity["file_id"],),
        )
        if cursor.rowcount != 1:
            raise ValueError("Files-raden forsvant under fullføring.")
        row = conn.execute(
            """
            INSERT INTO file_tombstones(
                sha256, size_bytes, original_filename,
                former_target_path, purged_at
            )
            VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
            RETURNING id
            """,
            (
                identity["expected_sha256"],
                identity["expected_size_bytes"],
                identity["original_filename"],
                identity["former_target_path"],
            ),
        ).fetchone()
        if row is None:
            raise ValueError("Databasen returnerte ikke id for tombstonen.")
        tombstone_id = int(row["id"])
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return tombstone_id
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def _validate_pending_file_purge_identity(identity: sqlite3.Row) -> None:
    if identity["file_target_path"] is None:
        raise ValueError("Kan ikke fullføre purge; files-raden mangler.")
    expected_to_actual = {
        "expected_path": "file_target_path",
        "expected_sha256": "file_sha256",
        "expected_size_bytes": "file_size_bytes",
        "expected_deleted_at": "file_deleted_at",
        "original_filename": "file_original_filename",
        "former_target_path": "file_former_target_path",
    }
    if any(
        identity[expected] != identity[actual]
        for expected, actual in expected_to_actual.items()
    ):
        raise ValueError(
            "Kan ikke fullføre purge; purge-posten stemmer ikke med files-raden."
        )
