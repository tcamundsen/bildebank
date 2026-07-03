from __future__ import annotations

import sqlite3
import tempfile
import unittest
import os
import uuid
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank.cli import (
    build_parser,
    main,
    wsl_path_from_windows_path,
)
from bildebank import db
from bildebank.db import DB_FILENAME
from bildebank.media import ImageDimensions, sha256_file
from bildebank.media_cache import cached_image_dimensions, cached_image_orientation
from bildebank.openclip import connect_openclip_db, embedding_blob
from bildebank.target_lock import LOCK_FILENAME, TargetLockError
from tests.test_media import (
    minimal_png,
)


def run_cli(args: list[str]) -> int:
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return main(args)


def capture_cli(args: list[str]) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(args)
    return code, stdout.getvalue(), stderr.getvalue()


def write_fake_exiftool(path: Path, body: str | None = None) -> None:
    script_body = body or "import json\nprint(json.dumps([]))\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""#!/usr/bin/env python3
import sys
if "-ver" in sys.argv:
    print("13.58")
    raise SystemExit(0)
{script_body}
""",
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(0o755)


def write_test_image(path: Path, *, size: tuple[int, int] = (8, 8), color: tuple[int, int, int] = (200, 20, 20)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    image.save(path)


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
        return {
            "active_id": active_id,
            "deleted_id": deleted_id,
            "missing_id": missing_id,
            "active_run_id": int(active_run_id),
            "orphan_run_id": int(orphan_run_id),
            "empty_run_id": int(empty_run_id),
        }
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


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.program_root_tempdir = tempfile.TemporaryDirectory()
        self.program_root = Path(self.program_root_tempdir.name)
        self.program_root_patcher = patch("bildebank.cli.program_repo_root", return_value=self.program_root)
        self.program_root_patcher.start()

    def tearDown(self) -> None:
        self.program_root_patcher.stop()
        self.program_root_tempdir.cleanup()

    def enable_face_recognition_config(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[face_recognition]
enabled = true
provider = "cpu"
model_root = ".bildebank-insightface"
model_name = "buffalo_l"
""",
            encoding="utf-8",
        )

    def enable_openclip_config(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[openclip]
enabled = true
model_root = ".bildebank-openclip"
device = "cpu"
model_name = "ViT-B-32"
pretrained = "laion2b_s34b_b79k"
""",
            encoding="utf-8",
        )

    def test_main_without_arguments_shows_help(self) -> None:
        code, stdout, stderr = capture_cli([])

        self.assertEqual(code, 0)
        self.assertIn("usage: bildebank [-h] [--version] <kommando> [<args>]", stdout)
        self.assertIn("Bildebank 0.4.0", stdout)
        self.assertIn("Vanlige kommandoer:", stdout)
        self.assertIn("bildebank <kommando> -h", stdout)
        self.assertIn("Vanlig start:", stdout)
        self.assertIn('bildebank import --name "Mobil 2024" --dry-run "E:\\DCIM"', stdout)
        self.assertIn("bildebank run-server", stdout)
        self.assertEqual(stderr, "")

    def test_main_help_groups_commands_by_user_task(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("usage: bildebank [-h] [--version] <kommando> [<args>]", stdout)
        self.assertIn("Bildebank 0.4.0", stdout)
        self.assertNotIn("--target", stdout)
        self.assertIn("Vanlige kommandoer:", stdout)
        self.assertIn("kom i gang\n   launcher", stdout)
        self.assertIn("import", stdout)
        self.assertIn("run-server", stdout)
        self.assertIn("kontrollere importen\n   errors", stdout)
        self.assertIn("rydde trygt\n   remove", stdout)
        self.assertIn("metadata og steder\n   refresh-metadata", stdout)
        self.assertIn("ansikter\n   download-face-model", stdout)
        self.assertIn("bildesøk\n   image-scan", stdout)
        self.assertIn("cleanup-image-search", stdout)
        self.assertIn("HTML-eksport\n   make-thumbnails", stdout)
        self.assertIn("vedlikehold\n   doctor", stdout)
        self.assertIn("backup", stdout)
        self.assertIn("Vanlig start:", stdout)
        self.assertIn("bildebank <kommando> -h", stdout)
        self.assertNotIn("{create,add,import", stdout)
        self.assertNotIn("face-group", stdout)
        self.assertNotIn("face-person-add-group", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_debug_shows_traceback_for_unhandled_errors(self) -> None:
        with patch("bildebank.cli.run", side_effect=RuntimeError("boom")):
            code, stdout, stderr = capture_cli(["--debug", "status"])

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Traceback (most recent call last):", stderr)
        self.assertIn("RuntimeError: boom", stderr)

    def test_errors_are_short_without_debug(self) -> None:
        with patch("bildebank.cli.run", side_effect=RuntimeError("boom")):
            code, stdout, stderr = capture_cli(["status"])

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "Feil: boom\n")

    def test_subcommand_help_has_clean_usage(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["create", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("usage: bildebank create [valg] mappe", stdout)
        self.assertIn("mappe       Mappen som skal bli bildesamling", stdout)
        self.assertNotIn("<kommando> [<args>] create", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_make_people_browser_help(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["make-people-browser", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("make-people-browser", stdout)
        self.assertIn("--month-preview-limit", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_media_metadata_cache_stores_dimensions_and_orientation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            target_path = target / "2024" / "01" / "IMG_20240102.png"

            dimensions = cached_image_dimensions(target, target_path)
            orientation = cached_image_orientation(target, target_path)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute(
                    """
                    SELECT media_width, media_height, media_orientation, media_metadata_mtime_ns
                    FROM files
                    WHERE stored_filename = 'IMG_20240102.png'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(dimensions, ImageDimensions(100, 80))
        self.assertEqual(orientation, 1)
        self.assertEqual(row[:3], (100, 80, 1))
        self.assertIsNotNone(row[3])

    def test_media_metadata_cache_miss_requires_target_lock_but_hit_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            target_path = target / "2024" / "01" / "IMG_20240102.png"
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=remove\n", encoding="utf-8")

            with self.assertRaises(TargetLockError):
                cached_image_dimensions(target, target_path)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                uncached = conn.execute(
                    "SELECT media_width, media_height, media_metadata_mtime_ns FROM files"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(uncached, (None, None, None))

            lock_path.unlink()
            self.assertEqual(cached_image_dimensions(target, target_path), ImageDimensions(100, 80))
            lock_path.write_text("command=remove\n", encoding="utf-8")

            self.assertEqual(cached_image_dimensions(target, target_path), ImageDimensions(100, 80))

    def test_target_command_is_not_available(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["target", "."])

    def test_old_commands_are_not_available(self) -> None:
        for command in (
            "add",
            "import-removable",
            "list-name-conflicts",
            "show-name-conflict",
            "delete",
            "list-deleted",
            "make-face-groups-browser",
            "remove-source",
        ):
            with self.subTest(command=command):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    build_parser().parse_args([command])

    def test_create_stores_collection_id_and_keeps_it_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                first_collection_id = conn.execute(
                    "SELECT value FROM meta WHERE key = 'collection_id'"
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(str(uuid.UUID(first_collection_id)), first_collection_id)

            self.assertEqual(run_cli(["--target", str(target), "status"]), 0)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                second_collection_id = conn.execute(
                    "SELECT value FROM meta WHERE key = 'collection_id'"
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(second_collection_id, first_collection_id)

    def test_create_rejects_existing_collection_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            code, stdout, stderr = capture_cli(["create", str(target)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamling finnes allerede", stderr)

    def test_rejects_target_inside_program_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "bildebank"
            target = repo / "samling"
            repo.mkdir()

            with patch("bildebank.cli.program_repo_root", return_value=repo.resolve()):
                code, stdout, stderr = capture_cli(["create", str(target)])

            self.assertEqual(code, 1)
            self.assertIn("Bildesamlingen kan ikke ligge inni programmappen", stderr)
            self.assertFalse((target / DB_FILENAME).exists())

    def test_wsl_path_from_windows_path_maps_drive_path(self) -> None:
        if os.name == "nt":
            self.skipTest("WSL path mapping is only used outside Windows")

        self.assertEqual(
            wsl_path_from_windows_path(r"C:\Users\TA487\kode\usbA"),
            Path("/mnt/c/Users/TA487/kode/usbA"),
        )



if __name__ == "__main__":
    unittest.main()
