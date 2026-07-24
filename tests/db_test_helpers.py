from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from bildebank import db
from bildebank.config import FaceRecognitionConfig
from bildebank.db import DB_FILENAME
from bildebank.face import connect_face_db
from bildebank.media import sha256_file
from bildebank.openclip import connect_openclip_db, embedding_blob
from tests.test_media import minimal_png


def insert_test_file(
    target: Path,
    relative_path: str,
    *,
    sha256: str | None = None,
    deleted: bool = False,
    gps_scanned: bool = False,
) -> int:
    file_path = target / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if not file_path.exists():
        file_path.write_bytes(minimal_png(10 + len(relative_path), 10))
    if sha256 is None:
        sha256 = uuid.uuid4().hex
    conn = db.connect(target)
    try:
        row = conn.execute(
            """
            INSERT INTO files(
                target_path, target_path_key, original_filename, stored_filename,
                sha256, size_bytes, taken_date, date_source, name_conflict,
                deleted_at, gps_scanned_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'filename', 0, ?, ?)
            RETURNING id
            """,
            (
                relative_path,
                relative_path.casefold(),
                Path(relative_path).name,
                Path(relative_path).name,
                sha256,
                file_path.stat().st_size,
                "2024-01-02",
                "2024-02-03 04:05:06" if deleted else None,
                "2024-02-03 04:05:06" if gps_scanned else None,
            ),
        ).fetchone()
        conn.commit()
        return int(row[0])
    finally:
        conn.close()


