from __future__ import annotations

import array
import html
import math
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import OpenClipConfig
from .html_paths import display_relative_path, path_to_url, relative_to_target
from .media import IMAGE_EXTENSIONS
from .media_cache import cached_image_dimensions
from .target_lock import TargetLock, TargetLockError
from .value_parsing import optional_int


OPENCLIP_DB_FILENAME = ".bilder-openclip.sqlite3"
OPENCLIP_SCHEMA_VERSION = 1
MAIN_DB_ALIAS = "main_db"
IMAGE_SCAN_RUN_META_KEY = "image_scan_run_id"
OPENCLIP_SCHEMA_REQUIRED_COLUMNS = {
    "meta": {"key", "value"},
    "image_embeddings": {
        "file_id",
        "target_path",
        "target_path_key",
        "sha256",
        "model_name",
        "pretrained",
        "embedding",
        "created_at",
        "updated_at",
    },
    "image_search_runs": {
        "id",
        "query",
        "model_name",
        "pretrained",
        "result_limit",
        "created_at",
    },
    "image_search_results": {
        "run_id",
        "file_id",
        "target_path",
        "target_path_key",
        "similarity",
        "rank",
    },
}


@dataclass(frozen=True)
class OpenClipDbSummary:
    exists: bool
    embeddings: int
    search_runs: int
    search_results: int


@dataclass(frozen=True)
class ImageScanStats:
    total: int
    checked: int = 0
    to_scan: int = 0
    skipped: int = 0
    scanned: int = 0
    errors: int = 0
    last_error_path: Path | None = None
    last_error_message: str | None = None


@dataclass(frozen=True)
class ImageSearchResult:
    rank: int
    file_id: int
    target_path: Path
    similarity: float


@dataclass(frozen=True)
class ImageSearchStats:
    query: str
    run_id: int
    results: tuple[ImageSearchResult, ...]
    output_path: Path


@dataclass(frozen=True)
class ImageSearchProgressStats:
    query: str
    compared: int = 0
    total: int = 0


@dataclass(frozen=True)
class OpenClipOrphanGroup:
    table: str
    file_id: int
    target_path: Path
    row_count: int


@dataclass(frozen=True)
class OpenClipCleanupStats:
    exists: bool
    embedding_rows: int = 0
    search_result_rows: int = 0
    groups: tuple[OpenClipOrphanGroup, ...] = ()
    deleted_embedding_rows: int = 0
    deleted_search_result_rows: int = 0
    deleted_search_runs: int = 0
    applied: bool = False


def openclip_db_path(target: Path) -> Path:
    return target / OPENCLIP_DB_FILENAME


