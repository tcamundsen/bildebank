from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import datetime as dt
import os
import time
import uuid
from contextlib import redirect_stderr, redirect_stdout
from http import HTTPStatus
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.cli import (
    build_parser,
    main,
    wsl_path_from_windows_path,
)
from bildebank.cli_server import lan_share_urls, run_server_command
from bildebank.config import AppConfig, BrowserConfig, BrowserHotkeyConfig, FaceRecognitionConfig, OpenClipConfig
from bildebank import db, server_browser
from bildebank.db import DB_FILENAME, init_database
from bildebank.face import (
    add_person_to_file,
    connect_face_db,
    create_person,
    remove_person_from_file,
)
from bildebank.geo import h3_cells_for_manual_cell, h3_cells_for_point
from bildebank.media import ImageDimensions, sha256_file
from bildebank.media_cache import cached_image_dimensions, cached_image_orientation
from bildebank.openclip import ImageSearchResult, connect_openclip_db, embedding_blob, openclip_db_path
from bildebank.server_actions import undelete_file_from_browser
from bildebank.server_assets import SERVER_CSS, SERVER_JS
from bildebank.server_response import add_csrf_to_html
from bildebank.server_files import read_server_file, server_file_path_by_id
from bildebank.server import (
    BildebankServer,
    BildebankRequestHandler,
    is_local_bind_host,
    run_server as run_http_server,
    validate_bind_host,
)
from bildebank.server_pages import (
    app_status_page_html,
    empty_source_html,
    geo_area_page_html,
    geo_index_page_html,
    geo_map_page_html,
    geo_missing_page_html,
    geo_stats_page_html,
    h3_cells_page_html,
    index_html,
    item_page_html,
    markdown_doc_page_html,
    month_page_html,
    person_item_page_html,
    people_page_html,
    person_month_page_html,
    removed_files_page_html,
    search_html,
    search_start_html,
    filter_start_html,
    source_item_page_html,
    source_month_page_html,
    source_year_months_page_html,
    source_years_page_html,
    sources_page_html,
    year_months_page_html,
    years_page_html,
)
from bildebank.server_browser import (
    adjacent_browser_items,
    adjacent_person_items,
    adjacent_source_items,
    browser_item_by_id,
    browser_month_items,
    browser_month_navigation,
    browser_year_cards,
    browser_year_month_cards,
    image_info_content_html,
    item_media_html,
    motion_video_for_image,
    person_item_by_id,
    person_month_items,
    person_month_navigation,
    raw_sidecar_id_by_image_id,
    source_item_by_id,
    source_item_count,
    source_item_ids,
    source_month_items,
    source_month_keys,
    source_month_navigation,
    source_summary_rows,
    valid_year_key,
)
from bildebank.server_browser_sources import (
    all_browser_source,
    imported_source_browser_source,
    parse_source_path,
    person_browser_source,
    source_has_sql_filter,
    tag_browser_source,
)
from bildebank.server_filter import parse_text_filter, text_filter_browser_source
from bildebank.server_faces import (
    cached_person_file_ids,
    clear_face_caches,
    face_overlay_content_html,
    people_for_file,
    person_file_ids,
    person_items,
)
from bildebank.server_search import (
    DEFAULT_SEARCH_LIMIT,
    OpenClipSearchCache,
    ServerSearchStats,
    load_search_embedding_cache,
    search_server_images,
)
from bildebank.target_lock import LOCK_FILENAME, TargetLockError
from tests.test_media import (
    jpeg_with_exif_datetime,
    jpeg_with_exif_camera,
    minimal_mp4_with_creation_date,
    minimal_png,
    minimal_tiff_with_datetime,
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

    def test_run_server_help_documents_local_options(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["run-server", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("usage: bildebank run-server [valg]", stdout)
        self.assertIn("--host", stdout)
        self.assertIn("--port", stdout)
        self.assertIn("--no-browser", stdout)
        self.assertIn("--preview-images", stdout)
        self.assertIn("--read-only", stdout)
        self.assertIn("--lan-share", stdout)
        self.assertIn("--allow-remote", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_run_server_preview_images_is_explicit_and_defaults_to_false(self) -> None:
        default_args = build_parser().parse_args(["run-server"])
        preview_args = build_parser().parse_args(["run-server", "--preview-images"])

        self.assertFalse(default_args.preview_images)
        self.assertTrue(preview_args.preview_images)

    def test_run_server_read_only_is_explicit_and_defaults_to_false(self) -> None:
        default_args = build_parser().parse_args(["run-server"])
        read_only_args = build_parser().parse_args(["run-server", "--read-only"])

        self.assertFalse(default_args.read_only)
        self.assertTrue(read_only_args.read_only)

    def test_run_server_lan_share_is_explicit_and_rejects_host(self) -> None:
        args = build_parser().parse_args(["run-server", "--lan-share", "--port", "8766"])
        self.assertTrue(args.lan_share)
        self.assertIsNone(args.host)
        self.assertEqual(args.port, 8766)

        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["run-server", "--lan-share", "--host", "0.0.0.0"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--lan-share kan ikke brukes sammen med --host", stderr.getvalue())

    def test_lan_share_urls_use_private_ipv4_addresses(self) -> None:
        with patch("bildebank.cli_server.local_lan_ipv4_addresses", return_value=["192.168.86.11"]):
            self.assertEqual(lan_share_urls(8766), ["http://192.168.86.11:8766/"])

    def test_run_server_local_bind_host_detection(self) -> None:
        cases = {
            "127.0.0.1": True,
            "localhost": True,
            "::1": True,
            "0.0.0.0": False,
            "::": False,
            "": False,
            "192.168.1.10": False,
            "10.0.0.5": False,
            "my-pc": False,
        }

        for host, expected in cases.items():
            with self.subTest(host=host):
                self.assertEqual(is_local_bind_host(host), expected)

    def test_run_server_bind_host_requires_explicit_remote_permission(self) -> None:
        with self.assertRaisesRegex(ValueError, "--allow-remote"):
            validate_bind_host("192.168.1.10", allow_remote=False)

        validate_bind_host("192.168.1.10", allow_remote=True)
        validate_bind_host("127.0.0.1", allow_remote=False)

        args = build_parser().parse_args(
            ["run-server", "--host", "0.0.0.0", "--allow-remote", "--no-browser"]
        )
        self.assertTrue(args.allow_remote)
        self.assertEqual(args.host, "0.0.0.0")

    def test_run_server_command_forwards_remote_permission(self) -> None:
        config = AppConfig()
        with (
            patch("bildebank.cli_server.load_config", return_value=config),
            patch("bildebank.cli_server.lan_share_urls", return_value=["http://192.168.86.11:8765/"]),
            patch("bildebank.cli_server.run_local_server") as run_local_server,
            redirect_stdout(StringIO()) as stdout,
        ):
            run_local_server.side_effect = lambda *args, **kwargs: kwargs["ready"]("http://0.0.0.0:8765/")
            result = run_server_command(
                Path("C:/Users/Tom/Bilder"),
                host="0.0.0.0",
                port=8765,
                repo_root=self.program_root,
                browser=False,
                allow_remote=True,
                preview_images=True,
                read_only=True,
                lan_share=True,
            )

        self.assertEqual(result, 0)
        run_local_server.assert_called_once()
        self.assertEqual(run_local_server.call_args.kwargs["host"], "0.0.0.0")
        self.assertTrue(run_local_server.call_args.kwargs["allow_remote"])
        self.assertTrue(run_local_server.call_args.kwargs["preview_images"])
        self.assertTrue(run_local_server.call_args.kwargs["read_only"])
        output = stdout.getvalue()
        self.assertIn("LAN-share er aktiv", output)
        self.assertIn("Serveren kan nås av alle på samme LAN", output)
        self.assertIn("Ikke bruk --lan-share på offentlige nettverk", output)
        self.assertIn("http://192.168.86.11:8765/", output)

    def test_run_server_lan_share_opens_localhost_in_browser(self) -> None:
        config = AppConfig()
        with (
            patch("bildebank.cli_server.load_config", return_value=config),
            patch("bildebank.cli_server.lan_share_urls", return_value=["http://192.168.86.11:8766/"]),
            patch("bildebank.cli_server.run_local_server") as run_local_server,
            patch("bildebank.cli_server.webbrowser.open") as open_browser,
            redirect_stdout(StringIO()),
        ):
            run_local_server.side_effect = lambda *args, **kwargs: kwargs["ready"]("http://0.0.0.0:8766/")
            result = run_server_command(
                Path("C:/Users/Tom/Bilder"),
                host="0.0.0.0",
                port=8766,
                repo_root=self.program_root,
                browser=True,
                allow_remote=True,
                preview_images=True,
                read_only=True,
                lan_share=True,
            )

        self.assertEqual(result, 0)
        open_browser.assert_called_once_with("http://127.0.0.1:8766/")

    def test_run_server_warns_before_allowed_remote_bind(self) -> None:
        fake_server = SimpleNamespace(
            server_address=("0.0.0.0", 8765),
            serve_forever=lambda: None,
            server_close=lambda: None,
        )
        stderr = StringIO()
        with (
            patch("bildebank.server.db.prepare_database"),
            patch("bildebank.server.BildebankServer", return_value=fake_server) as server_class,
            redirect_stderr(stderr),
        ):
            run_http_server(
                Path("."),
                AppConfig(),
                host="0.0.0.0",
                allow_remote=True,
            )

        server_class.assert_called_once_with(
            ("0.0.0.0", 8765),
            Path("."),
            AppConfig(),
            preview_images=False,
            read_only=False,
        )
        self.assertIn("ADVARSEL", stderr.getvalue())
        self.assertIn("andre maskiner på nettverket", stderr.getvalue())

    def test_run_server_creates_server_in_read_only_mode(self) -> None:
        fake_server = SimpleNamespace(
            server_address=("127.0.0.1", 8765),
            serve_forever=lambda: None,
            server_close=lambda: None,
        )
        with (
            patch("bildebank.server.db.prepare_database"),
            patch("bildebank.server.BildebankServer", return_value=fake_server) as server_class,
        ):
            run_http_server(Path("."), AppConfig(), read_only=True)

        server_class.assert_called_once_with(
            ("127.0.0.1", 8765),
            Path("."),
            AppConfig(),
            preview_images=False,
            read_only=True,
        )

    def test_read_only_blocks_admin_gets_and_posts_before_csrf(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        handler.server = SimpleNamespace(read_only=True)
        handler.path = "/settings"
        handler.text_response = None
        handler.json_response = None
        handler.respond_text = lambda content, *, status=HTTPStatus.OK: setattr(
            handler, "text_response", (content, status)
        )
        handler.respond_json = lambda content, *, status=HTTPStatus.OK: setattr(
            handler, "json_response", (content, status)
        )

        BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]
        self.assertEqual(handler.text_response[1], HTTPStatus.FORBIDDEN)

        handler.path = "/api/item-tag"
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]
        self.assertEqual(handler.json_response[1], HTTPStatus.FORBIDDEN)
        self.assertIn("read-only", handler.json_response[0]["error"])

    def test_run_server_display_returns_original_when_preview_images_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            image_path = target / "2024" / "01" / "image.png"
            write_test_image(image_path)
            file_id = register_target_file(target, Path("2024/01/image.png"))
            original = image_path.read_bytes()

            class FakeHandler:
                server = SimpleNamespace(target=target, preview_images=False)
                content = b""
                content_type = ""
                status = HTTPStatus.OK

                def respond_bytes(
                    self,
                    content: bytes,
                    content_type: str,
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.content = content
                    self.content_type = content_type
                    self.status = status

                def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    raise AssertionError(f"{status}: {content}")

                def respond_file(self, encoded_relative_path: str) -> None:
                    BildebankRequestHandler.respond_file(self, encoded_relative_path)  # type: ignore[arg-type]

            handler = FakeHandler()
            BildebankRequestHandler.respond_display(handler, str(file_id))  # type: ignore[arg-type]

        self.assertEqual(handler.content, original)
        self.assertEqual(handler.content_type, "image/png")
        self.assertEqual(handler.status, HTTPStatus.OK)

    def test_run_server_display_returns_scaled_jpeg_when_preview_images_is_true(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            image_path = target / "2024" / "01" / "image.png"
            write_test_image(image_path, size=(3000, 1000))
            file_id = register_target_file(target, Path("2024/01/image.png"))

            class FakeHandler:
                server = SimpleNamespace(target=target, preview_images=True)
                content = b""
                content_type = ""
                status = HTTPStatus.OK

                def respond_bytes(
                    self,
                    content: bytes,
                    content_type: str,
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.content = content
                    self.content_type = content_type
                    self.status = status

                def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    raise AssertionError(f"{status}: {content}")

                def respond_preview_image(self, requested_file_id: int) -> None:
                    BildebankRequestHandler.respond_preview_image(self, requested_file_id)  # type: ignore[arg-type]

            handler = FakeHandler()
            BildebankRequestHandler.respond_display(handler, str(file_id))  # type: ignore[arg-type]
            with Image.open(BytesIO(handler.content)) as preview:
                preview_format = preview.format
                preview_size = preview.size

        self.assertEqual(handler.content_type, "image/jpeg")
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(preview_format, "JPEG")
        self.assertEqual(preview_size, (1600, 533))

    def test_run_server_display_rejects_non_image_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            file_path = target / "2024" / "01" / "video.mp4"
            file_path.parent.mkdir(parents=True)
            file_path.write_bytes(b"not an image")
            file_id = register_target_file(target, Path("2024/01/video.mp4"))

            class FakeHandler:
                server = SimpleNamespace(target=target, preview_images=True)
                body = ""
                status = HTTPStatus.OK

                def respond_bytes(self, content: bytes, content_type: str, *, status=HTTPStatus.OK) -> None:
                    raise AssertionError("Non-image should not return preview bytes")

                def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

                def respond_preview_image(self, requested_file_id: int) -> None:
                    BildebankRequestHandler.respond_preview_image(self, requested_file_id)  # type: ignore[arg-type]

            handler = FakeHandler()
            BildebankRequestHandler.respond_display(handler, str(file_id))  # type: ignore[arg-type]

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("ikke et bilde", handler.body)

    def test_run_server_image_html_uses_display_source_and_original_link(self) -> None:
        body = item_media_html(
            Path("."),
            {
                "id": 7,
                "target_path": "2024/01/image.jpg",
                "stored_filename": "image.jpg",
                "view_rotation_degrees": 0,
            },
        )

        self.assertIn('href="/file/7"', body)
        self.assertIn('src="/display/7"', body)
        self.assertNotIn('src="/file/7"', body)

    def test_run_server_renders_index_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            init_database(target)
            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=OpenClipConfig(enabled=True)),
                face_enabled=True,
                openclip_enabled=True,
            )
            body = index_html(server)
            disabled_server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=OpenClipConfig(enabled=False)),
                face_enabled=True,
                openclip_enabled=False,
            )
            disabled_body = index_html(disabled_server)

        self.assertIn("Bildebrowser", body)
        self.assertIn("Bildesøk", body)
        self.assertIn("Ingen filer i bildesamlingen", body)
        self.assertNotIn("Bildesøk", disabled_body)

    def test_run_server_shell_pages_use_common_topline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            init_database(target)
            config = AppConfig(openclip=OpenClipConfig(enabled=True))
            server = SimpleNamespace(
                target=target,
                config=config,
                face_enabled=True,
                openclip_enabled=True,
                search_cache=SimpleNamespace(loaded=False),
            )
            pages = [
                sources_page_html(target),
                app_status_page_html(target, config),
                geo_stats_page_html(target),
                markdown_doc_page_html(Path("bildebrowser.md"), "# Hjelp\n\nTekst."),
                search_html(server, ServerSearchStats("strand", ()), DEFAULT_SEARCH_LIMIT),
            ]

        for body in pages:
            self.assertIn('<header class="browser-header">', body)
            self.assertIn('<div class="topline">', body)
            self.assertIn('href="/">Alle bilder</a>', body)
            self.assertIn('href="/settings">Innstillinger</a>', body)
            self.assertIn('href="/help/web/bildebrowser">Hjelp</a>', body)

    def test_run_server_search_page_warns_when_model_is_not_loaded(self) -> None:
        server = SimpleNamespace(
            config=AppConfig(openclip=OpenClipConfig(enabled=True)),
            face_enabled=True,
            openclip_enabled=True,
            search_cache=SimpleNamespace(loaded=False),
        )

        body = search_start_html(server)

        self.assertIn("Dette kan ta 10-20 sekunder", body)
        self.assertIn("Laster bildesøkmodellen", body)
        self.assertIn('data-model-loaded="false"', body)
        self.assertIn("data-search-loading", body)

    def test_run_server_search_results_marks_model_loaded(self) -> None:
        server = SimpleNamespace(
            target=Path("/tmp/target"),
            config=AppConfig(openclip=OpenClipConfig(enabled=True)),
            face_enabled=True,
            openclip_enabled=True,
            search_cache=SimpleNamespace(loaded=True),
        )

        body = search_html(server, ServerSearchStats("strand", ()), DEFAULT_SEARCH_LIMIT)

        self.assertIn("Dette kan ta 10-20 sekunder", body)
        self.assertIn('data-model-loaded="true"', body)

    def test_run_server_common_topline_respects_feature_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            init_database(target)
            body = sources_page_html(target, face_enabled=False, openclip_enabled=False)
            enabled_body = sources_page_html(target, openclip_enabled=True)

        self.assertIn('<header class="browser-header">', body)
        self.assertIn('href="/">Alle bilder</a>', body)
        self.assertIn('href="/geo">Steder</a>', body)
        self.assertIn('href="/search" data-search-preload>Bildesøk</a>', enabled_body)
        self.assertIn('fetch("/api/search-preload", {keepalive: true})', SERVER_JS)
        self.assertIn('link.addEventListener("pointerdown", preloadSearchModel)', SERVER_JS)
        self.assertNotIn('href="/people">Personer</a>', body)
        self.assertNotIn('href="/search">Bildesøk</a>', body)

    def test_run_server_search_preload_endpoint_starts_background_load(self) -> None:
        class FakeSearchCache:
            loaded = False
            started = False

            def preload_model_async(self) -> str:
                self.started = True
                return "loading"

        class FakeHandler:
            server = SimpleNamespace(openclip_enabled=True, search_cache=FakeSearchCache())
            body: dict[str, object] | None = None
            status = HTTPStatus.OK

            def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                self.body = content
                self.status = status

        handler = FakeHandler()
        BildebankRequestHandler.respond_search_preload(handler)  # type: ignore[arg-type]

        self.assertTrue(handler.server.search_cache.started)
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.body, {"ok": True, "status": "loading", "loaded": False})

    def test_run_server_image_search_stores_relative_result_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            config = OpenClipConfig()
            conn = connect_openclip_db(target)
            try:
                conn.execute(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        "2024/01/IMG_20240102.jpg",
                        "2024/01/img_20240102.jpg",
                        "sha",
                        config.model_name,
                        config.pretrained,
                        embedding_blob([1.0, 0.0]),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=config),
                search_cache=OpenClipSearchCache(AppConfig(openclip=config)),
            )

            with (
                patch("bildebank.server_search.load_text_model", return_value=(object(), object())),
                patch("bildebank.server_search.text_embedding", return_value=[1.0, 0.0]),
            ):
                stats = search_server_images(server, query="test", limit=10)

            self.assertEqual(len(stats.results), 1)
            conn = sqlite3.connect(openclip_db_path(target))
            try:
                self.assertEqual(
                    conn.execute("SELECT target_path FROM image_search_results").fetchone()[0],
                    "2024/01/IMG_20240102.jpg",
                )
            finally:
                conn.close()

    def test_run_server_image_search_refuses_to_run_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            config = OpenClipConfig()
            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=config),
                search_cache=OpenClipSearchCache(AppConfig(openclip=config)),
            )
            (target / LOCK_FILENAME).write_text("command=image-scan\n", encoding="utf-8")

            with self.assertRaises(TargetLockError):
                search_server_images(server, query="test", limit=10)

    def test_run_server_search_route_reports_target_lock_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            (target / LOCK_FILENAME).write_text("command=image-scan\n", encoding="utf-8")

            class FakeHandler:
                path = "/search?q=test"
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(openclip=OpenClipConfig(enabled=True)),
                    face_enabled=True,
                    openclip_enabled=True,
                    search_cache=OpenClipSearchCache(AppConfig(openclip=OpenClipConfig(enabled=True))),
                )
                body = ""
                status: HTTPStatus | None = None

                def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", handler.body)

    def test_run_server_image_search_reuses_embedding_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            config = OpenClipConfig()
            conn = connect_openclip_db(target)
            try:
                conn.executemany(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (1, "2024/01/a.jpg", "2024/01/a.jpg", "sha1", config.model_name, config.pretrained, embedding_blob([1.0, 0.0])),
                        (2, "2024/01/b.jpg", "2024/01/b.jpg", "sha2", config.model_name, config.pretrained, embedding_blob([0.0, 1.0])),
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=config),
                search_cache=OpenClipSearchCache(AppConfig(openclip=config)),
            )

            with (
                patch("bildebank.server_search.load_text_model", return_value=(object(), object())),
                patch("bildebank.server_search.text_embedding", return_value=[1.0, 0.0]),
                patch("bildebank.server_search.load_search_embedding_cache", wraps=load_search_embedding_cache) as load_cache,
            ):
                first = search_server_images(server, query="test", limit=1)
                second = search_server_images(server, query="test igjen", limit=1)

        self.assertEqual(load_cache.call_count, 1)
        self.assertEqual(first.results[0].file_id, 1)
        self.assertEqual(second.results[0].file_id, 1)

    def test_run_server_image_search_reloads_embedding_cache_when_database_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            config = OpenClipConfig()
            conn = connect_openclip_db(target)
            try:
                conn.execute(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (1, "2024/01/a.jpg", "2024/01/a.jpg", "sha1", config.model_name, config.pretrained, embedding_blob([1.0, 0.0])),
                )
                conn.commit()
            finally:
                conn.close()
            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=config),
                search_cache=OpenClipSearchCache(AppConfig(openclip=config)),
            )

            with (
                patch("bildebank.server_search.load_text_model", return_value=(object(), object())),
                patch("bildebank.server_search.text_embedding", return_value=[0.0, 1.0]),
                patch("bildebank.server_search.load_search_embedding_cache", wraps=load_search_embedding_cache) as load_cache,
            ):
                first = search_server_images(server, query="test", limit=10)
                conn = connect_openclip_db(target)
                try:
                    conn.execute(
                        """
                        INSERT INTO image_embeddings(
                            file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            2,
                            "2024/01/b.jpg",
                            "2024/01/b.jpg",
                            "sha2",
                            config.model_name,
                            config.pretrained,
                            embedding_blob([0.0, 1.0]),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
                second = search_server_images(server, query="test igjen", limit=10)

        self.assertEqual(load_cache.call_count, 2)
        self.assertEqual([result.file_id for result in first.results], [1])
        self.assertEqual([result.file_id for result in second.results], [2, 1])

    def test_run_server_image_search_numpy_ranking_matches_cosine_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            config = OpenClipConfig()
            conn = connect_openclip_db(target)
            try:
                conn.executemany(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (1, "2024/01/a.jpg", "2024/01/a.jpg", "sha1", config.model_name, config.pretrained, embedding_blob([1.0, 0.0])),
                        (2, "2024/01/b.jpg", "2024/01/b.jpg", "sha2", config.model_name, config.pretrained, embedding_blob([0.8, 0.6])),
                        (3, "2024/01/c.jpg", "2024/01/c.jpg", "sha3", config.model_name, config.pretrained, embedding_blob([0.0, 1.0])),
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=config),
                search_cache=OpenClipSearchCache(AppConfig(openclip=config)),
            )

            with (
                patch("bildebank.server_search.load_text_model", return_value=(object(), object())),
                patch("bildebank.server_search.text_embedding", return_value=[1.0, 0.0]),
            ):
                stats = search_server_images(server, query="test", limit=2)

        self.assertEqual([result.file_id for result in stats.results], [1, 2])
        self.assertGreater(stats.results[0].similarity, stats.results[1].similarity)

    def test_run_server_image_search_filters_out_of_focus_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            first_path = target / "2024" / "01" / "a.jpg"
            second_path = target / "2024" / "01" / "b.jpg"
            first_path.parent.mkdir(parents=True, exist_ok=True)
            first_path.write_bytes(b"image-a")
            second_path.write_bytes(b"image-b")
            register_target_file(target, Path("2024/01/a.jpg"))
            register_target_file(target, Path("2024/01/b.jpg"))
            conn = db.connect(target)
            try:
                db.tag_file(conn, file_id=1, tag_name=db.SYSTEM_TAG_OUT_OF_FOCUS)
                conn.commit()
            finally:
                conn.close()

            config = OpenClipConfig()
            conn = connect_openclip_db(target)
            try:
                conn.executemany(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (1, "2024/01/a.jpg", "2024/01/a.jpg", "sha1", config.model_name, config.pretrained, embedding_blob([1.0, 0.0])),
                        (2, "2024/01/b.jpg", "2024/01/b.jpg", "sha2", config.model_name, config.pretrained, embedding_blob([0.9, 0.1])),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            app_config = AppConfig(openclip=config, browser=BrowserConfig(hide_out_of_focus=True))
            server = SimpleNamespace(
                target=target,
                config=app_config,
                search_cache=OpenClipSearchCache(app_config),
            )

            with (
                patch("bildebank.server_search.load_text_model", return_value=(object(), object())),
                patch("bildebank.server_search.text_embedding", return_value=[1.0, 0.0]),
            ):
                stats = search_server_images(server, query="test", limit=2)

        self.assertEqual([result.file_id for result in stats.results], [2])

    def test_run_server_image_search_ignores_orphan_openclip_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            active_id = insert_test_file(target, "2024/01/active.png", sha256="sha-active")
            missing_id = active_id + 100
            config = OpenClipConfig()
            conn = connect_openclip_db(target)
            try:
                conn.executemany(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256,
                        model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            active_id,
                            "2024/01/active.png",
                            "2024/01/active.png",
                            "sha-active",
                            config.model_name,
                            config.pretrained,
                            embedding_blob([1.0, 0.0]),
                        ),
                        (
                            missing_id,
                            "2026/01/unimported.png",
                            "2026/01/unimported.png",
                            "sha-missing",
                            config.model_name,
                            config.pretrained,
                            embedding_blob([0.0, 1.0]),
                        ),
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=config),
                search_cache=OpenClipSearchCache(AppConfig(openclip=config)),
            )

            with (
                patch("bildebank.server_search.load_text_model", return_value=(object(), object())),
                patch("bildebank.server_search.text_embedding", return_value=[0.0, 1.0]),
            ):
                stats = search_server_images(server, query="cat", limit=10)

        self.assertEqual([result.file_id for result in stats.results], [active_id])
        self.assertEqual(stats.results[0].target_path, Path("2024/01/active.png"))

    def test_run_server_image_search_links_item_but_uses_target_path_for_image_url(self) -> None:
        target = Path("/tmp/target")
        server = SimpleNamespace(
            target=target,
            config=AppConfig(openclip=OpenClipConfig(enabled=True)),
            face_enabled=False,
            openclip_enabled=True,
            search_cache=SimpleNamespace(loaded=True),
        )
        result = ImageSearchResult(
            rank=1,
            file_id=999,
            target_path=Path("2025/07/PXL 20250709_193516074.jpg"),
            similarity=0.301,
        )

        body = search_html(server, ServerSearchStats("red wine", (result,)), DEFAULT_SEARCH_LIMIT)

        self.assertIn('src="/file/2025/07/PXL%2020250709_193516074.jpg"', body)
        self.assertIn('href="/item/999"', body)
        self.assertNotIn('href="/file/2025/07/PXL%2020250709_193516074.jpg"', body)
        self.assertNotIn('src="/file/999"', body)

    def test_run_server_image_search_rotates_rotated_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                conn.execute("UPDATE files SET view_rotation_degrees = 90 WHERE id = 1")
                conn.commit()
            finally:
                conn.close()

            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=OpenClipConfig(enabled=True)),
                face_enabled=False,
                openclip_enabled=True,
                search_cache=SimpleNamespace(loaded=True),
            )
            result = ImageSearchResult(
                rank=1,
                file_id=1,
                target_path=Path("2024/01/IMG_20240102.png"),
                similarity=0.301,
            )

            body = search_html(server, ServerSearchStats("strand", (result,)), DEFAULT_SEARCH_LIMIT)

        self.assertIn('class="media-link quarter-turn"', body)
        self.assertIn('data-view-rotation="90"', body)
        self.assertIn("transform: rotate(90deg)", body)

    def test_run_server_h3_cells_page_saves_and_lists_named_cell(self) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        form = f"name=Oslo&h3_cell={h3_cell}".encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            active_path = target / "2024/01/active.png"
            deleted_path = target / "2024/01/deleted.png"
            active_path.parent.mkdir(parents=True)
            active_path.write_bytes(minimal_png(100, 80))
            deleted_path.write_bytes(minimal_png(101, 80))
            active_id = register_target_file(target, Path("2024/01/active.png"))
            deleted_id = register_target_file(target, Path("2024/01/deleted.png"))
            cells = h3_cells_for_point(59.91273, 10.74609)
            conn = db.connect(target)
            try:
                db.update_file_gps(
                    conn,
                    file_id=active_id,
                    gps_lat=59.91273,
                    gps_lon=10.74609,
                    gps_alt=None,
                    h3_cells=cells,
                    gps_source="test",
                    gps_error=None,
                )
                db.update_file_gps(
                    conn,
                    file_id=deleted_id,
                    gps_lat=59.91273,
                    gps_lon=10.74609,
                    gps_alt=None,
                    h3_cells=cells,
                    gps_source="test",
                    gps_error=None,
                )
                conn.execute("UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (deleted_id,))
                conn.commit()
            finally:
                conn.close()

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(form)),
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                rfile = BytesIO(form)
                server = SimpleNamespace(target=target, face_enabled=True, openclip_enabled=True)
                redirect_url = ""

                def redirect(self, url: str) -> None:
                    self.redirect_url = url

                def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    raise AssertionError(f"Unexpected error response {status}: {content}")

            handler = FakeHandler()
            BildebankRequestHandler.respond_set_h3_cell_name(handler)  # type: ignore[arg-type]
            body = h3_cells_page_html(target)

            conn = db.connect(target)
            try:
                rows = db.geo_place_names(conn)
            finally:
                conn.close()

        self.assertEqual(handler.redirect_url, "/settings/h3-cells")
        self.assertEqual([(row["h3_cell"], row["name"]) for row in rows], [(h3_cell, "Oslo")])
        self.assertIn("Oslo", body)
        self.assertIn(h3_cell, body)
        self.assertIn(
            f'<a href="https://h3geo.org/#hex={h3_cell}" target="_blank" rel="noopener">{h3_cell}</a>',
            body,
        )
        self.assertIn('<div class="custom-place-list h3-cell-list">', body)
        self.assertIn('<details class="custom-place-edit">', body)
        self.assertIn('<span class="status">1 bilder</span>', body)
        self.assertIn('<span class="status">H3-7</span>', body)
        self.assertIn(f'<input type="hidden" name="original_h3_cell" value="{h3_cell}">', body)
        self.assertIn(f'name="h3_cell" value="{h3_cell}"', body)
        self.assertIn('formaction="/settings/h3-cell-delete"', body)
        self.assertIn('data-confirm-submit="Slette navn gitt til H3-celle?"', body)
        self.assertIn(">Slett</button>", body)
        self.assertIn("event.submitter?.dataset.confirmSubmit", SERVER_JS)
        self.assertIn("event.preventDefault()", SERVER_JS)

    def test_run_server_h3_cells_page_updates_and_deletes_named_cell(self) -> None:
        original_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        updated_cell = h3_cells_for_point(60.39299, 5.32415)["h3_res7"]
        update_form = f"original_h3_cell={original_cell}&name=Bergen&h3_cell={updated_cell}".encode("utf-8")
        delete_form = f"original_h3_cell={updated_cell}&name=Bergen&h3_cell={updated_cell}".encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = db.connect(target)
            try:
                db.set_geo_place_name(conn, original_cell, "Oslo")
                conn.commit()
            finally:
                conn.close()

            class FakeHandler:
                server = SimpleNamespace(target=target, face_enabled=True, openclip_enabled=True)
                redirect_url = ""

                def __init__(self, data: bytes) -> None:
                    self.headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/x-www-form-urlencoded",
                    }
                    self.rfile = BytesIO(data)

                def redirect(self, url: str) -> None:
                    self.redirect_url = url

                def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    raise AssertionError(f"Unexpected error response {status}: {content}")

            update_handler = FakeHandler(update_form)
            BildebankRequestHandler.respond_set_h3_cell_name(update_handler)  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                after_update = [(row["h3_cell"], row["name"]) for row in db.geo_place_names(conn)]
            finally:
                conn.close()

            delete_handler = FakeHandler(delete_form)
            BildebankRequestHandler.respond_delete_h3_cell_name(delete_handler)  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                after_delete = [(row["h3_cell"], row["name"]) for row in db.geo_place_names(conn)]
            finally:
                conn.close()

        self.assertEqual(update_handler.redirect_url, "/settings/h3-cells")
        self.assertEqual(after_update, [(updated_cell, "Bergen")])
        self.assertEqual(delete_handler.redirect_url, "/settings/h3-cells")
        self.assertEqual(after_delete, [])

    def test_run_server_face_enabled_uses_server_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "program"
            target = Path(tmp) / "target"
            root.mkdir()
            target.mkdir()
            server = object.__new__(BildebankServer)
            server.target = target
            server.config = AppConfig(face_recognition=FaceRecognitionConfig(enabled=False))

            self.assertFalse(server.face_enabled)
            (root / "bildebank-config.toml").write_text(
                "[face_recognition]\nenabled = true\n",
                encoding="utf-8",
            )
            self.assertFalse(server.face_enabled)

    def test_run_server_browser_month_keys_uses_existing_database_path_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            init_database(target)
            server = object.__new__(BildebankServer)
            server.target = target
            server._browser_item_ids = {}
            server._browser_month_keys = {}

            self.assertEqual(server.browser_month_keys(), [])
            self.assertEqual(server.browser_item_ids(), [])

    def test_run_server_navigation_cache_version_throttles_database_stat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            init_database(target)
            server = object.__new__(BildebankServer)
            server.target = target
            server._browser_item_ids = {}
            server._browser_month_keys = {}
            server._browser_navigation_cache_version = 0
            server._browser_navigation_db_mtime_ns = None
            server._browser_navigation_checked_at = 0.0

            with patch("bildebank.server.time.monotonic", side_effect=[10.0, 10.5]):
                with patch("bildebank.server.db.db_path_for_target", wraps=db.db_path_for_target) as db_path_for_target:
                    self.assertEqual(server.browser_navigation_cache_version(), 0)
                    self.assertEqual(server.browser_navigation_cache_version(), 0)

            self.assertEqual(db_path_for_target.call_count, 1)

    def test_run_server_renders_bookmarkable_item_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20231201.jpg").write_bytes(b"image-one")
            (source / "IMG_20240102.jpg").write_bytes(b"image-two")
            (source / "IMG_20240203.jpg").write_bytes(b"image-three")
            (source / "IMG_20250104.jpg").write_bytes(b"image-four")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            item = browser_item_by_id(target, 2)
            self.assertIsNotNone(item)
            previous_item, next_item = adjacent_browser_items(target, item)
            body = item_page_html(target, item, previous_item, next_item, browser_month_navigation(target, item))

        self.assertIn('<nav class="breadcrumb" aria-label="Plassering">', body)
        self.assertIn('href="/years">År</a>', body)
        self.assertIn('href="/years/2024">2024</a>', body)
        self.assertIn('href="/month/2024-01">Januar</a>', body)
        self.assertIn('href="/item/2">2</a>', body)
        self.assertIn('data-open-info data-info-item="2"', body)
        self.assertIn('aria-label="Åpne bildeinfo for IMG_20240102.jpg"', body)
        self.assertIn("/file/2", body)
        self.assertIn('href="/years/2023" title="Forrige år" data-key-nav="previous-year">◀ Å</a>', body)
        self.assertIn('href="/years/2025" title="Neste år" data-key-nav="next-year">r ▶</a>', body)
        self.assertIn('href="/month/2023-12" title="Forrige måned" data-key-nav="previous-month">◀ Mån</a>', body)
        self.assertIn('href="/month/2024-02" title="Neste måned" data-key-nav="next-month">ed ▶</a>', body)
        self.assertNotIn('href="/month/2023-12" title="Forrige år"', body)
        self.assertNotIn('href="/month/2025-01" title="Neste år"', body)
        self.assertIn("/search", body)
        self.assertIn('href="/settings">Innstillinger</a>', body)
        self.assertNotIn("Dato fra filnavn", body)
        self.assertNotIn("Dato fra mtime", body)
        self.assertNotIn("Månedsoversikt</a>", body)
        self.assertIn('data-key-nav="previous"', body)
        self.assertIn('data-key-nav="next"', body)
        self.assertIn('data-key-nav="previous-year"', body)
        self.assertIn('data-key-nav="next-year"', body)
        self.assertIn('data-key-nav="previous-month"', body)
        self.assertIn('data-key-nav="next-month"', body)
        self.assertIn('data-nav-button-pair="year"', body)
        self.assertIn('data-nav-button-pair="month"', body)
        self.assertIn('data-nav-button-pair="item"', body)
        self.assertIn('href="/static/server.css?v=', body)
        self.assertIn('src="/static/server.js?v=', body)
        self.assertNotIn('<footer class="browser-footer">', body)
        self.assertIn("ArrowLeft", SERVER_JS)
        self.assertIn("ArrowRight", SERVER_JS)
        self.assertIn("ArrowUp", SERVER_JS)
        self.assertIn("ArrowDown", SERVER_JS)
        self.assertIn("PageUp", SERVER_JS)
        self.assertIn("PageDown", SERVER_JS)
        self.assertIn("function attachSwipeNavigation", SERVER_JS)
        self.assertIn("const minDistance = 40;", SERVER_JS)
        self.assertIn("const verticalDominanceRatio = 0.75;", SERVER_JS)
        self.assertIn("absX <= absY * verticalDominanceRatio", SERVER_JS)
        self.assertIn("window.PointerEvent", SERVER_JS)
        self.assertIn("container.setPointerCapture(event.pointerId)", SERVER_JS)
        self.assertIn('event.pointerType !== "touch" && event.pointerType !== "pen"', SERVER_JS)
        self.assertIn('container.addEventListener("touchstart"', SERVER_JS)
        self.assertIn('direction > 0 ? \'[data-key-nav="next"]\' : \'[data-key-nav="previous"]\'', SERVER_JS)

    def test_run_server_item_breadcrumb_day_links_to_first_item_on_same_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "A_20240102.jpg").write_bytes(b"image-a")
            (source / "B_20240102.jpg").write_bytes(b"image-b")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            conn = db.connect(target)
            try:
                rows = {
                    str(row["stored_filename"]): int(row["id"])
                    for row in conn.execute("SELECT id, stored_filename FROM files")
                }
            finally:
                conn.close()
            first_id = rows["A_20240102.jpg"]
            second_id = rows["B_20240102.jpg"]
            item = browser_item_by_id(target, second_id)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))

        self.assertIn(
            f'href="/month/2024-01">Januar</a><span class="sep">/</span><a href="/item/{first_id}">2</a>',
            body,
        )
        self.assertIn(f'data-open-info data-info-item="{second_id}"', body)

    def test_run_server_source_item_breadcrumb_day_uses_source_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source_a = Path(tmp) / "source-a"
            source_b = Path(tmp) / "source-b"
            source_a.mkdir()
            source_b.mkdir()
            (source_a / "A_20240102.jpg").write_bytes(b"image-a")
            (source_a / "B_20240102.jpg").write_bytes(b"image-b")
            (source_b / "0_20240102.jpg").write_bytes(b"other-source")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source-a", "--quiet", str(source_a)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source-b", "--quiet", str(source_b)]), 0)
            conn = db.connect(target)
            try:
                imported = db.find_source_by_name(conn, "source-a")
                self.assertIsNotNone(imported)
                rows = {
                    str(row["stored_filename"]): int(row["id"])
                    for row in conn.execute("SELECT id, stored_filename FROM files")
                }
            finally:
                conn.close()
            source = imported_source_browser_source(imported)
            first_id = rows["A_20240102.jpg"]
            second_id = rows["B_20240102.jpg"]
            item = source_item_by_id(target, source, second_id)
            self.assertIsNotNone(item)
            body = source_item_page_html(
                target,
                source,
                item,
                *adjacent_source_items(target, source, item),
                source_month_navigation(target, source, item),
            )

        self.assertIn(
            f'href="/source/1/month/2024-01">Januar</a><span class="sep">/</span><a href="/source/1/item/{first_id}">2</a>',
            body,
        )

    def test_run_server_source_item_breadcrumb_day_avoids_global_raw_sidecar_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "DSC_0170.JPG").write_bytes(jpeg_with_exif_datetime("2019:03:03 12:00:00"))
            (source / "DSC_0170.NEF").write_bytes(minimal_tiff_with_datetime("2019:03:03 12:00:00"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            source_filter = text_filter_browser_source("filename:DSC_0170")
            image_item = next(item for item in source_month_items(target, source_filter, "2019-03") if item["stored_filename"] == "DSC_0170.JPG")
            previous_item, next_item = adjacent_source_items(target, source_filter, image_item)
            month_nav = source_month_navigation(target, source_filter, image_item)
            self.assertIsNotNone(raw_sidecar_id_by_image_id(target, int(image_item["id"])))
            with patch("bildebank.server_browser.raw_sidecar_groups", side_effect=AssertionError("global raw scan")):
                body = source_item_page_html(
                    target,
                    source_filter,
                    image_item,
                    previous_item,
                    next_item,
                    month_nav,
                )

        self.assertIn('href="/filter/filename%3ADSC_0170/item/', body)
        self.assertIn(">3</a>", body)

    def test_run_server_nef_item_reuses_raw_sidecar_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "DSC_0170.JPG").write_bytes(jpeg_with_exif_datetime("2019:03:03 12:00:00"))
            (source / "DSC_0170.NEF").write_bytes(minimal_tiff_with_datetime("2019:03:03 12:00:00"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            source_filter = text_filter_browser_source("filename:NEF")
            nef_item = next(item for item in source_month_items(target, source_filter, "2019-03") if item["stored_filename"] == "DSC_0170.NEF")
            previous_item, next_item = adjacent_source_items(target, source_filter, nef_item)
            month_nav = source_month_navigation(target, source_filter, nef_item)

            with (
                patch(
                    "bildebank.server_browser.query_raw_sidecar_ids_by_image_id",
                    wraps=server_browser.query_raw_sidecar_ids_by_image_id,
                ) as raw_sidecar_ids,
            ):
                first_body = source_item_page_html(
                    target,
                    source_filter,
                    nef_item,
                    previous_item,
                    next_item,
                    month_nav,
                    face_enabled=False,
                    openclip_enabled=False,
                )
                source_item_page_html(
                    target,
                    source_filter,
                    nef_item,
                    previous_item,
                    next_item,
                    month_nav,
                    face_enabled=False,
                    openclip_enabled=False,
                )

        self.assertIn("Vis JPG-bildet", first_body)
        self.assertEqual(raw_sidecar_ids.call_count, 1)

    def test_run_server_top_steder_link_points_to_geo_not_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))

        self.assertIn('<a class="server-search-link" href="/geo">Steder</a>', body)
        self.assertLess(body.index('href="/geo">Steder'), body.index('href="/search"'))

    def test_run_server_geo_pages_use_stored_geo_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(10, 10))
            (source / "IMG_20240103.png").write_bytes(minimal_png(11, 10))
            (source / "IMG_20240104.png").write_bytes(minimal_png(12, 10))
            (source / "IMG_20240105.png").write_bytes(minimal_png(13, 10))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            import h3

            cells = h3_cells_for_point(59.91273, 10.74609)
            neighbor_cell = next(cell for cell in sorted(h3.grid_disk(cells["h3_res7"], 1)) if cell != cells["h3_res7"])
            conn = db.connect(target)
            try:
                db.update_file_gps(
                    conn,
                    file_id=1,
                    gps_lat=59.91273,
                    gps_lon=10.74609,
                    gps_alt=None,
                    h3_cells=cells,
                    gps_source="test",
                    gps_error=None,
                )
                db.update_file_gps(
                    conn,
                    file_id=2,
                    gps_lat=59.91274,
                    gps_lon=10.74610,
                    gps_alt=None,
                    h3_cells=cells,
                    gps_source="test",
                    gps_error=None,
                )
                db.set_file_manual_h3_location(conn, file_id=4, h3_cells=h3_cells_for_manual_cell(neighbor_cell))
                db.set_geo_place_name(conn, cells["h3_res6"], "Oslo-området")
                conn.execute("UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = 2")
                conn.commit()
            finally:
                conn.close()

            index_body = geo_index_page_html(target, resolution=7, min_count=1, limit=10)
            map_body = geo_map_page_html(target, resolution=7, min_count=1, limit=10)
            map_zero_body = geo_map_page_html(target, resolution=0, min_count=1, limit=10)
            stats_body = geo_stats_page_html(target)
            area_body = geo_area_page_html(target, cells["h3_res7"], resolution=7, limit=10)
            empty_area_body = geo_area_page_html(target, "8001fffffffffff", resolution=0, limit=10)
            missing_body = geo_missing_page_html(target, limit=10, offset=0)

        self.assertIn("Steder", index_body)
        self.assertIn("/geo/map?resolution=7&min_count=1&limit=10", index_body)
        self.assertIn("Heksagonkart", map_body)
        self.assertIn('href="/geo">Steder</a><span class="sep">/</span>Heksagonkart</nav>', map_body)
        self.assertIn('href="/geo">Steder</a><span class="sep">/</span>Geo-statistikk</nav>', stats_body)
        self.assertIn(f'href="/geo">Steder</a><span class="sep">/</span>{cells["h3_res7"]}</nav>', area_body)
        self.assertIn('href="/geo">Steder</a><span class="sep">/</span>Bilder uten GPS</nav>', missing_body)
        self.assertIn('<form action="/geo/map" method="get" class="geo-filter">', map_body)
        self.assertIn('<select name="resolution">', map_body)
        self.assertIn('<option value="7" selected>H3-7 (ca. 5 km²)</option>', map_body)
        self.assertIn('<option value="0" selected>H3-0 (ca. 4 357 450 km²)</option>', map_zero_body)
        self.assertIn(f'href="/geo/area/{cells["h3_res7"]}"', map_body)
        self.assertIn(f'href="/geo/area/{neighbor_cell}"', map_body)
        self.assertIn("geo-hex", map_body)
        self.assertIn(">1</text>", map_body)
        self.assertIn("Med GPS", stats_body)
        self.assertIn("<div><strong>Manuell H3</strong><span>1</span></div>", stats_body)
        self.assertIn("IMG_20240102.png", area_body)
        self.assertIn("google.com/maps/@", area_body)
        self.assertIn(",11z", area_body)
        self.assertIn("Åpne i Google Maps (sentrum av H3-7)", area_body)
        self.assertIn('href="https://www.google.com/maps/@', empty_area_body)
        self.assertIn(",2z", empty_area_body)
        self.assertIn("Ingen aktive bilder i dette området.", empty_area_body)
        self.assertIn('href="https://h3geo.org/#hex=' + cells["h3_res7"] + '"', area_body)
        self.assertIn(">H3Geo</a>", area_body)
        self.assertIn("oppløsning 7, ca. 5 km²", area_body)
        self.assertIn(f'href="/geo/area/{cells["h3_res6"]}"', area_body)
        self.assertIn("Større område: H3-6 Oslo-området", area_body)
        self.assertNotIn(f"Større område: H3-6 {cells['h3_res6']}", area_body)
        self.assertNotIn("IMG_20240103.png", area_body)
        self.assertIn("IMG_20240104.png", missing_body)

    def test_run_server_item_page_does_not_show_nearby_geo_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(10, 10))
            (source / "IMG_20240103.png").write_bytes(minimal_png(11, 10))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            cells = h3_cells_for_point(59.91273, 10.74609)
            conn = db.connect(target)
            try:
                for file_id in (1, 2):
                    db.update_file_gps(
                        conn,
                        file_id=file_id,
                        gps_lat=59.91273,
                        gps_lon=10.74609,
                        gps_alt=None,
                        h3_cells=cells,
                        gps_source="test",
                        gps_error=None,
                    )
                conn.commit()
            finally:
                conn.close()
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))

        self.assertNotIn("Nærliggende bilder", body)
        self.assertNotIn("IMG_20240103.png", body)

    def test_run_server_month_page_uses_browser_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20231201.jpg").write_bytes(b"image-one")
            (source / "IMG_20240102.jpg").write_bytes(b"image-two")
            (source / "IMG_20240203.jpg").write_bytes(b"image-three")
            (source / "IMG_20250104.jpg").write_bytes(b"image-four")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            body = month_page_html(target, "2024-01", browser_month_items(target, "2024-01"))

        for label in (
            "◀ Å",
            "r ▶",
            "◀ Mån",
            "ed ▶",
            "◀ Bil",
            "de ▶",
        ):
            self.assertIn(label, body)
        self.assertIn('href="/years/2023" title="Forrige år" data-key-nav="previous-year">◀ Å</a>', body)
        self.assertIn('href="/years/2025" title="Neste år" data-key-nav="next-year">r ▶</a>', body)
        self.assertIn('href="/month/2023-12" title="Forrige måned" data-key-nav="previous-month">◀ Mån</a>', body)
        self.assertIn('href="/month/2024-02" title="Neste måned" data-key-nav="next-month">ed ▶</a>', body)
        self.assertNotIn('href="/month/2023-12" title="Forrige år"', body)
        self.assertNotIn('href="/month/2025-01" title="Neste år"', body)
        self.assertIn('href="/years">År</a>', body)
        self.assertIn('href="/years/2024">2024</a>', body)
        self.assertIn('<span class="sep">/</span>Januar</nav>', body)
        self.assertIn('data-key-nav="previous"', body)
        self.assertIn('data-key-nav="next"', body)
        self.assertIn('data-key-nav="previous-year"', body)
        self.assertIn('data-key-nav="next-year"', body)
        self.assertIn('data-key-nav="previous-month"', body)
        self.assertIn('data-key-nav="next-month"', body)
        self.assertIn('data-nav-button-pair="year"', body)
        self.assertIn('data-nav-button-pair="month"', body)
        self.assertIn('data-nav-button-pair="item"', body)
        self.assertIn('<main class="server-browser month-browser">', body)
        self.assertNotIn('<footer class="browser-footer">', body)

    def test_run_server_first_month_previous_month_links_to_years(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_19700102.jpg").write_bytes(b"image-one")
            (source / "IMG_19700203.jpg").write_bytes(b"image-two")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            items = browser_month_items(target, "1970-01")
            month_body = month_page_html(target, "1970-01", items)
            later_first_year_items = browser_month_items(target, "1970-02")
            later_first_year_month_body = month_page_html(target, "1970-02", later_first_year_items)
            item = items[0]
            item_body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )
            later_first_year_item = later_first_year_items[0]
            later_first_year_item_body = item_page_html(
                target,
                later_first_year_item,
                *adjacent_browser_items(target, later_first_year_item),
                browser_month_navigation(target, later_first_year_item),
            )
            empty_month_body = month_page_html(target, "1969-12", [])

        self.assertIn(
            'href="/years" title="Forrige år" data-key-nav="previous-year">◀ Å</a>',
            month_body,
        )
        self.assertIn(
            'href="/years" title="Forrige år" data-key-nav="previous-year">◀ Å</a>',
            later_first_year_month_body,
        )
        self.assertIn(
            'href="/years" title="Forrige år" data-key-nav="previous-year">◀ Å</a>',
            later_first_year_item_body,
        )
        self.assertIn(
            'href="/years" title="Forrige måned" data-key-nav="previous-month">◀ Mån</a>',
            month_body,
        )
        self.assertIn(
            'href="/years" title="Forrige måned" data-key-nav="previous-month">◀ Mån</a>',
            item_body,
        )
        self.assertIn('<span class="nav-button disabled">◀ Å</span>', empty_month_body)
        self.assertIn('<span class="nav-button disabled">◀ Mån</span>', empty_month_body)

    def test_run_server_years_pages_link_to_years_and_months(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            for name, content in (
                ("IMG_20050301.mp4", b"video-2005-03"),
                ("IMG_20050302.jpg", b"image-2005-03"),
                ("IMG_20050401.jpg", b"image-2005-04"),
                ("IMG_20050501.jpg", b"image-2005-05"),
                ("IMG_20060401.jpg", b"image-2006-04"),
                ("IMG_20070401.jpg", b"image-2007-04"),
            ):
                (source / name).write_bytes(content)

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                conn.execute("UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE target_path LIKE ?", ("%2006/04/%",))
                out_of_focus_file = conn.execute(
                    "SELECT id FROM files WHERE target_path LIKE ?",
                    ("%2007/04/%",),
                ).fetchone()
                self.assertIsNotNone(out_of_focus_file)
                db.tag_file(
                    conn,
                    file_id=int(out_of_focus_file["id"]),
                    tag_name=db.SYSTEM_TAG_OUT_OF_FOCUS,
                )
                conn.commit()
            finally:
                conn.close()

            with patch("bildebank.server_browser.browser_month_keys", wraps=server_browser.browser_month_keys) as month_keys:
                years_body = years_page_html(target)
            year_body = year_months_page_html(target, "2005")
            filtered_years_body = years_page_html(target, hide_out_of_focus=True)
            filtered_year_body = year_months_page_html(target, "2007", hide_out_of_focus=True)
            year_cards = browser_year_cards(target, hide_out_of_focus=True)
            month_cards = browser_year_month_cards(target, "2005")
            with patch("bildebank.server_browser.browser_month_items", wraps=server_browser.browser_month_items) as month_items:
                optimized_year_cards = server_browser.browser_year_cards(target)
            with patch("bildebank.server_browser.all_source_where", wraps=server_browser.all_source_where) as all_where:
                server_browser.browser_year_summaries(target)

        self.assertIn('href="/years/2005"', years_body)
        self.assertIn('data-nav-button-pair="year"', years_body)
        self.assertIn('data-nav-button-pair="month"', years_body)
        self.assertIn('<span class="nav-button disabled">◀ Å</span>', years_body)
        self.assertIn('<span class="nav-button disabled">◀ Mån</span>', years_body)
        self.assertIn('href="/years/2005" title="Neste år" data-key-nav="next-year">r ▶</a>', years_body)
        self.assertIn('href="/month/2005-03" title="Neste måned" data-key-nav="next-month">ed ▶</a>', years_body)
        self.assertIn('href="/years">År</a><span class="sep">/</span>2005</nav>', year_body)
        self.assertIn(">2005</div>", years_body)
        self.assertIn(">3 måneder, 4 bilder</div>", years_body)
        self.assertNotIn('href="/years/2006"', years_body)
        self.assertIn('href="/years/2007"', years_body)
        self.assertIn('src="/file/2005/03/IMG_20050302.jpg"', years_body)
        self.assertNotIn("Video<br>IMG_20050301.mp4", years_body)
        self.assertEqual(month_keys.call_count, 0)
        self.assertIn('href="/month/2005-03"', year_body)
        self.assertIn('href="/month/2005-04"', year_body)
        self.assertIn('href="/month/2005-05"', year_body)
        self.assertIn('data-nav-button-pair="year"', year_body)
        self.assertIn('data-nav-button-pair="month"', year_body)
        self.assertIn('href="/years" title="Forrige år" data-key-nav="previous-year">◀ Å</a>', year_body)
        self.assertIn('href="/years/2007" title="Neste år" data-key-nav="next-year">r ▶</a>', year_body)
        self.assertIn('href="/years" title="Forrige måned" data-key-nav="previous-month">◀ Mån</a>', year_body)
        self.assertIn('href="/month/2005-03" title="Neste måned" data-key-nav="next-month">ed ▶</a>', year_body)
        self.assertIn('<section class="month-grid-server year-month-grid-server">', year_body)
        self.assertIn('src="/file/2005/03/IMG_20050302.jpg"', year_body)
        self.assertIn(">2005-04</div>", year_body)
        self.assertIn(">1 bilde</div>", year_body)
        self.assertNotIn('href="/years/2007"', filtered_years_body)
        self.assertIn(">3 måneder, 4 bilder</div>", filtered_years_body)
        self.assertIn('href="/years/2005" title="Neste år" data-key-nav="next-year">r ▶</a>', filtered_years_body)
        self.assertIn('href="/month/2005-03" title="Neste måned" data-key-nav="next-month">ed ▶</a>', filtered_years_body)
        self.assertIn('<span class="nav-button disabled">◀ Å</span>', filtered_years_body)
        self.assertIn('<span class="nav-button disabled">◀ Mån</span>', filtered_years_body)
        self.assertIn('href="/years/2005" title="Forrige år" data-key-nav="previous-year">◀ Å</a>', filtered_year_body)
        self.assertIn('<span class="nav-button disabled">r ▶</span>', filtered_year_body)
        self.assertIn('href="/month/2005-05" title="Forrige måned" data-key-nav="previous-month">◀ Mån</a>', filtered_year_body)
        self.assertIn('<span class="nav-button disabled">ed ▶</span>', filtered_year_body)
        self.assertNotIn('href="/month/2007-04"', filtered_year_body)
        self.assertEqual([card["year"] for card in year_cards], ["2005"])
        self.assertEqual([card["year"] for card in optimized_year_cards], ["2005", "2007"])
        self.assertEqual(month_items.call_count, 0)
        self.assertTrue(all(call.kwargs.get("conn") is None for call in all_where.call_args_list))
        self.assertEqual([card["month_key"] for card in month_cards], ["2005-03", "2005-04", "2005-05"])

    def test_run_server_year_route_rejects_invalid_year(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        response: dict[str, object] = {}

        def fake_respond_text(content: str, *, status: HTTPStatus) -> None:
            response["content"] = content
            response["status"] = status

        handler.respond_text = fake_respond_text  # type: ignore[method-assign]

        self.assertFalse(valid_year_key("2005-04"))
        BildebankRequestHandler.respond_year(handler, "2005-04")  # type: ignore[arg-type]

        self.assertEqual(response["content"], "Ugyldig år.")
        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertEqual(parse_source_path("bjerkvik/year/2017"), ("bjerkvik", "year", "2017"))

    def test_run_server_month_items_use_taken_date_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                for name, taken_date in (
                    ("z.jpg", "2024-01-01"),
                    ("a.jpg", "2024-01-02"),
                    ("m.jpg", "2024-01-03"),
                ):
                    relative_path = f"2024/01/{name}"
                    conn.execute(
                        """
                        INSERT INTO files(
                            target_path, target_path_key, original_filename, stored_filename,
                            sha256, size_bytes, taken_date, date_source, name_conflict
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, 'filename', 0)
                        """,
                        (relative_path, relative_path, name, name, uuid.uuid4().hex, 1, taken_date),
                    )
                conn.commit()
            finally:
                conn.close()

            items = browser_month_items(target, "2024-01")
            middle = items[1]
            previous_item, next_item = adjacent_browser_items(target, middle)

        self.assertEqual([item["stored_filename"] for item in items], ["z.jpg", "a.jpg", "m.jpg"])
        self.assertEqual(middle["stored_filename"], "a.jpg")
        self.assertIsNotNone(previous_item)
        self.assertIsNotNone(next_item)
        self.assertEqual(previous_item["stored_filename"], "z.jpg")
        self.assertEqual(next_item["stored_filename"], "m.jpg")

    def test_run_server_item_page_has_manual_date_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))

        self.assertIn("📅", body)
        self.assertIn("Sett manuell dato", body)
        self.assertIn('data-open-manual-date', body)
        self.assertIn('data-manual-date-item="1"', body)
        self.assertIn('id="manualDateOverlay"', body)
        self.assertIn('value="uncertain"', body)
        self.assertIn('value="between"', body)
        self.assertIn("/api/item-manual-date", SERVER_JS)
        self.assertIn("/api/item-manual-date-clear", SERVER_JS)

    def test_run_server_manual_date_endpoint_sets_and_clears_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            data = json.dumps(
                {
                    "file_id": 1,
                    "mode": "uncertain",
                    "date": "2004-07-15",
                    "uncertainty": "1m",
                    "note": "Kamera hadde feil dato",
                }
            ).encode("utf-8")
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_manual_date_item(handler)  # type: ignore[arg-type]
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))
            info_body = image_info_content_html(target, item)
            original_month_after_set = [item["stored_filename"] for item in browser_month_items(target, "2026-01")]
            manual_month_after_set = [item["stored_filename"] for item in browser_month_items(target, "2004-07")]

            clear_data = json.dumps({"file_id": 1}).encode("utf-8")
            clear_response: dict[str, object] = {}

            class ClearHandler:
                headers = {
                    "Content-Length": str(len(clear_data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(clear_data)
                server = SimpleNamespace(target=target)

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    clear_response["content"] = content
                    clear_response["status"] = status

            clear_handler = ClearHandler()
            BildebankRequestHandler.respond_clear_manual_date_item(clear_handler)  # type: ignore[arg-type]
            manual_month_after_clear = [item["stored_filename"] for item in browser_month_items(target, "2004-07")]
            original_month_after_clear = [item["stored_filename"] for item in browser_month_items(target, "2026-01")]

        self.assertEqual(response["status"], HTTPStatus.OK)
        self.assertEqual(response["content"], {"ok": True, "file_id": 1, "manual_date_from": "2004-06-15", "manual_date_to": "2004-08-15"})
        self.assertEqual(original_month_after_set, [])
        self.assertEqual(manual_month_after_set, ["IMG_20260102.jpg"])
        self.assertIn("📅", body)
        self.assertIn("Endre manuell dato", body)
        self.assertIn('data-manual-date-from="2004-06-15"', body)
        self.assertIn('data-manual-date-to="2004-08-15"', body)
        self.assertIn('data-manual-date-note="Kamera hadde feil dato"', body)
        self.assertIn("ca. 2004-07-15", info_body)
        self.assertIn("Kamera hadde feil dato", info_body)
        self.assertEqual(clear_response["status"], HTTPStatus.OK)
        self.assertEqual(clear_response["content"], {"ok": True, "file_id": 1})
        self.assertEqual(manual_month_after_clear, [])
        self.assertEqual(original_month_after_clear, ["IMG_20260102.jpg"])

    def test_run_server_manual_date_endpoint_rejects_invalid_input_without_changing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            data = json.dumps(
                {
                    "file_id": 1,
                    "mode": "between",
                    "date_from": "2004-08-31",
                    "date_to": "2004-06-01",
                    "note": "Skal ikke lagres",
                }
            ).encode("utf-8")
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_manual_date_item(handler)  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                row = conn.execute("SELECT manual_date_from, manual_date_to, manual_date_note FROM files WHERE id = 1").fetchone()
            finally:
                conn.close()

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("Fra-dato kan ikke være etter til-dato", str(response["content"]))
        self.assertIsNone(row["manual_date_from"])
        self.assertIsNone(row["manual_date_to"])
        self.assertIsNone(row["manual_date_note"])

    def test_run_server_manual_date_endpoints_report_target_lock_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            set_data = json.dumps({"file_id": 1, "mode": "exact", "date": "2004-07-15"}).encode("utf-8")
            clear_data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                def __init__(self, data: bytes) -> None:
                    self.headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/json",
                    }
                    self.rfile = BytesIO(data)
                    self.server = SimpleNamespace(target=target)
                    self.body: dict[str, object] | None = None
                    self.status: HTTPStatus | None = None

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

            set_handler = FakeHandler(set_data)
            BildebankRequestHandler.respond_manual_date_item(set_handler)  # type: ignore[arg-type]
            clear_handler = FakeHandler(clear_data)
            BildebankRequestHandler.respond_clear_manual_date_item(clear_handler)  # type: ignore[arg-type]

        self.assertEqual(set_handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(set_handler.body["error"]))
        self.assertEqual(clear_handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(clear_handler.body["error"]))

    def test_run_server_manual_date_endpoint_rejects_deleted_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                conn.execute("UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = 1")
                conn.commit()
            finally:
                conn.close()
            data = json.dumps({"file_id": 1, "mode": "exact", "date": "2004-07-15"}).encode("utf-8")
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_manual_date_item(handler)  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("Filen er markert som slettet", str(response["content"]))

    def test_run_server_hotkey_action_is_rejected_when_hints_are_hidden(self) -> None:
        data = json.dumps({"file_id": 1, "key": "5"}).encode("utf-8")
        response: dict[str, object] = {}

        class FakeHandler:
            headers = {
                "Content-Length": str(len(data)),
                "Content-Type": "application/json",
            }
            rfile = BytesIO(data)
            server = SimpleNamespace(config=AppConfig(browser=BrowserConfig(hotkey_hints_enabled=False)))

            def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                response["content"] = content
                response["status"] = status

        BildebankRequestHandler.respond_hotkey_action(FakeHandler())  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.FORBIDDEN)
        self.assertEqual(response["content"], {"ok": False, "error": "Hurtigtaster er slått av."})

    def test_run_server_hotkey_action_sets_manual_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            data = json.dumps({"file_id": 1, "key": "5"}).encode("utf-8")
            response: dict[str, object] = {}
            hotkeys = {
                "5": BrowserHotkeyConfig(
                    action="manual_date",
                    mode="between",
                    date_from="2004-06-01",
                    date_to="2004-08-31",
                    note="Sommer 2004",
                )
            }

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(browser=BrowserConfig(hotkey_hints_enabled=True, hotkeys=hotkeys)),
                )

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_hotkey_action(handler)  # type: ignore[arg-type]
            item = browser_item_by_id(target, 1)

        self.assertEqual(response["status"], HTTPStatus.OK)
        self.assertEqual(response["content"]["action"], "manual_date")
        self.assertEqual(response["content"]["manual_date_from"], "2004-06-01")
        self.assertEqual(response["content"]["manual_date_to"], "2004-08-31")
        self.assertEqual(item["manual_date_note"], "Sommer 2004")

    def test_run_server_hotkey_action_adds_person_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            create_person(target, "Kari")
            data = json.dumps({"file_id": 1, "key": "3"}).encode("utf-8")
            response: dict[str, object] = {}
            hotkeys = {"3": BrowserHotkeyConfig(action="person", person_name="Kari")}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    config=AppConfig(browser=BrowserConfig(hotkey_hints_enabled=True, hotkeys=hotkeys)),
                )

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_hotkey_action(handler)  # type: ignore[arg-type]
            face_conn = connect_face_db(target)
            try:
                manual_link_count = face_conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0]
            finally:
                face_conn.close()

        self.assertEqual(response["status"], HTTPStatus.OK)
        self.assertEqual(response["content"]["action"], "person")
        self.assertEqual(response["content"]["person_name"], "Kari")
        self.assertEqual(response["content"]["confirmed"], True)
        self.assertEqual(manual_link_count, 1)

    def test_run_server_hotkey_action_sets_tag_on_file_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            data = json.dumps({"file_id": 1, "key": "2"}).encode("utf-8")
            responses: list[dict[str, object]] = []
            hotkeys = {"2": BrowserHotkeyConfig(action="tag", tag_name="  Familie  ")}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(browser=BrowserConfig(hotkey_hints_enabled=True, hotkeys=hotkeys)),
                )

                def __init__(self) -> None:
                    self.rfile = BytesIO(data)

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    responses.append({"content": content, "status": status})

            BildebankRequestHandler.respond_hotkey_action(FakeHandler())  # type: ignore[arg-type]
            BildebankRequestHandler.respond_hotkey_action(FakeHandler())  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                tag_row = conn.execute("SELECT id, name FROM tags WHERE name_key = ?", ("familie",)).fetchone()
                link_count = conn.execute("SELECT COUNT(*) AS count FROM file_tags WHERE file_id = 1").fetchone()["count"]
            finally:
                conn.close()

        self.assertEqual(responses[0]["status"], HTTPStatus.OK)
        self.assertEqual(responses[0]["content"]["action"], "tag")
        self.assertEqual(responses[0]["content"]["tag_name"], "Familie")
        self.assertEqual(responses[0]["content"]["tagged"], True)
        self.assertEqual(responses[1]["status"], HTTPStatus.OK)
        self.assertEqual(responses[1]["content"]["tag_name"], "Familie")
        self.assertEqual(tag_row["name"], "Familie")
        self.assertEqual(link_count, 1)

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

    def test_run_server_item_page_has_image_info_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            cells = h3_cells_for_point(59.91273, 10.74609)
            conn = db.connect(target)
            try:
                db.update_file_gps(
                    conn,
                    file_id=1,
                    gps_lat=59.91273,
                    gps_lon=10.74609,
                    gps_alt=None,
                    h3_cells=cells,
                    gps_source="test",
                    gps_error=None,
                )
                conn.commit()
            finally:
                conn.close()
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))
            info_body = image_info_content_html(target, item)

        self.assertIn('data-open-info', body)
        self.assertIn('data-info-item="1"', body)
        self.assertNotIn("<button class=\"nav-button\" type=\"button\" data-open-info", body)
        self.assertIn('href="/years">År</a>', body)
        self.assertIn('href="/years/2024">2024</a>', body)
        self.assertIn('href="/month/2024-01">Januar</a>', body)
        self.assertIn('aria-label="Åpne bildeinfo for IMG_20240102.png"', body)
        self.assertIn('id="infoOverlay"', body)
        self.assertIn("/api/item-info?file_id=", SERVER_JS)
        self.assertIn("querySelectorAll(\"[data-open-info]\")", SERVER_JS)
        self.assertNotIn("<dt>Filnavn</dt>", body)
        self.assertIn("Filnavn", info_body)
        self.assertIn("IMG_20240102.png", info_body)
        self.assertIn("<dt>Dato</dt>", info_body)
        self.assertIn("2024-01-02 (fra filnavn)", info_body)
        self.assertIn("Filstørrelse", info_body)
        self.assertIn("Oppløsning", info_body)
        self.assertIn("100 x 80", info_body)
        self.assertIn("Kamera", info_body)
        self.assertIn("Kilder", info_body)
        self.assertIn("<dt>Kart</dt>", info_body)
        self.assertIn('href="https://www.google.com/maps/search/?api=1&amp;query=59.9127300,10.7460900"', info_body)
        self.assertIn('target="_blank"', info_body)
        self.assertIn('rel="noopener"', info_body)
        self.assertIn("<dt>Steder</dt>", info_body)
        self.assertIn(f'href="/geo/area/{cells["h3_res5"]}"', info_body)
        self.assertIn(f'href="/geo/area/{cells["h3_res9"]}"', info_body)
        self.assertIn(f'href="/geo/area/{cells["h3_res11"]}"', info_body)
        self.assertIn(f"H3-7: {cells['h3_res7']}", info_body)
        self.assertIn(source.name, info_body)
        self.assertIn("closeInfoOverlay", SERVER_JS)

    def test_run_server_item_info_api_returns_lazy_panel_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            response: dict[str, object] = {}
            handler = object.__new__(BildebankRequestHandler)
            handler.server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))

            def fake_respond_json(content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                response["content"] = content
                response["status"] = status

            handler.respond_json = fake_respond_json  # type: ignore[method-assign]
            handler.respond_item_info("file_id=1")

        self.assertEqual(response["status"], HTTPStatus.OK)
        content = response["content"]
        assert isinstance(content, dict)
        self.assertIs(content["ok"], True)
        self.assertIn("<dt>Filnavn</dt>", str(content["html"]))
        self.assertIn("IMG_20240102.png", str(content["html"]))

    def test_run_server_item_info_api_rejects_unknown_and_deleted_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            handler = object.__new__(BildebankRequestHandler)
            handler.server = SimpleNamespace(target=target)
            responses: list[tuple[dict[str, object], HTTPStatus]] = []

            def fake_respond_json(content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                responses.append((content, status))

            handler.respond_json = fake_respond_json  # type: ignore[method-assign]
            handler.respond_item_info("file_id=999")

            with db.connect(target) as conn:
                conn.execute("UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = 1")
            handler.respond_item_info("file_id=1")

        self.assertEqual(len(responses), 2)
        for content, status in responses:
            self.assertEqual(status, HTTPStatus.NOT_FOUND)
            self.assertIs(content["ok"], False)
            self.assertEqual(content["error"], "Filen finnes ikke.")

    def test_run_server_item_page_omits_geo_info_without_h3_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = image_info_content_html(target, item)

        self.assertNotIn("<dt>Steder</dt>", body)
        self.assertNotIn("<dt>Kart</dt>", body)
        self.assertNotIn("google.com/maps", body)
        self.assertNotIn("/geo/area/", body)

    def test_run_server_item_page_has_delete_button(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))

        self.assertIn("Slett", body)
        self.assertIn('data-delete-item="1"', body)
        self.assertIn('data-delete-path="2024/01/IMG_20240102.png"', body)
        self.assertIn("/api/item-delete", SERVER_JS)
        self.assertIn("Flytte til deleted/?", SERVER_JS)

    def test_run_server_item_page_omits_legacy_manual_location_button(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))

        nav_start = body.index('<nav class="controls"')
        tag_rail_start = body.index('<aside class="tag-rail"')
        nav_body = body[nav_start:body.index("</nav>", nav_start)]
        tag_rail_body = body[tag_rail_start:body.index("</aside>", tag_rail_start)]
        self.assertNotIn("Sett valgt H3-celle", tag_rail_body)
        self.assertNotIn("Sett sted", body)
        self.assertNotIn("Sett valgt H3-celle", nav_body)
        self.assertNotIn('data-manual-location-item="1"', body)
        self.assertNotIn("data-manual-location-cell", body)
        self.assertNotIn('fetch("/api/item-manual-location"', SERVER_JS)
        self.assertIn("/api/item-manual-location-remove", SERVER_JS)
        self.assertIn("/api/item-hotkey-action", SERVER_JS)
        self.assertIn('["1", "2", "3", "4", "5"].includes(event.key)', SERVER_JS)
        self.assertNotIn('event.key.toLowerCase() === "g"', SERVER_JS)
        self.assertNotIn("setManualLocation(button)", SERVER_JS)
        self.assertNotIn("Sette sted fra aktiv H3-celle?", SERVER_JS)
        self.assertIn('data-browser-item-id="1"', body)

    def test_run_server_filter_item_page_has_source_url_for_hotkeys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            normal_body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))
            filter_source = text_filter_browser_source("missing:gps", target)
            filter_item = source_item_by_id(target, filter_source, 1)
            self.assertIsNotNone(filter_item)
            filter_body = source_item_page_html(
                target,
                filter_source,
                filter_item,
                *adjacent_source_items(target, filter_source, filter_item),
                source_month_navigation(target, filter_source, filter_item),
            )

        self.assertIn('data-browser-item-id="1"', filter_body)
        self.assertIn('data-browser-source-url="/filter/missing%3Agps"', filter_body)
        self.assertNotIn("data-browser-source-url", normal_body)
        self.assertIn("browserSourceUrl", SERVER_JS)
        self.assertIn("payload.redirect_url", SERVER_JS)
        self.assertIn("const itemRoot = button.closest(\"[data-browser-item-id]\");", SERVER_JS)
        self.assertIn("requestBody.source_url = itemRoot.dataset.browserSourceUrl", SERVER_JS)

    def test_run_server_item_page_can_show_hotkey_hints_in_tag_rail(self) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                db.set_geo_place_name(conn, h3_cell, "Brevik")
                conn.commit()
            finally:
                conn.close()
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            hotkeys = {
                "1": BrowserHotkeyConfig(action="h3", h3_cell=h3_cell),
                "3": BrowserHotkeyConfig(action="person", person_name="Viljar"),
                "4": BrowserHotkeyConfig(action="tag", tag_name="Familie"),
                "5": BrowserHotkeyConfig(action="manual_date", mode="uncertain", date="1948-12-30", uncertainty="1w"),
            }
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                face_enabled=False,
                hotkey_hints_enabled=True,
                hotkeys=hotkeys,
            )
            hidden_body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                face_enabled=False,
                hotkey_hints_enabled=False,
                hotkeys=hotkeys,
            )

        tag_rail_start = body.index('<aside class="tag-rail"')
        tag_rail_body = body[tag_rail_start:body.index("</aside>", tag_rail_start)]
        self.assertIn('class="hotkey-hints"', tag_rail_body)
        self.assertIn('<div class="hotkey-hints-heading">Hurtigtaster aktivert:</div>', tag_rail_body)
        self.assertIn("<span>1:</span> Sett H3 til Brevik", tag_rail_body)
        self.assertIn("<span>3:</span> Legg til Viljar", tag_rail_body)
        self.assertIn("<span>4:</span> Sett tagg Familie", tag_rail_body)
        self.assertIn("<span>5:</span> Sett dato til 30.12.48 ±1w", tag_rail_body)
        self.assertNotIn("date-status-badge", tag_rail_body)
        self.assertLess(tag_rail_body.index("location-status-badge"), tag_rail_body.index('class="hotkey-hints"'))
        self.assertTrue(tag_rail_body.strip().endswith("</section>"))
        self.assertIn('data-browser-hotkeys-enabled="true"', body)
        self.assertNotIn("data-browser-hotkeys-enabled", hidden_body)
        self.assertIn('itemRoot?.dataset.browserHotkeysEnabled === "true"', SERVER_JS)
        self.assertIn('itemRoot?.dataset.browserHotkeysEnabled !== "true"', SERVER_JS)
        self.assertNotIn('class="hotkey-hints"', hidden_body)

    def test_run_server_hotkey_action_sets_h3_location(self) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            data = json.dumps({"file_id": 1, "key": "1"}).encode("utf-8")
            hotkeys = {"1": BrowserHotkeyConfig(action="h3", h3_cell=h3_cell)}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    config=AppConfig(browser=BrowserConfig(hotkey_hints_enabled=True, hotkeys=hotkeys)),
                )
                body: dict[str, object] | None = None
                timings: list[str] = []

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content

                def record_server_timing(self, name: str, start: float) -> None:
                    self.timings.append(name)

            handler = FakeHandler()
            BildebankRequestHandler.respond_hotkey_action(handler)  # type: ignore[arg-type]

            conn = db.connect(target)
            try:
                db.set_file_tag(conn, file_id=1, tag_name=db.SYSTEM_TAG_OUT_OF_FOCUS, tagged=True)
                conn.commit()
            finally:
                conn.close()
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))
            info_body = image_info_content_html(target, item)
            conn = db.connect(target)
            try:
                row = conn.execute("SELECT gps_lat, gps_lon, gps_alt, gps_source, gps_error, h3_res0, h3_res3, h3_res4, h3_res11 FROM files WHERE id = 1").fetchone()
            finally:
                conn.close()

        self.assertEqual(handler.body, {"ok": True, "key": "1", "action": "h3", "file_id": 1, "gps_source": "manual-h3"})
        self.assertEqual(
            handler.timings,
            [
                "hotkey_read_payload",
                "hotkey_validate",
                "hotkey_filter_parse",
                "hotkey_apply",
                "hotkey_post_apply",
            ],
        )
        self.assertEqual(row["gps_source"], "manual-h3")
        self.assertIsNone(row["gps_alt"])
        self.assertIsNone(row["gps_error"])
        self.assertIsNone(row["gps_lat"])
        self.assertIsNone(row["gps_lon"])
        self.assertTrue(row["h3_res0"])
        self.assertEqual(row["h3_res3"], h3_cell)
        self.assertIsNone(row["h3_res4"])
        self.assertIsNone(row["h3_res11"])
        self.assertIn('<aside class="tag-rail"', body)
        self.assertIn(f'href="https://h3geo.org/#hex={h3_cell}" target="_blank" title="Vis plasseringen på kartet på https://h3geo.org/" rel="noopener">Manuell H3</a>', body)
        self.assertNotIn("Fjern manuelt sted", body)
        self.assertIn('class="inline-link danger-inline-link"', body)
        self.assertIn('Manuell H3</a><span class="manual-location-remove">(<button class="inline-link danger-inline-link"', body)
        self.assertIn(">fjern</button>)</span>", body)
        self.assertIn('data-remove-manual-location-item="1"', body)
        self.assertNotIn('data-manual-location-item="1"', body)
        self.assertNotIn("Sett valgt H3-celle", body)
        self.assertNotIn("<dt>Kart</dt>", info_body)
        self.assertIn("Manuell H3", info_body)
        self.assertIn(f'href="https://h3geo.org/#hex={h3_cell}"', info_body)
        self.assertLess(info_body.index("<dt>Tagger</dt>"), info_body.index("Manuell H3"))
        self.assertIn("<dt>GPS-kilde</dt>", info_body)
        self.assertIn("satt manuelt", info_body)

    def test_run_server_hotkey_action_redirects_to_next_filter_item_when_current_no_longer_matches(self) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240101.png").write_bytes(minimal_png(100, 80))
            (source / "IMG_20240102.png").write_bytes(minimal_png(101, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            filter_source = text_filter_browser_source("missing:gps", target)
            data = json.dumps({"file_id": 1, "key": "1", "source_url": filter_source.root_url}).encode("utf-8")
            hotkeys = {"1": BrowserHotkeyConfig(action="h3", h3_cell=h3_cell)}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    config=AppConfig(browser=BrowserConfig(hotkey_hints_enabled=True, hotkeys=hotkeys)),
                )
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_hotkey_action(handler)  # type: ignore[arg-type]
            row = browser_item_by_id(target, 1)

        self.assertEqual(handler.body["action"], "h3")
        self.assertEqual(handler.body["gps_source"], "manual-h3")
        self.assertEqual(handler.body["redirect_url"], "/filter/missing%3Agps/item/2")
        self.assertEqual(row["gps_source"], "manual-h3")

    def test_run_server_manual_h3_hotkey_reports_target_lock_conflict(self) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240101.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            (target / LOCK_FILENAME).write_text("command=geo-scan\n", encoding="utf-8")
            data = json.dumps({"file_id": 1, "key": "1"}).encode("utf-8")
            hotkeys = {"1": BrowserHotkeyConfig(action="h3", h3_cell=h3_cell)}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    config=AppConfig(browser=BrowserConfig(hotkey_hints_enabled=True, hotkeys=hotkeys)),
                )
                body: dict[str, object] | None = None
                status: HTTPStatus | None = None

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_hotkey_action(handler)  # type: ignore[arg-type]
            row = browser_item_by_id(target, 1)

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))
        self.assertIsNone(row["gps_source"])

    def test_run_server_hotkey_action_redirects_to_previous_filter_item_from_last_match(self) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240101.png").write_bytes(minimal_png(100, 80))
            (source / "IMG_20240102.png").write_bytes(minimal_png(101, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            filter_source = text_filter_browser_source("missing:gps", target)
            data = json.dumps({"file_id": 2, "key": "1", "source_url": filter_source.root_url}).encode("utf-8")
            hotkeys = {"1": BrowserHotkeyConfig(action="h3", h3_cell=h3_cell)}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    config=AppConfig(browser=BrowserConfig(hotkey_hints_enabled=True, hotkeys=hotkeys)),
                )
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_hotkey_action(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.body["redirect_url"], "/filter/missing%3Agps/item/1")

    def test_run_server_hotkey_action_redirects_to_filter_root_when_filter_becomes_empty(self) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240101.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            filter_source = text_filter_browser_source("missing:gps", target)
            data = json.dumps({"file_id": 1, "key": "1", "source_url": filter_source.root_url}).encode("utf-8")
            hotkeys = {"1": BrowserHotkeyConfig(action="h3", h3_cell=h3_cell)}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    config=AppConfig(browser=BrowserConfig(hotkey_hints_enabled=True, hotkeys=hotkeys)),
                )
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_hotkey_action(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.body["redirect_url"], "/filter/missing%3Agps")

    def test_run_server_item_manual_location_remove_endpoint_clears_manual_h3(self) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                db.set_file_manual_h3_location(conn, file_id=1, h3_cells={"h3_res0": h3.cell_to_parent(h3_cell, 0), "h3_res3": h3_cell})
                conn.commit()
            finally:
                conn.close()
            data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_remove_manual_location_item(handler)  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                row = conn.execute("SELECT gps_source, gps_scanned_at, h3_res0, h3_res3 FROM files WHERE id = 1").fetchone()
            finally:
                conn.close()

        self.assertEqual(handler.body, {"ok": True, "file_id": 1, "gps_source": None})
        self.assertIsNone(row["gps_source"])
        self.assertIsNone(row["gps_scanned_at"])
        self.assertIsNone(row["h3_res0"])
        self.assertIsNone(row["h3_res3"])

    def test_run_server_manual_location_remove_reports_target_lock_conflict(self) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                db.set_file_manual_h3_location(
                    conn,
                    file_id=1,
                    h3_cells={"h3_res0": h3.cell_to_parent(h3_cell, 0), "h3_res3": h3_cell},
                )
                conn.commit()
            finally:
                conn.close()
            (target / LOCK_FILENAME).write_text("command=geo-scan\n", encoding="utf-8")
            data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
                body: dict[str, object] | None = None
                status: HTTPStatus | None = None

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_remove_manual_location_item(handler)  # type: ignore[arg-type]
            row = browser_item_by_id(target, 1)

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))
        self.assertEqual(row["gps_source"], "manual-h3")

    def test_run_server_item_page_lists_defined_tags_before_geo_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                db.ensure_tag(conn, "Familie")
                conn.commit()
            finally:
                conn.close()
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))

        self.assertIn('class="tag-rail"', body)
        self.assertIn('data-tag-toggle="1"', body)
        self.assertIn('data-tag-name="Ute av fokus"', body)
        self.assertIn('data-tag-name="Familie"', body)
        self.assertIn('aria-pressed="false"', body)
        self.assertLess(body.index('data-tag-name="Ute av fokus"'), body.index('data-tag-name="Familie"'))
        self.assertNotIn('class="date-status-badge"', body)
        self.assertLess(body.index('data-tag-name="Familie"'), body.index('class="location-status-badge"'))
        self.assertIn("/api/item-tag", SERVER_JS)
        tag_handler = SERVER_JS[SERVER_JS.index('document.querySelectorAll("[data-tag-toggle]') : SERVER_JS.index("function updateHotkeyForm")]
        self.assertIn('button.classList.toggle("active", Boolean(payload.tagged));', tag_handler)
        self.assertNotIn("window.location.reload();", tag_handler)
        self.assertIn("stage-shell", SERVER_CSS)
        self.assertIn("tag-rail", SERVER_CSS)
        self.assertIn(".tag-toggle::before", SERVER_CSS)
        self.assertIn('.tag-toggle.active::before', SERVER_CSS)

    def test_run_server_item_tag_endpoint_sets_system_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            data = json.dumps({"file_id": 1, "tag_name": db.SYSTEM_TAG_OUT_OF_FOCUS, "tagged": True}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None
                timings: list[str] = []

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content

                def record_server_timing(self, name: str, start: float) -> None:
                    self.timings.append(name)

            handler = FakeHandler()
            BildebankRequestHandler.respond_tag_item(handler)  # type: ignore[arg-type]
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))
            info_body = image_info_content_html(target, item)

        self.assertEqual({"ok": True, "file_id": 1, "tag_name": db.SYSTEM_TAG_OUT_OF_FOCUS, "tagged": True}, handler.body)
        self.assertEqual(handler.timings, ["tag_read_payload", "tag_validate", "tag_apply"])
        self.assertIn('class="tag-toggle active"', body)
        self.assertIn('aria-pressed="true"', body)
        self.assertIn("Ute av fokus", info_body)
        self.assertIn("(system)", info_body)

    def test_run_server_item_tag_endpoint_sets_user_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            data = json.dumps({"file_id": 1, "tag_name": "Familie", "tagged": True}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None
                status = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_tag_item(handler)  # type: ignore[arg-type]
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))
            info_body = image_info_content_html(target, item)

        self.assertIsNone(handler.status)
        self.assertEqual({"ok": True, "file_id": 1, "tag_name": "Familie", "tagged": True}, handler.body)
        self.assertIn('data-tag-name="Familie" aria-pressed="true"', body)
        self.assertIn("Familie", info_body)

    def test_run_server_item_tag_returns_conflict_when_target_is_locked(self) -> None:
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
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=remove\npid=123\n", encoding="utf-8")
            data = json.dumps({"file_id": 1, "tag_name": "Familie", "tagged": True}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
                body: dict[str, object] | None = None
                status = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_tag_item(handler)  # type: ignore[arg-type]

            self.assertEqual(HTTPStatus.CONFLICT, handler.status)
            self.assertEqual(False, handler.body["ok"])
            self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))
            self.assertTrue(lock_path.exists())
            conn = db.connect(target)
            try:
                self.assertIsNone(conn.execute("SELECT id FROM tags WHERE name_key = 'familie'").fetchone())
            finally:
                conn.close()

    def test_run_server_delete_button_moves_file_to_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            original = target / "2024" / "01" / "IMG_20240102.png"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.png"
            original_bytes = original.read_bytes()
            data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_delete_item(handler)  # type: ignore[arg-type]
            conn = sqlite3.connect(target / DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT target_path, deleted_at, deleted_original_target_path FROM files WHERE id = 1").fetchone()
            finally:
                conn.close()

            self.assertEqual({"ok": True, "file_id": 1, "deleted_path": "deleted/2024/01/IMG_20240102.png"}, handler.body)
            self.assertFalse(original.exists())
            self.assertTrue(deleted.exists())
            self.assertEqual(deleted.read_bytes(), original_bytes)
            self.assertEqual(row["target_path"], "deleted/2024/01/IMG_20240102.png")
            self.assertEqual(row["deleted_original_target_path"], "2024/01/IMG_20240102.png")
            self.assertIsNotNone(row["deleted_at"])
            self.assertIsNone(browser_item_by_id(target, 1))

    def test_run_server_delete_returns_conflict_when_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))
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
            original = target / "2024" / "01" / "IMG_20240102.png"
            (target / LOCK_FILENAME).write_text(
                "command=import\npid=123\n", encoding="utf-8"
            )
            data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
                body: dict[str, object] | None = None
                status = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_delete_item(handler)  # type: ignore[arg-type]

            self.assertEqual(HTTPStatus.CONFLICT, handler.status)
            self.assertEqual(False, handler.body["ok"])
            self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))
            self.assertTrue(original.exists())

    def test_app_links_to_removed_files_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.png"]), 0)

            app_body = app_status_page_html(target)
            removed_body = removed_files_page_html(target)

        self.assertIn('href="/settings/removed"', app_body)
        self.assertIn("Slettede bilder", removed_body)
        self.assertIn('href="/file/deleted/2024/01/IMG_20240102.png"', removed_body)
        self.assertIn("2024/01/IMG_20240102.png", removed_body)
        self.assertIn('data-undelete-item="1"', removed_body)
        self.assertIn("/api/item-undelete", SERVER_JS)

    def test_run_server_undelete_restores_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.png"]), 0)
            restored = undelete_file_from_browser(target, 1)

            conn = sqlite3.connect(target / DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT target_path, deleted_at, deleted_original_target_path FROM files WHERE id = 1").fetchone()
            finally:
                conn.close()

            self.assertEqual(restored.as_posix(), "2024/01/IMG_20240102.png")
            self.assertEqual(row["target_path"], "2024/01/IMG_20240102.png")
            self.assertIsNone(row["deleted_at"])
            self.assertIsNone(row["deleted_original_target_path"])
            self.assertIsNotNone(browser_item_by_id(target, 1))

    def test_run_server_undelete_endpoint_restores_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.png"]), 0)
            data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_undelete_item(handler)  # type: ignore[arg-type]

            self.assertEqual({"ok": True, "file_id": 1, "restored_path": "2024/01/IMG_20240102.png"}, handler.body)
            self.assertTrue((target / "2024" / "01" / "IMG_20240102.png").exists())
            self.assertFalse((target / "deleted" / "2024" / "01" / "IMG_20240102.png").exists())

    def test_run_server_undelete_returns_conflict_when_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))
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
            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "remove",
                        "2024/01/IMG_20240102.png",
                    ]
                ),
                0,
            )
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.png"
            (target / LOCK_FILENAME).write_text(
                "command=import\npid=123\n", encoding="utf-8"
            )
            data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
                body: dict[str, object] | None = None
                status = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_undelete_item(handler)  # type: ignore[arg-type]

            self.assertEqual(HTTPStatus.CONFLICT, handler.status)
            self.assertEqual(False, handler.body["ok"])
            self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))
            self.assertTrue(deleted.exists())

    def test_run_server_item_page_can_rotate_image_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            image_path = target / "2024" / "01" / "IMG_20240102.png"
            original_bytes = image_path.read_bytes()
            data = json.dumps({"file_id": 1, "direction": "right"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_rotate_item(handler)  # type: ignore[arg-type]
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))
            month_body = month_page_html(target, "2024-01", browser_month_items(target, "2024-01"))
            final_bytes = image_path.read_bytes()

        self.assertEqual({"ok": True, "file_id": 1, "rotation": 90}, handler.body)
        self.assertEqual(original_bytes, final_bytes)
        self.assertIn("↺", body)
        self.assertIn("↻", body)
        self.assertIn('data-rotate-direction="left"', body)
        self.assertIn('data-rotate-direction="right"', body)
        self.assertIn('class="media-link quarter-turn"', body)
        self.assertIn("transform: rotate(90deg)", body)
        self.assertIn("--quarter-turn-width:", body)
        self.assertIn("transform: rotate(90deg)", month_body)
        self.assertIn(".server-browser {\n      height: 100vh;\n      height: 100dvh;", SERVER_CSS)
        self.assertIn(".stage img, .stage video", SERVER_CSS)
        self.assertIn("touch-action: pan-y;", SERVER_CSS)
        self.assertIn("-webkit-user-drag: none;", SERVER_CSS)
        self.assertIn("width: auto;", SERVER_CSS)
        self.assertIn("height: auto;", SERVER_CSS)
        self.assertIn("object-fit: contain;", SERVER_CSS)
        self.assertIn("max-height: 100%;", SERVER_CSS)
        self.assertIn(".stage .media-link.quarter-turn", SERVER_CSS)
        self.assertIn("overflow: visible;", SERVER_CSS)
        self.assertIn('.stage img[data-view-rotation="90"]', SERVER_CSS)
        self.assertIn('.stage img[data-view-rotation="270"]', SERVER_CSS)
        self.assertIn("max-width: min(calc(100vh - 10rem), var(--quarter-turn-width, 100%));", SERVER_CSS)
        self.assertIn("max-width: min(calc(100dvh - 10rem), var(--quarter-turn-width, 100%));", SERVER_CSS)
        self.assertIn("max-height: none;", SERVER_CSS)
        self.assertIn(".person-media {\n      position: relative;\n      display: grid;", SERVER_CSS)
        self.assertIn(".person-media > a", SERVER_CSS)
        self.assertIn("width: 100%;\n      height: 100%;\n      max-width: 100%;\n      max-height: 100%;", SERVER_CSS)
        self.assertIn(".person-media img", SERVER_CSS)
        self.assertIn("max-width: var(--quarter-turn-width, 100%);", SERVER_CSS)
        self.assertNotIn(".person-media {\n      position: relative;\n      display: inline-block;", SERVER_CSS)
        self.assertNotIn("max-width: min(100%, 92vw);", SERVER_CSS)
        self.assertNotIn("max-height: calc(100vh - 10rem);\n      transform-origin: center center;", SERVER_CSS)
        self.assertNotIn("max-height: calc(100vh - 10rem);\n      object-fit: contain;", SERVER_CSS)
        self.assertNotIn("padding: 14px;", SERVER_CSS)
        self.assertIn("function fitQuarterTurnMedia()", SERVER_JS)
        self.assertIn("const availableHeight = Math.max(stageRect.height, 1);", SERVER_JS)
        self.assertIn("const maxOriginalWidth = Math.max(Math.min(availableHeight, availableWidth * ratio), 1);", SERVER_JS)

    def test_run_server_rotate_reports_target_lock_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")
            data = json.dumps({"file_id": 1, "direction": "right"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
                body: dict[str, object] | None = None
                status: HTTPStatus | None = None

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_rotate_item(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))

    def test_run_server_rotate_image_view_wraps_left(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            data = json.dumps({"file_id": 1, "direction": "left"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_rotate_item(handler)  # type: ignore[arg-type]

        self.assertEqual({"ok": True, "file_id": 1, "rotation": 270}, handler.body)

    def test_run_server_rotate_redirects_when_current_item_leaves_filter(self) -> None:
        cases = (
            (3, 1, 90, "left", "/filter/is%3Arotated/item/2"),
            (3, 3, 270, "right", "/filter/is%3Arotated/item/2"),
            (1, 1, 90, "left", "/filter/is%3Arotated"),
        )
        for image_count, file_id, initial_rotation, direction, redirect_url in cases:
            with self.subTest(
                image_count=image_count,
                file_id=file_id,
                initial_rotation=initial_rotation,
                direction=direction,
            ), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "target"
                source = Path(tmp) / "source"
                source.mkdir()
                for day in range(1, image_count + 1):
                    (source / f"IMG_202401{day:02d}.png").write_bytes(minimal_png(100 + day, 80))

                self.assertEqual(run_cli(["create", str(target)]), 0)
                self.assertEqual(
                    run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                    0,
                )
                conn = db.connect(target)
                try:
                    conn.execute(
                        "UPDATE files SET view_rotation_degrees = 90 WHERE deleted_at IS NULL"
                    )
                    conn.execute(
                        "UPDATE files SET view_rotation_degrees = ? WHERE id = ?",
                        (initial_rotation, file_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
                filter_source = text_filter_browser_source("is:rotated", target)
                data = json.dumps(
                    {
                        "file_id": file_id,
                        "direction": direction,
                        "source_url": filter_source.root_url,
                    }
                ).encode("utf-8")

                class FakeHandler:
                    headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/json",
                    }
                    rfile = BytesIO(data)
                    server = SimpleNamespace(target=target)
                    body: dict[str, object] | None = None

                    def respond_json(self, content: dict[str, object], *, status=None) -> None:
                        self.body = content

                handler = FakeHandler()
                BildebankRequestHandler.respond_rotate_item(handler)  # type: ignore[arg-type]

                self.assertEqual(0, handler.body["rotation"])
                self.assertEqual(redirect_url, handler.body["redirect_url"])

    def test_run_server_rotate_does_not_redirect_while_item_still_matches_filter(self) -> None:
        cases = (
            (180, "left", 90),
            (90, "right", 180),
            (180, "right", 270),
        )
        for initial_rotation, direction, expected_rotation in cases:
            with self.subTest(initial_rotation=initial_rotation), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "target"
                source = Path(tmp) / "source"
                source.mkdir()
                (source / "IMG_20240101.png").write_bytes(minimal_png(100, 80))

                self.assertEqual(run_cli(["create", str(target)]), 0)
                self.assertEqual(
                    run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                    0,
                )
                conn = db.connect(target)
                try:
                    conn.execute(
                        "UPDATE files SET view_rotation_degrees = ? WHERE id = 1",
                        (initial_rotation,),
                    )
                    conn.commit()
                finally:
                    conn.close()
                filter_source = text_filter_browser_source("is:rotated", target)
                data = json.dumps(
                    {
                        "file_id": 1,
                        "direction": direction,
                        "source_url": filter_source.root_url,
                    }
                ).encode("utf-8")

                class FakeHandler:
                    headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/json",
                    }
                    rfile = BytesIO(data)
                    server = SimpleNamespace(target=target)
                    body: dict[str, object] | None = None

                    def respond_json(self, content: dict[str, object], *, status=None) -> None:
                        self.body = content

                handler = FakeHandler()
                BildebankRequestHandler.respond_rotate_item(handler)  # type: ignore[arg-type]

                self.assertEqual(expected_rotation, handler.body["rotation"])
                self.assertNotIn("redirect_url", handler.body)

    def test_run_server_rotate_image_view_rejects_invalid_direction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            data = json.dumps({"file_id": 1, "direction": "up"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None
                status = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_rotate_item(handler)  # type: ignore[arg-type]

        self.assertEqual({"ok": False, "error": "Ugyldig rotasjonsretning."}, handler.body)
        self.assertEqual(HTTPStatus.BAD_REQUEST, handler.status)

    def test_run_server_video_page_does_not_show_rotation_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2024, 1, 2)))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))

        self.assertNotIn("↻", body)
        self.assertNotIn("↺", body)

    def test_run_server_archive_image_page_links_file_without_image_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.nef").write_bytes(b"raw-photo")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))
            month_body = month_page_html(target, "2024-01", browser_month_items(target, "2024-01"))

        self.assertIn('<a class="file-card" href="/file/1" target="_blank">Fil<br>IMG_20240102.nef</a>', body)
        self.assertNotIn('<img src="/file/1"', body)
        self.assertNotIn("↺", body)
        self.assertIn("Fil<br>IMG_20240102.nef", month_body)

    def test_run_server_filter_browser_uses_exclusive_dates_and_location_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            for name, content in (
                ("IMG_20231201.jpg", b"after-boundary"),
                ("IMG_20240102.jpg", jpeg_with_exif_camera("Apple", "iPhone 17")),
                ("IMG_20241212.jpg", b"before-boundary"),
                ("IMG_20250101.jpg", b"manual-date-match"),
                ("IMG_20260115.jpg", b"manual-christmas-eve"),
            ):
                (source / name).write_bytes(content)

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                camera_row = conn.execute(
                    "SELECT camera_make, camera_model FROM files WHERE id = 2"
                ).fetchone()
                self.assertEqual((camera_row["camera_make"], camera_row["camera_model"]), ("Apple", "iPhone 17"))
                conn.execute(
                    "UPDATE files SET gps_lat = 59.9, gps_lon = 10.7, gps_source = 'exiftool' WHERE id IN (2, 3)"
                )
                oslo_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
                conn.execute("UPDATE files SET h3_res7 = ? WHERE id = 2", (oslo_cell,))
                db.set_custom_geo_place(conn, slug="oslo-test", name="Oslo test", h3_cells=[oslo_cell])
                db.tag_file(conn, file_id=2, tag_name="Ute av fokus")
                conn.execute(
                    """
                    UPDATE files
                    SET date_source = CASE id
                        WHEN 1 THEN 'filename'
                        WHEN 2 THEN 'metadata'
                        WHEN 3 THEN 'mtime'
                        WHEN 4 THEN 'filename'
                        WHEN 5 THEN 'filename'
                    END
                    """
                )
                conn.execute(
                    """
                    UPDATE files
                    SET size_bytes = CASE id
                        WHEN 1 THEN 100
                        WHEN 2 THEN 409600
                        WHEN 3 THEN 3145728
                        WHEN 4 THEN 2097152
                        WHEN 5 THEN 4096
                    END
                    """
                )
                conn.execute(
                    """
                    UPDATE files
                    SET manual_date_from = '2024-06-15',
                        manual_date_to = '2024-06-15',
                        gps_source = 'manual-h3',
                        h3_res7 = '872830828ffffff'
                    WHERE id = 4
                    """
                )
                conn.execute(
                    """
                    UPDATE files
                    SET manual_date_from = '2021-12-24',
                        manual_date_to = '2021-12-24',
                        gps_source = 'manual-h3',
                        h3_res7 = '87283082dffffff',
                        h3_res11 = '8b283082d8d4fff'
                    WHERE id = 5
                    """
                )
                conn.execute(
                    """
                    UPDATE files
                    SET media_width = CASE id
                        WHEN 2 THEN 400
                        WHEN 3 THEN 1000
                    END,
                        media_height = CASE id
                        WHEN 2 THEN 800
                        WHEN 3 THEN 500
                    END
                    WHERE id IN (2, 3)
                    """
                )
                conn.execute(
                    """
                    UPDATE files
                    SET view_rotation_degrees = CASE id
                        WHEN 1 THEN 0
                        WHEN 2 THEN 90
                        WHEN 3 THEN 180
                        WHEN 4 THEN 270
                    END
                    WHERE id IN (1, 2, 3, 4)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename, stored_filename,
                        sha256, size_bytes, taken_date, date_source, name_conflict
                    ) VALUES(
                        'udatert/no_date.bin', 'udatert/no_date.bin', 'no_date.bin', 'no_date.bin',
                        'missing-date', 12, NULL, 'mtime', 0
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename, stored_filename,
                        sha256, size_bytes, taken_date, date_source, name_conflict, deleted_at,
                        view_rotation_degrees
                    ) VALUES(
                        'deleted/2024/01/deleted.jpg', 'deleted/2024/01/deleted.jpg', 'deleted.jpg', 'deleted.jpg',
                        'deleted-row', 20, '2024-01-03', 'filename', 0, CURRENT_TIMESTAMP, 90
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Viljar')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Jill')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 2, 'face-key-2', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 3, 'face-key-3', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 2, 0.91)")
                face_conn.execute("INSERT INTO person_files(person_id, file_id) VALUES(2, 2)")
                face_conn.commit()
            finally:
                face_conn.close()

            source_filter = text_filter_browser_source("after:2023-12-01 before:2024-12-12")
            gps_filter = text_filter_browser_source("location:gps")
            manual_filter = text_filter_browser_source("after:2023-12-01 before:2024-12-12 location:manual")
            small_filter = text_filter_browser_source("size<300KB")
            large_filter = text_filter_browser_source("size>2MB")
            combined_size_filter = text_filter_browser_source("after:2023-12-01 before:2024-12-12 size>300KB")
            manual_date_filter = text_filter_browser_source("date:manual")
            metadata_date_filter = text_filter_browser_source("date:metadata")
            filename_date_filter = text_filter_browser_source("date:filename")
            mtime_date_filter = text_filter_browser_source("date:mtime")
            place_filter = text_filter_browser_source("location:oslo-test", target)
            camera_filter = text_filter_browser_source('camera:"iPhone"', target)
            self.assertTrue(source_has_sql_filter(camera_filter))
            deleted_filter = text_filter_browser_source("is:deleted")
            rotated_filter = text_filter_browser_source("is:rotated")
            deleted_rotated_filter = text_filter_browser_source("is:deleted is:rotated")
            rotated_metadata_filter = text_filter_browser_source("is:rotated date:metadata")
            extension_filter = text_filter_browser_source("extension:jpg")
            filename_filter = text_filter_browser_source("filename:20240102")
            missing_date_filter = text_filter_browser_source("missing:date")
            missing_gps_filter = text_filter_browser_source("missing:gps")
            missing_metadata_filter = text_filter_browser_source("missing:metadata")
            orientation_landscape_filter = text_filter_browser_source("orientation:landscape")
            orientation_portrait_filter = text_filter_browser_source("orientation:portrait")
            width_gt_filter = text_filter_browser_source("width>500")
            width_gte_filter = text_filter_browser_source("width>=400")
            width_lt_filter = text_filter_browser_source("width<500")
            width_lte_filter = text_filter_browser_source("width<=400")
            width_eq_filter = text_filter_browser_source("width=400")
            height_gt_filter = text_filter_browser_source("height>700")
            height_gte_filter = text_filter_browser_source("height>=500")
            height_lt_filter = text_filter_browser_source("height<700")
            height_lte_filter = text_filter_browser_source("height<=500")
            height_eq_filter = text_filter_browser_source("height=500")
            width_range_filter = text_filter_browser_source("width>500 width<1200")
            width_inclusive_range_filter = text_filter_browser_source("width>=400 width<=400")
            size_gte_filter = text_filter_browser_source("size>=300KB")
            size_lte_filter = text_filter_browser_source("size<=2MB")
            path_filter = text_filter_browser_source("path:2024/01")
            person_filter = text_filter_browser_source("person:viljar")
            person_both_filter = text_filter_browser_source("person:viljar person:jill")
            source_name_filter = text_filter_browser_source("source:source")
            tag_filter = text_filter_browser_source("tag:ute-av-fokus")
            type_file_filter = text_filter_browser_source("type:file")
            type_image_filter = text_filter_browser_source("type:image")
            year_filter = text_filter_browser_source("year:2024")
            year_eq_filter = text_filter_browser_source("year=2024")
            year_range_filter = text_filter_browser_source("year>2021 year<2024")
            month_filter = text_filter_browser_source("month:12")
            month_eq_filter = text_filter_browser_source("month=12")
            day_filter = text_filter_browser_source("day:24")
            day_eq_filter = text_filter_browser_source("day=24")
            month_day_filter = text_filter_browser_source("month:12 day:24")
            month_range_filter = text_filter_browser_source("month>5 month<10")
            month_inclusive_range_filter = text_filter_browser_source("month>=6 month<=10")
            day_range_filter = text_filter_browser_source("day>10 day<20")
            day_inclusive_range_filter = text_filter_browser_source("day>=10 day<=20")
            h3res_exact_filter = text_filter_browser_source("location:manual h3res:7")
            h3res_lt_filter = text_filter_browser_source("location:manual h3res<8")
            h3res_gt_filter = text_filter_browser_source("location:manual h3res>10")
            h3res_gte_filter = text_filter_browser_source("location:manual h3res>=8")
            h3res_lte_filter = text_filter_browser_source("location:manual h3res<=11")
            first_item = source_item_by_id(target, source_filter, 2)
            person_item = source_item_by_id(target, person_filter, 2)
            manual_item = source_item_by_id(target, manual_filter, 4)
            self.assertIsNotNone(first_item)
            self.assertIsNotNone(person_item)
            self.assertIsNotNone(manual_item)
            date_month = source_month_items(target, source_filter, "2024-01")
            date_month_nav = source_month_navigation(target, source_filter, first_item)
            person_month_nav = source_month_navigation(target, person_filter, person_item)
            gps_month = source_month_items(target, gps_filter, "2024-12")
            manual_month = source_month_items(target, manual_filter, "2024-06")
            small_month = source_month_items(target, small_filter, "2023-12")
            large_month = source_month_items(target, large_filter, "2024-12")
            combined_size_month = source_month_items(target, combined_size_filter, "2024-06")
            manual_date_month = source_month_items(target, manual_date_filter, "2024-06")
            metadata_date_month = source_month_items(target, metadata_date_filter, "2024-01")
            filename_date_month = source_month_items(target, filename_date_filter, "2023-12")
            mtime_date_month = source_month_items(target, mtime_date_filter, "2024-12")
            place_month = source_month_items(target, place_filter, "2024-01")
            camera_month = source_month_items(target, camera_filter, "2024-01")
            deleted_month = source_month_items(target, deleted_filter, "2024-01")
            rotated_ids = [
                item_id
                for item_id in range(1, 8)
                if source_item_by_id(target, rotated_filter, item_id) is not None
            ]
            deleted_rotated_month = source_month_items(target, deleted_rotated_filter, "2024-01")
            rotated_metadata_month = source_month_items(target, rotated_metadata_filter, "2024-01")
            extension_month = source_month_items(target, extension_filter, "2024-01")
            filename_month = source_month_items(target, filename_filter, "2024-01")
            missing_date_item = source_item_by_id(target, missing_date_filter, 6)
            missing_gps_month = source_month_items(target, missing_gps_filter, "2023-12")
            missing_metadata_month = source_month_items(target, missing_metadata_filter, "2023-12")
            orientation_landscape_month = source_month_items(target, orientation_landscape_filter, "2024-12")
            orientation_portrait_month = source_month_items(target, orientation_portrait_filter, "2024-01")
            width_gt_month = source_month_items(target, width_gt_filter, "2024-12")
            width_gte_month = source_month_items(target, width_gte_filter, "2024-01")
            width_lt_month = source_month_items(target, width_lt_filter, "2024-01")
            width_lte_month = source_month_items(target, width_lte_filter, "2024-01")
            width_eq_month = source_month_items(target, width_eq_filter, "2024-01")
            height_gt_month = source_month_items(target, height_gt_filter, "2024-01")
            height_gte_month = source_month_items(target, height_gte_filter, "2024-12")
            height_lt_month = source_month_items(target, height_lt_filter, "2024-12")
            height_lte_month = source_month_items(target, height_lte_filter, "2024-12")
            height_eq_month = source_month_items(target, height_eq_filter, "2024-12")
            width_range_month = source_month_items(target, width_range_filter, "2024-12")
            width_inclusive_range_month = source_month_items(target, width_inclusive_range_filter, "2024-01")
            size_gte_month = source_month_items(target, size_gte_filter, "2024-12")
            size_lte_month = source_month_items(target, size_lte_filter, "2023-12")
            path_month = source_month_items(target, path_filter, "2024-01")
            person_january_month = source_month_items(target, person_filter, "2024-01")
            person_december_month = source_month_items(target, person_filter, "2024-12")
            person_both_january_month = source_month_items(target, person_both_filter, "2024-01")
            person_both_december_month = source_month_items(target, person_both_filter, "2024-12")
            source_name_month = source_month_items(target, source_name_filter, "2024-01")
            tag_month = source_month_items(target, tag_filter, "2024-01")
            type_file_item = source_item_by_id(target, type_file_filter, 6)
            type_image_month = source_month_items(target, type_image_filter, "2024-01")
            year_january_2024 = source_month_items(target, year_filter, "2024-01")
            year_eq_december_2024 = source_month_items(target, year_eq_filter, "2024-12")
            year_december_2021 = source_month_items(target, year_filter, "2021-12")
            year_range_december_2023 = source_month_items(target, year_range_filter, "2023-12")
            year_range_december_2024 = source_month_items(target, year_range_filter, "2024-12")
            month_december_2021 = source_month_items(target, month_filter, "2021-12")
            month_eq_december_2021 = source_month_items(target, month_eq_filter, "2021-12")
            month_january_2022 = source_month_items(target, month_filter, "2022-01")
            month_december_2023 = source_month_items(target, month_filter, "2023-12")
            month_december_2024 = source_month_items(target, month_filter, "2024-12")
            day_december_2021 = source_month_items(target, day_filter, "2021-12")
            day_eq_december_2021 = source_month_items(target, day_eq_filter, "2021-12")
            month_day_december_2021 = source_month_items(target, month_day_filter, "2021-12")
            month_day_january_2024 = source_month_items(target, month_day_filter, "2024-01")
            month_range_june_2024 = source_month_items(target, month_range_filter, "2024-06")
            month_inclusive_range_june_2024 = source_month_items(target, month_inclusive_range_filter, "2024-06")
            month_range_december_2024 = source_month_items(target, month_range_filter, "2024-12")
            day_range_december_2024 = source_month_items(target, day_range_filter, "2024-12")
            day_inclusive_range_december_2024 = source_month_items(target, day_inclusive_range_filter, "2024-12")
            day_range_december_2021 = source_month_items(target, day_range_filter, "2021-12")
            h3res_exact_month = source_month_items(target, h3res_exact_filter, "2024-06")
            h3res_lt_month = source_month_items(target, h3res_lt_filter, "2024-06")
            h3res_gt_month = source_month_items(target, h3res_gt_filter, "2021-12")
            h3res_gte_month = source_month_items(target, h3res_gte_filter, "2021-12")
            h3res_lte_month = source_month_items(target, h3res_lte_filter, "2024-06")
            date_body = source_item_page_html(
                target,
                source_filter,
                first_item,
                *adjacent_source_items(target, source_filter, first_item),
                date_month_nav,
            )
            date_years_body = source_years_page_html(target, source_filter)
            date_year_body = source_year_months_page_html(target, source_filter, "2024")
            date_month_body = source_month_page_html(target, source_filter, "2024-01", date_month)
            person_body = source_item_page_html(
                target,
                person_filter,
                person_item,
                *adjacent_source_items(target, person_filter, person_item),
                person_month_nav,
            )
            missing_date_years_body = source_years_page_html(target, missing_date_filter)
            date_filter_excludes_after_boundary = source_item_by_id(target, source_filter, 1) is None
            date_filter_excludes_before_boundary = source_item_by_id(target, source_filter, 3) is None
            empty_body = empty_source_html(text_filter_browser_source("before:1900-01-01"))

        self.assertEqual(source_filter.root_url, "/filter/after%3A2023-12-01%20before%3A2024-12-12")
        self.assertEqual(person_both_filter.root_url, "/filter/person%3Aviljar%20person%3Ajill")
        self.assertTrue(date_filter_excludes_after_boundary)
        self.assertTrue(date_filter_excludes_before_boundary)
        self.assertEqual([item["id"] for item in date_month], [2])
        self.assertEqual([item["id"] for item in gps_month], [3])
        self.assertEqual([item["id"] for item in manual_month], [4])
        self.assertEqual([item["id"] for item in small_month], [1])
        self.assertEqual([item["id"] for item in large_month], [3])
        self.assertEqual([item["id"] for item in combined_size_month], [4])
        self.assertEqual([item["id"] for item in manual_date_month], [4])
        self.assertEqual([item["id"] for item in metadata_date_month], [2])
        self.assertEqual([item["id"] for item in filename_date_month], [1])
        self.assertEqual([item["id"] for item in mtime_date_month], [3])
        self.assertEqual([item["id"] for item in place_month], [2])
        self.assertEqual([item["id"] for item in camera_month], [2])
        self.assertEqual([item["id"] for item in deleted_month], [7])
        self.assertEqual(rotated_ids, [2, 3, 4])
        self.assertEqual([item["id"] for item in deleted_rotated_month], [7])
        self.assertEqual([item["id"] for item in rotated_metadata_month], [2])
        self.assertEqual([item["id"] for item in extension_month], [2])
        self.assertEqual([item["id"] for item in filename_month], [2])
        self.assertIsNotNone(missing_date_item)
        self.assertEqual([item["id"] for item in missing_gps_month], [1])
        self.assertEqual([item["id"] for item in missing_metadata_month], [1])
        self.assertEqual([item["id"] for item in orientation_landscape_month], [3])
        self.assertEqual([item["id"] for item in orientation_portrait_month], [2])
        self.assertEqual([item["id"] for item in width_gt_month], [3])
        self.assertEqual([item["id"] for item in width_gte_month], [2])
        self.assertEqual([item["id"] for item in width_lt_month], [2])
        self.assertEqual([item["id"] for item in width_lte_month], [2])
        self.assertEqual([item["id"] for item in width_eq_month], [2])
        self.assertEqual([item["id"] for item in height_gt_month], [2])
        self.assertEqual([item["id"] for item in height_gte_month], [3])
        self.assertEqual([item["id"] for item in height_lt_month], [3])
        self.assertEqual([item["id"] for item in height_lte_month], [3])
        self.assertEqual([item["id"] for item in height_eq_month], [3])
        self.assertEqual([item["id"] for item in width_range_month], [3])
        self.assertEqual([item["id"] for item in width_inclusive_range_month], [2])
        self.assertEqual([item["id"] for item in size_gte_month], [3])
        self.assertEqual([item["id"] for item in size_lte_month], [1])
        self.assertEqual([item["id"] for item in path_month], [2])
        self.assertEqual([item["id"] for item in person_january_month], [2])
        self.assertEqual([item["id"] for item in person_december_month], [3])
        self.assertEqual([item["id"] for item in person_both_january_month], [2])
        self.assertEqual([item["id"] for item in person_both_december_month], [])
        self.assertEqual([item["id"] for item in source_name_month], [2])
        self.assertEqual([item["id"] for item in tag_month], [2])
        self.assertIsNotNone(type_file_item)
        self.assertEqual([item["id"] for item in type_image_month], [2])
        self.assertEqual([item["id"] for item in year_january_2024], [2])
        self.assertEqual([item["id"] for item in year_eq_december_2024], [3])
        self.assertEqual([item["id"] for item in year_december_2021], [])
        self.assertEqual([item["id"] for item in year_range_december_2023], [1])
        self.assertEqual([item["id"] for item in year_range_december_2024], [])
        self.assertEqual([item["id"] for item in month_december_2021], [5])
        self.assertEqual([item["id"] for item in month_eq_december_2021], [5])
        self.assertEqual([item["id"] for item in month_january_2022], [])
        self.assertEqual([item["id"] for item in month_december_2023], [1])
        self.assertEqual([item["id"] for item in month_december_2024], [3])
        self.assertEqual([item["id"] for item in day_december_2021], [5])
        self.assertEqual([item["id"] for item in day_eq_december_2021], [5])
        self.assertEqual([item["id"] for item in month_day_december_2021], [5])
        self.assertEqual([item["id"] for item in month_day_january_2024], [])
        self.assertEqual([item["id"] for item in month_range_june_2024], [4])
        self.assertEqual([item["id"] for item in month_inclusive_range_june_2024], [4])
        self.assertEqual([item["id"] for item in month_range_december_2024], [])
        self.assertEqual([item["id"] for item in day_range_december_2024], [3])
        self.assertEqual([item["id"] for item in day_inclusive_range_december_2024], [3])
        self.assertEqual([item["id"] for item in day_range_december_2021], [])
        self.assertEqual([item["id"] for item in h3res_exact_month], [4])
        self.assertEqual([item["id"] for item in h3res_lt_month], [4])
        self.assertEqual([item["id"] for item in h3res_gt_month], [5])
        self.assertEqual([item["id"] for item in h3res_gte_month], [5])
        self.assertEqual([item["id"] for item in h3res_lte_month], [4])
        self.assertIn("Filtersøk: after:2023-12-01 before:2024-12-12", date_body)
        self.assertIn("Filtersøk: after:2023-12-01 before:2024-12-12 (2 treff)", date_body)
        self.assertIn('title="2 treff i filtersøket"', date_body)
        self.assertIn("Filtersøk: after:2023-12-01 before:2024-12-12 (2 treff)", date_years_body)
        self.assertIn('href="/filter/after%3A2023-12-01%20before%3A2024-12-12/year/2024"', date_years_body)
        self.assertIn(">2024</div>", date_years_body)
        self.assertNotIn('href="/filter/after%3A2023-12-01%20before%3A2024-12-12/year/2023"', date_years_body)
        self.assertNotIn('href="/filter/after%3A2023-12-01%20before%3A2024-12-12/year/2025"', date_years_body)
        self.assertIn(">2 måneder, 2 bilder</div>", date_years_body)
        self.assertIn('href="/filter/after%3A2023-12-01%20before%3A2024-12-12/year/2024" title="Neste år" data-key-nav="next-year">r ▶</a>', date_years_body)
        self.assertIn('href="/filter/after%3A2023-12-01%20before%3A2024-12-12/month/2024-01" title="Neste måned" data-key-nav="next-month">ed ▶</a>', date_years_body)
        self.assertIn("Filtersøk: after:2023-12-01 before:2024-12-12 (2 treff)", date_year_body)
        self.assertIn('title="2 treff i filtersøket"', date_year_body)
        self.assertIn('href="/filter/after%3A2023-12-01%20before%3A2024-12-12" title="2 treff i filtersøket">Filtersøk: after:2023-12-01 before:2024-12-12 (2 treff)</a><span class="sep">/</span>2024</nav>', date_year_body)
        self.assertIn('href="/filter/after%3A2023-12-01%20before%3A2024-12-12/month/2024-01"', date_year_body)
        self.assertIn("Filtersøk: after:2023-12-01 before:2024-12-12 (2 treff)", date_month_body)
        self.assertIn('title="2 treff i filtersøket"', date_month_body)
        self.assertIn("/filter/after%3A2023-12-01%20before%3A2024-12-12/item/4", date_body)
        self.assertIn('href="/filter">Filtersøk</a>', date_body)
        self.assertIn("Filtersøk: person:viljar", person_body)
        self.assertIn("/filter/person%3Aviljar/item/3", person_body)
        self.assertIn("Ingen daterte bilder matcher denne visningen.", missing_date_years_body)
        self.assertNotIn("/filter/missing%3Adate/year/", missing_date_years_body)
        self.assertNotIn("/filter/missing%3Adate/item/", missing_date_years_body)
        self.assertIn("Ingen aktive bilder matcher filtersøket.", empty_body)

    def test_run_server_filter_parser_rejects_invalid_queries(self) -> None:
        text_filter = parse_text_filter('  after:2023-12-01 camera:"iPhone 17" date:metadata filename:IMG location:gps size>2MB size<2.5GB width>1024 height=2000 ')
        self.assertEqual(text_filter.query, 'after:2023-12-01 camera:"iPhone 17" date:metadata filename:IMG location:gps size>2MB size<2.5GB width>1024 height=2000')
        self.assertEqual(text_filter.camera, "iPhone 17")
        self.assertEqual(text_filter.date_source, "metadata")
        self.assertEqual(text_filter.filename, "IMG")
        self.assertEqual(text_filter.persons, ())
        self.assertEqual(text_filter.size_gt, 2 * 1024 * 1024)
        self.assertEqual(text_filter.size_lt, int(2.5 * 1024 * 1024 * 1024))
        self.assertEqual(text_filter.width_gt, 1024)
        self.assertEqual(text_filter.height_eq, 2000)
        self.assertEqual(parse_text_filter("IMG").filename, "IMG")
        self.assertEqual(parse_text_filter("IMG").query, "filename:IMG")
        filename_phrase = parse_text_filter("sommer 2024")
        self.assertEqual(filename_phrase.filename, "sommer 2024")
        self.assertEqual(filename_phrase.query, 'filename:"sommer 2024"')
        mixed_filename = parse_text_filter("year:2024 sommer 2024 type:image")
        self.assertEqual(mixed_filename.filename, "sommer 2024")
        self.assertEqual(mixed_filename.year, 2024)
        self.assertEqual(mixed_filename.media_type, "image")
        self.assertEqual(mixed_filename.query, 'year:2024 filename:"sommer 2024" type:image')
        self.assertEqual(parse_text_filter("width>=1024").width_gte, 1024)
        self.assertIsNone(parse_text_filter("width>=1024").width_gt)
        self.assertEqual(parse_text_filter("width<=2000").width_lte, 2000)
        self.assertEqual(parse_text_filter("height>=1024").height_gte, 1024)
        self.assertEqual(parse_text_filter("height<=2000").height_lte, 2000)
        self.assertEqual(parse_text_filter("size>=300KB").size_gte, 300 * 1024)
        self.assertEqual(parse_text_filter("size<=2MB").size_lte, 2 * 1024 * 1024)
        self.assertEqual(parse_text_filter("month:12").month, 12)
        self.assertEqual(parse_text_filter("month:12").month_eq, 12)
        self.assertEqual(parse_text_filter("month=12").month, 12)
        self.assertEqual(parse_text_filter("month=12").month_eq, 12)
        self.assertEqual(parse_text_filter("day:24").day, 24)
        self.assertEqual(parse_text_filter("day:24").day_eq, 24)
        self.assertEqual(parse_text_filter("day=24").day, 24)
        self.assertEqual(parse_text_filter("day=24").day_eq, 24)
        self.assertEqual(parse_text_filter("year:2024").year, 2024)
        self.assertEqual(parse_text_filter("year:2024").year_eq, 2024)
        self.assertEqual(parse_text_filter("year=2024").year, 2024)
        self.assertEqual(parse_text_filter("year=2024").year_eq, 2024)
        self.assertEqual(parse_text_filter("person:Viljar").persons, ("Viljar",))
        self.assertEqual(parse_text_filter("person:Viljar person:Jill").persons, ("Viljar", "Jill"))
        year_range = parse_text_filter("year>2020 year<2025")
        year_inclusive_range = parse_text_filter("year>=2020 year<=2025")
        month_range = parse_text_filter("month>6 month<10")
        month_inclusive_range = parse_text_filter("month>=6 month<=10")
        day_range = parse_text_filter("day>10 day<20")
        day_inclusive_range = parse_text_filter("day>=10 day<=20")
        self.assertEqual((year_range.year_gt, year_range.year_lt), (2020, 2025))
        self.assertEqual(year_range.year, None)
        self.assertEqual((year_inclusive_range.year_gte, year_inclusive_range.year_lte), (2020, 2025))
        self.assertEqual(year_inclusive_range.year, None)
        self.assertEqual((month_range.month_gt, month_range.month_lt), (6, 10))
        self.assertEqual((month_range.month, month_range.day), (None, None))
        self.assertEqual((month_inclusive_range.month_gte, month_inclusive_range.month_lte), (6, 10))
        self.assertEqual((month_inclusive_range.month, month_inclusive_range.day), (None, None))
        self.assertEqual((day_range.day_gt, day_range.day_lt), (10, 20))
        self.assertEqual((day_range.month, day_range.day), (None, None))
        self.assertEqual((day_inclusive_range.day_gte, day_inclusive_range.day_lte), (10, 20))
        self.assertEqual((day_inclusive_range.month, day_inclusive_range.day), (None, None))
        h3res_gt = parse_text_filter("location:manual h3res>10")
        h3res_lt = parse_text_filter("location:manual h3res<8")
        h3res_eq = parse_text_filter("location:manual h3res:11")
        h3res_gte = parse_text_filter("location:manual h3res>=8")
        h3res_lte = parse_text_filter("location:manual h3res<=11")
        self.assertEqual((h3res_gt.h3res_operator, h3res_gt.h3res_value), (">", 10))
        self.assertEqual((h3res_lt.h3res_operator, h3res_lt.h3res_value), ("<", 8))
        self.assertEqual((h3res_eq.h3res_operator, h3res_eq.h3res_value), ("=", 11))
        self.assertEqual((h3res_gte.h3res_operator, h3res_gte.h3res_value), (">=", 8))
        self.assertEqual((h3res_lte.h3res_operator, h3res_lte.h3res_value), ("<=", 11))
        for query, attrs in (
            ("after:2023-12-01", {"after": dt.date(2023, 12, 1)}),
            ("before:2024-12-12", {"before": dt.date(2024, 12, 12)}),
            ("camera:Canon", {"camera": "Canon"}),
            ("location:gps", {"location": "gps"}),
            ("date:manual", {"date_source": "manual"}),
            ("is:deleted", {"deleted": True}),
            ("is:rotated", {"rotated": True}),
            ("extension:.JPG", {"extension": "jpg"}),
            ("filename:IMG", {"filename": "IMG"}),
            ("missing:metadata", {"missing": "metadata"}),
            ("orientation:portrait", {"orientation": "portrait"}),
            ("path:2024/01", {"path": "2024/01"}),
            ("person:Viljar", {"persons": ("Viljar",)}),
            ("source:phone", {"source": "phone"}),
            ("tag:ute-av-fokus", {"tag": "ute-av-fokus"}),
            ("type:video", {"media_type": "video"}),
            ("size<300KB", {"size_lt": 300 * 1024}),
            ("size>=300KB", {"size_gte": 300 * 1024}),
            ("size<=2MB", {"size_lte": 2 * 1024 * 1024}),
            ("year=2024", {"year": 2024, "year_eq": 2024}),
            ("year>2020", {"year_gt": 2020}),
            ("year>=2020", {"year_gte": 2020}),
            ("year<2025", {"year_lt": 2025}),
            ("year<=2025", {"year_lte": 2025}),
            ("month=12", {"month": 12, "month_eq": 12}),
            ("month>6", {"month_gt": 6}),
            ("month>=6", {"month_gte": 6}),
            ("month<10", {"month_lt": 10}),
            ("month<=10", {"month_lte": 10}),
            ("day=24", {"day": 24, "day_eq": 24}),
            ("day>10", {"day_gt": 10}),
            ("day>=10", {"day_gte": 10}),
            ("day<20", {"day_lt": 20}),
            ("day<=20", {"day_lte": 20}),
            ("width<2000", {"width_lt": 2000}),
            ("width>=1024", {"width_gte": 1024}),
            ("width<=2000", {"width_lte": 2000}),
            ("width=1024", {"width_eq": 1024}),
            ("height>1024", {"height_gt": 1024}),
            ("height>=1024", {"height_gte": 1024}),
            ("height<2000", {"height_lt": 2000}),
            ("height<=2000", {"height_lte": 2000}),
        ):
            with self.subTest(query=query):
                parsed = parse_text_filter(query)
                for attr, expected in attrs.items():
                    self.assertEqual(getattr(parsed, attr), expected)
        for query, message in (
            ("after:2023-02-30", "after må være en dato på formen YYYY-MM-DD."),
            ("before:", "Filteret mangler verdi: before:"),
            ("date:gps", "date må være manual, metadata, filename eller mtime."),
            ("date:manual date:metadata", "date kan bare brukes én gang."),
            ("month:12 month:11", "month kan bare brukes én gang."),
            ("month:12 month=11", "month= kan bare brukes én gang."),
            ("month=12 month>6", "month> kan ikke kombineres med month=."),
            ("month>6 month>7", "month> kan bare brukes én gang."),
            ("month>6 month>=7", "month>= kan ikke kombineres med month>."),
            ("month:0", "month må være et heltall fra 1 til 12."),
            ("month>0", "month må være et heltall fra 1 til 12."),
            ("month:13", "month må være et heltall fra 1 til 12."),
            ("month=13", "month må være et heltall fra 1 til 12."),
            ("month<13", "month må være et heltall fra 1 til 12."),
            ("month:desember", "month må være et heltall fra 1 til 12."),
            ("day:24 day:25", "day kan bare brukes én gang."),
            ("day:24 day=25", "day= kan bare brukes én gang."),
            ("day=24 day>10", "day> kan ikke kombineres med day=."),
            ("day<24 day<25", "day< kan bare brukes én gang."),
            ("day<20 day<=19", "day<= kan ikke kombineres med day<."),
            ("day:0", "day må være et heltall fra 1 til 31."),
            ("day>0", "day må være et heltall fra 1 til 31."),
            ("day:32", "day må være et heltall fra 1 til 31."),
            ("day=32", "day må være et heltall fra 1 til 31."),
            ("day<32", "day må være et heltall fra 1 til 31."),
            ("day:julaften", "day må være et heltall fra 1 til 31."),
            ("is:other", "Ukjent is-filter: other. Gyldige verdier er deleted og rotated."),
            ("deleted:true", "Ukjent filter: deleted"),
            ("deleted:false", "Ukjent filter: deleted"),
            ("extension:..jpg", "extension må være en filendelse"),
            ("missing:camera", "missing må være gps, date eller metadata."),
            ("orientation:square", "orientation må være portrait eller landscape."),
            ("after:2023-01-01 after:2024-01-01", "after kan bare brukes én gang."),
            ("size>2MB size>3MB", "size> kan bare brukes én gang."),
            ("size>=2MB size>=3MB", "size>= kan bare brukes én gang."),
            ("size>stor", "size må skrives som for eksempel size<300KB eller size>2MB."),
            ("year:2024 year:2023", "year kan bare brukes én gang."),
            ("year:2024 year=2023", "year= kan bare brukes én gang."),
            ("year=2024 year>2020", "year> kan ikke kombineres med year=."),
            ("year>2020 year>2021", "year> kan bare brukes én gang."),
            ("year>2020 year>=2021", "year>= kan ikke kombineres med year>."),
            ("year:0", "year må være et heltall fra 1 til 9999."),
            ("year=10000", "year må være et heltall fra 1 til 9999."),
            ("year:nyere", "year må være et heltall fra 1 til 9999."),
            ("width>100 width>200", "width> kan bare brukes én gang."),
            ("width>=100 width>=200", "width>= kan bare brukes én gang."),
            ("width>100 width>=200", "width>= kan ikke kombineres med width>."),
            ("width>=100 width>200", "width> kan ikke kombineres med width>=."),
            ("width>stor", "width må være et heltall i piksler uten enhet"),
            ("location:manual h3res:12", "h3res må være et heltall fra 0 til 11."),
            ("location:manual h3res:-1", "h3res må være et heltall fra 0 til 11."),
            ("location:manual h3res:elleve", "h3res må være et heltall fra 0 til 11."),
            ("location:manual h3res:7 h3res>10", "h3res kan bare brukes én gang."),
            ("h3res:11", "h3res kan bare brukes sammen med location:manual."),
            ("location:gps h3res:11", "h3res kan bare brukes sammen med location:manual."),
            ("location:oslo h3res:11", "h3res kan bare brukes sammen med location:manual."),
            ("type:audio", "type må være image, video eller file."),
            ("filename:IMG sommer", "filename kan bare brukes én gang."),
            ('camera:"iPhone', "Ugyldige anførselstegn i filtersøk."),
            ("unknown:canon", "Ukjent filter: unknown"),
        ):
            with self.subTest(query=query):
                with self.assertRaisesRegex(ValueError, message):
                    parse_text_filter(query)

        combined_is_filter = parse_text_filter("is:deleted is:rotated")
        self.assertTrue(combined_is_filter.deleted)
        self.assertTrue(combined_is_filter.rotated)
        self.assertEqual(combined_is_filter.query, "is:deleted is:rotated")
        self.assertEqual(parse_text_filter("  is:deleted  ").query, "is:deleted")
        self.assertEqual(parse_text_filter("  is:rotated  ").query, "is:rotated")
        self.assertEqual(text_filter_browser_source("is:deleted").root_url, "/filter/is%3Adeleted")
        self.assertEqual(text_filter_browser_source("is:rotated").root_url, "/filter/is%3Arotated")
        self.assertEqual(
            text_filter_browser_source("is:deleted is:rotated").root_url,
            "/filter/is%3Adeleted%20is%3Arotated",
        )

    def test_run_server_filter_parser_normalizes_spaces_around_operators(self) -> None:
        for query, canonical_query, attr, expected in (
            ("month > 6", "month>6", "month_gt", 6),
            ("year >= 2020", "year>=2020", "year_gte", 2020),
            ("month> 6", "month>6", "month_gt", 6),
            ("month >6", "month>6", "month_gt", 6),
            ("day <= 25", "day<=25", "day_lte", 25),
            ("width >= 1024", "width>=1024", "width_gte", 1024),
            ("height < 2000", "height<2000", "height_lt", 2000),
            ("size < 2MB", "size<2MB", "size_lt", 2 * 1024 * 1024),
            ("location:manual h3res >= 8", "location:manual h3res>=8", "h3res_value", 8),
        ):
            with self.subTest(query=query):
                parsed = parse_text_filter(query)
                self.assertEqual(parsed.query, canonical_query)
                self.assertEqual(getattr(parsed, attr), expected)

        combined = parse_text_filter("month > 6 day <= 25")
        self.assertEqual(combined.query, "month>6 day<=25")

        for query in ('camera:"iPhone 12"', 'tag:"Ute av fokus"', 'source:"Mobil 2024"'):
            with self.subTest(query=query):
                self.assertEqual(parse_text_filter(query).query, query)

        for query, message in (
            ("month >", "Filteret mangler verdi: month>"),
            ("width >=", "Filteret mangler verdi: width>="),
            ("size <", "Filteret mangler verdi: size<"),
        ):
            with self.subTest(query=query):
                with self.assertRaisesRegex(ValueError, message):
                    parse_text_filter(query)

    def test_run_server_hides_motion_video_unless_filter_explicitly_requests_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "PXL_20250102_123.MP").write_bytes(minimal_mp4_with_creation_date(dt.date(2025, 1, 2)))
            (source / "PXL_20250102_123.MP.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            month_items = browser_month_items(target, "2025-01")
            type_video_source = text_filter_browser_source("type:video")
            filename_source = text_filter_browser_source("filename:PXL_20250102_123")
            type_video_items = source_month_items(target, type_video_source, "2025-01")
            filename_items = source_month_items(target, filename_source, "2025-01")
            motion_file = read_server_file(target, str(type_video_items[0]["id"]))
            image_item = month_items[0]
            motion_item = type_video_items[0]
            image_body = item_page_html(
                target,
                image_item,
                *adjacent_browser_items(target, image_item),
                browser_month_navigation(target, image_item),
            )
            motion_body = source_item_page_html(
                target,
                type_video_source,
                motion_item,
                *adjacent_source_items(target, type_video_source, motion_item),
                source_month_navigation(target, type_video_source, motion_item),
            )

        self.assertEqual([item["stored_filename"] for item in month_items], ["PXL_20250102_123.MP.jpg"])
        self.assertEqual([item["stored_filename"] for item in type_video_items], ["PXL_20250102_123.mp4"])
        self.assertEqual(
            [item["stored_filename"] for item in filename_items],
            ["PXL_20250102_123.MP.jpg", "PXL_20250102_123.mp4"],
        )
        controls_start = image_body.index('<nav class="controls"')
        controls_end = image_body.index("</nav>", controls_start)
        controls_html = image_body[controls_start:controls_end]
        self.assertIn(".MP4</a>", controls_html)
        self.assertIn("/filter/filename%3APXL_20250102_123.mp4/item/", image_body)
        self.assertNotIn("Motion-video: PXL_20250102_123.mp4", image_body)
        self.assertNotIn('<footer class="browser-footer">', image_body)
        self.assertIn(f'href="/item/{int(image_item["id"])}">Vis JPG-bildet</a>', motion_body)
        self.assertNotIn("Åpne i alle bilder", motion_body)
        self.assertEqual(motion_file.content_type, "video/mp4")
        self.assertEqual(motion_file.content[4:8], b"ftyp")

    def test_run_server_hides_raw_sidecar_and_links_it_from_jpg(self) -> None:
        for extension in ("NEF", "PSD"):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / "target"
                source = root / "source"
                source.mkdir()
                sidecar_filename = f"DSC_0170.{extension}"
                (source / "DSC_0170.JPG").write_bytes(jpeg_with_exif_datetime("2019:03:03 12:00:00"))
                (source / sidecar_filename).write_bytes(minimal_tiff_with_datetime("2019:03:03 12:00:00"))

                self.assertEqual(run_cli(["create", str(target)]), 0)
                self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

                month_items = browser_month_items(target, "2019-03")
                type_file_source = text_filter_browser_source("type:file")
                extension_source = text_filter_browser_source(f"extension:{extension.lower()}")
                filename_source = text_filter_browser_source("filename:DSC_0170")
                type_file_items = source_month_items(target, type_file_source, "2019-03")
                extension_items = source_month_items(target, extension_source, "2019-03")
                filename_items = source_month_items(target, filename_source, "2019-03")
                image_item = month_items[0]
                sidecar_item = extension_items[0]
                sidecar_body = source_item_page_html(
                    target,
                    extension_source,
                    sidecar_item,
                    *adjacent_source_items(target, extension_source, sidecar_item),
                    source_month_navigation(target, extension_source, sidecar_item),
                )
                response: dict[str, object] = {}
                handler = object.__new__(BildebankRequestHandler)
                handler.server = SimpleNamespace(target=target)

                def fake_respond_json(content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    response["content"] = content
                    response["status"] = status

                handler.respond_json = fake_respond_json  # type: ignore[method-assign]
                handler.respond_item_info(f"file_id={sidecar_item['id']}")
                previous_item, next_item = adjacent_browser_items(target, image_item)
                month_nav = browser_month_navigation(target, image_item)
                self.assertIsNotNone(raw_sidecar_id_by_image_id(target, int(image_item["id"])))
                with patch("bildebank.server_browser.raw_sidecar_groups", side_effect=AssertionError("global raw scan")):
                    image_body = item_page_html(
                        target,
                        image_item,
                        previous_item,
                        next_item,
                        month_nav,
                    )

                self.assertEqual([item["stored_filename"] for item in month_items], ["DSC_0170.JPG"])
                self.assertEqual([item["stored_filename"] for item in type_file_items], [sidecar_filename])
                self.assertEqual([item["stored_filename"] for item in extension_items], [sidecar_filename])
                self.assertEqual(
                    [item["stored_filename"] for item in filename_items],
                    ["DSC_0170.JPG", sidecar_filename],
                )
                controls_start = image_body.index('<nav class="controls"')
                controls_end = image_body.index("</nav>", controls_start)
                controls_html = image_body[controls_start:controls_end]
                self.assertIn(f".{extension}</a>", controls_html)
                self.assertIn(f"/filter/filename%3ADSC_0170.{extension}/item/", image_body)
                self.assertNotIn(f"RAW-fil: {sidecar_filename}", image_body)
                self.assertNotIn('<footer class="browser-footer">', image_body)
                self.assertIn(f'href="/item/{int(image_item["id"])}">Vis JPG-bildet</a>', sidecar_body)
                self.assertNotIn("Åpne i alle bilder", sidecar_body)
                self.assertEqual(response["status"], HTTPStatus.OK)
                content = response["content"]
                assert isinstance(content, dict)
                self.assertIs(content["ok"], True)
                sidecar_info_html = str(content["html"])
                self.assertIn("<dt>Filnavn</dt>", sidecar_info_html)
                self.assertIn(sidecar_filename, sidecar_info_html)
                self.assertIn("<dt>Filstørrelse</dt>", sidecar_info_html)
                self.assertIn("<dt>Kilder</dt>", sidecar_info_html)
                self.assertIn(source.name, sidecar_info_html)

    def test_run_server_links_psd_sidecar_without_capture_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            jpg_path = source / "sample_640x426.jpg"
            psd_path = source / "sample_640x426.psd"
            jpg_path.write_bytes(b"jpg-without-exif")
            psd_path.write_bytes(b"psd-without-capture-metadata")
            jpg_mtime = dt.datetime(2024, 1, 2, 12, 0, 0).timestamp()
            psd_mtime = dt.datetime(2024, 1, 3, 12, 0, 0).timestamp()
            os.utime(jpg_path, (jpg_mtime, jpg_mtime))
            os.utime(psd_path, (psd_mtime, psd_mtime))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            month_items = browser_month_items(target, "2024-01")
            image_item = month_items[0]
            previous_item, next_item = adjacent_browser_items(target, image_item)
            month_nav = browser_month_navigation(target, image_item)
            psd_id = raw_sidecar_id_by_image_id(target, int(image_item["id"]))
            image_body = item_page_html(
                target,
                image_item,
                previous_item,
                next_item,
                month_nav,
            )

        self.assertEqual([item["stored_filename"] for item in month_items], ["sample_640x426.jpg"])
        self.assertIsNotNone(psd_id)
        self.assertIn(".PSD</a>", image_body)
        self.assertIn("/filter/filename%3Asample_640x426.psd/item/", image_body)

    def test_run_server_nef_sidecar_requires_same_source_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            (source / "jpg").mkdir(parents=True)
            (source / "raw").mkdir()
            (source / "jpg" / "DSC_0170.JPG").write_bytes(jpeg_with_exif_datetime("2019:03:03 12:00:00"))
            (source / "raw" / "DSC_0170.NEF").write_bytes(minimal_tiff_with_datetime("2019:03:03 12:00:00"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            month_items = browser_month_items(target, "2019-03")
            image_item = next(item for item in month_items if item["stored_filename"] == "DSC_0170.JPG")
            image_body = item_page_html(
                target,
                image_item,
                *adjacent_browser_items(target, image_item),
                browser_month_navigation(target, image_item),
            )

        self.assertEqual(
            [item["stored_filename"] for item in month_items],
            ["DSC_0170.JPG", "DSC_0170.NEF"],
        )
        self.assertNotIn("RAW-fil: DSC_0170.NEF", image_body)

    def test_server_file_path_by_id_stays_inside_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            init_database(target)
            relative_path = Path("2024/01/image.jpg")
            image_path = target / relative_path
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            file_id = register_target_file(target, relative_path)

            self.assertEqual(server_file_path_by_id(target, file_id), image_path.resolve())

    def test_server_file_path_by_id_rejects_missing_file_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)

            with self.assertRaisesRegex(FileNotFoundError, "Filen finnes ikke"):
                server_file_path_by_id(target, 999)

    def test_server_file_path_by_id_rejects_database_path_outside_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            init_database(target)
            relative_path = Path("2024/01/image.jpg")
            image_path = target / relative_path
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            file_id = register_target_file(target, relative_path)
            db.prepare_database(target)
            outside_path = root / "outside.jpg"
            outside_path.write_bytes(b"outside")
            conn = db.connect(target)
            try:
                conn.execute(
                    "UPDATE files SET target_path = ? WHERE id = ?",
                    (str(outside_path), file_id),
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(PermissionError, "Ugyldig filsti i databasen"):
                server_file_path_by_id(target, file_id)

    def test_run_server_motion_video_lookup_skips_non_motion_partner_images(self) -> None:
        class ExplodingConnection:
            def execute(self, *_args, **_kwargs):
                raise AssertionError("Non-motion image should not query for a motion video.")

        item = {
            "id": 1,
            "target_path": "2024/01/IMG_20240102.jpg",
            "target_path_key": "2024/01/img_20240102.jpg",
            "original_filename": "IMG_20240102.jpg",
            "stored_filename": "IMG_20240102.jpg",
        }

        self.assertIsNone(motion_video_for_image(Path("/unused"), item, conn=ExplodingConnection()))  # type: ignore[arg-type]

    def test_run_server_associated_files_skip_unrelated_image_names(self) -> None:
        item = {
            "id": 1,
            "target_path": "2024/01/IMG_20240102.jpg",
            "target_path_key": "2024/01/img_20240102.jpg",
            "original_filename": "IMG_20240102.jpg",
            "stored_filename": "IMG_20240102.jpg",
        }

        with (
            patch("bildebank.server_browser.motion_video_for_image", side_effect=AssertionError("motion lookup")),
            patch("bildebank.server_browser.raw_sidecar_for_image", return_value=None),
        ):
            self.assertEqual(
                server_browser.associated_files_for_item(Path("/unused"), item),
                (None, None),
            )

    def test_run_server_associated_files_check_motion_partner_images(self) -> None:
        item = {
            "id": 1,
            "target_path": "2024/01/PXL_20240102.MP.jpg",
            "target_path_key": "2024/01/pxl_20240102.mp.jpg",
            "original_filename": "PXL_20240102.MP.jpg",
            "stored_filename": "PXL_20240102.MP.jpg",
        }
        motion_item = {"id": 2, "stored_filename": "PXL_20240102.mp4"}

        with (
            patch("bildebank.server_browser.motion_video_for_image", return_value=motion_item) as motion_lookup,
            patch("bildebank.server_browser.raw_sidecar_for_image", return_value=None) as raw_lookup,
        ):
            self.assertEqual(
                server_browser.associated_files_for_item(Path("/unused"), item),
                (motion_item, None),
            )

        motion_lookup.assert_called_once()
        raw_lookup.assert_called_once()

    def test_run_server_filter_route_redirects_query_to_canonical_browser_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            handler = object.__new__(BildebankRequestHandler)
            response: dict[str, object] = {}
            handler.server = SimpleNamespace(target=target, face_enabled=True, openclip_enabled=True)  # type: ignore[attr-defined]

            def fake_redirect(location: str) -> None:
                response["location"] = location

            def fake_respond_html(content: str) -> None:
                response["html"] = content

            handler.redirect = fake_redirect  # type: ignore[method-assign]
            handler.respond_html = fake_respond_html  # type: ignore[method-assign]

            BildebankRequestHandler.respond_filter(handler, "q=after%3A2023-12-01+location%3Agps+size%3E2MB")  # type: ignore[arg-type]
            self.assertEqual(response["location"], "/filter/after%3A2023-12-01%20location%3Agps%20size%3E2MB")

            BildebankRequestHandler.respond_filter(handler, "q=location%3Aukjent-sted")  # type: ignore[arg-type]
            self.assertIn("Ukjent sted: ukjent-sted", str(response["html"]))

    def test_run_server_filter_item_page_uses_prefetched_source_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            filter_source = text_filter_browser_source("type:image")
            filter_item = source_item_by_id(target, filter_source, 1)
            self.assertIsNotNone(filter_item)
            response: dict[str, object] = {}
            handler = object.__new__(BildebankRequestHandler)
            handler.server = SimpleNamespace(  # type: ignore[attr-defined]
                target=target,
                config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=False)),
                face_enabled=False,
                openclip_enabled=False,
                hide_out_of_focus=False,
            )

            def fake_respond_html(content: str) -> None:
                response["html"] = content

            def fake_redirect(location: str) -> None:
                response["location"] = location

            handler.respond_html = fake_respond_html  # type: ignore[method-assign]
            handler.redirect = fake_redirect  # type: ignore[method-assign]

            with patch(
                "bildebank.server_browser.source_item_count",
                side_effect=AssertionError("Filter item page should use prefetched source count."),
            ):
                body = source_item_page_html(
                    target,
                    filter_source,
                    filter_item,
                    *adjacent_source_items(target, filter_source, filter_item),
                    source_month_navigation(target, filter_source, filter_item),
                    source_item_count_value=1,
                )
            BildebankRequestHandler.respond_browser_source(  # type: ignore[arg-type]
                handler,
                filter_source,
                None,
                "",
                item_not_found_message="Filen finnes ikke for dette filtersøket.",
                invalid_page_message="Ugyldig filtersøkside.",
            )

        self.assertIn("Filtersøk: type:image (1 treff)", body)
        self.assertNotIn("location", response)
        self.assertIn("Filtersøk: type:image (1 treff)", str(response["html"]))
        self.assertIn('href="/filter/type%3Aimage/year/2024"', str(response["html"]))

    def test_run_server_filter_page_documents_search_criteria(self) -> None:
        server = SimpleNamespace(face_enabled=True, openclip_enabled=True)
        body = filter_start_html(server)

        self.assertIn("<h2>Søkekriterier</h2>", body)
        self.assertIn("<code>month:12 day:24</code>", body)
        self.assertIn('<code>tag:"Ute av fokus"</code>', body)
        self.assertIn("<code>width>=3000 height>=2000", body)
        self.assertIn("<code>location:manual h3res>=9</code>", body)
        self.assertIn("h3res>=9</code>", body)

    def test_run_server_source_browser_reuses_source_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source_a = Path(tmp) / "source-a"
            source_b = Path(tmp) / "source-b"
            source_a.mkdir()
            source_b.mkdir()
            (source_a / "IMG_20230101.jpg").write_bytes(b"image-old")
            (source_a / "IMG_20240102.jpg").write_bytes(b"image-a")
            (source_a / "IMG_20250104.jpg").write_bytes(b"image-new")
            (source_b / "IMG_20240203.jpg").write_bytes(b"image-b")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source-a", "--quiet", str(source_a)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source-b", "--quiet", str(source_b)]), 0)
            conn = db.connect(target)
            try:
                source = db.find_source_by_name(conn, "source-a")
            finally:
                conn.close()
            self.assertIsNotNone(source)
            source_browser = imported_source_browser_source(source)
            with patch(
                "bildebank.server_browser.source_items",
                side_effect=AssertionError("Imported source should use SQL filter"),
            ):
                source_item = source_item_by_id(target, source_browser, 2)
                source_excludes_other_item = source_item_by_id(target, source_browser, 4) is None
                self.assertIsNotNone(source_item)
                source_adjacent = adjacent_source_items(target, source_browser, source_item)
                source_month_nav = source_month_navigation(target, source_browser, source_item)
                source_first_item = source_item_by_id(target, source_browser, 1)
                self.assertIsNotNone(source_first_item)
                source_first_adjacent = adjacent_source_items(target, source_browser, source_first_item)
                source_first_month_nav = source_month_navigation(target, source_browser, source_first_item)
                source_month = source_month_items(target, source_browser, "2024-01")
                source_first_month = source_month_items(target, source_browser, "2023-01")
            self.assertIsNotNone(source_item)
            self.assertTrue(source_has_sql_filter(source_browser))
            item_body = source_item_page_html(
                target,
                source_browser,
                source_item,
                *source_adjacent,
                source_month_nav,
            )
            with patch(
                "bildebank.server_browser.source_month_keys",
                side_effect=AssertionError("Item-rendering skal bruke eksisterende månedsnavigasjon."),
            ):
                first_item_body = source_item_page_html(
                    target,
                    source_browser,
                    source_first_item,
                    *source_first_adjacent,
                    source_first_month_nav,
                )
            year_body = source_year_months_page_html(target, source_browser, "2024")
            first_year_body = source_year_months_page_html(target, source_browser, "2023")
            month_body = source_month_page_html(target, source_browser, "2024-01", source_month)
            first_month_body = source_month_page_html(
                target,
                source_browser,
                "2023-01",
                source_first_month,
            )
            sources_body = sources_page_html(target)
            summaries = source_summary_rows(target)

        self.assertTrue(source_excludes_other_item)
        self.assertEqual(len(source_month), 1)
        self.assertEqual(len(summaries), 2)
        self.assertIn("Kilde: source-a", item_body)
        self.assertIn('href="/item/2">Åpne i alle bilder</a>', item_body)
        self.assertIn('href="/source/1/year/2024">2024</a>', item_body)
        self.assertIn('href="/source/1/year/2023" title="Forrige år" data-key-nav="previous-year">◀ Å</a>', item_body)
        self.assertIn('href="/source/1/year/2025" title="Neste år" data-key-nav="next-year">r ▶</a>', item_body)
        self.assertNotIn('href="/source/1/month/2023-01" title="Forrige år"', item_body)
        self.assertNotIn('href="/source/1/month/2025-01" title="Neste år"', item_body)
        self.assertIn(
            'href="/source/1" title="Forrige måned" data-key-nav="previous-month">◀ Mån</a>',
            first_item_body,
        )
        self.assertIn(
            'href="/source/1" title="Forrige år" data-key-nav="previous-year">◀ Å</a>',
            first_item_body,
        )
        self.assertNotIn("IMG_20240203", item_body)
        self.assertIn('href="/source/1">Kilde: source-a</a><span class="sep">/</span>2024</nav>', year_body)
        self.assertIn('href="/source/1/month/2024-01"', year_body)
        self.assertNotIn('href="/source/1/month/2024-02"', year_body)
        self.assertIn('href="/source/1/year/2023" title="Forrige år" data-key-nav="previous-year">◀ Å</a>', year_body)
        self.assertIn('href="/source/1/year/2025" title="Neste år" data-key-nav="next-year">r ▶</a>', year_body)
        self.assertIn('href="/source/1/month/2023-01" title="Forrige måned" data-key-nav="previous-month">◀ Mån</a>', year_body)
        self.assertIn('href="/source/1/month/2024-01" title="Neste måned" data-key-nav="next-month">ed ▶</a>', year_body)
        self.assertNotIn('href="/month/2023-01" title="Forrige måned"', year_body)
        self.assertNotIn('href="/month/2024-01" title="Neste måned"', year_body)
        self.assertIn('href="/source/1" title="Forrige år" data-key-nav="previous-year">◀ Å</a>', first_year_body)
        self.assertIn('href="/source/1" title="Forrige måned" data-key-nav="previous-month">◀ Mån</a>', first_year_body)
        self.assertIn('<section class="month-grid-server year-month-grid-server">', year_body)
        self.assertNotIn('<footer class="browser-footer">', year_body)
        self.assertIn('href="/source/1/item/2"', month_body)
        self.assertIn('href="/source/1/year/2024">2024</a>', month_body)
        self.assertIn('href="/source/1/year/2023" title="Forrige år" data-key-nav="previous-year">◀ Å</a>', month_body)
        self.assertIn('href="/source/1/year/2025" title="Neste år" data-key-nav="next-year">r ▶</a>', month_body)
        self.assertIn(
            'href="/source/1" title="Forrige måned" data-key-nav="previous-month">◀ Mån</a>',
            first_month_body,
        )
        self.assertIn(
            'href="/source/1" title="Forrige år" data-key-nav="previous-year">◀ Å</a>',
            first_month_body,
        )
        self.assertNotIn('href="/source/1/month/2023-01" title="Forrige år"', month_body)
        self.assertNotIn('href="/source/1/month/2025-01" title="Neste år"', month_body)
        self.assertIn('<span class="sep">/</span>Januar</nav>', month_body)
        self.assertNotIn('<footer class="browser-footer">', month_body)
        self.assertIn("<h1>Importerte mapper</h1>", sources_body)
        self.assertIn('href="/source/1">Vis bilder (3)</a>', sources_body)
        self.assertIn("source-a", sources_body)
        self.assertIn("source-b", sources_body)

    def test_imported_source_sql_filter_preserves_order_navigation_and_hidden_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source_a = Path(tmp) / "source-a"
            source_b = Path(tmp) / "source-b"
            source_a.mkdir()
            source_b.mkdir()
            (source_a / "A_20240301.jpg").write_bytes(b"march")
            (source_a / "B_20240105.jpg").write_bytes(b"january")
            (source_a / "C_20240202.jpg").write_bytes(b"february")
            (source_b / "D_20240101.jpg").write_bytes(b"other-source")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source-a", "--quiet", str(source_a)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source-b", "--quiet", str(source_b)]), 0)
            conn = db.connect(target)
            try:
                imported = db.find_source_by_name(conn, "source-a")
                self.assertIsNotNone(imported)
                rows = {
                    str(row["original_filename"]): int(row["id"])
                    for row in conn.execute("SELECT id, original_filename FROM files")
                }
                db.tag_file(
                    conn,
                    file_id=rows["C_20240202.jpg"],
                    tag_name=db.SYSTEM_TAG_OUT_OF_FOCUS,
                )
                conn.commit()
            finally:
                conn.close()

            source = imported_source_browser_source(imported)
            january_id = rows["B_20240105.jpg"]
            february_id = rows["C_20240202.jpg"]
            march_id = rows["A_20240301.jpg"]
            with patch(
                "bildebank.server_browser.source_items",
                side_effect=AssertionError("Imported source should not materialize source_items"),
            ):
                self.assertEqual(source_item_ids(target, source), [january_id, february_id, march_id])
                item = source_item_by_id(target, source, february_id)
                self.assertIsNotNone(item)
                previous_item, next_item = adjacent_source_items(target, source, item)
                month_nav = source_month_navigation(target, source, item)
                self.assertIsNone(source_item_by_id(target, source, rows["D_20240101.jpg"]))
                self.assertEqual(source_month_keys(target, source), ["2024-01", "2024-02", "2024-03"])
                self.assertEqual(
                    [int(row["id"]) for row in source_month_items(target, source, "2024-02")],
                    [february_id],
                )
                self.assertIsNone(source_item_by_id(target, source, february_id, hide_out_of_focus=True))
                self.assertEqual(
                    source_item_ids(target, source, hide_out_of_focus=True),
                    [january_id, march_id],
                )
                self.assertEqual(
                    source_month_keys(target, source, hide_out_of_focus=True),
                    ["2024-01", "2024-03"],
                )

        self.assertEqual(int(previous_item["id"]), january_id)
        self.assertEqual(int(next_item["id"]), march_id)
        self.assertEqual(
            month_nav,
            {
                "previous_year": None,
                "next_year": None,
                "previous_month": "2024-01",
                "next_month": "2024-03",
            },
        )

    def test_imported_source_item_requests_reuse_server_navigation_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source_dir = Path(tmp) / "source"
            source_dir.mkdir()
            (source_dir / "A_20240101.jpg").write_bytes(b"one")
            (source_dir / "B_20240201.jpg").write_bytes(b"two")
            (source_dir / "C_20240301.jpg").write_bytes(b"three")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source", "--quiet", str(source_dir)]), 0)
            conn = db.connect(target)
            try:
                imported = db.find_source_by_name(conn, "source")
            finally:
                conn.close()
            self.assertIsNotNone(imported)
            source = imported_source_browser_source(imported)

            server = object.__new__(BildebankServer)
            server.target = target
            server.config = AppConfig()
            server._browser_navigation_cache_version = 0
            server._browser_navigation_db_mtime_ns = db.db_path_for_target(target).stat().st_mtime_ns
            server._browser_navigation_face_db_mtime_ns = None
            server._browser_navigation_checked_at = time.monotonic()
            server._browser_item_ids = {}
            server._browser_month_keys = {}
            server._source_item_ids = {}
            server._source_month_keys = {}
            server._source_item_counts = {}

            class FakeHandler:
                def __init__(self) -> None:
                    self.server = server
                    self.body = ""
                    self.status = None

                def respond_html(self, body: str, *, status=HTTPStatus.OK) -> None:
                    self.body = body
                    self.status = status

                def respond_text(self, body: str, *, status=HTTPStatus.OK) -> None:
                    self.body = body
                    self.status = status

                def record_server_timing(self, name: str, start: float) -> None:
                    return

            handler = FakeHandler()
            with (
                patch("bildebank.server.source_item_ids", wraps=source_item_ids) as item_ids_mock,
                patch("bildebank.server.source_month_keys", wraps=source_month_keys) as month_keys_mock,
                patch("bildebank.server.source_item_by_id", wraps=source_item_by_id) as item_by_id_mock,
                patch("bildebank.server.adjacent_source_items", wraps=adjacent_source_items) as adjacent_mock,
            ):
                for file_id in (1, 2):
                    BildebankRequestHandler.respond_browser_source(  # type: ignore[arg-type]
                        handler,
                        source,
                        "item",
                        str(file_id),
                        item_not_found_message="Filen finnes ikke for denne kilden.",
                        invalid_page_message="Ugyldig kildeside.",
                    )
                    self.assertEqual(handler.status, HTTPStatus.OK)

            self.assertEqual(item_ids_mock.call_count, 1)
            self.assertEqual(month_keys_mock.call_count, 1)
            self.assertEqual(item_by_id_mock.call_count, 0)
            self.assertEqual(adjacent_mock.call_count, 0)

    def test_run_server_tags_page_can_create_rename_and_delete_user_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            create_data = b"name=Familie"

            class CreateHandler:
                headers = {"Content-Length": str(len(create_data))}
                rfile = BytesIO(create_data)
                server = SimpleNamespace(target=target, face_enabled=True, openclip_enabled=True)
                redirect_url: str | None = None

                def redirect(self, location: str) -> None:
                    self.redirect_url = location

                def respond_html(self, content: str, *, status: HTTPStatus) -> None:
                    raise AssertionError(f"{status}: {content}")

            create_handler = CreateHandler()
            BildebankRequestHandler.respond_create_tag(create_handler)  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                tag_row = conn.execute("SELECT id, name, kind FROM tags WHERE name_key = ?", ("familie",)).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(tag_row)
            tag_id = int(tag_row["id"])

            rename_data = f"tag_id={tag_id}&name=Familie+og+venner".encode("utf-8")

            class RenameHandler:
                headers = {"Content-Length": str(len(rename_data))}
                rfile = BytesIO(rename_data)
                server = SimpleNamespace(target=target, face_enabled=True, openclip_enabled=True)
                redirect_url: str | None = None

                def redirect(self, location: str) -> None:
                    self.redirect_url = location

                def respond_html(self, content: str, *, status: HTTPStatus) -> None:
                    raise AssertionError(f"{status}: {content}")

                def respond_text(self, content: str, *, status: HTTPStatus) -> None:
                    raise AssertionError(f"{status}: {content}")

            rename_handler = RenameHandler()
            BildebankRequestHandler.respond_rename_tag(rename_handler)  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                renamed_row = conn.execute("SELECT name, name_key FROM tags WHERE id = ?", (tag_id,)).fetchone()
            finally:
                conn.close()

            delete_data = f"tag_id={tag_id}".encode("utf-8")

            class DeleteHandler:
                headers = {"Content-Length": str(len(delete_data))}
                rfile = BytesIO(delete_data)
                server = SimpleNamespace(target=target, face_enabled=True, openclip_enabled=True)
                redirect_url: str | None = None

                def redirect(self, location: str) -> None:
                    self.redirect_url = location

                def respond_html(self, content: str, *, status: HTTPStatus) -> None:
                    raise AssertionError(f"{status}: {content}")

                def respond_text(self, content: str, *, status: HTTPStatus) -> None:
                    raise AssertionError(f"{status}: {content}")

            delete_handler = DeleteHandler()
            BildebankRequestHandler.respond_delete_tag(delete_handler)  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                deleted_row = conn.execute("SELECT id FROM tags WHERE id = ?", (tag_id,)).fetchone()
                system_row = conn.execute("SELECT id FROM tags WHERE name_key = ?", ("ute av fokus",)).fetchone()
            finally:
                conn.close()

        self.assertEqual(create_handler.redirect_url, "/tags")
        self.assertEqual(tag_row["name"], "Familie")
        self.assertEqual(tag_row["kind"], db.TAG_KIND_USER)
        self.assertEqual(rename_handler.redirect_url, "/tags")
        self.assertEqual(renamed_row["name"], "Familie og venner")
        self.assertEqual(renamed_row["name_key"], "familie og venner")
        self.assertEqual(delete_handler.redirect_url, "/tags")
        self.assertIsNone(deleted_row)
        self.assertIsNotNone(system_row)

    def test_run_server_tags_page_rejects_system_tag_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = db.connect(target)
            try:
                system_id = int(conn.execute("SELECT id FROM tags WHERE name_key = ?", ("ute av fokus",)).fetchone()["id"])
            finally:
                conn.close()
            data = f"tag_id={system_id}".encode("utf-8")
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, face_enabled=True, openclip_enabled=True)

                def respond_html(self, content: str, *, status: HTTPStatus) -> None:
                    response["content"] = content
                    response["status"] = status

                def respond_text(self, content: str, *, status: HTTPStatus) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_delete_tag(handler)  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("Systemtagger kan ikke slettes", str(response["content"]))

    def test_run_server_tag_definition_changes_report_target_lock_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = db.connect(target)
            try:
                tag_id = db.create_user_tag(conn, "Familie")
                conn.commit()
            finally:
                conn.close()
            (target / LOCK_FILENAME).write_text("command=tag-add\n", encoding="utf-8")

            class FakeHandler:
                server = SimpleNamespace(target=target, face_enabled=True, openclip_enabled=True)
                body = ""
                status: HTTPStatus | None = None

                def __init__(self, data: bytes) -> None:
                    self.headers = {"Content-Length": str(len(data))}
                    self.rfile = BytesIO(data)

                def respond_html(self, content: str, *, status: HTTPStatus) -> None:
                    self.body = content
                    self.status = status

                def respond_text(self, content: str, *, status: HTTPStatus) -> None:
                    self.body = content
                    self.status = status

                def redirect(self, location: str) -> None:
                    raise AssertionError(f"Uventet redirect: {location}")

            create_handler = FakeHandler(b"name=Ny")
            BildebankRequestHandler.respond_create_tag(create_handler)  # type: ignore[arg-type]
            rename_handler = FakeHandler(f"tag_id={tag_id}&name=Nytt+navn".encode("utf-8"))
            BildebankRequestHandler.respond_rename_tag(rename_handler)  # type: ignore[arg-type]
            delete_handler = FakeHandler(f"tag_id={tag_id}".encode("utf-8"))
            BildebankRequestHandler.respond_delete_tag(delete_handler)  # type: ignore[arg-type]

        for handler in (create_handler, rename_handler, delete_handler):
            self.assertEqual(handler.status, HTTPStatus.CONFLICT)
            self.assertIn("Bildesamlingen er låst", handler.body)

    def test_run_server_hide_out_of_focus_filters_browser_sources_but_not_tag_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-a")
            (source / "IMG_20240103.jpg").write_bytes(b"image-b")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source", "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                db.tag_file(conn, file_id=1, tag_name=db.SYSTEM_TAG_OUT_OF_FOCUS)
                imported = db.find_source_by_name(conn, "source")
                conn.commit()
            finally:
                conn.close()
            self.assertIsNotNone(imported)
            imported_source = imported_source_browser_source(imported)
            tag_source = tag_browser_source(db.SYSTEM_TAG_OUT_OF_FOCUS)

            hidden_browser_item = source_item_by_id(
                target,
                all_browser_source(),
                1,
                hide_out_of_focus=True,
            )
            visible_browser_item = source_item_by_id(
                target,
                all_browser_source(),
                2,
                hide_out_of_focus=True,
            )
            unfiltered_month_items = browser_month_items(target, "2024-01")
            filtered_month_items = browser_month_items(target, "2024-01", hide_out_of_focus=True)
            filtered_source_month_items = source_month_items(
                target,
                imported_source,
                "2024-01",
                hide_out_of_focus=True,
            )
            filtered_source_item = source_item_by_id(
                target,
                imported_source,
                1,
                hide_out_of_focus=True,
            )
            tag_item = source_item_by_id(target, tag_source, 1, hide_out_of_focus=True)
            tag_month_items = source_month_items(target, tag_source, "2024-01", hide_out_of_focus=True)
            self.assertIsNotNone(tag_item)
            tag_item_body = source_item_page_html(
                target,
                tag_source,
                tag_item,
                *adjacent_source_items(target, tag_source, tag_item, hide_out_of_focus=True),
                source_month_navigation(target, tag_source, tag_item, hide_out_of_focus=True),
                hide_out_of_focus=True,
            )

        self.assertIsNone(hidden_browser_item)
        self.assertIsNotNone(visible_browser_item)
        self.assertEqual([int(item["id"]) for item in unfiltered_month_items], [1, 2])
        self.assertEqual([int(item["id"]) for item in filtered_month_items], [2])
        self.assertEqual([int(item["id"]) for item in filtered_source_month_items], [2])
        self.assertIsNone(filtered_source_item)
        self.assertIsNotNone(tag_item)
        self.assertEqual([int(item["id"]) for item in tag_month_items], [1])
        self.assertIn('href="/">Åpne i alle bilder</a>', tag_item_body)
        self.assertNotIn('href="/item/1">Åpne i alle bilder</a>', tag_item_body)

    def test_source_item_page_uses_existing_connection_for_all_items_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-a")
            (source / "IMG_20240103.jpg").write_bytes(b"image-b")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source", "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                imported = db.find_source_by_name(conn, "source")
                self.assertIsNotNone(imported)
                imported_source = imported_source_browser_source(imported)
                item = source_item_by_id(target, imported_source, 1, conn=conn)
                self.assertIsNotNone(item)
                previous_item, next_item = adjacent_source_items(target, imported_source, item, conn=conn)
                month_nav = source_month_navigation(target, imported_source, item)

                server_browser.clear_sidecar_caches()
                server_browser.motion_video_file_ids(target)
                server_browser.raw_sidecar_file_ids(target)
                with (
                    patch(
                        "bildebank.server_browser.db.connect",
                        side_effect=AssertionError("opened nested connection"),
                    ),
                    patch(
                        "bildebank.server_browser.query_raw_sidecar_ids_by_image_id",
                        wraps=server_browser.query_raw_sidecar_ids_by_image_id,
                    ) as raw_sidecar_ids,
                ):
                    body = source_item_page_html(
                        target,
                        imported_source,
                        item,
                        previous_item,
                        next_item,
                        month_nav,
                        face_enabled=False,
                        openclip_enabled=False,
                        hide_out_of_focus=True,
                        conn=conn,
                    )
                    source_item_page_html(
                        target,
                        imported_source,
                        item,
                        previous_item,
                        next_item,
                        month_nav,
                        face_enabled=False,
                        openclip_enabled=False,
                        hide_out_of_focus=True,
                        conn=conn,
                    )
                    server_browser.hidden_sidecar_id_filter_sql(target, "1 = 1", (), conn=conn)
                    with (
                        patch(
                            "bildebank.server_browser.query_motion_video_file_ids",
                            side_effect=AssertionError("rescanned motion sidecars"),
                        ),
                        patch(
                            "bildebank.server_browser.raw_sidecar_groups",
                            side_effect=AssertionError("rescanned raw sidecars"),
                        ),
                    ):
                        server_browser.hidden_sidecar_id_filter_sql(target, "1 = 1", (), conn=conn)
            finally:
                conn.close()

        self.assertIn("Åpne i alle bilder", body)
        self.assertIn('href="/item/1">Åpne i alle bilder</a>', body)
        self.assertEqual(raw_sidecar_ids.call_count, 0)

    def test_run_server_out_of_focus_button_redirects_to_adjacent_visible_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-a")
            (source / "IMG_20240103.jpg").write_bytes(b"image-b")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source", "--quiet", str(source)]), 0)
            first_item = browser_item_by_id(target, 1, hide_out_of_focus=True)
            second_item = browser_item_by_id(target, 2, hide_out_of_focus=True)
            self.assertIsNotNone(first_item)
            self.assertIsNotNone(second_item)
            first_body = source_item_page_html(
                target,
                all_browser_source(),
                first_item,
                *adjacent_source_items(target, all_browser_source(), first_item, hide_out_of_focus=True),
                source_month_navigation(target, all_browser_source(), first_item, hide_out_of_focus=True),
                hide_out_of_focus=True,
            )
            second_body = source_item_page_html(
                target,
                all_browser_source(),
                second_item,
                *adjacent_source_items(target, all_browser_source(), second_item, hide_out_of_focus=True),
                source_month_navigation(target, all_browser_source(), second_item, hide_out_of_focus=True),
                hide_out_of_focus=True,
            )

        self.assertIn('data-tag-name="Ute av fokus" aria-pressed="false" data-tag-hide-redirect="/item/2"', first_body)
        self.assertIn('data-tag-name="Ute av fokus" aria-pressed="false" data-tag-hide-redirect="/item/1"', second_body)
        self.assertIn("tagHideRedirect", SERVER_JS)

    def test_run_server_month_navigation_tolerates_foreign_path_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20231201.jpg").write_bytes(b"image-one")
            (source / "IMG_20240102.jpg").write_bytes(b"image-two")
            (source / "IMG_20240203.jpg").write_bytes(b"image-three")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                rows = conn.execute("SELECT id, target_path FROM files ORDER BY id").fetchall()
                for file_id, target_path in rows:
                    path = Path(str(target_path))
                    conn.execute(
                        "UPDATE files SET target_path = ?, target_path_key = ? WHERE id = ?",
                        (path.as_posix(), f"c:\\annen-base\\{file_id}", file_id),
                    )
                conn.commit()
            finally:
                conn.close()

            item = browser_item_by_id(target, 2)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))
            month_body = month_page_html(target, "2024-01", browser_month_items(target, "2024-01"))

        self.assertIn("/month/2023-12", body)
        self.assertIn("/month/2024-02", body)
        self.assertIn("/month/2023-12", month_body)
        self.assertIn("/month/2024-02", month_body)

    def test_run_server_item_page_links_known_and_suggested_people(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Ola Nordmann')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(3, 'Per Manual')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(4, 'Siril')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(5, 'Viljar')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(6, 'Anne Begge')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 1, 'key-2', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(3, 1, 'key-3', 5, 6, 14, 24, 0.7, 'test', ?)
                    """,
                    (b"embedding-3",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(4, 1, 'key-4', 7, 8, 16, 26, 0.6, 'test', ?)
                    """,
                    (b"embedding-4",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(5, 1, 'key-5', 9, 10, 18, 28, 0.5, 'test', ?)
                    """,
                    (b"embedding-5",),
                )
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(4, 3)")
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(5, 4)")
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(6, 5)")
                face_conn.execute(
                    """
                    INSERT INTO face_suggestions(person_id, face_id, reference_face_id, similarity)
                    VALUES(2, 2, 1, 0.91)
                    """
                )
                face_conn.execute(
                    """
                    INSERT INTO face_suggestions(person_id, face_id, reference_face_id, similarity)
                    VALUES(3, 2, NULL, 0.81)
                    """
                )
                face_conn.execute("INSERT INTO person_files(person_id, file_id) VALUES(3, 1)")
                face_conn.execute("INSERT INTO person_files(person_id, file_id) VALUES(6, 1)")
                face_conn.commit()
            finally:
                face_conn.close()

            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            default_body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                person_reference_links_enabled=True,
            )
            manual_disabled_body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                manual_person_controls_enabled=False,
            )
            face_body = face_overlay_content_html(target, item, person_reference_links_enabled=True)
            disabled_body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                face_enabled=False,
                openclip_enabled=False,
            )

        self.assertNotIn('class="person-reference-link"', default_body)
        self.assertNotIn('href="/person/Ola%20Nordmann/confirmed/item/1"', default_body)
        self.assertNotIn("1 filer, 1 måneder", body)
        self.assertIn('class="person-link" href="/person/Kari/no-faces/item/1"', body)
        self.assertIn('data-person-name="Kari" title="Vis alle bilder med denne personen"', body)
        self.assertIn('Kari<span class="confirmed-badge" title="Bekreftet" aria-label="Bekreftet"> ✅</span></a>', body)
        self.assertIn('class="person-link" href="/person/Ola%20Nordmann/no-faces/item/1"', body)
        self.assertIn('data-person-name="Ola Nordmann" title="Vis alle bilder med denne personen">Ola Nordmann</a>', body)
        self.assertIn('class="person-link" href="/person/Per%20Manual/no-faces/item/1"', body)
        self.assertIn(
            'data-person-name="Per Manual" title="Vis alle bilder med denne personen">'
            'Per Manual<span class="confirmed-badge" title="Bekreftet" aria-label="Bekreftet"> ✅</span></a>',
            body,
        )
        self.assertIn('class="person-link" href="/person/Anne%20Begge/no-faces/item/1"', body)
        self.assertIn(
            'data-person-name="Anne Begge" title="Vis alle bilder med denne personen">'
            'Anne Begge<span class="confirmed-badge" title="Bekreftet" aria-label="Bekreftet"> ✅</span></a>',
            body,
        )
        self.assertIn("Bekreft ansikt", body)
        self.assertNotIn("Ubekreftet ansikter i bildet", body)
        tag_rail_start = body.index('<aside class="tag-rail"')
        tag_rail_end = body.index("</aside>", tag_rail_start)
        tag_rail_html = body[tag_rail_start:tag_rail_end]
        self.assertIn("Personer i bildet", tag_rail_html)
        self.assertIn("Referanseansikter:", tag_rail_html)
        self.assertIn('class="person-link" href="/person/Kari/no-faces/item/1"', tag_rail_html)
        self.assertIn('class="person-link" href="/person/Ola%20Nordmann/no-faces/item/1"', tag_rail_html)
        self.assertIn('class="person-link-with-reference"', tag_rail_html)
        self.assertIn('class="person-reference-link" href="/person/Ola%20Nordmann/confirmed/item/1"', tag_rail_html)
        self.assertIn(
            'Ola Nordmann</a><span class="person-reference-marker">(</span>'
            '<a class="person-reference-link" href="/person/Ola%20Nordmann/confirmed/item/1" '
            'title="Vis referansebildet som ga forslaget, face-id 1">r</a>'
            '<span class="person-reference-marker">)</span>',
            tag_rail_html,
        )
        self.assertIn('class="person-link" href="/person/Per%20Manual/no-faces/item/1"', tag_rail_html)
        self.assertNotIn('href="/person/Per%20Manual/confirmed/item/', tag_rail_html)
        self.assertIn('class="person-link" href="/person/Anne%20Begge/no-faces/item/1"', tag_rail_html)
        self.assertIn('class="person-link" href="/person/Siril/no-faces/item/1"', tag_rail_html)
        self.assertIn('class="person-link" href="/person/Viljar/no-faces/item/1"', tag_rail_html)
        self.assertIn("Bekreft ansikt", tag_rail_html)
        assumed_people_start = tag_rail_html.index("Personer i bildet")
        faces_button_start = tag_rail_html.index("Bekreft ansikt")
        confirmed_faces_start = tag_rail_html.index("Referanseansikt")
        self.assertNotIn("date-status-badge", tag_rail_html)
        assumed_people_html = tag_rail_html[assumed_people_start:faces_button_start]
        confirmed_faces_html = tag_rail_html[confirmed_faces_start:]
        self.assertIn("Kari", assumed_people_html)
        self.assertIn("Anne Begge", assumed_people_html)
        self.assertIn("Ola Nordmann", assumed_people_html)
        self.assertIn("Per Manual", assumed_people_html)
        self.assertIn("Siril", assumed_people_html)
        self.assertIn("Viljar", assumed_people_html)
        self.assertIn("Anne Begge", confirmed_faces_html)
        self.assertIn("Kari", confirmed_faces_html)
        self.assertIn("Siril", confirmed_faces_html)
        self.assertIn("Viljar", confirmed_faces_html)
        self.assertIn('data-unconfirm-face="1"', confirmed_faces_html)
        self.assertIn('data-unconfirm-face="3"', confirmed_faces_html)
        self.assertIn('data-unconfirm-face="4"', confirmed_faces_html)
        self.assertIn('data-unconfirm-face="5"', confirmed_faces_html)
        self.assertIn('data-unconfirm-person="Anne Begge"', confirmed_faces_html)
        self.assertIn('data-unconfirm-person="Kari"', confirmed_faces_html)
        self.assertIn('data-unconfirm-person="Siril"', confirmed_faces_html)
        self.assertIn('data-unconfirm-person="Viljar"', confirmed_faces_html)
        self.assertEqual(confirmed_faces_html.count(">fjern</button>"), 4)
        self.assertNotIn('class="person-link"', confirmed_faces_html)
        self.assertNotIn("Ola Nordmann", confirmed_faces_html)
        self.assertNotIn("Per Manual", confirmed_faces_html)
        self.assertLess(assumed_people_start, faces_button_start)
        self.assertLess(faces_button_start, confirmed_faces_start)
        self.assertNotIn("Person i bildet", body)
        self.assertIn("Velg person", tag_rail_html)
        self.assertIn("Legg til", tag_rail_html)
        self.assertIn("Ferdig", tag_rail_html)
        self.assertIn('data-open-manual-person-form', tag_rail_html)
        self.assertIn('data-manual-person-form', body)
        self.assertEqual(tag_rail_html.count('data-manual-person-remove'), 2)
        self.assertIn('data-person-name="Per Manual">×</button>', tag_rail_html)
        self.assertIn('data-person-name="Anne Begge">×</button>', tag_rail_html)
        self.assertNotIn('data-person-name="Kari">×</button>', tag_rail_html)
        self.assertNotIn('data-person-name="Siril">×</button>', tag_rail_html)
        self.assertNotIn('data-person-name="Viljar">×</button>', tag_rail_html)
        controls_start = body.index('<nav class="controls"')
        controls_end = body.index("</nav>", controls_start)
        controls_html = body[controls_start:controls_end]
        stage_start = body.index('<section class="stage">')
        stage_end = body.index("</section>", stage_start)
        stage_html = body[stage_start:stage_end]
        self.assertNotIn("data-manual-person-form", controls_html)
        self.assertNotIn("Person i bildet", controls_html)
        self.assertNotIn("data-manual-person-form", stage_html)
        self.assertNotIn("Person i bildet", manual_disabled_body)
        self.assertNotIn('data-manual-person-form', manual_disabled_body)
        self.assertNotIn('data-open-manual-person-form', manual_disabled_body)
        self.assertNotIn('data-manual-person-remove', manual_disabled_body)
        self.assertIn('data-faces-item="1"', body)
        self.assertIn('data-face-list', body)
        self.assertNotIn("Ny person", body)
        self.assertIn("width: fit-content;", SERVER_CSS)
        self.assertIn("justify-self: start;", SERVER_CSS)
        self.assertIn(".tag-rail .person-link {\n      flex: 1 1 max-content;", SERVER_CSS)
        self.assertIn(".tag-rail .person-link-with-reference", SERVER_CSS)
        self.assertIn(".person-reference-link", SERVER_CSS)
        self.assertIn(".person-reference-marker", SERVER_CSS)
        self.assertIn("      min-height: 26px;", SERVER_CSS)
        self.assertIn("      background: transparent;", SERVER_CSS)
        self.assertIn(".manual-person-remove-button {\n      display: none;", SERVER_CSS)
        self.assertIn(".people-section.manual-person-editing .manual-person-remove-button", SERVER_CSS)
        self.assertIn(".inline-link {\n      border: 0;", SERVER_CSS)
        self.assertIn(".tag-rail .faces-button {\n      flex: 0 0 auto;", SERVER_CSS)
        self.assertIn(".controls .delete-button { margin-left: auto; }", SERVER_CSS)
        self.assertIn("Ny person", face_body)
        self.assertIn("/api/face-person-create-and-add-face", SERVER_JS)
        self.assertIn("/api/item-faces?file_id=", SERVER_JS)
        self.assertIn("function ensureRailPersonLink", SERVER_JS)
        self.assertIn("ensureRailPersonLink(payload.person_name, payload.person_url, payload.confirmed);", SERVER_JS)
        self.assertIn("ensureRailPersonLink(payload.person_name, payload.person_url, true, true, fileId);", SERVER_JS)
        self.assertIn('const addButton = people.querySelector("[data-open-manual-person-form]");', SERVER_JS)
        self.assertIn("people.insertBefore(item, addButton);", SERVER_JS)
        self.assertIn('removeButton.dataset.manualPersonRemove = "";', SERVER_JS)
        self.assertIn("wireManualPersonRemoveButton(removeButton);", SERVER_JS)
        self.assertIn('const currentPerson = pathParts[0] === "person"', SERVER_JS)
        self.assertIn('window.location.href = `/item/${fileId}`;', SERVER_JS)
        self.assertIn('section.classList.add("manual-person-editing");', SERVER_JS)
        self.assertIn('section?.classList.remove("manual-person-editing");', SERVER_JS)
        self.assertNotIn("data-remove-person-file", SERVER_JS)
        self.assertNotIn("ensureTopPersonLink", SERVER_JS)
        self.assertIn('document.querySelector(".tag-rail")', SERVER_JS)
        self.assertNotIn('document.querySelector(".topline .people")', SERVER_JS)
        self.assertIn("Identifiser", face_body)
        self.assertIn("Forslag:", face_body)
        self.assertIn("Ola Nordmann <strong>0.910</strong>", face_body)
        self.assertIn('href="/person/Ola%20Nordmann/confirmed/item/1"', face_body)
        self.assertIn('title="Referanse face-id 1">referanse</a>', face_body)
        self.assertIn("Per Manual <strong>0.810</strong>", face_body)
        self.assertNotIn('href="/person/Per%20Manual/confirmed/item/', face_body)
        self.assertIn('data-face-id="2"', face_body)
        self.assertIn('data-person-name="Kari"', face_body)
        self.assertIn('data-person-name="Ola Nordmann"', face_body)
        self.assertNotIn('data-face-id="1"', body)
        self.assertNotIn('data-face-id="1"', face_body)
        self.assertNotIn('href="/people"', disabled_body)
        self.assertNotIn('href="/person/Kari"', disabled_body)
        self.assertNotIn("Personer i bildet", disabled_body)
        self.assertNotIn("Referanseansikt", disabled_body)
        self.assertNotIn('href="/search"', disabled_body)
        self.assertNotIn("Ansikter i bildet", disabled_body)
        self.assertNotIn("Ny person", disabled_body)

    def test_person_reference_links_disabled_uses_base_people_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Ola')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 1, 'key-2', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                face_conn.execute(
                    """
                    INSERT INTO face_suggestions(person_id, face_id, reference_face_id, similarity)
                    VALUES(2, 2, 1, 0.91)
                    """
                )
                face_conn.commit()
            finally:
                face_conn.close()

            real_connect = sqlite3.connect
            queries: list[str] = []

            class RecordingConnection:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    self._conn = real_connect(*args, **kwargs)

                @property
                def row_factory(self) -> object:
                    return self._conn.row_factory

                @row_factory.setter
                def row_factory(self, value: object) -> None:
                    self._conn.row_factory = value

                def execute(self, sql: str, *args: object, **kwargs: object) -> object:
                    queries.append(sql)
                    if "reference_face_id" in sql or "reference_faces" in sql:
                        raise AssertionError("disabled reference links should not query reference data")
                    return self._conn.execute(sql, *args, **kwargs)

                def executescript(self, sql: str) -> object:
                    return self._conn.executescript(sql)

                def commit(self) -> None:
                    self._conn.commit()

                def close(self) -> None:
                    self._conn.close()

            clear_face_caches()
            with patch("bildebank.server_faces.sqlite3.connect", RecordingConnection):
                people, confirmed = people_for_file(target, 1, person_reference_links_enabled=False)

        self.assertTrue(queries)
        self.assertEqual([person["name"] for person in people], ["Kari", "Ola"])
        self.assertEqual([person["name"] for person in confirmed], ["Kari"])

    def test_run_server_item_faces_api_returns_lazy_overlay_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.commit()
            finally:
                face_conn.close()

            response: dict[str, object] = {}
            handler = object.__new__(BildebankRequestHandler)
            handler.server = SimpleNamespace(target=target, face_enabled=True, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))

            def fake_respond_json(content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                response["content"] = content
                response["status"] = status

            handler.respond_json = fake_respond_json  # type: ignore[method-assign]
            handler.respond_item_faces("file_id=1")

        self.assertEqual(response["status"], HTTPStatus.OK)
        content = response["content"]
        assert isinstance(content, dict)
        self.assertIs(content["ok"], True)
        self.assertIn('data-face-detail="1"', str(content["html"]))
        self.assertIn('data-person-name="Kari"', str(content["html"]))
        self.assertIn("face-box", str(content["html"]))
        self.assertIn("Ingen forslag for dette ansiktet.", str(content["html"]))

    def test_run_server_api_adds_face_to_person(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 1, 0.91)")
                face_conn.commit()
            finally:
                face_conn.close()

            data = b"face_id=1&person_name=Kari"

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_add_face_to_person(handler)  # type: ignore[arg-type]

            self.assertEqual(
                {
                    "ok": True,
                    "person_name": "Kari",
                    "person_url": "/person/Kari/no-faces/item/1",
                    "confirmed": True,
                    "face_id": 1,
                    "added": True,
                },
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(
                    face_conn.execute("SELECT COUNT(*) FROM person_faces WHERE person_id = 1 AND face_id = 1").fetchone()[0],
                    1,
                )
                self.assertEqual(face_conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 0)
            finally:
                face_conn.close()

    def test_run_server_api_creates_person_and_adds_face(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.commit()
            finally:
                face_conn.close()

            data = json.dumps({"face_id": 1, "person_name": "Kari"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_create_person_and_add_face(handler)  # type: ignore[arg-type]

            self.assertEqual(
                {
                    "ok": True,
                    "person_name": "Kari",
                    "person_url": "/person/Kari/no-faces/item/1",
                    "confirmed": True,
                    "face_id": 1,
                    "added": True,
                },
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(face_conn.execute("SELECT COUNT(*) FROM persons WHERE name = 'Kari'").fetchone()[0], 1)
                self.assertEqual(
                    face_conn.execute("SELECT COUNT(*) FROM person_faces WHERE face_id = 1").fetchone()[0],
                    1,
                )
            finally:
                face_conn.close()

    def test_run_server_api_face_write_reports_target_lock_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=face-scan\n", encoding="utf-8")
            data = json.dumps({"face_id": 1, "person_name": "Kari"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)),
                )
                body: dict[str, object] | None = None
                status: HTTPStatus | None = None

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_create_person_and_add_face(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))

    def test_run_server_api_removes_face_from_person(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                face_conn.commit()
            finally:
                face_conn.close()

            data = json.dumps({"face_id": 1, "person_name": "Kari"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_remove_face_from_person(handler)  # type: ignore[arg-type]

            self.assertEqual(
                {
                    "ok": True,
                    "person_name": "Kari",
                    "person_url": "/person/Kari",
                    "face_id": 1,
                    "file_id": 1,
                    "redirect_url": "/item/1",
                    "removed": True,
                },
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(face_conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0], 0)
            finally:
                face_conn.close()

    def test_run_server_api_adds_and_removes_manual_person_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.commit()
            finally:
                face_conn.close()

            data = json.dumps({"file_id": 1, "person_name": "Kari"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None
                status = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_add_person_to_file(handler)  # type: ignore[arg-type]

            self.assertIsNone(handler.status)
            self.assertEqual(
                {
                    "ok": True,
                    "person_name": "Kari",
                    "person_url": "/person/Kari/no-faces/item/1",
                    "confirmed": True,
                    "file_id": 1,
                    "added": True,
                },
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(face_conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0], 1)
            finally:
                face_conn.close()

            handler = FakeHandler()
            handler.rfile = BytesIO(data)
            BildebankRequestHandler.respond_remove_person_from_file(handler)  # type: ignore[arg-type]

            self.assertIsNone(handler.status)
            self.assertEqual(
                {
                    "ok": True,
                    "person_name": "Kari",
                    "person_url": "/person/Kari",
                    "file_id": 1,
                    "removed": True,
                },
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(face_conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0], 0)
            finally:
                face_conn.close()

    def test_manual_person_file_requires_existing_person_and_active_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.commit()
            finally:
                face_conn.close()

            with self.assertRaisesRegex(ValueError, "Fant ikke person: Ola"):
                add_person_to_file(target, "Ola", 1)
            with self.assertRaisesRegex(ValueError, "Fant ikke aktiv fil-id 999"):
                add_person_to_file(target, "Kari", 999)

            result = add_person_to_file(target, "Kari", 1)
            self.assertTrue(result.added)
            removed = remove_person_from_file(target, "Kari", 1)
            self.assertTrue(removed.removed)

    def test_run_server_api_manual_person_file_is_disabled_when_faces_are_disabled(self) -> None:
        class FakeHandler:
            path = "/api/face-person-add-file"
            headers = {"X-CSRF-Token": "test-token"}
            rfile = BytesIO()
            server = SimpleNamespace(face_enabled=False, csrf_token="test-token")
            body: dict[str, object] | None = None
            status = None

            def respond_json(self, content: dict[str, object], *, status=None) -> None:
                self.body = content
                self.status = status

        handler = FakeHandler()
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertEqual(HTTPStatus.FORBIDDEN, handler.status)
        self.assertEqual({"ok": False, "error": "Ansiktsgjenkjenning er av."}, handler.body)

    def test_run_server_rejects_post_without_csrf_token(self) -> None:
        class FakeHandler:
            path = "/api/item-tag"
            headers: dict[str, str] = {}
            rfile = BytesIO()
            server = SimpleNamespace(csrf_token="test-token")
            body: dict[str, object] | None = None
            status = None

            def respond_json(self, content: dict[str, object], *, status=None) -> None:
                self.body = content
                self.status = status

        handler = FakeHandler()
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertEqual(HTTPStatus.FORBIDDEN, handler.status)
        self.assertEqual(
            {"ok": False, "error": "Ugyldig eller manglende CSRF-token."},
            handler.body,
        )

    def test_run_server_generates_one_csrf_token_at_startup(self) -> None:
        with (
            patch("bildebank.server.ThreadingHTTPServer.__init__", return_value=None),
            patch("bildebank.server.secrets.token_urlsafe", return_value="generated-token") as token_urlsafe,
        ):
            server = BildebankServer(("127.0.0.1", 0), Path("."), AppConfig())

        self.assertEqual(server.csrf_token, "generated-token")
        token_urlsafe.assert_called_once_with(32)

    def test_run_server_accepts_csrf_header_and_form_field(self) -> None:
        def validate(headers: dict[str, str], body: bytes = b"") -> tuple[bool, BytesIO]:
            class FakeHandler:
                rfile = BytesIO(body)
                server = SimpleNamespace(csrf_token="test-token")
                response = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.response = (content, status)

            handler = FakeHandler()
            handler.headers = headers
            accepted = BildebankRequestHandler.validate_csrf_request(handler)  # type: ignore[arg-type]
            return accepted, handler.rfile

        header_accepted, _ = validate({"X-CSRF-Token": "test-token"})
        form = b"csrf_token=test-token&name=Familie"
        form_accepted, restored_body = validate(
            {
                "Content-Length": str(len(form)),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            form,
        )

        self.assertTrue(header_accepted)
        self.assertTrue(form_accepted)
        self.assertEqual(restored_body.read(), form)

    def test_run_server_html_includes_csrf_meta_and_post_form_fields(self) -> None:
        content = (
            "<!doctype html><html><head><title>Test</title></head><body>"
            '<form method="post" action="/save"></form>'
            '<form action="/other" method="POST"><button>OK</button></form>'
            '<form method="get" action="/search"></form>'
            "</body></html>"
        )

        rendered = add_csrf_to_html(content, 'token<&"')

        self.assertIn(
            '<meta name="csrf-token" content="token&lt;&amp;&quot;">',
            rendered,
        )
        self.assertEqual(rendered.count('name="csrf_token"'), 2)
        self.assertIn(
            '<input type="hidden" name="csrf_token" value="token&lt;&amp;&quot;">',
            rendered,
        )
        self.assertIn('const csrfToken = document.querySelector', SERVER_JS)
        self.assertIn('headers.set("X-CSRF-Token", csrfToken);', SERVER_JS)
        self.assertNotIn('await fetch("/api/', SERVER_JS)

    def test_run_server_api_renames_person(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.commit()
            finally:
                face_conn.close()

            data = json.dumps({"old_name": "Kari", "new_name": "Kari Nordmann"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_rename_person(handler)  # type: ignore[arg-type]

            self.assertEqual(
                {
                    "ok": True,
                    "old_name": "Kari",
                    "new_name": "Kari Nordmann",
                    "person_url": "/person/Kari%20Nordmann/no-faces",
                },
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(face_conn.execute("SELECT COUNT(*) FROM persons WHERE name = 'Kari'").fetchone()[0], 0)
                self.assertEqual(face_conn.execute("SELECT COUNT(*) FROM persons WHERE name = 'Kari Nordmann'").fetchone()[0], 1)
            finally:
                face_conn.close()

    def test_run_server_api_rename_person_reports_existing_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Ola')")
                face_conn.commit()
            finally:
                face_conn.close()

            data = json.dumps({"old_name": "Kari", "new_name": "Ola"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None
                status = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_rename_person(handler)  # type: ignore[arg-type]

            self.assertEqual(HTTPStatus.BAD_REQUEST, handler.status)
            self.assertEqual({"ok": False, "error": "Person finnes allerede: Ola"}, handler.body)

    def test_run_server_api_rename_person_validates_name_and_accepts_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.commit()
            finally:
                face_conn.close()

            def call_rename(payload: dict[str, object]):
                data = json.dumps(payload).encode("utf-8")

                class FakeHandler:
                    headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/json",
                    }
                    rfile = BytesIO(data)
                    server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                    body: dict[str, object] | None = None
                    status = None

                    def respond_json(self, content: dict[str, object], *, status=None) -> None:
                        self.body = content
                        self.status = status

                handler = FakeHandler()
                BildebankRequestHandler.respond_rename_person(handler)  # type: ignore[arg-type]
                return handler

            missing_name = call_rename({"old_name": "Kari", "new_name": " "})
            same_name = call_rename({"old_name": " Kari ", "new_name": "Kari"})

            self.assertEqual(HTTPStatus.BAD_REQUEST, missing_name.status)
            self.assertEqual({"ok": False, "error": "Nytt personnavn mangler."}, missing_name.body)
            self.assertEqual(
                {"ok": True, "old_name": "Kari", "new_name": "Kari", "person_url": "/person/Kari/no-faces"},
                same_name.body,
            )

    def test_run_server_api_rename_person_is_disabled_when_faces_are_disabled(self) -> None:
        class FakeHandler:
            path = "/api/face-person-rename"
            headers = {"X-CSRF-Token": "test-token"}
            rfile = BytesIO()
            server = SimpleNamespace(face_enabled=False, csrf_token="test-token")
            body: dict[str, object] | None = None
            status = None

            def respond_json(self, content: dict[str, object], *, status=None) -> None:
                self.body = content
                self.status = status

        handler = FakeHandler()
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertEqual(HTTPStatus.FORBIDDEN, handler.status)
        self.assertEqual({"ok": False, "error": "Ansiktsgjenkjenning er av."}, handler.body)

    def test_run_server_api_delete_person_removes_person_links_and_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 2, 'key-2', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                face_conn.execute("INSERT INTO person_files(person_id, file_id) VALUES(1, 1)")
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 2, 0.91)")
                face_conn.commit()
            finally:
                face_conn.close()

            data = json.dumps({"person_name": "Kari"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)))
                body: dict[str, object] | None = None
                status = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_delete_person(handler)  # type: ignore[arg-type]

            face_conn = connect_face_db(target)
            try:
                person_count = face_conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
                link_count = face_conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0]
                manual_link_count = face_conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0]
                suggestion_count = face_conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0]
                face_count = face_conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
            finally:
                face_conn.close()

        self.assertIsNone(handler.status)
        self.assertEqual(
            {"ok": True, "person_name": "Kari", "removed_faces": 1, "removed_files": 1, "removed_suggestions": 1},
            handler.body,
        )
        self.assertEqual(0, person_count)
        self.assertEqual(0, link_count)
        self.assertEqual(0, manual_link_count)
        self.assertEqual(0, suggestion_count)
        self.assertEqual(2, face_count)

    def test_run_server_api_delete_person_is_disabled_when_faces_are_disabled(self) -> None:
        class FakeHandler:
            path = "/api/face-person-delete"
            headers = {"X-CSRF-Token": "test-token"}
            rfile = BytesIO()
            server = SimpleNamespace(face_enabled=False, csrf_token="test-token")
            body: dict[str, object] | None = None
            status = None

            def respond_json(self, content: dict[str, object], *, status=None) -> None:
                self.body = content
                self.status = status

        handler = FakeHandler()
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertEqual(HTTPStatus.FORBIDDEN, handler.status)
        self.assertEqual({"ok": False, "error": "Ansiktsgjenkjenning er av."}, handler.body)

    def test_run_server_person_browser_filters_and_marks_faces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))
            (source / "IMG_20240203.png").write_bytes(minimal_png(101, 80))
            (source / "IMG_20250104.png").write_bytes(minimal_png(102, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Ola')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 2, 'key-2', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(3, 3, 'key-3', 5, 6, 14, 24, 0.7, 'test', ?)
                    """,
                    (b"embedding-3",),
                )
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 2, 0.91)")
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(2, 3)")
                face_conn.execute("INSERT INTO person_files(person_id, file_id) VALUES(1, 1)")
                face_conn.commit()
            finally:
                face_conn.close()
            conn = db.connect(target)
            try:
                db.rotate_file_view(conn, 1, "right")
                conn.commit()
            finally:
                conn.close()
            cached_image_dimensions(target, target / "2024" / "01" / "IMG_20240102.png")
            cached_image_orientation(target, target / "2024" / "01" / "IMG_20240102.png")

            item = person_item_by_id(target, "Kari", 1)
            self.assertIsNotNone(item)
            with (
                patch("bildebank.server_faces.image_dimensions", side_effect=AssertionError("cached dimensions should be used")),
                patch("bildebank.server_faces.image_orientation", side_effect=AssertionError("cached orientation should be used")),
            ):
                body = person_item_page_html(target, "Kari", item, *adjacent_person_items(target, "Kari", item), person_month_navigation(target, "Kari", item))
            plain_source = person_browser_source("Kari", include_suggestions=True, show_faces=False)
            plain_item = source_item_by_id(target, plain_source, 1)
            self.assertIsNotNone(plain_item)
            plain_body = source_item_page_html(
                target,
                plain_source,
                plain_item,
                *adjacent_source_items(target, plain_source, plain_item),
                source_month_navigation(target, plain_source, plain_item),
            )
            suggested_item = source_item_by_id(target, plain_source, 2)
            self.assertIsNotNone(suggested_item)
            suggested_body = source_item_page_html(
                target,
                plain_source,
                suggested_item,
                *adjacent_source_items(target, plain_source, suggested_item),
                source_month_navigation(target, plain_source, suggested_item),
            )
            plain_years_body = source_years_page_html(target, plain_source, face_config=FaceRecognitionConfig(enabled=True))
            confirmed_plain_source = person_browser_source("Kari", include_suggestions=False, show_faces=False)
            confirmed_years_body = source_years_page_html(
                target,
                confirmed_plain_source,
                face_config=FaceRecognitionConfig(enabled=True),
            )
            marked_source = person_browser_source("Kari", include_suggestions=True, show_faces=True)
            marked_years_body = source_years_page_html(target, marked_source, face_config=FaceRecognitionConfig(enabled=True))
            month_body = person_month_page_html(target, "Kari", "2024-02", person_month_items(target, "Kari", "2024-02"))

        self.assertIn(">Kari<", body)
        self.assertIn("/person/Kari/month/2024-02", body)
        self.assertIn('href="/item/1">Åpne i alle bilder</a>', body)
        controls_start = body.index('<nav class="controls"')
        controls_end = body.index("</nav>", controls_start)
        controls_html = body[controls_start:controls_end]
        self.assertIn(
            '<a class="nav-button" href="/person/Kari/no-faces/item/1" title="Skjul ansiktsmarkering"><span class="face-toggle-icon face-toggle-icon-active">👤</span></a>',
            controls_html,
        )
        self.assertIn(".face-toggle-icon-active", SERVER_CSS)
        self.assertIn('<a class="nav-button" href="/person/Kari/confirmed/item/1">[✓] Ta med forslag</a>', controls_html)
        self.assertNotIn("Bare bekreftede", body)
        self.assertNotIn("Med forslag", body)
        self.assertNotIn("Uten ansiktsmarkering", body)
        self.assertNotIn("Med ansiktsmarkering", body)
        self.assertIn("person-face-box", body)
        self.assertIn("person-face-layer", body)
        self.assertIn("bekreftet face-id 1", body)
        self.assertIn('<span class="person-face-label">face-id 1</span>', body)
        self.assertIn('<div class="person-media" style="transform: rotate(90deg); --quarter-turn-width:', body)
        self.assertIn('<a href="/file/1" target="_blank"><img src="/file/1"', body)
        self.assertIn(".person-face-layer", SERVER_CSS)
        self.assertIn("function fitPersonFaceLayer", SERVER_JS)
        self.assertIn("function observePersonFaceLayers", SERVER_JS)
        self.assertIn("ResizeObserver", SERVER_JS)
        self.assertIn('data-view-rotation="90">', body)
        self.assertNotIn("IMG_20250104", body)
        self.assertIn("Kari - uten ansiktsmarkering", plain_body)
        self.assertIn('href="/person/Kari/no-faces">Kari</a>', plain_body)
        self.assertIn('href="/person/Kari/no-faces/year/2024">2024</a>', plain_body)
        self.assertIn("Kari - uten ansiktsmarkering", plain_years_body)
        self.assertIn('href="/person/Kari/no-faces/year/2024"', plain_years_body)
        self.assertIn(">2024</div>", plain_years_body)
        self.assertIn(">2 måneder, 2 bilder</div>", plain_years_body)
        self.assertIn('href="/person/Kari/no-faces/month/2024-01" title="Neste måned" data-key-nav="next-month">ed ▶</a>', plain_years_body)
        self.assertIn("Kari - bekreftet - uten ansiktsmarkering", confirmed_years_body)
        self.assertIn('href="/person/Kari/confirmed/no-faces/year/2024"', confirmed_years_body)
        self.assertIn(">1 måned, 1 bilde</div>", confirmed_years_body)
        self.assertNotIn("/person/Kari/confirmed/no-faces/year/2025", confirmed_years_body)
        self.assertIn('href="/person/Kari/year/2024"', marked_years_body)
        self.assertNotIn('Kari - uten ansiktsmarkering</a><span class="sep">/</span>', plain_body)
        plain_tag_rail_start = plain_body.index('<aside class="tag-rail"')
        plain_tag_rail_end = plain_body.index("</aside>", plain_tag_rail_start)
        plain_tag_rail_html = plain_body[plain_tag_rail_start:plain_tag_rail_end]
        self.assertIn("Personer i bildet", plain_tag_rail_html)
        self.assertIn('data-open-manual-person-form', plain_tag_rail_html)
        self.assertIn('data-manual-person-form', plain_tag_rail_html)
        self.assertIn("Velg person", plain_tag_rail_html)
        self.assertIn("Legg til", plain_tag_rail_html)
        self.assertIn("Ferdig", plain_tag_rail_html)
        self.assertIn('data-manual-person-remove data-file-id="1" data-person-name="Kari">×</button>', plain_tag_rail_html)
        plain_controls_start = plain_body.index('<nav class="controls"')
        plain_controls_end = plain_body.index("</nav>", plain_controls_start)
        plain_controls_html = plain_body[plain_controls_start:plain_controls_end]
        self.assertNotIn("Fjern manuell person-i-bilde", plain_controls_html)
        self.assertNotIn("data-remove-person-file", plain_controls_html)
        self.assertIn(
            '<a class="nav-button" href="/person/Kari/item/1" title="Vis ansiktsmarkering"><span class="face-toggle-icon">👤</span></a>',
            plain_controls_html,
        )
        self.assertNotIn("face-toggle-icon-active", plain_controls_html)
        self.assertIn('<a class="nav-button" href="/person/Kari/confirmed/no-faces/item/1">[✓] Ta med forslag</a>', plain_controls_html)
        self.assertIn('href="/item/1">Åpne i alle bilder</a>', plain_body)
        self.assertNotIn("Med ansiktsmarkering", plain_body)
        self.assertNotIn('<div class="person-face-box"', plain_body)
        self.assertNotIn('<span class="person-face-label">face-id 1</span>', plain_body)
        self.assertIn('<img src="/display/1"', plain_body)
        suggested_controls_start = suggested_body.index('<nav class="controls"')
        suggested_controls_end = suggested_body.index("</nav>", suggested_controls_start)
        suggested_controls_html = suggested_body[suggested_controls_start:suggested_controls_end]
        self.assertIn('<a class="nav-button" href="/person/Kari/confirmed/no-faces">[✓] Ta med forslag</a>', suggested_controls_html)
        self.assertNotIn('/person/Kari/confirmed/no-faces/item/2">[✓] Ta med forslag</a>', suggested_controls_html)
        month_controls_start = month_body.index('<nav class="controls"')
        month_controls_end = month_body.index("</nav>", month_controls_start)
        month_controls_html = month_body[month_controls_start:month_controls_end]
        self.assertIn('<a class="nav-button" href="/person/Kari/confirmed">[✓] Ta med forslag</a>', month_controls_html)
        self.assertIn("/person/Kari/item/2", month_body)
        self.assertNotIn("/person/Kari/item/3", month_body)

    def test_run_server_person_browser_uses_sql_filter_for_item_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))
            (source / "IMG_20240203.png").write_bytes(minimal_png(101, 80))
            (source / "IMG_20240304.png").write_bytes(minimal_png(102, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 2, 'key-2', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 2, 0.91)")
                face_conn.execute("INSERT INTO person_files(person_id, file_id) VALUES(1, 3)")
                face_conn.commit()
            finally:
                face_conn.close()

            all_source = person_browser_source("Kari", include_suggestions=True, show_faces=False)
            confirmed_source = person_browser_source("Kari", include_suggestions=False, show_faces=False)

            with patch("bildebank.server_browser.source_items", side_effect=AssertionError("person browser should use SQL filter")):
                all_item = source_item_by_id(target, all_source, 2)
                self.assertIsNotNone(all_item)
                previous_item, next_item = adjacent_source_items(target, all_source, all_item)
                all_month_nav = source_month_navigation(target, all_source, all_item)
                manual_month_items = source_month_items(target, all_source, "2024-03")
                confirmed_item = source_item_by_id(target, confirmed_source, 1)
                self.assertIsNotNone(confirmed_item)

                self.assertTrue(source_has_sql_filter(all_source))
                self.assertTrue(source_has_sql_filter(confirmed_source))
                self.assertEqual(3, source_item_count(target, all_source))
                self.assertEqual(1, source_item_count(target, confirmed_source))
                self.assertEqual(1, int(previous_item["id"]))
                self.assertEqual(3, int(next_item["id"]))
                self.assertEqual({"previous_year": None, "next_year": None, "previous_month": "2024-01", "next_month": "2024-03"}, all_month_nav)
                self.assertEqual([3], [int(item["id"]) for item in manual_month_items])
                self.assertIsNone(source_item_by_id(target, confirmed_source, 2))
                self.assertIsNone(source_item_by_id(target, confirmed_source, 3))

                class FakeServer:
                    def __init__(self, target: Path) -> None:
                        self.target = target
                        self.config = AppConfig(face_recognition=FaceRecognitionConfig(enabled=True), openclip=OpenClipConfig(enabled=False))
                        self.face_enabled = True
                        self.openclip_enabled = False
                        self.hide_out_of_focus = False
                        self._source_item_ids = {}
                        self._source_first_day_item_ids = {}
                        self._browser_navigation_cache_version = 0

                    def source_month_keys(self, source, *, hide_out_of_focus: bool = False):
                        return source_month_keys(
                            self.target,
                            source,
                            self.config.face_recognition,
                            hide_out_of_focus=hide_out_of_focus,
                        )

                    def source_item_order(self, source, *, hide_out_of_focus: bool = False):
                        return BildebankServer.source_item_order(
                            self,
                            source,
                            hide_out_of_focus=hide_out_of_focus,
                        )

                    def browser_navigation_cache_version(self) -> int:
                        return 0

                    def source_first_day_item_id(self, source, day_key: str, *, hide_out_of_focus: bool = False):
                        return BildebankServer.source_first_day_item_id(
                            self,
                            source,
                            day_key,
                            hide_out_of_focus=hide_out_of_focus,
                        )

                class FakeHandler:
                    server = FakeServer(target)
                    body = ""
                    status = None

                    def respond_html(self, body: str, *, status=HTTPStatus.OK) -> None:
                        self.body = body
                        self.status = status

                    def respond_text(self, body: str, *, status=HTTPStatus.OK) -> None:
                        self.body = body
                        self.status = status

                    def redirect(self, location: str) -> None:
                        self.body = location
                        self.status = HTTPStatus.FOUND

                    def record_server_timing(self, name: str, start: float) -> None:
                        return

                handler = FakeHandler()
                with (
                    patch("bildebank.server.source_item_ids", wraps=source_item_ids) as item_ids_mock,
                    patch("bildebank.server.source_item_by_id", wraps=source_item_by_id) as item_by_id_mock,
                    patch("bildebank.server.adjacent_source_items", wraps=adjacent_source_items) as adjacent_mock,
                    patch("bildebank.server_browser.first_source_day_item", side_effect=AssertionError("handler should pass cached first day item")),
                ):
                    BildebankRequestHandler.respond_browser_source(  # type: ignore[arg-type]
                        handler,
                        all_source,
                        "item",
                        "2",
                        face_config=handler.server.config.face_recognition,
                        item_not_found_message="Filen finnes ikke for denne personen.",
                        invalid_page_message="Ugyldig personside.",
                    )

                self.assertEqual(HTTPStatus.OK, handler.status)
                self.assertIn('data-browser-item-id="2"', handler.body)
                self.assertEqual(1, item_ids_mock.call_count)
                self.assertEqual(0, item_by_id_mock.call_count)
                self.assertEqual(0, adjacent_mock.call_count)

    def test_run_server_people_page_links_confirmed_and_suggested_person_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))
            (source / "IMG_20240203.png").write_bytes(minimal_png(101, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            main_conn = db.connect(target)
            try:
                file_rows = {
                    int(row["id"]): row
                    for row in main_conn.execute("SELECT id, target_path, target_path_key, sha256 FROM files")
                }
            finally:
                main_conn.close()
            face_conn = connect_face_db(target)
            try:
                face_conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, ?, ?, 'ok', 2)
                    """,
                    (file_rows[1]["target_path"], file_rows[1]["target_path_key"], file_rows[1]["sha256"]),
                )
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 2, 'key-2', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(3, 1, 'key-3', 5, 6, 14, 24, 0.7, 'test', ?)
                    """,
                    (b"embedding-3",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(4, 999, 'stale-confirmed', 5, 6, 14, 24, 0.7, 'test', ?)
                    """,
                    (b"embedding-4",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(5, 1000, 'stale-suggested', 5, 6, 14, 24, 0.7, 'test', ?)
                    """,
                    (b"embedding-5",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(6, 2, 'key-2-extra', 7, 8, 16, 26, 0.7, 'test', ?)
                    """,
                    (b"embedding-6",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(7, 1, 'key-1-suggested', 7, 8, 16, 26, 0.7, 'test', ?)
                    """,
                    (b"embedding-7",),
                )
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 3)")
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 4)")
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 2, 0.91)")
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 5, 0.92)")
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 6, 0.90)")
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 7, 0.89)")
                face_conn.commit()
            finally:
                face_conn.close()

            cached_person_file_ids.cache_clear()
            first_file_ids = person_file_ids(target, "Kari", include_suggestions=True)
            cache_after_first = cached_person_file_ids.cache_info()
            second_file_ids = person_file_ids(target, "Kari", include_suggestions=True)
            cache_after_second = cached_person_file_ids.cache_info()
            body = people_page_html(target)
            confirmed_items = person_items(target, "Kari", include_suggestions=False)
            all_items = person_items(target, "Kari", include_suggestions=True)
            confirmed_source = person_browser_source("Kari", include_suggestions=False)
            confirmed_item = source_item_by_id(target, confirmed_source, 1)
            self.assertIsNotNone(confirmed_item)
            with patch("bildebank.server_browser.source_item_by_id", wraps=source_item_by_id) as item_by_id_mock:
                confirmed_body = source_item_page_html(
                    target,
                    confirmed_source,
                    confirmed_item,
                    *adjacent_source_items(target, confirmed_source, confirmed_item),
                    source_month_navigation(target, confirmed_source, confirmed_item),
                )
            self.assertEqual(item_by_id_mock.call_count, 0)
            confirmed_month_body = source_month_page_html(
                target,
                confirmed_source,
                "2024-01",
                source_month_items(target, confirmed_source, "2024-01"),
            )

        self.assertEqual(first_file_ids, [1, 2])
        self.assertEqual(second_file_ids, [1, 2])
        self.assertEqual(cache_after_first.misses, 1)
        self.assertEqual(cache_after_second.hits, cache_after_first.hits + 1)
        self.assertIn("<div><strong>Antall bilder i databasen</strong><span>2</span></div>", body)
        self.assertIn("<div><strong>Scannet av face-scan</strong><span>1</span></div>", body)
        self.assertIn("<div><strong>Ikke scannet av face-scan</strong><span>1</span></div>", body)
        self.assertIn("<div><strong>Ansikter funnet</strong><span>5</span></div>", body)
        self.assertIn("<div><strong>Ansikter med forslag</strong><span>3</span></div>", body)
        self.assertNotIn('href="/person/Kari/confirmed/no-faces"', body)
        self.assertIn('href="/person/Kari/no-faces"', body)
        self.assertIn('data-open-person-rename', body)
        self.assertIn('data-person-name="Kari"', body)
        self.assertIn("endre navn", body)
        self.assertIn('data-delete-person-name="Kari"', body)
        self.assertIn("slett person", body)
        self.assertIn('id="personRenameDialog"', body)
        self.assertNotIn("Bekreftede bilder (1)", body)
        self.assertIn("Bekreftede og forslag (2)", body)
        self.assertNotIn("forslag:", body)
        self.assertIn("NB: 2 bekreftede ansikter i samme bilde", body)
        self.assertIn("NB: 2 bekreftede ansikter for Kari i dette bildet", confirmed_body)
        confirmed_controls_start = confirmed_body.index('<nav class="controls"')
        confirmed_controls_end = confirmed_body.index("</nav>", confirmed_controls_start)
        confirmed_controls_html = confirmed_body[confirmed_controls_start:confirmed_controls_end]
        self.assertIn('<a class="nav-button" href="/person/Kari/item/1">[&nbsp;&nbsp;&nbsp;] Ta med forslag</a>', confirmed_controls_html)
        self.assertNotIn("Bare bekreftede", confirmed_body)
        self.assertNotIn("Med forslag", confirmed_body)
        confirmed_month_controls_start = confirmed_month_body.index('<nav class="controls"')
        confirmed_month_controls_end = confirmed_month_body.index("</nav>", confirmed_month_controls_start)
        confirmed_month_controls_html = confirmed_month_body[confirmed_month_controls_start:confirmed_month_controls_end]
        self.assertIn('<a class="nav-button" href="/person/Kari">[&nbsp;&nbsp;&nbsp;] Ta med forslag</a>', confirmed_month_controls_html)
        confirmed_tag_rail_start = confirmed_body.index('<aside class="tag-rail"')
        confirmed_tag_rail_end = confirmed_body.index("</aside>", confirmed_tag_rail_start)
        confirmed_tag_rail_html = confirmed_body[confirmed_tag_rail_start:confirmed_tag_rail_end]
        self.assertIn('class="person-link"', confirmed_tag_rail_html)
        self.assertIn('data-person-name="Kari"', confirmed_tag_rail_html)
        self.assertIn('data-unconfirm-face="1"', confirmed_tag_rail_html)
        self.assertIn('data-unconfirm-face="3"', confirmed_tag_rail_html)
        self.assertIn('data-unconfirm-person="Kari"', confirmed_tag_rail_html)
        self.assertIn(">fjern</button>", confirmed_tag_rail_html)
        self.assertIn('title="Avkreft face-id 1"', confirmed_tag_rail_html)
        confirmed_controls_start = confirmed_body.index('<nav class="controls"')
        confirmed_controls_end = confirmed_body.index("</nav>", confirmed_controls_start)
        confirmed_controls_html = confirmed_body[confirmed_controls_start:confirmed_controls_end]
        self.assertNotIn("Avbekreft face-id", confirmed_controls_html)
        self.assertNotIn('data-unconfirm-face', confirmed_controls_html)
        self.assertIn("/api/face-person-remove-face", SERVER_JS)
        self.assertIn("/api/face-person-rename", SERVER_JS)
        self.assertIn("/api/face-person-delete", SERVER_JS)
        self.assertEqual([int(item["id"]) for item in confirmed_items], [1])
        self.assertEqual([int(item["id"]) for item in all_items], [1, 2])

    def test_run_server_confirm_messages_use_javascript_newlines(self) -> None:
        self.assertIn("Tilsvarer:\\n${command}", SERVER_JS)
        self.assertNotIn("Tilsvarer:\\\\n${command}", SERVER_JS)
        self.assertIn("Flytte til deleted/?\\n\\n${path}", SERVER_JS)
        self.assertNotIn("Flytte til deleted/?\\\\n\\\\n${path}", SERVER_JS)
        self.assertIn("window.location.href = payload.redirect_url", SERVER_JS)

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