def insert_openclip_cleanup_fixture(target: Path) -> dict[str, int]:
    active_id = insert_test_file(target, "2024/01/active.png", sha256="sha-active")
    deleted_id = insert_test_file(
        target,
        "deleted/2024/01/deleted.png",
        sha256="sha-deleted",
        deleted=True,
    )
    missing_id = active_id + deleted_id + 1000
    conn = connect_openclip_db(target)
    try:
        conn.executemany(
            """
            INSERT INTO image_embeddings(
                file_id, target_path, target_path_key, sha256,
                model_name, pretrained, embedding
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    active_id,
                    "2024/01/active.png",
                    "2024/01/active.png",
                    "sha-active",
                    "Test-Model",
                    "test-weights",
                    embedding_blob([1.0, 0.0]),
                ),
                (
                    deleted_id,
                    "deleted/2024/01/deleted.png",
                    "deleted/2024/01/deleted.png",
                    "sha-deleted",
                    "Test-Model",
                    "test-weights",
                    embedding_blob([0.0, 1.0]),
                ),
                (
                    missing_id,
                    "2026/01/unimported.png",
                    "2026/01/unimported.png",
                    "sha-missing",
                    "Test-Model",
                    "test-weights",
                    embedding_blob([0.5, 0.5]),
                ),
            ],
        )
        active_run_id = conn.execute(
            """
            INSERT INTO image_search_runs(query, model_name, pretrained, result_limit)
            VALUES('active', 'Test-Model', 'test-weights', 10)
            """
        ).lastrowid
        orphan_run_id = conn.execute(
            """
            INSERT INTO image_search_runs(query, model_name, pretrained, result_limit)
            VALUES('orphan', 'Test-Model', 'test-weights', 10)
            """
        ).lastrowid
        empty_run_id = conn.execute(
            """
            INSERT INTO image_search_runs(query, model_name, pretrained, result_limit)
            VALUES('empty', 'Test-Model', 'test-weights', 10)
            """
        ).lastrowid
        conn.executemany(
            """
            INSERT INTO image_search_results(
                run_id, file_id, target_path, target_path_key, similarity, rank
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    active_run_id,
                    active_id,
                    "2024/01/active.png",
                    "2024/01/active.png",
                    0.8,
                    1,
                ),
                (
                    orphan_run_id,
                    missing_id,
                    "2026/01/unimported.png",
                    "2026/01/unimported.png",
                    0.9,
                    1,
                ),
                (
                    orphan_run_id,
                    deleted_id,
                    "deleted/2024/01/deleted.png",
                    "deleted/2024/01/deleted.png",
                    0.7,
                    2,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "active_id": active_id,
        "deleted_id": deleted_id,
        "missing_id": missing_id,
        "active_run_id": int(active_run_id),
        "orphan_run_id": int(orphan_run_id),
        "empty_run_id": int(empty_run_id),
    }


def insert_basic_item_sidecar_fixture(
    target: Path,
    *,
    file_id: int,
    target_path: str,
    sha256: str,
    face_configs: tuple[FaceRecognitionConfig, ...],
) -> None:
    for face_config in face_configs:
        conn = connect_face_db(target, face_config)
        try:
            conn.execute(
                """
                INSERT INTO scanned_files(
                    file_id, target_path, target_path_key, sha256, status, face_count
                ) VALUES(?, ?, ?, ?, 'ok', 1)
                """,
                (file_id, target_path, target_path.casefold(), sha256),
            )
            face_id = int(
                conn.execute(
                    """
                    INSERT INTO faces(
                        file_id, target_path_key, bbox_x, bbox_y, bbox_width,
                        bbox_height, detection_score, embedding_model, embedding
                    ) VALUES(?, ?, 1, 2, 3, 4, 0.9, 'test', X'00')
                    RETURNING id
                    """,
                    (file_id, target_path.casefold()),
                ).fetchone()[0]
            )
            person_id = int(
                conn.execute(
                    "INSERT INTO persons(name) VALUES('Kari') RETURNING id"
                ).fetchone()[0]
            )
            conn.execute(
                "INSERT INTO person_faces(person_id, face_id) VALUES(?, ?)",
                (person_id, face_id),
            )
            conn.execute(
                "INSERT INTO person_files(person_id, file_id) VALUES(?, ?)",
                (person_id, file_id),
            )
            conn.execute(
                """
                INSERT INTO face_suggestions(
                    person_id, face_id, reference_face_id, similarity
                ) VALUES(?, ?, ?, 0.8)
                """,
                (person_id, face_id, face_id),
            )
            conn.commit()
        finally:
            conn.close()

    conn = connect_openclip_db(target)
    try:
        conn.execute(
            """
            INSERT INTO image_embeddings(
                file_id, target_path, target_path_key, sha256,
                model_name, pretrained, embedding
            ) VALUES(?, ?, ?, ?, 'test', 'test', X'00')
            """,
            (file_id, target_path, target_path.casefold(), sha256),
        )
        run_id = conn.execute(
            """
            INSERT INTO image_search_runs(
                query, model_name, pretrained, result_limit
            ) VALUES('test', 'test', 'test', 1)
            """
        ).lastrowid
        conn.execute(
            """
            INSERT INTO image_search_results(
                run_id, file_id, target_path, target_path_key, similarity, rank
            ) VALUES(?, ?, ?, ?, 0.9, 1)
            """,
            (run_id, file_id, target_path, target_path.casefold()),
        )
        conn.commit()
    finally:
        conn.close()


def register_target_file(target: Path, relative_path: Path, *, source: Path | None = None) -> int:
    path = target / relative_path
    conn = db.connect(target)
    try:
        source_path = source or path
        source_root = source_path.parent
        source_id = db.add_named_source(conn, source_root, f"source-{uuid.uuid4()}")
        file_id = db.insert_imported_file(
            conn,
            source_id=source_id,
            source_path=source_path,
            target_root=target,
            target_path=path,
            original_filename=path.name,
            stored_filename=path.name,
            sha256=sha256_file(path),
            size_bytes=path.stat().st_size,
            taken_date="2024-01-02",
            date_source="filename",
            name_conflict=False,
        )
        conn.commit()
        return file_id
    finally:
        conn.close()


def create_legacy_database(
    target: Path,
    source: Path,
    *,
    include_duplicate: bool = False,
    corrupt_duplicate: bool = False,
) -> None:
    target.mkdir()
    conn = sqlite3.connect(target / DB_FILENAME)
    try:
        conn.executescript(
            """
            create table meta (key text primary key, value text not null);
            create table sources (
                id integer primary key autoincrement,
                kind text not null,
                path text not null,
                path_key text,
                name text,
                added_at text not null default current_timestamp,
                imported_at text,
                status text not null default 'pending'
            );
            create table files (
                id integer primary key autoincrement,
                source_id integer not null,
                source_path text not null,
                source_path_key text not null,
                target_path text not null,
                target_path_key text not null unique,
                original_filename text not null,
                stored_filename text not null,
                sha256 text not null,
                size_bytes integer not null,
                taken_date text,
                date_source text not null,
                name_conflict integer not null default 0,
                imported_at text not null default current_timestamp,
                unique(source_id, source_path_key)
            );
            create table duplicate_findings (
                id integer primary key autoincrement,
                source_id integer not null,
                source_path text not null,
                source_path_key text not null,
                matched_file_id integer not null,
                sha256 text not null,
                found_at text not null default current_timestamp,
                unique(source_id, source_path_key)
            );
            create table errors (
                id integer primary key autoincrement,
                source_id integer,
                source_path text,
                stage text not null,
                message text not null,
                created_at text not null default current_timestamp
            );
            insert into meta(key, value) values('schema_version', '1');
            """
        )
        source.mkdir(exist_ok=True)
        source_file = source / "IMG_20240102.jpg"
        source_file.write_bytes(b"legacy-image")
        imported = target / "2024" / "01" / "IMG_20240102.jpg"
        imported_relative = imported.relative_to(target)
        imported.parent.mkdir(parents=True)
        imported.write_bytes(b"legacy-image")
        file_hash = sha256_file(imported)
        source_id = conn.execute(
            "insert into sources(kind, path, path_key, imported_at, status) values('directory', ?, ?, current_timestamp, 'imported') returning id",
            (str(source.resolve()), str(source.resolve())),
        ).fetchone()[0]
        conn.execute(
            """
            insert into files(
                source_id, source_path, source_path_key, target_path, target_path_key,
                original_filename, stored_filename, sha256, size_bytes, taken_date,
                date_source, name_conflict
            ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                source_id,
                str(source_file.resolve()),
                str(source_file.resolve()),
                imported_relative.as_posix(),
                db.relative_path_key(imported_relative),
                source_file.name,
                imported.name,
                file_hash,
                imported.stat().st_size,
                "2024-01-02",
                "filename",
            ),
        )
        if include_duplicate:
            duplicate = source / "COPY_20240203.jpg"
            duplicate.write_bytes(b"legacy-image")
            conn.execute(
                """
                insert into duplicate_findings(
                    source_id, source_path, source_path_key, matched_file_id, sha256
                ) values(?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    str(duplicate.resolve()),
                    str(duplicate.resolve()),
                    9999 if corrupt_duplicate else 1,
                    file_hash,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def create_v4_database(
    target: Path,
    source: Path,
    *,
    imported: Path,
) -> None:
    target.mkdir()
    source.mkdir(exist_ok=True)
    imported.parent.mkdir(parents=True, exist_ok=True)

    source_file = source / imported.name
    source_file.write_bytes(b"v4-image")
    imported.write_bytes(b"v4-image")
    file_hash = sha256_file(imported)

    conn = sqlite3.connect(target / DB_FILENAME)
    try:
        conn.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE command_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT NOT NULL,
                args_json TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                path_key TEXT,
                name TEXT NOT NULL,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                imported_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                superseded_by_source_id INTEGER REFERENCES sources(id)
            );
            CREATE TABLE files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_path TEXT NOT NULL,
                target_path_key TEXT NOT NULL UNIQUE,
                original_filename TEXT NOT NULL,
                stored_filename TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                taken_date TEXT,
                date_source TEXT NOT NULL,
                name_conflict INTEGER NOT NULL DEFAULT 0,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                deleted_at TEXT,
                deleted_original_target_path TEXT
            );
            CREATE TABLE file_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                source_id INTEGER NOT NULL,
                source_path TEXT NOT NULL,
                source_path_key TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_id, source_path_key)
            );
            CREATE TABLE errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                source_path TEXT,
                stage TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            );
            """
        )
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '4')")
        source_id = conn.execute(
            """
            INSERT INTO sources(path, path_key, name, imported_at, status)
            VALUES(?, ?, ?, CURRENT_TIMESTAMP, 'imported')
            RETURNING id
            """,
            (str(source.resolve()), str(source.resolve()), source.name),
        ).fetchone()[0]
        file_id = conn.execute(
            """
            INSERT INTO files(
                target_path, target_path_key, original_filename, stored_filename,
                sha256, size_bytes, taken_date, date_source, name_conflict
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0)
            RETURNING id
            """,
            (
                str(imported.resolve()),
                str(imported.resolve()),
                imported.name,
                imported.name,
                file_hash,
                imported.stat().st_size,
                "2024-01-02",
                "filename",
            ),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO file_sources(
                file_id, source_id, source_path, source_path_key, sha256, size_bytes
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                source_id,
                str(source_file.resolve()),
                str(source_file.resolve()),
                file_hash,
                source_file.stat().st_size,
            ),
        )
        conn.commit()
    finally:
        conn.close()
