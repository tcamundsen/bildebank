from __future__ import annotations

import json
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import unittest
import datetime as dt
import os
import sys
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

from bildebank.cli import build_parser, main, print_image_search_progress
from bildebank.config import AppConfig, BrowserConfig, FaceRecognitionConfig, OpenClipConfig, load_config
from bildebank import db
from bildebank.db import DB_FILENAME, init_database
from bildebank.exiftool import managed_exiftool_path, resolve_exiftool_path
from bildebank.face import (
    apply_face_schema,
    connect_face_db,
    face_box_percent,
    face_db_path,
    normalize_insightface_model_layout,
    read_image,
    remove_insightface_model_zip,
)
from bildebank.geo import h3_cells_for_manual_cell, h3_cells_for_point
from bildebank.html_export import render_html
from bildebank.importer import safe_copy
from bildebank.media import ImageDimensions, sha256_file
from bildebank.media_cache import cached_image_dimensions, cached_image_orientation
from bildebank.openclip import ImageSearchResult, connect_openclip_db, embedding_blob, openclip_db_path, resolve_torch_device
from bildebank.program_state import PROGRAM_DB_FILENAME, ensure_schema, known_targets, record_target
from bildebank.server_actions import undelete_file_from_browser
from bildebank.server_assets import SERVER_ASSET_VERSION, SERVER_CSS, SERVER_JS
from bildebank.server import (
    BildebankServer,
    BildebankRequestHandler,
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
    source_item_page_html,
    source_month_page_html,
    sources_page_html,
    tags_page_html,
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
    date_source_text,
    image_info_content_html,
    person_item_by_id,
    person_month_items,
    person_month_navigation,
    source_item_by_id,
    source_month_items,
    source_month_navigation,
    source_summary_rows,
    valid_year_key,
)
from bildebank.server_browser_sources import (
    all_browser_source,
    date_source_browser_source,
    imported_source_browser_source,
    person_browser_source,
    tag_browser_source,
)
from bildebank.server_faces import cached_person_file_ids, face_overlay_content_html, person_file_ids, person_items
from bildebank.server_geo import geo_component_pixel_coordinates
from bildebank.server_search import (
    DEFAULT_SEARCH_LIMIT,
    OpenClipSearchCache,
    ServerSearchStats,
    load_search_embedding_cache,
    search_server_images,
)
from bildebank.target_lock import LOCK_FILENAME
from bildebank.thumbnails import (
    existing_thumbnail_url,
    thumbnail_absolute_path,
    thumbnail_is_current,
    thumbnail_relative_path,
)
from tests.test_media import (
    jpeg_with_xmp_date,
    minimal_avi_with_creation_date,
    minimal_avi_with_idit_outside_info,
    minimal_mp4_with_creation_date,
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
        for command in ("face-group", "face-person-add-group"):
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

    def test_make_face_browser_help_marks_command_as_debug(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["make-face-browser", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("Debug", stdout)
        self.assertIn("--limit", stdout)
        self.assertIn("ikke ment for vanlig bruk", stdout.lower())
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
        self.assertEqual(stderr_buffer.getvalue(), "")

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

        self.assertIn('<header class="browser-header">', body)
        self.assertIn('href="/">Alle bilder</a>', body)
        self.assertIn('href="/geo">Steder</a>', body)
        self.assertIn('href="/sources">Kilder</a>', body)
        self.assertNotIn('href="/people">Personer</a>', body)
        self.assertNotIn('href="/search">Bildesøk</a>', body)

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

    def test_run_server_image_search_uses_target_path_for_image_url(self) -> None:
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
        self.assertIn('href="/file/2025/07/PXL%2020250709_193516074.jpg"', body)
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
                browser=BrowserConfig(hide_out_of_focus=True, manual_h3_cell="872830828ffffff"),
            )

            with (
                patch("bildebank.server_app.module_available", side_effect=lambda name: name == "open_clip"),
            ):
                body = app_status_page_html(target, config)

        self.assertIn("<h1>Innstillinger</h1>", body)
        self.assertIn("Bildebank-versjon", body)
        self.assertIn("Bildesamling", body)
        self.assertLess(body.index("Bildesamling"), body.index("Skjul bilder tagget"))
        self.assertLess(body.index("Skjul bilder tagget"), body.index("Bildebank-versjon"))
        self.assertIn('action="/settings/hide-out-of-focus"', body)
        self.assertIn('action="/settings/manual-h3-cell"', body)
        self.assertIn('<option value="872830828ffffff" selected>Ikke navngitt: 872830828ffffff</option>', body)
        self.assertIn("Aktiv manuell H3-celle", body)
        self.assertIn('<span class="app-toggle-status">På</span>', body)
        self.assertIn(str(target), body)
        self.assertIn("InsightFace aktivert", body)
        self.assertIn('action="/settings/face-config"', body)
        self.assertIn('name="enabled" value="true" checked', body)
        self.assertIn("InsightFace-modell", body)
        self.assertIn('action="/settings/face-model"', body)
        self.assertIn('<option value="antelopev2">antelopev2</option>', body)
        self.assertIn('<option value="buffalo_l" selected>buffalo_l</option>', body)
        self.assertIn("må installeres for å scanne ansikter i nye bilder.", body)
        self.assertNotIn("app-toggle-submit", body)
        self.assertIn("<dd>ja</dd>", body)
        self.assertIn("InsightFace installert", body)
        self.assertIn("OpenCLIP aktivert", body)
        self.assertIn('href="/date-source/filename">Dato fra filnavn</a>', body)
        self.assertIn('href="/date-source/mtime">Dato fra mtime</a>', body)
        self.assertIn("OpenCLIP tilgjengelig", body)
        self.assertIn("Test-Model", body)
        self.assertIn("test-weights", body)
        self.assertIn("cpu", body)

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
            deleted_path.write_bytes(minimal_png(100, 80))
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

    def test_run_server_app_status_manual_h3_status_shows_named_cell(self) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = db.connect(target)
            try:
                db.set_geo_place_name(conn, h3_cell, "Oslo")
                conn.commit()
            finally:
                conn.close()
            config = AppConfig(browser=BrowserConfig(manual_h3_cell=h3_cell))

            body = app_status_page_html(target, config)

        self.assertIn(f'<option value="{h3_cell}" selected>Oslo (H3-7)</option>', body)
        self.assertIn('<span class="app-toggle-status">Oslo</span>', body)
        self.assertNotIn(f'<span class="app-toggle-status">{h3_cell}</span>', body)

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

    def test_run_server_manual_h3_cell_post_updates_config(self) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = f"h3_cell={h3_cell}".encode("utf-8")

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
                BildebankRequestHandler.respond_set_manual_h3_cell(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertEqual(config.browser.manual_h3_cell, h3_cell)
        self.assertEqual(handler.server.config.browser.manual_h3_cell, h3_cell)
        self.assertEqual(handler.location, "/settings")

    def test_run_server_manual_h3_cell_post_rejects_invalid_cell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"h3_cell=ikke-h3"
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
                BildebankRequestHandler.respond_set_manual_h3_cell(handler)  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("Ugyldig H3-celle", str(response["content"]))

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
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            item = browser_item_by_id(target, 2)
            self.assertIsNotNone(item)
            previous_item, next_item = adjacent_browser_items(target, item)
            body = item_page_html(target, item, previous_item, next_item, browser_month_navigation(target, item))

        self.assertIn("Bildebrowser", body)
        self.assertIn("/file/2", body)
        self.assertIn("/month/2023-12", body)
        self.assertIn("/month/2024-02", body)
        self.assertIn("/month/2025-01", body)
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
        self.assertIn('href="/static/server.css?v=', body)
        self.assertIn('src="/static/server.js?v=', body)
        self.assertIn("ArrowLeft", SERVER_JS)
        self.assertIn("ArrowRight", SERVER_JS)
        self.assertIn("ArrowUp", SERVER_JS)
        self.assertIn("ArrowDown", SERVER_JS)
        self.assertIn("PageUp", SERVER_JS)
        self.assertIn("PageDown", SERVER_JS)

    def test_run_server_top_steder_link_points_to_geo_not_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))

        self.assertIn('<a class="server-search-link" href="/geo">Steder</a>', body)
        self.assertLess(body.index('href="/geo">Steder'), body.index('href="/search">Bildesøk'))

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
            missing_body = geo_missing_page_html(target, limit=10, offset=0)

        self.assertIn("Steder", index_body)
        self.assertIn("/geo/map?resolution=7&min_count=1&limit=10", index_body)
        self.assertIn("Heksagonkart", map_body)
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
        self.assertIn('href="https://www.google.com/maps/search/?api=1&amp;query=59.9127300,10.7460900"', area_body)
        self.assertIn("Åpne i Google Maps", area_body)
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
            "Forrige år",
            "Neste år",
            "Forrige måned",
            "Neste måned",
            "Forrige bilde",
            "Neste bilde",
        ):
            self.assertIn(label, body)
        self.assertIn("/month/2023-12", body)
        self.assertIn("/month/2024-02", body)
        self.assertIn("/month/2025-01", body)
        self.assertIn('data-key-nav="previous"', body)
        self.assertIn('data-key-nav="next"', body)
        self.assertIn('data-key-nav="previous-year"', body)
        self.assertIn('data-key-nav="next-year"', body)
        self.assertIn('data-key-nav="previous-month"', body)
        self.assertIn('data-key-nav="next-month"', body)

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
        self.assertIn(">2005</div>", years_body)
        self.assertIn(">3 måneder</div>", years_body)
        self.assertNotIn('href="/years/2006"', years_body)
        self.assertIn('href="/years/2007"', years_body)
        self.assertIn('src="/file/2005/03/IMG_20050302.jpg"', years_body)
        self.assertNotIn("Video<br>IMG_20050301.mp4", years_body)
        self.assertIn('href="/month/2005-03"', year_body)
        self.assertIn('href="/month/2005-04"', year_body)
        self.assertIn('href="/month/2005-05"', year_body)
        self.assertIn('src="/file/2005/03/IMG_20050302.jpg"', year_body)
        self.assertIn(">2005-04</div>", year_body)
        self.assertIn(">1 bilde</div>", year_body)
        self.assertNotIn('href="/years/2007"', filtered_years_body)
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

    def test_server_month_thumbnails_clip_rotated_images(self) -> None:
        self.assertIn(".thumb-link {", SERVER_CSS)
        self.assertIn("aspect-ratio: 4 / 3;", SERVER_CSS)
        self.assertIn("overflow: hidden;", SERVER_CSS)
        self.assertEqual(SERVER_ASSET_VERSION, "3")

    def test_static_browser_sorts_by_taken_date_inside_month(self) -> None:
        html = render_html([], month_preview_limit=None)
        compare = html[html.index("function compareItems") : html.index("function buildMonths")]

        month_order = compare.index("a.monthKey.localeCompare")
        date_order = compare.index("aDate.localeCompare")
        path_order = compare.index("a.path.localeCompare")

        self.assertLess(month_order, date_order)
        self.assertLess(date_order, path_order)

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

        self.assertLess(body.index("Neste bilde"), body.index("Bildeinfo"))
        self.assertIn('data-open-info', body)
        self.assertIn('data-info-item="1"', body)
        self.assertIn('id="infoOverlay"', body)
        self.assertIn("/api/item-info?file_id=", SERVER_JS)
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

    def test_run_server_item_page_has_manual_location_button_when_configured(self) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                manual_h3_cell=h3_cell,
            )

        self.assertIn("Sett sted", body)
        self.assertIn('data-manual-location-item="1"', body)
        self.assertIn(f'data-manual-location-cell="{h3_cell}"', body)
        self.assertIn("/api/item-manual-location", SERVER_JS)
        self.assertIn('event.key.toLowerCase() === "g"', SERVER_JS)
        self.assertIn("setManualLocation(button)", SERVER_JS)
        self.assertNotIn("Sette sted fra aktiv H3-celle?", SERVER_JS)

    def test_run_server_item_manual_location_endpoint_sets_h3_location(self) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(browser=BrowserConfig(manual_h3_cell=h3_cell)))
                body: dict[str, object] | None = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_manual_location_item(handler)  # type: ignore[arg-type]

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

        self.assertEqual(handler.body, {"ok": True, "file_id": 1, "gps_source": "manual-h3"})
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
        self.assertIn(
            f'<div class="location-status-badge"><a href="https://h3geo.org/#hex={h3_cell}" target="_blank" rel="noopener">Manuell H3</a></div>',
            body,
        )
        self.assertNotIn("<dt>Kart</dt>", info_body)
        self.assertIn("Manuell H3", info_body)
        self.assertIn(f'href="https://h3geo.org/#hex={h3_cell}"', info_body)
        self.assertLess(info_body.index("<dt>Tagger</dt>"), info_body.index("Manuell H3"))
        self.assertIn("<dt>GPS-kilde</dt>", info_body)
        self.assertIn("satt manuelt", info_body)

    def test_run_server_item_manual_location_endpoint_rejects_unknown_file(self) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            data = json.dumps({"file_id": 99}).encode("utf-8")
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(browser=BrowserConfig(manual_h3_cell=h3_cell)))

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_manual_location_item(handler)  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("Filen finnes ikke", str(response["content"]))

    def test_run_server_item_manual_location_endpoint_rejects_invalid_h3_cell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            data = json.dumps({"file_id": 1}).encode("utf-8")
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(browser=BrowserConfig(manual_h3_cell="ikke-h3")))

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_manual_location_item(handler)  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("Ugyldig H3-celle", str(response["content"]))

    def test_run_server_item_manual_location_endpoint_rejects_file_with_gps(self) -> None:
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
                db.update_file_gps(
                    conn,
                    file_id=1,
                    gps_lat=59.91273,
                    gps_lon=10.74609,
                    gps_alt=None,
                    h3_cells=h3_cells_for_point(59.91273, 10.74609),
                    gps_source="exiftool",
                    gps_error=None,
                )
                conn.commit()
            finally:
                conn.close()
            data = json.dumps({"file_id": 1}).encode("utf-8")
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target, config=AppConfig(browser=BrowserConfig(manual_h3_cell=h3_cell)))

                def respond_json(self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_manual_location_item(handler)  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("har GPS-lokasjon", str(response["content"]))

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

    def test_run_server_item_page_has_system_tag_buttons(self) -> None:
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

        self.assertIn('class="tag-rail"', body)
        self.assertIn('data-tag-toggle="1"', body)
        self.assertIn('data-tag-name="Ute av fokus"', body)
        self.assertIn('aria-pressed="false"', body)
        self.assertIn("/api/item-tag", SERVER_JS)
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

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.body = content

            handler = FakeHandler()
            BildebankRequestHandler.respond_tag_item(handler)  # type: ignore[arg-type]
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))
            info_body = image_info_content_html(target, item)

        self.assertEqual({"ok": True, "file_id": 1, "tag_name": db.SYSTEM_TAG_OUT_OF_FOCUS, "tagged": True}, handler.body)
        self.assertIn('class="tag-toggle active"', body)
        self.assertIn('aria-pressed="true"', body)
        self.assertIn("Ute av fokus", info_body)
        self.assertIn("(system)", info_body)

    def test_run_server_item_tag_endpoint_rejects_user_tag(self) -> None:
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

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIs(handler.body["ok"], False)
        self.assertIn("systemtagger", str(handler.body["error"]))

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
        self.assertIn("Roter venstre", body)
        self.assertIn("Roter høyre", body)
        self.assertIn('data-rotate-direction="left"', body)
        self.assertIn('data-rotate-direction="right"', body)
        self.assertIn("transform: rotate(90deg)", body)
        self.assertIn("transform: rotate(90deg)", month_body)

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

        self.assertNotIn("Roter venstre", body)
        self.assertNotIn("Roter høyre", body)

    def test_run_server_date_source_browser_reuses_source_pages(self) -> None:
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
                conn.execute("UPDATE files SET date_source = 'filename' WHERE id = 1")
                conn.execute("UPDATE files SET date_source = 'mtime' WHERE id = 2")
                conn.execute("UPDATE files SET date_source = 'filename' WHERE id = 3")
                conn.commit()
            finally:
                conn.close()

            filename_source = date_source_browser_source("filename")
            mtime_source = date_source_browser_source("mtime")
            with patch("bildebank.server_browser.source_items", side_effect=AssertionError("source_items should not be used")):
                filename_item = source_item_by_id(target, filename_source, 1)
                mtime_item = source_item_by_id(target, mtime_source, 2)
                self.assertIsNotNone(filename_item)
                self.assertIsNotNone(mtime_item)
                filename_adjacent = adjacent_source_items(target, filename_source, filename_item)
                filename_month_navigation = source_month_navigation(target, filename_source, filename_item)
                mtime_month_items = source_month_items(target, mtime_source, "2024-01")
                filename_excludes_mtime_item = source_item_by_id(target, filename_source, 2) is None
            self.assertIsNotNone(filename_item)
            self.assertIsNotNone(mtime_item)
            filename_body = source_item_page_html(
                target,
                filename_source,
                filename_item,
                *filename_adjacent,
                filename_month_navigation,
            )
            mtime_month_body = source_month_page_html(
                target,
                mtime_source,
                "2024-01",
                mtime_month_items,
            )
            all_month_disabled_body = source_month_page_html(
                target,
                all_browser_source(),
                "2024-01",
                browser_month_items(target, "2024-01"),
                face_enabled=False,
                openclip_enabled=False,
            )
            empty_source_disabled_body = empty_source_html(filename_source, face_enabled=False, openclip_enabled=False)

        self.assertIn("Dato fra filnavn", filename_body)
        self.assertIn("/date-source/filename/item/3", filename_body)
        self.assertIn('href="/item/1">Alle bilder</a>', filename_body)
        self.assertTrue(filename_excludes_mtime_item)
        self.assertIn("Dato fra mtime", mtime_month_body)
        self.assertIn("/date-source/mtime/item/2", mtime_month_body)
        self.assertNotIn("/date-source/mtime/item/1", mtime_month_body)
        self.assertNotIn('href="/people"', all_month_disabled_body)
        self.assertNotIn('href="/search"', all_month_disabled_body)
        self.assertIn('href="/">Alle bilder</a>', empty_source_disabled_body)
        self.assertNotIn('href="/people"', empty_source_disabled_body)
        self.assertNotIn('href="/search"', empty_source_disabled_body)

    def test_run_server_source_browser_reuses_source_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source_a = Path(tmp) / "source-a"
            source_b = Path(tmp) / "source-b"
            source_a.mkdir()
            source_b.mkdir()
            (source_a / "IMG_20240102.jpg").write_bytes(b"image-a")
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
                "bildebank.server_browser.adjacent_sql_filtered_source_items",
                side_effect=AssertionError("SQL filter path should not be used"),
            ):
                source_item = source_item_by_id(target, source_browser, 1)
                source_excludes_other_item = source_item_by_id(target, source_browser, 2) is None
                self.assertIsNotNone(source_item)
                source_adjacent = adjacent_source_items(target, source_browser, source_item)
                source_month_nav = source_month_navigation(target, source_browser, source_item)
                source_month = source_month_items(target, source_browser, "2024-01")
            self.assertIsNotNone(source_item)
            item_body = source_item_page_html(
                target,
                source_browser,
                source_item,
                *source_adjacent,
                source_month_nav,
            )
            month_body = source_month_page_html(target, source_browser, "2024-01", source_month)
            sources_body = sources_page_html(target)
            summaries = source_summary_rows(target)

        self.assertTrue(source_excludes_other_item)
        self.assertEqual(len(source_month), 1)
        self.assertEqual(len(summaries), 2)
        self.assertIn("Kilde: source-a", item_body)
        self.assertIn('href="/item/1">Alle bilder</a>', item_body)
        self.assertIn('href="/sources">Kilder</a>', item_body)
        self.assertNotIn("IMG_20240203", item_body)
        self.assertIn('href="/source/1/item/1"', month_body)
        self.assertIn("<h1>Kilder</h1>", sources_body)
        self.assertIn('href="/source/1">Vis bilder (1)</a>', sources_body)
        self.assertIn("source-a", sources_body)
        self.assertIn("source-b", sources_body)

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
        self.assertIn('href="/item/1">Alle bilder</a>', item_body)
        self.assertIn('href="/tag/familie/item/1"', month_body)
        self.assertEqual(len(tag_month), 1)
        self.assertIn("<h1>Tagger</h1>", tags_body)
        self.assertIn('href="/tag/Familie">Vis bilder (1)</a>', tags_body)
        self.assertIn("brukertagg", tags_body)
        self.assertIn("systemtagg", tags_body)

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
        self.assertIn('href="/">Synlige bilder</a>', tag_item_body)
        self.assertNotIn('href="/item/1">Synlige bilder</a>', tag_item_body)

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

    def test_system_tag_promotes_existing_user_tag(self) -> None:
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

            conn = db.connect(target)
            try:
                row = conn.execute("SELECT name, kind FROM tags WHERE name_key = ?", (db.tag_name_key(db.SYSTEM_TAG_OUT_OF_FOCUS),)).fetchone()
                linked = conn.execute("SELECT COUNT(*) AS count FROM file_tags").fetchone()
            finally:
                conn.close()

        self.assertEqual(row["name"], db.SYSTEM_TAG_OUT_OF_FOCUS)
        self.assertEqual(row["kind"], db.TAG_KIND_SYSTEM)
        self.assertEqual(int(linked["count"]), 1)

    def test_existing_tags_table_without_kind_gets_system_tag(self) -> None:
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
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(2, 2, 0.91)")
                face_conn.commit()
            finally:
                face_conn.close()

            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(target, item, *adjacent_browser_items(target, item), browser_month_navigation(target, item))
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
        self.assertIn(
            'class="person-link" href="/person/Kari/no-faces/item/1" data-person-name="Kari">'
            'Kari<span class="confirmed-badge" title="Bekreftet" aria-label="Bekreftet"> ✅</span></a>',
            body,
        )
        self.assertIn(
            'class="person-link" href="/person/Ola%20Nordmann/no-faces/item/1" data-person-name="Ola Nordmann">'
            "Ola Nordmann</a>",
            body,
        )
        self.assertIn("Ubekreftet ansikter i bildet (1)", body)
        self.assertIn('data-faces-item="1"', body)
        self.assertIn('data-face-list', body)
        self.assertNotIn("Ny person", body)
        self.assertIn("width: fit-content;", SERVER_CSS)
        self.assertIn("justify-self: start;", SERVER_CSS)
        self.assertIn("Ny person", face_body)
        self.assertIn("/api/face-person-create-and-add-face", SERVER_JS)
        self.assertIn("/api/item-faces?file_id=", SERVER_JS)
        self.assertIn("Identifiser", face_body)
        self.assertIn("Forslag:", face_body)
        self.assertIn("Ola Nordmann <strong>0.910</strong>", face_body)
        self.assertIn('data-face-id="2"', face_body)
        self.assertIn('data-person-name="Kari"', face_body)
        self.assertIn('data-person-name="Ola Nordmann"', face_body)
        self.assertNotIn('data-face-id="1"', body)
        self.assertNotIn('data-face-id="1"', face_body)
        self.assertNotIn('href="/people"', disabled_body)
        self.assertNotIn('href="/person/Kari"', disabled_body)
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
                {"ok": True, "person_name": "Kari", "person_url": "/person/Kari", "face_id": 1, "removed": True},
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(face_conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0], 0)
            finally:
                face_conn.close()

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
            server = SimpleNamespace(face_enabled=False)
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
                suggestion_count = face_conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0]
                face_count = face_conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
            finally:
                face_conn.close()

        self.assertIsNone(handler.status)
        self.assertEqual(
            {"ok": True, "person_name": "Kari", "removed_faces": 1, "removed_suggestions": 1},
            handler.body,
        )
        self.assertEqual(0, person_count)
        self.assertEqual(0, link_count)
        self.assertEqual(0, suggestion_count)
        self.assertEqual(2, face_count)

    def test_run_server_api_delete_person_is_disabled_when_faces_are_disabled(self) -> None:
        class FakeHandler:
            path = "/api/face-person-delete"
            server = SimpleNamespace(face_enabled=False)
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
            month_body = person_month_page_html(target, "Kari", "2024-02", person_month_items(target, "Kari", "2024-02"))

        self.assertIn(">Kari<", body)
        self.assertIn("/person/Kari/month/2024-02", body)
        self.assertIn('href="/item/1">Alle bilder</a>', body)
        self.assertIn('href="/person/Kari/no-faces/item/1"', body)
        self.assertIn("Uten ansiktsmarkering", body)
        self.assertIn("person-face-box", body)
        self.assertIn("bekreftet face-id 1", body)
        self.assertIn('<span class="person-face-label">face-id 1</span>', body)
        self.assertIn('<div class="person-media" style="transform: rotate(90deg);" data-view-rotation="90">', body)
        self.assertNotIn("IMG_20250104", body)
        self.assertIn("Kari - uten ansiktsmarkering", plain_body)
        self.assertIn('href="/item/1">Alle bilder</a>', plain_body)
        self.assertIn('href="/person/Kari/item/1"', plain_body)
        self.assertIn("Med ansiktsmarkering", plain_body)
        self.assertNotIn('<div class="person-face-box"', plain_body)
        self.assertNotIn('<span class="person-face-label">face-id 1</span>', plain_body)
        self.assertIn('<img src="/file/1"', plain_body)
        self.assertIn("/person/Kari/item/2", month_body)
        self.assertNotIn("/person/Kari/item/3", month_body)

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

        self.assertEqual(first_file_ids, [1, 2])
        self.assertEqual(second_file_ids, [1, 2])
        self.assertEqual(cache_after_first.misses, 1)
        self.assertEqual(cache_after_second.hits, cache_after_first.hits + 1)
        self.assertIn("<div><strong>Antall bilder i databasen</strong><span>2</span></div>", body)
        self.assertIn("<div><strong>Scannet av face-scan</strong><span>1</span></div>", body)
        self.assertIn("<div><strong>Ikke scannet av face-scan</strong><span>1</span></div>", body)
        self.assertIn("<div><strong>Ansikter funnet</strong><span>5</span></div>", body)
        self.assertIn("<div><strong>Ansikter med forslag</strong><span>3</span></div>", body)
        self.assertIn('href="/person/Kari/confirmed/no-faces"', body)
        self.assertIn('href="/person/Kari/no-faces"', body)
        self.assertIn('data-open-person-rename', body)
        self.assertIn('data-person-name="Kari"', body)
        self.assertIn("endre navn", body)
        self.assertIn('data-delete-person-name="Kari"', body)
        self.assertIn("slett person", body)
        self.assertIn('id="personRenameDialog"', body)
        self.assertIn("Bekreftede bilder (1)", body)
        self.assertIn("Bekreftede og forslag (2)", body)
        self.assertNotIn("forslag:", body)
        self.assertIn("NB: 2 bekreftede ansikter i samme bilde", body)
        self.assertIn("NB: 2 bekreftede ansikter for Kari i dette bildet", confirmed_body)
        self.assertIn('data-unconfirm-face="1"', confirmed_body)
        self.assertIn('data-unconfirm-face="3"', confirmed_body)
        self.assertIn('data-unconfirm-person="Kari"', confirmed_body)
        self.assertIn("Avbekreft face-id 1", confirmed_body)
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
                    "8",
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

    def test_opening_current_database_without_collection_id_repairs_it(self) -> None:
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

            self.assertEqual(run_cli(["--target", str(target), "status"]), 0)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                repaired_collection_id = conn.execute(
                    "SELECT value FROM meta WHERE key = 'collection_id'"
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(str(uuid.UUID(repaired_collection_id)), repaired_collection_id)

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
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("  OK: face_recognition er slått på", stdout)
        self.assertIn("  FEIL: face_recognition er slått på, men insightface mangler.", stdout)
        self.assertIn("  FEIL: face_recognition er slått på, men onnxruntime mangler.", stdout)
        self.assertIn("install-insightface.ps1", stdout)
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

            code, stdout, stderr = capture_cli(["--target", str(target), "check-source", "--quiet", str(source)])

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
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            missing.write_bytes(b"not-imported")

            code, stdout, stderr = capture_cli(["--target", str(target), "check-source", "--quiet", str(source)])

            self.assertEqual(code, 2, stderr)
            self.assertIn("scannet=2, dekket=1, mangler=1", stdout)
            self.assertIn(str(missing), stdout)
            self.assertIn("filen er ikke importert i bildesamlingen", stdout)
            self.assertIn("Kildemappen er derfor ikke trygg å slette.", stdout)
            self.assertNotIn("Remove-Item", stdout)

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

            code, stdout, stderr = capture_cli(["--target", str(target), "check-source", "--quiet", str(source)])

            self.assertEqual(code, 2, stderr)
            self.assertIn("scannet=1, dekket=0, mangler=1", stdout)

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
            self.assertIn("Ny schema_version: 8", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "8",
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

            code, stdout, stderr = capture_cli(["--target", str(target), "make-face-browser"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser for ansikter", stdout)
            html = (target / "faces.html").read_text(encoding="utf-8")
            self.assertIn("Ansikter (1 bilder)", html)
            self.assertIn("IMG_20240102.jpg", html)
            self.assertIn("Ansikt-id: 1", html)
            self.assertIn("class=\"box\"", html)
            self.assertIn("left: ", html)

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

    def test_make_face_browser_uses_relative_face_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            image_path = target / "2024" / "01" / "IMG_20240102.png"
            relative_image_path = Path("2024/01/IMG_20240102.png")

            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(minimal_png(640, 480))
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
                    ) VALUES(1, ?, 1, 2, 30, 40, 0.9, 'test', ?)
                    """,
                    (db.relative_path_key(relative_image_path), b"embedding"),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "make-face-browser", "--limit", "1"])

            self.assertEqual(code, 0, stderr)
            html = (target / "faces.html").read_text(encoding="utf-8")
            self.assertIn("2024/01/IMG_20240102.png", html)

    def test_make_face_browser_limit_restricts_number_of_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            first = target / "first.jpg"
            second = target / "second.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            config = load_config(self.program_root).face_recognition
            conn = connect_face_db(target, config)
            try:
                for file_id, path in ((1, first), (2, second)):
                    relative_path = path.relative_to(target)
                    conn.execute(
                        """
                        INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                        VALUES(?, ?, ?, ?, 'ok', 1)
                        """,
                        (file_id, relative_path.as_posix(), db.relative_path_key(relative_path), f"hash-{file_id}"),
                    )
                    conn.execute(
                        """
                        INSERT INTO faces(
                            file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                            detection_score, embedding_model, embedding
                        ) VALUES(?, ?, 1, 2, 10, 20, 0.9, 'test-model', ?)
                        """,
                        (file_id, db.relative_path_key(relative_path), b"embedding"),
                    )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "make-face-browser", "--limit", "1"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser for ansikter", stdout)
            html = (target / "faces.html").read_text(encoding="utf-8")
            self.assertIn("first.jpg", html)
            self.assertNotIn("second.jpg", html)

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

            code, stdout, stderr = capture_cli(["--target", str(target), "make-person-browser", "Kari"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser for person", stdout)
            html = (target / "person-Kari.html").read_text(encoding="utf-8")
            self.assertIn("<title>Kari</title>", html)
            self.assertIn('<div class="title">Kari</div>', html)
            self.assertIn("Forrige måned", html)
            self.assertIn("const embeddedItems", html)
            self.assertIn("IMG_20240102.jpg", html)
            self.assertIn('"kind": "image"', html)
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
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "3")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0], 0)
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
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "3")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0], 1)
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

                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "3")
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
            self.assertIn("item.sizeText", html)
            self.assertIn("const MONTH_PREVIEW_LIMIT = null;", html)
            self.assertIn('state.viewMode = "month";', html)
            self.assertIn("function representativeItems(items, limit)", html)
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
            self.assertIn("Ny schema_version: 8", stdout)
            self.assertIn("Vil opprette tabellen file_sources.", stdout)
            self.assertIn("  importerte filer: 1", stdout)
            self.assertIn("  duplikatfunn: 1", stdout)
            self.assertIn("  bygge om files uten gamle v1-kildekolonner", stdout)
            self.assertIn("  fjerne legacy-tabellen duplicate_findings", stdout)
            self.assertIn("Ingen endringer er gjort (--check).", stdout)
            self.assertFalse(list(target.glob(".bilder.sqlite3.backup-before-schema-8-*")))
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
            self.assertEqual(len(list(target.glob(".bilder.sqlite3.backup-before-schema-8-*"))), 1)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "8",
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

    def test_migrate_v5_to_v8_creates_performance_indexes(self) -> None:
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
            self.assertIn("Ny schema_version: 8", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "8",
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
            self.assertIn("Nåværende schema_version: 8", stdout)
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

    def test_migrate_v7_to_v8_adds_h3_10_11_and_backfills_existing_gps(self) -> None:
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
            self.assertIn("Ny schema_version: 8", stdout)
            self.assertIn("Fyller h3_res10 og h3_res11", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                    "8",
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

    def test_migrate_v6_to_v8_replaces_legacy_gps_error_messages(self) -> None:
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
            self.assertIn("Ny schema_version: 8", stdout)
            self.assertIn("Rydder gamle GPS-feilmeldinger.", stdout)
            self.assertIn("bildebank vacuum", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "8",
                )
                self.assertEqual(
                    conn.execute("SELECT gps_error FROM files").fetchone()[0],
                    db.GPS_ERROR_EXIFTOOL,
                )
            finally:
                conn.close()

    def test_migrate_v6_to_v8_keeps_missing_file_as_short_marker(self) -> None:
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

    def test_current_schema_rejects_v8_database_with_absolute_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            create_v4_database(target, source, imported=imported)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
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
            self.assertIn("Ny schema_version: 8", stdout)
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
            self.assertEqual(len(list(target.glob(".bilder.sqlite3.backup-before-schema-8-*"))), 1)
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
            self.assertIn("schema_version=8", stderr)
            self.assertIn("bildebank migrate", stderr)


if __name__ == "__main__":
    unittest.main()
