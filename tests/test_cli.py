from __future__ import annotations

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
from bildebank.config import AppConfig, FaceRecognitionConfig, OpenClipConfig
from bildebank import db, server_browser
from bildebank.db import DB_FILENAME, init_database
from bildebank.face import (
    connect_face_db,
)
from bildebank.geo import h3_cells_for_point
from bildebank.media import ImageDimensions, sha256_file
from bildebank.media_cache import cached_image_dimensions, cached_image_orientation
from bildebank.openclip import connect_openclip_db, embedding_blob
from bildebank.server_assets import SERVER_JS
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
    geo_stats_page_html,
    index_html,
    item_page_html,
    markdown_doc_page_html,
    month_page_html,
    search_html,
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
    adjacent_source_items,
    browser_item_by_id,
    browser_month_items,
    browser_month_navigation,
    browser_year_cards,
    browser_year_month_cards,
    image_info_content_html,
    item_media_html,
    motion_video_for_image,
    raw_sidecar_id_by_image_id,
    source_item_by_id,
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
    source_has_sql_filter,
    tag_browser_source,
)
from bildebank.server_filter import parse_text_filter, text_filter_browser_source
from bildebank.server_search import (
    DEFAULT_SEARCH_LIMIT,
    ServerSearchStats,
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
