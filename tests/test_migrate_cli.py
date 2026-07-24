from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from bildebank import db
from bildebank.db import DB_FILENAME, init_database
from bildebank.geo import h3_cells_for_point
from bildebank.media import sha256_file
from bildebank.target_lock import LOCK_FILENAME
from tests.cli_helpers import capture_cli, run_cli
from tests.db_test_helpers import create_legacy_database, create_v4_database
from tests.test_media import (
    jpeg_with_exif_camera,
    jpeg_with_exif_datetime,
)


def database_dump(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


class MigrateCliTests(unittest.TestCase):

    def test_database_schema_includes_general_performance_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            init_database(target)
            conn = db.connect(target)
            try:
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
            finally:
                conn.close()

        self.assertIn("idx_files_active_browser_order", indexes)
        self.assertIn("idx_files_active_date_source_order", indexes)
        self.assertIn("idx_files_active_target_path_key", indexes)
        self.assertIn("idx_file_sources_source_id_id", indexes)
        self.assertIn("idx_errors_unresolved_stage_id", indexes)

    def test_migrate_promotes_existing_user_tag_to_system_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-a")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source", "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                file_id = int(conn.execute("SELECT id FROM files").fetchone()["id"])
                conn.execute("DELETE FROM file_tags")
                conn.execute("DELETE FROM tags WHERE name_key = ?", (db.tag_name_key(db.SYSTEM_TAG_OUT_OF_FOCUS),))
                cursor = conn.execute(
                    "INSERT INTO tags(name, name_key, kind) VALUES(?, ?, ?)",
                    ("ute av fokus", db.tag_name_key(db.SYSTEM_TAG_OUT_OF_FOCUS), db.TAG_KIND_USER),
                )
                conn.execute("INSERT INTO file_tags(file_id, tag_id) VALUES(?, ?)", (file_id, int(cursor.lastrowid)))
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])
            self.assertEqual(code, 1)
            self.assertIn("bildebank migrate", stderr)

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])
            self.assertEqual(code, 0, stderr)

            conn = db.connect(target)
            try:
                row = conn.execute("SELECT name, kind FROM tags WHERE name_key = ?", (db.tag_name_key(db.SYSTEM_TAG_OUT_OF_FOCUS),)).fetchone()
                linked = conn.execute("SELECT COUNT(*) AS count FROM file_tags").fetchone()
            finally:
                conn.close()

        self.assertEqual(row["name"], db.SYSTEM_TAG_OUT_OF_FOCUS)
        self.assertEqual(row["kind"], db.TAG_KIND_SYSTEM)
        self.assertEqual(int(linked["count"]), 1)

    def test_migrate_adds_kind_to_existing_tags_table_and_seeds_system_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.executescript(
                    """
                    DROP TABLE file_tags;
                    DROP TABLE tags;
                    CREATE TABLE tags (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        name_key TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE file_tags (
                        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(file_id, tag_id)
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])
            self.assertEqual(code, 1)
            self.assertIn("bildebank migrate", stderr)

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])
            self.assertEqual(code, 0, stderr)

            conn = db.connect(target)
            try:
                columns = db.table_columns(conn, "tags")
                row = conn.execute("SELECT name, kind FROM tags WHERE name_key = ?", (db.tag_name_key(db.SYSTEM_TAG_OUT_OF_FOCUS),)).fetchone()
            finally:
                conn.close()

        self.assertIn("kind", columns)
        self.assertEqual(row["name"], db.SYSTEM_TAG_OUT_OF_FOCUS)
        self.assertEqual(row["kind"], db.TAG_KIND_SYSTEM)

    def test_opening_current_database_without_collection_id_requires_migrate_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("DELETE FROM meta WHERE key = 'collection_id'")
                conn.commit()
            finally:
                conn.close()

            database_path = target / DB_FILENAME
            before = database_path.read_bytes()
            before_mtime = database_path.stat().st_mtime_ns
            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                collection_id = conn.execute(
                    "SELECT value FROM meta WHERE key = 'collection_id'"
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("meta.collection_id", stderr)
            self.assertIn("bildebank migrate", stderr)
            self.assertIsNone(collection_id)
            self.assertEqual(database_path.read_bytes(), before)
            self.assertEqual(database_path.stat().st_mtime_ns, before_mtime)

    def test_migrate_v2_removes_legacy_source_fk_and_kind_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            parent = root / "Bilder"
            child = parent / "2006"
            child.mkdir(parents=True)
            child_source_file = child / "IMG_20061003.jpg"
            parent_source_file = parent / "IMG_20070104.jpg"
            child_source_file.write_bytes(b"child")
            parent_source_file.write_bytes(b"parent")
            child_target_file = target / "2006" / "10" / "IMG_20061003.jpg"
            parent_target_file = target / "2007" / "01" / "IMG_20070104.jpg"
            child_target_file.parent.mkdir(parents=True)
            parent_target_file.parent.mkdir(parents=True)
            child_target_file.write_bytes(b"child")
            parent_target_file.write_bytes(b"parent")
            child_hash = sha256_file(child_target_file)
            parent_hash = sha256_file(parent_target_file)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE command_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        command TEXT NOT NULL,
                        args_json TEXT NOT NULL,
                        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE sources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        path TEXT NOT NULL,
                        path_key TEXT,
                        name TEXT,
                        added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        imported_at TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        superseded_by_source_id INTEGER REFERENCES sources(id)
                    );
                    CREATE TABLE files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_id INTEGER REFERENCES sources(id),
                        source_path TEXT,
                        source_path_key TEXT,
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
                        file_id INTEGER NOT NULL REFERENCES files(id),
                        source_id INTEGER NOT NULL REFERENCES sources(id),
                        source_path TEXT NOT NULL,
                        source_path_key TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(source_id, source_path_key)
                    );
                    CREATE TABLE errors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_id INTEGER REFERENCES sources(id),
                        source_path TEXT,
                        stage TEXT NOT NULL,
                        message TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TEXT
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '2');
                    """
                )
                parent_id = conn.execute(
                    """
                    INSERT INTO sources(id, kind, path, path_key, imported_at, status)
                    VALUES(2, 'directory', ?, ?, CURRENT_TIMESTAMP, 'imported')
                    RETURNING id
                    """,
                    (str(parent.resolve()), str(parent.resolve())),
                ).fetchone()[0]
                child_id = conn.execute(
                    """
                    INSERT INTO sources(id, kind, path, path_key, imported_at, status, superseded_by_source_id)
                    VALUES(1, 'directory', ?, ?, CURRENT_TIMESTAMP, 'superseded', 2)
                    RETURNING id
                    """,
                    (str(child.resolve()), str(child.resolve())),
                ).fetchone()[0]
                child_file_id = conn.execute(
                    """
                    INSERT INTO files(
                        source_id, source_path, source_path_key, target_path, target_path_key,
                        original_filename, stored_filename, sha256, size_bytes, taken_date,
                        date_source, name_conflict
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'filename', 0)
                    RETURNING id
                    """,
                    (
                        child_id,
                        str(child_source_file.resolve()),
                        str(child_source_file.resolve()),
                        child_target_file.relative_to(target).as_posix(),
                        db.relative_path_key(child_target_file.relative_to(target)),
                        child_source_file.name,
                        child_target_file.name,
                        child_hash,
                        child_target_file.stat().st_size,
                        "2006-10-03",
                    ),
                ).fetchone()[0]
                parent_file_id = conn.execute(
                    """
                    INSERT INTO files(
                        source_id, source_path, source_path_key, target_path, target_path_key,
                        original_filename, stored_filename, sha256, size_bytes, taken_date,
                        date_source, name_conflict
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'filename', 0)
                    RETURNING id
                    """,
                    (
                        parent_id,
                        str(parent_source_file.resolve()),
                        str(parent_source_file.resolve()),
                        parent_target_file.relative_to(target).as_posix(),
                        db.relative_path_key(parent_target_file.relative_to(target)),
                        parent_source_file.name,
                        parent_target_file.name,
                        parent_hash,
                        parent_target_file.stat().st_size,
                        "2007-01-04",
                    ),
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO file_sources(
                        file_id, source_id, source_path, source_path_key, sha256, size_bytes, kind
                    ) VALUES(?, ?, ?, ?, ?, ?, 'imported')
                    """,
                    (
                        child_file_id,
                        child_id,
                        str(child_source_file.resolve()),
                        str(child_source_file.resolve()),
                        child_hash,
                        child_target_file.stat().st_size,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO file_sources(
                        file_id, source_id, source_path, source_path_key, sha256, size_bytes, kind
                    ) VALUES(?, ?, ?, ?, ?, ?, 'imported')
                    """,
                    (
                        parent_file_id,
                        parent_id,
                        str(parent_source_file.resolve()),
                        str(parent_source_file.resolve()),
                        parent_hash,
                        parent_target_file.stat().st_size,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Nåværende schema_version: 2", stdout)
            self.assertIn("Ny schema_version: 16", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "16",
                )
                file_columns = {row[1] for row in conn.execute("pragma table_info(files)")}
                source_columns = {row[1] for row in conn.execute("pragma table_info(sources)")}
                file_source_columns = {row[1] for row in conn.execute("pragma table_info(file_sources)")}
                self.assertFalse({"source_id", "source_path", "source_path_key"} & file_columns)
                self.assertNotIn("kind", source_columns)
                self.assertNotIn("superseded_by_source_id", source_columns)
                self.assertNotIn("kind", file_source_columns)
                self.assertEqual(conn.execute("select count(*) from sources where name is null").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute("select count(*) from sources where status = 'superseded'").fetchone()[0],
                    0,
                )
                self.assertEqual(conn.execute("pragma foreign_key_list(errors)").fetchall(), [])
            finally:
                conn.close()

    def test_writing_command_requires_explicit_migration_for_old_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            create_legacy_database(target, source)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", source.name, str(source)]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("schema_version=1", stderr)
            self.assertIn("bildebank migrate", stderr)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertFalse(
                    conn.execute(
                        "select 1 from sqlite_master where type = 'table' and name = 'command_log'"
                    ).fetchone()
                )
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "1",
                )
            finally:
                conn.close()

    def test_module_migrate_from_repo_parent_reports_missing_target_without_traceback(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")

        result = subprocess.run(
            [sys.executable, "-m", "bildebank", "migrate"],
            cwd=repo.parent,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Fant ingen bildesamling. Kjør kommandoen fra bildesamlingsmappen.", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_migrate_check_reports_plan_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            create_legacy_database(target, source, include_duplicate=True)

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate", "--check"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Nåværende schema_version: 1", stdout)
            self.assertIn("Ny schema_version: 16", stdout)
            self.assertIn("Vil opprette tabellen file_sources.", stdout)
            self.assertIn("  importerte filer: 1", stdout)
            self.assertIn("  duplikatfunn: 1", stdout)
            self.assertIn("  bygge om files uten gamle v1-kildekolonner", stdout)
            self.assertIn("  fjerne legacy-tabellen duplicate_findings", stdout)
            self.assertIn("Ingen endringer er gjort (--check).", stdout)
            self.assertFalse(list(target.glob(".bilder.sqlite3.backup-before-schema-16-*")))
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertFalse(
                    conn.execute(
                        "select 1 from sqlite_master where type = 'table' and name = 'file_sources'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_migrate_backfills_file_sources_and_then_status_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            create_legacy_database(target, source, include_duplicate=True)

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Lager backup:", stdout)
            self.assertIn("Ferdig. Databasen er migrert.", stdout)
            self.assertEqual(len(list(target.glob(".bilder.sqlite3.backup-before-schema-16-*"))), 1)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "16",
                )
                self.assertEqual(conn.execute("select count(*) from file_sources").fetchone()[0], 2)
                file_columns = {row[1] for row in conn.execute("pragma table_info(files)")}
                source_columns = {row[1] for row in conn.execute("pragma table_info(sources)")}
                file_source_columns = {row[1] for row in conn.execute("pragma table_info(file_sources)")}
                self.assertFalse({"source_id", "source_path", "source_path_key"} & file_columns)
                self.assertNotIn("kind", source_columns)
                self.assertNotIn("kind", file_source_columns)
                self.assertEqual(conn.execute("select name from sources").fetchone()[0], "source")
                self.assertFalse(
                    conn.execute(
                        "select 1 from sqlite_master where type = 'table' and name = 'duplicate_findings'"
                    ).fetchone()
                )
                self.assertEqual(conn.execute("pragma foreign_key_list(errors)").fetchall(), [])
                self.assertEqual(conn.execute("select count(*) from command_log").fetchone()[0], 1)
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Importerte filer: 1", stdout)
            self.assertIn("Registrerte filer i kilder: 2", stdout)
            self.assertIn("Duplikatkilder: 1", stdout)

    def test_migrate_v5_to_v11_creates_performance_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
                conn.execute("DROP INDEX idx_files_active_browser_order")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Nåværende schema_version: 5", stdout)
            self.assertIn("Ny schema_version: 16", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "16",
                )
                self.assertTrue(
                    conn.execute(
                        "select 1 from sqlite_master where type = 'index' and name = 'idx_files_active_browser_order'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_migrate_current_schema_repairs_missing_performance_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("DROP INDEX idx_file_sources_source_id_file_id")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Nåværende schema_version: 16", stdout)
            self.assertIn("oppdatere manglende ytelsesindekser", stdout)
            self.assertIn("Oppdaterer manglende ytelsesindekser.", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertTrue(
                    conn.execute(
                        "select 1 from sqlite_master where type = 'index' and name = 'idx_file_sources_source_id_file_id'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_migrate_rejects_invalid_current_schema_before_already_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            database_path = target / DB_FILENAME
            absolute_target_path = str((target / "2024" / "01" / "image.jpg").resolve())
            conn = sqlite3.connect(database_path)
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename, stored_filename,
                        sha256, size_bytes, date_source
                    )
                    VALUES(?, ?, 'image.jpg', 'image.jpg', 'abc', 3, 'filename')
                    """,
                    (absolute_target_path, absolute_target_path),
                )
                conn.commit()
            finally:
                conn.close()
            before = database_path.read_bytes()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "migrate"]
            )

            self.assertEqual(code, 1)
            self.assertNotIn("Databasen er allerede migrert.", stdout)
            self.assertIn("absolutt target_path", stderr)
            self.assertEqual(database_path.read_bytes(), before)
            self.assertFalse(
                list(target.glob(".bilder.sqlite3.backup-before-schema-16-*"))
            )

    def test_migrate_valid_current_schema_is_already_migrated_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            database_path = target / DB_FILENAME
            before = database_path.read_bytes()
            before_mtime = database_path.stat().st_mtime_ns

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "migrate"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Databasen er allerede migrert.", stdout)
            self.assertEqual(database_path.read_bytes(), before)
            self.assertEqual(database_path.stat().st_mtime_ns, before_mtime)
            self.assertFalse(
                list(target.glob(".bilder.sqlite3.backup-before-schema-16-*"))
            )

    def test_migrate_check_reports_internal_v11_repairs_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("DELETE FROM tags WHERE name_key = 'ute av fokus'")
                conn.execute("DELETE FROM meta WHERE key = 'collection_id'")
                conn.execute("DROP INDEX idx_file_tags_tag_id_file_id")
                conn.commit()
            finally:
                conn.close()
            database_path = target / DB_FILENAME
            before = database_path.read_bytes()
            before_mtime = database_path.stat().st_mtime_ns

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate", "--check"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("reparere intern v16-struktur", stdout)
            self.assertIn("systemtaggen", stdout)
            self.assertIn("meta.collection_id", stdout)
            self.assertIn("idx_file_tags_tag_id_file_id", stdout)
            self.assertIn("Ingen endringer er gjort (--check).", stdout)
            self.assertEqual(database_path.read_bytes(), before)
            self.assertEqual(database_path.stat().st_mtime_ns, before_mtime)
            self.assertFalse(list(target.glob(".bilder.sqlite3.backup-before-schema-16-*")))

    def test_migrate_repairs_internal_v11_structure_and_preserves_tags_and_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename, stored_filename,
                        sha256, size_bytes, date_source
                    )
                    VALUES('2024/01/image.jpg', '2024/01/image.jpg', 'image.jpg',
                           'image.jpg', 'abc', 3, 'filename')
                    """
                )
                user_tag_id = conn.execute(
                    "INSERT INTO tags(name, name_key, kind) VALUES('Familie', 'familie', 'user')"
                ).lastrowid
                conn.execute("INSERT INTO file_tags(file_id, tag_id) VALUES(1, ?)", (user_tag_id,))
                conn.execute("DELETE FROM tags WHERE name_key = 'ute av fokus'")
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'collection_id'",
                    (str(uuid.uuid4()).upper(),),
                )
                conn.execute("DROP TABLE file_tags")
                conn.execute(
                    """
                    CREATE TABLE file_tags (
                        file_id INTEGER NOT NULL,
                        tag_id INTEGER NOT NULL,
                        created_at TEXT
                    )
                    """
                )
                conn.execute("INSERT INTO file_tags(file_id, tag_id) VALUES(1, ?)", (user_tag_id,))
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Reparerer intern v16-struktur.", stdout)
            self.assertEqual(len(list(target.glob(".bilder.sqlite3.backup-before-schema-16-*"))), 1)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                collection_id = conn.execute(
                    "SELECT value FROM meta WHERE key = 'collection_id'"
                ).fetchone()[0]
                system_tag = conn.execute(
                    "SELECT name, kind FROM tags WHERE name_key = 'ute av fokus'"
                ).fetchone()
                user_tag = conn.execute(
                    "SELECT name, kind FROM tags WHERE id = ?",
                    (user_tag_id,),
                ).fetchone()
                link = conn.execute(
                    "SELECT file_id, tag_id FROM file_tags WHERE tag_id = ?",
                    (user_tag_id,),
                ).fetchone()
                index_exists = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'index' AND name = 'idx_file_tags_tag_id_file_id'
                    """
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(str(uuid.UUID(collection_id)), collection_id)
            self.assertEqual(system_tag, (db.SYSTEM_TAG_OUT_OF_FOCUS, db.TAG_KIND_SYSTEM))
            self.assertEqual(user_tag, ("Familie", db.TAG_KIND_USER))
            self.assertEqual(link, (1, user_tag_id))
            self.assertIsNotNone(index_exists)

    def test_migrate_repairs_missing_tag_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("DROP TABLE file_tags")
                conn.execute("DROP TABLE tags")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            conn = db.connect(target)
            try:
                db.validate_current_schema(conn)
            finally:
                conn.close()

    def test_migrate_v9_to_v11_adds_camera_columns_without_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(jpeg_with_exif_camera("Canon", "EOS 80D"))
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("ALTER TABLE files DROP COLUMN camera_make")
                conn.execute("ALTER TABLE files DROP COLUMN camera_model")
                conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Nåværende schema_version: 9", stdout)
            self.assertIn("Ny schema_version: 16", stdout)
            self.assertIn("Legger til kamerakolonner i files.", stdout)
            self.assertIn("refresh-metadata --rescan", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
                self.assertIn("camera_make", columns)
                self.assertIn("camera_model", columns)
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                    "16",
                )
                self.assertEqual(
                    conn.execute("SELECT camera_make, camera_model FROM files").fetchone(),
                    (None, None),
                )
            finally:
                conn.close()

    def test_migrate_v10_to_v11_adds_pending_file_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("DROP TABLE pending_file_deletes")
                conn.execute("UPDATE meta SET value = '10' WHERE key = 'schema_version'")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Nåværende schema_version: 10", stdout)
            self.assertIn("Ny schema_version: 16", stdout)
            self.assertIn("Oppretter pending_file_deletes.", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta WHERE key = 'schema_version'"
                    ).fetchone()[0],
                    "16",
                )
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(pending_file_deletes)")
                }
            finally:
                conn.close()
            self.assertIn("last_error", columns)

    def test_migrate_v12_to_v16_adds_metadata_datetime_without_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(jpeg_with_exif_datetime("2024:01:02 03:04:05"))
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("ALTER TABLE files DROP COLUMN metadata_datetime")
                conn.execute("UPDATE meta SET value = '12' WHERE key = 'schema_version'")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Nåværende schema_version: 12", stdout)
            self.assertIn("Ny schema_version: 16", stdout)
            self.assertIn("Legger til metadata_datetime i files.", stdout)
            self.assertIn("refresh-metadata --rescan", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
                self.assertIn("metadata_datetime", columns)
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                    "16",
                )
                self.assertIsNone(conn.execute("SELECT metadata_datetime FROM files").fetchone()[0])
            finally:
                conn.close()

    def test_migrate_v14_to_v16_adds_nullable_comment_without_changing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "import",
                        "--name",
                        source.name,
                        "--quiet",
                        str(source),
                    ]
                ),
                0,
            )
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                before_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                before_sources = conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0]
                conn.execute("ALTER TABLE files DROP COLUMN comment")
                conn.execute("UPDATE meta SET value = '14' WHERE key = 'schema_version'")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Nåværende schema_version: 14", stdout)
            self.assertIn("Ny schema_version: 16", stdout)
            self.assertIn("Legger til comment i files.", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertIn(
                    "comment",
                    {row[1] for row in conn.execute("PRAGMA table_info(files)")},
                )
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], before_files)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0],
                    before_sources,
                )
                self.assertIsNone(conn.execute("SELECT comment FROM files").fetchone()[0])
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                    "16",
                )
            finally:
                conn.close()

    def test_migrate_v15_to_v16_adds_pending_delete_identity_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    """
                    INSERT INTO pending_file_deletes(path, reason, source_id)
                    VALUES('2024/01/legacy.jpg', 'unimport', 7)
                    """
                )
                conn.execute(
                    "ALTER TABLE pending_file_deletes DROP COLUMN expected_sha256"
                )
                conn.execute(
                    "ALTER TABLE pending_file_deletes DROP COLUMN expected_size_bytes"
                )
                conn.execute(
                    "UPDATE meta SET value = '15' WHERE key = 'schema_version'"
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "migrate"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Nåværende schema_version: 15", stdout)
            self.assertIn(
                "Legger til innholdsidentitet for pending_file_deletes.",
                stdout,
            )
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(pending_file_deletes)"
                    )
                }
                self.assertTrue(
                    {"expected_sha256", "expected_size_bytes"} <= columns
                )
                row = conn.execute(
                    """
                    SELECT path, source_id, expected_sha256, expected_size_bytes
                    FROM pending_file_deletes
                    """
                ).fetchone()
                self.assertEqual(
                    row,
                    ("2024/01/legacy.jpg", 7, None, None),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta WHERE key = 'schema_version'"
                    ).fetchone()[0],
                    "16",
                )
            finally:
                conn.close()

    def test_migrate_v7_to_v11_adds_h3_10_11_and_backfills_existing_gps(self) -> None:
        cells = h3_cells_for_point(59.91273, 10.74609)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename, stored_filename,
                        sha256, size_bytes, date_source, gps_lat, gps_lon, gps_source
                    )
                    VALUES('2024/01/image.jpg', '2024/01/image.jpg', 'image.jpg', 'image.jpg',
                           'abc', 3, 'filename', 59.91273, 10.74609, 'exiftool')
                    """
                )
                for index_name in (
                    "idx_files_h3_res10",
                    "idx_files_h3_res10_browser_order",
                    "idx_files_h3_res11",
                    "idx_files_h3_res11_browser_order",
                ):
                    conn.execute(f"DROP INDEX {index_name}")
                conn.execute("ALTER TABLE files DROP COLUMN h3_res10")
                conn.execute("ALTER TABLE files DROP COLUMN h3_res11")
                conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Nåværende schema_version: 7", stdout)
            self.assertIn("Ny schema_version: 16", stdout)
            self.assertIn("Fyller h3_res10 og h3_res11", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                    "16",
                )
                columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(files)")}
                row = conn.execute("SELECT h3_res10, h3_res11 FROM files").fetchone()
            finally:
                conn.close()

        self.assertIn("h3_res10", columns)
        self.assertIn("h3_res11", columns)
        self.assertIn("idx_files_h3_res10", indexes)
        self.assertIn("idx_files_h3_res11", indexes)
        self.assertEqual(row[0], cells["h3_res10"])
        self.assertEqual(row[1], cells["h3_res11"])

    def test_migrate_v6_to_v11_replaces_legacy_gps_error_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename, stored_filename,
                        sha256, size_bytes, date_source, gps_error
                    )
                    VALUES('2024/01/image.jpg', '2024/01/image.jpg', 'image.jpg', 'image.jpg',
                           'abc', 3, 'filename', ?)
                    """,
                    ("Error: File not found\n" * 1000,),
                )
                conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Nåværende schema_version: 6", stdout)
            self.assertIn("Ny schema_version: 16", stdout)
            self.assertIn("Rydder gamle GPS-feilmeldinger.", stdout)
            self.assertIn("bildebank vacuum", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "16",
                )
                self.assertEqual(
                    conn.execute("SELECT gps_error FROM files").fetchone()[0],
                    db.GPS_ERROR_EXIFTOOL,
                )
            finally:
                conn.close()

    def test_migrate_v6_to_v11_keeps_missing_file_as_short_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename, stored_filename,
                        sha256, size_bytes, date_source, gps_error
                    )
                    VALUES('2024/01/missing.jpg', '2024/01/missing.jpg', 'missing.jpg', 'missing.jpg',
                           'abc', 3, 'filename', ?)
                    """,
                    ("Filen finnes ikke: C:\\Bilder\\missing.jpg",),
                )
                conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("SELECT gps_error FROM files").fetchone()[0],
                    db.GPS_ERROR_FILE_MISSING,
                )
            finally:
                conn.close()

    def test_current_schema_rejects_v16_database_with_absolute_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            create_v4_database(target, source, imported=imported)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("ALTER TABLE files ADD COLUMN manual_date_from TEXT")
                conn.execute("ALTER TABLE files ADD COLUMN manual_date_to TEXT")
                conn.execute("ALTER TABLE files ADD COLUMN manual_date_note TEXT")
                conn.execute("ALTER TABLE files ADD COLUMN camera_make TEXT")
                conn.execute("ALTER TABLE files ADD COLUMN camera_model TEXT")
                conn.execute("ALTER TABLE files ADD COLUMN metadata_datetime TEXT")
                conn.execute("ALTER TABLE files ADD COLUMN comment TEXT")
                conn.execute("ALTER TABLE sources DROP COLUMN superseded_by_source_id")
                db.create_pending_file_deletes_schema(conn)
                db.create_pending_file_moves_schema(conn)
                conn.execute("UPDATE meta SET value = '16' WHERE key = 'schema_version'")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 1)
            self.assertIn("absolutt target_path", stderr)
            self.assertIn("bildebank migrate", stderr)

    def test_migrate_rejects_unsupported_v4_without_database_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            create_v4_database(target, source, imported=imported)
            database_path = target / DB_FILENAME
            before = database_dump(database_path)
            before_bytes = database_path.read_bytes()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "migrate"]
            )

            self.assertEqual(code, 1)
            self.assertIn("Lager backup:", stdout)
            self.assertIn(
                "Kan ikke migrere database med schema_version=4",
                stderr,
            )
            self.assertEqual(database_dump(database_path), before)
            self.assertEqual(database_path.read_bytes(), before_bytes)
            backups = list(
                target.glob(".bilder.sqlite3.backup-before-schema-16-*")
            )
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), before_bytes)

    def test_migrate_v3_names_unnamed_sources_and_removes_kind_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            db_path = target / DB_FILENAME
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE command_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        command TEXT NOT NULL,
                        args_json TEXT NOT NULL,
                        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE sources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        path TEXT NOT NULL,
                        path_key TEXT,
                        name TEXT,
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
                        file_id INTEGER NOT NULL REFERENCES files(id),
                        source_id INTEGER NOT NULL REFERENCES sources(id),
                        source_path TEXT NOT NULL,
                        source_path_key TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        kind TEXT NOT NULL,
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
                    INSERT INTO meta(key, value) VALUES('schema_version', '3');
                    """
                )
                for source_id, path_value, name in (
                    (1, r"C:\A\sommer", None),
                    (2, r"D:\B\sommer", None),
                    (3, r"F:\\", None),
                    (4, r"E:\bilder", "eksisterende"),
                ):
                    conn.execute(
                        """
                        INSERT INTO sources(id, kind, path, path_key, name, imported_at, status)
                        VALUES(?, 'directory', ?, ?, ?, CURRENT_TIMESTAMP, 'imported')
                        """,
                        (source_id, path_value, path_value, name),
                    )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Ny schema_version: 16", stdout)
            conn = sqlite3.connect(db_path)
            try:
                names = [row[0] for row in conn.execute("SELECT name FROM sources ORDER BY id")]
                self.assertEqual(names, ["sommer", "sommer-1", "F", "eksisterende"])
                source_columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
                file_source_columns = {row[1] for row in conn.execute("PRAGMA table_info(file_sources)")}
                self.assertNotIn("kind", source_columns)
                self.assertNotIn("kind", file_source_columns)
                name_column = [
                    row for row in conn.execute("PRAGMA table_info(sources)") if row[1] == "name"
                ][0]
                self.assertEqual(name_column[3], 1)
            finally:
                conn.close()

    def test_migrate_keeps_backup_and_rolls_back_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            create_legacy_database(target, source, include_duplicate=True, corrupt_duplicate=True)

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 1)
            self.assertIn("Lager backup:", stdout)
            self.assertIn("Databasen ble ikke migrert", stderr)
            self.assertEqual(len(list(target.glob(".bilder.sqlite3.backup-before-schema-16-*"))), 1)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "1",
                )
                self.assertFalse(
                    conn.execute(
                        "select 1 from sqlite_master where type = 'table' and name = 'file_sources'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_migrate_v1_rolls_back_late_failure_and_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            create_legacy_database(target, source, include_duplicate=True)
            database_path = target / DB_FILENAME
            before_dump = database_dump(database_path)
            before_bytes = database_path.read_bytes()

            with mock.patch(
                "bildebank.db_schema.validate_database_health",
                side_effect=RuntimeError("injisert sen feil"),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "migrate"]
                )

            self.assertEqual(code, 1)
            self.assertIn("injisert sen feil", stderr)
            self.assertIn("Ingen endringer er skrevet", stderr)
            self.assertEqual(database_dump(database_path), before_dump)
            backups = list(
                target.glob(".bilder.sqlite3.backup-before-schema-16-*")
            )
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), before_bytes)
            conn = sqlite3.connect(database_path)
            try:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "migrate"]
            )

            self.assertEqual(code, 0, stderr)
            conn = db.connect(target)
            try:
                self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                conn.close()

    def test_migrate_v14_rolls_back_rebuilt_sources_after_late_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            database_path = target / DB_FILENAME
            conn = sqlite3.connect(database_path)
            try:
                conn.execute(
                    """
                    INSERT INTO sources(path, path_key, name, imported_at, status)
                    VALUES('C:\\Bilder', 'c:\\bilder', 'eldre-kilde',
                           CURRENT_TIMESTAMP, 'superseded')
                    """
                )
                conn.execute(
                    "ALTER TABLE sources ADD COLUMN superseded_by_source_id INTEGER"
                )
                conn.execute(
                    "UPDATE meta SET value = '14' WHERE key = 'schema_version'"
                )
                conn.commit()
            finally:
                conn.close()
            before_dump = database_dump(database_path)

            with mock.patch(
                "bildebank.db_schema.validate_database_health",
                side_effect=RuntimeError("injisert sen feil"),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "migrate"]
                )

            self.assertEqual(code, 1)
            self.assertIn("Ingen endringer er skrevet", stderr)
            self.assertEqual(database_dump(database_path), before_dump)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "migrate"]
            )

            self.assertEqual(code, 0, stderr)
            conn = db.connect(target)
            try:
                self.assertNotIn(
                    "superseded_by_source_id",
                    db.table_columns(conn, "sources"),
                )
                self.assertEqual(
                    conn.execute("SELECT status FROM sources").fetchone()[0],
                    "imported",
                )
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                conn.close()

    def test_migrate_keyboard_interrupt_rolls_back_and_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            create_legacy_database(target, source, include_duplicate=True)
            database_path = target / DB_FILENAME
            before_dump = database_dump(database_path)

            with mock.patch(
                "bildebank.db_schema.validate_database_health",
                side_effect=KeyboardInterrupt,
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "migrate"]
                )

            self.assertEqual(code, 130)
            self.assertEqual(stderr, "Avbrutt.\n")
            self.assertEqual(database_dump(database_path), before_dump)
            self.assertFalse((target / LOCK_FILENAME).exists())

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "migrate"]
            )

            self.assertEqual(code, 0, stderr)
            conn = db.connect(target)
            try:
                self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                conn.close()

    def test_current_schema_rejects_v4_database_with_legacy_tables_and_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            create_legacy_database(target, source)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", source.name, str(source)]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("schema_version=16", stderr)
            self.assertIn("bildebank migrate", stderr)
