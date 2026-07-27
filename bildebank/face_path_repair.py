from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db
from .collection_paths import (
    CollectionFileHashError,
    hash_stable_collection_file,
    parse_collection_relative_path,
)
from .face import (
    face_database_model,
    require_current_face_schema_read_only,
)
from .item_sidecars import (
    attach_existing_face_databases,
    existing_face_database_paths,
)
from .sidecar_paths import (
    sqlite_read_write_uri,
    validate_regular_database_file,
)


READ_ONLY_MAIN_ALIAS = "main_db"


@dataclass(frozen=True)
class FacePathRepairGroup:
    database_path: Path
    model_name: str
    file_id: int
    stored_target_path: Path
    expected_target_path: Path
    expected_target_path_key: str
    expected_sha256: str
    expected_size_bytes: int
    scanned_file_rows: int
    face_rows: int


@dataclass(frozen=True)
class FacePathRepairStats:
    database_count: int
    repairable_files: int = 0
    repairable_scanned_file_rows: int = 0
    repairable_face_rows: int = 0
    unsafe_issue_rows: int = 0
    groups: tuple[FacePathRepairGroup, ...] = ()
    updated_scanned_file_rows: int = 0
    updated_face_rows: int = 0
    applied: bool = False


def repair_face_paths(
    target: Path,
    *,
    apply: bool = False,
) -> FacePathRepairStats:
    try:
        validate_face_path_repair_main_database(target)
        face_paths = existing_face_database_paths(target)
        plans = tuple(
            plan_face_database_path_repair(
                target,
                path,
                main_alias=READ_ONLY_MAIN_ALIAS,
            )
            for path in face_paths
        )
        groups = tuple(group for plan in plans for group in plan.groups)
        unsafe_issue_rows = sum(plan.unsafe_issue_rows for plan in plans)
        stats = repair_stats(
            len(face_paths),
            groups,
            unsafe_issue_rows=unsafe_issue_rows,
        )
        if not face_paths or unsafe_issue_rows:
            return stats

        validate_repairable_face_files(target, groups)
        if not apply:
            return stats

        return apply_face_path_repairs(
            target,
            face_paths,
            expected_groups=groups,
        )
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        raise ValueError(
            "InsightFace-stiene kan ikke repareres trygt. "
            f"Ingen face-stier ble endret: {exc}"
        ) from exc


@dataclass(frozen=True)
class _FaceDatabaseRepairPlan:
    groups: tuple[FacePathRepairGroup, ...]
    unsafe_issue_rows: int


def plan_face_database_path_repair(
    target: Path,
    path: Path,
    *,
    main_alias: str,
) -> _FaceDatabaseRepairPlan:
    validate_regular_database_file(path)
    model_name, allow_missing_model_name = face_database_model(path)
    conn = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        require_current_face_schema_read_only(
            conn,
            model_name,
            allow_missing_model_name=allow_missing_model_name,
        )
        db.validate_database_health(conn)
        attach_main_database_read_only(conn, target, alias=main_alias)
        groups = repairable_face_path_groups(
            conn,
            database_path=path,
            model_name=model_name,
            face_database=None,
            main_database=main_alias,
        )
        unsafe_issue_rows = count_unsafe_face_repair_issues(
            conn,
            model_name=model_name,
            face_database=None,
            main_database=main_alias,
        )
        return _FaceDatabaseRepairPlan(
            groups=groups,
            unsafe_issue_rows=unsafe_issue_rows,
        )
    finally:
        conn.close()


def validate_face_path_repair_main_database(target: Path) -> None:
    validate_regular_database_file(db.db_path_for_target(target))
    conn = db.connect_read_only(target)
    try:
        db.validate_database_health(conn)
        path_issues = db.file_path_integrity_issues(conn)
        if path_issues:
            first = path_issues[0]
            raise ValueError(
                "hoveddatabasen har databaseførte stifeil; "
                f"første avvik er file #{first.file_id} {first.field}: "
                f"{first.message}"
            )
        pending_moves = db.prepared_pending_file_moves(conn)
        if pending_moves:
            raise ValueError(
                f"hoveddatabasen har {len(pending_moves)} uavklart(e) "
                "filflytting(er)"
            )
    finally:
        conn.close()


def attach_main_database_read_only(
    conn: sqlite3.Connection,
    target: Path,
    *,
    alias: str,
) -> None:
    uri = f"{db.db_path_for_target(target).resolve().as_uri()}?mode=ro"
    conn.execute(f"ATTACH DATABASE ? AS {alias}", (uri,))