def connect_openclip_db(target: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(openclip_db_path(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        apply_schema(conn)
        set_meta(conn, "target_path", str(target.resolve()))
        conn.commit()
        return conn
    except BaseException:
        conn.close()
        raise


def ensure_openclip_schema_path(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        apply_schema(conn)
    finally:
        conn.close()


def apply_schema(conn: sqlite3.Connection) -> None:
    version = openclip_schema_version(conn)
    if version == OPENCLIP_SCHEMA_VERSION:
        validate_current_openclip_schema(conn)
        return
    reject_unknown_openclip_schema_version(version)

    conn.execute("BEGIN IMMEDIATE")
    try:
        version = openclip_schema_version(conn)
        if version == OPENCLIP_SCHEMA_VERSION:
            validate_current_openclip_schema(conn)
            conn.commit()
            return
        reject_unknown_openclip_schema_version(version)

        existing_tables = openclip_user_tables(conn)
        if existing_tables:
            validate_openclip_schema_structure(conn, require_meta=False)
            validate_relative_openclip_paths(conn)
            validate_openclip_foreign_keys(conn)
            if not db.table_exists(conn, "meta"):
                create_openclip_meta_schema(conn)
        else:
            create_current_openclip_schema(conn)

        set_meta(conn, "schema_version", str(OPENCLIP_SCHEMA_VERSION))
        validate_current_openclip_schema(conn)
        db.validate_database_health(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def openclip_schema_version(conn: sqlite3.Connection) -> int | None:
    if not db.table_exists(conn, "meta"):
        return None
    validate_openclip_table_columns(conn, "meta", OPENCLIP_SCHEMA_REQUIRED_COLUMNS["meta"])
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return None
    value = row[0]
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Ugyldig schema_version i OpenCLIP-databasen: {value}"
        ) from exc


def reject_unknown_openclip_schema_version(version: int | None) -> None:
    if version is None:
        return
    if version > OPENCLIP_SCHEMA_VERSION:
        raise ValueError(
            "OpenCLIP-databasen bruker et nyere format "
            f"(schema_version={version}) enn programmet støtter "
            f"(schema_version={OPENCLIP_SCHEMA_VERSION})."
        )
    raise ValueError(
        f"Kan ikke migrere OpenCLIP-database med schema_version={version}."
    )


def openclip_user_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        )
    }


def create_openclip_meta_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def create_current_openclip_schema(conn: sqlite3.Connection) -> None:
    db.execute_sql_statements(
        conn,
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE image_embeddings (
            file_id INTEGER NOT NULL,
            target_path TEXT NOT NULL,
            target_path_key TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            model_name TEXT NOT NULL,
            pretrained TEXT NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(file_id, model_name, pretrained)
        );

        CREATE INDEX idx_image_embeddings_model
        ON image_embeddings(model_name, pretrained);

        CREATE TABLE image_search_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            model_name TEXT NOT NULL,
            pretrained TEXT NOT NULL,
            result_limit INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE image_search_results (
            run_id INTEGER NOT NULL REFERENCES image_search_runs(id) ON DELETE CASCADE,
            file_id INTEGER NOT NULL,
            target_path TEXT NOT NULL,
            target_path_key TEXT NOT NULL,
            similarity REAL NOT NULL,
            rank INTEGER NOT NULL,
            PRIMARY KEY(run_id, rank)
        );

        CREATE INDEX idx_image_search_results_run_id
        ON image_search_results(run_id);
        """
    )


def validate_current_openclip_schema(conn: sqlite3.Connection) -> None:
    validate_openclip_schema_structure(conn, require_meta=True)
    validate_relative_openclip_paths(conn)
    validate_openclip_foreign_keys(conn)


def validate_openclip_schema_structure(
    conn: sqlite3.Connection,
    *,
    require_meta: bool,
) -> None:
    required_tables = set(OPENCLIP_SCHEMA_REQUIRED_COLUMNS)
    if not require_meta:
        required_tables.remove("meta")
    missing_tables = sorted(
        table for table in required_tables if not db.table_exists(conn, table)
    )
    if missing_tables:
        raise ValueError(
            "OpenCLIP-databasen mangler forventede tabeller: "
            f"{', '.join(missing_tables)}."
        )
    for table, required_columns in OPENCLIP_SCHEMA_REQUIRED_COLUMNS.items():
        if table == "meta" and not db.table_exists(conn, table):
            continue
        validate_openclip_table_columns(conn, table, required_columns)


def validate_openclip_table_columns(
    conn: sqlite3.Connection,
    table: str,
    required_columns: set[str],
) -> None:
    columns = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    missing_columns = sorted(required_columns - columns)
    if missing_columns:
        raise ValueError(
            f"OpenCLIP-databasen: {table} mangler forventede kolonner: "
            f"{', '.join(missing_columns)}."
        )


def validate_openclip_foreign_keys(conn: sqlite3.Connection) -> None:
    errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if errors:
        raise ValueError(
            f"OpenCLIP-databasens foreign_key_check feilet: {tuple(errors[0])}"
        )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def validate_relative_openclip_paths(conn: sqlite3.Connection) -> None:
    for table in ("image_embeddings", "image_search_results"):
        row = conn.execute(
            f"""
            SELECT file_id, target_path
            FROM {table}
            WHERE target_path LIKE '/%'
               OR target_path GLOB '[A-Za-z]:*'
            ORDER BY file_id
            LIMIT 1
            """
        ).fetchone()
        if row is not None:
            raise ValueError(
                "OpenCLIP-databasen har absolutt target_path i "
                f"{table} for file_id={row['file_id']}: {row['target_path']}. "
                "Kjør bildebank image-scan på nytt."
            )


def attach_main_database(conn: sqlite3.Connection, target: Path) -> None:
    if any(str(row["name"]) == MAIN_DB_ALIAS for row in conn.execute("PRAGMA database_list")):
        return
    main_db_path = db.db_path_for_target(target)
    if main_db_path.exists():
        conn.execute(f"ATTACH DATABASE ? AS {MAIN_DB_ALIAS}", (str(main_db_path),))


def active_embedding_table(conn: sqlite3.Connection) -> str:
    if any(str(row["name"]) == MAIN_DB_ALIAS for row in conn.execute("PRAGMA database_list")):
        return (
            "image_embeddings "
            f"JOIN {MAIN_DB_ALIAS}.files ON {MAIN_DB_ALIAS}.files.id = image_embeddings.file_id "
            f"AND {MAIN_DB_ALIAS}.files.deleted_at IS NULL"
        )
    return "image_embeddings"


def active_image_files(target: Path, *, limit: int | None = None) -> list[sqlite3.Row]:
    conn = db.connect(target)
    try:
        rows: list[sqlite3.Row] = []
        for row in conn.execute(
            """
            SELECT id, target_path, target_path_key, sha256, stored_filename
            FROM files
            WHERE deleted_at IS NULL
            ORDER BY imported_at, id
            """
        ):
            if Path(str(row["stored_filename"])).suffix.lower() in IMAGE_EXTENSIONS:
                rows.append(row)
                if limit is not None and len(rows) >= limit:
                    break
        return rows
    finally:
        conn.close()


ImageScanProgress = Callable[[str, int, int, ImageScanStats, Path | None], None]
ImageSearchProgress = Callable[[str, int, int, ImageSearchProgressStats], None]


def scan_images(
    target: Path,
    config: OpenClipConfig,
    *,
    limit: int | None = None,
    progress: ImageScanProgress | None = None,
) -> ImageScanStats:
    with TargetLock(target, command="image-scan"):
        image_rows = active_image_files(target, limit=limit)
        total = len(image_rows)
        stats = ImageScanStats(total=total)
        if progress is not None:
            progress("start", 0, total, stats, None)
        if not image_rows:
            return stats

        conn = connect_openclip_db(target)
        try:
            scan_run_id = str(uuid.uuid4())
            set_meta(conn, IMAGE_SCAN_RUN_META_KEY, scan_run_id)
            conn.commit()
            rows_to_scan = []
            skipped = 0
            for index, row in enumerate(image_rows, start=1):
                file_id = int(row["id"])
                sha256 = str(row["sha256"])
                if has_current_embedding(conn, file_id, sha256, config):
                    skipped += 1
                else:
                    rows_to_scan.append(row)
                stats = ImageScanStats(
                    total=total,
                    checked=index,
                    to_scan=len(rows_to_scan),
                    skipped=skipped,
                )
                if progress is not None:
                    progress("check", index, total, stats, db.absolute_target_path(target, Path(str(row["target_path"]))))
        finally:
            conn.close()

    if not rows_to_scan:
        if progress is not None:
            progress("done", total, total, stats, None)
        return stats
    if progress is not None:
        progress("load_model", 0, len(rows_to_scan), stats, None)
    model, preprocess = load_image_model(config)
    scanned = 0
    errors = 0
    last_error_path = None
    last_error_message = None
    for index, row in enumerate(rows_to_scan, start=1):
        target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
        try:
            vector = image_embedding(model, preprocess, target_path)
            try:
                stored = store_image_scan_embedding(target, config, row, vector, scan_run_id)
            except TargetLockError:
                stored = False
            if stored:
                scanned += 1
            else:
                skipped += 1
        except Exception as exc:
            errors += 1
            last_error_path = target_path
            last_error_message = str(exc)
            stats = ImageScanStats(
                total=total,
                checked=total,
                to_scan=len(rows_to_scan),
                skipped=skipped,
                scanned=scanned,
                errors=errors,
                last_error_path=last_error_path,
                last_error_message=last_error_message,
            )
            if progress is not None:
                progress("error", index, len(rows_to_scan), stats, target_path)
            continue
        stats = ImageScanStats(
            total=total,
            checked=total,
            to_scan=len(rows_to_scan),
            skipped=skipped,
            scanned=scanned,
            errors=errors,
            last_error_path=last_error_path,
            last_error_message=last_error_message,
        )
        if progress is not None:
            progress("scan", index, len(rows_to_scan), stats, target_path)
    if progress is not None:
        progress("done", len(rows_to_scan), len(rows_to_scan), stats, None)
    return stats


def store_image_scan_embedding(
    target: Path,
    config: OpenClipConfig,
    original_row: sqlite3.Row,
    vector: list[float],
    scan_run_id: str,
) -> bool:
    with TargetLock(target, command="image-scan"):
        main_conn = db.connect(target)
        try:
            current_row = active_file_for_image_scan(main_conn, original_row)
            if current_row is None:
                return False
            conn = connect_openclip_db(target)
            try:
                if get_meta(conn, IMAGE_SCAN_RUN_META_KEY) != scan_run_id:
                    return False
                store_embedding(
                    conn,
                    file_id=int(current_row["id"]),
                    target_root=target,
                    target_path=db.absolute_target_path(target, Path(str(current_row["target_path"]))),
                    target_path_key=str(current_row["target_path_key"]),
                    sha256=str(current_row["sha256"]),
                    config=config,
                    vector=vector,
                )
                conn.commit()
            finally:
                conn.close()
        finally:
            main_conn.close()
    return True


def active_file_for_image_scan(conn: sqlite3.Connection, original_row: sqlite3.Row) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, target_path, target_path_key, sha256
        FROM files
        WHERE id = ?
          AND sha256 = ?
          AND deleted_at IS NULL
        """,
        (int(original_row["id"]), str(original_row["sha256"])),
    ).fetchone()


def search_images(
    target: Path,
    config: OpenClipConfig,
    *,
    query: str,
    limit: int = 100,
    progress: ImageSearchProgress | None = None,
) -> ImageSearchStats:
    with TargetLock(target, command="image-search"):
        return _search_images_unlocked(
            target,
            config,
            query=query,
            limit=limit,
            progress=progress,
        )


def _search_images_unlocked(
    target: Path,
    config: OpenClipConfig,
    *,
    query: str,
    limit: int = 100,
    progress: ImageSearchProgress | None = None,
) -> ImageSearchStats:
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Søketekst kan ikke være tom.")
    if limit < 1:
        raise ValueError("--limit må være minst 1.")

    conn = connect_openclip_db(target)
    try:
        attach_main_database(conn, target)
        rows = list(
            conn.execute(
                f"""
                SELECT
                    image_embeddings.file_id,
                    image_embeddings.target_path,
                    image_embeddings.target_path_key,
                    image_embeddings.embedding
                FROM {active_embedding_table(conn)}
                WHERE image_embeddings.model_name = ? AND image_embeddings.pretrained = ?
                """,
                (config.model_name, config.pretrained),
            )
        )
        if not rows:
            raise ValueError("Fant ingen bilde-embeddings. Kjør bildebank image-scan først.")
        search_progress = ImageSearchProgressStats(clean_query, compared=0, total=len(rows))
        if progress is not None:
            progress("load_model", 0, len(rows), search_progress)
        model, tokenizer = load_text_model(config)
        text_vector = text_embedding(model, tokenizer, clean_query)
        if progress is not None:
            progress("compare_start", 0, len(rows), search_progress)
        scored_items = []
        for index, row in enumerate(rows, start=1):
            scored_items.append(
                (
                    cosine_similarity(text_vector, embedding_from_blob(bytes(row["embedding"]))),
                    int(row["file_id"]),
                    Path(str(row["target_path"])),
                    str(row["target_path_key"]),
                )
            )
            search_progress = ImageSearchProgressStats(clean_query, compared=index, total=len(rows))
            if progress is not None:
                progress("compare", index, len(rows), search_progress)
        scored = sorted(scored_items, reverse=True, key=lambda item: item[0])[:limit]
        if progress is not None:
            progress("write", len(scored), len(scored), search_progress)
        run_id = create_search_run(conn, clean_query, config, limit)
        results: list[ImageSearchResult] = []
        for index, (score, file_id, target_path, target_path_key) in enumerate(scored, start=1):
            conn.execute(
                """
                INSERT INTO image_search_results(run_id, file_id, target_path, target_path_key, similarity, rank)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (run_id, file_id, target_path.as_posix(), target_path_key, score, index),
            )
            results.append(ImageSearchResult(index, file_id, target_path, score))
        conn.commit()
    finally:
        conn.close()

    output_path = export_image_search_html(target, clean_query, config, results)
    if progress is not None:
        progress("done", len(results), len(results), search_progress)
    return ImageSearchStats(clean_query, run_id, tuple(results), output_path)


def openclip_db_summary(target: Path) -> OpenClipDbSummary:
    path = openclip_db_path(target)
    if not path.exists():
        return OpenClipDbSummary(False, 0, 0, 0)
    conn = sqlite3.connect(path)
    try:
        return OpenClipDbSummary(
            exists=True,
            embeddings=count_rows_if_table_exists(conn, "image_embeddings"),
            search_runs=count_rows_if_table_exists(conn, "image_search_runs"),
            search_results=count_rows_if_table_exists(conn, "image_search_results"),
        )
    finally:
        conn.close()


def cleanup_image_search(target: Path, *, apply: bool = False) -> OpenClipCleanupStats:
    path = openclip_db_path(target)
    if not path.exists():
        return OpenClipCleanupStats(exists=False, applied=apply)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        validate_openclip_cleanup_schema(conn)
        attach_main_database(conn, target)
        groups = (
            *orphan_openclip_groups(conn, "image_embeddings"),
            *orphan_openclip_groups(conn, "image_search_results"),
        )
        embedding_rows = sum(
            group.row_count for group in groups if group.table == "image_embeddings"
        )
        search_result_rows = sum(
            group.row_count for group in groups if group.table == "image_search_results"
        )
        if not apply:
            return OpenClipCleanupStats(
                exists=True,
                embedding_rows=embedding_rows,
                search_result_rows=search_result_rows,
                groups=groups,
            )
        deleted_embedding_rows = delete_orphan_openclip_rows(conn, "image_embeddings")
        deleted_search_result_rows = delete_orphan_openclip_rows(conn, "image_search_results")
        deleted_search_runs = delete_empty_search_runs(conn)
        conn.commit()
        return OpenClipCleanupStats(
            exists=True,
            embedding_rows=embedding_rows,
            search_result_rows=search_result_rows,
            groups=groups,
            deleted_embedding_rows=deleted_embedding_rows,
            deleted_search_result_rows=deleted_search_result_rows,
            deleted_search_runs=deleted_search_runs,
            applied=True,
        )
    except sqlite3.DatabaseError as exc:
        conn.rollback()
        raise ValueError(
            "OpenCLIP-databasen kan ikke ryddes automatisk. "
            "Kjør bildebank image-scan på nytt, eller reset bildesøkdatabasen senere. "
            f"SQLite-feil: {exc}"
        ) from exc
    finally:
        conn.close()


def validate_openclip_cleanup_schema(conn: sqlite3.Connection) -> None:
    required_columns = {
        "image_embeddings": {"file_id", "target_path"},
        "image_search_results": {"run_id", "file_id", "target_path"},
        "image_search_runs": {"id"},
    }
    for table, columns in required_columns.items():
        rows = list(conn.execute(f"PRAGMA table_info({table})"))
        if not rows:
            raise ValueError(
                f"OpenCLIP-databasen mangler tabellen {table}. "
                "Kjør bildebank image-scan på nytt, eller reset bildesøkdatabasen senere."
            )
        existing = {str(row["name"]) for row in rows}
        missing = sorted(columns - existing)
        if missing:
            raise ValueError(
                f"OpenCLIP-tabellen {table} mangler kolonne(r): {', '.join(missing)}. "
                "Kjør bildebank image-scan på nytt, eller reset bildesøkdatabasen senere."
            )


def orphan_openclip_groups(conn: sqlite3.Connection, table: str) -> tuple[OpenClipOrphanGroup, ...]:
    if table not in {"image_embeddings", "image_search_results"}:
        raise ValueError(f"Uventet OpenCLIP-tabell: {table}")
    rows = conn.execute(
        f"""
        SELECT {table}.file_id, {table}.target_path, COUNT(*) AS row_count
        FROM {table}
        LEFT JOIN {MAIN_DB_ALIAS}.files ON {MAIN_DB_ALIAS}.files.id = {table}.file_id
        WHERE {MAIN_DB_ALIAS}.files.id IS NULL
           OR {MAIN_DB_ALIAS}.files.deleted_at IS NOT NULL
        GROUP BY {table}.file_id, {table}.target_path
        ORDER BY {table}.file_id, {table}.target_path
        """
    )
    return tuple(
        OpenClipOrphanGroup(
            table=table,
            file_id=int(row["file_id"]),
            target_path=Path(str(row["target_path"])),
            row_count=int(row["row_count"]),
        )
        for row in rows
    )


def delete_orphan_openclip_rows(conn: sqlite3.Connection, table: str) -> int:
    if table not in {"image_embeddings", "image_search_results"}:
        raise ValueError(f"Uventet OpenCLIP-tabell: {table}")
    cursor = conn.execute(
        f"""
        DELETE FROM {table}
        WHERE rowid IN (
            SELECT {table}.rowid
            FROM {table}
            LEFT JOIN {MAIN_DB_ALIAS}.files ON {MAIN_DB_ALIAS}.files.id = {table}.file_id
            WHERE {MAIN_DB_ALIAS}.files.id IS NULL
               OR {MAIN_DB_ALIAS}.files.deleted_at IS NOT NULL
        )
        """
    )
    return cursor.rowcount if cursor.rowcount >= 0 else 0


def delete_empty_search_runs(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        DELETE FROM image_search_runs
        WHERE NOT EXISTS (
            SELECT 1
            FROM image_search_results
            WHERE image_search_results.run_id = image_search_runs.id
        )
        """
    )
    return cursor.rowcount if cursor.rowcount >= 0 else 0


def count_rows_if_table_exists(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        return 0
    return count_rows(conn, table)


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def has_current_embedding(
    conn: sqlite3.Connection,
    file_id: int,
    sha256: str,
    config: OpenClipConfig,
) -> bool:
    row = conn.execute(
        """
        SELECT sha256
        FROM image_embeddings
        WHERE file_id = ? AND model_name = ? AND pretrained = ?
        """,
        (file_id, config.model_name, config.pretrained),
    ).fetchone()
    return row is not None and row["sha256"] == sha256


def store_embedding(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_root: Path,
    target_path: Path,
    target_path_key: str,
    sha256: str,
    config: OpenClipConfig,
    vector: list[float],
) -> None:
    conn.execute(
        """
        INSERT INTO image_embeddings(
            file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_id, model_name, pretrained) DO UPDATE SET
            target_path = excluded.target_path,
            target_path_key = excluded.target_path_key,
            sha256 = excluded.sha256,
            embedding = excluded.embedding,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            file_id,
            db.target_relative_path(target_root, target_path).as_posix(),
            target_path_key,
            sha256,
            config.model_name,
            config.pretrained,
            embedding_blob(vector),
        ),
    )


def create_search_run(
    conn: sqlite3.Connection,
    query: str,
    config: OpenClipConfig,
    limit: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO image_search_runs(query, model_name, pretrained, result_limit)
        VALUES(?, ?, ?, ?)
        """,
        (query, config.model_name, config.pretrained, limit),
    )
    search_run_id = optional_int(cur.lastrowid, "søkejobb-id")
    if search_run_id is None:
        raise ValueError("Databasen returnerte ikke id for søkejobben.")
    return search_run_id


def load_image_model(config: OpenClipConfig) -> tuple[Any, Any]:
    open_clip = import_open_clip()
    device = resolve_torch_device(config.device)
    model, _, preprocess = open_clip.create_model_and_transforms(
        config.model_name,
        pretrained=config.pretrained,
        device=device,
        cache_dir=str(config.model_root),
    )
    model.eval()
    return model, preprocess


def load_text_model(config: OpenClipConfig) -> tuple[Any, Any]:
    open_clip = import_open_clip()
    device = resolve_torch_device(config.device)
    model, _, _ = open_clip.create_model_and_transforms(
        config.model_name,
        pretrained=config.pretrained,
        device=device,
        cache_dir=str(config.model_root),
    )
    model.eval()
    return model, open_clip.get_tokenizer(config.model_name)


def import_open_clip():
    try:
        import open_clip
    except ImportError as exc:
        raise ValueError("OpenCLIP er ikke installert. Kjør install-openclip.ps1 fra programmappen.") from exc
    return open_clip


def image_embedding(model: Any, preprocess: Any, path: Path) -> list[float]:
    try:
        from PIL import Image, ImageOps
        import torch
    except ImportError as exc:
        raise ValueError("OpenCLIP/Pillow mangler. Kjør install-openclip.ps1 fra programmappen.") from exc
    with Image.open(path) as image_file:
        normalized_image = ImageOps.exif_transpose(image_file).convert("RGB")
        tensor = preprocess(normalized_image).unsqueeze(0).to(model_device(model))
    with torch.no_grad():
        embedding = model.encode_image(tensor)
    return normalized_tensor_values(embedding)


def text_embedding(model: Any, tokenizer: Any, query: str) -> list[float]:
    try:
        import torch
    except ImportError as exc:
        raise ValueError("PyTorch mangler. Kjør install-openclip.ps1 fra programmappen.") from exc
    tokens = tokenizer([query]).to(model_device(model))
    with torch.no_grad():
        embedding = model.encode_text(tokens)
    return normalized_tensor_values(embedding)


def resolve_torch_device(configured_device: str = "auto") -> str:
    device = configured_device.strip().lower()
    if device == "auto":
        try:
            import torch
        except ImportError:
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"
    if device in {"cpu", "cuda"}:
        return device
    raise ValueError("OpenCLIP device må være 'auto', 'cpu' eller 'cuda'.")


def model_device(model: Any) -> str:
    try:
        return str(next(model.parameters()).device)
    except StopIteration:
        return "cpu"


def torch_gpu_status() -> dict[str, str]:
    try:
        import torch
    except ImportError:
        return {"torch": "nei", "cuda": "nei", "device": "-"}
    cuda_available = torch.cuda.is_available()
    device_name = "-"
    if cuda_available:
        try:
            device_name = torch.cuda.get_device_name(0)
        except Exception:
            device_name = "cuda"
    return {
        "torch": "ja",
        "cuda": "ja" if cuda_available else "nei",
        "device": device_name,
    }


def normalized_tensor_values(tensor: Any) -> list[float]:
    tensor = tensor / tensor.norm(dim=-1, keepdim=True)
    values = tensor.squeeze(0).detach().cpu().tolist()
    return [float(value) for value in values]


def embedding_blob(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def embedding_from_blob(blob: bytes) -> list[float]:
    values = array.array("f")
    values.frombytes(blob)
    return list(values)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def export_image_search_html(
    target: Path,
    query: str,
    config: OpenClipConfig,
    results: list[ImageSearchResult],
) -> Path:
    output_path = target / "image-search.html"
    items = "\n".join(image_result_html(target, result) for result in results)
    output_path.write_text(
        f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bildesøk: {html.escape(query)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #f7f7f7; color: #222; }}
    h1 {{ margin-bottom: 0.25rem; }}
    .meta {{ color: #555; margin-bottom: 1.5rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 1rem; }}
    .item {{ background: white; border: 1px solid #ddd; border-radius: 6px; overflow: hidden; }}
    .item img {{ width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block; background: #eee; }}
    .text {{ padding: 0.75rem; font-size: 0.9rem; }}
    .path {{ overflow-wrap: anywhere; }}
    .score {{ color: #555; margin-top: 0.35rem; }}
  </style>
</head>
<body>
  <h1>Bildesøk: {html.escape(query)}</h1>
  <div class="meta">{len(results)} treff med {html.escape(config.model_name)} ({html.escape(config.pretrained)}). Sortert med beste match først.</div>
  <div class="grid">
{items}
  </div>
</body>
</html>
""",
        encoding="utf-8",
    )
    return output_path


def image_result_html(target: Path, result: ImageSearchResult) -> str:
    relative = relative_to_target(target, result.target_path)
    url = path_to_url(relative)
    dimensions = cached_image_dimensions(
        target,
        db.absolute_target_path(target, result.target_path),
        target_locked=True,
    )
    size = f"{dimensions.width} x {dimensions.height}" if dimensions else "-"
    path_text = display_relative_path(target, result.target_path)
    return f"""    <div class="item">
      <a href="{html.escape(url)}"><img src="{html.escape(url)}" alt=""></a>
      <div class="text">
        <div class="path">#{result.rank} {html.escape(path_text)}</div>
        <div class="score">score={result.similarity:.3f} · {html.escape(size)}</div>
      </div>
    </div>"""
