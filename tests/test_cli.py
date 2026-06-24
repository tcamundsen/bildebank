from __future__ import annotations

import json
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import unittest
import datetime as dt
import os
import sys
import time
import warnings
import uuid
import zipfile
from collections.abc import Iterable
from contextlib import redirect_stderr, redirect_stdout
from http import HTTPStatus
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.cli import (
    build_parser,
    lan_share_urls,
    main,
    print_image_search_progress,
    run_server_command,
    wsl_path_from_windows_path,
)
from bildebank.config import AppConfig, BrowserConfig, BrowserHotkeyConfig, FaceRecognitionConfig, OpenClipConfig, load_config
from bildebank import db, server_browser
from bildebank.db import DB_FILENAME, init_database
from bildebank.exiftool import managed_exiftool_path, resolve_exiftool_path
from bildebank.export_person import (
    PersonExportInterrupted,
    export_person,
    finalize_export_directory,
    validate_windows_folder_name,
)
from bildebank.face import (
    add_person_to_file,
    apply_face_schema,
    connect_face_db,
    create_person,
    face_box_percent,
    face_db_path,
    insightface_import_error_message,
    normalize_insightface_model_layout,
    read_image,
    remove_person_from_file,
    remove_insightface_model_zip,
)
from bildebank.geo import h3_cells_for_manual_cell, h3_cells_for_point
from bildebank.html_export import render_html
from bildebank.importer import safe_copy
from bildebank.media import ImageDimensions, sha256_file
from bildebank.media_cache import cached_image_dimensions, cached_image_orientation
from bildebank.openclip import ImageSearchResult, connect_openclip_db, embedding_blob, openclip_db_path, resolve_torch_device
from bildebank.program_state import PROGRAM_DB_FILENAME, ensure_schema, known_targets, record_target
from bildebank.server_actions import remove_file_from_browser, undelete_file_from_browser
from bildebank.server_assets import SERVER_ASSET_VERSION, SERVER_CSS, SERVER_JS
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
    sources_page_html,
    tags_page_html,
    year_months_page_html,
    years_page_html,
)
from bildebank.server_app import MaintenanceStatus, maintenance_statuses, thumbnail_maintenance_status
from bildebank.server_browser import (
    adjacent_browser_items,
    adjacent_person_items,
    adjacent_source_items,
    browser_item_by_id,
    browser_month_items,
    browser_month_navigation,
    browser_year_cards,
    browser_year_month_cards,
    date_source_text,
    image_info_content_html,
    item_media_html,
    month_key_for_item,
    motion_video_for_image,
    out_of_focus_file_ids,
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
    cached_face_box_media_metadata,
    cached_person_file_ids,
    face_overlay_content_html,
    person_file_ids,
    person_items,
    update_face_box_media_metadata,
)
from bildebank.server_geo import geo_component_pixel_coordinates
from bildebank.server_search import (
    DEFAULT_SEARCH_LIMIT,
    OpenClipSearchCache,
    ServerSearchStats,
    load_search_embedding_cache,
    search_server_images,
)
from bildebank.target_lock import LOCK_FILENAME, TargetLockError
from bildebank.thumbnails import (
    existing_thumbnail_url,
    thumbnail_absolute_path,
    thumbnail_is_current,
    thumbnail_relative_path,
)
from tests.test_media import (
    jpeg_with_xmp_date,
    jpeg_with_exif_datetime,
    jpeg_with_exif_camera,
    minimal_avi_with_creation_date,
    minimal_avi_with_idit_outside_info,
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
        self.assertIn("Bildebank 0.3.0", stdout)
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
        self.assertIn("Bildebank 0.3.0", stdout)
        self.assertNotIn("--target", stdout)
        self.assertIn("Vanlige kommandoer:", stdout)
        self.assertIn("kom i gang\n   create", stdout)
        self.assertIn("import", stdout)
        self.assertIn("run-server", stdout)
        self.assertIn("kontrollere importen\n   errors", stdout)
        self.assertIn("rydde trygt\n   remove", stdout)
        self.assertIn("metadata og steder\n   refresh-metadata", stdout)
        self.assertIn("ansikter\n   face-status", stdout)
        self.assertIn("bildesøk\n   image-scan", stdout)
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

    def test_config_help_preserves_description_examples(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["config", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("Slå valgfrie funksjoner på eller av i bildebank-config.toml.\nEksempel:\n\n", stdout)
        self.assertIn(" bildebank config face_recognition enable\n", stdout)
        self.assertIn(" bildebank config image_search disable\n", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_face_reset_help_documents_reset_levels(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["face-reset", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("--all", stdout)
        self.assertIn("--keep-scan", stdout)
        self.assertIn("Standard hvis ingen", stdout)
        self.assertIn("nivåvalg er brukt", stdout)
        self.assertIn("krever alltid", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_removed_group_commands_are_unavailable(self) -> None:
        for command in ("face-group", "face-person-add-group", "make-face-browser"):
            with self.subTest(command=command):
                stdout_buffer = StringIO()
                stderr_buffer = StringIO()
                with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
                    main([command, "-h"])

                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(stdout_buffer.getvalue(), "")
                stderr = stderr_buffer.getvalue()
                self.assertIn("invalid choice", stderr)
                self.assertIn(command, stderr)

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

    def test_face_suggest_help_documents_threshold_and_model(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["face-suggest", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("--threshold", stdout)
        self.assertIn("--model", stdout)
        self.assertNotIn("--no-browser", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_face_person_rename_help_documents_names(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["face-person-rename", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("gammelt_navn", stdout)
        self.assertIn("nytt_navn", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_image_search_help_documents_limit_and_no_browser(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["image-search", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("usage: bildebank image-search [valg] søk", stdout)
        self.assertIn("--limit", stdout)
        self.assertIn("--no-browser", stdout)
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
        with patch("bildebank.cli.local_lan_ipv4_addresses", return_value=["192.168.86.11"]):
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
            patch("bildebank.cli.load_config", return_value=config),
            patch("bildebank.cli.lan_share_urls", return_value=["http://192.168.86.11:8765/"]),
            patch("bildebank.cli.run_local_server") as run_local_server,
            redirect_stdout(StringIO()) as stdout,
        ):
            run_local_server.side_effect = lambda *args, **kwargs: kwargs["ready"]("http://0.0.0.0:8765/")
            result = run_server_command(
                Path("C:/Users/Tom/Bilder"),
                host="0.0.0.0",
                port=8765,
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
            patch("bildebank.cli.load_config", return_value=config),
            patch("bildebank.cli.lan_share_urls", return_value=["http://192.168.86.11:8766/"]),
            patch("bildebank.cli.run_local_server") as run_local_server,
            patch("bildebank.cli.webbrowser.open") as open_browser,
            redirect_stdout(StringIO()),
        ):
            run_local_server.side_effect = lambda *args, **kwargs: kwargs["ready"]("http://0.0.0.0:8766/")
            result = run_server_command(
                Path("C:/Users/Tom/Bilder"),
                host="0.0.0.0",
                port=8766,
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

    def test_exiftool_install_help_documents_force(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["exiftool-install", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("usage: bildebank exiftool-install [valg]", stdout)
        self.assertIn("--force", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_openclip_database_schema_is_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            conn = connect_openclip_db(target)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                conn.close()

            self.assertEqual(openclip_db_path(target), target / ".bilder-openclip.sqlite3")
            self.assertIn("image_embeddings", tables)
            self.assertIn("image_search_runs", tables)
            self.assertIn("image_search_results", tables)

    def test_openclip_device_validation(self) -> None:
        self.assertEqual(resolve_torch_device("cpu"), "cpu")
        with self.assertRaises(ValueError):
            resolve_torch_device("gpu")

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

    def test_openclip_search_cache_can_preload_model(self) -> None:
        config = OpenClipConfig(enabled=True)
        cache = OpenClipSearchCache(AppConfig(openclip=config))
        with patch("bildebank.server_search.load_text_model", return_value=("model", "tokenizer")) as load_model:
            cache.preload_model()

        load_model.assert_called_once_with(config)
        self.assertTrue(cache.loaded)

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

    def test_openclip_database_rejects_absolute_target_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            absolute_image = target / "2024" / "01" / "IMG_20240102.jpg"
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
                        str(absolute_image),
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

            with self.assertRaisesRegex(ValueError, "OpenCLIP-databasen har absolutt target_path"):
                connect_openclip_db(target).close()

    def test_run_server_app_status_page_shows_config_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "program"
            target = Path(tmp) / "target"
            model_root = root / ".bildebank-insightface"
            (model_root / "models" / "antelopev2").mkdir(parents=True)
            (model_root / "models" / "antelopev2" / "scrfd_10g_bnkps.onnx").write_bytes(b"model")
            (model_root / "models" / "buffalo_l").mkdir(parents=True)
            (model_root / "models" / "buffalo_l" / "det_10g.onnx").write_bytes(b"model")
            config = AppConfig(
                face_recognition=FaceRecognitionConfig(enabled=True, model_root=model_root, model_name="buffalo_l"),
                openclip=OpenClipConfig(enabled=True, model_name="Test-Model", pretrained="test-weights", device="cpu"),
                browser=BrowserConfig(
                    hide_out_of_focus=True,
                    manual_person_controls_enabled=False,
                    hotkey_hints_enabled=True,
                    hotkeys={"1": BrowserHotkeyConfig(action="person", person_name="Kari")},
                ),
            )

            with (
                patch("bildebank.server_app.module_available", side_effect=lambda name: name == "open_clip"),
                patch("bildebank.server_app.maintenance_statuses", side_effect=AssertionError("maintenance count")),
                patch("bildebank.server_app.active_thumbnail_candidates", side_effect=AssertionError("thumbnail count")),
            ):
                body = app_status_page_html(target, config, scroll_y=312)

        self.assertIn("<h2>Innstillinger</h2>", body)
        self.assertIn('data-settings-scroll-restore="312"', body)
        self.assertIn("function setSettingsScrollField", SERVER_JS)
        self.assertIn('input[name="scroll_y"]', SERVER_JS)
        self.assertIn("window.scrollTo", SERVER_JS)
        self.assertIn("Bildebank-versjon", body)
        self.assertIn("Bildesamling", body)
        self.assertIn("Vedlikehold", body)
        self.assertLess(body.index("Vedlikehold"), body.index("Bildesamling"))
        self.assertIn('href="/help/face-scan.md"', body)
        self.assertIn('href="/help/geo-scan.md"', body)
        self.assertIn('href="/help/image-scan.md"', body)
        self.assertIn('href="/help/make-thumbnails.md"', body)
        self.assertIn("face-scan", body)
        self.assertIn("geo-scan", body)
        self.assertIn("image-scan", body)
        self.assertEqual(body.count("Oppdaterer..."), 3)
        self.assertIn('data-maintenance-name="face-scan"', body)
        self.assertIn('data-maintenance-name="geo-scan"', body)
        self.assertIn('data-maintenance-name="image-scan"', body)
        self.assertIn("<dt>Scannet</dt><dd data-maintenance-current>-</dd>", body)
        self.assertIn("<dt>Mangler</dt><dd data-maintenance-missing>-</dd>", body)
        self.assertIn("<dt>Totalt</dt><dd data-maintenance-total>-</dd>", body)
        self.assertIn("thumbnails", body)
        self.assertIn("Ikke telt ennå", body)
        self.assertIn("Tell thumbnails", body)
        self.assertIn("data-count-thumbnails", body)
        self.assertIn("<dt>Oppdatert</dt><dd data-thumbnail-current>-</dd>", body)
        self.assertIn("<dt>Mangler</dt><dd data-thumbnail-missing>-</dd>", body)
        self.assertIn("<dt>Totalt</dt><dd data-thumbnail-total>-</dd>", body)
        self.assertIn("/api/maintenance/statuses", SERVER_JS)
        self.assertIn("data-maintenance-name", SERVER_JS)
        self.assertIn('window.addEventListener("load", scheduleMaintenanceStatusesLoad', SERVER_JS)
        self.assertIn("setTimeout(loadMaintenanceStatuses, 0)", SERVER_JS)
        self.assertEqual(SERVER_ASSET_VERSION, "43")
        self.assertIn("bildebank ${payload.name}", SERVER_JS)
        self.assertIn("bilder trenger ${payload.name}", SERVER_JS)
        self.assertIn("/api/maintenance/thumbnails", SERVER_JS)
        self.assertIn("Teller thumbnails...", SERVER_JS)
        self.assertIn("bildebank make-thumbnails", SERVER_JS)
        self.assertIn(".maintenance-row", SERVER_CSS)
        self.assertIn("grid-template-columns: minmax(110px, 150px)", SERVER_CSS)
        settings_start = body.index('<dl class="info-list app-status">')
        settings_html = body[settings_start : body.index("</dl>", settings_start)]
        settings_labels = re.findall(r"<dt>(.*?)</dt>", settings_html)
        self.assertEqual(
            settings_labels,
            [
                "Bildesamling",
                "Bildebank-versjon",
                "Skjul bilder tagget “Ute av fokus”",
                "Hurtigtaster 1-5",
                "InsightFace aktivert",
                "InsightFace-modell",
                "InsightFace installert",
                "GUI for manuell bekrefting av person i bildet",
                "OpenCLIP tilgjengelig",
                "Bildesøk aktivert",
                "OpenCLIP-modell",
                "OpenCLIP-pretrained",
                "OpenCLIP-device",
            ],
        )
        self.assertEqual(
            settings_labels[4:8],
            [
                "InsightFace aktivert",
                "InsightFace-modell",
                "InsightFace installert",
                "GUI for manuell bekrefting av person i bildet",
            ],
        )
        self.assertIn('action="/settings/hide-out-of-focus"', body)
        self.assertNotIn('action="/settings/manual-h3-cell"', body)
        self.assertNotIn("Aktiv manuell H3-celle", body)
        self.assertIn("Hurtigtaster 1-5", body)
        self.assertIn('action="/settings/hotkey"', body)
        self.assertIn('action="/settings/hotkey-hints"', body)
        self.assertIn('href="/settings/h3-cells"', body)
        self.assertIn("Rediger H3-celler", body)
        self.assertIn("Aktiver hurtigtaster 1-5: På", body)
        self.assertIn('<input type="hidden" name="key" value="1">', body)
        self.assertLess(
            body.index('action="/settings/hotkey-hints"'),
            body.index('<input type="hidden" name="key" value="1">'),
        )
        self.assertIn("data-hotkey-action", body)
        self.assertIn('data-hotkey-fields="h3"', body)
        self.assertIn('data-hotkey-fields="manual_date"', body)
        self.assertIn('data-hotkey-fields="person"', body)
        self.assertIn('data-hotkey-fields="tag"', body)
        self.assertIn('data-hotkey-fields=""', body)
        self.assertIn(".hotkey-empty-fields", SERVER_CSS)
        self.assertIn("function updateHotkeyForm", SERVER_JS)
        self.assertIn('<option value="person" selected>Legg til person</option>', body)
        self.assertIn('<option value="tag">Sett tagg</option>', body)
        self.assertIn('<option value="Kari" selected>Kari</option>', body)
        self.assertIn('<span class="app-toggle-status">På</span>', body)
        self.assertIn(str(target), body)
        self.assertIn("InsightFace aktivert", body)
        self.assertIn('action="/settings/face-config"', body)
        self.assertIn('name="enabled" value="true" checked', body)
        self.assertIn("InsightFace-modell", body)
        self.assertIn('action="/settings/face-model"', body)
        self.assertIn("GUI for manuell bekrefting av person", body)
        self.assertIn('action="/settings/manual-person-controls"', body)
        self.assertIn('<span class="app-toggle-status">Av</span>', body)

        self.assertIn('<option value="antelopev2">antelopev2</option>', body)
        self.assertIn('<option value="buffalo_l" selected>buffalo_l</option>', body)
        self.assertIn("må installeres for å scanne ansikter i nye bilder.", body)
        self.assertNotIn("app-toggle-submit", body)
        self.assertIn("<dd>ja</dd>", body)
        self.assertIn("InsightFace installert", body)
        self.assertNotIn('href="/date-source/filename">Dato fra filnavn</a>', body)
        self.assertNotIn('href="/date-source/mtime">Dato fra mtime</a>', body)
        self.assertIn("OpenCLIP tilgjengelig", body)
        self.assertIn("Bildesøk aktivert", body)
        self.assertIn('action="/settings/image-search"', body)
        self.assertIn("Test-Model", body)
        self.assertIn("test-weights", body)
        self.assertIn("cpu", body)

    def test_run_server_settings_maintenance_status_counts_scan_needs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            insert_test_file(target, "2024/01/current.png", sha256="sha-current", gps_scanned=True)
            insert_test_file(target, "2024/01/missing.png", sha256="sha-missing")
            insert_test_file(target, "2024/01/deleted.png", sha256="sha-deleted", deleted=True)
            config = AppConfig(openclip=OpenClipConfig(model_name="Test-Model", pretrained="test-weights"))

            statuses = {status.name: status for status in maintenance_statuses(target, config)}

        self.assertEqual(statuses["face-scan"].total, 2)
        self.assertEqual(statuses["face-scan"].scanned, 0)
        self.assertEqual(statuses["face-scan"].missing, 2)
        self.assertEqual(statuses["geo-scan"].total, 2)
        self.assertEqual(statuses["geo-scan"].scanned, 1)
        self.assertEqual(statuses["geo-scan"].missing, 1)
        self.assertEqual(statuses["image-scan"].total, 2)
        self.assertEqual(statuses["image-scan"].scanned, 0)
        self.assertEqual(statuses["image-scan"].missing, 2)

    def test_run_server_maintenance_statuses_api_counts_scan_needs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            insert_test_file(target, "2024/01/current.png", sha256="sha-current", gps_scanned=True)
            insert_test_file(target, "2024/01/missing.png", sha256="sha-missing")
            insert_test_file(target, "2024/01/deleted.png", sha256="sha-deleted", deleted=True)
            config = AppConfig(openclip=OpenClipConfig(model_name="Test-Model", pretrained="test-weights"))
            response: dict[str, object] = {}
            handler = object.__new__(BildebankRequestHandler)
            handler.server = SimpleNamespace(target=target, config=config)

            def fake_respond_json(content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                response["content"] = content
                response["status"] = status

            handler.respond_json = fake_respond_json  # type: ignore[method-assign]
            handler.respond_maintenance_statuses()

        self.assertEqual(response["status"], HTTPStatus.OK)
        self.assertEqual(
            response["content"],
            {
                "ok": True,
                "statuses": [
                    {
                        "name": "face-scan",
                        "total": 2,
                        "current": 0,
                        "missing": 2,
                        "help_path": "/help/face-scan.md",
                    },
                    {
                        "name": "geo-scan",
                        "total": 2,
                        "current": 1,
                        "missing": 1,
                        "help_path": "/help/geo-scan.md",
                    },
                    {
                        "name": "image-scan",
                        "total": 2,
                        "current": 0,
                        "missing": 2,
                        "help_path": "/help/image-scan.md",
                    },
                ],
            },
        )

    def test_run_server_thumbnail_maintenance_api_counts_missing_current_and_active_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            insert_test_file(target, "2024/01/current.jpg", sha256="sha-current")
            insert_test_file(target, "2024/01/missing.png", sha256="sha-missing")
            insert_test_file(target, "2024/01/deleted.jpg", sha256="sha-deleted", deleted=True)
            insert_test_file(target, "2024/01/not-image.txt", sha256="sha-text")
            current_original = target / "2024/01/current.jpg"
            thumb_path = thumbnail_absolute_path(target, Path("2024/01/current.jpg"))
            thumb_path.parent.mkdir(parents=True)
            thumb_path.write_bytes(b"thumb")
            os.utime(thumb_path, ns=(current_original.stat().st_mtime_ns, current_original.stat().st_mtime_ns))

            status = thumbnail_maintenance_status(target)
            response: dict[str, object] = {}
            handler = object.__new__(BildebankRequestHandler)
            handler.server = SimpleNamespace(target=target)

            def fake_respond_json(content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                response["content"] = content
                response["status"] = status

            handler.respond_json = fake_respond_json  # type: ignore[method-assign]
            handler.respond_thumbnail_maintenance()

        self.assertEqual(status.total, 2)
        self.assertEqual(status.scanned, 1)
        self.assertEqual(status.missing, 1)
        self.assertEqual(response["status"], HTTPStatus.OK)
        self.assertEqual(
            response["content"],
            {"ok": True, "name": "thumbnails", "total": 2, "current": 1, "missing": 1},
        )

    def test_run_server_settings_image_scan_status_counts_current_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            current_id = insert_test_file(target, "2024/01/current.png", sha256="sha-current")
            stale_id = insert_test_file(target, "2024/01/stale.png", sha256="sha-stale")
            config = AppConfig(openclip=OpenClipConfig(model_name="Test-Model", pretrained="test-weights"))
            conn = connect_openclip_db(target)
            try:
                conn.executemany(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            current_id,
                            "2024/01/current.png",
                            "2024/01/current.png",
                            "sha-current",
                            "Test-Model",
                            "test-weights",
                            embedding_blob([1.0, 0.0]),
                        ),
                        (
                            stale_id,
                            "2024/01/stale.png",
                            "2024/01/stale.png",
                            "old-sha",
                            "Test-Model",
                            "test-weights",
                            embedding_blob([0.0, 1.0]),
                        ),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            statuses = {status.name: status for status in maintenance_statuses(target, config)}

        self.assertEqual(statuses["image-scan"].total, 2)
        self.assertEqual(statuses["image-scan"].scanned, 1)
        self.assertEqual(statuses["image-scan"].missing, 1)

    def test_run_server_settings_maintenance_status_shows_updated_when_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            file_id = insert_test_file(target, "2024/01/current.png", sha256="sha-current", gps_scanned=True)
            config = AppConfig(openclip=OpenClipConfig(model_name="Test-Model", pretrained="test-weights"))
            conn = connect_openclip_db(target)
            try:
                conn.execute(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        "2024/01/current.png",
                        "2024/01/current.png",
                        "sha-current",
                        "Test-Model",
                        "test-weights",
                        embedding_blob([1.0, 0.0]),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            with patch("bildebank.server_app.face_scan_maintenance_status") as face_status:
                face_status.return_value = MaintenanceStatus("face-scan", 1, 1, 0, "/help/face-scan.md")
                statuses = {status.name: status for status in maintenance_statuses(target, config)}

        self.assertEqual(statuses["geo-scan"].missing, 0)
        self.assertEqual(statuses["image-scan"].missing, 0)

    def test_run_server_settings_hotkey_tag_select_keeps_missing_selected_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            body = app_status_page_html(
                target,
                AppConfig(browser=BrowserConfig(hotkeys={"1": BrowserHotkeyConfig(action="tag", tag_name="Familie")})),
            )

        self.assertIn('<option value="tag" selected>Sett tagg</option>', body)
        self.assertIn('<option value="Familie" selected>Familie</option>', body)

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

    def test_run_server_face_config_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=false&enabled=true"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=False)))
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                BildebankRequestHandler.respond_set_face_config(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertTrue(config.face_recognition.enabled)
        self.assertTrue(handler.server.config.face_recognition.enabled)
        self.assertEqual(handler.location, "/settings")

    def test_run_server_settings_post_redirect_preserves_scroll_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=false&enabled=true&scroll_y=312"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=False)))
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                BildebankRequestHandler.respond_set_face_config(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.location, "/settings?scroll=312")

    def test_run_server_image_search_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=false&enabled=true"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig(openclip=OpenClipConfig(enabled=False)))
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                BildebankRequestHandler.respond_set_image_search(handler)  # type: ignore[arg-type]

            config = load_config(root)
            config_text = (root / "bildebank-config.toml").read_text(encoding="utf-8")

        self.assertTrue(config.openclip.enabled)
        self.assertTrue(handler.server.config.openclip.enabled)
        self.assertIsInstance(handler.server.search_cache, OpenClipSearchCache)
        self.assertEqual(handler.location, "/settings")
        self.assertIn("[image_search]", config_text)
        self.assertIn("enabled = true", config_text)
        self.assertNotIn("[openclip]", config_text)

    def test_run_server_hide_out_of_focus_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=false&enabled=true"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                BildebankRequestHandler.respond_set_hide_out_of_focus(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertTrue(config.browser.hide_out_of_focus)
        self.assertTrue(handler.server.config.browser.hide_out_of_focus)
        self.assertEqual(handler.location, "/settings")

    def test_run_server_manual_person_controls_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=false"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    config=AppConfig(browser=BrowserConfig(manual_person_controls_enabled=True))
                )
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                BildebankRequestHandler.respond_set_manual_person_controls(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertFalse(config.browser.manual_person_controls_enabled)
        self.assertFalse(handler.server.config.browser.manual_person_controls_enabled)
        self.assertEqual(handler.location, "/settings")

    def test_run_server_hotkey_hints_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=true"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                BildebankRequestHandler.respond_set_hotkey_hints(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertTrue(config.browser.hotkey_hints_enabled)
        self.assertTrue(handler.server.config.browser.hotkey_hints_enabled)
        self.assertEqual(handler.location, "/settings")

    def test_load_config_reads_tag_hotkey(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bildebank-config.toml").write_text(
                '[browser.hotkeys]\n"1" = { action = "tag", tag_name = "Familie" }\n',
                encoding="utf-8",
            )

            config = load_config(root)

        self.assertEqual(config.browser.hotkeys["1"], BrowserHotkeyConfig(action="tag", tag_name="Familie"))

    def test_run_server_hotkey_post_updates_config(self) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = (
                f"key=1&action=h3&h3_cell={h3_cell}&person_name=&mode=exact&"
                "date=&uncertainty=1m&date_from=&date_to=&note="
            ).encode("utf-8")

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

                def respond_text(self, content: str, *, status: HTTPStatus) -> None:
                    raise AssertionError(f"{status}: {content}")

            handler = FakeHandler()
            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                BildebankRequestHandler.respond_set_hotkey(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertEqual(config.browser.hotkeys["1"], BrowserHotkeyConfig(action="h3", h3_cell=h3_cell))
        self.assertEqual(handler.server.config.browser.hotkeys["1"], BrowserHotkeyConfig(action="h3", h3_cell=h3_cell))
        self.assertEqual(handler.location, "/settings")

    def test_run_server_hotkey_post_updates_tag_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"key=1&action=tag&tag_name=+Familie++&h3_cell=&person_name=&mode=exact&date=&uncertainty=1m"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

                def respond_text(self, content: str, *, status: HTTPStatus) -> None:
                    raise AssertionError(f"{status}: {content}")

            handler = FakeHandler()
            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                BildebankRequestHandler.respond_set_hotkey(handler)  # type: ignore[arg-type]

            config = load_config(root)
            config_text = (root / "bildebank-config.toml").read_text(encoding="utf-8")

        self.assertEqual(config.browser.hotkeys["1"], BrowserHotkeyConfig(action="tag", tag_name="Familie"))
        self.assertEqual(handler.server.config.browser.hotkeys["1"], BrowserHotkeyConfig(action="tag", tag_name="Familie"))
        self.assertIn('"1" = { action = "tag", tag_name = "Familie" }', config_text)
        self.assertEqual(handler.location, "/settings")

    def test_run_server_hotkey_post_rejects_invalid_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = (
                "key=5&action=manual_date&h3_cell=&person_name=&mode=between&"
                "date=&uncertainty=1m&date_from=2004-08-31&date_to=2004-06-01&note="
            ).encode("utf-8")
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())

                def respond_text(self, content: str, *, status: HTTPStatus) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                BildebankRequestHandler.respond_set_hotkey(handler)  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("Fra-dato kan ikke være etter til-dato", str(response["content"]))

    def test_run_server_hotkey_post_rejects_empty_tag_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"key=1&action=tag&tag_name=+&h3_cell=&person_name=&mode=exact&date=&uncertainty=1m"
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())

                def respond_text(self, content: str, *, status: HTTPStatus) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                BildebankRequestHandler.respond_set_hotkey(handler)  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("Taggnavn kan ikke være tomt", str(response["content"]))

    def test_run_server_face_model_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_root = root / ".bildebank-insightface"
            (model_root / "models" / "antelopev2").mkdir(parents=True)
            (model_root / "models" / "antelopev2" / "scrfd_10g_bnkps.onnx").write_bytes(b"model")
            (root / "bildebank-config.toml").write_text(
                """
[face_recognition]
model_root = ".bildebank-insightface"
model_name = "buffalo_l"
""",
                encoding="utf-8",
            )
            data = b"model_name=antelopev2"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=load_config(root))
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                BildebankRequestHandler.respond_set_face_model(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertEqual(config.face_recognition.model_name, "antelopev2")
        self.assertEqual(handler.server.config.face_recognition.model_name, "antelopev2")
        self.assertEqual(handler.location, "/settings")

    def test_run_server_face_model_post_rejects_not_installed_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bildebank-config.toml").write_text(
                """
[face_recognition]
model_root = ".bildebank-insightface"
model_name = "buffalo_l"
""",
                encoding="utf-8",
            )
            data = b"model_name=antelopev2"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=load_config(root))

            handler = FakeHandler()
            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                with self.assertRaisesRegex(ValueError, "ikke installert"):
                    BildebankRequestHandler.respond_set_face_model(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertEqual(config.face_recognition.model_name, "buffalo_l")

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
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
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
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
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

    def test_geo_map_component_orientation_matches_cardinal_directions(self) -> None:
        import h3

        origin = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        cells = sorted(h3.grid_disk(origin, 1))
        coords = geo_component_pixel_coordinates(cells, 28.0)
        origin_lat, origin_lon = h3.cell_to_latlng(origin)
        origin_x, origin_y = coords[origin]

        mismatches = 0
        comparisons = 0
        for cell in cells:
            if cell == origin:
                continue
            lat, lon = h3.cell_to_latlng(cell)
            x, y = coords[cell]
            if abs(lon - origin_lon) > 0.000001:
                comparisons += 1
                mismatches += (x > origin_x) != (lon > origin_lon)
            if abs(lat - origin_lat) > 0.000001:
                comparisons += 1
                mismatches += (y > origin_y) != (lat < origin_lat)

        self.assertGreater(comparisons, 0)
        self.assertLessEqual(mismatches, 1)

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

            years_body = years_page_html(target)
            year_body = year_months_page_html(target, "2005")
            filtered_years_body = years_page_html(target, hide_out_of_focus=True)
            filtered_year_body = year_months_page_html(target, "2007", hide_out_of_focus=True)
            year_cards = browser_year_cards(target, hide_out_of_focus=True)
            month_cards = browser_year_month_cards(target, "2005")

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
        self.assertIn('href="/month/2005-03"', year_body)
        self.assertIn('href="/month/2005-04"', year_body)
        self.assertIn('href="/month/2005-05"', year_body)
        self.assertIn('data-nav-button-pair="year"', year_body)
        self.assertIn('data-nav-button-pair="month"', year_body)
        self.assertIn('href="/years" title="Forrige år" data-key-nav="previous-year">◀ Å</a>', year_body)
        self.assertIn('href="/years/2007" title="Neste år" data-key-nav="next-year">r ▶</a>', year_body)
        self.assertIn('<span class="nav-button disabled">◀ Mån</span>', year_body)
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

    def test_manual_date_changes_browser_month_without_moving_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2026" / "01" / "IMG_20260102.jpg"

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "date-set",
                    str(imported),
                    "--date",
                    "2004-07-15",
                    "--uncertainty",
                    "1m",
                    "--note",
                    "Kamera hadde feil dato",
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertTrue(imported.exists())
            self.assertIn("Manuell dato satt: ca. 2004-07-15", stdout)
            self.assertEqual(browser_month_items(target, "2026-01"), [])
            manual_items = browser_month_items(target, "2004-07")
            self.assertEqual([item["stored_filename"] for item in manual_items], ["IMG_20260102.jpg"])
            self.assertEqual(month_key_for_item(target, manual_items[0]), "2004-07")
            body = item_page_html(
                target,
                manual_items[0],
                *adjacent_browser_items(target, manual_items[0]),
                browser_month_navigation(target, manual_items[0]),
            )
            info_body = image_info_content_html(target, manual_items[0])
            self.assertNotIn("date-status-badge", body)
            self.assertNotIn("Opprinnelig: 2026-01-02", body)
            self.assertIn("ca. 2004-07-15", info_body)
            self.assertIn("Kamera hadde feil dato", info_body)
            self.assertIn("Opprinnelig dato", info_body)

            code, stdout, stderr = capture_cli(["--target", str(target), "date-clear", str(imported)])

            self.assertEqual(code, 0, stderr)
            self.assertEqual(browser_month_items(target, "2004-07"), [])
            self.assertEqual([item["stored_filename"] for item in browser_month_items(target, "2026-01")], ["IMG_20260102.jpg"])

    def test_manual_date_cli_commands_refuse_to_run_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2026" / "01" / "IMG_20260102.jpg"
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            set_code, _set_stdout, set_stderr = capture_cli(
                ["--target", str(target), "date-set", str(imported), "--date", "2004-07-15"]
            )
            clear_code, _clear_stdout, clear_stderr = capture_cli(
                ["--target", str(target), "date-clear", str(imported)]
            )

        self.assertEqual(set_code, 1)
        self.assertIn("Bildesamlingen er låst", set_stderr)
        self.assertEqual(clear_code, 1)
        self.assertIn("Bildesamlingen er låst", clear_stderr)

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

    def test_manual_between_date_uses_midpoint_in_static_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2026" / "01" / "IMG_20260102.jpg"

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "date-set",
                    str(imported),
                    "--between",
                    "2004-06-01",
                    "2004-08-31",
                ]
            )
            self.assertEqual(code, 0, stderr)

            output = root / "index.html"
            self.assertEqual(run_cli(["--target", str(target), "make-browser", "--output", str(output)]), 0)
            html = output.read_text(encoding="utf-8")

        self.assertIn('"monthKey": "2004-07"', html)
        self.assertIn('"browserDate": "2004-07-16"', html)
        self.assertIn('"manualDateFrom": "2004-06-01"', html)
        self.assertIn('"manualDateTo": "2004-08-31"', html)

    def test_make_browser_requires_target_lock_before_writing_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            with patch("bildebank.cli.recover_pending_file_moves"):
                code, stdout, stderr = capture_cli(["--target", str(target), "make-browser"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertFalse((target / "index.html").exists())

    def test_server_month_thumbnails_clip_rotated_images(self) -> None:
        self.assertIn(".thumb-link {", SERVER_CSS)
        self.assertIn("aspect-ratio: 4 / 3;", SERVER_CSS)
        self.assertIn("overflow: hidden;", SERVER_CSS)
        self.assertIn(".server-browser.month-browser", SERVER_CSS)
        self.assertIn("min-height: 100vh;", SERVER_CSS)
        self.assertIn("min-height: 100dvh;", SERVER_CSS)
        self.assertIn("grid-template-rows: max-content minmax(0, 1fr);", SERVER_CSS)
        self.assertIn(".year-month-grid-server", SERVER_CSS)
        self.assertIn("grid-template-columns: repeat(6, minmax(120px, 1fr));", SERVER_CSS)
        self.assertIn("gap: 10px;", SERVER_CSS)
        self.assertIn(".year-month-grid-server .text { padding: 6px 8px; font-size: 13px; line-height: 1.2; }", SERVER_CSS)
        self.assertIn("grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));", SERVER_CSS)
        self.assertIn(".month-browser .month-grid-server { overflow: visible; }", SERVER_CSS)

    def test_static_browser_sorts_by_taken_date_inside_month(self) -> None:
        html = render_html([], month_preview_limit=None)
        compare = html[html.index("function compareItems") : html.index("function buildMonths")]

        month_order = compare.index("a.monthKey.localeCompare")
        date_order = compare.index("aDate.localeCompare")
        path_order = compare.index("a.path.localeCompare")

        self.assertLess(month_order, date_order)
        self.assertLess(date_order, path_order)

    def test_static_browser_has_dynamic_year_breadcrumb_and_overviews(self) -> None:
        html = render_html(
            [
                {
                    "path": "2022/01/<ferie & vinter>.jpg",
                    "url": "2022/01/%3Cferie%20%26%20vinter%3E.jpg",
                    "thumbnailSrc": "",
                    "kind": "image",
                    "monthKey": "2022-01",
                    "browserDate": "2022-01-02",
                    "name": "<ferie & vinter>.jpg",
                    "sizeText": "1 byte",
                },
                {
                    "path": "udatert/gammelt.jpg",
                    "url": "udatert/gammelt.jpg",
                    "thumbnailSrc": "",
                    "kind": "image",
                    "monthKey": "udatert",
                    "browserDate": "9999-99-99",
                    "name": "gammelt.jpg",
                    "sizeText": "1 byte",
                },
            ]
        )

        self.assertIn('<nav class="breadcrumb" aria-label="Plassering"><span>År</span></nav>', html)
        self.assertIn('"01": "Januar"', html)
        self.assertIn('"12": "Desember"', html)
        self.assertIn("function renderYears()", html)
        self.assertIn("function renderYear()", html)
        self.assertIn('state.viewMode = "years";', html)
        self.assertIn(
            'state.viewMode = year.key === "udatert" || year.key === "ukjent" ? "month" : "year";',
            html,
        )
        self.assertIn('parts.push({ label: "År", action: showYears });', html)
        self.assertIn(
            'action: state.viewMode === "item" ? event => showMonth(month.key, event) : null',
            html,
        )
        self.assertIn('if (state.viewMode === "item" && item) parts.push({ label: item.name });', html)
        self.assertIn('link.addEventListener("click", part.action);', html)
        self.assertIn("text.textContent = part.label;", html)
        self.assertIn("function attachSwipeNavigation", html)
        self.assertIn("window.PointerEvent", html)
        self.assertIn('event.pointerType !== "touch" && event.pointerType !== "pen"', html)
        self.assertIn('container.addEventListener("touchstart"', html)
        self.assertIn("moveItem(direction);", html)
        self.assertNotIn("<ferie & vinter>.jpg", html)
        self.assertIn(r"\u003cferie \u0026 vinter\u003e.jpg", html)
        self.assertNotIn("data-open-info", html)
        self.assertNotIn("<footer", html)
        self.assertNotIn('id="filename"', html)
        self.assertNotIn("filenameEl", html)
        self.assertNotIn("Årsoversikt:", html)
        self.assertNotIn("Månedsoversikt:", html)

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

    def test_face_box_media_metadata_write_requires_target_lock(self) -> None:
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
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            with self.assertRaises(TargetLockError):
                update_face_box_media_metadata(
                    target,
                    1,
                    ImageDimensions(100, 80),
                    1,
                    123,
                )

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute(
                    """
                    SELECT media_width, media_height, media_orientation, media_metadata_mtime_ns
                    FROM files
                    """
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(row, (None, None, None, None))

    def test_face_box_media_metadata_cache_locks_before_reading_file(self) -> None:
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
            conn = db.connect(target)
            try:
                item = dict(
                    conn.execute(
                        """
                        SELECT
                            id, target_path, media_width, media_height,
                            media_orientation, media_metadata_mtime_ns
                        FROM files
                        """
                    ).fetchone()
                )
            finally:
                conn.close()
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            with (
                patch(
                    "bildebank.server_faces.image_dimensions",
                    side_effect=AssertionError("filen skal ikke leses uten lås"),
                ),
                self.assertRaises(TargetLockError),
            ):
                cached_face_box_media_metadata(target, item)

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

    def test_image_info_date_source_labels_are_human_readable(self) -> None:
        self.assertEqual(date_source_text("metadata"), "fra metadata")
        self.assertEqual(date_source_text("filename"), "fra filnavn")
        self.assertEqual(date_source_text("mtime"), "fra mtime")

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

    def test_tag_add_stops_before_database_writes_when_target_is_locked(self) -> None:
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
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=remove\npid=123\n", encoding="utf-8")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "tag-add", "2024/01/IMG_20240102.jpg", "Familie"]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertTrue(lock_path.exists())
            conn = db.connect(target)
            try:
                self.assertIsNone(conn.execute("SELECT id FROM tags WHERE name_key = 'familie'").fetchone())
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM command_log WHERE command = 'tag-add'").fetchone()[0],
                    0,
                )
            finally:
                conn.close()

    def test_tag_add_rolls_back_and_releases_lock_on_validation_error(self) -> None:
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

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "tag-add", "2024/01/IMG_20240102.jpg", ""]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Taggnavn kan ikke være tomt", stderr)
            self.assertFalse((target / LOCK_FILENAME).exists())
            conn = db.connect(target)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM command_log WHERE command = 'tag-add'").fetchone()[0],
                    0,
                )
            finally:
                conn.close()

            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "tag-add",
                        "2024/01/IMG_20240102.jpg",
                        "Familie",
                    ]
                ),
                0,
            )

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
            date_year_body = source_year_months_page_html(target, source_filter, "2024")
            date_month_body = source_month_page_html(target, source_filter, "2024-01", date_month)
            person_body = source_item_page_html(
                target,
                person_filter,
                person_item,
                *adjacent_source_items(target, person_filter, person_item),
                person_month_nav,
            )
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
        self.assertIn("Filtersøk: after:2023-12-01 before:2024-12-12 (2 treff)", date_year_body)
        self.assertIn('title="2 treff i filtersøket"', date_year_body)
        self.assertIn("Filtersøk: after:2023-12-01 before:2024-12-12 (2 treff)", date_month_body)
        self.assertIn('title="2 treff i filtersøket"', date_month_body)
        self.assertIn("/filter/after%3A2023-12-01%20before%3A2024-12-12/item/4", date_body)
        self.assertIn('href="/filter">Filtersøk</a>', date_body)
        self.assertIn("Filtersøk: person:viljar", person_body)
        self.assertIn("/filter/person%3Aviljar/item/3", person_body)
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
            image_body = item_page_html(
                target,
                image_item,
                *adjacent_browser_items(target, image_item),
                browser_month_navigation(target, image_item),
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
        self.assertEqual(motion_file.content_type, "video/mp4")
        self.assertEqual(motion_file.content[4:8], b"ftyp")

    def test_run_server_hides_nef_sidecar_and_links_it_from_jpg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "DSC_0170.JPG").write_bytes(jpeg_with_exif_datetime("2019:03:03 12:00:00"))
            (source / "DSC_0170.NEF").write_bytes(minimal_tiff_with_datetime("2019:03:03 12:00:00"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            month_items = browser_month_items(target, "2019-03")
            type_file_source = text_filter_browser_source("type:file")
            extension_source = text_filter_browser_source("extension:nef")
            filename_source = text_filter_browser_source("filename:DSC_0170")
            type_file_items = source_month_items(target, type_file_source, "2019-03")
            extension_items = source_month_items(target, extension_source, "2019-03")
            filename_items = source_month_items(target, filename_source, "2019-03")
            image_item = month_items[0]
            nef_item = extension_items[0]
            response: dict[str, object] = {}
            handler = object.__new__(BildebankRequestHandler)
            handler.server = SimpleNamespace(target=target)

            def fake_respond_json(content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                response["content"] = content
                response["status"] = status

            handler.respond_json = fake_respond_json  # type: ignore[method-assign]
            handler.respond_item_info(f"file_id={nef_item['id']}")
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
        self.assertEqual([item["stored_filename"] for item in type_file_items], ["DSC_0170.NEF"])
        self.assertEqual([item["stored_filename"] for item in extension_items], ["DSC_0170.NEF"])
        self.assertEqual(
            [item["stored_filename"] for item in filename_items],
            ["DSC_0170.JPG", "DSC_0170.NEF"],
        )
        controls_start = image_body.index('<nav class="controls"')
        controls_end = image_body.index("</nav>", controls_start)
        controls_html = image_body[controls_start:controls_end]
        self.assertIn(".NEF</a>", controls_html)
        self.assertIn("/filter/filename%3ADSC_0170.NEF/item/", image_body)
        self.assertNotIn("RAW-fil: DSC_0170.NEF", image_body)
        self.assertNotIn('<footer class="browser-footer">', image_body)
        self.assertEqual(response["status"], HTTPStatus.OK)
        content = response["content"]
        assert isinstance(content, dict)
        self.assertIs(content["ok"], True)
        nef_info_html = str(content["html"])
        self.assertIn("<dt>Filnavn</dt>", nef_info_html)
        self.assertIn("DSC_0170.NEF", nef_info_html)
        self.assertIn("<dt>Filstørrelse</dt>", nef_info_html)
        self.assertIn("<dt>Kilder</dt>", nef_info_html)
        self.assertIn(source.name, nef_info_html)

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

        self.assertIn("Filtersøk: type:image (1 treff)", body)

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
                source_month = source_month_items(target, source_browser, "2024-01")
            self.assertIsNotNone(source_item)
            self.assertTrue(source_has_sql_filter(source_browser))
            item_body = source_item_page_html(
                target,
                source_browser,
                source_item,
                *source_adjacent,
                source_month_nav,
            )
            year_body = source_year_months_page_html(target, source_browser, "2024")
            first_year_body = source_year_months_page_html(target, source_browser, "2023")
            month_body = source_month_page_html(target, source_browser, "2024-01", source_month)
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
        self.assertIn('<span class="nav-button disabled">◀ Mån</span>', first_year_body)
        self.assertIn('<section class="month-grid-server year-month-grid-server">', year_body)
        self.assertNotIn('<footer class="browser-footer">', year_body)
        self.assertIn('href="/source/1/item/2"', month_body)
        self.assertIn('href="/source/1/year/2024">2024</a>', month_body)
        self.assertIn('href="/source/1/year/2023" title="Forrige år" data-key-nav="previous-year">◀ Å</a>', month_body)
        self.assertIn('href="/source/1/year/2025" title="Neste år" data-key-nav="next-year">r ▶</a>', month_body)
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
                patch("bildebank.server.source_item_by_id", side_effect=AssertionError("Handler should use cached IDs")),
                patch("bildebank.server.adjacent_source_items", side_effect=AssertionError("Handler should use cached IDs")),
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

    def test_tag_cli_and_server_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-a")
            (source / "IMG_20240203.jpg").write_bytes(b"image-b")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source", "--quiet", str(source)]), 0)
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            item_path = target / str(item["target_path"])

            code, stdout, stderr = capture_cli(["--target", str(target), "tag-add", str(item_path), "Familie"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("La til: Familie", stdout)
            code, stdout, stderr = capture_cli(["--target", str(target), "tag-list"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Familie\t1\tuser", stdout)
            self.assertIn("Ute av fokus\t0\tsystem", stdout)
            code, stdout, stderr = capture_cli(["--target", str(target), "tag-files", "familie"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("IMG_20240102.jpg", stdout)

            tag_source = tag_browser_source("familie")
            tag_item = source_item_by_id(target, tag_source, 1)
            self.assertIsNotNone(tag_item)
            self.assertIsNone(source_item_by_id(target, tag_source, 2))
            item_body = source_item_page_html(
                target,
                tag_source,
                tag_item,
                *adjacent_source_items(target, tag_source, tag_item),
                source_month_navigation(target, tag_source, tag_item),
            )
            tag_month = source_month_items(target, tag_source, "2024-01")
            month_body = source_month_page_html(target, tag_source, "2024-01", tag_month)
            tags_body = tags_page_html(target)

            code, stdout, stderr = capture_cli(["--target", str(target), "tag-remove", str(item_path), "familie"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Fjernet: familie", stdout)
            code, stdout, stderr = capture_cli(["--target", str(target), "tag-add", str(item_path), "Ute av fokus"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("La til: Ute av fokus", stdout)
            code, stdout, stderr = capture_cli(["--target", str(target), "tag-list", str(item_path)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Ute av fokus\tsystem", stdout)
            code, stdout, stderr = capture_cli(["--target", str(target), "tag-remove", str(item_path), "Ute av fokus"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Fjernet: Ute av fokus", stdout)
            code, stdout, stderr = capture_cli(["--target", str(target), "tag-list"])
            self.assertEqual(code, 0, stderr)
            self.assertNotIn("Familie", stdout)
            self.assertIn("Ute av fokus\t0\tsystem", stdout)

        self.assertIn("Tagg: familie", item_body)
        self.assertIn('href="/item/1">Åpne i alle bilder</a>', item_body)
        self.assertIn('href="/tag/familie/year/2024">2024</a>', item_body)
        self.assertIn('href="/tag/familie/item/1"', month_body)
        self.assertEqual(len(tag_month), 1)
        self.assertIn("<h1>Tagger</h1>", tags_body)
        self.assertIn('action="/tags/create"', tags_body)
        self.assertIn('action="/tags/rename"', tags_body)
        self.assertIn('action="/tags/delete"', tags_body)
        self.assertIn('class="tag-actions"', tags_body)
        self.assertIn(".tag-actions", SERVER_CSS)
        self.assertIn('data-confirm-submit="Slette taggen Familie fra alle bilder?"', tags_body)
        self.assertIn("systemtagg kan ikke endres", tags_body)
        self.assertIn('href="/tag/Familie">Vis bilder (1)</a>', tags_body)
        self.assertIn("brukertagg", tags_body)
        self.assertIn("systemtagg", tags_body)

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
            finally:
                conn.close()

        self.assertIn("Åpne i alle bilder", body)
        self.assertIn('href="/item/1">Åpne i alle bilder</a>', body)
        self.assertEqual(raw_sidecar_ids.call_count, 1)

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
                face_conn.execute("INSERT INTO person_files(person_id, file_id) VALUES(3, 1)")
                face_conn.execute("INSERT INTO person_files(person_id, file_id) VALUES(6, 1)")
                face_conn.commit()
            finally:
                face_conn.close()

            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))
            manual_disabled_body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                manual_person_controls_enabled=False,
            )
            face_body = face_overlay_content_html(target, item)
            disabled_body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                face_enabled=False,
                openclip_enabled=False,
            )

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
        self.assertIn('class="person-link" href="/person/Per%20Manual/no-faces/item/1"', tag_rail_html)
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
        self.assertIn("(referanse face-id 1)", face_body)
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

                    def source_item_order(self, source, *, hide_out_of_focus: bool = False):
                        item_ids = source_item_ids(
                            self.target,
                            source,
                            self.config.face_recognition,
                            hide_out_of_focus=hide_out_of_focus,
                        )
                        return item_ids, {file_id: index for index, file_id in enumerate(item_ids)}

                    def source_month_keys(self, source, *, hide_out_of_focus: bool = False):
                        return source_month_keys(
                            self.target,
                            source,
                            self.config.face_recognition,
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
                    patch("bildebank.server.source_item_by_id", side_effect=AssertionError("handler should use cached source item order")),
                    patch("bildebank.server.adjacent_source_items", side_effect=AssertionError("handler should use cached adjacent item ids")),
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
            confirmed_body = source_item_page_html(
                target,
                confirmed_source,
                confirmed_item,
                *adjacent_source_items(target, confirmed_source, confirmed_item),
                source_month_navigation(target, confirmed_source, confirmed_item),
            )
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

    def test_import_requires_name(self) -> None:
        for args in (["import", "."], ["import", "--dry-run", "."]):
            with self.subTest(args=args):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    build_parser().parse_args(args)

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

    def test_target_add_and_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertTrue((target / DB_FILENAME).exists())
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            self.assertTrue(imported.exists())
            self.assertEqual(imported.read_bytes(), b"image-one")

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                    "13",
                )
                file_columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
                self.assertNotIn("source_id", file_columns)
                self.assertNotIn("source_path", file_columns)
                self.assertNotIn("source_path_key", file_columns)
                source_columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
                file_source_columns = {row[1] for row in conn.execute("PRAGMA table_info(file_sources)")}
                self.assertNotIn("kind", source_columns)
                self.assertNotIn("kind", file_source_columns)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources WHERE name IS NULL").fetchone()[0], 0)
            finally:
                conn.close()

    def test_import_accepts_raw_nef_and_psd_archive_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.raw").write_bytes(b"raw")
            (source / "IMG_20240103.nef").write_bytes(b"nef")
            (source / "edited_20240104.psd").write_bytes(b"psd")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertEqual((target / "2024" / "01" / "IMG_20240102.raw").read_bytes(), b"raw")
            self.assertEqual((target / "2024" / "01" / "IMG_20240103.nef").read_bytes(), b"nef")
            self.assertEqual((target / "2024" / "01" / "edited_20240104.psd").read_bytes(), b"psd")

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 3)
            finally:
                conn.close()

    def test_backup_creates_named_backup_with_metadata_and_deleted_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "bilde-samling"
            backup_parent = root / "backup-root"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            image = target / "2024" / "01" / "IMG_20240102.jpg"
            removed = target / "deleted" / "2024" / "01" / "IMG_20240103.jpg"
            image.parent.mkdir(parents=True)
            removed.parent.mkdir(parents=True)
            image.write_bytes(b"image")
            removed.write_bytes(b"removed")

            with patch("bildebank.backup.select_backup_engine", return_value=None):
                code, stdout, stderr = capture_cli(["--target", str(target), "backup", str(backup_parent)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("ADVARSEL: robocopy/rsync mangler", stdout)
            self.assertIn("files_copied=", stdout)
            backup_dir = backup_parent / target.name
            self.assertIn(str(backup_dir), stdout)
            self.assertEqual((backup_dir / "2024" / "01" / "IMG_20240102.jpg").read_bytes(), b"image")
            self.assertEqual((backup_dir / "deleted" / "2024" / "01" / "IMG_20240103.jpg").read_bytes(), b"removed")
            metadata = json.loads((backup_dir / ".bildebank-backup.json").read_text(encoding="utf-8"))
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                collection_id = conn.execute("SELECT value FROM meta WHERE key = 'collection_id'").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(metadata["backup_of"], collection_id)
            self.assertEqual(metadata["source_name"], target.name)

    def test_backup_dry_run_does_not_create_backup_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)

            with patch("bildebank.backup.select_backup_engine", return_value=None):
                code, stdout, stderr = capture_cli(["--target", str(target), "backup", "--dry-run", str(backup_parent)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Dry run", stdout)
            self.assertIn("Would create new backup.", stdout)
            self.assertIn("motor=python", stdout)
            self.assertIn("Dry-run viser bare plan", stdout)
            self.assertFalse((backup_parent / target.name).exists())

    def test_backup_dry_run_uses_rsync_dry_run_without_creating_backup_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)

            with (
                patch("bildebank.backup.sys.platform", "linux"),
                patch("bildebank.backup.shutil.which", return_value="/usr/bin/rsync"),
                patch("bildebank.backup.subprocess.run", return_value=SimpleNamespace(returncode=0)) as subprocess_run,
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "backup", "--dry-run", str(backup_parent)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Dry run", stdout)
            self.assertIn("motor=rsync", stdout)
            command = subprocess_run.call_args.args[0]
            self.assertIn("--dry-run", command)
            self.assertIn("--delete", command)
            self.assertIn("--exclude", command)
            self.assertIn(".bildebank-backup.json", command)
            self.assertEqual(command[-2], str(target.resolve()) + "/")
            self.assertEqual(command[-1], str((backup_parent / target.name).resolve()) + "/")
            self.assertFalse((backup_parent / target.name).exists())

    def test_backup_dry_run_uses_robocopy_list_only_without_creating_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)

            with (
                patch("bildebank.backup.sys.platform", "win32"),
                patch("bildebank.backup.shutil.which", return_value="robocopy"),
                patch("bildebank.backup.subprocess.run", return_value=SimpleNamespace(returncode=3)) as subprocess_run,
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "backup", "--dry-run", str(backup_parent)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Dry run", stdout)
            self.assertIn("motor=robocopy", stdout)
            command = subprocess_run.call_args.args[0]
            self.assertIn("/MIR", command)
            self.assertIn("/L", command)
            self.assertIn("/XF", command)
            self.assertIn(".bildebank-backup.json", command)
            self.assertFalse(((backup_parent / target.name) / ".bildebank-backup.json").exists())

    def test_backup_stops_when_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=import\npid=123\n", encoding="utf-8")

            code, stdout, stderr = capture_cli(["--target", str(target), "backup", str(backup_parent)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertIn(str(lock_path), stderr)
            self.assertTrue(lock_path.exists())
            self.assertFalse((backup_parent / target.name).exists())

    def test_backup_dry_run_does_not_require_target_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=import\npid=123\n", encoding="utf-8")

            with patch("bildebank.backup.select_backup_engine", return_value=None):
                code, stdout, stderr = capture_cli(["--target", str(target), "backup", "--dry-run", str(backup_parent)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Dry run", stdout)
            self.assertTrue(lock_path.exists())
            self.assertFalse((backup_parent / target.name).exists())

    def test_backup_holds_target_lock_while_mirroring_and_removes_it_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            lock_path = target / LOCK_FILENAME
            observed_lock = []

            def mirror_with_lock_check(source, destination):  # noqa: ANN001
                observed_lock.append(lock_path.exists())
                return SimpleNamespace(files_copied=0, files_deleted=0, dirs_created=0, dirs_deleted=0)

            with (
                patch("bildebank.backup.select_backup_engine", return_value=None),
                patch("bildebank.backup.mirror_directory", side_effect=mirror_with_lock_check),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "backup", str(backup_parent)])

            self.assertEqual(code, 0, stderr)
            self.assertEqual(observed_lock, [True])
            self.assertFalse(lock_path.exists())

    def test_backup_removes_target_lock_but_leaves_in_progress_metadata_when_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            lock_path = target / LOCK_FILENAME

            with (
                patch("bildebank.backup.select_backup_engine", return_value=None),
                patch("bildebank.backup.mirror_directory", side_effect=KeyboardInterrupt),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "backup", str(backup_parent)])

            self.assertEqual(code, 130)
            self.assertIn("Avbrutt.", stderr)
            self.assertFalse(lock_path.exists())
            metadata = json.loads(
                ((backup_parent / target.name) / ".bildebank-backup.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "in-progress")

    def test_backup_updates_existing_backup_and_removes_extra_backup_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / "first.txt").write_text("first\n", encoding="utf-8")
            with patch("bildebank.backup.select_backup_engine", return_value=None):
                self.assertEqual(run_cli(["--target", str(target), "backup", str(backup_parent)]), 0)
            backup_dir = backup_parent / target.name
            extra = backup_dir / "extra.txt"
            extra.write_text("extra\n", encoding="utf-8")
            (target / "first.txt").write_text("changed\n", encoding="utf-8")

            with patch("bildebank.backup.select_backup_engine", return_value=None):
                code, stdout, stderr = capture_cli(["--target", str(target), "backup", str(backup_parent)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Updated backup.", stdout)
            self.assertEqual((backup_dir / "first.txt").read_text(encoding="utf-8"), "changed\n")
            self.assertFalse(extra.exists())

    def test_backup_rejects_existing_directory_without_backup_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_dir = backup_parent / target.name
            backup_dir.mkdir(parents=True)
            (backup_dir / "unrelated.txt").write_text("do not touch\n", encoding="utf-8")

            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "backup", str(backup_parent)])

            self.assertEqual(code, 1)
            self.assertIn("ser ikke ut til å være en bildebank-backup", stderr)
            self.assertEqual((backup_dir / "unrelated.txt").read_text(encoding="utf-8"), "do not touch\n")

    def test_backup_rejects_destination_inside_existing_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            nested_parent = backup_parent / target.name / "nybackup"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            with patch("bildebank.backup.select_backup_engine", return_value=None):
                self.assertEqual(run_cli(["--target", str(target), "backup", str(backup_parent)]), 0)
            nested_parent.mkdir(parents=True)

            code, stdout, stderr = capture_cli(["--target", str(target), "backup", str(nested_parent)])

            self.assertEqual(code, 1)
            self.assertIn("Kan ikke lage backup inni en annen backup", stderr)
            self.assertFalse((nested_parent / target.name).exists())

    def test_backup_uses_rsync_when_available_and_excludes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)

            with (
                patch("bildebank.backup.sys.platform", "linux"),
                patch("bildebank.backup.shutil.which", return_value="/usr/bin/rsync"),
                patch("bildebank.backup.subprocess.run", return_value=SimpleNamespace(returncode=0)) as subprocess_run,
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "backup", str(backup_parent)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("motor=rsync", stdout)
            self.assertNotIn("files_copied=", stdout)
            self.assertNotIn("files_deleted=", stdout)
            command = subprocess_run.call_args.args[0]
            self.assertIn("--exclude", command)
            self.assertIn(".bildebank-backup.json", command)
            self.assertEqual(command[-2], str(target.resolve()) + "/")
            self.assertEqual(command[-1], str((backup_parent / target.name).resolve()) + "/")
            metadata = json.loads(
                ((backup_parent / target.name) / ".bildebank-backup.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "complete")
            self.assertEqual(metadata["engine"], "rsync")

    def test_backup_leaves_in_progress_metadata_when_rsync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)

            with (
                patch("bildebank.backup.sys.platform", "linux"),
                patch("bildebank.backup.shutil.which", return_value="/usr/bin/rsync"),
                patch("bildebank.backup.subprocess.run", return_value=SimpleNamespace(returncode=23)),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "backup", str(backup_parent)])

            self.assertEqual(code, 1)
            self.assertIn("rsync feilet", stderr)
            metadata = json.loads(
                ((backup_parent / target.name) / ".bildebank-backup.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "in-progress")

    def test_backup_uses_robocopy_on_windows_and_accepts_success_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_parent.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)

            with (
                patch("bildebank.backup.sys.platform", "win32"),
                patch("bildebank.backup.shutil.which", return_value="robocopy"),
                patch("bildebank.backup.subprocess.run", return_value=SimpleNamespace(returncode=3)) as subprocess_run,
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "backup", str(backup_parent)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("motor=robocopy", stdout)
            self.assertNotIn("files_copied=", stdout)
            self.assertNotIn("files_deleted=", stdout)
            command = subprocess_run.call_args.args[0]
            self.assertIn("/MIR", command)
            self.assertIn("/XF", command)
            self.assertIn(".bildebank-backup.json", command)

    def test_where_is_lists_program_and_registered_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["where-is"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Bildebank-program:", stdout)
            self.assertIn(str(self.program_root), stdout)
            self.assertIn("Programdata:", stdout)
            self.assertIn(str(self.program_root / PROGRAM_DB_FILENAME), stdout)
            self.assertIn("Kjente bildesamlingsmapper:", stdout)
            self.assertIn(str(target.resolve()), stdout)
            self.assertIn('cd "', stdout)

    def test_existing_target_is_registered_when_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            (self.program_root / PROGRAM_DB_FILENAME).unlink()

            self.assertEqual(run_cli(["--target", str(target), "status"]), 0)
            code, stdout, stderr = capture_cli(["where-is"])

            self.assertEqual(code, 0, stderr)
            self.assertIn(str(target.resolve()), stdout)

    def test_program_state_ignores_temporary_targets_for_real_program_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program_root = root / "repo"
            temp_root = root / "tmp"
            target = temp_root / "target"
            program_root.mkdir()
            db.init_database(target)

            with patch("bildebank.program_state.tempfile.gettempdir", return_value=str(temp_root)):
                record_target(program_root, target, created=True)
                targets = known_targets(program_root)

            self.assertEqual(targets, [])
            self.assertFalse((program_root / PROGRAM_DB_FILENAME).exists())

            conn = sqlite3.connect(program_root / PROGRAM_DB_FILENAME)
            try:
                ensure_schema(conn)
                conn.execute(
                    "INSERT INTO targets(path, path_key) VALUES(?, ?)",
                    (str(target.resolve()), str(target.resolve())),
                )
                conn.commit()
            finally:
                conn.close()

            with patch("bildebank.program_state.tempfile.gettempdir", return_value=str(temp_root)):
                targets = known_targets(program_root)

            self.assertEqual(targets, [])

    def test_program_state_records_collection_id_and_updates_moved_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            moved_target = root / "moved-target"
            target.rename(moved_target)

            self.assertEqual(run_cli(["--target", str(moved_target), "status"]), 0)

            conn = sqlite3.connect(self.program_root / PROGRAM_DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute("SELECT * FROM targets ORDER BY id").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(Path(rows[0]["path"]), moved_target.resolve())
                self.assertIsNotNone(rows[0]["collection_id"])
                self.assertEqual(str(uuid.UUID(rows[0]["collection_id"])), rows[0]["collection_id"])
            finally:
                conn.close()

    def test_program_state_legacy_schema_gets_collection_id_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"

            conn = sqlite3.connect(self.program_root / PROGRAM_DB_FILENAME)
            try:
                conn.execute(
                    """
                    CREATE TABLE targets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        path TEXT NOT NULL,
                        path_key TEXT NOT NULL UNIQUE,
                        created_at TEXT,
                        last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(run_cli(["create", str(target)]), 0)

            conn = sqlite3.connect(self.program_root / PROGRAM_DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(targets)")}
                self.assertIn("collection_id", columns)
                row = conn.execute("SELECT collection_id FROM targets").fetchone()
                self.assertIsNotNone(row["collection_id"])
            finally:
                conn.close()

    def test_program_state_backfills_collection_id_for_existing_target_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                collection_id = conn.execute(
                    "SELECT value FROM meta WHERE key = 'collection_id'"
                ).fetchone()[0]
            finally:
                conn.close()

            (self.program_root / PROGRAM_DB_FILENAME).unlink()
            conn = sqlite3.connect(self.program_root / PROGRAM_DB_FILENAME)
            try:
                conn.execute(
                    """
                    CREATE TABLE targets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        path TEXT NOT NULL,
                        path_key TEXT NOT NULL UNIQUE,
                        created_at TEXT,
                        last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO targets(path, path_key) VALUES(?, ?)",
                    (str(target.resolve()), str(target.resolve())),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["where-is"])

            self.assertEqual(code, 0, stderr)
            self.assertIn(str(target.resolve()), stdout)
            conn = sqlite3.connect(self.program_root / PROGRAM_DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT collection_id FROM targets").fetchone()
                self.assertEqual(row["collection_id"], collection_id)
            finally:
                conn.close()

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

    def test_status_and_browser_database_preparation_do_not_write_current_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            database_path = target / DB_FILENAME
            before = database_path.read_bytes()
            before_mtime = database_path.stat().st_mtime_ns

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])
            db.prepare_database(target)
            out_of_focus_file_ids(target)

            self.assertEqual(code, 0, stderr)
            self.assertIn("Importerte filer: 0", stdout)
            self.assertEqual(database_path.read_bytes(), before)
            self.assertEqual(database_path.stat().st_mtime_ns, before_mtime)

    def test_where_is_works_without_target(self) -> None:
        code, stdout, stderr = capture_cli(["where-is"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Bildebank-program:", stdout)
        self.assertIn("Ingen registrert ennå.", stdout)

    def test_doctor_is_disabled_by_default(self) -> None:
        exiftool = self.program_root / "bildebank-tools" / "exiftool" / "exiftool.exe"
        with (
            patch("bildebank.cli.resolve_exiftool_path", return_value=exiftool),
            patch("bildebank.cli.validate_exiftool_install", return_value="13.58"),
            patch("bildebank.cli.python_module_available", side_effect=lambda name: name == "h3"),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Bildebank doctor", stdout)
        self.assertIn("  OK: h3 installert", stdout)
        self.assertIn("  OK: ExifTool funnet:", stdout)
        self.assertIn("  OBS: face_recognition er slått av.", stdout)
        self.assertIn("  OBS: image_search er slått av.", stdout)
        self.assertIn("  OBS: ingen aktiv bildesamling funnet.", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_deep_is_explicit_and_not_added_to_face_status(self) -> None:
        default_args = build_parser().parse_args(["doctor"])
        deep_args = build_parser().parse_args(["doctor", "--deep"])
        face_status_args = build_parser().parse_args(["face-status"])

        self.assertFalse(default_args.deep)
        self.assertTrue(deep_args.deep)
        self.assertFalse(hasattr(face_status_args, "deep"))

    def test_face_status_is_doctor_alias(self) -> None:
        with patch("bildebank.cli.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")):
            code, stdout, stderr = capture_cli(["face-status"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Bildebank doctor", stdout)
        self.assertIn("Ansiktsgjenkjenning:", stdout)
        self.assertIn("Tekstbasert bildesøk:", stdout)
        self.assertIn("  FEIL: ExifTool mangler eller virker ikke: mangler", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_shows_exiftool_status(self) -> None:
        exiftool = self.program_root / "bildebank-tools" / "exiftool" / "exiftool.exe"
        with (
            patch("bildebank.cli.resolve_exiftool_path", return_value=exiftool),
            patch("bildebank.cli.validate_exiftool_install", return_value="13.58"),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn(f"  OK: ExifTool funnet: {exiftool} (13.58)", stdout)

    def test_doctor_reports_missing_exiftool_without_failing(self) -> None:
        with patch("bildebank.cli.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("  FEIL: ExifTool mangler eller virker ikke: mangler", stdout)
        self.assertIn("  Råd:", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_reports_enabled_face_recognition_missing_dependencies(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[face_recognition]
enabled = true
""",
            encoding="utf-8",
        )

        with (
            patch("bildebank.cli.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
            patch("bildebank.cli.python_module_available", side_effect=lambda name: name == "h3"),
            patch(
                "bildebank.cli.insightface_runtime_error",
                return_value="InsightFace er ikke installert. Kjør install-insightface.ps1 fra programmappen.",
            ),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("  OK: face_recognition er slått på", stdout)
        self.assertIn("  FEIL: InsightFace er ikke installert.", stdout)
        self.assertIn("  FEIL: face_recognition er slått på, men onnxruntime mangler.", stdout)
        self.assertIn("install-insightface.ps1", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_reports_insightface_opencv_linux_system_dependency(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[face_recognition]
enabled = true
""",
            encoding="utf-8",
        )

        with (
            patch("bildebank.cli.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
            patch("bildebank.cli.python_module_available", side_effect=lambda name: name in {"h3", "onnxruntime"}),
            patch(
                "bildebank.cli.insightface_runtime_error",
                return_value=(
                    "InsightFace er installert, men OpenCV mangler Linux-biblioteket libGL.so.1. "
                    "Installer det i WSL/Linux med `sudo apt install libgl1`."
                ),
            ),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("  FEIL: InsightFace er installert, men OpenCV mangler Linux-biblioteket libGL.so.1.", stdout)
        self.assertIn("  Råd: Installer Linux-pakken: `sudo apt install libgl1`.", stdout)
        self.assertIn("  OK: onnxruntime installert", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_reports_enabled_image_search_missing_dependencies(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[image_search]
enabled = true
""",
            encoding="utf-8",
        )

        with (
            patch("bildebank.cli.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
            patch("bildebank.cli.python_module_available", side_effect=lambda name: name == "h3"),
            patch("bildebank.cli.torch_gpu_status", return_value={"torch": "nei", "cuda": "nei", "device": "-"}),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("  OK: image_search er slått på", stdout)
        self.assertIn("  FEIL: image_search er slått på, men open_clip mangler.", stdout)
        self.assertIn("  FEIL: image_search er slått på, men torch mangler.", stdout)
        self.assertIn("install-openclip.ps1", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_reports_missing_h3_without_failing(self) -> None:
        with (
            patch("bildebank.cli.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
            patch("bildebank.cli.python_module_available", return_value=False),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("  FEIL: h3 mangler. Geografiske funksjoner virker ikke.", stdout)
        self.assertIn("  Råd: Kjør setup-windows.ps1 på nytt", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_reports_database_file_missing_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = db.connect(target)
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename,
                        stored_filename, sha256, size_bytes, date_source
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2024/01/missing.jpg",
                        db.relative_path_key(Path("2024/01/missing.jpg")),
                        "missing.jpg",
                        "missing.jpg",
                        "missing-file-sha256",
                        123,
                        "filename",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch("bildebank.cli.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
                patch("bildebank.cli.python_module_available", return_value=False),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "doctor"]
                )

        self.assertEqual(code, 0, stderr)
        self.assertIn("Doctor filer: kontrollert=1/1", stdout)
        self.assertIn(
            "Doctor filer: ferdig kontrollert 1/1 filer.",
            stdout,
        )
        self.assertIn(
            "FEIL: 1 aktiv(e) databasefil(er) mangler på disk.",
            stdout,
        )
        self.assertIn(
            "INFO: file #1: 2024/01/missing.jpg",
            stdout,
        )
        self.assertIn("Undersøk filene og sikkerhetskopien", stdout)

    def test_doctor_reports_database_files_present_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            stored_path = target / "2024" / "01" / "present.jpg"
            stored_path.parent.mkdir(parents=True)
            stored_path.write_bytes(b"present")
            conn = db.connect(target)
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename,
                        stored_filename, sha256, size_bytes, date_source
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2024/01/present.jpg",
                        db.relative_path_key(Path("2024/01/present.jpg")),
                        "present.jpg",
                        "present.jpg",
                        "present-file-sha256",
                        stored_path.stat().st_size,
                        "filename",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename,
                        stored_filename, sha256, size_bytes, date_source,
                        deleted_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        "deleted/2024/01/missing.jpg",
                        db.relative_path_key(
                            Path("deleted/2024/01/missing.jpg")
                        ),
                        "missing.jpg",
                        "missing.jpg",
                        "deleted-missing-file-sha256",
                        123,
                        "filename",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch("bildebank.cli.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
                patch("bildebank.cli.python_module_available", return_value=False),
                patch("bildebank.cli.sha256_file") as hash_file,
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "doctor"]
                )

        self.assertEqual(code, 0, stderr)
        hash_file.assert_not_called()
        self.assertIn("Doctor filer: kontrollert=1/1", stdout)
        self.assertIn(
            "Doctor filer: ferdig kontrollert 1/1 filer.",
            stdout,
        )
        self.assertIn(
            "OK: alle 1 aktive databasefiler finnes på disk",
            stdout,
        )
        self.assertNotIn("aktiv(e) databasefil(er) mangler på disk", stdout)
        self.assertNotIn("Dyp filintegritet:", stdout)

    def test_doctor_deep_reports_missing_unreadable_and_wrong_hash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            good_path = target / "2024" / "01" / "good.jpg"
            changed_path = target / "2024" / "01" / "changed.jpg"
            unreadable_path = target / "2024" / "01" / "unreadable.jpg"
            for path, content in (
                (good_path, b"good"),
                (changed_path, b"before"),
                (unreadable_path, b"unreadable"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                register_target_file(target, path.relative_to(target))

            changed_path.write_bytes(b"after")
            conn = db.connect(target)
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename,
                        stored_filename, sha256, size_bytes, date_source
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2024/01/missing.jpg",
                        db.relative_path_key(Path("2024/01/missing.jpg")),
                        "missing.jpg",
                        "missing.jpg",
                        "missing-file-sha256",
                        123,
                        "filename",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            database_path = target / DB_FILENAME
            database_before = database_path.read_bytes()

            def hash_or_fail(path: Path) -> str:
                if path.name == "unreadable.jpg":
                    raise OSError("ingen lesetilgang")
                return sha256_file(path)

            with (
                patch("bildebank.cli.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
                patch("bildebank.cli.python_module_available", return_value=False),
                patch("bildebank.cli.sha256_file", side_effect=hash_or_fail),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "doctor", "--deep"]
                )

            database_after = database_path.read_bytes()

        self.assertEqual(code, 0, stderr)
        self.assertIn("Dyp filintegritet:", stdout)
        self.assertIn("Doctor SHA-256: kontrollert=1/4", stdout)
        self.assertIn("Doctor SHA-256: kontrollert=4/4", stdout)
        self.assertIn(
            "Doctor SHA-256: ferdig kontrollert 4/4 filer.",
            stdout,
        )
        self.assertIn("INFO: aktive databasefiler kontrollert: 4", stdout)
        self.assertIn("FEIL: 1 aktiv(e) fil(er) mangler på disk.", stdout)
        self.assertIn("FEIL: 1 aktiv(e) fil(er) kunne ikke leses.", stdout)
        self.assertIn("unreadable.jpg (ingen lesetilgang)", stdout)
        self.assertIn("FEIL: 1 aktiv(e) fil(er) har feil SHA-256.", stdout)
        self.assertIn("changed.jpg", stdout)
        self.assertEqual(database_after, database_before)

    def test_doctor_reports_orphan_file_in_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            registered_path = target / "2024" / "01" / "registered.jpg"
            registered_path.parent.mkdir(parents=True)
            registered_path.write_bytes(b"registered")
            register_target_file(
                target,
                registered_path.relative_to(target),
            )
            orphan_path = target / "2024" / "01" / "orphan.jpg"
            orphan_path.write_bytes(b"orphan")
            (target / "2024" / "01" / "notes.txt").write_text(
                "ikke media",
                encoding="utf-8",
            )

            with (
                patch("bildebank.cli.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
                patch("bildebank.cli.python_module_available", return_value=False),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "doctor"]
                )

        self.assertEqual(code, 0, stderr)
        self.assertNotIn("Dyp filintegritet:", stdout)
        self.assertNotIn("Doctor SHA-256:", stdout)
        self.assertIn("Doctor orphan: scannet=1", stdout)
        self.assertIn(
            "Doctor orphan: ferdig scannet 2 mediefiler.",
            stdout,
        )
        self.assertIn(
            "FEIL: 1 orphan-fil(er) finnes i samlingen uten databasepost.",
            stdout,
        )
        self.assertIn("INFO: orphan: 2024/01/orphan.jpg", stdout)
        self.assertNotIn("orphan: 2024/01/registered.jpg", stdout)
        self.assertNotIn("orphan: 2024/01/notes.txt", stdout)

    def test_face_config_creates_config_file(self) -> None:
        code, stdout, stderr = capture_cli(["face-config", "true"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Ansiktsgjenkjenning er satt til på.", stdout)
        config = load_config(self.program_root)
        self.assertTrue(config.face_recognition.enabled)

    def test_config_enables_face_recognition(self) -> None:
        code, stdout, stderr = capture_cli(["config", "face_recognition", "enable"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("face_recognition.enabled er satt til true.", stdout)
        self.assertIn("Config-fil:", stdout)
        config = load_config(self.program_root)
        self.assertTrue(config.face_recognition.enabled)

    def test_config_updates_image_search_without_changing_other_fields(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[face_recognition]
enabled = true
provider = "cpu"
model_root = "models/insightface"
database_dir = ".faces-by-model"
model_name = "buffalo_s"

[openclip]
enabled = true
model_root = "models/openclip"
device = "cpu"
model_name = "ViT-L-14"
pretrained = "laion2b_s32b_b82k"
""",
            encoding="utf-8",
        )

        code, stdout, stderr = capture_cli(["config", "image_search", "disable"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("image_search.enabled er satt til false.", stdout)
        config = load_config(self.program_root)
        self.assertTrue(config.face_recognition.enabled)
        self.assertEqual(config.face_recognition.model_name, "buffalo_s")
        self.assertFalse(config.openclip.enabled)
        self.assertEqual(config.openclip.model_root, self.program_root / "models" / "openclip")
        self.assertEqual(config.openclip.device, "cpu")
        self.assertEqual(config.openclip.model_name, "ViT-L-14")
        self.assertEqual(config.openclip.pretrained, "laion2b_s32b_b82k")
        config_text = (self.program_root / "bildebank-config.toml").read_text(encoding="utf-8")
        self.assertIn("[image_search]", config_text)
        self.assertNotIn("[openclip]", config_text)

        code, stdout, stderr = capture_cli(["config", "image_search", "enable"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("image_search.enabled er satt til true.", stdout)
        self.assertTrue(load_config(self.program_root).openclip.enabled)

    def test_config_rejects_unknown_section_without_writing_file(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(["config", "unknown", "enable"])

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("invalid choice", stderr.getvalue())
        self.assertFalse((self.program_root / "bildebank-config.toml").exists())

    def test_face_config_updates_existing_config_file(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[face_recognition]
enabled = true
provider = "cpu"
model_root = "models/insightface"
database_dir = ".faces-by-model"
model_name = "buffalo_s"

[openclip]
enabled = true
model_root = "models/openclip"
device = "cpu"
model_name = "ViT-L-14"
pretrained = "laion2b_s32b_b82k"
""",
            encoding="utf-8",
        )

        code, stdout, stderr = capture_cli(["face-config", "false"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Ansiktsgjenkjenning er satt til av.", stdout)
        config = load_config(self.program_root)
        self.assertFalse(config.face_recognition.enabled)
        self.assertEqual(config.face_recognition.provider, "cpu")
        self.assertEqual(config.face_recognition.model_root, self.program_root / "models" / "insightface")
        self.assertEqual(config.face_recognition.database_dir, Path(".faces-by-model"))
        self.assertEqual(config.face_recognition.model_name, "buffalo_s")
        self.assertTrue(config.openclip.enabled)
        self.assertEqual(config.openclip.model_root, self.program_root / "models" / "openclip")

    def test_face_status_uses_explicit_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "doctor"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Aktiv bildesamling:", stdout)
            self.assertIn(str(target.resolve()), stdout)

    def test_face_status_does_not_migrate_openclip_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            old_target = root / "old-target"
            old_image = old_target / "2024" / "01" / "IMG_20240102.jpg"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(openclip_db_path(target))
            try:
                conn.executescript(
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
                    """
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('target_path', ?)",
                    (str(old_target),),
                )
                conn.execute(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        str(old_image),
                        str(old_image),
                        "sha",
                        "ViT-B-32",
                        "laion2b_s34b_b79k",
                        b"embedding",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "doctor"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("bilde-embeddings: 1", stdout)
            conn = sqlite3.connect(openclip_db_path(target))
            try:
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'target_path'").fetchone()[0],
                    str(old_target),
                )
                self.assertEqual(
                    conn.execute("SELECT target_path FROM image_embeddings").fetchone()[0],
                    str(old_image),
                )
            finally:
                conn.close()

    def test_load_config_reads_local_face_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bildebank-config.toml").write_text(
                """
[face_recognition]
enabled = true
provider = "cpu"
model_root = "models/insightface"
database_dir = "faces"
model_name = "buffalo_s"

[openclip]
enabled = true
model_root = "models/openclip"
device = "cpu"
model_name = "ViT-L-14"
pretrained = "laion2b_s32b_b82k"
""",
                encoding="utf-8",
            )

            config = load_config(root)

            self.assertTrue(config.face_recognition.enabled)
            self.assertEqual(config.face_recognition.provider, "cpu")
            self.assertEqual(config.face_recognition.model_root, root / "models" / "insightface")
            self.assertEqual(config.face_recognition.database_dir, Path("faces"))
            self.assertEqual(config.face_recognition.model_name, "buffalo_s")
            self.assertTrue(config.openclip.enabled)
            self.assertEqual(config.openclip.model_root, root / "models" / "openclip")
            self.assertEqual(config.openclip.device, "cpu")
            self.assertEqual(config.openclip.model_name, "ViT-L-14")
            self.assertEqual(config.openclip.pretrained, "laion2b_s32b_b82k")
            config_text = (root / "bildebank-config.toml").read_text(encoding="utf-8")
            self.assertIn("[image_search]", config_text)
            self.assertNotIn("[openclip]", config_text)

    def test_load_config_prefers_image_search_over_openclip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bildebank-config.toml").write_text(
                """
[openclip]
enabled = true
model_root = "models/openclip"
device = "cpu"
model_name = "ViT-L-14"
pretrained = "laion2b_s32b_b82k"

[image_search]
enabled = false
""",
                encoding="utf-8",
            )

            config = load_config(root)

            self.assertFalse(config.openclip.enabled)
            self.assertEqual(config.openclip.model_root, root / "models" / "openclip")
            self.assertEqual(config.openclip.device, "cpu")
            self.assertEqual(config.openclip.model_name, "ViT-L-14")
            self.assertEqual(config.openclip.pretrained, "laion2b_s32b_b82k")

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

    def test_update_runs_update_script_without_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            update_script = repo / "update.ps1"
            update_script.write_text("# update\n", encoding="utf-8")

            with (
                patch("bildebank.cli.sys.platform", "win32"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch("bildebank.cli.subprocess.run") as subprocess_run,
            ):
                subprocess_run.return_value.returncode = 7

                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 7)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "")
            subprocess_run.assert_called_once_with(
                [
                    "powershell.exe",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(update_script),
                ],
                check=False,
            )

    def test_update_runs_linux_update_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
            venv_python = repo / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("# python\n", encoding="utf-8")

            with (
                patch("bildebank.cli.sys.platform", "linux"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch("bildebank.cli.subprocess.run") as subprocess_run,
            ):
                subprocess_run.return_value.returncode = 0
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 0)
            self.assertIn("Ferdig", stdout)
            self.assertEqual(stderr, "")
            self.assertEqual(subprocess_run.call_count, 2)
            subprocess_run.assert_any_call(["git", "pull", "--ff-only"], cwd=repo, check=False)
            subprocess_run.assert_any_call(
                [str(venv_python), "-m", "pip", "install", "-e", "."],
                cwd=repo,
                check=False,
            )

    def test_update_creates_linux_venv_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")

            with (
                patch("bildebank.cli.sys.platform", "linux"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch("bildebank.cli.shutil.which", return_value="/usr/bin/python3.13"),
                patch("bildebank.cli.subprocess.run") as subprocess_run,
            ):
                subprocess_run.return_value.returncode = 0
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 0)
            self.assertIn("Ferdig", stdout)
            self.assertEqual(stderr, "")
            subprocess_run.assert_any_call(
                ["/usr/bin/python3.13", "-m", "venv", ".venv"],
                cwd=repo,
                check=False,
            )

    def test_update_reports_missing_update_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            with (
                patch("bildebank.cli.sys.platform", "win32"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Fant ikke update.ps1", stderr)

    def test_update_reports_missing_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "update.ps1").write_text("# update\n", encoding="utf-8")

            with (
                patch("bildebank.cli.sys.platform", "win32"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch("bildebank.cli.subprocess.run", side_effect=FileNotFoundError),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Fant ikke PowerShell", stderr)

    def test_import_accepts_path_with_accidental_trailing_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, str(source) + '"']),
                0,
            )

    def test_wsl_path_from_windows_path_maps_drive_path(self) -> None:
        if os.name == "nt":
            self.skipTest("WSL path mapping is only used outside Windows")

        self.assertEqual(
            wsl_path_from_windows_path(r"C:\Users\TA487\kode\usbA"),
            Path("/mnt/c/Users/TA487/kode/usbA"),
        )

    def test_show_source_displays_origin_for_imported_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(["--target", str(target), "show-source", str(imported)])

            self.assertEqual(code, 0, stderr)
            self.assertIn(f"Målfil: {imported.resolve()}", stdout)
            self.assertIn(f"Kildefil: {source_file.resolve()}", stdout)
            self.assertIn("Kildefil finnes: ja", stdout)
            self.assertIn("Kilde-id: 1", stdout)
            self.assertIn("Kilde: source", stdout)
            self.assertIn("Originalt filnavn: IMG_20240102.jpg", stdout)
            self.assertIn("Lagret filnavn: IMG_20240102.jpg", stdout)
            self.assertIn("Dato: 2024-01-02 (filename)", stdout)
            self.assertIn("SHA-256:", stdout)

    def test_show_source_resolves_relative_path_under_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            workdir = root / "workdir"
            source.mkdir()
            workdir.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            old_cwd = Path.cwd()
            try:
                os.chdir(workdir)
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "show-source", "2024/01/IMG_20240102.jpg"]
                )
            finally:
                os.chdir(old_cwd)

            self.assertEqual(code, 0, stderr)
            self.assertIn(f"Målfil: {imported.resolve()}", stdout)
            self.assertIn(f"Kildefil: {source_file.resolve()}", stdout)

    def test_check_source_reports_imported_folder_as_safe_without_logging_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
            finally:
                conn.close()

            with patch("bildebank.cli.open_check_source_missing_report"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "check-source", "--quiet", str(source)]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("scannet=1, dekket=1, mangler=0", stdout)
            self.assertIn("validert med SHA-256", stdout)
            self.assertIn("Bildebank sletter ikke kildemapper.", stdout)
            self.assertIn("Remove-Item -LiteralPath", stdout)
            self.assertNotIn("-Recurse", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                commands_after = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(commands_after, commands_before)

    def test_check_source_progress_counts_files_before_checking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            (source / "IMG_20240103.jpg").write_bytes(b"image-two")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )

            code, stdout, stderr = capture_cli(["--target", str(target), "check-source", str(source)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("scannet=2, dekket=2, mangler=0", stdout)
            self.assertIn(f"Check-source: leser filoversikt for {source.resolve()}.", stderr)
            self.assertIn(f"Check-source: fant 2 filer i {source.resolve()}.", stderr)
            self.assertIn("Check-source: kontrollert=1/2", stderr)
            self.assertIn("Check-source: kontrollert=2/2", stderr)
            self.assertIn("gjenstår=", stderr)

    def test_check_source_reports_unimported_file_as_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            imported = source / "IMG_20240102.jpg"
            missing = source / "notes.txt"
            imported.write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            missing.write_bytes(b"not-imported")

            with patch("bildebank.cli.open_check_source_missing_report"):
                code, stdout, stderr = capture_cli(["--target", str(target), "check-source", "--quiet", str(source)])

            self.assertEqual(code, 2, stderr)
            self.assertIn("scannet=2, dekket=1, mangler=1", stdout)
            self.assertIn(str(missing), stdout)
            self.assertIn("filen er ikke importert i bildesamlingen", stdout)
            self.assertIn("Kildemappen er derfor ikke trygg å slette.", stdout)
            self.assertNotIn("Remove-Item", stdout)

    def test_check_source_ignores_google_json_sidecars_but_reports_other_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            image = source / "IMG_20240102.jpg"
            sidecar = source / "IMG_20240102.jpg.json"
            other_json = source / "album.json"
            image.write_bytes(b"image")
            sidecar.write_text('{"title":"IMG_20240102.jpg"}', encoding="utf-8")
            other_json.write_text("{}", encoding="utf-8")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            opened: list[Path] = []

            def fake_open(path: Path) -> None:
                opened.append(path)

            with patch("bildebank.cli.open_check_source_missing_report", fake_open):
                code, stdout, stderr = capture_cli(["--target", str(target), "check-source", "--quiet", str(source)])

            self.assertEqual(code, 2, stderr)
            self.assertIn("scannet=2, dekket=1, mangler=1, slettet=0, ignorert_json=1", stdout)
            self.assertNotIn(str(sidecar), stdout)
            self.assertIn(str(other_json), stdout)
            self.assertEqual(opened[0].read_text(encoding="utf-8"), f"{other_json}\n")
            opened[0].unlink()

    def test_check_source_writes_and_opens_missing_file_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            imported = source / "IMG_20240102.jpg"
            missing = source / "notes.txt"
            imported.write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            missing.write_bytes(b"not-imported")

            opened: list[Path] = []

            def fake_open(path: Path) -> None:
                opened.append(path)

            with patch("bildebank.cli.open_check_source_missing_report", fake_open):
                code, stdout, stderr = capture_cli(["--target", str(target), "check-source", "--quiet", str(source)])

            self.assertEqual(code, 2, stderr)
            self.assertEqual(len(opened), 1)
            self.assertIn("Liste over problemfiler er lagret i:", stdout)
            self.assertEqual(opened[0].read_text(encoding="utf-8"), f"{missing}\n")
            opened[0].unlink()

    def test_check_source_does_not_open_missing_file_list_when_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            with patch("bildebank.cli.open_check_source_missing_report") as open_report:
                code, stdout, stderr = capture_cli(["--target", str(target), "check-source", "--quiet", str(source)])

            self.assertEqual(code, 0, stderr)
            open_report.assert_not_called()
            self.assertNotIn("Liste over problemfiler", stdout)

    def test_check_source_accepts_unknown_extension_when_hash_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source1 = root / "source1"
            source2 = root / "source2"
            source1.mkdir()
            source2.mkdir()
            (source1 / "IMG_20240102.jpg").write_bytes(b"same")
            (source2 / "same-content.unknown").write_bytes(b"same")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source1.name, "--quiet", str(source1)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "check-source", "--quiet", str(source2)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("scannet=1, dekket=1, mangler=0", stdout)
            self.assertIn("Alle filer i kildemappen finnes i bildesamlingen", stdout)

    def test_check_source_accepts_duplicate_source_file_when_hash_exists_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source1 = root / "source1"
            source2 = root / "source2"
            source1.mkdir()
            source2.mkdir()
            (source1 / "IMG_20240102.jpg").write_bytes(b"same")
            (source2 / "COPY_20240203.jpg").write_bytes(b"same")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source1.name, "--quiet", str(source1)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "check-source", "--quiet", str(source2)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("scannet=1, dekket=1, mangler=0", stdout)

    def test_check_source_does_not_count_deleted_file_as_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            self.assertEqual(run_cli(["--target", str(target), "remove", str(imported)]), 0)

            opened: list[Path] = []

            def fake_open(path: Path) -> None:
                opened.append(path)

            with patch("bildebank.cli.open_check_source_missing_report", fake_open):
                code, stdout, stderr = capture_cli(["--target", str(target), "check-source", "--quiet", str(source)])

            self.assertEqual(code, 2, stderr)
            self.assertEqual(len(opened), 1)
            self.assertIn("scannet=1, dekket=0, mangler=0, slettet=1", stdout)
            self.assertIn(f"{source / 'IMG_20240102.jpg'} [deleted/]", stdout)
            self.assertIn("deleted/2024/01/IMG_20240102.jpg", stdout)
            self.assertEqual(opened[0].read_text(encoding="utf-8"), f"{source / 'IMG_20240102.jpg'} [deleted/]\n")
            opened[0].unlink()

    def test_check_source_accepts_deleted_file_with_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            self.assertEqual(run_cli(["--target", str(target), "remove", str(imported)]), 0)

            with patch("bildebank.cli.open_check_source_missing_report") as open_report:
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "check-source", "--accept-deleted", "--quiet", str(source)]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("scannet=1, dekket=0, mangler=0, slettet=1", stdout)
            self.assertIn("deleted/ og er validert med SHA-256", stdout)
            self.assertNotIn("Problemer:", stdout)
            open_report.assert_not_called()

    def test_check_source_marks_deleted_file_when_deleted_copy_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            self.assertEqual(run_cli(["--target", str(target), "remove", str(imported)]), 0)
            deleted.write_bytes(b"changed")

            with patch("bildebank.cli.open_check_source_missing_report"):
                code, stdout, stderr = capture_cli(["--target", str(target), "check-source", "--quiet", str(source)])

            self.assertEqual(code, 2, stderr)
            self.assertIn("scannet=1, dekket=0, mangler=0, slettet=1", stdout)
            self.assertIn("målfeil=1", stdout)
            self.assertIn(f"{source / 'IMG_20240102.jpg'} [deleted/]", stdout)
            self.assertIn("deleted/-filen mangler eller har endret innhold", stdout)

    def test_check_source_reports_corrupt_target_file_as_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            imported.write_bytes(b"changed")

            code, stdout, stderr = capture_cli(["--target", str(target), "check-source", "--quiet", str(source)])

            self.assertEqual(code, 2, stderr)
            self.assertIn("scannet=1, dekket=0, mangler=0", stdout)
            self.assertIn("målfeil=1", stdout)
            self.assertIn("matchende målfil mangler eller har endret innhold", stdout)

    def test_remove_moves_file_marks_database_and_hides_from_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Flyttet til slettet mappe", stdout)
            self.assertFalse(imported.exists())
            self.assertTrue(deleted.exists())
            self.assertEqual(deleted.read_bytes(), b"image-one")

            conn = sqlite3.connect(target / DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT * FROM files").fetchone()
                self.assertEqual(Path(row["target_path"]), deleted.relative_to(target))
                self.assertEqual(Path(row["deleted_original_target_path"]), imported.relative_to(target))
                self.assertIsNotNone(row["deleted_at"])
            finally:
                conn.close()

            self.assertEqual(run_cli(["--target", str(target), "make-browser"]), 0)
            html = (target / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("IMG_20240102.jpg", html)

            code, stdout, stderr = capture_cli(["--target", str(target), "list-removed"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("ja\t2024-01-02\tfilename", stdout)
            self.assertIn(str(imported.resolve()), stdout)
            self.assertIn(f"  slettet fil: {deleted.resolve()}", stdout)
            self.assertIn(f"  kildefil: {(source / 'IMG_20240102.jpg').resolve()}", stdout)
            self.assertIn("filstørrelse: 9 bytes (9 bytes)", stdout)
            self.assertIn("sha256:", stdout)

    def test_remove_stops_when_target_is_locked_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

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
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=import\npid=123\n", encoding="utf-8")
            with sqlite3.connect(target / DB_FILENAME) as conn:
                row_before = conn.execute(
                    "SELECT target_path, deleted_at FROM files WHERE id = 1"
                ).fetchone()
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertTrue(imported.exists())
            with sqlite3.connect(target / DB_FILENAME) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT target_path, deleted_at FROM files WHERE id = 1"
                    ).fetchone(),
                    row_before,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0],
                    commands_before,
                )

    def test_remove_holds_lock_while_moving_and_releases_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
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
            lock_path = target / LOCK_FILENAME
            observed_lock: list[bool] = []
            real_move = shutil.move

            def move_with_lock_check(source_path, destination_path):  # noqa: ANN001
                observed_lock.append(lock_path.exists())
                return real_move(source_path, destination_path)

            with patch(
                "bildebank.file_lifecycle.shutil.move",
                side_effect=move_with_lock_check,
            ):
                remove_file_from_browser(target, 1)

            self.assertEqual(observed_lock, [True])
            self.assertFalse(lock_path.exists())

    def test_remove_releases_lock_when_move_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
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
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            lock_path = target / LOCK_FILENAME

            with (
                patch(
                    "bildebank.file_lifecycle.shutil.move",
                    side_effect=OSError("move failed"),
                ),
                self.assertRaisesRegex(OSError, "move failed"),
            ):
                remove_file_from_browser(target, 1)

            self.assertFalse(lock_path.exists())
            self.assertTrue(imported.exists())
            with sqlite3.connect(target / DB_FILENAME) as conn:
                row = conn.execute(
                    "SELECT target_path, deleted_at FROM files WHERE id = 1"
                ).fetchone()
            self.assertEqual(row, ("2024/01/IMG_20240102.jpg", None))

    def test_remove_completes_pending_move_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )

            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]), 0)

            with sqlite3.connect(target / DB_FILENAME) as conn:
                row = conn.execute(
                    "SELECT operation, state, completed_at FROM pending_file_moves"
                ).fetchone()
            self.assertEqual(row[0], "remove")
            self.assertEqual(row[1], "completed")
            self.assertIsNotNone(row[2])

    def test_recovery_completes_remove_after_file_was_moved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            real_move = shutil.move

            def move_then_crash(source_path, destination_path):  # noqa: ANN001
                real_move(source_path, destination_path)
                raise RuntimeError("crash after move")

            with patch("bildebank.file_lifecycle.shutil.move", side_effect=move_then_crash):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]
                )

            self.assertEqual(code, 1)
            self.assertFalse(imported.exists())
            self.assertTrue(deleted.exists())
            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 0, stderr)
            with sqlite3.connect(target / DB_FILENAME) as conn:
                row = conn.execute(
                    """
                    SELECT files.target_path, files.deleted_at, pending_file_moves.state
                    FROM files
                    JOIN pending_file_moves ON pending_file_moves.file_id = files.id
                    """
                ).fetchone()
            self.assertEqual(row[0], "deleted/2024/01/IMG_20240102.jpg")
            self.assertIsNotNone(row[1])
            self.assertEqual(row[2], "completed")

    def test_recovery_aborts_remove_when_file_was_not_moved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            imported = target / "2024" / "01" / "IMG_20240102.jpg"

            with patch("bildebank.file_lifecycle.shutil.move", side_effect=OSError("move failed")):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]
                )

            self.assertEqual(code, 1)
            self.assertTrue(imported.exists())
            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 0, stderr)
            with sqlite3.connect(target / DB_FILENAME) as conn:
                row = conn.execute(
                    """
                    SELECT files.target_path, files.deleted_at, pending_file_moves.state
                    FROM files
                    JOIN pending_file_moves ON pending_file_moves.file_id = files.id
                    """
                ).fetchone()
            self.assertEqual(row[0], "2024/01/IMG_20240102.jpg")
            self.assertIsNone(row[1])
            self.assertEqual(row[2], "aborted")

    def test_recovery_stops_when_both_move_paths_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            deleted.parent.mkdir(parents=True)
            deleted.write_bytes(b"image-one")
            with sqlite3.connect(target / DB_FILENAME) as conn:
                db.create_pending_file_move(
                    conn,
                    file_id=1,
                    target_root=target,
                    from_path=imported,
                    to_path=deleted,
                    sha256=sha256_file(imported),
                    operation="remove",
                )
                conn.commit()

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 1)
            self.assertIn("både kilde og mål finnes", stderr)

    def test_recovery_stops_when_moved_file_hash_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            deleted.parent.mkdir(parents=True)
            shutil.move(str(imported), str(deleted))
            deleted.write_bytes(b"changed")
            with sqlite3.connect(target / DB_FILENAME) as conn:
                db.create_pending_file_move(
                    conn,
                    file_id=1,
                    target_root=target,
                    from_path=imported,
                    to_path=deleted,
                    sha256="0" * 64,
                    operation="remove",
                )
                conn.commit()

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 1)
            self.assertIn("forventet " + "0" * 64, stderr)

    def test_undelete_restores_removed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            code, stdout, stderr = capture_cli(
                ["--target", str(target), "undelete", "deleted/2024/01/IMG_20240102.jpg"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Flyttet tilbake til bildesamlingen", stdout)
            self.assertTrue(imported.exists())
            self.assertFalse(deleted.exists())
            self.assertEqual(imported.read_bytes(), b"image-one")
            conn = sqlite3.connect(target / DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT target_path, deleted_at, deleted_original_target_path FROM files").fetchone()
            finally:
                conn.close()
            self.assertEqual(row["target_path"], "2024/01/IMG_20240102.jpg")
            self.assertIsNone(row["deleted_at"])
            self.assertIsNone(row["deleted_original_target_path"])
            self.assertIsNotNone(browser_item_by_id(target, 1))

    def test_undelete_stops_when_target_is_locked_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
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
                        "2024/01/IMG_20240102.jpg",
                    ]
                ),
                0,
            )
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=import\npid=123\n", encoding="utf-8")
            with sqlite3.connect(target / DB_FILENAME) as conn:
                row_before = conn.execute(
                    "SELECT target_path, deleted_at FROM files WHERE id = 1"
                ).fetchone()
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "undelete",
                    "deleted/2024/01/IMG_20240102.jpg",
                ]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertTrue(deleted.exists())
            with sqlite3.connect(target / DB_FILENAME) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT target_path, deleted_at FROM files WHERE id = 1"
                    ).fetchone(),
                    row_before,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0],
                    commands_before,
                )

    def test_undelete_holds_lock_while_moving_and_releases_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
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
                        "2024/01/IMG_20240102.jpg",
                    ]
                ),
                0,
            )
            lock_path = target / LOCK_FILENAME
            observed_lock: list[bool] = []
            real_move = shutil.move

            def move_with_lock_check(source_path, destination_path):  # noqa: ANN001
                observed_lock.append(lock_path.exists())
                return real_move(source_path, destination_path)

            with patch(
                "bildebank.file_lifecycle.shutil.move",
                side_effect=move_with_lock_check,
            ):
                undelete_file_from_browser(target, 1)

            self.assertEqual(observed_lock, [True])
            self.assertFalse(lock_path.exists())

    def test_undelete_rejects_original_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "undelete", "2024/01/IMG_20240102.jpg"]
            )

            self.assertNotEqual(code, 0)
            self.assertIn("Undelete krever sti under deleted/", stderr)
            self.assertFalse((target / "2024" / "01" / "IMG_20240102.jpg").exists())
            self.assertTrue((target / "deleted" / "2024" / "01" / "IMG_20240102.jpg").exists())

    def test_undelete_fails_when_destination_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]), 0)
            destination = target / "2024" / "01" / "IMG_20240102.jpg"
            destination.write_bytes(b"already-here")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "undelete", "deleted/2024/01/IMG_20240102.jpg"]
            )

            self.assertNotEqual(code, 0)
            self.assertIn("Målfilen finnes allerede", stderr)
            self.assertEqual(destination.read_bytes(), b"already-here")
            self.assertTrue((target / "deleted" / "2024" / "01" / "IMG_20240102.jpg").exists())

    def test_undelete_fails_when_deleted_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]), 0)
            (target / "deleted" / "2024" / "01" / "IMG_20240102.jpg").unlink()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "undelete", "deleted/2024/01/IMG_20240102.jpg"]
            )

            self.assertNotEqual(code, 0)
            self.assertIn("Slettet fil finnes ikke på disk", stderr)
            self.assertFalse((target / "2024" / "01" / "IMG_20240102.jpg").exists())

    def test_undelete_fails_when_database_row_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            deleted.parent.mkdir(parents=True)
            deleted.write_bytes(b"image-one")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "undelete", "deleted/2024/01/IMG_20240102.jpg"]
            )

            self.assertNotEqual(code, 0)
            self.assertIn("Filen finnes ikke i importdatabasen", stderr)
            self.assertTrue(deleted.exists())

    def test_import_dry_run_lists_files_without_database_or_copy_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", source.name, "--dry-run", "--quiet", str(source)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertNotIn("IMPORT\t", stdout)
            self.assertNotIn(str(source_file.resolve()), stdout)
            self.assertNotIn(str((target / "2024" / "01" / "IMG_20240102.jpg").resolve()), stdout)
            self.assertIn("importert=1", stdout)
            self.assertFalse((target / "2024").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
                commands_after = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
                self.assertEqual(commands_after, commands_before)
            finally:
                conn.close()

    def test_rescan_source_imports_new_supported_files_without_new_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"jpeg")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            (source / "IMG_20240103.nef").write_bytes(b"raw-photo")

            code, stdout, stderr = capture_cli(["--target", str(target), "rescan-source", "--name", source.name, "--quiet"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("importert=1", stdout)
            self.assertIn("eksisterende=1", stdout)
            imported_raw = target / "2024" / "01" / "IMG_20240103.nef"
            self.assertEqual(imported_raw.read_bytes(), b"raw-photo")
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 2)
                raw_source = conn.execute(
                    """
                    SELECT sources.name
                    FROM file_sources
                    JOIN sources ON sources.id = file_sources.source_id
                    WHERE file_sources.source_path LIKE ?
                    """,
                    ("%IMG_20240103.nef",),
                ).fetchone()[0]
                self.assertEqual(raw_source, source.name)
            finally:
                conn.close()

    def test_rescan_source_dry_run_does_not_copy_or_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"jpeg")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            (source / "IMG_20240103.nef").write_bytes(b"raw-photo")
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "rescan-source", "--name", source.name, "--dry-run", "--quiet"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("importert=1", stdout)
            self.assertFalse((target / "2024" / "01" / "IMG_20240103.nef").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                commands_after = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
                self.assertEqual(commands_after, commands_before)
            finally:
                conn.close()

    def test_rescan_source_records_duplicate_without_copying_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source_a = root / "source-a"
            source_b = root / "source-b"
            source_a.mkdir()
            source_b.mkdir()
            (source_a / "IMG_20240102.jpg").write_bytes(b"same")
            (source_b / "placeholder_20240101.jpg").write_bytes(b"placeholder")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source_a.name, "--quiet", str(source_a)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source_b.name, "--quiet", str(source_b)]), 0)
            (source_b / "COPY_20240102.nef").write_bytes(b"same")

            code, stdout, stderr = capture_cli(["--target", str(target), "rescan-source", "--name", source_b.name, "--quiet"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("duplikater=1", stdout)
            self.assertFalse((target / "2024" / "01" / "COPY_20240102.nef").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 3)
                source_names = [
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT sources.name
                        FROM file_sources
                        JOIN sources ON sources.id = file_sources.source_id
                        WHERE file_sources.sha256 = (SELECT sha256 FROM files WHERE stored_filename = 'IMG_20240102.jpg')
                        ORDER BY sources.name
                        """
                    )
                ]
                self.assertEqual(source_names, ["source-a", "source-b"])
            finally:
                conn.close()

    def test_import_stops_when_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=import\npid=123\n", encoding="utf-8")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertIn(str(lock_path), stderr)
            self.assertTrue(lock_path.exists())
            self.assertFalse((target / "2024").exists())

    def test_duplicate_is_recorded_not_copied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source1 = root / "source1"
            source2 = root / "source2"
            source1.mkdir()
            source2.mkdir()
            (source1 / "IMG_20240102.jpg").write_bytes(b"same")
            (source2 / "COPY_20240203.jpg").write_bytes(b"same")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source1.name, "--quiet", str(source1)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source2.name, "--quiet", str(source2)]), 0)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 2)
                self.assertFalse(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'duplicate_findings'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_unimport_duplicate_source_keeps_shared_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source1 = root / "source1"
            source2 = root / "source2"
            source1.mkdir()
            source2.mkdir()
            (source1 / "IMG_20240102.jpg").write_bytes(b"same")
            (source2 / "COPY_20240203.jpg").write_bytes(b"same")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source1.name, "--quiet", str(source1)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source2.name, "--quiet", str(source2)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", source2.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Unimport: kildefiler=1/1", stdout)
            self.assertIn("Unimport: målfiler=0/0", stdout)
            self.assertIn("Filer som fjernes fra aktiv samling: 0", stdout)
            self.assertIn("Filer som blir liggende fordi de også finnes i andre kilder: 1", stdout)
            self.assertTrue(imported.exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                self.assertIsNone(conn.execute(
                    "SELECT id FROM sources WHERE path = ?",
                    (str(source2.resolve()),),
                ).fetchone())
            finally:
                conn.close()

    def test_unimport_only_source_removes_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            self.assertTrue(imported.exists())

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", source.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Unimport: kildefiler=1/1", stdout)
            self.assertIn("Unimport: målfiler=1/1", stdout)
            self.assertIn("Filer som fjernes fra aktiv samling: 1", stdout)
            self.assertIn("Kilden er fjernet fra kildelisten.", stdout)
            self.assertFalse(imported.exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
            finally:
                conn.close()

    def test_unimport_dry_run_reports_directory_plan_without_changes_or_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"

            with patch("builtins.input", side_effect=AssertionError("dry-run should not ask")):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--dry-run", "--name", source.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Unimport: kildefiler=1/1", stdout)
            self.assertIn("Unimport: målfiler=1/1", stdout)
            self.assertIn("Filer som fjernes fra aktiv samling: 1", stdout)
            self.assertIn("Kilden ville blitt fjernet fra kildelisten.", stdout)
            self.assertIn("Dry-run: ingen endringer er gjort.", stdout)
            self.assertTrue(imported.exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                status = conn.execute("SELECT status FROM sources").fetchone()[0]
                self.assertEqual(status, "imported")
                commands = [
                    row[0]
                    for row in conn.execute("SELECT command FROM command_log ORDER BY id").fetchall()
                ]
                self.assertEqual(commands, ["create", "import"])
            finally:
                conn.close()

    def test_unimport_plan_does_not_count_sources_per_file(self) -> None:
        class CountingConnection:
            def __init__(self, conn: sqlite3.Connection) -> None:
                self.conn = conn
                self.file_source_count_queries = 0

            def execute(self, sql: str, parameters: Iterable[object] = ()):
                normalized = " ".join(sql.split()).lower()
                if normalized.startswith("select count(*) from file_sources where file_id"):
                    self.file_source_count_queries += 1
                return self.conn.execute(sql, tuple(parameters))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            for index in range(3):
                (source / f"IMG_2024010{index + 1}.jpg").write_bytes(f"image-{index}".encode("ascii"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            conn = sqlite3.connect(target / DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                source_row = db.find_source_by_name(conn, source.name)
                assert source_row is not None
                counting_conn = CountingConnection(conn)
                plan = db.build_unimport_plan(counting_conn, target, source_row)  # type: ignore[arg-type]
            finally:
                conn.close()

        self.assertEqual(plan.source_file_count, 3)
        self.assertEqual(plan.active_remove_count, 3)
        self.assertEqual(counting_conn.file_source_count_queries, 0)

    def test_unimport_aborts_without_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"

            with patch("builtins.input", return_value="ja"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", source.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Avbrutt. Ingen endringer er gjort.", stdout)
            self.assertTrue(imported.exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
            finally:
                conn.close()

    def test_unimport_aborts_when_source_file_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            source_file.write_bytes(b"changed")

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", source.name]
                )

            self.assertEqual(code, 1)
            self.assertIn("Unimport: kontrollerer 1 kildefiler.", stdout)
            self.assertIn("Kildefil har endret", stderr)
            self.assertTrue((target / "2024" / "01" / "IMG_20240102.jpg").exists())

    def test_unimport_rejects_superseded_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2006"
            child.mkdir(parents=True)
            (child / "IMG_20061003.jpg").write_bytes(b"child")
            (parent / "IMG_20070104.jpg").write_bytes(b"parent")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", child.name, "--quiet", str(child)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", parent.name, "--quiet", str(parent)]), 0)

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", child.name]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Unimport gjennomført.", stdout)
            self.assertTrue((target / "2006" / "10" / "IMG_20061003.jpg").exists())

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
            self.assertIn("Ny schema_version: 13", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "13",
                )
                file_columns = {row[1] for row in conn.execute("pragma table_info(files)")}
                source_columns = {row[1] for row in conn.execute("pragma table_info(sources)")}
                file_source_columns = {row[1] for row in conn.execute("pragma table_info(file_sources)")}
                self.assertFalse({"source_id", "source_path", "source_path_key"} & file_columns)
                self.assertNotIn("kind", source_columns)
                self.assertNotIn("kind", file_source_columns)
                self.assertEqual(conn.execute("select count(*) from sources where name is null").fetchone()[0], 0)
                self.assertEqual(conn.execute("pragma foreign_key_list(errors)").fetchall(), [])
            finally:
                conn.close()

    def test_show_source_lists_duplicate_sources_for_same_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source1 = root / "source1"
            source2 = root / "source2"
            source1.mkdir()
            source2.mkdir()
            first = source1 / "IMG_20240102.jpg"
            duplicate = source2 / "COPY_20240203.jpg"
            first.write_bytes(b"same")
            duplicate.write_bytes(b"same")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source1.name, "--quiet", str(source1)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source2.name, "--quiet", str(source2)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            code, stdout, stderr = capture_cli(["--target", str(target), "show-source", str(imported)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Kildefiler:", stdout)
            self.assertIn(f"- {first.resolve()}", stdout)
            self.assertIn(f"- {duplicate.resolve()}", stdout)

    def test_status_counts_media_types_and_date_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Totalt: 2", stdout)
            self.assertIn("Bilder: 1", stdout)
            self.assertIn("Videoer: 1", stdout)
            self.assertIn("  metadata: 1", stdout)
            self.assertIn("  filename: 1", stdout)
            self.assertIn("  mtime: 0", stdout)
            self.assertIn("Kilder: 1", stdout)
            self.assertIn("Importerte filer: 2", stdout)
            self.assertIn("Kildefilforekomster: 2", stdout)
            self.assertIn("Duplikatkilder: 0", stdout)
            self.assertIn("Uløste feil: 0", stdout)
            self.assertIn("Navnekollisjoner: 0", stdout)
            self.assertIn("Filer uten dato: 0", stdout)

    def test_parent_source_supersedes_imported_child_without_duplicate_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2006"
            child.mkdir(parents=True)
            (child / "IMG_20061003.jpg").write_bytes(b"child")
            (parent / "IMG_20070104.jpg").write_bytes(b"parent")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", child.name, "--quiet", str(child)]), 0)
            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", parent.name, "--quiet", str(parent)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("duplikater=1", stdout)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 3)
                statuses = conn.execute(
                    "SELECT path, status, superseded_by_source_id FROM sources ORDER BY id"
                ).fetchall()
                self.assertEqual(statuses[0][1], "imported")
                self.assertIsNone(statuses[0][2])
                self.assertEqual(statuses[1][1], "imported")
            finally:
                conn.close()

    def test_overlapping_child_source_after_parent_import_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2007"
            child.mkdir(parents=True)
            (parent / "IMG_20070104.jpg").write_bytes(b"parent")
            (child / "IMG_20070203.jpg").write_bytes(b"child")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", parent.name, "--quiet", str(parent)]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", child.name, "--quiet", str(child)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("duplikater=1", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 3)
            finally:
                conn.close()

    def test_rejects_superseded_child_source_added_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2006"
            child.mkdir(parents=True)
            (child / "IMG_20061003.jpg").write_bytes(b"child")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", child.name, "--quiet", str(child)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", parent.name, "--quiet", str(parent)]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", "child-again", "--quiet", str(child)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("duplikater=1", stdout)

    def test_name_conflict_gets_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            (source / "a" / "IMG_20240102.jpg").write_bytes(b"first")
            (source / "b" / "IMG_20240102.jpg").write_bytes(b"second")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertTrue((target / "2024" / "01" / "IMG_20240102.jpg").exists())
            self.assertTrue((target / "2024" / "01" / "IMG_20240102-1.jpg").exists())

            code, stdout, stderr = capture_cli(["--target", str(target), "conflicts"])
            self.assertEqual(code, 0, stderr)
            self.assertIn(str(target / "2024" / "01" / "IMG_20240102-1.jpg"), stdout)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM files WHERE name_conflict = 1").fetchone()[0],
                    1,
                )
            finally:
                conn.close()

    def test_show_name_conflict_sources_lists_all_files_in_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            first_source = source / "a" / "IMG_20240102.png"
            second_source = source / "b" / "IMG_20240102.png"
            first_source.write_bytes(minimal_png(640, 480))
            second_source.write_bytes(minimal_png(320, 240))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            first_target = target / "2024" / "01" / "IMG_20240102.png"
            second_target = target / "2024" / "01" / "IMG_20240102-1.png"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "show-conflict", str(second_target)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Navnekollisjon: IMG_20240102.png", stdout)
            self.assertIn(str(first_target.resolve()), stdout)
            self.assertIn(str(second_target.resolve()), stdout)
            self.assertIn(str(first_source.resolve()), stdout)
            self.assertIn(str(second_source.resolve()), stdout)
            self.assertIn("oppløsning: 640x480", stdout)
            self.assertIn("oppløsning: 320x240", stdout)
            self.assertIn("dato: 2024-01-02 (filename)", stdout)
            self.assertIn("sha256:", stdout)
            self.assertIn("kildefil finnes: ja", stdout)

    def test_show_name_conflict_resolves_relative_path_under_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            workdir = root / "workdir"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            workdir.mkdir()
            first_source = source / "a" / "IMG_20240102.png"
            second_source = source / "b" / "IMG_20240102.png"
            first_source.write_bytes(minimal_png(640, 480))
            second_source.write_bytes(minimal_png(320, 240))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            first_target = target / "2024" / "01" / "IMG_20240102.png"
            second_target = target / "2024" / "01" / "IMG_20240102-1.png"
            old_cwd = Path.cwd()
            try:
                os.chdir(workdir)
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "show-conflict", "2024/01/IMG_20240102-1.png"]
                )
            finally:
                os.chdir(old_cwd)

            self.assertEqual(code, 0, stderr)
            self.assertIn("Navnekollisjon: IMG_20240102.png", stdout)
            self.assertIn(str(first_target.resolve()), stdout)
            self.assertIn(str(second_target.resolve()), stdout)

    def test_show_name_conflict_sources_works_for_first_file_in_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            (source / "a" / "IMG_20240102.jpg").write_bytes(b"first")
            (source / "b" / "IMG_20240102.jpg").write_bytes(b"second")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            first_target = target / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "show-conflict", str(first_target)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("IMG_20240102-1.jpg", stdout)

    def test_show_name_conflict_sources_reports_non_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            target_file = target / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "show-conflict", str(target_file)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("ikke del av en navnekollisjon", stdout)

    def test_exiftool_resolver_prefers_explicit_path_then_managed_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            explicit = root / "custom-exiftool.exe"
            managed = managed_exiftool_path(repo)
            write_fake_exiftool(explicit)
            write_fake_exiftool(managed)
            (managed.parent / "exiftool_files").mkdir()

            self.assertEqual(resolve_exiftool_path(repo, explicit), explicit)
            self.assertEqual(resolve_exiftool_path(repo), managed)

    def test_exiftool_resolver_falls_back_to_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path_tool = root / "exiftool"
            write_fake_exiftool(path_tool)

            with patch("bildebank.exiftool.shutil.which", return_value=str(path_tool)):
                self.assertEqual(resolve_exiftool_path(root / "repo"), str(path_tool))

    def test_exiftool_resolver_requires_managed_support_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            write_fake_exiftool(managed_exiftool_path(repo))

            with self.assertRaisesRegex(FileNotFoundError, "exiftool_files"):
                resolve_exiftool_path(repo)

    def test_exiftool_install_downloads_zip_to_managed_tools_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            source_zip = root / "exiftool.zip"
            script = """#!/usr/bin/env python3
import sys
if "-ver" in sys.argv:
    print("13.58")
"""
            with zipfile.ZipFile(source_zip, "w") as archive:
                archive.writestr("exiftool-13.58_64/exiftool(-k).exe", script)
                archive.writestr("exiftool-13.58_64/exiftool_files/ExifTool_config", "config")

            def fake_urlretrieve(url: str, filename: str | Path):
                shutil.copyfile(source_zip, filename)
                return (str(filename), None)

            with (
                patch("bildebank.cli.sys.platform", "win32"),
                patch("bildebank.cli.program_repo_root", return_value=repo),
                patch("bildebank.exiftool.urllib.request.urlretrieve", side_effect=fake_urlretrieve),
            ):
                code, stdout, stderr = capture_cli(["exiftool-install"])

            installed = repo / "bildebank-tools" / "exiftool"
            self.assertEqual(code, 0, stderr)
            self.assertIn("Installerte ExifTool 13.58", stdout)
            self.assertTrue((installed / "exiftool.exe").exists())
            self.assertTrue((installed / "exiftool_files").is_dir())

    def test_exiftool_install_fails_on_linux(self) -> None:
        with patch("bildebank.cli.sys.platform", "linux"):
            code, stdout, stderr = capture_cli(["exiftool-install"])

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertIn("støttes bare på Windows", stderr)
        self.assertIn("libimage-exiftool-perl", stderr)

    def test_exiftool_metadata_gaps_lists_dates_bildebank_does_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            exiftool = root / "exiftool.exe"
            write_fake_exiftool(
                exiftool,
                """import json
print(json.dumps([{"SourceFile": "x", "DateTimeOriginal": "2024:01:02 03:04:05"}]))
""",
            )

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "exiftool-metadata-gaps", "--exiftool", str(exiftool)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("2024-01-02\tDateTimeOriginal", stdout)
            self.assertIn("bildebank=filename:2024-01-02", stdout)
            self.assertIn("IMG_20240102.jpg", stdout)
            self.assertIn("Oppsummering: exiftool_metadata_funnet=1", stdout)
            self.assertIn("exiftool: kontrollert=1/1", stderr)
            self.assertIn("gjenstår=0s", stderr)

    def test_exiftool_metadata_gaps_reads_files_in_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            for index, name in enumerate(("IMG_20240102.jpg", "IMG_20240103.jpg", "IMG_20240104.jpg")):
                (source / name).write_bytes(f"image-{index}".encode("ascii"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            calls = target / "exiftool-calls.txt"
            exiftool = root / "exiftool.exe"
            write_fake_exiftool(
                exiftool,
                f"""import json
import sys
from pathlib import Path
paths = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
with Path({str(calls)!r}).open("a", encoding="utf-8") as fh:
    fh.write("call\\n")
print(json.dumps([
    {{"SourceFile": path, "DateTimeOriginal": "2024:01:02 03:04:05"}}
    for path in paths
]))
""",
            )

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "exiftool-metadata-gaps", "--exiftool", str(exiftool), "--batch-size", "10"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(calls.read_text(encoding="utf-8"), "call\n")
            self.assertIn("Oppsummering: exiftool_metadata_funnet=3", stdout)

    def test_rejects_target_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = source / "target"
            source.mkdir()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, str(source)]),
                1,
            )

    def test_import_records_walk_errors_and_keeps_source_pending_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            blocked = source / "blocked"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"visible")

            def fake_walk(path, *args, onerror=None, **kwargs):
                if onerror is not None:
                    onerror(PermissionError(13, "Permission denied", str(blocked)))
                yield str(path), [], ["IMG_20240102.jpg"]

            self.assertEqual(run_cli(["create", str(target)]), 0)

            with patch("bildebank.importer.os.walk", fake_walk):
                self.assertEqual(
                    run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                    2,
                )

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                error = conn.execute("SELECT stage, source_path FROM errors").fetchone()
                self.assertEqual(error[0], "scan")
                self.assertEqual(error[1], str(blocked))
                status = conn.execute("SELECT status FROM sources").fetchone()[0]
                self.assertEqual(status, "error")
            finally:
                conn.close()

    def test_face_scan_requires_enabled_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--limit", "1"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Ansiktsgjenkjenning er av", stderr)
            self.assertFalse((face_db_path(target)).exists())

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-list"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Ansiktsgjenkjenning er av", stderr)
            self.assertFalse((face_db_path(target)).exists())

    def test_face_scan_reports_insightface_opencv_linux_system_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.enable_face_recognition_config()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            with patch(
                "bildebank.face.load_face_app",
                side_effect=ValueError(
                    "InsightFace er installert, men OpenCV mangler Linux-biblioteket libGL.so.1. "
                    "Installer det i WSL/Linux med `sudo apt install libgl1`."
                ),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--limit", "1"])

            self.assertEqual(code, 1)
            self.assertIn("Face-scan: laster ansiktsmodell.", stdout)
            self.assertNotIn("Oppsummering:", stdout)
            self.assertIn("Feil: InsightFace er installert, men OpenCV mangler Linux-biblioteket libGL.so.1.", stderr)
            self.assertIn("sudo apt install libgl1", stderr)

    def test_face_scan_and_suggest_refuse_to_run_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=import\n", encoding="utf-8")

            scan_code, _scan_stdout, scan_stderr = capture_cli(
                ["--target", str(target), "face-scan", "--limit", "1"]
            )
            suggest_code, _suggest_stdout, suggest_stderr = capture_cli(
                ["--target", str(target), "face-suggest"]
            )

        self.assertEqual(scan_code, 1)
        self.assertIn("Bildesamlingen er låst", scan_stderr)
        self.assertEqual(suggest_code, 1)
        self.assertIn("Bildesamlingen er låst", suggest_stderr)

    def test_image_commands_require_enabled_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "image-scan", "--limit", "1"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Tekstbasert bildesøk er av", stderr)
            self.assertFalse(openclip_db_path(target).exists())

            code, stdout, stderr = capture_cli(["--target", str(target), "image-search", "strand"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Tekstbasert bildesøk er av", stderr)
            self.assertFalse(openclip_db_path(target).exists())

    def test_image_scan_and_search_refuse_to_run_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.enable_openclip_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            scan_code, _scan_stdout, scan_stderr = capture_cli(
                ["--target", str(target), "image-scan", "--limit", "1"]
            )
            search_code, _search_stdout, search_stderr = capture_cli(
                ["--target", str(target), "image-search", "strand", "--no-browser"]
            )

        self.assertEqual(scan_code, 1)
        self.assertIn("Bildesamlingen er låst", scan_stderr)
        self.assertEqual(search_code, 1)
        self.assertIn("Bildesamlingen er låst", search_stderr)

    def test_image_search_passes_browser_option_to_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.enable_openclip_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)

            with patch("bildebank.cli.run_image_search", return_value=0) as run_search:
                self.assertEqual(run_cli(["--target", str(target), "image-search", "strand"]), 0)
                run_search.assert_called_once_with(target.resolve(), query="strand", limit=100, browser=True)

            with patch("bildebank.cli.run_image_search", return_value=0) as run_search:
                self.assertEqual(
                    run_cli(["--target", str(target), "image-search", "strand", "--no-browser"]),
                    0,
                )
                run_search.assert_called_once_with(target.resolve(), query="strand", limit=100, browser=False)

    def test_image_search_progress_uses_progress_meter(self) -> None:
        stdout = StringIO()
        stats = SimpleNamespace(query="strand")

        with redirect_stdout(stdout):
            print_image_search_progress("load_model", 0, 10, stats)
            print_image_search_progress("compare_start", 0, 10, stats)
            print_image_search_progress("compare", 10, 10, stats)
            print_image_search_progress("write", 5, 5, stats)
            print_image_search_progress("done", 5, 5, stats)

        output = stdout.getvalue()
        self.assertIn("Image-search: søker etter \"strand\" i 10 bilder.", output)
        self.assertIn("Image-search: søkt=10/10, gjenstår=0s", output)
        self.assertIn("Image-search: skriver 5 treff til image-search.html.", output)

    def test_face_scan_writes_faces_to_separate_database(self) -> None:
        class FakeFace:
            bbox = [1.0, 2.0, 11.0, 22.0]
            det_score = 0.9
            embedding = [0.1, 0.2, 0.3]

        class FakeApp:
            def get(self, image):
                print("internal model get stdout")
                warnings.warn("internal model warning", FutureWarning, stacklevel=1)
                return [FakeFace()]

        def fake_load_face_app(config):
            print("internal model load stdout")
            print("internal model load stderr", file=sys.stderr)
            return FakeApp()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.enable_face_recognition_config()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )

            with (
                patch("bildebank.face.load_face_app", side_effect=fake_load_face_app),
                patch("bildebank.face.read_image", return_value=object()),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--limit", "1"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Face-scan: 1 bildefiler skal kontrolleres.", stdout)
            self.assertIn("Face-scan: 1 nye eller endrede bilder skal scannes.", stdout)
            self.assertIn("Face-scan: ansiktsmodellen finnes ikke lokalt.", stdout)
            self.assertIn("Face-scan: scannet=1/1", stdout)
            self.assertIn("gjenstår=0s", stdout)
            self.assertNotIn("internal model", stdout)
            self.assertNotIn("internal model", stderr)
            self.assertIn("ansikter=1", stdout)
            face_db = face_db_path(target, load_config(self.program_root).face_recognition)
            self.assertTrue(face_db.exists())
            conn = sqlite3.connect(face_db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0], 1)
                face = conn.execute(
                    "SELECT bbox_x, bbox_y, bbox_width, bbox_height, detection_score, embedding_model FROM faces"
                ).fetchone()
                self.assertEqual(face, (1.0, 2.0, 10.0, 20.0, 0.9, "buffalo_l"))
            finally:
                conn.close()

            with (
                patch("bildebank.face.load_face_app", side_effect=AssertionError("should not load model")),
                patch("bildebank.face.read_image", side_effect=AssertionError("should not read image")),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--limit", "1"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Face-scan: kontrollert=1/1", stdout)
            self.assertIn("hoppet_over=1", stdout)
            self.assertIn("scannet=0", stdout)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-report"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Ansiktsrapport", stdout)
            self.assertIn("Scannede filer: 1", stdout)
            self.assertIn("Ansikter funnet: 1", stdout)
            self.assertIn("Filer med ett ansikt: 1", stdout)
            self.assertIn("Flest ansikter:", stdout)
            self.assertIn("Personstatus:", stdout)
            self.assertIn("Personer registrert: 0", stdout)
            self.assertIn("Bilder med ansikter, men ingen bekreftet person: 1", stdout)
            self.assertIn("IMG_20240102.jpg", stdout)

    def test_face_scan_prints_file_path_when_image_fails(self) -> None:
        class FakeApp:
            def get(self, image):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            bad_image = source / "bad.jpg"
            bad_image.write_bytes(b"image")
            self.enable_face_recognition_config()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )

            with (
                patch("bildebank.face.load_face_app", return_value=FakeApp()),
                patch("bildebank.face.read_image", side_effect=ValueError("Kunne ikke lese testbildet")),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--limit", "1"])

            self.assertEqual(code, 2)
            self.assertIn("Face-scan-feil:", stdout)
            self.assertIn("bad.jpg", stdout)
            self.assertIn("Kunne ikke lese testbildet", stdout)
            self.assertIn("feil=1", stdout)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-report"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Siste scan-feil:", stdout)
            self.assertIn("bad.jpg", stdout)
            self.assertIn("Kunne ikke lese testbildet", stdout)

    def test_face_scan_force_rescans_limited_existing_files(self) -> None:
        class FakeFace:
            det_score = 0.9
            embedding = [0.1, 0.2, 0.3]

            def __init__(self, bbox):
                self.bbox = bbox

        class FakeApp:
            def __init__(self, face):
                self.face = face

            def get(self, image):
                return [self.face]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.enable_face_recognition_config()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )

            with (
                patch("bildebank.face.load_face_app", return_value=FakeApp(FakeFace([1.0, 2.0, 11.0, 22.0]))),
                patch("bildebank.face.read_image", return_value=object()),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--limit", "1"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("scannet=1", stdout)

            face_db = face_db_path(target, load_config(self.program_root).face_recognition)
            conn = sqlite3.connect(face_db)
            conn.row_factory = sqlite3.Row
            try:
                old_face_id = int(conn.execute("SELECT id FROM faces").fetchone()["id"])
                person_id = int(
                    conn.execute("INSERT INTO persons(name) VALUES('Kari') RETURNING id").fetchone()["id"]
                )
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(?, ?)", (person_id, old_face_id))
                conn.execute(
                    "INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(?, ?, 0.8)",
                    (person_id, old_face_id),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch("bildebank.face.load_face_app", return_value=FakeApp(FakeFace([3.0, 4.0, 13.0, 24.0]))),
                patch("bildebank.face.read_image", return_value=object()),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--force", "--limit", "1"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("hoppet_over=0", stdout)
            self.assertIn("scannet=1", stdout)

            conn = sqlite3.connect(face_db)
            try:
                face = conn.execute("SELECT id, bbox_x, bbox_y, bbox_width, bbox_height FROM faces").fetchone()
                self.assertNotEqual(face[0], old_face_id)
                self.assertEqual(face[1:], (3.0, 4.0, 10.0, 20.0))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 0)
            finally:
                conn.close()

    def test_face_report_prints_relative_face_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            image_path = target / "2024" / "01" / "IMG_20240102.jpg"
            relative_image_path = Path("2024/01/IMG_20240102.jpg")

            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            config = load_config(self.program_root).face_recognition

            conn = sqlite3.connect(face_db_path(target, config))
            try:
                apply_face_schema(conn)
                conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, ?, 'sha', 'ok', 1)
                    """,
                    (relative_image_path.as_posix(), db.relative_path_key(relative_image_path)),
                )
                conn.execute(
                    """
                    INSERT INTO faces(
                        file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    ) VALUES(1, ?, 1, 2, 3, 4, 0.9, 'test', ?)
                    """,
                    (db.relative_path_key(relative_image_path), b"embedding"),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "face-report"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("1\t2024/01/IMG_20240102.jpg", stdout)

    def test_face_database_rejects_absolute_target_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            image_path = target / "2024" / "01" / "IMG_20240102.jpg"
            target.mkdir()
            conn = sqlite3.connect(face_db_path(target))
            try:
                apply_face_schema(conn)
                conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, '2024/01/img_20240102.jpg', 'sha', 'ok', 0)
                    """,
                    (str(image_path),),
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(ValueError, "Face-databasen har absolutt target_path"):
                connect_face_db(target).close()

    def test_face_database_path_uses_model_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            config = FaceRecognitionConfig(model_name="antelopev2")

            conn = connect_face_db(target, config)
            try:
                self.assertEqual(face_db_path(target, config), target / ".bildebank-faces" / "antelopev2.sqlite3")
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'model_name'").fetchone()[0], "antelopev2")
            finally:
                conn.close()

    def test_face_database_rejects_model_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            antelope_config = FaceRecognitionConfig(model_name="antelopev2")
            conn = sqlite3.connect(face_db_path(target, antelope_config))
            try:
                apply_face_schema(conn)
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('model_name', 'buffalo_l')"
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(ValueError, "tilhører en annen modell"):
                connect_face_db(target, antelope_config).close()

    def test_face_database_moves_legacy_buffalo_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            legacy_path = target / ".bilder-faces.sqlite3"
            conn = sqlite3.connect(legacy_path)
            try:
                apply_face_schema(conn)
                conn.commit()
            finally:
                conn.close()

            new_path = face_db_path(target, FaceRecognitionConfig(model_name="buffalo_l"))

            self.assertFalse(legacy_path.exists())
            self.assertTrue(new_path.exists())

    def test_normalize_insightface_model_layout_moves_nested_onnx_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / ".bildebank-insightface" / "models" / "antelopev2" / "antelopev2"
            nested.mkdir(parents=True)
            (nested / "scrfd_10g_bnkps.onnx").write_bytes(b"detector")
            (nested / "glintr100.onnx").write_bytes(b"recognition")
            config = FaceRecognitionConfig(model_root=root / ".bildebank-insightface", model_name="antelopev2")

            self.assertTrue(normalize_insightface_model_layout(config))

            model_dir = root / ".bildebank-insightface" / "models" / "antelopev2"
            self.assertTrue((model_dir / "scrfd_10g_bnkps.onnx").exists())
            self.assertTrue((model_dir / "glintr100.onnx").exists())
            self.assertFalse(nested.exists())

    def test_remove_insightface_model_zip_removes_only_active_model_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models_dir = root / ".bildebank-insightface" / "models"
            models_dir.mkdir(parents=True)
            active_zip = models_dir / "antelopev2.zip"
            other_zip = models_dir / "buffalo_l.zip"
            active_zip.write_bytes(b"zip")
            other_zip.write_bytes(b"zip")
            config = FaceRecognitionConfig(model_root=root / ".bildebank-insightface", model_name="antelopev2")

            self.assertTrue(remove_insightface_model_zip(config))
            self.assertFalse(active_zip.exists())
            self.assertTrue(other_zip.exists())
            self.assertFalse(remove_insightface_model_zip(config))

    def test_insightface_import_error_message_reports_linux_libgl_dependency(self) -> None:
        message = insightface_import_error_message(
            ImportError("libGL.so.1: cannot open shared object file: No such file or directory")
        )

        self.assertIn("OpenCV mangler Linux-biblioteket libGL.so.1", message)
        self.assertIn("sudo apt install libgl1", message)

    def test_face_suggest_uses_relative_face_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "new-name"
            image_path = target / "2021" / "08" / "2019-1-6-1.jpg"
            relative_image_path = Path("2021/08/2019-1-6-1.jpg")

            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(minimal_png(640, 480))
            config = load_config(self.program_root).face_recognition

            conn = sqlite3.connect(face_db_path(target, config))
            try:
                apply_face_schema(conn)
                embedding = struct.pack("ff", 1.0, 0.0)
                conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, ?, 'sha', 'ok', 2)
                    """,
                    (relative_image_path.as_posix(), db.relative_path_key(relative_image_path)),
                )
                for face_id in (1, 2):
                    conn.execute(
                        """
                        INSERT INTO faces(
                            id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                            detection_score, embedding_model, embedding
                        ) VALUES(?, 1, ?, 1, 2, 30, 40, 0.9, 'test', ?)
                        """,
                        (face_id, db.relative_path_key(relative_image_path), embedding),
                    )
                conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "face-suggest"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Face-suggest: leser 1 bekreftede ansikter.", stdout)
            self.assertIn("Face-suggest: leser 1 ukjente ansikter.", stdout)
            self.assertIn("Face-suggest: sammenlignet=1/1", stdout)
            self.assertIn("forslag=1", stdout)
            self.assertNotIn("Kari\tface-id=2", stdout)
            conn = sqlite3.connect(face_db_path(target, config))
            try:
                self.assertEqual(
                    conn.execute("SELECT target_path FROM scanned_files WHERE file_id = 1").fetchone()[0],
                    "2021/08/2019-1-6-1.jpg",
                )
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 1)
            finally:
                conn.close()

    def test_face_suggest_model_uses_model_specific_database_without_changing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            relative_image_path = Path("2021/08/IMG_20210801.jpg")
            embedding = embedding_blob([1.0, 0.0, 0.0])
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / relative_image_path).parent.mkdir(parents=True)
            (target / relative_image_path).write_bytes(minimal_png(640, 480))

            antelope_config = FaceRecognitionConfig(model_name="antelopev2")
            conn = connect_face_db(target, antelope_config)
            try:
                conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, ?, 'sha', 'ok', 2)
                    """,
                    (relative_image_path.as_posix(), db.relative_path_key(relative_image_path)),
                )
                for face_id in (1, 2):
                    conn.execute(
                        """
                        INSERT INTO faces(
                            id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                            detection_score, embedding_model, embedding
                        ) VALUES(?, 1, ?, 1, 2, 30, 40, 0.9, 'antelopev2', ?)
                        """,
                        (face_id, db.relative_path_key(relative_image_path), embedding),
                    )
                conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "face-suggest", "--model", "antelopev2"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Modell: antelopev2", stdout)
            self.assertIn("forslag=1", stdout)
            self.assertEqual(load_config(self.program_root).face_recognition.model_name, "buffalo_l")
            conn = sqlite3.connect(face_db_path(target, antelope_config))
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 1)
            finally:
                conn.close()
            buffalo_config = load_config(self.program_root).face_recognition
            self.assertFalse(face_db_path(target, buffalo_config).exists())

    def test_face_suggest_without_confirmed_faces_deletes_old_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            relative_image_path = Path("2021/08/IMG_20210801.jpg")
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)

            config = load_config(self.program_root).face_recognition
            conn = connect_face_db(target, config)
            try:
                conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, ?, 'sha', 'ok', 1)
                    """,
                    (relative_image_path.as_posix(), db.relative_path_key(relative_image_path)),
                )
                conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    ) VALUES(1, 1, ?, 1, 2, 30, 40, 0.9, 'buffalo_l', ?)
                    """,
                    (db.relative_path_key(relative_image_path), embedding_blob([1.0, 0.0, 0.0])),
                )
                conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 1, 0.95)")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "face-suggest"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("personer=0", stdout)
            self.assertIn("ukjente_ansikter=1", stdout)
            self.assertIn("forslag=0", stdout)
            conn = connect_face_db(target, config)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 0)
            finally:
                conn.close()

    def test_face_suggest_matches_best_confirmed_face_not_person_centroid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            relative_image_path = Path("2021/08/IMG_20210801.jpg")
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)

            config = load_config(self.program_root).face_recognition
            conn = connect_face_db(target, config)
            try:
                conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, ?, 'sha', 'ok', 4)
                    """,
                    (relative_image_path.as_posix(), db.relative_path_key(relative_image_path)),
                )
                faces = (
                    (1, [1.0, 0.0, 0.0]),
                    (2, [0.0, 1.0, 0.0]),
                    (3, [1.0, 0.0, 0.0]),
                    (4, [0.0, 0.0, 1.0]),
                )
                for face_id, embedding in faces:
                    conn.execute(
                        """
                        INSERT INTO faces(
                            id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                            detection_score, embedding_model, embedding
                        ) VALUES(?, 1, ?, 1, 2, 30, 40, 0.9, 'buffalo_l', ?)
                        """,
                        (face_id, db.relative_path_key(relative_image_path), embedding_blob(embedding)),
                    )
                conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Ola')")
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 2)")
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(2, 4)")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "face-suggest", "--threshold", "0.9"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("personer=2", stdout)
            self.assertIn("ukjente_ansikter=1", stdout)
            self.assertIn("forslag=1", stdout)
            conn = connect_face_db(target, config)
            try:
                suggestion = conn.execute(
                    """
                    SELECT
                        persons.name,
                        face_suggestions.face_id,
                        face_suggestions.reference_face_id,
                        face_suggestions.similarity
                    FROM face_suggestions
                    JOIN persons ON persons.id = face_suggestions.person_id
                    """
                ).fetchone()
                self.assertIsNotNone(suggestion)
                self.assertEqual((suggestion[0], suggestion[1], suggestion[2]), ("Kari", 3, 1))
                self.assertAlmostEqual(suggestion[3], 1.0)
            finally:
                conn.close()

    def test_read_image_uses_unicode_safe_file_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "utenrødeøyne.jpg"
            path.write_bytes(b"image-bytes")

            class FakeData:
                size = 11

            class FakeNp:
                uint8 = object()

                @staticmethod
                def fromfile(filename, dtype):
                    self.assertEqual(filename, str(path))
                    self.assertIs(dtype, FakeNp.uint8)
                    return FakeData()

            class FakeCv2:
                IMREAD_COLOR = 1
                IMREAD_IGNORE_ORIENTATION = 128

                @staticmethod
                def imdecode(data, flags):
                    self.assertIsInstance(data, FakeData)
                    self.assertEqual(flags, FakeCv2.IMREAD_COLOR | FakeCv2.IMREAD_IGNORE_ORIENTATION)
                    return {"decoded": True}

            modules = {"cv2": FakeCv2, "numpy": FakeNp}
            with patch.dict(sys.modules, modules):
                self.assertEqual(read_image(path), {"decoded": True})

    def test_face_box_percent_accounts_for_exif_rotation(self) -> None:
        face = {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0}
        dimensions = ImageDimensions(width=100, height=200)

        self.assertEqual(
            face_box_percent(face, dimensions, orientation=6),
            (70.0, 10.0, 20.0, 30.0),
        )

    def test_face_report_handles_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-report"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Face-database finnes ikke.", stdout)
            self.assertIn("Kjør bildebank face-scan først.", stdout)

    def test_face_person_add_remove_face_and_suggest(self) -> None:
        class FakeFace:
            def __init__(self, bbox, embedding):
                self.bbox = bbox
                self.det_score = 0.9
                self.embedding = embedding

        class FakeApp:
            def get(self, image):
                return [
                    FakeFace([1.0, 2.0, 11.0, 22.0], [1.0, 0.0, 0.0]),
                    FakeFace([30.0, 4.0, 42.0, 24.0], [0.99, 0.01, 0.0]),
                    FakeFace([50.0, 6.0, 64.0, 30.0], [0.0, 1.0, 0.0]),
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.enable_face_recognition_config()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            main_conn = db.connect(target)
            try:
                file_id = int(main_conn.execute("SELECT id FROM files").fetchone()["id"])
            finally:
                main_conn.close()
            with (
                patch("bildebank.face.load_face_app", return_value=FakeApp()),
                patch("bildebank.face.read_image", return_value=object()),
            ):
                self.assertEqual(run_cli(["--target", str(target), "face-scan", "--limit", "1"]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "face-person-add-face", "Krai", "1"]
            )

            self.assertEqual(code, 1)
            self.assertIn("Fant ikke person: Krai", stderr)

            self.assertEqual(run_cli(["--target", str(target), "face-person-create", "Kari"]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "face-person-add-face", "Kari", "1"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Person: Kari", stdout)
            self.assertIn("Ansikt-id: 1", stdout)
            self.assertIn("Ansiktet er koblet til personen.", stdout)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "face-person-remove-face", "Kari", "1"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Ansiktet er fjernet fra personen.", stdout)

            self.assertEqual(
                run_cli(["--target", str(target), "face-person-add-face", "Kari", "1"]),
                0,
            )

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "face-suggest", "--threshold", "0.9"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("personer=1", stdout)
            self.assertIn("ukjente_ansikter=2", stdout)
            self.assertIn("forslag=1", stdout)
            self.assertNotIn("Forslag:", stdout)
            self.assertNotIn("Kari\tface-id=2", stdout)
            self.assertNotIn("Skrev person-index", stdout)
            self.assertFalse((target / "personer.html").exists())
            self.assertFalse((target / "person-Kari.html").exists())

            code, stdout, stderr = capture_cli(["--target", str(target), "face-report"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Personstatus:", stdout)
            self.assertIn("Personer registrert: 1", stdout)
            self.assertIn("Bekreftede ansiktskoblinger: 1", stdout)
            self.assertIn("Forslag: 1", stdout)
            self.assertIn("Bilder med minst én bekreftet person: 1", stdout)
            self.assertIn("Bilder med ansikter, men ingen bekreftet person: 0", stdout)
            self.assertIn("Bilder med både bekreftede og ukjente ansikter: 1", stdout)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-list"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Navn  Bilder  Ansikter  Forslag  Oppdatert", stdout)
            self.assertRegex(stdout, r"Kari\s+1\s+1\s+1\s+\d{4}-\d{2}-\d{2}")

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-rename", "Kari", "Kari Nordmann"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Endret personnavn: Kari -> Kari Nordmann", stdout)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-list"])

            self.assertEqual(code, 0, stderr)
            self.assertRegex(stdout, r"Kari Nordmann\s+1\s+1\s+1\s+\d{4}-\d{2}-\d{2}")
            self.assertNotIn("Kari  ", stdout)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-rename", "Kari", "Kari Nordmann"])

            self.assertEqual(code, 1)
            self.assertIn("Fant ikke person: Kari", stderr)

            self.assertEqual(run_cli(["--target", str(target), "face-person-create", "Ola"]), 0)
            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-rename", "Kari Nordmann", "Ola"])

            self.assertEqual(code, 1)
            self.assertIn("Person finnes allerede: Ola", stderr)

            with patch("builtins.input", return_value="slett Ola"):
                self.assertEqual(run_cli(["--target", str(target), "face-person-delete", "Ola"]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-rename", "Kari Nordmann", "Kari"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Endret personnavn: Kari Nordmann -> Kari", stdout)

            with patch("builtins.input", return_value="slett Kari"):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-person-delete", "Kari"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Slettet person: Kari", stdout)
            self.assertIn("Fjernet bekreftede ansiktskoblinger: 1", stdout)
            self.assertIn("Fjernet ansiktsforslag: 1", stdout)
            self.assertIn("Ingen bilder eller scannede ansikter er slettet.", stdout)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-list"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Ingen personer registrert.", stdout)

            self.assertEqual(run_cli(["--target", str(target), "face-person-create", "Kari"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "face-person-add-face", "Kari", "1"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "face-suggest", "--threshold", "0.9"]), 0)
            self.assertFalse((target / "personer.html").exists())
            main_conn = db.connect(target)
            try:
                main_conn.execute(
                    "UPDATE files SET view_rotation_degrees = 270 WHERE id = ?",
                    (file_id,),
                )
                main_conn.commit()
            finally:
                main_conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "make-person-browser", "Kari"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser for person", stdout)
            html = (target / "person-Kari.html").read_text(encoding="utf-8")
            self.assertIn("<title>Kari</title>", html)
            self.assertIn('id="title" class="title"', html)
            self.assertIn('<nav class="breadcrumb" aria-label="Plassering">', html)
            self.assertIn('parts.push({ label: "År", action: showYears });', html)
            self.assertIn('title="Forrige måned">◀ Mån</button>', html)
            self.assertIn('title="Neste måned">ed ▶</button>', html)
            self.assertIn("const embeddedItems", html)
            self.assertIn("IMG_20240102.jpg", html)
            self.assertIn('"kind": "image"', html)
            self.assertIn('"viewRotation": 270', html)
            self.assertNotIn('"faceId": 1', html)
            self.assertNotIn('"status": "bekreftet"', html)
            self.assertNotIn('"faceId": 2', html)
            self.assertNotIn('"status": "forslag"', html)
            self.assertNotIn('"box suggested"', html)
            self.assertNotIn("const imageRect = img.getBoundingClientRect();", html)

            code, stdout, stderr = capture_cli(["--target", str(target), "make-people-browser"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev person-index", stdout)
            self.assertIn("Skrev personsider: 1", stdout)
            index_html = (target / "personer.html").read_text(encoding="utf-8")
            self.assertIn("<h1>Personer (1)</h1>", index_html)
            self.assertIn("person-Kari.html", index_html)
            self.assertIn('href="person-Kari.html"', index_html)
            self.assertNotIn(str(target), index_html)
            self.assertIn("Kari", index_html)
            self.assertIn("1 bilder", index_html)
            self.assertIn("1 bekreftet, 1 forslag", index_html)
            self.assertIn('data-view-rotation="270"', index_html)

            config = load_config(self.program_root).face_recognition
            conn = sqlite3.connect(face_db_path(target, config))
            try:
                suggestion = conn.execute(
                    """
                    SELECT persons.name, face_suggestions.face_id
                    FROM face_suggestions
                    JOIN persons ON persons.id = face_suggestions.person_id
                    """
                ).fetchone()
                self.assertEqual(suggestion, ("Kari", 2))
            finally:
                conn.close()

    def test_face_reset_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            config = load_config(self.program_root).face_recognition
            face_db = face_db_path(target, config)
            face_db.write_bytes(b"face-data")

            with patch("builtins.input", return_value="nei"):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-reset", "--all"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Avbrutt", stdout)
            self.assertTrue(face_db.exists())

            with patch("builtins.input", return_value="ja, slett ansiktsdata"):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-reset", "--all"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Slettet face-database", stdout)
            self.assertFalse(face_db.exists())

    def test_face_reset_all_refuses_to_delete_database_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            config = load_config(self.program_root).face_recognition
            face_db = face_db_path(target, config)
            face_db.write_bytes(b"face-data")
            (target / LOCK_FILENAME).write_text("command=face-scan\n", encoding="utf-8")

            with patch("builtins.input", return_value="ja, slett ansiktsdata"):
                code, _stdout, stderr = capture_cli(["--target", str(target), "face-reset", "--all"])

            self.assertEqual(code, 1)
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertTrue(face_db.exists())

    def test_face_reset_can_keep_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            config = load_config(self.program_root).face_recognition
            conn = sqlite3.connect(face_db_path(target, config))
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE scanned_files (
                        file_id INTEGER PRIMARY KEY,
                        target_path TEXT NOT NULL,
                        target_path_key TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        status TEXT NOT NULL,
                        error_message TEXT,
                        face_count INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE faces (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_id INTEGER NOT NULL,
                        target_path_key TEXT NOT NULL,
                        bbox_x REAL NOT NULL,
                        bbox_y REAL NOT NULL,
                        bbox_width REAL NOT NULL,
                        bbox_height REAL NOT NULL,
                        detection_score REAL NOT NULL,
                        embedding_model TEXT NOT NULL,
                        embedding BLOB NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE face_group_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        threshold REAL NOT NULL,
                        method TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE face_groups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL,
                        group_index INTEGER NOT NULL,
                        member_count INTEGER NOT NULL
                    );
                    CREATE TABLE face_group_members (
                        group_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL,
                        similarity REAL NOT NULL,
                        PRIMARY KEY(group_id, face_id)
                    );
                    CREATE TABLE persons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE person_faces (
                        person_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL,
                        confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(person_id, face_id)
                    );
                    CREATE TABLE face_suggestions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        person_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL,
                        similarity REAL NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(person_id, face_id)
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '2');
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, 'image.jpg', 'image.jpg', 'hash', 'ok', 1);
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    ) VALUES(1, 1, 'image.jpg', 1, 2, 10, 20, 0.9, 'test', x'00000000');
                    INSERT INTO face_group_runs(id, threshold, method) VALUES(1, 0.6, 'test');
                    INSERT INTO face_groups(id, run_id, group_index, member_count) VALUES(1, 1, 1, 1);
                    INSERT INTO face_group_members(group_id, face_id, similarity) VALUES(1, 1, 1.0);
                    INSERT INTO persons(id, name) VALUES(1, 'Kari');
                    INSERT INTO person_faces(person_id, face_id) VALUES(1, 1);
                    INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 1, 0.95);
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with patch("builtins.input", return_value="ja, slett personer"):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-reset", "--keep-scan"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Face-scan-resultater er beholdt", stdout)
            conn = sqlite3.connect(face_db_path(target, config))
            try:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "5")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 0)
                legacy_tables = {
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table' AND name LIKE 'face_group_%'
                        """
                    )
                }
                self.assertEqual(legacy_tables, set())
            finally:
                conn.close()

            conn = sqlite3.connect(face_db_path(target, config))
            try:
                conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                conn.execute("INSERT INTO person_files(person_id, file_id) VALUES(1, 1)")
                conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 1, 0.95)")
                conn.commit()
            finally:
                conn.close()

            with patch("builtins.input", return_value="ja, slett personer"):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-reset"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Face-scan-resultater er beholdt", stdout)
            conn = sqlite3.connect(face_db_path(target, config))
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0], 0)
            finally:
                conn.close()

    def test_face_schema_v2_migration_drops_legacy_group_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            conn = sqlite3.connect(face_db_path(target))
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE scanned_files (
                        file_id INTEGER PRIMARY KEY,
                        target_path TEXT NOT NULL,
                        target_path_key TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        status TEXT NOT NULL,
                        error_message TEXT,
                        face_count INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE face_group_runs (id INTEGER PRIMARY KEY AUTOINCREMENT);
                    CREATE TABLE face_groups (id INTEGER PRIMARY KEY AUTOINCREMENT);
                    CREATE TABLE face_group_members (group_id INTEGER NOT NULL, face_id INTEGER NOT NULL);
                    INSERT INTO meta(key, value) VALUES('schema_version', '2');
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, 'image.jpg', 'image.jpg', 'hash', 'ok', 0);
                    """
                )
                conn.commit()
            finally:
                conn.close()

            conn = connect_face_db(target)
            try:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "5")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0], 0)
                legacy_tables = {
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table' AND name LIKE 'face_group_%'
                        """
                    )
                }
                self.assertEqual(legacy_tables, set())
            finally:
                conn.close()

    def test_face_schema_v3_migration_adds_person_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            conn = sqlite3.connect(face_db_path(target))
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE scanned_files (
                        file_id INTEGER PRIMARY KEY,
                        target_path TEXT NOT NULL,
                        target_path_key TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        status TEXT NOT NULL,
                        error_message TEXT,
                        face_count INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE faces (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_id INTEGER NOT NULL,
                        target_path_key TEXT NOT NULL,
                        bbox_x REAL NOT NULL,
                        bbox_y REAL NOT NULL,
                        bbox_width REAL NOT NULL,
                        bbox_height REAL NOT NULL,
                        detection_score REAL NOT NULL,
                        embedding_model TEXT NOT NULL,
                        embedding BLOB NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE persons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE person_faces (
                        person_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL,
                        confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(person_id, face_id)
                    );
                    CREATE TABLE face_suggestions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        person_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL,
                        similarity REAL NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(person_id, face_id)
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '3');
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, 'image.jpg', 'image.jpg', 'hash', 'ok', 1);
                    INSERT INTO persons(id, name) VALUES(1, 'Kari');
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    ) VALUES(1, 1, 'image.jpg', 1, 2, 10, 20, 0.9, 'test', x'00000000');
                    INSERT INTO person_faces(person_id, face_id) VALUES(1, 1);
                    INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 1, 0.95);
                    """
                )
                conn.commit()
            finally:
                conn.close()

            conn = connect_face_db(target)
            try:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "5")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0], 0)
            finally:
                conn.close()

    def test_face_schema_v4_migration_adds_reference_face_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            conn = sqlite3.connect(face_db_path(target))
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE persons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE face_suggestions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        person_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL,
                        similarity REAL NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(person_id, face_id)
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '4');
                    INSERT INTO persons(id, name) VALUES(1, 'Kari');
                    INSERT INTO face_suggestions(person_id, face_id, similarity)
                    VALUES(1, 10, 0.95);
                    """
                )
                conn.commit()
            finally:
                conn.close()

            conn = connect_face_db(target)
            try:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "5")
                columns = {row[1] for row in conn.execute("PRAGMA table_info(face_suggestions)")}
                self.assertIn("reference_face_id", columns)
                suggestion = conn.execute(
                    "SELECT person_id, face_id, reference_face_id, similarity FROM face_suggestions"
                ).fetchone()
                self.assertEqual(tuple(suggestion), (1, 10, None, 0.95))
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(face_suggestions)")}
                self.assertIn("idx_face_suggestions_reference_face_id", indexes)
            finally:
                conn.close()

    def test_face_schema_current_version_rejects_legacy_group_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            conn = sqlite3.connect(face_db_path(target))
            try:
                apply_face_schema(conn)
                conn.execute("CREATE TABLE face_group_runs (id INTEGER PRIMARY KEY)")
                conn.execute("INSERT INTO face_group_runs(id) VALUES(1)")
                conn.commit()

                with self.assertRaisesRegex(ValueError, "legacy-gruppetabeller"):
                    apply_face_schema(conn)

                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "5")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_group_runs").fetchone()[0], 1)
            finally:
                conn.close()

    def test_safe_copy_does_not_overwrite_existing_different_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            source.write_bytes(b"new-image")
            destination.write_bytes(b"existing-image")

            with self.assertRaises(FileExistsError):
                safe_copy(source, destination, sha256_file(source))

            self.assertEqual(destination.read_bytes(), b"existing-image")

    def test_safe_copy_does_not_require_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            source.write_bytes(b"image")

            with patch("bildebank.importer.os.link", side_effect=OSError("hardlink unsupported")):
                safe_copy(source, destination, sha256_file(source))

            self.assertEqual(destination.read_bytes(), b"image")

    def test_import_recovers_file_copied_before_database_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"already-copied")

            self.assertEqual(run_cli(["create", str(target)]), 0)

            recovered = target / "2024" / "01" / "IMG_20240102.jpg"
            recovered.parent.mkdir(parents=True)
            recovered.write_bytes(b"already-copied")

            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertFalse((target / "2024" / "01" / "IMG_20240102-1.jpg").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
            finally:
                conn.close()

    def test_named_import_only_imports_that_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            normal = root / "normal"
            removable = root / "removable"
            normal.mkdir()
            removable.mkdir()
            (normal / "NORMAL_20240102.jpg").write_bytes(b"normal")
            (removable / "REM_20240203.jpg").write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", "usb-test", str(removable)]),
                0,
            )

            self.assertFalse((target / "2024" / "01" / "NORMAL_20240102.jpg").exists())
            self.assertTrue((target / "2024" / "02" / "REM_20240203.jpg").exists())

    def test_import_rejects_reused_imported_name_without_changing_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "REM_20240203.jpg").write_bytes(b"first")
            (second / "REM_20240304.jpg").write_bytes(b"second")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", "usb-test", str(first)]),
                0,
            )

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--name", "usb-test", str(second)]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("er allerede importert", stderr)
            self.assertIn("Bruk et nytt --name", stderr)
            self.assertFalse((target / "2024" / "03" / "REM_20240304.jpg").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                rows = conn.execute("SELECT path FROM sources WHERE name = 'usb-test'").fetchall()
                self.assertEqual(rows, [(str(first.resolve()),)])
            finally:
                conn.close()

    def test_unimport_named_source_requires_name_and_rejects_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            removable = root / "removable"
            removable.mkdir()
            (removable / "REM_20240203.jpg").write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", "usb-test", str(removable)]),
                0,
            )

            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(["--target", str(target), "unimport", str(removable)])
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(["--target", str(target), "unimport"])
            self.assertTrue((target / "2024" / "02" / "REM_20240203.jpg").exists())

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", "usb-test"]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Unimport gjennomført.", stdout)
            self.assertNotIn("Kilden er satt tilbake til pending.", stdout)
            self.assertIn("Kilden er fjernet fra kildelisten.", stdout)
            self.assertFalse((target / "2024" / "02" / "REM_20240203.jpg").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
            finally:
                conn.close()

    def test_unimport_dry_run_reports_removable_plan_without_changes_or_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            removable = root / "removable"
            removable.mkdir()
            (removable / "REM_20240203.jpg").write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", "usb-test", str(removable)]),
                0,
            )
            imported = target / "2024" / "02" / "REM_20240203.jpg"

            with patch("builtins.input", side_effect=AssertionError("dry-run should not ask")):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--dry-run", "--name", "usb-test"]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Filer som fjernes fra aktiv samling: 1", stdout)
            self.assertIn("Kilden ville blitt fjernet fra kildelisten.", stdout)
            self.assertIn("Dry-run: ingen endringer er gjort.", stdout)
            self.assertTrue(imported.exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                commands = [
                    row[0]
                    for row in conn.execute("SELECT command FROM command_log ORDER BY id").fetchall()
                ]
                self.assertEqual(commands, ["create", "import"])
            finally:
                conn.close()

    def test_unimport_named_source_missing_source_file_explains_media_may_be_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            removable = root / "removable"
            removable.mkdir()
            source_file = removable / "REM_20240203.jpg"
            source_file.write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", "usb-test", str(removable)]),
                0,
            )
            source_file.unlink()

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", "usb-test"]
            )

            self.assertEqual(code, 1)
            self.assertIn("Unimport: kontrollerer 1 kildefiler.", stdout)
            self.assertIn("Kildefil mangler", stderr)
            self.assertIn("Sjekk at riktig mappe, USB-disk", stderr)
            self.assertTrue((target / "2024" / "02" / "REM_20240203.jpg").exists())

    def test_import_dry_run_does_not_register_or_copy_named_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            removable = root / "removable"
            removable.mkdir()
            removable_file = removable / "REM_20240203.jpg"
            removable_file.write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "import",
                    "--name",
                    "usb-test",
                    "--dry-run",
                    str(removable),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertNotIn("IMPORT\t", stdout)
            self.assertNotIn(str(removable_file.resolve()), stdout)
            self.assertIn("importert=1", stdout)
            self.assertFalse((target / "2024" / "02" / "REM_20240203.jpg").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
                commands_after = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
                self.assertEqual(commands_after, commands_before)
            finally:
                conn.close()

    def test_import_video_uses_mp4_metadata_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertTrue((target / "2010" / "07" / "video.mp4").exists())

    def test_import_mp_motion_file_stores_copy_as_mp4_and_keeps_original_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            motion = source / "PXL_20250102_123.MP"
            motion.write_bytes(minimal_mp4_with_creation_date(dt.date(2025, 1, 2)))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertTrue((target / "2025" / "01" / "PXL_20250102_123.mp4").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute("SELECT original_filename, stored_filename FROM files").fetchone()
            finally:
                conn.close()
            self.assertEqual(row, ("PXL_20250102_123.MP", "PXL_20250102_123.mp4"))

    def test_import_mp_motion_name_conflict_uses_mp4_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            (source / "a" / "PXL_20250102_123.MP").write_bytes(
                minimal_mp4_with_creation_date(dt.date(2025, 1, 2))
            )
            (source / "b" / "PXL_20250102_123.MP").write_bytes(
                minimal_mp4_with_creation_date(dt.date(2025, 1, 3))
            )

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertTrue((target / "2025" / "01" / "PXL_20250102_123.mp4").exists())
            self.assertTrue((target / "2025" / "01" / "PXL_20250102_123-1.mp4").exists())

    def test_import_avi_uses_metadata_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "oktnov07 063.avi").write_bytes(
                minimal_avi_with_creation_date(dt.date(2007, 10, 31))
            )

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertTrue((target / "2007" / "10" / "oktnov07 063.avi").exists())

    def test_import_nef_uses_tiff_metadata_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "DSC_0170.JPG").write_bytes(jpeg_with_exif_datetime("2019:03:03 12:00:00"))
            (source / "DSC_0170.NEF").write_bytes(minimal_tiff_with_datetime("2019:03:03 12:00:00"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            self.assertTrue((target / "2019" / "03" / "DSC_0170.JPG").exists())
            self.assertTrue((target / "2019" / "03" / "DSC_0170.NEF").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                rows = conn.execute(
                    "SELECT stored_filename, taken_date, date_source, metadata_datetime FROM files ORDER BY stored_filename"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(
                rows,
                [
                    ("DSC_0170.JPG", "2019-03-03", "metadata", "2019-03-03 12:00:00"),
                    ("DSC_0170.NEF", "2019-03-03", "metadata", "2019-03-03 12:00:00"),
                ],
            )

    def test_non_metadata_lists_files_not_placed_by_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))
            (source / "IMG_20240102.jpg").write_bytes(b"filename-date")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "non-metadata"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("filename\t2024-01-02", stdout)
            self.assertIn("IMG_20240102.jpg", stdout)
            self.assertNotIn("video.mp4", stdout)

    def test_explain_date_shows_selected_date_and_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "IMG_20240102.jpg"
            path.write_bytes(b"not-a-real-jpeg")

            code, stdout, stderr = capture_cli(["explain-date", str(path)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Valgt dato: 2024-01-02", stdout)
            self.assertIn("Valgt kilde: filename", stdout)
            self.assertIn("JPEG EXIF", stdout)
            self.assertIn("Dato i filnavn", stdout)

    def test_inspect_metadata_shows_metadata_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "xmp-only.jpg"
            path.write_bytes(jpeg_with_xmp_date("2007-03-12T19:54:18+01:00"))

            code, stdout, stderr = capture_cli(["inspect-metadata", str(path)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("JPEG metadata:", stdout)
            self.assertIn("APP1", stdout)
            self.assertIn("XMP dato: 2007-03-12", stdout)

    def test_inspect_metadata_shows_tiff_raw_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "DSC_0170.NEF"
            path.write_bytes(minimal_tiff_with_datetime("2019:03:03 12:00:00"))

            code, stdout, stderr = capture_cli(["inspect-metadata", str(path)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Valgt dato: 2019-03-03", stdout)
            self.assertIn("TIFF/RAW metadata:", stdout)
            self.assertIn("TIFF/RAW dato: 2019-03-03", stdout)

    def test_refresh_metadata_moves_non_metadata_file_when_metadata_becomes_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "video.avi"
            source_file.write_bytes(b"RIFF\x04\x00\x00\x00AVI ")
            old_time = dt.datetime(2008, 2, 29, 12, 0).timestamp()
            os.utime(source_file, (old_time, old_time))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            old_target = target / "2008" / "02" / "video.avi"
            new_target = target / "2007" / "03" / "video.avi"
            self.assertTrue(old_target.exists())

            old_target.write_bytes(minimal_avi_with_idit_outside_info())

            code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Refresh-metadata: kontrollert=1/1", stdout)
            self.assertIn("gjenstår=0s", stdout)
            self.assertIn("flyttet=1", stdout)
            self.assertFalse(old_target.exists())
            self.assertTrue(new_target.exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute(
                    "SELECT target_path, taken_date, date_source FROM files"
                ).fetchone()
                self.assertEqual(row[0], str(new_target.relative_to(target)))
                self.assertEqual(row[1], "2007-03-12")
                self.assertEqual(row[2], "metadata")
            finally:
                conn.close()

    def test_recovery_completes_refresh_metadata_after_file_was_moved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "video.avi"
            source_file.write_bytes(b"RIFF\x04\x00\x00\x00AVI ")
            old_time = dt.datetime(2008, 2, 29, 12, 0).timestamp()
            os.utime(source_file, (old_time, old_time))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )

            old_target = target / "2008" / "02" / "video.avi"
            new_target = target / "2007" / "03" / "video.avi"
            old_target.write_bytes(minimal_avi_with_idit_outside_info())
            with sqlite3.connect(target / DB_FILENAME) as conn:
                conn.execute("UPDATE files SET sha256 = ?", (sha256_file(old_target),))
                conn.commit()
            real_move = shutil.move

            def move_then_crash(source_path, destination_path):  # noqa: ANN001
                real_move(source_path, destination_path)
                raise RuntimeError("crash after move")

            with patch("bildebank.importer.shutil.move", side_effect=move_then_crash):
                code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata"])

            self.assertEqual(code, 2)
            self.assertFalse(old_target.exists())
            self.assertTrue(new_target.exists())
            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 0, stderr)
            with sqlite3.connect(target / DB_FILENAME) as conn:
                row = conn.execute(
                    """
                    SELECT files.target_path, files.taken_date, files.date_source,
                           pending_file_moves.state
                    FROM files
                    JOIN pending_file_moves ON pending_file_moves.file_id = files.id
                    """
                ).fetchone()
            self.assertEqual(row[0], "2007/03/video.avi")
            self.assertEqual(row[1], "2007-03-12")
            self.assertEqual(row[2], "metadata")
            self.assertEqual(row[3], "completed")

    def test_refresh_metadata_refuses_to_run_while_target_is_locked(self) -> None:
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
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "refresh-metadata", "--dry-run"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Dry-run", stdout)

    def test_refresh_metadata_rescan_fills_camera_for_existing_metadata_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(jpeg_with_exif_camera("Canon", "EOS 80D"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    """
                    UPDATE files
                    SET date_source = 'metadata',
                        camera_make = NULL,
                        camera_model = NULL
                    """
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("sjekket=0", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("SELECT camera_make, camera_model FROM files").fetchone(),
                    (None, None),
                )
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata", "--rescan"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("sjekket=1", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("SELECT camera_make, camera_model FROM files").fetchone(),
                    ("Canon", "EOS 80D"),
                )
            finally:
                conn.close()

    def test_refresh_metadata_rescan_commits_progress_when_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(jpeg_with_exif_camera("Canon", "EOS 80D"))
            (source / "IMG_20240103.jpg").write_bytes(jpeg_with_exif_camera("Apple", "iPhone 17"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    """
                    UPDATE files
                    SET date_source = 'metadata',
                        camera_make = NULL,
                        camera_model = NULL
                    """
                )
                conn.commit()
            finally:
                conn.close()

            calls = 0

            def interrupt_after_one(conn, target, row, stats, *, dry_run, verbose):
                nonlocal calls
                calls += 1
                if calls == 1:
                    db.update_file_camera(
                        conn,
                        file_id=int(row["id"]),
                        camera_make="Canon",
                        camera_model="EOS 80D",
                    )
                    return
                raise KeyboardInterrupt

            with patch("bildebank.importer.refresh_non_metadata_file", side_effect=interrupt_after_one):
                code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata", "--rescan"])

            self.assertEqual(code, 130, stderr)
            self.assertIn("Avbrutt. Databaseendringer er lagret", stdout)
            self.assertIn("avbrutt=ja", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                rows = conn.execute(
                    "SELECT camera_make, camera_model FROM files ORDER BY target_path"
                ).fetchall()
                self.assertEqual(rows[0], ("Canon", "EOS 80D"))
                self.assertEqual(rows[1], (None, None))
            finally:
                conn.close()

    def test_errors_lists_recorded_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            missing = root / "missing-source"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    "insert into errors(stage, source_path, message) values(?, ?, ?)",
                    ("refresh-metadata", str(missing), "Målfil finnes ikke"),
                )
                conn.execute(
                    """
                    insert into errors(stage, source_path, message, resolved_at)
                    values(?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    ("refresh-metadata", str(root / "fixed"), "Løst feil"),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "errors"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("refresh-metadata", stdout)
            self.assertIn("Målfil finnes ikke", stdout)
            self.assertNotIn("Løst feil", stdout)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "errors", "--include-resolved"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Løst feil", stdout)

    def test_refresh_metadata_verbose_prints_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"filename-date")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            imported.unlink()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "refresh-metadata", "--verbose"]
            )

            self.assertEqual(code, 2, stderr)
            self.assertIn("FEIL", stdout)
            self.assertIn("Målfil finnes ikke", stdout)

    def test_refresh_metadata_repairs_missing_target_path_and_resolves_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            old_target = target / "2008" / "02" / "video.avi"
            repaired_target = target / "2007" / "03" / "video.avi"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            repaired_target.parent.mkdir(parents=True)
            repaired_target.write_bytes(minimal_avi_with_idit_outside_info())
            file_hash = sha256_file(repaired_target)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                source_id = conn.execute(
                    "insert into sources(path, path_key, name) values(?, ?, 'source') returning id",
                    (str(root / "source"), str(root / "source")),
                ).fetchone()[0]
                file_id = conn.execute(
                    """
                    insert into files(
                        target_path, target_path_key, original_filename, stored_filename, sha256,
                        size_bytes, taken_date, date_source, name_conflict
                    ) values(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    returning id
                    """,
                    (
                        old_target.relative_to(target).as_posix(),
                        old_target.relative_to(target).as_posix(),
                        "video.avi",
                        "video.avi",
                        file_hash,
                        repaired_target.stat().st_size,
                        "2008-02-29",
                        "mtime",
                        0,
                    ),
                ).fetchone()[0]
                conn.execute(
                    """
                    insert into file_sources(
                        file_id, source_id, source_path, source_path_key, sha256, size_bytes
                    ) values(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        source_id,
                        str(root / "source" / "video.avi"),
                        str(root / "source" / "video.avi"),
                        file_hash,
                        repaired_target.stat().st_size,
                    ),
                )
                conn.execute(
                    "insert into errors(stage, source_path, message) values(?, ?, ?)",
                    ("refresh-metadata", str(old_target), "Målfil finnes ikke"),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "refresh-metadata", "--verbose"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("REPARERER_DB_PATH", stdout)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute(
                    "select target_path, taken_date, date_source from files"
                ).fetchone()
                self.assertEqual(row[0], str(repaired_target.relative_to(target)))
                self.assertEqual(row[1], "2007-03-12")
                self.assertEqual(row[2], "metadata")
                unresolved = conn.execute(
                    "select count(*) from errors where resolved_at is null"
                ).fetchone()[0]
                self.assertEqual(unresolved, 0)
            finally:
                conn.close()

    def test_make_browser_writes_index_with_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG 20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                file_id = int(conn.execute("SELECT id FROM files").fetchone()[0])
                conn.execute(
                    "UPDATE files SET view_rotation_degrees = 90 WHERE id = ?",
                    (file_id,),
                )
                conn.commit()
            finally:
                conn.close()
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Ola Nordmann')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, ?, 'key', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (file_id, b"embedding-1"),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, ?, 'key', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (file_id, b"embedding-2"),
                )
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(2, 2, 0.91)")
                face_conn.commit()
            finally:
                face_conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "make-browser"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser", stdout)
            html = (target / "index.html").read_text(encoding="utf-8")
            self.assertIn('"path": "2024/01/IMG 20240102.jpg"', html)
            self.assertIn('"url": "2024/01/IMG%2020240102.jpg"', html)
            self.assertIn('"sizeText": "9 bytes"', html)
            self.assertIn('"viewRotation": 90', html)
            self.assertIn('applyImageViewRotation(img, item, "contain");', html)
            self.assertIn('applyImageViewRotation(img, item, "cover");', html)
            self.assertNotIn("padding: 10px;\n      overflow: hidden;", html)
            self.assertNotIn("border-radius: 8px;\n      overflow: hidden;\n    }\n    .media-link", html)
            self.assertIn("html, body {\n      width: 100%;\n      height: 100%;\n      overflow: hidden;", html)
            self.assertIn("height: 100dvh;", html)
            self.assertIn("width: auto;\n      height: auto;\n      max-width: 100%;\n      max-height: 100%;", html)
            self.assertNotIn("max-height: calc(100vh - 10rem);", html)
            self.assertIn("const MONTH_PREVIEW_LIMIT = null;", html)
            self.assertIn('state.viewMode = "month";', html)
            self.assertIn("function representativeItems(items, limit)", html)
            self.assertIn('id="title" class="title"', html)
            self.assertIn("function renderBreadcrumb()", html)
            self.assertIn("function renderYears()", html)
            self.assertIn('"01": "Januar"', html)
            self.assertIn('<nav class="controls" aria-label="Navigering">', html)
            self.assertIn('data-nav-button-pair="year"', html)
            self.assertIn('title="Forrige år">◀ Å</button>', html)
            self.assertIn('title="Neste år">r ▶</button>', html)
            self.assertIn('data-nav-button-pair="month"', html)
            self.assertIn('title="Forrige måned">◀ Mån</button>', html)
            self.assertIn('title="Neste måned">ed ▶</button>', html)
            self.assertIn('data-nav-button-pair="item"', html)
            self.assertIn('title="Forrige bilde">◀ Bil</button>', html)
            self.assertIn('title="Neste bilde">de ▶</button>', html)
            self.assertNotIn('id="position"', html)
            self.assertNotIn("positionEl", html)
            self.assertNotIn("server-search-link", html)
            self.assertIn('img.loading = "lazy";', html)
            self.assertNotIn('"people":', html)
            self.assertNotIn('"faces":', html)
            self.assertNotIn("Personer:", html)
            self.assertNotIn("(forslag)", html)
            self.assertNotIn("Ansikter i bildet", html)
            self.assertNotIn('face-person-add-face "Navn"', html)
            self.assertNotIn("navigator.clipboard.writeText", html)
            self.assertNotIn("fallbackCopyCommand", html)

            limited_output = root / "limited.html"
            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "make-browser",
                    "--month-preview-limit",
                    "40",
                    "--output",
                    str(limited_output),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser", stdout)
            limited_html = limited_output.read_text(encoding="utf-8")
            self.assertIn("const MONTH_PREVIEW_LIMIT = 40;", limited_html)
            self.assertIn("if (limit === 1) return [items[0]];", limited_html)

    def test_make_browser_writes_custom_output_without_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))
            (source / "IMG_20240103.nef").write_bytes(b"raw-photo")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            custom_output = root / "filtered.html"
            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "make-browser",
                    "--output",
                    str(custom_output),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser", stdout)
            self.assertTrue(custom_output.exists())
            html = custom_output.read_text(encoding="utf-8")
            self.assertIn('"path": "2010/07/video.mp4"', html)
            self.assertIn('"path": "2024/01/IMG_20240102.jpg"', html)
            self.assertIn('"path": "2024/01/IMG_20240103.nef"', html)
            self.assertIn('"kind": "file"', html)
            self.assertIn('item.kind === "image"', html)

    def test_static_browser_normalizes_all_view_rotations_and_leaves_video_unrotated(self) -> None:
        html = render_html(
            [
                {
                    "path": f"2024/01/{rotation}.jpg",
                    "url": f"{rotation}.jpg",
                    "thumbnailSrc": f"{rotation}.jpg",
                    "kind": "image",
                    "viewRotation": rotation,
                    "monthKey": "2024-01",
                    "name": f"{rotation}.jpg",
                    "sizeText": "1 byte",
                }
                for rotation in (0, 90, 180, 270)
            ]
            + [
                {
                    "path": "2024/01/video.mp4",
                    "url": "video.mp4",
                    "thumbnailSrc": "",
                    "kind": "video",
                    "viewRotation": 90,
                    "monthKey": "2024-01",
                    "name": "video.mp4",
                    "sizeText": "1 byte",
                }
            ]
        )

        for rotation in (0, 90, 180, 270):
            self.assertIn(f'"viewRotation": {rotation}', html)
        self.assertIn("const quarterTurn = rotation === 90 || rotation === 270;", html)
        self.assertIn('fit === "cover" ? Math.max(scaleX, scaleY) : Math.min(scaleX, scaleY)', html)
        self.assertIn('container.classList.add("view-rotation-container");', html)
        self.assertIn('translate(-50%, -50%) rotate(${rotation}deg)', html)
        self.assertLess(
            html.index("link.append(img);"),
            html.index('applyImageViewRotation(img, item, "contain");'),
        )
        self.assertLess(
            html.index("button.append(img);"),
            html.index('applyImageViewRotation(img, item, "cover");'),
        )
        self.assertIn('if (item.kind === "video")', html)
        self.assertIn('} else if (item.kind === "image")', html)

    def test_make_browser_help_omits_filters(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["make-browser", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("make-browser", stdout)
        self.assertIn("--month-preview-limit", stdout)
        self.assertIn("--output", stdout)
        self.assertNotIn("--media", stdout)
        self.assertNotIn("--date-source", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_thumbnail_paths_and_existing_url_use_current_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            original = target / "2024" / "01" / "image.png"
            write_test_image(original)

            relative = Path("2024/01/image.png")
            thumb_relative = thumbnail_relative_path(relative)
            thumb_path = thumbnail_absolute_path(target, relative)

            self.assertEqual(thumb_relative, Path("thumbs/2024/01/image.jpg"))
            self.assertEqual(existing_thumbnail_url(target, relative), "2024/01/image.png")

            write_test_image(thumb_path)
            os.utime(thumb_path, ns=(original.stat().st_mtime_ns + 1_000_000, original.stat().st_mtime_ns + 1_000_000))

            self.assertTrue(thumbnail_is_current(original, thumb_path))
            self.assertEqual(existing_thumbnail_url(target, relative), "thumbs/2024/01/image.jpg")

    def test_existing_thumbnail_url_falls_back_when_thumbnail_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            original = target / "2024" / "01" / "image.jpg"
            write_test_image(original)
            relative = Path("2024/01/image.jpg")
            thumb_path = thumbnail_absolute_path(target, relative)
            write_test_image(thumb_path)
            os.utime(thumb_path, ns=(original.stat().st_mtime_ns - 1_000_000, original.stat().st_mtime_ns - 1_000_000))

            self.assertFalse(thumbnail_is_current(original, thumb_path))
            self.assertEqual(existing_thumbnail_url(target, relative), "2024/01/image.jpg")

    def test_make_thumbnails_continues_after_corrupt_file_and_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            good = target / "2024" / "01" / "good.jpg"
            bad = target / "2024" / "01" / "bad.jpg"
            write_test_image(good)
            bad.write_bytes(b"not a real image")
            register_target_file(target, Path("2024/01/good.jpg"))
            register_target_file(target, Path("2024/01/bad.jpg"))

            code, stdout, stderr = capture_cli(["--target", str(target), "make-thumbnails"])

            self.assertEqual(code, 2, stderr)
            self.assertIn("feil=1", stdout)
            self.assertTrue(thumbnail_absolute_path(target, Path("2024/01/good.jpg")).is_file())

    def test_make_thumbnails_shows_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            image = target / "2024" / "01" / "image.jpg"
            write_test_image(image)
            register_target_file(target, Path("2024/01/image.jpg"))

            code, stdout, stderr = capture_cli(["--target", str(target), "make-thumbnails"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Thumbnails: 1 filer skal kontrolleres.", stdout)
        self.assertIn("Thumbnails: kontrollert=1/1", stdout)
        self.assertIn("Thumbnails: ferdig kontrollert 1/1 filer.", stdout)

    def test_make_browser_writes_thumbnail_src_when_thumbnail_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            original = target / "2024" / "01" / "image.jpg"
            write_test_image(original)
            register_target_file(target, Path("2024/01/image.jpg"))
            thumb_path = thumbnail_absolute_path(target, Path("2024/01/image.jpg"))
            write_test_image(thumb_path)
            os.utime(thumb_path, ns=(original.stat().st_mtime_ns + 1_000_000, original.stat().st_mtime_ns + 1_000_000))

            code, stdout, stderr = capture_cli(["--target", str(target), "make-browser"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser", stdout)
            html = (target / "index.html").read_text(encoding="utf-8")
            self.assertIn('"thumbnailSrc": "thumbs/2024/01/image.jpg"', html)
            self.assertIn("item.thumbnailSrc || item.url", html)

    def test_server_month_uses_current_thumbnail_via_file_thumbs_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            original = target / "2024" / "01" / "image.jpg"
            write_test_image(original)
            register_target_file(target, Path("2024/01/image.jpg"))
            thumb_path = thumbnail_absolute_path(target, Path("2024/01/image.jpg"))
            write_test_image(thumb_path)
            os.utime(thumb_path, ns=(original.stat().st_mtime_ns + 1_000_000, original.stat().st_mtime_ns + 1_000_000))

            items = browser_month_items(target, "2024-01")
            html = month_page_html(target, "2024-01", items)

            self.assertIn('src="/file/thumbs/2024/01/image.jpg"', html)

    def test_docs_reference_includes_make_thumbnails(self) -> None:
        reference = Path("docs/reference.md").read_text(encoding="utf-8")

        self.assertIn("[`make-thumbnails`](make-thumbnails.md)", reference)

    def test_make_conflict_browser_writes_conflict_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir()
            (source / "a" / "IMG_20240102.png").write_bytes(minimal_png(640, 480))
            (source / "b" / "IMG_20240102.png").write_bytes(minimal_png(320, 240))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                conn.execute(
                    "UPDATE files SET view_rotation_degrees = 270 WHERE stored_filename = 'IMG_20240102-1.png'"
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "make-conflict-browser"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser for navnekollisjoner", stdout)
            html = (target / "name-conflicts.html").read_text(encoding="utf-8")
            self.assertIn("<title>Navnekollisjoner</title>", html)
            self.assertIn('"originalFilename": "IMG_20240102.png"', html)
            self.assertIn('"storedFilename": "IMG_20240102-1.png"', html)
            self.assertIn('"dimensions": "640x480"', html)
            self.assertIn('"dimensions": "320x240"', html)
            self.assertIn('"sourceExists": true', html)
            self.assertIn('"viewRotation": 270', html)
            self.assertIn('applyImageViewRotation(img, item, "contain");', html)
            self.assertNotIn('id="position"', html)
            self.assertNotIn("positionEl", html)

            custom_output = root / "conflicts.html"
            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "make-conflict-browser",
                    "-o",
                    str(custom_output),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser for navnekollisjoner", stdout)
            self.assertTrue(custom_output.exists())

    def test_make_conflict_browser_requires_target_lock_before_writing_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            with patch("bildebank.cli.recover_pending_file_moves"):
                code, stdout, stderr = capture_cli(["--target", str(target), "make-conflict-browser"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertFalse((target / "name-conflicts.html").exists())

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
            self.assertIn("Ny schema_version: 13", stdout)
            self.assertIn("Vil opprette tabellen file_sources.", stdout)
            self.assertIn("  importerte filer: 1", stdout)
            self.assertIn("  duplikatfunn: 1", stdout)
            self.assertIn("  bygge om files uten gamle v1-kildekolonner", stdout)
            self.assertIn("  fjerne legacy-tabellen duplicate_findings", stdout)
            self.assertIn("Ingen endringer er gjort (--check).", stdout)
            self.assertFalse(list(target.glob(".bilder.sqlite3.backup-before-schema-13-*")))
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
            self.assertEqual(len(list(target.glob(".bilder.sqlite3.backup-before-schema-13-*"))), 1)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "13",
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
            self.assertIn("Kildefilforekomster: 2", stdout)
            self.assertIn("Duplikatkilder: 1", stdout)

    def test_report_prints_status_merge_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "report"])

        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "report er slått sammen med status\n")

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
            self.assertIn("Ny schema_version: 13", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "13",
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
            self.assertIn("Nåværende schema_version: 13", stdout)
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
            self.assertIn("reparere intern v13-struktur", stdout)
            self.assertIn("systemtaggen", stdout)
            self.assertIn("meta.collection_id", stdout)
            self.assertIn("idx_file_tags_tag_id_file_id", stdout)
            self.assertIn("Ingen endringer er gjort (--check).", stdout)
            self.assertEqual(database_path.read_bytes(), before)
            self.assertEqual(database_path.stat().st_mtime_ns, before_mtime)
            self.assertFalse(list(target.glob(".bilder.sqlite3.backup-before-schema-13-*")))

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
            self.assertIn("Reparerer intern v13-struktur.", stdout)
            self.assertEqual(len(list(target.glob(".bilder.sqlite3.backup-before-schema-13-*"))), 1)
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
            self.assertIn("Ny schema_version: 13", stdout)
            self.assertIn("Legger til kamerakolonner i files.", stdout)
            self.assertIn("refresh-metadata --rescan", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
                self.assertIn("camera_make", columns)
                self.assertIn("camera_model", columns)
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                    "13",
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
            self.assertIn("Ny schema_version: 13", stdout)
            self.assertIn("Oppretter pending_file_deletes.", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta WHERE key = 'schema_version'"
                    ).fetchone()[0],
                    "13",
                )
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(pending_file_deletes)")
                }
            finally:
                conn.close()
            self.assertIn("last_error", columns)

    def test_migrate_v12_to_v13_adds_metadata_datetime_without_backfill(self) -> None:
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
            self.assertIn("Ny schema_version: 13", stdout)
            self.assertIn("Legger til metadata_datetime i files.", stdout)
            self.assertIn("refresh-metadata --rescan", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
                self.assertIn("metadata_datetime", columns)
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                    "13",
                )
                self.assertIsNone(conn.execute("SELECT metadata_datetime FROM files").fetchone()[0])
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
            self.assertIn("Ny schema_version: 13", stdout)
            self.assertIn("Fyller h3_res10 og h3_res11", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                    "13",
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
            self.assertIn("Ny schema_version: 13", stdout)
            self.assertIn("Rydder gamle GPS-feilmeldinger.", stdout)
            self.assertIn("bildebank vacuum", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "13",
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

    def test_vacuum_packs_current_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "vacuum"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Database:", stdout)
            self.assertIn("Størrelse før:", stdout)
            self.assertIn("Størrelse etter:", stdout)
            self.assertIn("Ferdig. Databasen er pakket.", stdout)

    def test_current_schema_rejects_v11_database_with_absolute_target_path(self) -> None:
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
                db.create_pending_file_deletes_schema(conn)
                db.create_pending_file_moves_schema(conn)
                conn.execute("UPDATE meta SET value = '13' WHERE key = 'schema_version'")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 1)
            self.assertIn("absolutt target_path", stderr)
            self.assertIn("bildebank migrate", stderr)

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
            self.assertIn("Ny schema_version: 13", stdout)
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
            self.assertEqual(len(list(target.glob(".bilder.sqlite3.backup-before-schema-13-*"))), 1)
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
            self.assertIn("schema_version=13", stderr)
            self.assertIn("bildebank migrate", stderr)


class ExportPersonTests(unittest.TestCase):
    def make_collection(self, root: Path) -> tuple[Path, AppConfig, dict[str, int]]:
        target = root / "target"
        init_database(target)
        files = {
            "confirmed": Path("2024/01/same.jpg"),
            "manual": Path("2024/02/same.jpg"),
            "suggested": Path("udatert/suggested.jpg"),
            "hidden": Path("2024/03/hidden.jpg"),
            "motion": Path("2024/04/PXL.mp4"),
            "motion_partner": Path("2024/04/PXL.MP.jpg"),
            "deleted": Path("2024/05/deleted.jpg"),
        }
        ids: dict[str, int] = {}
        for index, (name, relative_path) in enumerate(files.items(), start=1):
            path = target / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"image-{name}".encode())
            os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
            ids[name] = register_target_file(target, relative_path)

        conn = db.connect(target)
        try:
            conn.execute(
                """
                UPDATE files
                SET stored_filename = 'same.jpg',
                    manual_date_from = '2024-01-01',
                    manual_date_to = '2024-01-31'
                WHERE id = ?
                """,
                (ids["manual"],),
            )
            conn.execute(
                "UPDATE files SET taken_date = NULL, date_source = 'none' WHERE id = ?",
                (ids["suggested"],),
            )
            db.tag_file(conn, file_id=ids["hidden"], tag_name=db.SYSTEM_TAG_OUT_OF_FOCUS)
            conn.execute(
                "UPDATE files SET original_filename = 'PXL.MP', stored_filename = 'PXL.mp4' WHERE id = ?",
                (ids["motion"],),
            )
            conn.execute(
                "UPDATE files SET original_filename = 'PXL.MP.jpg' WHERE id = ?",
                (ids["motion_partner"],),
            )
            conn.execute(
                "UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
                (ids["deleted"],),
            )
            conn.commit()
        finally:
            conn.close()

        face_conn = connect_face_db(target)
        try:
            face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
            for face_id, name in enumerate(
                ("confirmed", "suggested", "hidden", "motion", "deleted"),
                start=1,
            ):
                file_id = ids[name]
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    ) VALUES(?, ?, ?, 1, 1, 10, 10, 0.9, 'test', ?)
                    """,
                    (face_id, file_id, f"key-{file_id}", f"embedding-{face_id}".encode()),
                )
            face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
            face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 1, 0.99)")
            face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 2, 0.90)")
            face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 3)")
            face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 4)")
            face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 5)")
            face_conn.execute(
                "INSERT INTO person_files(person_id, file_id) VALUES(1, ?)",
                (ids["manual"],),
            )
            face_conn.commit()
        finally:
            face_conn.close()

        config = AppConfig(
            face_recognition=FaceRecognitionConfig(enabled=True),
            browser=BrowserConfig(hide_out_of_focus=True),
        )
        return target, config, ids

    def test_export_person_uses_browser_selection_dates_collisions_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, ids = self.make_collection(root)
            destination_root = root / "exports"
            destination_root.mkdir()
            conn = db.connect(target)
            try:
                conn.execute(
                    """
                    UPDATE files
                    SET stored_filename = 'foreslått bilde.jpg',
                        manual_date_note = 'Omtrent januar'
                    WHERE id = ?
                    """,
                    (ids["suggested"],),
                )
                conn.execute(
                    "UPDATE files SET view_rotation_degrees = 180 WHERE id = ?",
                    (ids["confirmed"],),
                )
                conn.commit()
            finally:
                conn.close()

            source_hash = sha256_file(target / "2024/01/same.jpg")
            plan = export_person(target, "Kari", destination_root, config=config)

            self.assertEqual(len(plan.entries), 3)
            exported = destination_root / "Kari"
            self.assertEqual((exported / "2024/01/same.jpg").read_bytes(), b"image-confirmed")
            self.assertEqual(sha256_file(exported / "2024/01/same.jpg"), source_hash)
            self.assertEqual((exported / "2024/01/same-1.jpg").read_bytes(), b"image-manual")
            self.assertEqual(
                (exported / "udatert/foreslått bilde.jpg").read_bytes(),
                b"image-suggested",
            )
            self.assertFalse((exported / "2024/03/hidden.jpg").exists())
            self.assertFalse((exported / "2024/04/PXL.mp4").exists())
            self.assertFalse((exported / "2024/05/deleted.jpg").exists())
            source_mtime = (target / "2024/01/same.jpg").stat().st_mtime_ns
            self.assertEqual((exported / "2024/01/same.jpg").stat().st_mtime_ns, source_mtime)
            html = (exported / "index.html").read_text(encoding="utf-8")
            self.assertIn("<title>Kari</title>", html)
            self.assertIn('<nav class="breadcrumb" aria-label="Plassering"><span>År</span></nav>', html)
            self.assertIn("function renderYears()", html)
            self.assertIn('"01": "Januar"', html)
            self.assertIn('"path": "2024/01/same.jpg"', html)
            self.assertIn('"path": "2024/01/same-1.jpg"', html)
            self.assertIn('"url": "udatert/foresl%C3%A5tt%20bilde.jpg"', html)
            self.assertIn('"thumbnailSrc": "udatert/foresl%C3%A5tt%20bilde.jpg"', html)
            self.assertIn('"monthKey": "udatert"', html)
            self.assertIn('"browserDate": "9999-99-99"', html)
            self.assertIn('"manualDateFrom": "2024-01-01"', html)
            self.assertIn('"manualDateTo": "2024-01-31"', html)
            self.assertIn('"dateText": "ca. 2024-01-16 (manuell dato)"', html)
            self.assertIn('"kind": "image"', html)
            self.assertIn('"viewRotation": 180', html)
            self.assertIn('"sizeText":', html)
            self.assertNotIn("hidden.jpg", html)
            self.assertNotIn("PXL.mp4", html)
            self.assertNotIn("deleted.jpg", html)
            self.assertNotIn(str(target), html)
            self.assertFalse((target / LOCK_FILENAME).exists())

    def test_export_person_dry_run_and_cli_output_create_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, _ids = self.make_collection(root)
            destination_root = root / "exports"
            destination_root.mkdir()

            plan = export_person(target, "Kari", destination_root, config=config, dry_run=True)

            self.assertEqual(len(plan.entries), 3)
            self.assertFalse((destination_root / "Kari").exists())
            self.assertEqual(list(destination_root.iterdir()), [])

            with patch("bildebank.cli.load_config", return_value=config):
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "export-person",
                        "Kari",
                        "--dest",
                        str(destination_root),
                        "--dry-run",
                    ]
                )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(stdout.count(" -> "), 3)
            self.assertIn("Antall bilder: 3", stdout)
            self.assertNotIn("index.html", stdout)
            self.assertFalse((destination_root / "Kari").exists())

            with patch("bildebank.cli.load_config", return_value=config):
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "export-person",
                        "Kari",
                        "--dest",
                        str(destination_root),
                    ]
                )
            self.assertEqual(code, 0, stderr)
            self.assertIn(f"Statisk browser: {destination_root / 'Kari' / 'index.html'}", stdout)
            self.assertTrue((destination_root / "Kari" / "index.html").is_file())

    def test_export_person_rejects_invalid_inputs_and_keeps_failed_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, _ids = self.make_collection(root)
            destination_root = root / "exports"
            destination_root.mkdir()

            with self.assertRaisesRegex(ValueError, "finnes ikke"):
                export_person(target, "Kari", root / "missing", config=config, dry_run=True)
            with self.assertRaisesRegex(ValueError, "Fant ikke person"):
                export_person(target, "Ukjent", destination_root, config=config, dry_run=True)
            empty_face_conn = connect_face_db(target)
            try:
                empty_face_conn.execute("INSERT INTO persons(name) VALUES('Tom')")
                empty_face_conn.commit()
            finally:
                empty_face_conn.close()
            with self.assertRaisesRegex(ValueError, "ingen synlige bilder"):
                export_person(target, "Tom", destination_root, config=config, dry_run=True)
            self.assertFalse((destination_root / "Tom").exists())
            with self.assertRaisesRegex(ValueError, "overlappe"):
                export_person(target, "Kari", target, config=config, dry_run=True)
            (destination_root / "Kari").mkdir()
            with self.assertRaisesRegex(ValueError, "finnes allerede"):
                export_person(target, "Kari", destination_root, config=config, dry_run=True)
            with self.assertRaisesRegex(ValueError, "Windows-mappenavn"):
                validate_windows_folder_name("Kari.")
            with self.assertRaisesRegex(ValueError, "reservert"):
                validate_windows_folder_name("CON.txt")

            (destination_root / "Kari").rmdir()
            with patch("bildebank.export_person.safe_copy", side_effect=OSError("kopifeil")):
                with self.assertRaisesRegex(RuntimeError, "Ufullstendig eksport er beholdt"):
                    export_person(target, "Kari", destination_root, config=config)
            self.assertFalse((destination_root / "Kari").exists())
            incomplete = list(destination_root.glob(".bildebank-export-person-Kari-incomplete-*"))
            self.assertEqual(len(incomplete), 1)

            incomplete[0].rename(root / "failed-export")
            with patch("bildebank.export_person.write_export_browser", side_effect=OSError("skrivefeil")):
                with self.assertRaisesRegex(RuntimeError, "Ufullstendig eksport er beholdt"):
                    export_person(target, "Kari", destination_root, config=config)
            self.assertFalse((destination_root / "Kari").exists())
            html_incomplete = list(destination_root.glob(".bildebank-export-person-Kari-incomplete-*"))
            self.assertEqual(len(html_incomplete), 1)
            self.assertEqual(len(list(html_incomplete[0].rglob("*.jpg"))), 3)

            html_incomplete[0].rename(root / "failed-html-export")
            with patch("bildebank.export_person.safe_copy", side_effect=KeyboardInterrupt):
                with self.assertRaisesRegex(PersonExportInterrupted, "Ufullstendig eksport er beholdt"):
                    export_person(target, "Kari", destination_root, config=config)
            self.assertFalse((destination_root / "Kari").exists())

    def test_export_person_retries_transient_windows_directory_rename_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            temporary = root / ".incomplete"
            destination = root / "Anders"
            temporary.mkdir()
            real_rename = Path.rename
            attempts = 0

            def transient_rename(path: Path, target: Path) -> Path:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    error = PermissionError(13, "Ingen tilgang", str(path), 5)
                    error.winerror = 5
                    raise error
                return real_rename(path, target)

            with (
                patch("pathlib.Path.rename", transient_rename),
                patch("bildebank.export_person.time.sleep") as sleep,
            ):
                finalize_export_directory(temporary, destination)

            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertTrue(destination.is_dir())
            self.assertFalse(temporary.exists())

    def test_export_person_parser_help_and_reference(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["export-person", "Kari", "--dest", r"D:\Eksport", "--dry-run"])
        self.assertEqual(args.command, "export-person")
        self.assertEqual(args.name, "Kari")
        self.assertTrue(args.dry_run)
        self.assertIn("export-person", parser.format_help())
        reference = Path("docs/reference.md").read_text(encoding="utf-8")
        self.assertIn("[`export-person`](export-person.md)", reference)


if __name__ == "__main__":
    unittest.main()