def repairable_face_path_groups(
    conn: sqlite3.Connection,
    *,
    database_path: Path,
    model_name: str,
    face_database: str | None,
    main_database: str,
) -> tuple[FacePathRepairGroup, ...]:
    scanned_files = qualified_table(face_database, "scanned_files")
    faces = qualified_table(face_database, "faces")
    files = qualified_table(main_database, "files")
    rows = conn.execute(
        f"""
        SELECT
            scanned_files.file_id,
            scanned_files.target_path AS stored_target_path,
            files.target_path AS expected_target_path,
            files.target_path_key AS expected_target_path_key,
            files.sha256 AS expected_sha256,
            files.size_bytes AS expected_size_bytes,
            CASE
                WHEN scanned_files.target_path <> files.target_path
                  OR scanned_files.target_path_key <> files.target_path_key
                THEN 1 ELSE 0
            END AS scanned_file_rows,
            COALESCE(SUM(
                CASE
                    WHEN faces.target_path_key <> files.target_path_key
                    THEN 1 ELSE 0
                END
            ), 0) AS face_rows
        FROM {scanned_files} AS scanned_files
        JOIN {files} AS files ON files.id = scanned_files.file_id
        LEFT JOIN {faces} AS faces ON faces.file_id = scanned_files.file_id
        WHERE files.deleted_at IS NULL
          AND scanned_files.sha256 = files.sha256
        GROUP BY
            scanned_files.file_id,
            scanned_files.target_path,
            scanned_files.target_path_key,
            files.target_path,
            files.target_path_key,
            files.sha256,
            files.size_bytes
        HAVING scanned_file_rows <> 0 OR face_rows <> 0
        ORDER BY scanned_files.file_id
        """
    )
    return tuple(
        FacePathRepairGroup(
            database_path=database_path,
            model_name=model_name,
            file_id=int(row["file_id"]),
            stored_target_path=Path(str(row["stored_target_path"])),
            expected_target_path=Path(str(row["expected_target_path"])),
            expected_target_path_key=str(row["expected_target_path_key"]),
            expected_sha256=str(row["expected_sha256"]),
            expected_size_bytes=int(row["expected_size_bytes"]),
            scanned_file_rows=int(row["scanned_file_rows"]),
            face_rows=int(row["face_rows"]),
        )
        for row in rows
    )


def count_unsafe_face_repair_issues(
    conn: sqlite3.Connection,
    *,
    model_name: str,
    face_database: str | None,
    main_database: str,
) -> int:
    scanned_files = qualified_table(face_database, "scanned_files")
    faces = qualified_table(face_database, "faces")
    person_files = qualified_table(face_database, "person_files")
    person_faces = qualified_table(face_database, "person_faces")
    face_suggestions = qualified_table(face_database, "face_suggestions")
    files = qualified_table(main_database, "files")

    unsafe = 0
    for table in (scanned_files, faces, person_files):
        unsafe += scalar_count(
            conn,
            f"""
            SELECT COUNT(*)
            FROM {table} AS candidate
            LEFT JOIN {files} AS files ON files.id = candidate.file_id
            WHERE files.id IS NULL OR files.deleted_at IS NOT NULL
            """,
        )
    unsafe += scalar_count(
        conn,
        f"""
        SELECT COUNT(*)
        FROM {scanned_files} AS scanned_files
        JOIN {files} AS files ON files.id = scanned_files.file_id
        WHERE files.deleted_at IS NULL
          AND scanned_files.sha256 <> files.sha256
        """,
    )
    unsafe += scalar_count(
        conn,
        f"SELECT COUNT(*) FROM {faces} WHERE embedding_model <> ?",
        (model_name,),
    )
    unsafe += scalar_count(
        conn,
        f"""
        SELECT COUNT(*)
        FROM {faces} AS faces
        JOIN {files} AS files ON files.id = faces.file_id
        LEFT JOIN {scanned_files} AS scanned_files
          ON scanned_files.file_id = faces.file_id
        WHERE files.deleted_at IS NULL
          AND faces.target_path_key <> files.target_path_key
          AND (
                scanned_files.file_id IS NULL
             OR scanned_files.sha256 <> files.sha256
          )
        """,
    )
    unsafe += scalar_count(
        conn,
        f"""
        SELECT COUNT(*)
        FROM {person_faces} AS person_faces
        LEFT JOIN {faces} AS faces ON faces.id = person_faces.face_id
        WHERE faces.id IS NULL
        """,
    )
    unsafe += scalar_count(
        conn,
        f"""
        SELECT COUNT(*)
        FROM {face_suggestions} AS suggestions
        LEFT JOIN {faces} AS faces ON faces.id = suggestions.face_id
        WHERE faces.id IS NULL
        """,
    )
    unsafe += scalar_count(
        conn,
        f"""
        SELECT COUNT(*)
        FROM {face_suggestions} AS suggestions
        LEFT JOIN {faces} AS faces
          ON faces.id = suggestions.reference_face_id
        WHERE suggestions.reference_face_id IS NOT NULL
          AND faces.id IS NULL
        """,
    )
    unsafe += scalar_count(
        conn,
        f"""
        WITH actual AS (
            SELECT file_id, COUNT(*) AS face_count
            FROM {faces}
            GROUP BY file_id
        )
        SELECT COUNT(*)
        FROM {scanned_files} AS scanned_files
        LEFT JOIN actual ON actual.file_id = scanned_files.file_id
        WHERE scanned_files.face_count <> COALESCE(actual.face_count, 0)
        """,
    )
    unsafe += scalar_count(
        conn,
        f"""
        SELECT COUNT(*)
        FROM (
            SELECT faces.file_id
            FROM {faces} AS faces
            LEFT JOIN {scanned_files} AS scanned_files
              ON scanned_files.file_id = faces.file_id
            WHERE scanned_files.file_id IS NULL
            GROUP BY faces.file_id
        )
        """,
    )
    return unsafe


def scalar_count(
    conn: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> int:
    return int(conn.execute(sql, parameters).fetchone()[0])


def qualified_table(database: str | None, table: str) -> str:
    if database is None:
        return table
    if not database.startswith(("face_db_", "main")):
        raise ValueError(f"Ugyldig internt databasenavn: {database}")
    return f"{database}.{table}"


def validate_repairable_face_files(
    target: Path,
    groups: tuple[FacePathRepairGroup, ...],
) -> None:
    expected_by_file: dict[int, FacePathRepairGroup] = {}
    for group in groups:
        previous = expected_by_file.setdefault(group.file_id, group)
        if (
            previous.expected_target_path != group.expected_target_path
            or previous.expected_target_path_key
            != group.expected_target_path_key
            or previous.expected_sha256 != group.expected_sha256
            or previous.expected_size_bytes != group.expected_size_bytes
        ):
            raise ValueError(
                f"file #{group.file_id} har motstridende reparasjonsgrunnlag"
            )

    for group in expected_by_file.values():
        relative_path = parse_collection_relative_path(
            group.expected_target_path.as_posix()
        )
        try:
            actual_sha256, actual_size = hash_stable_collection_file(
                target,
                relative_path,
            )
        except (CollectionFileHashError, OSError) as exc:
            raise ValueError(
                f"file #{group.file_id} kunne ikke hashes stabilt: {exc}"
            ) from exc
        if actual_size != group.expected_size_bytes:
            raise ValueError(
                f"file #{group.file_id} har feil størrelse på disk "
                f"(database={group.expected_size_bytes}, disk={actual_size})"
            )
        if actual_sha256 != group.expected_sha256:
            raise ValueError(
                f"file #{group.file_id} har SHA-256-avvik mellom "
                "hoveddatabasen og filen på disk"
            )


def apply_face_path_repairs(
    target: Path,
    face_paths: tuple[Path, ...],
    *,
    expected_groups: tuple[FacePathRepairGroup, ...],
) -> FacePathRepairStats:
    conn = sqlite3.connect(
        sqlite_read_write_uri(db.db_path_for_target(target)),
        uri=True,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        db.validate_current_schema(conn)
        db.validate_database_health(conn)
        attached = attach_existing_face_databases(
            conn,
            target,
            prepare_schema=False,
        )
        attached_paths = tuple(path for _alias, path in attached)
        if attached_paths != face_paths:
            raise ValueError(
                "oversikten over InsightFace-databaser endret seg under "
                "reparasjonen"
            )

        conn.execute("BEGIN IMMEDIATE")
        current_groups: list[FacePathRepairGroup] = []
        unsafe_issue_rows = 0
        for alias, path in attached:
            model_name, _allow_missing = face_database_model(path)
            current_groups.extend(
                repairable_face_path_groups(
                    conn,
                    database_path=path,
                    model_name=model_name,
                    face_database=alias,
                    main_database="main",
                )
            )
            unsafe_issue_rows += count_unsafe_face_repair_issues(
                conn,
                model_name=model_name,
                face_database=alias,
                main_database="main",
            )
        groups = tuple(current_groups)
        if unsafe_issue_rows:
            raise ValueError(
                f"{unsafe_issue_rows} avvik er ikke rene, reparerbare stiavvik"
            )
        if groups != expected_groups:
            raise ValueError(
                "reparasjonsplanen endret seg før apply kunne starte"
            )
        validate_repairable_face_files(target, groups)

        updated_scanned = 0
        updated_faces = 0
        for alias, path in attached:
            model_name, _allow_missing = face_database_model(path)
            scanned_rows, face_rows = update_repairable_face_paths(
                conn,
                model_name=model_name,
                face_database=alias,
                main_database="main",
            )
            updated_scanned += scanned_rows
            updated_faces += face_rows

        expected_scanned = sum(group.scanned_file_rows for group in groups)
        expected_faces = sum(group.face_rows for group in groups)
        if (
            updated_scanned != expected_scanned
            or updated_faces != expected_faces
        ):
            raise ValueError(
                "InsightFace-stireparasjonen endret et uventet antall rader "
                f"(planlagt scanned_files={expected_scanned}, "
                f"faces={expected_faces}; endret "
                f"scanned_files={updated_scanned}, faces={updated_faces})"
            )
        for alias, path in attached:
            model_name, _allow_missing = face_database_model(path)
            remaining = repairable_face_path_groups(
                conn,
                database_path=path,
                model_name=model_name,
                face_database=alias,
                main_database="main",
            )
            if remaining:
                raise ValueError(
                    f"{path.name} har fortsatt reparerbare stiavvik"
                )
            validate_attached_database_health(conn, alias)
        db.validate_database_health(conn)
        conn.commit()
        return repair_stats(
            len(face_paths),
            groups,
            unsafe_issue_rows=0,
            updated_scanned_file_rows=updated_scanned,
            updated_face_rows=updated_faces,
            applied=True,
        )
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_repairable_face_paths(
    conn: sqlite3.Connection,
    *,
    model_name: str,
    face_database: str,
    main_database: str,
) -> tuple[int, int]:
    scanned_files = qualified_table(face_database, "scanned_files")
    faces = qualified_table(face_database, "faces")
    files = qualified_table(main_database, "files")
    scanned_cursor = conn.execute(
        f"""
        UPDATE {scanned_files} AS scanned_files
        SET
            target_path = (
                SELECT files.target_path
                FROM {files} AS files
                WHERE files.id = scanned_files.file_id
            ),
            target_path_key = (
                SELECT files.target_path_key
                FROM {files} AS files
                WHERE files.id = scanned_files.file_id
            )
        WHERE EXISTS (
            SELECT 1
            FROM {files} AS files
            WHERE files.id = scanned_files.file_id
              AND files.deleted_at IS NULL
              AND files.sha256 = scanned_files.sha256
              AND (
                    files.target_path <> scanned_files.target_path
                 OR files.target_path_key <> scanned_files.target_path_key
              )
        )
        """
    )
    faces_cursor = conn.execute(
        f"""
        UPDATE {faces} AS faces
        SET target_path_key = (
            SELECT files.target_path_key
            FROM {files} AS files
            WHERE files.id = faces.file_id
        )
        WHERE faces.embedding_model = ?
          AND EXISTS (
              SELECT 1
              FROM {files} AS files
              JOIN {scanned_files} AS scanned_files
                ON scanned_files.file_id = files.id
              WHERE files.id = faces.file_id
                AND files.deleted_at IS NULL
                AND files.sha256 = scanned_files.sha256
                AND files.target_path_key <> faces.target_path_key
          )
        """,
        (model_name,),
    )
    return max(scanned_cursor.rowcount, 0), max(faces_cursor.rowcount, 0)


def validate_attached_database_health(
    conn: sqlite3.Connection,
    alias: str,
) -> None:
    check_rows = list(conn.execute(f"PRAGMA {alias}.integrity_check"))
    if [str(row[0]) for row in check_rows] != ["ok"]:
        raise ValueError(
            f"SQLite integrity_check feilet for tilkoblet database {alias}"
        )
    foreign_key_rows = list(conn.execute(f"PRAGMA {alias}.foreign_key_check"))
    if foreign_key_rows:
        raise ValueError(
            f"SQLite foreign_key_check feilet for tilkoblet database {alias}"
        )


def repair_stats(
    database_count: int,
    groups: tuple[FacePathRepairGroup, ...],
    *,
    unsafe_issue_rows: int,
    updated_scanned_file_rows: int = 0,
    updated_face_rows: int = 0,
    applied: bool = False,
) -> FacePathRepairStats:
    return FacePathRepairStats(
        database_count=database_count,
        repairable_files=len(groups),
        repairable_scanned_file_rows=sum(
            group.scanned_file_rows for group in groups
        ),
        repairable_face_rows=sum(group.face_rows for group in groups),
        unsafe_issue_rows=unsafe_issue_rows,
        groups=groups,
        updated_scanned_file_rows=updated_scanned_file_rows,
        updated_face_rows=updated_face_rows,
        applied=applied,
    )
